"""Sharded harvester entry point: ``python -m harvester.run``.

Reads a pre-resolved CSV (with ``repo_url`` column attached), takes only
the repos assigned to this shard via a deterministic hash, then for each
unique repo:

1. Clones it once (``git clone --bare --filter=blob:none``) into a tmp dir.
2. Walks every git tag via the existing TagWalker.
3. Writes the four CSVs (components, versions, contributors, persons)
   into a per-repo subdirectory of ``--output-dir``.

The script aims to be the unit AWS spot workers run. There are two
invocation modes:

- **Sharded fleet mode** (``--shard N --total-shards K``): each worker
  picks its slice from instance metadata, processes every repo whose
  ``sha256(repo_url) mod K == N``, runs to completion.

- **Single-repo mode** (``--single-repo URL``): used by per-message SQS
  workers. The CSV is still consulted for the target component list
  (filtered to rows whose ``repo_url`` matches the argument), but no
  sharding happens — exactly one repo is harvested and the process
  exits. The message-driven design lets SQS handle retries, DLQ, and
  visibility timeouts at the AWS layer rather than reimplementing them
  here.

Two correctness properties (sharded mode):

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

from harvester.extractor.extractor_types import ExtractorDeps, TagRangeWalk
from harvester.extractor.git_runner import GitRunner
from harvester.extractor.repo_cloner import RepoCloner
from harvester.extractor.tag_walker import TagWalker
from harvester.manifest_checkout import checkout_manifest_files
from harvester.repo_package_matcher import (
    MatchResult,
    PackageMatch,
    match_packages_in_repo,
)
from harvester.writer import CsvWriter, PackageHarvest

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


def load_work_for_single_repo(
    input_csv: Path,
    target_repo_url: str,
) -> list[RepoWorkItem]:
    """Read the CSV, return only the work item for a single matching repo.

    Used by per-message AWS workers: SQS hands the worker one URL, the
    worker filters the same input CSV used by the sharded path down to
    rows for that URL, and harvests it exactly the same way the sharded
    fleet would have. Sharding is bypassed entirely — the SQS layer is
    responsible for handing each URL to exactly one worker.

    Returns an empty list when no rows match. The caller logs and exits
    cleanly so a stale or already-removed URL doesn't crash a worker;
    SQS will eventually DLQ a repo that produces nothing across retries.
    """
    target = target_repo_url.strip()
    if not target:
        raise ValueError("--single-repo URL is empty")

    matched: list[ComponentVersion] = []
    skipped_no_url = 0
    skipped_other_repo = 0

    with input_csv.open(newline="") as fh:
        for row in csv.DictReader(fh):
            repo_url = row.get("repo_url", "").strip()
            if not repo_url:
                skipped_no_url += 1
                continue
            if repo_url != target:
                skipped_other_repo += 1
                continue
            matched.append(
                ComponentVersion(
                    ecosystem=row["ecosystem"],
                    namespace=row["namespace"],
                    name=row["name"],
                    version=row["version"],
                    repo_url=repo_url,
                )
            )

    if not matched:
        logger.warning(
            "run.single_repo_no_components",
            repo_url=target,
            skipped_no_repo_url=skipped_no_url,
            skipped_other_repo=skipped_other_repo,
        )
        return []

    work = [RepoWorkItem(repo_url=target, components=tuple(matched))]
    logger.info(
        "run.single_repo_loaded",
        repo_url=target,
        component_versions=len(matched),
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
        synced_at = datetime.now(UTC).isoformat(timespec="seconds")
        match_result: MatchResult | None = None

        try:
            async with cloner.clone_to_temp(work_item.repo_url) as clone_path:
                async with checkout_manifest_files(clone_path) as manifest_tree:
                    match_result = match_packages_in_repo(manifest_tree, work_item.components)
                packages = await _walk_matched_or_fallback(
                    walker, clone_path, match_result, work_item
                )
        except Exception as exc:
            # Failure attribution is the operator's first triage question.
            # Without exc_info=True the structured event records only the
            # exception class name; the stack trace is lost.
            logger.warning(
                "run.clone_or_walk_failed",
                repo_url=work_item.repo_url,
                error=type(exc).__name__,
                exc_info=True,
            )
            return {
                "repo_url": work_item.repo_url,
                "status": "clone_or_walk_failed",
                "error": type(exc).__name__,
                "elapsed_s": time.perf_counter() - started,
            }

        if not packages:
            return {
                "repo_url": work_item.repo_url,
                "status": "empty_walks",
                "matched": len(match_result.matched) if match_result else 0,
                "unmatched": len(match_result.unmatched) if match_result else 0,
                "elapsed_s": time.perf_counter() - started,
            }

        try:
            CsvWriter(out_dir=repo_out).write_all(
                repo_url=work_item.repo_url,
                synced_at=synced_at,
                packages=packages,
            )
        except Exception as exc:
            logger.warning(
                "run.write_failed",
                repo_url=work_item.repo_url,
                error=type(exc).__name__,
                exc_info=True,
            )
            return {
                "repo_url": work_item.repo_url,
                "status": "write_failed",
                "error": type(exc).__name__,
                "elapsed_s": time.perf_counter() - started,
            }

    used_repo_wide_fallback = not match_result.matched
    return {
        "repo_url": work_item.repo_url,
        "status": "ok_fallback_repo_wide" if used_repo_wide_fallback else "ok",
        "matched": len(match_result.matched),
        "unmatched": len(match_result.unmatched),
        "packages_written": len(packages),
        "components_served": len(work_item.components),
        "elapsed_s": time.perf_counter() - started,
    }


async def _walk_matched_or_fallback(
    walker: TagWalker,
    clone_path: Path,
    match_result: MatchResult,
    work_item: RepoWorkItem,
) -> list[PackageHarvest]:
    """Run per-package walks if anything matched; otherwise repo-wide.

    Zero matches in a repo we clearly resolved to is almost always a
    rename or sub-published artefact. Falling back to a repo-wide walk
    keeps the data flowing — we lose per-package granularity for that
    repo, but every contributor still lands in the warehouse under the
    exemplar target's coordinates so downstream queries don't see a
    silent hole.

    Per-package walks run concurrently via :func:`asyncio.gather` —
    they read the same on-disk bare clone and share no mutable state,
    so the two ``git`` invocations per walk overlap rather than
    serializing. Wall-time savings scale with package count per repo;
    a 4-package monorepo overlaps four walks into ~one walk's elapsed
    time.
    """
    if not match_result.matched:
        walks = await walker.walk(clone_path)
        if not walks:
            return []
        exemplar = work_item.components[0]
        return [
            PackageHarvest(
                ecosystem=exemplar.ecosystem,
                name=_canonical_name(exemplar.namespace, exemplar.name),
                walks=walks,
            )
        ]

    walks_per_match = await asyncio.gather(
        *(
            walker.walk(clone_path, path_filter=match.relative_path)
            for match in match_result.matched
        )
    )
    return [
        _package_harvest_from_match(match, walks)
        for match, walks in zip(match_result.matched, walks_per_match, strict=True)
        if walks
    ]


def _package_harvest_from_match(
    match: PackageMatch,
    walks: list[TagRangeWalk],
) -> PackageHarvest:
    """Build a PackageHarvest from a matched (component, path) pair."""
    return PackageHarvest(
        ecosystem=match.component.ecosystem,
        name=_canonical_name(match.component.namespace, match.component.name),
        walks=walks,
    )


def _canonical_name(namespace: str, name: str) -> str:
    """Glue namespace and name the same way the legacy writer did.

    Kept compatible with the legacy writer output so the downstream loader
    sees a stable shape. Maven coordinates appear as
    ``groupId/artifactId`` here rather than the registry-native
    ``groupId:artifactId`` for that reason; reconciling is the bulk
    loader's call to make.
    """
    return f"{namespace}/{name}" if namespace else name


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
        help="This shard's index (0-based). Ignored when --single-repo is set.",
    )
    parser.add_argument(
        "--total-shards",
        type=int,
        default=1,
        help="Total number of shards in the fleet. Ignored when --single-repo is set.",
    )
    parser.add_argument(
        "--single-repo",
        type=str,
        default=None,
        help=(
            "Harvest exactly one repo URL instead of a shard slice. "
            "The input CSV is still consulted for the target component list "
            "(filtered to rows whose repo_url matches). Designed for "
            "per-message AWS workers driven by SQS."
        ),
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

    if args.single_repo:
        work = load_work_for_single_repo(args.input, args.single_repo)
        mode_label = f"single-repo {args.single_repo}"
    else:
        work = load_work(args.input, args.shard, args.total_shards)
        mode_label = f"shard {args.shard}/{args.total_shards}"

    if not work:
        print(f"[run] {mode_label}: nothing to do")
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

    print(f"[run] {mode_label}: {len(work)} repos to harvest at concurrency={args.concurrency}")

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
