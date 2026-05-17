"""Manifest discovery — find packages and their paths inside a cloned repo.

Given a cloned repository, a registered detector per ecosystem walks
the working tree for ecosystem-native manifests and yields
``DiscoveredPackage`` records. The matcher (downstream) joins these
against a target package list to produce ``{target: relative_path}``
mappings consumed by the extractor's ``path_filter`` argument.

The public surface is intentionally narrow:

- ``DiscoveredPackage`` — the per-manifest result type
- ``ManifestDetector`` — the protocol every detector satisfies
- ``DETECTORS`` — the registry the orchestrator iterates

Adding a new ecosystem is a single file plus one registry entry.
"""

from harvester.manifest_discovery.base import (
    ManifestDetector,
    should_skip_directory,
)
from harvester.manifest_discovery.cargo import (
    CargoManifestDetector,
)
from harvester.manifest_discovery.discovery_types import (
    DiscoveredPackage,
)
from harvester.manifest_discovery.go import (
    GoManifestDetector,
)
from harvester.manifest_discovery.maven import (
    MavenManifestDetector,
)
from harvester.manifest_discovery.npm import (
    NpmManifestDetector,
)
from harvester.manifest_discovery.nuget import (
    NuGetManifestDetector,
)
from harvester.manifest_discovery.pypi import (
    PypiManifestDetector,
)
from harvester.manifest_discovery.rubygems import (
    RubyGemsManifestDetector,
)

# Registry of all known ecosystem detectors. The matcher runs every
# detector against a repo; each yields zero or more discovered packages.
# Filtering by ``ecosystem`` against the target package list happens
# downstream — discovery itself is ecosystem-agnostic. Order has no
# semantic meaning.
DETECTORS: tuple[ManifestDetector, ...] = (
    NpmManifestDetector(),
    PypiManifestDetector(),
    MavenManifestDetector(),
    CargoManifestDetector(),
    NuGetManifestDetector(),
    GoManifestDetector(),
    RubyGemsManifestDetector(),
)

__all__ = [
    "DETECTORS",
    "CargoManifestDetector",
    "DiscoveredPackage",
    "GoManifestDetector",
    "ManifestDetector",
    "MavenManifestDetector",
    "NpmManifestDetector",
    "NuGetManifestDetector",
    "PypiManifestDetector",
    "RubyGemsManifestDetector",
    "should_skip_directory",
]
