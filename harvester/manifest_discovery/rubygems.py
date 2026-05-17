"""RubyGems package detector — parses ``*.gemspec`` files.

Gemspecs are Ruby DSL files, not declarative data — a complete parse
would require a Ruby runtime. In practice the ``name`` field is
near-universally assigned with a literal string on a single line:

    Gem::Specification.new do |spec|
      spec.name = "rails"
      ...
    end

or the equivalent ``s.name``. A regex captures both forms reliably
across the gemspecs of the top RubyGems packages. The rare
metaprogrammatic gemspec that computes its name from a constant or
file reference is skipped (logged) rather than incorrectly parsed.

Walking for ``*.gemspec`` (not a fixed filename) handles repos that
ship one gemspec per gem in subdirectories — common in monorepos
like ``rails/rails`` and ``ruby/rubygems``.
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


# Matches ``spec.name = "..."`` and ``s.name = "..."`` (single or
# double quotes). Spaces around ``=`` are optional. The captured
# group is the name. We deliberately don't try to handle
# ``spec.name = SomeConstant`` or string interpolation — those are
# rare and would require a Ruby parser.
_NAME_ASSIGNMENT = re.compile(
    r"""^\s*(?:\w+)\.name\s*=\s*['"]([^'"]+)['"]\s*$""",
    re.MULTILINE,
)


class RubyGemsManifestDetector:
    """Discovers Ruby gems by walking for ``*.gemspec`` files."""

    ecosystem = "RUBYGEMS"

    def discover(self, repo_dir: Path) -> Iterator[DiscoveredPackage]:
        """Yield one record per ``*.gemspec`` with a literal name assignment."""
        for manifest_path in self._iter_manifests(repo_dir):
            name = self._read_name(manifest_path)
            if name is None:
                continue
            relative_path = manifest_path.parent.relative_to(repo_dir).as_posix()
            yield DiscoveredPackage(
                name=name,
                relative_path=relative_path,
                manifest_file=manifest_path.name,
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
                elif entry.is_file() and entry.suffix == ".gemspec":
                    yield entry

    @staticmethod
    def _read_name(manifest_path: Path) -> str | None:
        """Capture the gem name from a literal assignment, or ``None``.

        Reads the whole gemspec (they're small — kilobytes at most)
        and applies the regex. Returns the first match; multiple
        ``.name`` assignments in one gemspec would be a bug in the
        gemspec itself, not something to defend against.
        """
        try:
            content = manifest_path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            logger.debug(
                "rubygems manifest unreadable, skipping",
                manifest=str(manifest_path),
                error=str(error),
            )
            return None
        match = _NAME_ASSIGNMENT.search(content)
        if not match:
            return None
        name = match.group(1)
        return name if name else None
