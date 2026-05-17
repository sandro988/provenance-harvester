"""Smoke-test the harvest pipeline on a stratified slice of the footprint.

For each ecosystem present in ``footprint.csv``, sample up to N components,
resolve their repository URLs via the multi-source resolver, clone,
extract per-version contributors via the existing extractor, and write
per-component CSVs to ``output/smoke/<eco>__<name>/``.

Purpose: prove the whole pipeline (resolver + cloner + walker + writer)
works end-to-end against real heterogeneous components — not just the
lodash benchmark — before scaling.

The script categorises each attempt as one of:

- ``ok`` — clone+walk+write all succeeded with at least one tag
- ``no_repo_url`` — no source registry could give us a URL
- ``clone_failed`` — network/auth/timeout/SSRF-block
- ``extract_failed`` — walker raised on the cloned repo
- ``empty`` — clone succeeded but produced zero tags
- ``skipped_ecosystem`` — neither resolver supports this ecosystem
  (apk, deb, rpm, generic, github, swift, huggingface, bitnami)

Success rate is computed over **harvestable** attempts only — i.e.
excludes ``skipped_ecosystem`` and ``no_repo_url`` since those signal
upstream gaps, not pipeline bugs.

Usage:
    uv run python scripts/smoke_footprint.py
    uv run python scripts/smoke_footprint.py --per-ecosystem 3
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from harvester.deps_dev_client import DepsDevClient
from harvester.ecosystems_client import ECOSYSTEM_TO_REGISTRY, EcosystemsClient
from harvester.extractor.extractor_types import ExtractorDeps
from harvester.extractor.git_runner import GitRunner
from harvester.extractor.repo_cloner import RepoCloner
from harvester.extractor.tag_walker import TagWalker
from harvester.repo_url_resolver import RepoUrlResolver
from harvester.writer import CsvWriter

REPO_ROOT = Path(__file__).resolve().parent.parent
FOOTPRINT_CSV = REPO_ROOT / "output" / "footprint.csv"
SMOKE_OUT_DIR = REPO_ROOT / "output" / "smoke"
CLONE_TIMEOUT_S = 300
LOG_TIMEOUT_S = 180
CONCURRENT_HARVESTS = 4

UNRESOLVABLE_ECOSYSTEMS = frozenset({"apk", "deb", "rpm", "generic", "github", "swift", "huggingface", "bitnami"})


@dataclass(frozen=True)
class Component:
    ecosystem: str
    namespace: str
    name: str
    version: str

    @property
    def display_name(self) -> str:
        return f"{self.namespace}/{self.name}" if self.namespace else self.name

    @property
    def slug(self) -> str:
        """Filesystem-safe identifier for the per-component output dir."""
        qualified = self.display_name.replace("/", "_").replace(":", "_")
        return f"{self.ecosystem}__{qualified}"


@dataclass
class Result:
    component: Component
    status: str
    repo_url: str | None = None
    tag_count: int = 0
    elapsed_s: float = 0.0
    error: str | None = None


def stratified_sample(footprint: list[Component], per_ecosystem: int) -> list[Component]:
    """Return up to ``per_ecosystem`` components from each ecosystem."""
    buckets: dict[str, list[Component]] = defaultdict(list)
    for component in footprint:
        buckets[component.ecosystem].append(component)
    sample: list[Component] = []
    for ecosystem in sorted(buckets):
        sample.extend(buckets[ecosystem][:per_ecosystem])
    return sample


def load_footprint(path: Path) -> list[Component]:
    components: list[Component] = []
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            components.append(
                Component(row["ecosystem"], row["namespace"], row["name"], row["version"])
            )
    return components


async def harvest_one(
    component: Component,
    resolver: RepoUrlResolver,
    cloner: RepoCloner,
    walker: TagWalker,
    semaphore: asyncio.Semaphore,
) -> Result:
    """Resolve, clone, extract, and write a single component's CSVs."""
    started = time.perf_counter()

    if component.ecosystem in UNRESOLVABLE_ECOSYSTEMS:
        return Result(component, "skipped_ecosystem")

    repo_url = resolver.resolve(component.ecosystem, component.namespace, component.name)
    if not repo_url:
        return Result(component, "no_repo_url", elapsed_s=time.perf_counter() - started)

    async with semaphore:
        try:
            async with cloner.clone_to_temp(repo_url) as clone_path:
                walks = await walker.walk(clone_path)
        except Exception as exc:  # noqa: BLE001 — smoke wants every failure categorised
            return Result(
                component,
                "clone_failed",
                repo_url=repo_url,
                elapsed_s=time.perf_counter() - started,
                error=type(exc).__name__,
            )

        if not walks:
            return Result(
                component, "empty", repo_url=repo_url, elapsed_s=time.perf_counter() - started
            )

        try:
            per_component_writer = CsvWriter(out_dir=SMOKE_OUT_DIR / component.slug)
            per_component_writer.write_all(
                ecosystem=component.ecosystem,
                name=component.display_name,
                repo_url=repo_url,
                synced_at=datetime.now(UTC).isoformat(timespec="seconds"),
                walks=walks,
            )
        except Exception as exc:  # noqa: BLE001
            return Result(
                component,
                "extract_failed",
                repo_url=repo_url,
                elapsed_s=time.perf_counter() - started,
                error=type(exc).__name__,
            )

    return Result(
        component,
        "ok",
        repo_url=repo_url,
        tag_count=len(walks),
        elapsed_s=time.perf_counter() - started,
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-ecosystem", type=int, default=5)
    args = parser.parse_args()

    footprint = load_footprint(FOOTPRINT_CSV)
    sample = stratified_sample(footprint, args.per_ecosystem)
    print(f"[smoke] sampled {len(sample)} components from footprint ({args.per_ecosystem} per ecosystem)")

    deps = replace(ExtractorDeps.defaults(), clone_timeout_s=CLONE_TIMEOUT_S, log_timeout_s=LOG_TIMEOUT_S)
    runner = GitRunner(git_executable=deps.git_executable)
    cloner = RepoCloner(deps, runner)
    walker = TagWalker(deps, runner)
    semaphore = asyncio.Semaphore(CONCURRENT_HARVESTS)

    with EcosystemsClient() as ecosystems, DepsDevClient() as deps_dev:
        resolver = RepoUrlResolver(ecosystems, deps_dev)
        coroutines = [
            harvest_one(component, resolver, cloner, walker, semaphore)
            for component in sample
        ]
        started = time.perf_counter()
        results = await asyncio.gather(*coroutines)
        elapsed = time.perf_counter() - started

    print()
    print(f"[smoke] === finished in {elapsed:.1f}s ===")
    status_counts = Counter(r.status for r in results)
    for status, count in status_counts.most_common():
        print(f"  {status:<18} {count:>4}")

    print()
    print("[smoke] === per-component ===")
    for result in results:
        marker = "OK " if result.status == "ok" else "   "
        url = result.repo_url or "(no url)"
        tail = (
            f"tags={result.tag_count}"
            if result.status == "ok"
            else (result.error or "")
        )
        print(
            f"  {marker} [{result.status:<18}] "
            f"{result.component.ecosystem}/{result.component.display_name:<40} "
            f"{result.elapsed_s:>6.1f}s  {url}  {tail}"
        )

    # Success rate is over "harvestable" attempts only — exclude upstream gaps
    harvestable = [r for r in results if r.status not in ("skipped_ecosystem", "no_repo_url")]
    ok_count = status_counts.get("ok", 0)
    print()
    if harvestable:
        print(f"[smoke] harvestable success: {ok_count}/{len(harvestable)} ({ok_count / len(harvestable):.0%})")
    print(f"[smoke] overall coverage    : {ok_count}/{len(results)} ({ok_count / len(results):.0%})")
    return 0 if not harvestable or ok_count / len(harvestable) >= 0.7 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
