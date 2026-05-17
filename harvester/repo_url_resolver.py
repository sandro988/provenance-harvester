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

import structlog

from harvester.deps_dev_client import DepsDevClient
from harvester.ecosystems_client import EcosystemsClient

logger = structlog.get_logger("harvester.repo_url_resolver")


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
        primary = self._ecosystems.resolve_repo_url(ecosystem, namespace, name)
        if primary:
            return primary

        fallback = self._deps_dev.resolve_repo_url(ecosystem, namespace, name)
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
