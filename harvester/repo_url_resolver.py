"""Repository URL resolution with multi-source fallback.

The harvester needs a source repository URL for every component it
intends to clone. Different package registries publish this metadata
with very different reliability:

- ecosyste.ms covers the most ecosystems (npm, pypi, maven, cargo, gem,
  go, composer, nuget, cocoapods, hex, pub, conan, luarocks, pear) but
  its repository-URL coverage is uneven, especially for Maven.
- deps.dev covers fewer ecosystems (npm, pypi, maven, cargo, nuget, go)
  but often surfaces repo URLs that ecosyste.ms misses, particularly
  for Maven where it can read the POM's ``<scm>`` block directly.

This module composes the two: ecosyste.ms first (broader coverage),
deps.dev as a fallback for the gaps it can help with. The order
matters — ecosyste.ms is faster (one HTTP call vs two) and supports
more registries.
"""

from __future__ import annotations

import re

import structlog

from harvester.deps_dev_client import DepsDevClient
from harvester.ecosystems_client import EcosystemsClient

logger = structlog.get_logger("harvester.repo_url_resolver")

# Strip GitHub/GitLab/Bitbucket subfolder URLs back to their clone root.
# Registries sometimes publish the web URL of the package's sub-directory
# inside a monorepo (``…/repo/tree/main/crates/foo``) instead of the bare
# clone URL — git clone rejects the former. The slug capture stops at the
# first ``/tree`` or ``/blob`` segment (or GitLab's ``/-/tree`` variant).
_SUBPATH_PATTERNS = (
    re.compile(r"^(https?://github\.com/[^/]+/[^/]+)/(?:tree|blob)/.+$"),
    re.compile(r"^(https?://gitlab\.com/[^/]+/[^/]+)/-/(?:tree|blob)/.+$"),
    re.compile(r"^(https?://bitbucket\.org/[^/]+/[^/]+)/src/.+$"),
)


def normalise_clone_url(url: str | None) -> str | None:
    """Return a URL git can clone, stripping web-UI subpath suffixes.

    Registries occasionally publish ``…/<repo>/tree/<branch>/<subpath>``
    for packages that live in a monorepo subdirectory (e.g. opentelemetry-
    rust's ``opentelemetry-sdk`` crate). The trailing path isn't accepted
    by ``git clone``; we keep just the repo-root portion. URLs that
    don't match any host pattern are returned unchanged.
    """
    if not url:
        return url
    for pattern in _SUBPATH_PATTERNS:
        match = pattern.match(url)
        if match:
            return match.group(1)
    return url


class RepoUrlResolver:
    """Composite resolver: ecosyste.ms primary, deps.dev fallback."""

    def __init__(
        self,
        ecosystems: EcosystemsClient,
        deps_dev: DepsDevClient,
    ) -> None:
        self._ecosystems = ecosystems
        self._deps_dev = deps_dev

    def resolve(self, ecosystem: str, namespace: str, name: str) -> str | None:
        """Return the package's source repository URL, or ``None`` if neither source has it."""
        primary = normalise_clone_url(self._ecosystems.resolve_repo_url(ecosystem, namespace, name))
        if primary:
            return primary

        fallback = normalise_clone_url(self._deps_dev.resolve_repo_url(ecosystem, namespace, name))
        if fallback:
            logger.info(
                "resolver.recovered_via_deps_dev",
                ecosystem=ecosystem,
                namespace=namespace,
                name=name,
                url=fallback,
            )
            return fallback

        return None
