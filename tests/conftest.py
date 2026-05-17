"""Shared test fixtures for the harvester suite.

Two building blocks compose every harvester test:

- ``repo_files_factory`` writes an arbitrary in-memory file tree
  into a tmp dir and returns the dir path. Manifests, source files,
  whatever — the factory only cares about paths and contents.
- ``make_component`` produces a ``ComponentVersion`` with sensible
  defaults overrideable per call. Tests describe only what differs
  from a canonical target row.

Both are pure factories — no module-level state, no mutation between
tests. New helpers added here MUST follow the same shape: callable
that returns a value, no fixture-coupled side-effects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from harvester.run import ComponentVersion

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def repo_files_factory(tmp_path: Path) -> Callable[[dict[str, str]], Path]:
    """Return a factory that materialises an in-memory file tree on disk.

    Keys are POSIX-style relative paths; values are file contents.
    Parent directories are created on demand. The returned path is the
    tmp dir's root — pass it straight to detectors as ``repo_dir``.
    """

    def _build(files: dict[str, str]) -> Path:
        for rel, content in files.items():
            dest = tmp_path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
        return tmp_path

    return _build


def make_component(
    *,
    ecosystem: str = "npm",
    namespace: str = "",
    name: str = "react-dom",
    version: str = "19.1.0",
    repo_url: str = "https://github.com/facebook/react",
) -> ComponentVersion:
    """Build a ``ComponentVersion`` for tests — override only what matters."""
    return ComponentVersion(
        ecosystem=ecosystem,
        namespace=namespace,
        name=name,
        version=version,
        repo_url=repo_url,
    )
