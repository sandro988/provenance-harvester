"""HTTP client for Google's deps.dev (Open Source Insights) API.

Used as a fallback to :mod:`harvester.ecosystems_client` when ecosyste.ms
doesn't have a repository URL for a package. deps.dev covers a much
narrower set of ecosystems (npm, pypi, maven, cargo, nuget, go) but
often fills the gaps in those — especially Maven, where ecosyste.ms
frequently lacks source-repo metadata that deps.dev surfaces via the
package's POM links.

Lookup is two-call: GET the package to find a usable version, then GET
the version to extract the ``SOURCE_REPO`` link. Repository URLs in the
wild come in shapes like ``scm:git:git://github.com/...`` (Maven SCM
prefix), ``git+https://...`` (npm), or plain HTTPS; this client
normalises them before returning.
"""

from __future__ import annotations

import re
import time

import httpx
import structlog

logger = structlog.get_logger("harvester.deps_dev")

API_BASE = "https://api.deps.dev/v3"
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 4

# Maps our footprint ecosystem names to deps.dev system identifiers
# (which are uppercase). Ecosystems deps.dev doesn't cover are absent.
ECOSYSTEM_TO_SYSTEM: dict[str, str] = {
    "npm": "NPM",
    "pypi": "PYPI",
    "maven": "MAVEN",
    "cargo": "CARGO",
    "nuget": "NUGET",
    "golang": "GO",
}

_SCM_PREFIX = re.compile(r"^scm:git:")  # Maven POM SCM-URL convention
_GIT_PROTO_PREFIX = re.compile(r"^git\+")  # npm "git+https://..." prefix


class DepsDevClient:
    """Synchronous client for the deps.dev Insights API."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._client = httpx.Client(
            base_url=API_BASE,
            timeout=timeout,
            headers={"User-Agent": "provenance-harvester (sandro988)"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DepsDevClient":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def resolve_repo_url(self, ecosystem: str, namespace: str, name: str) -> str | None:
        """Return the package's normalised source repository URL, or ``None``.

        Returns ``None`` when the ecosystem is not on deps.dev, the
        package or version isn't found, or no ``SOURCE_REPO`` link is
        published for the latest version.
        """
        system = ECOSYSTEM_TO_SYSTEM.get(ecosystem)
        if system is None:
            return None

        qualified = _qualified_name_for_depsdev(ecosystem, namespace, name)
        package = self._get(f"/systems/{system}/packages/{qualified}")
        if not isinstance(package, dict):
            return None

        latest_version = _pick_latest_version(package.get("versions", []))
        if latest_version is None:
            return None

        version_doc = self._get(
            f"/systems/{system}/packages/{qualified}/versions/{latest_version}"
        )
        if not isinstance(version_doc, dict):
            return None

        return _extract_source_repo_url(version_doc.get("links", []))

    def _get(self, path: str) -> dict | None:
        for attempt in range(MAX_RETRIES):
            try:
                response = self._client.get(path)
            except httpx.HTTPError as exc:
                logger.warning(
                    "deps_dev.network_error",
                    path=path,
                    attempt=attempt,
                    error=str(exc),
                )
                time.sleep(2**attempt)
                continue
            if response.status_code == 404:
                return None
            if response.status_code >= 500:
                logger.warning(
                    "deps_dev.server_error",
                    path=path,
                    status=response.status_code,
                    attempt=attempt,
                )
                time.sleep(2**attempt)
                continue
            if not response.is_success:
                logger.warning(
                    "deps_dev.unexpected_status",
                    path=path,
                    status=response.status_code,
                )
                return None
            return response.json()
        logger.error("deps_dev.giving_up", path=path)
        return None


def _qualified_name_for_depsdev(ecosystem: str, namespace: str, name: str) -> str:
    """Combine namespace + name into the form deps.dev expects in the path.

    Mostly the same shape as ecosyste.ms (maven uses ``:``, others use
    ``/``) but deps.dev wants URL-encoded slashes for go modules and
    npm scoped packages. httpx handles the encoding for us when we pass
    it as a path segment; we just build the natural form here.
    """
    if not namespace:
        return name
    separator = ":" if ecosystem == "maven" else "/"
    return f"{namespace}{separator}{name}"


def _pick_latest_version(versions: list[dict]) -> str | None:
    """Pick the latest non-prerelease version from deps.dev's versions array.

    Falls back to the last version of any kind if none are marked
    default/stable — better than returning ``None`` and missing the
    repo URL entirely.
    """
    default_versions = [
        v for v in versions
        if v.get("isDefault") and not v.get("isPrerelease")
    ]
    if default_versions:
        return default_versions[-1]["versionKey"]["version"]
    stable = [v for v in versions if not v.get("isPrerelease")]
    if stable:
        return stable[-1]["versionKey"]["version"]
    if versions:
        return versions[-1]["versionKey"]["version"]
    return None


def _extract_source_repo_url(links: list[dict]) -> str | None:
    """Pull the SOURCE_REPO link out and normalise its URL shape."""
    for link in links:
        if link.get("label") == "SOURCE_REPO":
            return _normalise_repo_url(link.get("url"))
    return None


def _normalise_repo_url(url: str | None) -> str | None:
    """Normalise the URL shapes seen in the wild into plain HTTPS."""
    if not url:
        return None
    url = _SCM_PREFIX.sub("", url)
    url = _GIT_PROTO_PREFIX.sub("", url)
    if url.startswith("git://"):
        url = "https://" + url.removeprefix("git://")
    if url.endswith(".git"):
        url = url.removesuffix(".git")
    return url
