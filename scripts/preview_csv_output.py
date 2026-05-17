"""Preview the harvester's CSV output shape against one repo.

Uses the extractor's lower-level cloner + walker directly (rather than
``ExtractorOrchestrator.extract``) because the writer needs raw
``TagRangeWalk`` data — per-commit tz_offset and SHAs that the standard
``Contributor`` aggregate would collapse away.

Usage:
    uv run python scripts/preview_csv_output.py <repo_url> <ecosystem> <name>

Defaults: lodash if no args given.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from harvester.extractor.extractor_types import ExtractorDeps
from harvester.extractor.git_runner import GitRunner
from harvester.extractor.repo_cloner import RepoCloner
from harvester.extractor.tag_walker import TagWalker
from harvester.writer import CsvWriter

OUT_DIR = Path(__file__).resolve().parent.parent / "output" / "preview"


async def main() -> int:
    repo_url = sys.argv[1] if len(sys.argv) > 1 else "https://github.com/lodash/lodash.git"
    ecosystem = sys.argv[2] if len(sys.argv) > 2 else "npm"
    name = sys.argv[3] if len(sys.argv) > 3 else "lodash"

    deps = replace(
        ExtractorDeps.defaults(),
        clone_timeout_s=600,
        log_timeout_s=300,
    )
    runner = GitRunner(git_executable=deps.git_executable)
    cloner = RepoCloner(deps, runner)
    walker = TagWalker(deps, runner)

    print(f"[preview] extracting {repo_url}")
    t0 = time.perf_counter()
    async with cloner.clone_to_temp(repo_url) as clone_path:
        walks = await walker.walk(clone_path)
    elapsed = time.perf_counter() - t0

    writer = CsvWriter(out_dir=OUT_DIR)
    output = writer.write_all(
        ecosystem=ecosystem,
        name=name,
        repo_url=repo_url,
        synced_at=datetime.now(UTC).isoformat(timespec="seconds"),
        walks=walks,
    )

    print()
    print("[preview] === extraction ===")
    print(f"  versions      : {output.versions_rows}")
    print(f"  contrib rows  : {output.contributors_rows}")
    print(f"  unique persons: {output.persons_rows}")
    print(f"  wall time     : {elapsed:.2f}s")
    print()
    print("[preview] === written ===")
    for path in (
        output.components_path,
        output.versions_path,
        output.contributors_path,
        output.persons_path,
    ):
        print(f"  {path.name:<20} ({path.stat().st_size:>7} bytes)  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
