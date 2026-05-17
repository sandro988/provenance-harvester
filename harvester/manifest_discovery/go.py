"""Go module detector — parses ``go.mod`` files.

The ``module`` directive on the first non-comment line of a
``go.mod`` declares the import path, which is the same string the
ecosystem uses to identify the package (``github.com/spf13/cobra``,
``go.opentelemetry.io/otel``, etc.). We surface that verbatim.

Go modules can nest: a repository may declare a root ``go.mod`` and
additional ``go.mod`` files for sub-modules in subdirectories. Each
yields its own record — exactly what per-package attribution wants.

We deliberately avoid the official ``modfile`` parser (golang
stdlib) — it's a few hundred lines, would force a Go runtime
dependency, and the ``module`` directive grammar is trivial:
``module <import-path>`` on its own line, optionally quoted. A
~10-line regex parser covers every conformant manifest.
"""

from __future__ import annotations

import re
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


# Matches a single-line module directive: ``module <path>`` with the
# path optionally wrapped in double quotes. The Go grammar also
# permits a block form (``module ( ... )``) but in practice no
# real-world go.mod uses it for the module directive — modulefiles
# only block-group ``require``/``replace``/``exclude``. We match the
# common case and log+skip the rest.
_MODULE_LINE = re.compile(r'^\s*module\s+(?:"([^"]+)"|(\S+))\s*$')


class GoManifestDetector:
    """Discovers Go modules by walking for ``go.mod`` files."""

    ecosystem = "GO"

    def discover(self, repo_dir: Path) -> Iterator[DiscoveredPackage]:
        """Yield one record per ``go.mod`` with a parseable ``module``."""
        for manifest_path in self._iter_manifests(repo_dir):
            name = self._read_module(manifest_path)
            if name is None:
                continue
            relative_path = manifest_path.parent.relative_to(repo_dir).as_posix()
            yield DiscoveredPackage(
                name=name,
                relative_path=relative_path,
                manifest_file="go.mod",
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
                elif entry.name == "go.mod":
                    yield entry

    @staticmethod
    def _read_module(manifest_path: Path) -> str | None:
        """Parse the ``module`` directive, or return ``None``.

        Reads the file line-by-line until it finds the module
        directive — the spec guarantees it appears before any other
        directives, but in practice many files comment-prefix it.
        We skip blank lines and ``//`` comments and stop at the
        first non-trivial line.
        """
        try:
            with manifest_path.open(encoding="utf-8") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line or line.startswith("//"):
                        continue
                    match = _MODULE_LINE.match(line)
                    if match:
                        return match.group(1) or match.group(2)
                    # First substantive line wasn't the module
                    # directive — malformed or block-form go.mod.
                    break
        except OSError as error:
            logger.debug(
                "go manifest unreadable, skipping",
                manifest=str(manifest_path),
                error=str(error),
            )
            return None
        return None
