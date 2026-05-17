"""HTTP client for the public ecosyste.ms packages API.

Two operations the harvester needs across multiple flows:

- :func:`EcosystemsClient.resolve_repo_url` — given an
  ``(ecosystem, namespace, name)`` triple, return the source repository
  URL (e.g. ``https://github.com/lodash/lodash``). Used by the harvester
  to decide where to clone for a component.

- :func:`EcosystemsClient.top_packages` — yield the top-N packages of an
  ecosystem sorted by total downloads. Backs the popular-packages list
  that extends provenance coverage beyond the customer footprint.

The client is intentionally synchronous: the harvester is a batch tool,
not a request hot-path, and httpx's sync interface keeps error handling
straightforward.

Rate limit: the public endpoint allows 5000 anonymous requests per hour
(per ``X-RateLimit-Limit``).
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import httpx
import structlog

logger = structlog.get_logger("harvester.ecosystems")

API_BASE = "https://packages.ecosyste.ms/api/v1"
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_PER_PAGE = 100  # API returns 500 above this
MAX_RETRIES = 4

# Maps the ecosystem names that appear in our footprint.csv (which come
# from PURL ``pkg:<type>/...`` strings) to the registry names ecosyste.ms
# exposes at ``/api/v1/registries/<registry>/...``. OS package managers
# (apk/deb/rpm), ``generic``, and ``github`` are intentionally absent —
# they have no registry-level popularity data and need other handling.
ECOSYSTEM_TO_REGISTRY: dict[str, str] = {
    "npm": "npmjs.org",
    "pypi": "pypi.org",
    "maven": "repo1.maven.org",
    "cargo": "crates.io",
    "gem": "rubygems.org",
    "golang": "proxy.golang.org",
    "composer": "packagist.org",
    "nuget": "nuget.org",
    "cocoapods": "cocoapods.org",
    "pub": "pub.dev",
    "hex": "hex.pm",
    "conan": "conan.io",
    "luarocks": "luarocks.org",
    "pear": "pear.php.net",
}


def qualified_package_name(ecosystem: str, namespace: str, name: str) -> str:
    """Combine a parsed PURL ``(namespace, name)`` into the form registries expect.

    Maven uses ``groupId:artifactId``; everything else with a namespace
    uses ``namespace/name`` (npm scoped packages, go modules, composer
    vendor/package, etc.). Packages without a namespace are returned
    as-is.
    """
    if not namespace:
        return name
    separator = ":" if ecosystem == "maven" else "/"
    return f"{namespace}{separator}{name}"


class EcosystemsClient:
    """Synchronous client for the ecosyste.ms packages API."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._client = httpx.Client(
            base_url=API_BASE,
            timeout=timeout,
            headers={"User-Agent": "provenance-harvester (sandro988)"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> EcosystemsClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def resolve_repo_url(self, ecosystem: str, namespace: str, name: str) -> str | None:
        """Return the package's source repository URL, or ``None``.

        Returns ``None`` when the ecosystem is not on ecosyste.ms, the
        package does not exist there, or the registry has no recorded
        repository URL for it (common for very small packages).
        """
        registry = _registry_for(ecosystem)
        if registry is None:
            return None
        qualified = qualified_package_name(ecosystem, namespace, name)
        body = self._get(f"/registries/{registry}/packages/{qualified}")
        if not isinstance(body, dict):
            return None
        return body.get("repository_url") or None

    def top_packages(
        self, ecosystem: str, limit: int
    ) -> Iterator[tuple[str, str | None, int | None, str | None]]:
        """Yield up to ``limit`` packages sorted by downloads descending.

        Each tuple is ``(qualified_name, latest_release_number, downloads,
        repository_url)``. The qualified name comes through whatever shape
        the registry publishes (e.g. ``@types/node`` for npm scoped,
        ``org.hdrhistogram:HdrHistogram`` for maven); callers that need
        namespace/name split should do so themselves. ``repository_url``
        is what the registry has on file — often the GitHub clone URL,
        but ``None`` for packages where ecosyste.ms hasn't surfaced one.
        """
        registry = _registry_for(ecosystem)
        if registry is None:
            return
        yielded = 0
        page = 1
        while yielded < limit:
            body = self._get(
                f"/registries/{registry}/packages",
                params={
                    "sort": "downloads",
                    "order": "desc",
                    "per_page": MAX_PER_PAGE,
                    "page": page,
                },
            )
            if not isinstance(body, list) or not body:
                return
            for package in body:
                if yielded >= limit:
                    return
                yield (
                    package["name"],
                    package.get("latest_release_number"),
                    package.get("downloads"),
                    package.get("repository_url"),
                )
                yielded += 1
            if len(body) < MAX_PER_PAGE:
                return  # last page
            page += 1

    def _get(self, path: str, params: dict | None = None) -> dict | list | None:
        """GET ``path`` with retry-on-transient logic.

        Returns the parsed JSON body on success, ``None`` on 404 or after
        exhausting retries.
        """
        for attempt in range(MAX_RETRIES):
            try:
                response = self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                logger.warning(
                    "ecosystems.network_error",
                    path=path,
                    attempt=attempt,
                    error=str(exc),
                )
                time.sleep(2**attempt)
                continue
            if response.status_code == 404:
                return None
            if response.status_code == 429:
                retry_after = int(response.headers.get("retry-after", 2**attempt))
                logger.info(
                    "ecosystems.rate_limited",
                    path=path,
                    retry_after=retry_after,
                )
                time.sleep(retry_after)
                continue
            if response.status_code >= 500:
                logger.warning(
                    "ecosystems.server_error",
                    path=path,
                    status=response.status_code,
                    attempt=attempt,
                )
                time.sleep(2**attempt)
                continue
            if not response.is_success:
                logger.warning(
                    "ecosystems.unexpected_status",
                    path=path,
                    status=response.status_code,
                )
                return None
            return response.json()
        logger.error("ecosystems.giving_up", path=path)
        return None


def _registry_for(ecosystem: str) -> str | None:
    """Return the ecosyste.ms registry slug for our ecosystem name, or ``None``."""
    registry = ECOSYSTEM_TO_REGISTRY.get(ecosystem)
    if registry is None:
        logger.debug("ecosystems.unsupported_ecosystem", ecosystem=ecosystem)
    return registry
