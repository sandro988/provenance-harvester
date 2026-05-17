"""NuGet package detector — parses ``*.csproj`` and ``*.nuspec`` files.

NuGet packages can be declared in two places:

- ``*.nuspec`` — explicit package manifest, ``<id>`` is the package
  identifier. Used by traditional library projects and by anything
  built with ``nuget pack`` directly.
- ``*.csproj`` (SDK-style) — ``<PackageId>`` if set, otherwise the
  project assembly name (``<AssemblyName>``), otherwise the project
  filename without the ``.csproj`` extension. This is the modern
  default for .NET projects that opt into ``IsPackable``.

We prefer ``.nuspec`` when both are present in the same directory,
matching ``dotnet pack``'s precedence rules. A package without an
explicit ``<PackageId>`` falls back to the file-stem heuristic so
SDK-style projects still surface — their published id matches the
filename in the overwhelming majority of cases on NuGet.org.

``*.vbproj`` / ``*.fsproj`` follow the same schema as csproj and
get the same treatment. The walker matches any of the three.
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


_PROJECT_EXTENSIONS: frozenset[str] = frozenset({".csproj", ".vbproj", ".fsproj"})


def _local_name(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


class NuGetManifestDetector:
    """Discovers NuGet packages from .nuspec and SDK-style project files."""

    ecosystem = "NUGET"

    def discover(self, repo_dir: Path) -> Iterator[DiscoveredPackage]:
        """Yield one record per directory with a parseable NuGet manifest.

        When both ``.nuspec`` and a project file coexist in a
        directory, the ``.nuspec`` wins — explicit beats inferred.
        Multiple project files in one directory each yield their
        own record; rare in practice but legal.
        """
        for package_dir, manifest_path in self._iter_manifest_dirs(repo_dir):
            name = self._read_name(manifest_path)
            if name is None:
                continue
            relative_path = package_dir.relative_to(repo_dir).as_posix()
            yield DiscoveredPackage(
                name=name,
                relative_path=relative_path,
                manifest_file=manifest_path.name,
            )

    @staticmethod
    def _iter_manifest_dirs(repo_dir: Path) -> Iterator[tuple[Path, Path]]:
        """Walk and emit ``(dir, manifest)`` for every NuGet manifest.

        For each directory: emits a ``.nuspec`` if present (one per
        directory — repos rarely ship more than one), otherwise
        emits every project file in the directory. Pruning the
        common .NET build outputs (``bin``, ``obj``) prevents
        descending into compiled artifacts that occasionally
        contain stale project files.
        """
        stack: list[Path] = [repo_dir]
        while stack:
            current = stack.pop()
            try:
                entries = list(current.iterdir())
            except (OSError, PermissionError):
                continue
            nuspecs = [e for e in entries if e.is_file() and e.suffix == ".nuspec"]
            if nuspecs:
                for nuspec in nuspecs:
                    yield current, nuspec
            else:
                for entry in entries:
                    if entry.is_file() and entry.suffix in _PROJECT_EXTENSIONS:
                        yield current, entry
            for entry in entries:
                if (
                    entry.is_dir()
                    and not should_skip_directory(entry.name)
                    and entry.name not in {"bin", "obj"}
                ):
                    stack.append(entry)

    @classmethod
    def _read_name(cls, manifest_path: Path) -> str | None:
        """Dispatch by extension, returning ``None`` on parse failure."""
        try:
            tree = ElementTree.parse(manifest_path)  # noqa: S314 — local files
        except (OSError, ElementTree.ParseError) as error:
            logger.debug(
                "nuget manifest unreadable, skipping",
                manifest=str(manifest_path),
                error=str(error),
            )
            return None
        root = tree.getroot()
        if manifest_path.suffix == ".nuspec":
            return cls._read_nuspec_id(root)
        return cls._read_project_id(root, manifest_path)

    @staticmethod
    def _read_nuspec_id(root: ElementTree.Element) -> str | None:
        """Extract ``<metadata><id>`` from a nuspec root."""
        if _local_name(root.tag) != "package":
            return None
        for child in root:
            if _local_name(child.tag) != "metadata":
                continue
            for meta_child in child:
                if _local_name(meta_child.tag) == "id" and meta_child.text:
                    name = meta_child.text.strip()
                    if name:
                        return name
        return None

    @staticmethod
    def _read_project_id(
        root: ElementTree.Element,
        manifest_path: Path,
    ) -> str | None:
        """Walk ``<PropertyGroup>`` for ``<PackageId>`` / ``<AssemblyName>``.

        Falls back to the project filename without its extension —
        SDK-style projects without explicit ``<PackageId>`` publish
        under exactly that identifier by default.
        """
        package_id: str | None = None
        assembly_name: str | None = None
        for property_group in root.iter():
            if _local_name(property_group.tag) != "PropertyGroup":
                continue
            for prop in property_group:
                local = _local_name(prop.tag)
                if local == "PackageId" and prop.text:
                    package_id = prop.text.strip() or None
                elif local == "AssemblyName" and prop.text:
                    assembly_name = prop.text.strip() or None
        return package_id or assembly_name or manifest_path.stem
