"""Cargo package detector — parses ``Cargo.toml`` manifests.

Walks the cloned repository for every ``Cargo.toml`` outside pruned
directories and reads the ``[package].name`` field.

Cargo workspaces use a root ``Cargo.toml`` that declares
``[workspace]`` and lists member crates; each member ships its own
``Cargo.toml`` with a ``[package].name``. We do not parse the
``[workspace].members`` glob — every member has its own manifest, so
walking finds them all. A workspace-only root manifest (no
``[package]`` section) is silently skipped, same as a package.json
without a ``name`` field.
"""

from __future__ import annotations

import tomllib
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


class CargoManifestDetector:
    """Discovers Cargo packages by walking for ``Cargo.toml`` files."""

    ecosystem = "CARGO"

    def discover(self, repo_dir: Path) -> Iterator[DiscoveredPackage]:
        """Yield one record per ``Cargo.toml`` with a ``[package].name``.

        Workspace-only manifests (no ``[package]`` table) are silently
        skipped — they declare structure, not a publishable crate.
        Parse failures are logged at debug and the walk continues.
        """
        for manifest_path in self._iter_manifests(repo_dir):
            name = self._read_name(manifest_path)
            if name is None:
                continue
            relative_path = manifest_path.parent.relative_to(repo_dir).as_posix()
            yield DiscoveredPackage(
                name=name,
                relative_path=relative_path,
                manifest_file="Cargo.toml",
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
                elif entry.name == "Cargo.toml":
                    yield entry

    @staticmethod
    def _read_name(manifest_path: Path) -> str | None:
        """Parse ``[package].name`` from ``manifest_path``, or ``None``.

        Returns ``None`` for unreadable files, malformed TOML, missing
        ``[package]`` table (workspace-only root manifests), missing
        ``name`` key, or non-string name.
        """
        try:
            with manifest_path.open("rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as error:
            logger.debug(
                "cargo manifest unreadable, skipping",
                manifest=str(manifest_path),
                error=str(error),
            )
            return None
        package_section = data.get("package")
        if not isinstance(package_section, dict):
            return None
        name = package_section.get("name")
        if not isinstance(name, str) or not name:
            return None
        return name
