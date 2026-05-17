"""Build popular.csv — top-starred packages per ecosystem via deps.dev BigQuery.

Replaces the ecosyste.ms-based ``build_popular_csv.py`` for the 7 ecosystems
deps.dev covers (npm, pypi, maven, golang, cargo, nuget, rubygems). Direct
queries against the public dataset ``bigquery-public-data.deps_dev_v1``,
ranking packages by GitHub star count on their source repository. The
deps.dev public dataset exposes a row-per-edge ``Dependents`` table
(76 TiB) but no pre-aggregated dependent count — aggregating it on the
fly blows the cost fuse, so we use ``Projects.StarsCount`` as the
popularity proxy. It correlates well enough with real-world usage for
the purpose of this CSV: deciding which repos to pre-harvest contributor
data for.

Output schema matches ``footprint_resolved.csv`` so the harvester's
``load_work`` consumes either file with no code change::

    ecosystem,namespace,name,version,repo_url,resolution_source

Cost & safety. ``Projects`` and ``PackageVersionToProject`` are both
DAY-partitioned on ``SnapshotAt``; filtering to a single snapshot drops
each scan to a few GiB. Every job carries a ``maximum_bytes_billed=20
GiB`` fuse — a misconfigured query fails fast instead of scanning the
multi-TiB tail.

Authentication mirrors the pattern in
``src/app/services/ai/ai_data_enrichment/application/app_services/data_preprocessing.py``:
service account JSON loaded via ``GOOGLE_APPLICATION_CREDENTIALS``,
``cloud-platform`` scope, project discovered from the credentials file.
"""

from __future__ import annotations

import csv
import os
import sys
import time
from collections import Counter
from pathlib import Path

from google.cloud import bigquery
from google.oauth2 import service_account

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_CSV = REPO_ROOT / "output" / "popular.csv"

# Per-ecosystem caps. Sized to the meaningful popularity tail per
# state.md's monorepo-concentration table; beyond these is mostly
# abandoned packages that don't appear in real customer BOMs.
ECOSYSTEM_CAPS: dict[str, int] = {
    "NPM": 50_000,
    "MAVEN": 30_000,
    "PYPI": 30_000,
    "GO": 30_000,
    "CARGO": 30_000,
    "NUGET": 20_000,
    "RUBYGEMS": 20_000,
}

# BigQuery's ``System`` values → ecosystem column in popular.csv. NPM/PYPI/
# MAVEN/CARGO/NUGET match directly when lowercased; GO and RUBYGEMS need
# explicit translation to align with the harvester's
# ``CSV_TO_DETECTOR_ECOSYSTEM`` map.
SYSTEM_TO_CSV_ECOSYSTEM: dict[str, str] = {
    "NPM": "npm",
    "PYPI": "pypi",
    "MAVEN": "maven",
    "GO": "golang",
    "CARGO": "cargo",
    "NUGET": "nuget",
    "RUBYGEMS": "gem",
}

# Cost fuse — any query exceeding this is rejected by BigQuery rather
# than scanning the 2 TiB ``PackageVersionToProject`` table.
MAX_BYTES_BILLED = 20 * 1024**3  # 20 GiB

DATASET = "bigquery-public-data.deps_dev_v1"


def initialize_bq_client() -> bigquery.Client:
    """Build a BigQuery client from the service account JSON.

    Mirrors ``AsyncDataPreprocessingService.initialize_bq_client`` minus
    the async wrapper and connection pool — this script is single-threaded
    and the default urllib3 pool is sufficient.
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
    print(f"[popular] BigQuery client ready (project: {credentials.project_id})", flush=True)
    return client


def latest_snapshot(client: bigquery.Client) -> str:
    """Resolve the most recent snapshot timestamp from the metadata table.

    The data tables are DAY-partitioned on ``SnapshotAt`` but a plain
    ``MAX(SnapshotAt)`` scans every partition's column (~140 GiB on the
    large tables, well past our 20 GiB fuse). The deps.dev dataset
    publishes a tiny unpartitioned ``Snapshots`` table (~1.7 KiB total)
    that lists every snapshot date — querying that is effectively free.
    """
    job_config = bigquery.QueryJobConfig(
        maximum_bytes_billed=MAX_BYTES_BILLED,
        use_query_cache=True,
    )
    query = f"""
        SELECT MAX(Time) AS latest
        FROM `{DATASET}.Snapshots`
    """
    rows = list(client.query(query, job_config=job_config).result())
    latest = rows[0]["latest"]
    if latest is None:
        raise RuntimeError("No snapshots found — dataset broken?")
    print(f"[popular] latest snapshot: {latest.isoformat()}", flush=True)
    return latest.isoformat(sep=" ")


def fetch_popular_packages(
    client: bigquery.Client,
    snapshot_at: str,
    *,
    dry_run: bool = False,
) -> list[tuple[str, str, str, str, int]]:
    """Return ranked popular packages joined to their source repos.

    One row per ``(System, Name)`` — an arbitrary version that PVtP has
    for that package on the snapshot, plus the GitHub project URL and
    its star count. Packages whose source repo isn't on GitHub are
    dropped at the SQL level so the CSV never carries empty ``repo_url``
    rows.

    Pipeline (single SQL statement):

    1. ``Projects`` filtered to the latest snapshot, GITHUB only, with a
       star count.
    2. ``PackageVersionToProject`` filtered to the latest snapshot,
       ``RelationType = 'SOURCE_REPO_TYPE'``, GITHUB only, restricted
       to the 7 in-scope systems.
    3. Join the two; collapse to one row per ``(System, Name)`` keeping
       any version + the repo's stars.
    4. Rank by stars within each system and cap per
       ``ECOSYSTEM_CAPS``.

    Passing ``dry_run=True`` returns no rows but populates
    ``total_bytes_processed`` for cost estimation before committing
    to the real run.
    """
    caps = ECOSYSTEM_CAPS
    cap_when = "\n            ".join(
        f"WHEN System = '{system}' THEN {cap}" for system, cap in caps.items()
    )
    systems_list = ", ".join(f"'{s}'" for s in caps)

    query = f"""
        WITH
        proj AS (
            SELECT Name AS ProjectName, StarsCount
            FROM `{DATASET}.Projects`
            WHERE SnapshotAt = TIMESTAMP("{snapshot_at}")
              AND Type = 'GITHUB'
              AND StarsCount IS NOT NULL
        ),
        pvtp AS (
            SELECT System, Name, Version, ProjectName
            FROM `{DATASET}.PackageVersionToProject`
            WHERE SnapshotAt = TIMESTAMP("{snapshot_at}")
              AND RelationType = 'SOURCE_REPO_TYPE'
              AND ProjectType = 'GITHUB'
              AND System IN ({systems_list})
        ),
        joined AS (
            SELECT pv.System, pv.Name, pv.Version, pv.ProjectName, pr.StarsCount
            FROM pvtp pv
            JOIN proj pr USING (ProjectName)
        ),
        one_per_package AS (
            SELECT
                System,
                Name,
                ANY_VALUE(Version) AS Version,
                ANY_VALUE(ProjectName) AS ProjectName,
                MAX(StarsCount) AS StarsCount
            FROM joined
            GROUP BY System, Name
        ),
        ranked AS (
            SELECT
                System, Name, Version, ProjectName, StarsCount,
                ROW_NUMBER() OVER (
                    PARTITION BY System
                    ORDER BY StarsCount DESC, Name ASC
                ) AS rank
            FROM one_per_package
        )
        SELECT System, Name, Version, ProjectName, StarsCount
        FROM ranked
        WHERE rank <= (CASE
            {cap_when}
        END)
        ORDER BY System, rank
    """

    job_config = bigquery.QueryJobConfig(
        maximum_bytes_billed=MAX_BYTES_BILLED,
        use_query_cache=True,
        priority=bigquery.QueryPriority.BATCH,
        dry_run=dry_run,
    )

    started = time.perf_counter()
    job = client.query(query, job_config=job_config)

    if dry_run:
        elapsed = time.perf_counter() - started
        bytes_processed = job.total_bytes_processed or 0
        print(
            f"[popular] dry-run: would scan {bytes_processed / 1024**3:.2f} GiB "
            f"(est_cost=${bytes_processed / 1024**4 * 5:.4f}) — {elapsed:.1f}s",
            flush=True,
        )
        return []

    rows = list(job.result())
    elapsed = time.perf_counter() - started

    bytes_billed = job.total_bytes_billed or 0
    bytes_processed = job.total_bytes_processed or 0
    print(
        f"[popular] query complete: {len(rows):,} rows in {elapsed:,.1f}s "
        f"(processed={bytes_processed / 1024**3:.2f} GiB, "
        f"billed={bytes_billed / 1024**3:.2f} GiB, "
        f"est_cost=${bytes_billed / 1024**4 * 5:.4f})",
        flush=True,
    )

    return [
        (r["System"], r["Name"], r["Version"], r["ProjectName"], r["StarsCount"] or 0) for r in rows
    ]


def split_qualified_name(ecosystem: str, qualified: str) -> tuple[str, str]:
    """Split BigQuery's combined ``Name`` field into ``(namespace, name)``.

    Mirrors the splitter in ``build_popular_csv.py`` so the two scripts
    produce interchangeable output. Rules per ecosystem:

    - npm scoped: ``@scope/pkg`` → ``("@scope", "pkg")``
    - maven: ``groupId:artifactId`` → ``("groupId", "artifactId")``
    - golang: ``host/org/repo`` → namespace is everything up to the last slash
    - everything else without a separator: ``("", qualified)``
    """
    if ecosystem == "maven" and ":" in qualified:
        namespace, _, name = qualified.partition(":")
        return namespace, name
    if "/" not in qualified:
        return "", qualified
    if qualified.startswith("@"):
        namespace, _, name = qualified.partition("/")
        return namespace, name
    if ecosystem == "golang":
        namespace, _, name = qualified.rpartition("/")
        return namespace, name
    namespace, _, name = qualified.partition("/")
    return namespace, name


def project_name_to_repo_url(project_name: str) -> str:
    """Convert deps.dev's bare ``owner/repo`` → ``https://github.com/owner/repo``.

    deps.dev stores ``ProjectName`` as bare ``owner/repo`` when
    ``ProjectType = 'GITHUB'`` (no scheme, no host). The harvester
    expects fully-qualified URLs because it groups work by exact string
    match on the URL, so we prepend the canonical GitHub host. The
    query filters to ``ProjectType = 'GITHUB'`` so every project_name
    is implicitly a GitHub slug.
    """
    project_name = project_name.strip().strip("/")
    if not project_name:
        return ""
    if project_name.startswith(("http://", "https://")):
        return project_name
    if project_name.startswith("github.com/"):
        return f"https://{project_name}"
    return f"https://github.com/{project_name}"


def write_csv(
    rows: list[tuple[str, str, str, str, int]],
    out_path: Path,
) -> tuple[int, Counter[str]]:
    """Emit popular.csv. Returns ``(row_count, per_ecosystem_counter)``."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    eco_counter: Counter[str] = Counter()
    written = 0

    with out_path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["ecosystem", "namespace", "name", "version", "repo_url", "resolution_source"]
        )
        for system, qualified_name, version, project_name, _stars in rows:
            ecosystem = SYSTEM_TO_CSV_ECOSYSTEM.get(system)
            if ecosystem is None:
                continue
            repo_url = project_name_to_repo_url(project_name)
            if not repo_url:
                continue
            namespace, name = split_qualified_name(ecosystem, qualified_name)
            writer.writerow([ecosystem, namespace, name, version, repo_url, "deps_dev"])
            eco_counter[ecosystem] += 1
            written += 1

    return written, eco_counter


def main() -> int:
    client = initialize_bq_client()
    snapshot_at = latest_snapshot(client)

    # Cheap dry-run first so we know the real query stays under the
    # cost fuse before it runs for several minutes in BATCH priority.
    fetch_popular_packages(client, snapshot_at, dry_run=True)

    rows = fetch_popular_packages(client, snapshot_at)

    if not rows:
        print("[popular] no rows returned — aborting", flush=True)
        return 1

    written, eco_counter = write_csv(rows, OUT_CSV)

    distinct_repos = {r[3] for r in rows}

    print()
    print(f"[popular] wrote {OUT_CSV}")
    print(f"[popular] total rows   : {written:,}")
    print(f"[popular] distinct repos: {len(distinct_repos):,}")
    print("[popular] per-ecosystem:")
    for ecosystem, count in eco_counter.most_common():
        print(f"  {ecosystem:<11} {count:>7,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
