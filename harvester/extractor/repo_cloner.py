"""RepoCloner — SSRF-validated bare clone with retry and temp cleanup.

The cloner is the second I/O collaborator (after ``GitRunner``).
It performs three jobs end to end, in order:

1. Validate the supplied repo URL against the SSRF allowlist —
   refusing anything outside the configured hosts before any
   subprocess is spawned.
2. Create a temporary directory, clone the repo into it with
   ``--bare --filter=blob:none --no-checkout`` (small clones that
   disk-efficient form for our read-only access pattern), retrying
   on transient errors with linear backoff.
3. Yield the clone path to the caller and, on context exit, delete
   the temporary tree — no matter how the caller leaves the block.

The async-context-manager surface is deliberate: it forces callers
into the structured-cleanup idiom and removes the chance of a
caller forgetting to remove the temp directory after a partial
read.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from harvester.extractor._logger import logger
from harvester.extractor.git_runner import (
    GitCommandError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from harvester.extractor.extractor_types import (
        ExtractorDeps,
    )
    from harvester.extractor.git_runner import GitRunner


# Substrings (lower-case) that, when seen in git's stderr, mark the
# failure as worth retrying. Conservative on purpose: anything not
# matching is treated as permanent and raised on the first attempt.
_TRANSIENT_STDERR_PATTERNS: tuple[str, ...] = (
    "could not resolve",
    "connection reset",
    "timed out",
    "early eof",
    "remote end hung up",
)

# Path-prefix segments on github.com that look like a repo
# ("github.com/<thing>/<thing>") but aren't. Rejecting these prevents
# accidental clone attempts against profile / sponsor / marketplace
# pages. Only applied when host is github.com — gitlab/bitbucket may
# legitimately have groups with these names.
_NON_REPO_GITHUB_PREFIXES: frozenset[str] = frozenset(
    {"sponsors", "settings", "orgs", "marketplace", "features"}
)

# Schemes ``urllib.parse.urlsplit`` may produce that we accept. Any
# other scheme is rejected — including ``file://``, ``javascript:``,
# ``data:``, and the like.
_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https", "git", "ssh"})

# A path segment that is safe to hand to git as part of a repo path.
# Starts with an alphanumeric — that rejects leading-dash inputs (no
# git CLI ambiguity at the argv level) and leading-dot inputs
# (``..`` traversal, hidden refs). Body characters are the ones
# real-world hosts use for owner / repo names.
_SAFE_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# SCP-like git URL: ``git@host:owner/repo``. Captures host and path.
# The non-``:`` host class also rejects port-in-host bypasses like
# ``github.com:attacker.com``.
_SCP_URL_RE = re.compile(r"^git@([A-Za-z0-9.\-]+):(.+)$")

# Characters that have no business appearing in a repo URL we just
# pulled from a BOM. NUL truncates strings inside some downstream
# parsers; CR/LF/TAB confuse log aggregators; whitespace is never
# part of a valid URL.
_FORBIDDEN_URL_CHARS: frozenset[str] = frozenset("\x00\r\n\t ")


class InvalidRepoUrlError(ValueError):
    """Raised when a repo URL fails the SSRF allowlist or shape check."""


def validate_repo_url(repo_url: str, allowed_hosts: frozenset[str]) -> None:
    """Raise :class:`InvalidRepoUrlError` if ``repo_url`` is not safe to clone.

    Defense layers, in order:

    1. Reject control characters and whitespace up front — they have
       no legitimate use in a repo URL and can confuse downstream
       parsers (NUL truncation, CRLF log smuggling).
    2. Parse with ``urllib.parse.urlsplit`` for scheme-prefixed URLs
       or a strict regex for the SCP ``git@host:path`` form. This
       avoids substring-matching tricks like
       ``https://github.com:attacker.com/...``.
    3. Validate the host against ``allowed_hosts``.
    4. Validate every path segment against a conservative
       allow-pattern — owner / repo only, no leading dashes (which
       would otherwise survive into git's argv), no ``..`` traversal.

    This is a pure function — no I/O, no DNS lookup. The allowlist
    is the only line of SSRF defense, so callers must populate
    ``allowed_hosts`` from a trusted configuration source.
    """
    if not repo_url:
        raise InvalidRepoUrlError("Repo URL is empty")
    if any(char in _FORBIDDEN_URL_CHARS for char in repo_url):
        raise InvalidRepoUrlError(
            f"Repo URL contains control or whitespace characters: {repo_url!r}"
        )

    host, segments = _parse_repo_url(repo_url)
    if host is None:
        raise InvalidRepoUrlError(f"Repo URL is unparseable: {repo_url}")
    if host.lower() not in allowed_hosts:
        raise InvalidRepoUrlError(
            f"Repo host {host!r} is not in allowlist "
            f"{sorted(allowed_hosts)}: {repo_url}"
        )
    if len(segments) < 2:
        raise InvalidRepoUrlError(
            f"Repo path does not look like owner/repo: {repo_url}"
        )
    if host.lower() == "github.com" and segments[0] in _NON_REPO_GITHUB_PREFIXES:
        raise InvalidRepoUrlError(
            f"Repo path uses a known non-repo GitHub prefix: {repo_url}"
        )
    for segment in segments:
        if not _SAFE_PATH_SEGMENT_RE.match(segment):
            raise InvalidRepoUrlError(
                f"Repo path segment {segment!r} is not a safe identifier: {repo_url}"
            )


def redact_url(repo_url: str) -> str:
    """Return ``repo_url`` with userinfo stripped for log-safe printing.

    Strips ``user:password@`` from URL-form URLs (e.g.
    ``https://x-access-token:ghp_…@github.com/…``). SCP-form URLs
    (``git@host:owner/repo``) carry no separable credentials so they
    pass through unchanged.

    Best-effort: malformed URLs that don't parse cleanly come back
    as a fixed sentinel rather than the raw string, so a logging
    site cannot accidentally leak credentials by formatting an
    unredacted URL the caller forgot to pre-redact.
    """
    if any(char in _FORBIDDEN_URL_CHARS for char in repo_url):
        return "<redacted: malformed url>"
    try:
        parsed = urllib.parse.urlsplit(_strip_git_prefix(repo_url))
    except ValueError:
        return "<redacted: malformed url>"
    if not parsed.scheme:
        # SCP form or no scheme — no userinfo to strip.
        return repo_url
    safe_netloc = parsed.hostname or ""
    if parsed.port is not None:
        safe_netloc = f"{safe_netloc}:{parsed.port}"
    return urllib.parse.urlunsplit(
        (parsed.scheme, safe_netloc, parsed.path, parsed.query, parsed.fragment)
    )


class RepoCloner:
    """Clone repos into managed temporary directories."""

    def __init__(self, deps: ExtractorDeps, runner: GitRunner) -> None:
        self._deps = deps
        self._runner = runner

    @asynccontextmanager
    async def clone_to_temp(self, repo_url: str) -> AsyncIterator[Path]:
        """Async-context-manager that clones ``repo_url`` and yields the path.

        The yielded path is the cloned ``.git`` directory (bare clone).
        On exit — whether the caller returned normally, raised, or was
        cancelled — the temporary parent directory is removed.

        Raises:
            InvalidRepoUrlError: When the URL fails the allowlist.
            asyncio.TimeoutError: When every clone attempt times out.
            GitCommandError: When git exits non-zero on a permanent
                failure (404, auth) or when retries are exhausted on
                a transient failure.
        """
        validate_repo_url(repo_url, self._deps.allowed_hosts)
        target_parent = Path(
            await asyncio.to_thread(tempfile.mkdtemp, prefix="provenance-clone-")
        )
        clone_target = target_parent / "repo.git"
        try:
            await self._clone_with_retry(repo_url, clone_target)
            yield clone_target
        finally:
            # ``ignore_errors`` so a stale lock file from a half-finished
            # clone (or an inflight git process still releasing handles)
            # cannot mask the original exception with a cleanup error.
            # ``asyncio.to_thread`` keeps the event loop responsive when
            # the tree is large or on slow disks.
            await asyncio.to_thread(shutil.rmtree, target_parent, ignore_errors=True)

    async def _clone_with_retry(self, repo_url: str, target: Path) -> None:
        max_attempts = max(1, self._deps.clone_max_attempts)
        redacted = redact_url(repo_url)
        for attempt in range(1, max_attempts + 1):
            try:
                await self._run_clone(repo_url, target)
                return
            except (TimeoutError, GitCommandError) as error:
                if attempt == max_attempts or not _is_transient(error):
                    raise
                backoff = self._deps.clone_backoff_base_seconds * attempt
                logger.warning(
                    "Transient clone failure; retrying after backoff",
                    repo_url=redacted,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    backoff_seconds=backoff,
                    error_class=type(error).__name__,
                )
                # A failed clone may have left a partial target directory
                # in place; git would refuse to clone into a non-empty
                # destination on the next attempt, turning a transient
                # failure into a permanent one.
                await asyncio.to_thread(shutil.rmtree, target, ignore_errors=True)
                await asyncio.sleep(backoff)

    async def _run_clone(self, repo_url: str, target: Path) -> None:
        # ``--bare`` + ``--filter=blob:none`` + ``--no-checkout``
        # keeps all commit + tree objects local while deferring file
        # contents until something actually reads them. Trees MUST be
        # local because ``git log -- <path>`` (used by the per-package
        # path filter) computes per-commit diffs to determine which
        # commits touched the path — that diff needs both sides'
        # trees. The smaller ``--filter=tree:0`` defers tree fetches
        # too, but the on-demand promisor protocol does not reliably
        # produce historical trees needed by a path-filter walk
        # (commit-graph references can outrun what the remote will
        # serve back on-demand). Trees are typically 10–30% of repo
        # size, an acceptable cost for a reliable walk.
        #
        # The literal ``--`` before ``repo_url`` is defense-in-depth:
        # the validator already rejects leading-dash segments, but a
        # belt-and-braces end-of-options marker prevents any future
        # git CLI ambiguity from being weaponized through this argv.
        await self._runner.run(
            (
                "clone",
                "--bare",
                "--filter=blob:none",
                "--no-checkout",
                "--",
                repo_url,
                str(target),
            ),
            timeout=self._deps.clone_timeout_s,
        )


def _strip_git_prefix(url: str) -> str:
    """Strip ``git+`` from URLs like ``git+https://github.com/…``.

    ``git+`` is a pip-style convention git itself does not understand;
    callers sometimes hand us URLs in that form. Trimming once here
    keeps the rest of the parser ignorant of the convention.
    """
    return url[4:] if url.startswith("git+") else url


def _parse_repo_url(repo_url: str) -> tuple[str | None, list[str]]:
    """Return ``(host, path_segments)`` or ``(None, [])`` on failure.

    Two branches:

    - SCP form (``git@host:owner/repo``) — matched via strict regex
      so the host cannot smuggle a ``:`` into itself.
    - URL form — parsed via ``urllib.parse.urlsplit`` so embedded
      credentials, ports, IDN, and query strings are handled by the
      standard library rather than a hand-rolled split.

    ``.git`` suffix on the final segment is stripped before
    returning so callers don't have to special-case it.
    """
    stripped = _strip_git_prefix(repo_url)

    scp_match = _SCP_URL_RE.match(stripped)
    if scp_match is not None:
        host, raw_path = scp_match.group(1), scp_match.group(2)
        return host, _path_segments(raw_path)

    try:
        parsed = urllib.parse.urlsplit(stripped)
    except ValueError:
        return None, []

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return None, []
    try:
        # ``.port`` raises ValueError when the netloc has a port-like
        # but non-numeric segment (the classic
        # ``host:attacker.com`` trick).
        _ = parsed.port
    except ValueError:
        return None, []
    host = parsed.hostname
    if host is None or not host:
        return None, []
    return host, _path_segments(parsed.path)


def _path_segments(raw_path: str) -> list[str]:
    """Split ``raw_path`` into ``[segment, …]`` minus any ``.git`` tail."""
    trimmed = raw_path.rstrip("/")
    if trimmed.endswith(".git"):
        trimmed = trimmed[: -len(".git")]
    return [segment for segment in trimmed.split("/") if segment]


def _is_transient(error: BaseException) -> bool:
    """Classify a clone failure as worth retrying.

    Network-shaped failures (timeout, reset, DNS, hung-up remote)
    are transient. Everything else — auth, 404, malformed URL —
    raises on the first attempt to avoid burning the retry budget
    on a request that will keep failing identically.
    """
    if isinstance(error, asyncio.TimeoutError):
        return True
    if isinstance(error, GitCommandError):
        lowered = error.stderr.lower()
        return any(pattern in lowered for pattern in _TRANSIENT_STDERR_PATTERNS)
    return False
