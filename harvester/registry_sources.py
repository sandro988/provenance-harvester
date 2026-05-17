"""Top-N package generators backed by direct registry APIs.

Each registry exposes its own top-by-popularity endpoint, so fanning
out to per-registry clients gives us much more aggregate throughput
than going through one aggregator (e.g. ecosyste.ms) — each registry
has its own rate-limit budget, and fewer hops means lower latency.

Five language ecosystems are covered:

- ``npm`` via npms.io ``/v2/search?q=not:deprecated`` (1.95M packages,
  score-sorted as popularity proxy)
- ``pypi`` via the curated ``hugovk/top-pypi-packages`` JSON enriched
  with versions and project URLs from PyPI's own JSON API
- ``cargo`` via crates.io ``/api/v1/crates?sort=downloads``
- ``gem`` via rubygems.org ``/api/v1/search.json?order=most_downloads``
- ``nuget`` via the V3 search service with ``sortBy=totalDownloads``

The remaining footprint ecosystems (maven, golang, composer, cocoapods,
pub, hex, conan, luarocks, pear) don't have clean native popularity
endpoints; ecosyste.ms remains the right source for them when it's
available.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from urllib.parse import quote

import httpx
import structlog

logger = structlog.get_logger("harvester.registry_sources")

DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 3
USER_AGENT = "provenance-harvester (sandro988)"

# What each generator yields: (namespace, name, version, repo_url).
# Namespace and repo_url may be empty strings when the registry doesn't
# expose them on the listing endpoint; callers tolerate this.
Package = tuple[str, str, str, str]


def _get_with_retry(
    client: httpx.Client, url: str, **kwargs
) -> httpx.Response | None:
    for attempt in range(MAX_RETRIES):
        try:
            response = client.get(url, **kwargs)
        except httpx.HTTPError as exc:
            logger.warning(
                "registry_sources.network_error",
                url=url,
                attempt=attempt,
                error=str(exc),
            )
            time.sleep(2**attempt)
            continue
        if response.status_code == 429:
            retry_after = int(response.headers.get("retry-after", 2**attempt))
            logger.info(
                "registry_sources.rate_limited",
                url=url,
                retry_after=retry_after,
            )
            time.sleep(retry_after)
            continue
        if response.status_code >= 500:
            logger.warning(
                "registry_sources.server_error",
                url=url,
                status=response.status_code,
                attempt=attempt,
            )
            time.sleep(2**attempt)
            continue
        return response
    logger.error("registry_sources.giving_up", url=url)
    return None


def top_crates(client: httpx.Client, limit: int) -> Iterator[Package]:
    """crates.io top crates sorted by total downloads."""
    page = 1
    yielded = 0
    while yielded < limit:
        response = _get_with_retry(
            client,
            "https://crates.io/api/v1/crates",
            params={"sort": "downloads", "per_page": 100, "page": page},
            headers={"User-Agent": USER_AGENT},
        )
        if response is None or not response.is_success:
            return
        crates = response.json().get("crates", [])
        if not crates:
            return
        for crate in crates:
            if yielded >= limit:
                return
            yield (
                "",
                crate["name"],
                crate.get("max_version") or "",
                crate.get("repository") or "",
            )
            yielded += 1
        page += 1


def top_gem(client: httpx.Client, limit: int) -> Iterator[Package]:
    """rubygems.org top gems sorted by lifetime downloads.

    rubygems' search returns ~30 per page (server-fixed; no per_page
    parameter); a ~15k cap means ~500 page calls.
    """
    page = 1
    yielded = 0
    while yielded < limit:
        response = _get_with_retry(
            client,
            "https://rubygems.org/api/v1/search.json",
            params={"query": "*", "order": "most_downloads", "page": page},
            headers={"User-Agent": USER_AGENT},
        )
        if response is None or not response.is_success:
            return
        gems = response.json()
        if not isinstance(gems, list) or not gems:
            return
        for gem in gems:
            if yielded >= limit:
                return
            yield (
                "",
                gem["name"],
                gem.get("version") or "",
                gem.get("source_code_uri") or gem.get("homepage_uri") or "",
            )
            yielded += 1
        page += 1


def top_nuget(client: httpx.Client, limit: int) -> Iterator[Package]:
    """NuGet packages sorted by total downloads via the V3 search service."""
    skip = 0
    while skip < limit:
        take = min(1000, limit - skip)
        response = _get_with_retry(
            client,
            "https://azuresearch-usnc.nuget.org/query",
            params={"q": "", "take": take, "skip": skip, "sortBy": "totalDownloads"},
            headers={"User-Agent": USER_AGENT},
        )
        if response is None or not response.is_success:
            return
        data = response.json().get("data", [])
        if not data:
            return
        for pkg in data:
            yield (
                "",
                pkg["id"],
                pkg.get("version") or "",
                # NuGet exposes ``projectUrl`` (project homepage), which is
                # usually the GitHub repo for OSS packages. Falls through
                # to clone-URL normalization downstream.
                pkg.get("projectUrl") or "",
            )
        skip += len(data)


def top_npm(client: httpx.Client, limit: int) -> Iterator[Package]:
    """npm packages by npms.io popularity score.

    npms.io paginates via ``size`` (max 250) and ``from`` (offset). It
    caps total pagination at ``from + size <= 5000`` for the default
    flow but accepts much higher offsets when ``q`` is restrictive
    enough; ``q=not:deprecated`` gets us across the whole catalog.
    """
    offset = 0
    while offset < limit:
        size = min(250, limit - offset)
        response = _get_with_retry(
            client,
            "https://api.npms.io/v2/search",
            params={"q": "not:deprecated", "size": size, "from": offset},
            headers={"User-Agent": USER_AGENT},
        )
        if response is None or not response.is_success:
            return
        results = response.json().get("results", [])
        if not results:
            return
        for entry in results:
            pkg = entry.get("package", {})
            full_name = pkg.get("name") or ""
            # npm scoped packages publish their name as ``@scope/pkg``; we
            # split that into the PURL-compatible (``@scope``, ``pkg``).
            if full_name.startswith("@") and "/" in full_name:
                namespace, _, name = full_name.partition("/")
            else:
                namespace, name = "", full_name
            yield (
                namespace,
                name,
                pkg.get("version") or "",
                pkg.get("links", {}).get("repository") or "",
            )
        offset += len(results)


def top_pypi(client: httpx.Client, limit: int) -> Iterator[Package]:
    """PyPI top packages from a curated ranking + per-package metadata fetch.

    hugovk/top-pypi-packages publishes a JSON ranking of PyPI projects
    by 30-day downloads. The list has names only, so for each entry we
    also fetch ``pypi.org/pypi/{name}/json`` to pick up the latest
    version and a repo URL from ``project_urls``. PyPI's own JSON API
    is unrestricted and fast enough to do this serially.
    """
    response = _get_with_retry(
        client,
        "https://raw.githubusercontent.com/hugovk/top-pypi-packages/main/top-pypi-packages.min.json",
        headers={"User-Agent": USER_AGENT},
    )
    if response is None or not response.is_success:
        return
    rows = response.json().get("rows", [])

    for row in rows[:limit]:
        name = row.get("project")
        if not name:
            continue
        meta_response = _get_with_retry(
            client,
            f"https://pypi.org/pypi/{quote(name, safe='')}/json",
            headers={"User-Agent": USER_AGENT},
        )
        if meta_response is None or not meta_response.is_success:
            yield ("", name, "", "")
            continue
        info = meta_response.json().get("info", {}) or {}
        project_urls = info.get("project_urls") or {}
        # PyPI publishers use inconsistent key casing; check the
        # well-known synonyms in priority order.
        repo_url = (
            project_urls.get("Source")
            or project_urls.get("Source Code")
            or project_urls.get("source")
            or project_urls.get("Repository")
            or project_urls.get("repository")
            or project_urls.get("Code")
            or info.get("home_page")
            or ""
        )
        yield ("", name, info.get("version") or "", repo_url)


def top_packages(
    client: httpx.Client, ecosystem: str, limit: int
) -> Iterator[Package]:
    """Dispatch to the right per-registry generator for an ecosystem.

    Returns nothing for ecosystems without a direct-API generator —
    callers can fall back to ecosyste.ms (when healthy) for those.
    """
    dispatch = {
        "npm": top_npm,
        "pypi": top_pypi,
        "cargo": top_crates,
        "gem": top_gem,
        "nuget": top_nuget,
    }
    generator = dispatch.get(ecosystem)
    if generator is None:
        logger.debug("registry_sources.no_native_source", ecosystem=ecosystem)
        return
    yield from generator(client, limit)
