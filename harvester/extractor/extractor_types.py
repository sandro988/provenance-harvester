"""Value objects and dependency container for the extractor sub-package.

The extractor takes a repository URL and produces a per-tag-range
contributor breakdown — one ``TagRangeContributors`` for every tag in
chronological order, listing the contributors whose commits land in
``(prev_tag, this_tag]``. The first tag in the range gets the full
reachable history because no earlier baseline exists.

These types are intentionally schema-agnostic. They describe what an
extraction produces, not how it ends up in Neo4j. The bulk loader in
Phase 3a maps these to ``:Version`` and ``:Person`` nodes once the
graph schema lands.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

# Allowlist of trusted git hosts. The repo cloner consults this set
# before any subprocess call to ``git clone`` — without it, a hostile
# BOM-supplied repo URL could trick a worker into cloning from
# internal infrastructure or arbitrary external servers.
#
# Mirrored from the legacy ``GitHubContributorsService`` constant. When
# a second consumer appears we should extract this to a shared util;
# until then a single copy keeps the strangler-fig boundary clean.
ALLOWED_GIT_HOSTS: frozenset[str] = frozenset(
    {"github.com", "gitlab.com", "bitbucket.org"}
)


@dataclass(frozen=True, slots=True)
class ExtractorDeps:
    """Configuration knobs shared across the extractor's collaborators.

    Unlike the ``saga``/``failure_cache``/``single_flight`` Deps
    classes, this one owns no async resources — there is nothing to
    close. ``defaults()`` exists for API symmetry and to give tests a
    single place to override the SSRF allowlist or git binary path
    without monkey-patching module constants.
    """

    allowed_hosts: frozenset[str] = ALLOWED_GIT_HOSTS
    git_executable: str = "git"
    clone_timeout_s: int = 60
    log_timeout_s: int = 30
    # Clone retries only kick in for *transient* failures (DNS,
    # reset, "early EOF") — permanent failures (404, auth) raise on
    # the first attempt. Linear backoff is ``base * attempt``:
    # default produces 1s, 2s before giving up after 3 tries.
    clone_max_attempts: int = 3
    clone_backoff_base_seconds: float = 1.0

    @classmethod
    def defaults(cls) -> ExtractorDeps:
        """Return production defaults — no resources to wire up."""
        return cls()


@dataclass(frozen=True, slots=True)
class ExtractorParams:
    """Per-extraction inputs.

    ``tags_limit`` caps the number of tags processed, taking the
    most recent N — so a smoke test against React with
    ``tags_limit=5`` exercises the latest five releases rather than
    five releases from 2013. Leave it ``None`` to process every tag.

    ``clone_depth`` is deliberately absent: the extractor needs the
    full commit history to walk parent pointers per tag range, and
    a shallow clone (``--depth N``) would truncate ancestors the BFS
    requires. Bulk harvest always uses full bare clones via
    ``--filter=tree:0``.
    """

    repo_url: str
    tags_limit: int | None = None


@dataclass(frozen=True, slots=True)
class TagInfo:
    """Identifier triple for a git tag.

    ``creator_date`` comes from ``git for-each-ref --sort=creatordate``
    and is the most consistent cross-ecosystem proxy for "when this
    version shipped" — registry upload timestamps live elsewhere and
    are the bulk loader's problem.
    """

    name: str
    sha: str
    creator_date: datetime


@dataclass(frozen=True, slots=True)
class CommitRecord:
    """A single commit parsed from ``git log --all`` output.

    The tag walker produces these as the intermediate representation
    between git's textual log output and the per-tag-range
    aggregation. ``parents`` is a tuple so the record stays hashable
    and the BFS can build its reachability map without copying.

    ``author_ts`` is the author timestamp (``%at``), not the commit
    timestamp — author dates survive rebase and reflect when the
    work was actually written, which is what "contributor activity"
    means in practice.
    """

    sha: str
    parents: tuple[str, ...]
    author_name: str
    author_email: str
    author_ts: datetime


@dataclass(frozen=True, slots=True)
class TagRangeWalk:
    """One tag's slice of commit history, before aggregation.

    Produced by ``TagWalker.walk()`` as the intermediate handoff to
    the orchestrator. The orchestrator runs the aggregator over
    ``commits`` to turn this into a ``TagRangeContributors``.
    Keeping the raw commits visible here (rather than collapsing to
    contributors inside the walker) lets the equivalence test in
    commit 5 compare commit sets directly against the legacy
    service's output.
    """

    prev: TagInfo | None
    this: TagInfo
    commits: list[CommitRecord]


@dataclass(frozen=True, slots=True)
class Contributor:
    """One contributor's footprint within a single tag range.

    ``commits`` is the count within the range, not the lifetime total
    across the repo. ``first_commit_ts`` and ``last_commit_ts`` bound
    the contributor's activity inside that same range.
    """

    name: str
    email: str
    commits: int
    first_commit_ts: datetime
    last_commit_ts: datetime
    is_bot: bool


@dataclass(frozen=True, slots=True)
class TagRangeContributors:
    """Contributors whose commits fall in ``(prev_tag, this_tag]``.

    ``prev_tag`` is ``None`` for the first tag in chronological order
    — the range is the full history reachable from that tag, since no
    earlier baseline exists to subtract.

    ``released_at`` is the tag's creator-date (``git
    for-each-ref --sort=creatordate``), which is the most consistent
    cross-ecosystem proxy for "when this version shipped." Some
    registries publish a different upload timestamp; reconciling
    those is the bulk loader's problem, not ours.
    """

    tag: str
    prev_tag: str | None
    tag_sha: str
    released_at: datetime
    contributors: list[Contributor]


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """Top-level output: per-tag contributor sets plus repo metadata.

    ``error`` is the human-readable diagnostic; ``error_kind`` is the
    stable machine-matchable category. Callers that branch on the
    failure shape (retry, cache negative, alert) should switch on
    ``error_kind``; ``error`` is for log lines and operator triage.
    Both are ``None`` on success.
    """

    repo_url: str
    default_branch: str
    tag_ranges: list[TagRangeContributors]
    error: str | None = None
    error_kind: ExtractionErrorKind | None = None


class ExtractionErrorKind(str, Enum):
    """Stable category for ``ExtractionResult.error``.

    Members are the contract callers branch on. Adding a new kind is
    a deliberate API extension — orchestrator + tests must update in
    the same commit.
    """

    INVALID_URL = "invalid_url"
    GIT_COMMAND = "git_command"
    GIT_OUTPUT_TOO_LARGE = "git_output_too_large"
    TIMEOUT = "timeout"
    FILESYSTEM = "filesystem"
    UNEXPECTED = "unexpected"
