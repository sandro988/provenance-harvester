"""Value objects for the manifest discovery sub-package.

A detector walks a cloned repository looking for ecosystem-native
package manifests (``package.json``, ``pom.xml``, ``Cargo.toml``, ...)
and returns one ``DiscoveredPackage`` per manifest it could parse.
The downstream matcher joins these against a target package list to
produce ``{target_package: relative_path}`` mappings that feed the
extractor's ``path_filter``.

These types are intentionally schema-agnostic. They describe what a
detector found in a repo, not how the harvester or live pipeline
consumes it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DiscoveredPackage:
    """A package's identity and location inside a repository.

    ``name`` is the manifest-declared package identifier — exactly
    what an ecosystem registry would publish under. For npm this is
    the ``name`` field (``react-dom``); for maven it's
    ``groupId:artifactId``; for cargo it's ``[package].name``.

    ``relative_path`` is the directory containing the manifest,
    relative to the repository root, in POSIX form. A single-package
    repository whose manifest lives at the root yields ``"."`` (not
    the empty string) so the matcher can pass the value verbatim as
    a ``git log -- <path>`` filter without special-casing root.

    ``manifest_file`` is the filename the detector parsed
    (``package.json``, ``pom.xml``, ...). It lets a multi-format
    detector (PyPI's ``pyproject.toml`` / ``setup.py`` / ``setup.cfg``)
    record which schema actually produced the name, useful for
    diagnostics when a package surfaces from one source but not
    another.
    """

    name: str
    relative_path: str
    manifest_file: str
