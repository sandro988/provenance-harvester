"""ContributorAggregator — pure transform from commits to per-range contributors.

The aggregator is the only collaborator in the extractor that performs
no I/O. It takes an iterable of ``CommitRecord`` objects (already
sliced to one tag range by the tag walker's BFS) and produces the
``list[Contributor]`` that the orchestrator hands to the result.

All methods are ``@staticmethod`` — there is no per-instance state
worth carrying, and statelessness keeps the implementation trivially
testable in isolation.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from harvester.extractor.extractor_types import (
    CommitRecord,
    Contributor,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

# Bot-identity heuristics. Each substring is matched case-insensitively
# against either the author name or the author email. The patterns
# below are the union of what GitHub itself uses for bot accounts,
# common ecosystem bots (Dependabot, Renovate, Greenkeeper, Snyk), and
# CI-generated commit identities (``github-actions``, ``web-flow``).
#
# Note: ``noreply`` is intentionally NOT in this list as a bare
# substring — GitHub's per-user privacy emails look like
# ``12345+username@users.noreply.github.com`` and matching ``noreply``
# alone would flag every privacy-conscious human contributor as a bot.
# The ``[bot]`` suffix on the name and the dedicated bot patterns
# below are sufficient.
BOT_PATTERNS: tuple[str, ...] = (
    "[bot]",
    "dependabot",
    "renovate",
    "github-actions",
    "web-flow",
    "snyk-bot",
    "greenkeeper",
)


class ContributorAggregator:
    """Pure-function bundle for collapsing commits into contributors."""

    @staticmethod
    def detect_bot(name: str, email: str) -> bool:
        """Return ``True`` if either the name or email signals a bot identity.

        Substring match is case-insensitive. The contract is "any one
        pattern hits" — adding a new bot to ``BOT_PATTERNS`` is the
        only place a future change should happen.
        """
        haystack = f"{name} {email}".lower()
        return any(pattern in haystack for pattern in BOT_PATTERNS)

    @staticmethod
    def aggregate(commits: Iterable[CommitRecord]) -> list[Contributor]:
        """Group ``commits`` by ``(name, email)`` into ``Contributor`` rows.

        For each unique identity, the resulting ``Contributor``
        carries:

        - ``commits`` — count of commits in the input
        - ``first_commit_ts`` / ``last_commit_ts`` — min/max of
          ``author_ts`` across those commits
        - ``is_bot`` — :meth:`detect_bot` applied to the identity

        The output list is sorted by descending commit count (most
        active contributors first), then by name to break ties
        deterministically — the bulk loader downstream relies on a
        stable order to make CSV diffs reviewable.
        """
        # ``defaultdict`` over a small mutable accumulator avoids two
        # passes (one to discover identities, one to count). The
        # accumulator is intentionally a tiny tuple of running
        # aggregates rather than a class — it never leaves this
        # function.
        grouped: dict[tuple[str, str], _Accumulator] = defaultdict(_Accumulator)
        for commit in commits:
            grouped[(commit.author_name, commit.author_email)].observe(commit)

        contributors = [
            Contributor(
                name=name,
                email=email,
                commits=acc.commits,
                first_commit_ts=acc.first_ts,
                last_commit_ts=acc.last_ts,
                is_bot=ContributorAggregator.detect_bot(name, email),
            )
            for (name, email), acc in grouped.items()
        ]
        contributors.sort(key=lambda c: (-c.commits, c.name))
        return contributors


class _Accumulator:
    """Mutable running aggregate for one ``(name, email)`` identity.

    Private to this module — never appears in the public API. Plain
    class (not dataclass) so the hot loop can mutate fields without
    going through ``replace()`` or ``__setattr__`` machinery.
    """

    __slots__ = ("commits", "first_ts", "last_ts")

    def __init__(self) -> None:
        self.commits = 0
        # ``first_ts``/``last_ts`` are populated on the first observation;
        # ``min``/``max`` against ``None`` would be a footgun, so they
        # start unset and the ``observe`` call seeds them.
        self.first_ts = None  # type: ignore[assignment]
        self.last_ts = None  # type: ignore[assignment]

    def observe(self, commit: CommitRecord) -> None:
        self.commits += 1
        if self.first_ts is None or commit.author_ts < self.first_ts:
            self.first_ts = commit.author_ts
        if self.last_ts is None or commit.author_ts > self.last_ts:
            self.last_ts = commit.author_ts
