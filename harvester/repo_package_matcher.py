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

**Drift warning vs. the main project.** The exodos-backend
``catalog_identity.normalize_name_for_key`` helper lower-cases NPM
package names; this module preserves case (npm registry rejects
uppercase publishes in practice, so the join is normally lossless,
but a manifest that retained mixed-case authors-side could land in
the warehouse under a different key than catalog_identity would
generate). When the downstream loader (Phase 3a) wires the two
emission paths together, reconcile by either lowercasing NPM keys
here or relaxing the catalog_identity comparison; either side can
move, but they must agree on a single canonical form before joins
land in production.
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

    Runs only the detectors whose ecosystem appears in ``targets`` —
    a repo with only npm targets never walks for ``pom.xml`` /
    ``Cargo.toml`` / ``go.mod``. Builds a
    ``{(detector_ecosystem, normalised_name): relative_path}`` index
    from the discovered manifests, then looks each target up. When
    two manifests in the same ecosystem report the same normalised
    name (rare; the detectors largely prevent it within a directory),
    the index keeps the lexicographically-smallest path so monorepos
    with a sub-published re-export pick the canonical root.

    Pre-computes targets' lookup keys to enable an early-exit when
    every target has been matched — sparse-target sprawling monorepos
    stop walking irrelevant subtrees.

    Returns a ``MatchResult`` — never raises on unmatched targets.
    """
    # Resolve each target to its lookup key up-front. Targets whose
    # ecosystem has no detector (apk/deb/composer/...) go straight to
    # unmatched so we know not to spend any discovery cost on them.
    lookups: dict[ComponentVersion, tuple[str, str]] = {}
    unmatched: list[ComponentVersion] = []
    for target in targets:
        detector_ecosystem = CSV_TO_DETECTOR_ECOSYSTEM.get(target.ecosystem)
        csv_key = _csv_key(target.ecosystem, target.namespace, target.name)
        if detector_ecosystem is None or csv_key is None:
            unmatched.append(target)
            continue
        lookups[target] = (detector_ecosystem, csv_key)

    needed_ecosystems = {key[0] for key in lookups.values()}
    pending_lookups = set(lookups.values())
    index: dict[tuple[str, str], str] = {}
    for detector in DETECTORS:
        if detector.ecosystem not in needed_ecosystems:
            continue
        for discovered in detector.discover(repo_dir):
            key = (detector.ecosystem, _discovered_key(detector.ecosystem, discovered))
            existing = index.get(key)
            if existing is None or discovered.relative_path < existing:
                index[key] = discovered.relative_path
            pending_lookups.discard(key)
        if not pending_lookups:
            # Every target's key is now in the index — no further
            # detection can change the result. Stop walking.
            break

    matched: list[PackageMatch] = []
    for target, key in lookups.items():
        path = index.get(key)
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
