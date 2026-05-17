"""TagWalker — thin orchestrator over runner, parser, and graph helpers.

Composes the four pieces required to turn a cloned repository into
a per-tag commit slice:

1. ``GitRunner.run`` to fetch tag refs   → text
2. ``parse_tag_lines``                   → list[TagInfo]
3. ``GitRunner.run`` to fetch full log   → text
4. ``parse_commit_lines``                → dict[sha, CommitRecord]
5. ``pair_tags_chronologically``         → list[(prev, this)]
6. ``bfs_per_tag_range``                 → dict[tag_name, list[CommitRecord]]

Every step except the two runner calls is pure and unit-tested in
isolation. The walker itself contains no logic worth testing
beyond "does it wire the steps together in the right order with
the right inputs," which a fake ``GitRunner`` exercises cleanly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harvester.extractor.commit_graph import (
    bfs_per_tag_range,
    pair_tags_chronologically,
)
from harvester.extractor.extractor_types import (
    ExtractorDeps,
    TagRangeWalk,
)
from harvester.extractor.git_log_parser import (
    COMMIT_LOG_FORMAT,
    TAG_REF_FORMAT,
    parse_commit_lines,
    parse_tag_lines,
)

if TYPE_CHECKING:
    from pathlib import Path

    from harvester.extractor.git_runner import GitRunner


class TagWalker:
    """Wire the parser and graph helpers around a ``GitRunner``."""

    def __init__(self, deps: ExtractorDeps, runner: GitRunner) -> None:
        self._deps = deps
        self._runner = runner

    async def walk(
        self,
        repo_dir: Path,
        *,
        path_filter: str | None = None,
    ) -> list[TagRangeWalk]:
        """Return one ``TagRangeWalk`` per tag, oldest first.

        An empty list is returned when the repo has no tags — that
        is a legitimate state, not an error (early-stage repos, or
        registry packages whose source lives unmaintained on a
        branch). The orchestrator decides whether to escalate.

        ``path_filter`` narrows the contributor attribution to
        commits that touched the given repo-relative path. Tag
        enumeration and the main commit log remain repo-wide — the
        BFS needs the full parent graph to compute reachability per
        tag, so we cannot pre-filter the log itself. Instead, a
        cheap second log fetches the SHA set of commits touching
        the path and we intersect each range's commits with that
        set. ``None`` skips the second log and the filter.

        Raises:
            asyncio.TimeoutError: When either git invocation
                exceeds ``deps.log_timeout_s``.
            GitCommandError: When git exits non-zero.
        """
        tag_lines = await self._runner.run(
            (
                "for-each-ref",
                "--sort=creatordate",
                f"--format={TAG_REF_FORMAT}",
                "refs/tags",
            ),
            timeout=self._deps.log_timeout_s,
            repo_dir=repo_dir,
        )
        tags = parse_tag_lines(tag_lines)
        if not tags:
            return []

        # ``--no-use-mailmap`` is deliberate, not a default. Git 2.6+
        # ships with ``log.mailmap=true``, so omitting the flag would
        # silently apply the repo's ``.mailmap`` — which lives inside
        # the cloned repo and is therefore attacker-controlled. A
        # malicious package could rewrite author identities to claim
        # well-known maintainers authored its code, defeating the
        # entire point of this extractor. Personas modeling (a
        # downstream phase) handles legitimate identity collapse in
        # a context where we control the input.
        commit_lines = await self._runner.run(
            (
                "log",
                "--all",
                "--no-merges",
                "--no-use-mailmap",
                f"--pretty=format:{COMMIT_LOG_FORMAT}",
            ),
            timeout=self._deps.log_timeout_s,
            repo_dir=repo_dir,
        )
        commits = parse_commit_lines(commit_lines)

        ranges = bfs_per_tag_range(commits, tags)
        if path_filter is not None:
            path_shas = await self._fetch_path_touching_shas(repo_dir, path_filter)
            ranges = {
                tag_name: [commit for commit in tag_commits if commit.sha in path_shas]
                for tag_name, tag_commits in ranges.items()
            }
        pairs = pair_tags_chronologically(tags)
        return [
            TagRangeWalk(prev=prev, this=this, commits=ranges[this.name])
            for prev, this in pairs
        ]

    async def _fetch_path_touching_shas(
        self,
        repo_dir: Path,
        path_filter: str,
    ) -> set[str]:
        """Return the SHA set of commits that touched ``path_filter``.

        Runs ``git log --all --no-merges --pretty=%H -- <path>`` —
        a SHA-only output mode that skips author/parent/timestamp
        decoding and is roughly an order of magnitude cheaper than
        the main log we already paid for. ``--no-merges`` matches
        the main log's filter so the SHA set lines up with the
        commits the BFS placed into ranges.

        The path is passed as a separate argv element after ``--``,
        keeping it out of git's option parsing space — a path that
        happens to start with ``-`` (rare but legal) cannot be
        misread as a flag.
        """
        sha_lines = await self._runner.run(
            (
                "log",
                "--all",
                "--no-merges",
                "--pretty=format:%H",
                "--",
                path_filter,
            ),
            timeout=self._deps.log_timeout_s,
            repo_dir=repo_dir,
        )
        return {line.strip() for line in sha_lines.splitlines() if line.strip()}
