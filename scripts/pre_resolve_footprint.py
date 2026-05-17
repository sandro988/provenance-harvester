"""Pre-resolve repo URLs for every package in footprint.csv.

Walks ``output/footprint.csv``, groups rows by ``(ecosystem, namespace,
name)`` (since the repo URL is the same across all versions of a
package), resolves each distinct *package* exactly once via the
RepoUrlResolver, and writes ``output/footprint_resolved.csv`` with the
``repo_url`` attached to every original row.

Why per-package, not per-component: a footprint with 54k component-
versions covers only ~25k distinct packages. Calling the registry once
per package is ~2x cheaper in API calls and ~2x faster in wall-clock.

Run is **resumable**: packages already resolved (matched by their
3-tuple identity) are skipped on re-run; you can interrupt and resume.

Throughput: concurrent via asyncio + thread pool over the sync HTTP
client. deps.dev (no visible rate limit) handles npm/pypi/maven/cargo/
nuget/golang — ~92% of footprint. ecosyste.ms (5k/hr anonymous) handles
the long tail. Total wall-clock ~30 min for 25k packages.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import structlog

from harvester.deps_dev_client import DepsDevClient
from harvester.ecosystems_client import EcosystemsClient
from harvester.repo_url_resolver import RepoUrlResolver

logger = structlog.get_logger("scripts.pre_resolve_footprint")

REPO_ROOT = Path(__file__).resolve().parent.parent
FOOTPRINT_CSV = REPO_ROOT / "output" / "footprint.csv"
RESOLVED_CSV = REPO_ROOT / "output" / "footprint_resolved.csv"
PACKAGE_CACHE_CSV = REPO_ROOT / "output" / "package_repo_cache.csv"
CONCURRENCY = 16
PROGRESS_EVERY = 200

# Ecosystems that have no source-repo concept; we record them without
# making an API call so the resolved CSV is complete and downstream
# steps don't need to re-skip them.
UNRESOLVABLE_ECOSYSTEMS = frozenset(
    {"apk", "deb", "rpm", "generic", "github", "swift", "huggingface", "bitnami"}
)


@dataclass(frozen=True)
class Package:
    ecosystem: str
    namespace: str
    name: str


@dataclass
class PackageResolution:
    package: Package
    repo_url: str
    source: str  # "ecosystems" | "deps_dev" | "none" | "unresolvable_ecosystem"


def load_footprint_rows(path: Path) -> list[dict]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def load_cached_packages(path: Path) -> dict[Package, PackageResolution]:
    """Return cached package resolutions so we don't re-call the API."""
    if not path.exists():
        return {}
    cache: dict[Package, PackageResolution] = {}
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            package = Package(row["ecosystem"], row["namespace"], row["name"])
            cache[package] = PackageResolution(package, row["repo_url"], row["resolution_source"])
    return cache


def resolve_one_package(
    resolver: RepoUrlResolver,
    ecosystems: EcosystemsClient,
    deps_dev: DepsDevClient,
    package: Package,
) -> PackageResolution:
    """Resolve one distinct package, attributing which source succeeded."""
    if package.ecosystem in UNRESOLVABLE_ECOSYSTEMS:
        return PackageResolution(package, "", "unresolvable_ecosystem")

    # Try deps.dev first for the 6 ecosystems it covers (no rate limit).
    deps_dev_supported = package.ecosystem in {
        "npm", "pypi", "maven", "cargo", "nuget", "golang"
    }
    if deps_dev_supported:
        url = deps_dev.resolve_repo_url(
            package.ecosystem, package.namespace, package.name
        )
        if url:
            return PackageResolution(package, url, "deps_dev")

    # Fall through to ecosyste.ms (broader ecosystem support, rate-limited).
    url = ecosystems.resolve_repo_url(
        package.ecosystem, package.namespace, package.name
    )
    if url:
        return PackageResolution(package, url, "ecosystems")

    # Last resort: if we haven't tried deps.dev yet (unsupported eco),
    # we're done. If we have tried deps.dev, also done.
    return PackageResolution(package, "", "none")


async def resolve_pending_packages(
    pending: list[Package],
    resolver: RepoUrlResolver,
    ecosystems: EcosystemsClient,
    deps_dev: DepsDevClient,
    concurrency: int,
    cache_writer: csv.writer,
    cache_fh,
) -> dict[Package, PackageResolution]:
    """Concurrently resolve all pending packages; persist cache as we go."""
    loop = asyncio.get_event_loop()
    resolved: dict[Package, PackageResolution] = {}
    counts: Counter = Counter()
    completed = 0
    started = time.perf_counter()

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            loop.run_in_executor(
                executor, resolve_one_package, resolver, ecosystems, deps_dev, package
            )
            for package in pending
        ]
        for future in asyncio.as_completed(futures):
            result: PackageResolution = await future
            resolved[result.package] = result
            cache_writer.writerow(
                [
                    result.package.ecosystem,
                    result.package.namespace,
                    result.package.name,
                    result.repo_url,
                    result.source,
                ]
            )
            counts[result.source] += 1
            completed += 1
            if completed % PROGRESS_EVERY == 0:
                cache_fh.flush()
                elapsed = time.perf_counter() - started
                rate = completed / elapsed if elapsed > 0 else 0
                remaining_secs = (len(pending) - completed) / rate if rate else 0
                logger.info(
                    "pre_resolve.progress",
                    completed=completed,
                    total=len(pending),
                    rate_per_sec=round(rate, 2),
                    eta_minutes=round(remaining_secs / 60, 1),
                    by_source=dict(counts),
                )
    return resolved


def write_resolved_csv(
    footprint_rows: list[dict],
    package_resolutions: dict[Package, PackageResolution],
    path: Path,
) -> None:
    """Expand per-package resolutions back to per-row output."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["ecosystem", "namespace", "name", "version", "repo_url", "resolution_source"]
        )
        for row in footprint_rows:
            package = Package(row["ecosystem"], row["namespace"], row["name"])
            resolution = package_resolutions.get(package)
            repo_url = resolution.repo_url if resolution else ""
            source = resolution.source if resolution else "none"
            writer.writerow(
                [row["ecosystem"], row["namespace"], row["name"], row["version"], repo_url, source]
            )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Resolve only the first N un-cached packages (for smoke testing).",
    )
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY)
    args = parser.parse_args()

    footprint_rows = load_footprint_rows(FOOTPRINT_CSV)
    distinct_packages = {
        Package(row["ecosystem"], row["namespace"], row["name"]) for row in footprint_rows
    }
    cache = load_cached_packages(PACKAGE_CACHE_CSV)
    pending = sorted(
        distinct_packages - cache.keys(),
        key=lambda p: (p.ecosystem, p.namespace, p.name),
    )
    if args.limit is not None:
        pending = pending[: args.limit]

    print(f"[pre-resolve] component-version rows : {len(footprint_rows)}")
    print(f"[pre-resolve] distinct packages      : {len(distinct_packages)}")
    print(f"[pre-resolve] already cached         : {len(cache)}")
    print(f"[pre-resolve] pending this run       : {len(pending)}")
    print(f"[pre-resolve] concurrency            : {args.concurrency}")

    cache_is_new = not PACKAGE_CACHE_CSV.exists()
    PACKAGE_CACHE_CSV.parent.mkdir(parents=True, exist_ok=True)

    if pending:
        with (
            EcosystemsClient() as ecosystems,
            DepsDevClient() as deps_dev,
            PACKAGE_CACHE_CSV.open("a", newline="") as cache_fh,
        ):
            cache_writer = csv.writer(cache_fh)
            if cache_is_new:
                cache_writer.writerow(
                    ["ecosystem", "namespace", "name", "repo_url", "resolution_source"]
                )
            resolver = RepoUrlResolver(ecosystems, deps_dev)
            new_resolutions = await resolve_pending_packages(
                pending,
                resolver,
                ecosystems,
                deps_dev,
                args.concurrency,
                cache_writer,
                cache_fh,
            )
            cache.update(new_resolutions)
    else:
        print("[pre-resolve] no pending packages — using cache only")

    # If we ran with --limit, the cache is partial; only write full
    # footprint_resolved.csv when we have resolutions for every package.
    if len(cache) >= len(distinct_packages):
        write_resolved_csv(footprint_rows, cache, RESOLVED_CSV)
        print()
        print(f"[pre-resolve] wrote {RESOLVED_CSV}")
    else:
        print()
        print(
            f"[pre-resolve] cache is partial ({len(cache)}/{len(distinct_packages)} packages); "
            "re-run without --limit to write footprint_resolved.csv"
        )

    counts: Counter = Counter(r.source for r in cache.values())
    print()
    print("[pre-resolve] resolutions by source:")
    total = sum(counts.values())
    for source, count in counts.most_common():
        print(f"  {source:<24} {count:>6}  ({count / total:.1%})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
