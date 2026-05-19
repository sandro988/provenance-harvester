"""Build repos.csv — repo-level metadata for every URL in footprint+popular.

Enriches the harvest input with deps.dev Project signals (stars, forks,
issues, license, description) and OpenSSF Scorecard scores. Output is
one row per distinct GitHub repository, joined back to the harvest
output by ``repo_url`` downstream.

This file is independent of the harvest itself — pulling stars and a
scorecard doesn't require a clone, so we generate it as a separate
CSV alongside ``footprint_resolved.csv`` and ``popular.csv``. Loaders
join the three on ``repo_url`` after the fact.

Cost & safety: two partition-pruned BigQuery scans, each ~0.5 GiB,
both under the 20 GiB ``maximum_bytes_billed`` fuse. Authentication
mirrors ``build_popular_csv_bigquery.py``: service-account JSON via
``GOOGLE_APPLICATION_CREDENTIALS``, ``cloud-platform`` scope.

Run::

    GOOGLE_APPLICATION_CREDENTIALS=~/.gcp/exodos-dev-sa.json \\
    uv run python scripts/build_repos_csv.py
"""

from __future__ import annotations

import csv
import os
import sys
import time
from pathlib import Path

from google.cloud import bigquery
from google.oauth2 import service_account

REPO_ROOT = Path(__file__).resolve().parent.parent
FOOTPRINT_CSV = REPO_ROOT / "output" / "footprint_resolved.csv"
POPULAR_CSV = REPO_ROOT / "output" / "popular.csv"
OUT_CSV = REPO_ROOT / "output" / "repos.csv"

MAX_BYTES_BILLED = 20 * 1024**3  # 20 GiB

DEPS_DEV_DATASET = "bigquery-public-data.deps_dev_v1"
SCORECARD_TABLE = "openssf.scorecardcron.scorecard-v2"

# Only the OpenSSF Scorecard overall score is surfaced as a column.
# Per-check scores live in the same dataset and can be re-derived
# downstream if needed; expanding the schema by 19 columns just to
# capture them eagerly bloats the file with rarely-used noise.


def initialize_bq_client() -> bigquery.Client:
    """Build a BigQuery client from the service account JSON.

    Same pattern as build_popular_csv_bigquery.initialize_bq_client.
    """
    credentials_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not credentials_path:
        raise RuntimeError(
            "GOOGLE_APPLICATION_CREDENTIALS not set. "
            "Export it pointing at ~/.gcp/exodos-dev-sa.json before running."
        )
    expanded = Path(credentials_path).expanduser()
    if not expanded.exists():
        raise RuntimeError(f"Credentials file not found at: {credentials_path}")

    credentials = service_account.Credentials.from_service_account_file(
        str(expanded),
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    client = bigquery.Client(credentials=credentials, project=credentials.project_id)
    print(f"[repos] BigQuery client ready (project: {credentials.project_id})", flush=True)
    return client


def collect_repo_names() -> tuple[list[str], dict[str, str]]:
    """Read footprint + popular, return the deduped ``owner/repo`` slugs.

    The two upstream datasets use different conventions for the same
    GitHub repo. deps.dev's ``Projects.Name`` when ``Type = 'GITHUB'``
    is bare ``owner/repo``; OpenSSF Scorecard's ``repo.name`` is the
    fully-qualified ``github.com/owner/repo``. To keep one canonical
    in-memory key, we strip both upstream forms down to ``owner/repo``
    and translate back to the host-prefixed form only when querying
    scorecard. The dict also returns the original ``https://`` URL so
    the output CSV joins to the harvester files by exact string match.
    """
    slugs: set[str] = set()
    slug_to_url: dict[str, str] = {}

    def absorb(csv_path: Path) -> None:
        if not csv_path.exists():
            print(f"[repos] WARNING: {csv_path} missing — skipping", flush=True)
            return
        with csv_path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                url = (row.get("repo_url") or "").strip().rstrip("/")
                if not url:
                    continue
                if not url.startswith("https://github.com/"):
                    continue
                slug = url.removeprefix("https://github.com/")
                slugs.add(slug)
                slug_to_url.setdefault(slug, url)

    absorb(FOOTPRINT_CSV)
    absorb(POPULAR_CSV)

    print(f"[repos] distinct GitHub repos: {len(slugs):,}", flush=True)
    return sorted(slugs), slug_to_url


def latest_deps_dev_snapshot(client: bigquery.Client) -> str:
    """Latest snapshot timestamp from the tiny ``Snapshots`` metadata table."""
    rows = list(
        client.query(
            f"SELECT MAX(Time) AS latest FROM `{DEPS_DEV_DATASET}.Snapshots`",
            job_config=bigquery.QueryJobConfig(
                maximum_bytes_billed=MAX_BYTES_BILLED,
                use_query_cache=True,
            ),
        ).result()
    )
    latest = rows[0]["latest"]
    if latest is None:
        raise RuntimeError("No deps.dev snapshots found")
    print(f"[repos] deps.dev latest snapshot: {latest.isoformat()}", flush=True)
    return latest.isoformat(sep=" ")


def latest_scorecard_partition(client: bigquery.Client) -> str:
    """Most recent partition_id of scorecard-v2, from INFORMATION_SCHEMA.

    Querying the data tables for ``MAX(date)`` scans every partition;
    INFORMATION_SCHEMA.PARTITIONS is metadata-only and free.
    """
    rows = list(
        client.query(
            """
            SELECT partition_id
            FROM `openssf.scorecardcron.INFORMATION_SCHEMA.PARTITIONS`
            WHERE table_name = 'scorecard-v2'
              AND partition_id IS NOT NULL
              AND partition_id != '__NULL__'
            ORDER BY partition_id DESC LIMIT 1
            """,
            job_config=bigquery.QueryJobConfig(
                maximum_bytes_billed=MAX_BYTES_BILLED,
                use_query_cache=True,
            ),
        ).result()
    )
    partition_id = rows[0]["partition_id"]
    iso = f"{partition_id[:4]}-{partition_id[4:6]}-{partition_id[6:8]}"
    print(f"[repos] scorecard latest partition: {iso}", flush=True)
    return iso


def fetch_deps_dev_projects(
    client: bigquery.Client,
    slugs: list[str],
    snapshot_at: str,
) -> dict[str, dict]:
    """Pull stars/forks/issues/license/description for each repo.

    Returns a dict keyed by ``owner/repo`` slug; absent keys mean
    deps.dev has no Project row for that repo (private, deleted, never
    indexed). The Projects table stores ``Type='GITHUB'`` repos under
    bare ``owner/repo`` — no host prefix — so we pass the slugs through
    unmodified.
    """
    query = f"""
        SELECT Name, StarsCount, ForksCount, OpenIssuesCount, Licenses,
               Description, SnapshotAt
        FROM `{DEPS_DEV_DATASET}.Projects`
        WHERE SnapshotAt = TIMESTAMP("{snapshot_at}")
          AND Type = 'GITHUB'
          AND Name IN UNNEST(@names)
    """
    job_config = bigquery.QueryJobConfig(
        maximum_bytes_billed=MAX_BYTES_BILLED,
        use_query_cache=True,
        priority=bigquery.QueryPriority.BATCH,
        query_parameters=[
            bigquery.ArrayQueryParameter("names", "STRING", slugs),
        ],
    )
    started = time.perf_counter()
    job = client.query(query, job_config=job_config)
    rows = list(job.result())
    elapsed = time.perf_counter() - started

    bytes_billed = job.total_bytes_billed or 0
    print(
        f"[repos] deps.dev fetched {len(rows):,}/{len(slugs):,} repos "
        f"in {elapsed:.1f}s (billed={bytes_billed / 1024**3:.2f} GiB)",
        flush=True,
    )

    return {
        r["Name"]: {
            "stars": r["StarsCount"],
            "forks": r["ForksCount"],
            "open_issues": r["OpenIssuesCount"],
            "license": "|".join(r["Licenses"]) if r["Licenses"] else "",
            "description": (r["Description"] or "").replace("\n", " ").replace("\r", " "),
            "snapshot_at": r["SnapshotAt"].isoformat() if r["SnapshotAt"] else "",
        }
        for r in rows
    }


def fetch_scorecards(
    client: bigquery.Client,
    slugs: list[str],
    scorecard_date: str,
) -> dict[str, dict]:
    """Pull the latest OpenSSF Scorecard for each repo.

    Scorecard's ``repo.name`` is the fully-qualified
    ``github.com/owner/repo`` — we prepend ``github.com/`` to each slug
    on the way in, then strip it on the way out so the returned dict
    is keyed by the same ``owner/repo`` slug the deps.dev dict uses.
    Each repo has at most one row in the chosen partition because
    scorecard runs weekly and we picked the most recent partition.
    """
    scorecard_names = [f"github.com/{slug}" for slug in slugs]
    query = f"""
        SELECT repo.name AS name, date, score
        FROM `{SCORECARD_TABLE}`
        WHERE date = DATE("{scorecard_date}")
          AND repo.name IN UNNEST(@names)
    """
    job_config = bigquery.QueryJobConfig(
        maximum_bytes_billed=MAX_BYTES_BILLED,
        use_query_cache=True,
        priority=bigquery.QueryPriority.BATCH,
        query_parameters=[
            bigquery.ArrayQueryParameter("names", "STRING", scorecard_names),
        ],
    )
    started = time.perf_counter()
    job = client.query(query, job_config=job_config)
    rows = list(job.result())
    elapsed = time.perf_counter() - started

    bytes_billed = job.total_bytes_billed or 0
    print(
        f"[repos] scorecard fetched {len(rows):,}/{len(slugs):,} repos "
        f"in {elapsed:.1f}s (billed={bytes_billed / 1024**3:.2f} GiB)",
        flush=True,
    )

    out: dict[str, dict] = {}
    for r in rows:
        slug = r["name"].removeprefix("github.com/")
        out[slug] = {
            "scorecard_score": r["score"],
            "scorecard_date": r["date"].isoformat() if r["date"] else "",
        }
    return out


def write_csv(
    slugs: list[str],
    slug_to_url: dict[str, str],
    deps_dev: dict[str, dict],
    scorecard: dict[str, dict],
    out_path: Path,
) -> tuple[int, int, int]:
    """Write one row per repo. Returns (written, with_deps_dev, with_scorecard).

    All three dicts (deps_dev, scorecard, slug_to_url) are keyed by the
    same ``owner/repo`` slug so the per-row lookup is a straight ``.get``.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    columns: list[str] = [
        "repo_url",
        "stars",
        "forks",
        "open_issues",
        "license",
        "description",
        "deps_dev_snapshot_at",
        "scorecard_score",
        "scorecard_date",
    ]
    with_deps_dev = 0
    with_scorecard = 0
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for slug in slugs:
            dd = deps_dev.get(slug, {})
            sc = scorecard.get(slug, {})
            if dd:
                with_deps_dev += 1
            if sc:
                with_scorecard += 1
            row = [
                slug_to_url[slug],
                dd.get("stars", ""),
                dd.get("forks", ""),
                dd.get("open_issues", ""),
                dd.get("license", ""),
                dd.get("description", ""),
                dd.get("snapshot_at", ""),
                sc.get("scorecard_score", ""),
                sc.get("scorecard_date", ""),
            ]
            writer.writerow(row)
    return len(slugs), with_deps_dev, with_scorecard


def main() -> int:
    client = initialize_bq_client()
    slugs, slug_to_url = collect_repo_names()
    if not slugs:
        print("[repos] no repos to fetch — aborting", flush=True)
        return 1

    snapshot_at = latest_deps_dev_snapshot(client)
    scorecard_date = latest_scorecard_partition(client)

    deps_dev = fetch_deps_dev_projects(client, slugs, snapshot_at)
    scorecard = fetch_scorecards(client, slugs, scorecard_date)

    written, with_dd, with_sc = write_csv(slugs, slug_to_url, deps_dev, scorecard, OUT_CSV)

    print()
    print(f"[repos] wrote {OUT_CSV}")
    print(f"[repos] total rows         : {written:,}")
    print(f"[repos] with deps.dev data : {with_dd:,} ({with_dd / written:.0%})")
    print(f"[repos] with scorecard     : {with_sc:,} ({with_sc / written:.0%})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
