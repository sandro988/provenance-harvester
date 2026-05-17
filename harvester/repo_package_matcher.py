"""Match popular-CSV targets to their directories inside a cloned repo.

Given a single repository and the subset of popular.csv rows that
resolved to its URL, this module runs every manifest detector and
joins the discovered packages back to the targets. Each match becomes
a ``path_filter`` value the harvester passes into the extractor so the
contributor walk attributes commits only to the touched directory.

Per-ecosystem name normalisation is unavoidable because the CSV and
the detector each speak the convention native to their side:

| Ecosystem | CSV (ecosystem, namespace, name)   | Detector ``name``    |
|-----------|-------------------------------------|----------------------|
| npm       | ``(npm, @scope|"", n)``             | ``@scope/n`` or n    |
| pypi      | ``(pypi, "", n)`` PEP 503 raw       | manifest-declared    |
| maven     | ``(maven, groupId, artifactId)``    | ``groupId:artifactId``|
| cargo     | ``(cargo, "", crate)``              | crate                |
| nuget     | ``(nuget, "", id)`` case-insensitive| manifest-declared id |
| golang    | ``(golang, host/owner, repo[/v2])`` | module directive     |
| gem       | ``(gem, "", n)``                    | gem name             |

Both sides are reduced to a single ``(detector_ecosystem, key)`` tuple
and matched on that key. Unmatched targets are returned alongside the
matches so the orchestrator can log them without an extra detection
pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from harvester.manifest_discovery import DETECTORS, DiscoveredPackage

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from harvester.run import ComponentVersion

logger = structlog.get_logger("harvester.repo_package_matcher")


# Maps the lower-case ecosystem string used in popular.csv to the
# upper-case identifier each detector reports via its ``ecosystem``
# attribute. Only entries listed here participate in matching; CSV
# rows whose ecosystem is absent here (apk, deb, composer, ...) are
# silently skipped because no detector knows how to find them.
CSV_TO_DETECTOR_ECOSYSTEM: dict[str, str] = {
    "npm": "NPM",
    "pypi": "PYPI",
    "maven": "MAVEN",
    "cargo": "CARGO",
    "nuget": "NUGET",
    "golang": "GO",
    "gem": "RUBYGEMS",
}


_PEP_503_SEPARATORS = re.compile(r"[-_.]+")


def _pep503_normalize(name: str) -> str:
    """Collapse ``-``/``_``/``.`` runs and lower-case (PEP 503)."""
    return _PEP_503_SEPARATORS.sub("-", name).lower()


def _csv_key(ecosystem: str, namespace: str, name: str) -> str | None:
    """Return the lookup key a CSV row contributes, or ``None`` to skip."""
    if ecosystem == "npm":
        # Scoped packages arrive as namespace=@scope, name=foo. The npm
        # detector emits ``@scope/foo`` verbatim, so we glue here.
        return f"{namespace}/{name}" if namespace else name
    if ecosystem == "pypi":
        return _pep503_normalize(name)
    if ecosystem == "maven":
        # popular.csv splits maven coordinates; the detector concatenates
        # them. Both sides converge on ``groupId:artifactId``.
        return f"{namespace}:{name}" if namespace else name
    if ecosystem == "cargo":
        return name
    if ecosystem == "nuget":
        # NuGet IDs are case-insensitive per the spec; lower both sides.
        return name.lower()
    if ecosystem == "golang":
        # Module paths may live entirely in name (``github.com/x/y``) or
        # be split across namespace+name. Glue if both present, fall
        # back to name otherwise.
        return f"{namespace}/{name}" if namespace else name
    if ecosystem == "gem":
        return name
    return None


def _discovered_key(ecosystem: str, discovered: DiscoveredPackage) -> str:
    """Return the lookup key a discovered manifest contributes."""
    if ecosystem == "PYPI":
        return _pep503_normalize(discovered.name)
    if ecosystem == "NUGET":
        return discovered.name.lower()
    return discovered.name


@dataclass(frozen=True, slots=True)
class PackageMatch:
    """One target paired with the in-repo path that hosts its manifest."""

    component: ComponentVersion
    relative_path: str


@dataclass(frozen=True, slots=True)
class MatchResult:
    """All matches for a repo plus the targets no detector could place.

    The orchestrator iterates ``matched`` for per-package extraction
    and logs ``unmatched`` (so operators can investigate renames,
    sub-published artefacts, repo moves, or version drift) without
    re-running detection.
    """

    matched: tuple[PackageMatch, ...]
    unmatched: tuple[ComponentVersion, ...]


def match_packages_in_repo(
    repo_dir: Path,
    targets: Sequence[ComponentVersion],
) -> MatchResult:
    """Match every target to a directory inside ``repo_dir``, when possible.

    The function runs every detector exactly once and builds a
    ``{(detector_ecosystem, normalised_name): relative_path}`` index.
    Each target is then looked up in that index. When two manifests
    in the same ecosystem report the same normalised name (rare; the
    detectors largely prevent it within a directory), the index keeps
    the lexicographically-smallest path so monorepos with a sub-published
    re-export pick the canonical root.

    Returns a ``MatchResult`` — never raises on unmatched targets.
    """
    index: dict[tuple[str, str], str] = {}
    for detector in DETECTORS:
        for discovered in detector.discover(repo_dir):
            key = (detector.ecosystem, _discovered_key(detector.ecosystem, discovered))
            existing = index.get(key)
            if existing is None or discovered.relative_path < existing:
                index[key] = discovered.relative_path

    matched: list[PackageMatch] = []
    unmatched: list[ComponentVersion] = []
    for target in targets:
        detector_ecosystem = CSV_TO_DETECTOR_ECOSYSTEM.get(target.ecosystem)
        csv_key = _csv_key(target.ecosystem, target.namespace, target.name)
        if detector_ecosystem is None or csv_key is None:
            # Ecosystem outside the detector roster — record as unmatched
            # so the orchestrator can fall back without crashing.
            unmatched.append(target)
            continue
        path = index.get((detector_ecosystem, csv_key))
        if path is None:
            unmatched.append(target)
            continue
        matched.append(PackageMatch(component=target, relative_path=path))

    if unmatched:
        logger.info(
            "matcher.unmatched_targets",
            repo_url=targets[0].repo_url if targets else "",
            unmatched_count=len(unmatched),
            matched_count=len(matched),
        )
    return MatchResult(matched=tuple(matched), unmatched=tuple(unmatched))
