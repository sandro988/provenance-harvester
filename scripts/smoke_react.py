"""Smoke test the vendored extractor against facebook/react.

Asserts the vendored code produces the same output shape as the
exodos-backend Phase 1b code did when benchmarked on this machine:
- 168 tags
- ~958 unique authors
- ~18s wall time (range 10-60s acceptable)

If this passes, the vendoring is functionally correct.

Usage:
    uv run python scripts/smoke_react.py
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

from harvester.extractor import (
    ExtractorDeps,
    ExtractorOrchestrator,
    ExtractorParams,
)

REPO_URL = "https://github.com/facebook/react.git"

# Bounds from the earlier worktree benchmark — same machine, same network,
# same code. Wide enough to absorb cold-cache vs warm-cache + network jitter.
EXPECTED_TAGS = 168
MIN_UNIQUE_AUTHORS = 800
MAX_WALL_SECONDS = 60.0


async def main() -> int:
    deps = replace(
        ExtractorDeps.defaults(),
        clone_timeout_s=600,
        log_timeout_s=300,
    )
    orchestrator = ExtractorOrchestrator.from_deps(deps)
    params = ExtractorParams(repo_url=REPO_URL, tags_limit=None)

    print(f"[smoke] extracting {REPO_URL}")
    t0 = time.perf_counter()
    result = await orchestrator.extract(params)
    elapsed = time.perf_counter() - t0

    if result.error_kind is not None:
        print(f"[smoke] FAILED: {result.error_kind.value} — {result.error}")
        return 1

    unique_authors = {
        (c.email or c.name)
        for r in result.tag_ranges
        for c in r.contributors
    }

    print(f"[smoke] tags processed         : {len(result.tag_ranges)}")
    print(f"[smoke] unique authors         : {len(unique_authors)}")
    print(f"[smoke] wall time              : {elapsed:.2f}s")

    failures = []
    if len(result.tag_ranges) != EXPECTED_TAGS:
        failures.append(
            f"tags: got {len(result.tag_ranges)}, expected exactly {EXPECTED_TAGS} "
            f"(react's tag history is stable — a mismatch means extraction broke)"
        )
    if len(unique_authors) < MIN_UNIQUE_AUTHORS:
        failures.append(
            f"unique authors: got {len(unique_authors)}, expected >= {MIN_UNIQUE_AUTHORS}"
        )
    if elapsed > MAX_WALL_SECONDS:
        failures.append(
            f"wall time: {elapsed:.2f}s exceeds {MAX_WALL_SECONDS}s ceiling"
        )

    if failures:
        print()
        print("[smoke] FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print()
    print("[smoke] ✓ vendored extractor matches source-of-truth behaviour")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
