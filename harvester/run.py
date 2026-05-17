"""Sharded harvester entry point: ``python -m harvester.run``.

Reads a pre-resolved CSV (with ``repo_url`` column attached), takes only
the repos assigned to this shard via a deterministic hash, then for each
unique repo:

1. Clones it once (``git clone --bare --filter=tree:0``) into a tmp dir.
2. Walks every git tag via the existing TagWalker.
3. Writes the four CSVs (components, versions, contributors, persons)
   into a per-repo subdirectory of ``--output-dir``.

The script aims to be the unit AWS spot workers run. Each worker picks
its ``--shard`` and ``--total-shards`` from instance metadata, runs to
completion, and the per-repo output trees get rsynced to S3 by a
collection sidecar.

Two correctness properties:

- **Repo-level dedup.** A repo serving many packages (e.g.
  ``dart-lang/sdk`` for many ``pub/*`` versions) is cloned exactly once
  per shard. We read the input CSV once and group rows by ``repo_url``.

- **Stable sharding.** ``hash(repo_url) % total_shards == shard``
  ensures each repo is processed by exactly one shard, even if
  instances start in different orders or one restarts mid-run.

Resumability: per-repo progress is tracked on disk. A repo with all
four output CSVs already written is skipped — so a spot-interrupted
worker that restarts picks up where it left off. SIGTERM is trapped to
flush in-flight state before AWS reclaims the instance.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import hashlib
import signal
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import structlog

from harvester.extractor.extractor_types import ExtractorDeps
from harvester.extractor.git_runner import GitRunner
from harvester.extractor.repo_cloner import RepoCloner
from harvester.extractor.tag_walker import TagWalker
from harvester.writer import CsvWriter

logger = structlog.get_logger("harvester.run")

DEFAULT_CONCURRENCY = 30
CLONE_TIMEOUT_S = 600
LOG_TIMEOUT_S = 300


@dataclass(frozen=True)
class ComponentVersion:
    """One row from the pre-resolved footprint CSV."""

    ecosystem: str
    namespace: str
    name: str
    version: str
    repo_url: str


@dataclass(frozen=True)
class RepoWorkItem:
    """All the component-versions served by a single repo URL."""

    repo_url: str
    components: tuple[ComponentVersion, ...]

    @property
    def slug(self) -> str:
        """Filesystem-safe directory name for this repo's output."""
        # Use a stable URL-derived slug; collisions on the slug alone are
        # impossible because we keep the full hash as a suffix.
        digest = hashlib.sha256(self.repo_url.encode("utf-8")).hexdigest()[:12]
        readable = (
            self.repo_url.removeprefix("https://")
            .removeprefix("http://")
            .replace("/", "_")
            .replace(":", "_")[:60]
        )
        return f"{readable}__{digest}"


def shard_for_repo(repo_url: str, total_shards: int) -> int:
    """Deterministic shard assignment via SHA-256 mod total_shards.

    Plain ``hash()`` would vary between Python processes due to
    PYTHONHASHSEED; SHA-256 is stable across instances and restarts.
    """
    digest = hashlib.sha256(repo_url.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % total_shards


def load_work(input_csv: Path, shard: int, total_shards: int) -> list[RepoWorkItem]:
    """Read pre-resolved CSV, filter to this shard, group by repo URL."""
    by_repo: dict[str, list[ComponentVersion]] = defaultdict(list)
    skipped_no_url = 0
    skipped_other_shard = 0

    with input_csv.open(newline="") as fh:
        for row in csv.DictReader(fh):
            repo_url = row.get("repo_url", "").strip()
            if not repo_url:
                skipped_no_url += 1
                continue
            if shard_for_repo(repo_url, total_shards) != shard:
                skipped_other_shard += 1
                continue
            by_repo[repo_url].append(
                ComponentVersion(
                    ecosystem=row["ecosystem"],
                    namespace=row["namespace"],
                    name=row["name"],
                    version=row["version"],
                    repo_url=repo_url,
                )
            )

    work = [
        RepoWorkItem(repo_url=url, components=tuple(comps))
        for url, comps in sorted(by_repo.items())
    ]
    logger.info(
        "run.shard_loaded",
        shard=shard,
        total_shards=total_shards,
        repos_in_shard=len(work),
        component_versions_in_shard=sum(len(c.components) for c in work),
        skipped_no_repo_url=skipped_no_url,
        skipped_other_shard=skipped_other_shard,
    )
    return work


def is_repo_already_done(output_dir: Path) -> bool:
    """A repo is considered done if all four expected CSVs are present."""
    expected = ("components.csv", "versions.csv", "contributors.csv", "persons.csv")
    return all((output_dir / name).exists() for name in expected)


async def harvest_one_repo(
    work_item: RepoWorkItem,
    output_root: Path,
    cloner: RepoCloner,
    walker: TagWalker,
    semaphore: asyncio.Semaphore,
    shutdown_event: asyncio.Event,
) -> dict:
    """Harvest a single repo: clone, walk, write CSVs. Return per-repo telemetry."""
    repo_out = output_root / work_item.slug
    started = time.perf_counter()

    if is_repo_already_done(repo_out):
        return {"repo_url": work_item.repo_url, "status": "skipped_already_done"}

    if shutdown_event.is_set():
        return {"repo_url": work_item.repo_url, "status": "skipped_shutdown"}

    async with semaphore:
        if shutdown_event.is_set():
            return {"repo_url": work_item.repo_url, "status": "skipped_shutdown"}

        # The exemplar component identifies the row in components.csv; we
        # write one row even if the repo serves many packages, because
        # provenance is a repo-level fact. Pick the first deterministically.
        exemplar = work_item.components[0]
        synced_at = datetime.now(UTC).isoformat(timespec="seconds")

        try:
            async with cloner.clone_to_temp(work_item.repo_url) as clone_path:
                walks = await walker.walk(clone_path)
        except Exception as exc:
            return {
                "repo_url": work_item.repo_url,
                "status": "clone_or_walk_failed",
                "error": type(exc).__name__,
                "elapsed_s": time.perf_counter() - started,
            }

        if not walks:
            return {
                "repo_url": work_item.repo_url,
                "status": "empty_walks",
                "elapsed_s": time.perf_counter() - started,
            }

        try:
            CsvWriter(out_dir=repo_out).write_all(
                ecosystem=exemplar.ecosystem,
                name=(
                    f"{exemplar.namespace}/{exemplar.name}" if exemplar.namespace else exemplar.name
                ),
                repo_url=work_item.repo_url,
                synced_at=synced_at,
                walks=walks,
            )
        except Exception as exc:
            return {
                "repo_url": work_item.repo_url,
                "status": "write_failed",
                "error": type(exc).__name__,
                "elapsed_s": time.perf_counter() - started,
            }

    return {
        "repo_url": work_item.repo_url,
        "status": "ok",
        "tag_count": len(walks),
        "components_served": len(work_item.components),
        "elapsed_s": time.perf_counter() - started,
    }


def install_signal_handlers(shutdown_event: asyncio.Event) -> None:
    """Trap SIGTERM/SIGINT so spot-interrupt drains the in-flight pool."""
    loop = asyncio.get_event_loop()

    def _shutdown(signal_name: str) -> None:
        logger.warning("run.shutdown_signal_received", signal=signal_name)
        shutdown_event.set()

    # Windows asyncio doesn't support signal handlers — fine for the
    # local-dev path; the AWS runtime is Linux.
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _shutdown, sig.name)


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one shard of the harvester over a pre-resolved CSV."
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Path to a pre-resolved CSV with columns including 'repo_url'.",
    )
    parser.add_argument(
        "--shard",
        type=int,
        default=0,
        help="This shard's index (0-based).",
    )
    parser.add_argument(
        "--total-shards",
        type=int,
        default=1,
        help="Total number of shards in the fleet.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/harvest"),
        help="Per-repo CSVs land under this directory.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="How many repos to clone+walk in parallel.",
    )
    args = parser.parse_args()

    work = load_work(args.input, args.shard, args.total_shards)
    if not work:
        print(f"[run] shard {args.shard}/{args.total_shards}: nothing to do")
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)

    deps = replace(
        ExtractorDeps.defaults(),
        clone_timeout_s=CLONE_TIMEOUT_S,
        log_timeout_s=LOG_TIMEOUT_S,
    )
    runner = GitRunner(git_executable=deps.git_executable)
    cloner = RepoCloner(deps, runner)
    walker = TagWalker(deps, runner)
    semaphore = asyncio.Semaphore(args.concurrency)
    shutdown_event = asyncio.Event()
    install_signal_handlers(shutdown_event)

    print(
        f"[run] shard {args.shard}/{args.total_shards}: "
        f"{len(work)} repos to harvest at concurrency={args.concurrency}"
    )

    started = time.perf_counter()
    results = await asyncio.gather(
        *(
            harvest_one_repo(work_item, args.output_dir, cloner, walker, semaphore, shutdown_event)
            for work_item in work
        )
    )
    elapsed = time.perf_counter() - started

    by_status: dict[str, int] = defaultdict(int)
    for result in results:
        by_status[result["status"]] += 1

    print()
    print(f"[run] finished in {elapsed / 60:.1f} min")
    for status, count in sorted(by_status.items(), key=lambda kv: -kv[1]):
        print(f"  {status:<24} {count:>6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
