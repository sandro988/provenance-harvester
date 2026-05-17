"""Protocol and shared helpers for ecosystem manifest detectors.

Each ecosystem ships a detector module (``npm.py``, ``maven.py``, ...)
exposing a ``discover`` function that takes a repository root and
returns the packages it found. The ``ManifestDetector`` protocol
formalises the shape so the orchestrator can iterate detectors
uniformly.

Detectors must be pure: walk the filesystem, parse what they find,
return ``DiscoveredPackage`` records. No git invocations, no network,
no shared mutable state — keeps them trivially testable against
real-world manifest fixtures.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from harvester.manifest_discovery.discovery_types import (
        DiscoveredPackage,
    )


# Directories the walker should never descend into during manifest
# discovery. ``node_modules`` is the dominant case (every node project
# vendors hundreds of unrelated package.jsons there); the others are
# common build-tool caches that occasionally vendor manifests but
# never represent the repository's own packages.
SKIP_DIRECTORIES: frozenset[str] = frozenset(
    {
        "node_modules",
        ".git",
        "vendor",
        "target",
        "build",
        "dist",
        ".gradle",
        ".tox",
        ".venv",
        "venv",
        "__pycache__",
    }
)


class ManifestDetector(Protocol):
    """One detector per ecosystem.

    Implementations live in sibling modules (``npm.py``, etc.) and
    are registered in ``__init__.py``. Adding a new ecosystem is a
    single file plus one registry line — no dispatch changes
    elsewhere.
    """

    ecosystem: str
    """Stable identifier matching the System values used elsewhere
    (``NPM``, ``MAVEN``, ``PYPI``, ``CARGO``, ``NUGET``, ``GO``,
    ``RUBYGEMS``). Used by the matcher to filter discovery results
    to the ecosystem of each target package."""

    def discover(self, repo_dir: Path) -> Iterable[DiscoveredPackage]:
        """Walk ``repo_dir`` and yield one record per parseable manifest.

        Implementations may yield zero records (repo has no
        manifests of this ecosystem) without raising. Parse failures
        on individual manifests should be logged-and-skipped, not
        propagated — a malformed package.json buried in fixtures
        shouldn't fail the whole repo's discovery.
        """
        ...


def should_skip_directory(name: str) -> bool:
    """True when a directory should be pruned from manifest walks.

    Centralised so every detector's ``walk`` agrees on what to skip
    without duplicating the constant. Hidden directories (those
    starting with ``.``) are also pruned to avoid spurious matches
    inside ``.github/``, ``.idea/``, etc. — none of which carry
    real package definitions.
    """
    return name in SKIP_DIRECTORIES or name.startswith(".")
