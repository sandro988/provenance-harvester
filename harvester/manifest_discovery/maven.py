"""Maven package detector — parses ``pom.xml`` files.

Maven coordinates are ``groupId:artifactId`` — both required to
identify a published artifact. We surface that exact string as the
package name, matching the format BigQuery's deps.dev dataset and
Maven Central use (``org.springframework:spring-core``).

A child POM may omit ``<groupId>`` and inherit it from
``<parent><groupId>``. The detector handles this fallback explicitly.
``<artifactId>`` is always required by the Maven spec, so an
artifact-less POM is malformed and gets skipped.

Multi-module projects ship one ``pom.xml`` per module. Walking
captures all of them; each module has its own (groupId, artifactId)
and produces a separate record. Parent POMs that exist only to
share build configuration (``<packaging>pom</packaging>`` with no
own source) still produce a record — they're real coordinates on
Maven Central, just not very useful targets. The matcher filters
those out by joining against the target list.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from typing import TYPE_CHECKING

from harvester.manifest_discovery._logger import logger
from harvester.manifest_discovery.base import (
    should_skip_directory,
)
from harvester.manifest_discovery.discovery_types import (
    DiscoveredPackage,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# Maven 4 POMs declare a default namespace; ElementTree's tag
# matching treats namespaced and non-namespaced elements as
# different. The helper below strips namespaces uniformly so the
# lookup code can use plain element names.
def _local_name(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


class MavenManifestDetector:
    """Discovers Maven artifacts by walking for ``pom.xml`` files."""

    ecosystem = "MAVEN"

    def discover(self, repo_dir: Path) -> Iterator[DiscoveredPackage]:
        """Yield one record per parseable ``pom.xml``."""
        for manifest_path in self._iter_manifests(repo_dir):
            name = self._read_coordinates(manifest_path)
            if name is None:
                continue
            relative_path = manifest_path.parent.relative_to(repo_dir).as_posix()
            yield DiscoveredPackage(
                name=name,
                relative_path=relative_path,
                manifest_file="pom.xml",
            )

    @staticmethod
    def _iter_manifests(repo_dir: Path) -> Iterator[Path]:
        stack: list[Path] = [repo_dir]
        while stack:
            current = stack.pop()
            try:
                entries = list(current.iterdir())
            except (OSError, PermissionError):
                continue
            for entry in entries:
                if entry.is_dir():
                    if not should_skip_directory(entry.name):
                        stack.append(entry)
                elif entry.name == "pom.xml":
                    yield entry

    @staticmethod
    def _read_coordinates(manifest_path: Path) -> str | None:
        """Return ``groupId:artifactId``, applying parent-POM inheritance.

        The Maven spec lets a child POM omit ``<groupId>`` if a
        ``<parent><groupId>`` is declared. We try the direct field
        first, fall back to the parent, and bail if neither is
        present. ``<artifactId>`` has no inheritance fallback.
        """
        try:
            tree = ElementTree.parse(manifest_path)  # noqa: S314 — local files
        except (OSError, ElementTree.ParseError) as error:
            logger.debug(
                "maven manifest unreadable, skipping",
                manifest=str(manifest_path),
                error=str(error),
            )
            return None
        root = tree.getroot()
        if _local_name(root.tag) != "project":
            return None

        group_id = _find_text(root, "groupId")
        artifact_id = _find_text(root, "artifactId")
        if not group_id:
            parent = _find_child(root, "parent")
            if parent is not None:
                group_id = _find_text(parent, "groupId")
        if not group_id or not artifact_id:
            return None
        return f"{group_id}:{artifact_id}"


def _find_child(element: ElementTree.Element, name: str) -> ElementTree.Element | None:
    """Direct-child lookup by local name, namespace-agnostic."""
    for child in element:
        if _local_name(child.tag) == name:
            return child
    return None


def _find_text(element: ElementTree.Element, name: str) -> str | None:
    """Direct-child text content by local name, stripped."""
    child = _find_child(element, name)
    if child is None or child.text is None:
        return None
    text = child.text.strip()
    return text or None
