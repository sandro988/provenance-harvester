"""npm package detector — parses ``package.json`` manifests.

Walks the cloned repository for every ``package.json`` outside the
pruned directories (``node_modules`` is the dominant one), reads the
``name`` field, and yields one ``DiscoveredPackage`` per manifest.

Handles the three monorepo conventions uniformly:

- Lerna (``packages/<name>/package.json``)
- Yarn workspaces (``packages/<name>/`` or ``apps/<name>/``)
- pnpm workspaces (``packages/<name>/``)

The walker does not parse ``pnpm-workspace.yaml`` or the root
``workspaces`` field — it doesn't need to. Every workspace member
ships its own ``package.json`` with the declared name; finding those
captures all members regardless of how the workspace is configured.
A package.json without a ``name`` field (root configs, examples,
private fixtures) is silently skipped.
"""

from __future__ import annotations

import json
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


class NpmManifestDetector:
    """Discovers npm packages by walking for ``package.json`` files."""

    ecosystem = "NPM"

    def discover(self, repo_dir: Path) -> Iterator[DiscoveredPackage]:
        """Yield one record per ``package.json`` with a ``name`` field.

        Pruned directories never get descended into — a 500-package
        node_modules tree would otherwise dominate the walk and
        produce thousands of irrelevant matches. Hidden directories
        (``.github``, ``.idea``, ...) are skipped the same way.

        Parse failures on individual manifests are logged at debug
        and skipped. The walk continues — a single malformed
        package.json in a fixture directory shouldn't fail the whole
        repo's discovery.
        """
        for manifest_path in self._iter_manifests(repo_dir):
            name = self._read_name(manifest_path)
            if name is None:
                continue
            relative_path = manifest_path.parent.relative_to(repo_dir).as_posix()
            yield DiscoveredPackage(
                name=name,
                relative_path=relative_path,
                manifest_file="package.json",
            )

    @staticmethod
    def _iter_manifests(repo_dir: Path) -> Iterator[Path]:
        """Walk ``repo_dir`` for ``package.json`` files, pruning early.

        Uses an explicit stack rather than ``Path.rglob`` so pruning
        happens before we descend — ``rglob`` walks everything and
        filters after the fact, which on a real monorepo (react has
        ~10k node_modules entries even in a fresh clone) is the
        difference between sub-second and tens of seconds.
        """
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
                elif entry.name == "package.json":
                    yield entry

    @staticmethod
    def _read_name(manifest_path: Path) -> str | None:
        """Parse the manifest's ``name`` field, or return ``None``.

        Returns ``None`` for any condition that means "no usable
        package here": unreadable file, malformed JSON, missing
        ``name``, or non-string ``name``. The walker keeps going —
        a single bad manifest never aborts discovery.
        """
        try:
            with manifest_path.open(encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as error:
            logger.debug(
                "npm manifest unreadable, skipping",
                manifest=str(manifest_path),
                error=str(error),
            )
            return None
        name = data.get("name") if isinstance(data, dict) else None
        if not isinstance(name, str) or not name:
            return None
        return name
