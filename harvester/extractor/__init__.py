"""Per-version contributor extractor.

Sub-package replacing the legacy single-author-per-tag service with a
single-pass ``git log --all`` + pure-Python BFS over tag pairs. Public
surface is intentionally narrow: ``ExtractorOrchestrator`` (wired in
the final commit) takes an ``ExtractorParams`` and returns an
``ExtractionResult``.

Collaborators:

- ``git_runner`` — async ``git`` subprocess wrapper (single I/O point)
- ``git_log_parser`` — pure text → typed-object parsers
- ``commit_graph`` — pure graph operations (BFS, tag pairing)
- ``tag_walker`` — thin orchestrator over the three above
- ``contributor_aggregator`` — pure transform: commits → ``Contributor`` list
- ``repo_cloner`` — SSRF-validated bare clone with retry and cleanup (next commit)
"""

from harvester.extractor.contributor_aggregator import (
    BOT_PATTERNS,
    ContributorAggregator,
)
from harvester.extractor.extractor_types import (
    ALLOWED_GIT_HOSTS,
    CommitRecord,
    Contributor,
    ExtractionErrorKind,
    ExtractionResult,
    ExtractorDeps,
    ExtractorParams,
    TagInfo,
    TagRangeContributors,
    TagRangeWalk,
)
from harvester.extractor.git_runner import (
    GitCommandError,
    GitOutputTooLargeError,
    GitRunner,
)
from harvester.extractor.orchestrator import (
    ExtractorOrchestrator,
)
from harvester.extractor.repo_cloner import (
    InvalidRepoUrlError,
    RepoCloner,
    redact_url,
    validate_repo_url,
)
from harvester.extractor.tag_walker import TagWalker

__all__ = [
    "ALLOWED_GIT_HOSTS",
    "BOT_PATTERNS",
    "CommitRecord",
    "Contributor",
    "ContributorAggregator",
    "ExtractionErrorKind",
    "ExtractionResult",
    "ExtractorDeps",
    "ExtractorOrchestrator",
    "ExtractorParams",
    "GitCommandError",
    "GitOutputTooLargeError",
    "GitRunner",
    "InvalidRepoUrlError",
    "RepoCloner",
    "TagInfo",
    "TagRangeContributors",
    "TagRangeWalk",
    "TagWalker",
    "redact_url",
    "validate_repo_url",
]
