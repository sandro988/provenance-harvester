"""Pure graph operations over the commits dict produced by the parser.

Two functions, both module-level, both pure: take ``CommitRecord``
data as input, return derived structures, never touch I/O.

``bfs_per_tag_range`` walks the commit graph **once** over all tags
in chronological order. A running ``visited`` set ensures every
commit is attributed to the earliest tag whose ancestry contains
it — the same semantics as ``git log prev..this`` for
chronologically ordered tags, but in O(C) total work rather than
O(N × C) where N is the tag count. That single-pass scaling is the
performance claim Phase 1b makes against the legacy 12×-subprocess
service; doing per-tag full reachability walks instead would
restore exactly the cost the rewrite was designed to eliminate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harvester.extractor._logger import logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from harvester.extractor.extractor_types import (
        CommitRecord,
        TagInfo,
    )


def pair_tags_chronologically(
    tags: list[TagInfo],
) -> list[tuple[TagInfo | None, TagInfo]]:
    """Pair each tag with its chronological predecessor.

    Returns ``[(None, tag0), (tag0, tag1), (tag1, tag2), ...]``.
    Empty input returns an empty list. The leading ``None`` is the
    signal that the range for ``tag0`` is the full history reachable
    from it — no earlier baseline exists to subtract.

    Input must already be sorted oldest-first; this function does
    not re-sort. ``parse_tag_lines`` produces that order when the
    underlying git command uses ``--sort=creatordate``.
    """
    if not tags:
        return []
    predecessors: list[TagInfo | None] = [None, *tags[:-1]]
    return list(zip(predecessors, tags, strict=True))


def bfs_per_tag_range(
    commits: Mapping[str, CommitRecord],
    tags: list[TagInfo],
) -> dict[str, list[CommitRecord]]:
    """Slice ``commits`` into one list per tag, single-pass.

    Walks the commit graph exactly once across all tags. For each
    tag in input order, BFS from its SHA via parent edges,
    accumulating commits that have not been claimed by an earlier
    tag's walk. The first tag receives its full reachable history;
    each subsequent tag receives only what is new relative to all
    earlier tags' ancestries — the same set ``git log prev..this``
    produces for chronologically ordered tags.

    A tag whose SHA is absent from ``commits`` produces an empty
    range and a warning log line. The most common cause is the SHA
    being filtered out by ``--no-merges`` in the upstream log
    command; the warning makes that case visible without
    overcounting the subsequent tag's range. The walk also handles
    pathological cycles (which real git cannot produce) safely
    because every visited SHA is added to ``visited`` exactly once.
    """
    result: dict[str, list[CommitRecord]] = {}
    visited: set[str] = set()
    for tag in tags:
        if tag.sha not in commits:
            logger.warning(
                "Tag points at unknown commit, range is empty",
                tag=tag.name,
                sha=tag.sha,
            )
            result[tag.name] = []
            continue
        records_in_range: list[CommitRecord] = []
        stack = [tag.sha]
        while stack:
            sha = stack.pop()
            if sha in visited:
                continue
            visited.add(sha)
            commit = commits.get(sha)
            if commit is None:
                continue
            records_in_range.append(commit)
            stack.extend(commit.parents)
        result[tag.name] = records_in_range
    return result
