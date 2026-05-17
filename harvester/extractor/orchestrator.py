"""ExtractorOrchestrator — top-level entry point of the sub-package.

The orchestrator is the public face of the extractor: callers
construct one with ``from_deps()`` (or wire it manually for tests),
call ``extract(params)``, and receive a fully-populated
``ExtractionResult``. Internally it chains the three collaborators
in a fixed order:

1. ``RepoCloner.clone_to_temp`` — validate URL, clone to tmpfs
2. ``TagWalker.walk``           — list tags + parse all commits + slice per tag
3. ``ContributorAggregator``    — collapse each slice into ``Contributor`` rows

Failure semantics follow the ``ExtractionResult`` contract: every
shape returns a populated result, never raises out of ``extract``.
Permanent failures (bad URL, non-existent repo, auth) yield a
result with ``error`` set and ``tag_ranges`` empty. Transient
failures after retry exhaustion yield the same shape. Failures
always return ``tag_ranges=[]`` — the walker either succeeds and
produces every tag's slice, or fails and the caller decides whether
to retry the whole extraction. Per-tag partial preservation would
require streaming the walker incrementally, which Phase 1b
deliberately doesn't do; revisit if Phase 1c needs it.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from harvester.extractor._logger import logger
from harvester.extractor.contributor_aggregator import (
    ContributorAggregator,
)
from harvester.extractor.extractor_types import (
    ExtractionErrorKind,
    ExtractionResult,
    ExtractorDeps,
    ExtractorParams,
    TagRangeContributors,
)
from harvester.extractor.git_runner import (
    GitCommandError,
    GitOutputTooLargeError,
    GitRunner,
)
from harvester.extractor.repo_cloner import (
    InvalidRepoUrlError,
    RepoCloner,
)
from harvester.extractor.tag_walker import TagWalker

if TYPE_CHECKING:
    from harvester.extractor.extractor_types import (
        TagRangeWalk,
    )


class ExtractorOrchestrator:
    """Wire the collaborators into a single ``extract`` entry point."""

    def __init__(
        self,
        deps: ExtractorDeps,
        cloner: RepoCloner,
        walker: TagWalker,
    ) -> None:
        self._deps = deps
        self._cloner = cloner
        self._walker = walker

    @classmethod
    def from_deps(cls, deps: ExtractorDeps | None = None) -> ExtractorOrchestrator:
        """Wire a production orchestrator from ``deps`` (or defaults).

        Constructs one shared ``GitRunner`` and threads it into both
        the cloner and the walker so a future change to subprocess
        handling has exactly one place to land.
        """
        effective_deps = deps if deps is not None else ExtractorDeps.defaults()
        runner = GitRunner(git_executable=effective_deps.git_executable)
        return cls(
            deps=effective_deps,
            cloner=RepoCloner(effective_deps, runner),
            walker=TagWalker(effective_deps, runner),
        )

    async def extract(self, params: ExtractorParams) -> ExtractionResult:
        """Run the full clone → walk → aggregate pipeline.

        Every documented and undocumented failure path collapses
        into ``ExtractionResult.error`` rather than propagating —
        callers consume one shape regardless of how the underlying
        work went. ``CancelledError`` from a parent task is the one
        exception: it re-raises so structured cancellation in
        Celery / FastAPI keeps working.
        """
        try:
            async with self._cloner.clone_to_temp(params.repo_url) as clone_path:
                walks = await self._walker.walk(
                    clone_path, path_filter=params.path_filter
                )
        except asyncio.CancelledError:
            # Structured cancellation must propagate untouched —
            # absorbing it here would make Celery / parent-task
            # shutdown impossible to observe.
            raise
        except InvalidRepoUrlError as error:
            return self._failure(params, ExtractionErrorKind.INVALID_URL, str(error))
        except GitCommandError as error:
            return self._failure(params, ExtractionErrorKind.GIT_COMMAND, str(error))
        except GitOutputTooLargeError as error:
            return self._failure(
                params, ExtractionErrorKind.GIT_OUTPUT_TOO_LARGE, str(error)
            )
        except TimeoutError:
            return self._failure(
                params,
                ExtractionErrorKind.TIMEOUT,
                "git invocation timed out after configured retries",
            )
        except OSError as error:
            # Disk full, EACCES on /tmp, descriptor exhaustion. The
            # cloner uses ``tempfile.mkdtemp`` and ``shutil.rmtree``,
            # both of which surface OS-level conditions as OSError.
            return self._failure(params, ExtractionErrorKind.FILESYSTEM, str(error))
        except Exception as error:
            # Last-resort catch for the "never raises" contract.
            # ``logger.exception`` keeps the stack trace in production
            # logs so the unanticipated path is investigable; the
            # caller still sees the unified result shape.
            logger.exception(
                "Extractor failed with unexpected error",
                repo_url=params.repo_url,
            )
            return self._failure(params, ExtractionErrorKind.UNEXPECTED, str(error))

        ranges = self._aggregate_walks(walks, params)
        return ExtractionResult(
            repo_url=params.repo_url,
            default_branch="",  # populated by Phase 1c when the integration needs it
            tag_ranges=ranges,
        )

    @staticmethod
    def _aggregate_walks(
        walks: list[TagRangeWalk],
        params: ExtractorParams,
    ) -> list[TagRangeContributors]:
        """Apply ``tags_limit`` and run the aggregator per walk row.

        ``walks`` arrives oldest-first (driven by
        ``for-each-ref --sort=creatordate``); ``tags_limit`` keeps
        the most recent N so smoke tests exercise the current tail
        of release history rather than ancient prehistory.
        """
        sliced = walks[-params.tags_limit :] if params.tags_limit else walks
        return [
            TagRangeContributors(
                tag=walk.this.name,
                prev_tag=walk.prev.name if walk.prev is not None else None,
                tag_sha=walk.this.sha,
                released_at=walk.this.creator_date,
                contributors=ContributorAggregator.aggregate(walk.commits),
            )
            for walk in sliced
        ]

    @staticmethod
    def _failure(
        params: ExtractorParams,
        error_kind: ExtractionErrorKind,
        error_detail: str,
    ) -> ExtractionResult:
        """Assemble an ``ExtractionResult`` for a failure path.

        ``error_kind`` is the stable category; ``error_detail`` is
        the variable diagnostic copy. The result carries both: the
        kind for callers to switch on, and the joined string as
        ``error`` for log lines and operator triage.
        """
        message = f"{error_kind.value}: {error_detail}"
        logger.warning(
            "Extractor failed",
            repo_url=params.repo_url,
            error_kind=error_kind.value,
            error_detail=error_detail,
        )
        return ExtractionResult(
            repo_url=params.repo_url,
            default_branch="",
            tag_ranges=[],
            error=message,
            error_kind=error_kind,
        )
