"""Tests for the manifest-only working-tree materializer.

The harvester clones with ``--bare --filter=tree:0 --no-checkout``, so
the on-disk repo has no working tree. The matcher needs to walk a
``Path`` looking for ``package.json``, ``pom.xml``, etc. — those files
must be selectively materialized first. These tests cover the
selection rules (which filenames qualify), the materialization
correctness (relative paths preserved, contents readable), and the
boundary cases (empty repo, repo with only non-manifest files).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from harvester.manifest_checkout import (
    checkout_manifest_files,
    is_manifest_path,
)

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------- Predicate tests (pure, no git) ----------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("package.json", True),
        ("packages/react/package.json", True),
        ("pyproject.toml", True),
        ("setup.py", True),
        ("setup.cfg", True),
        ("pom.xml", True),
        ("Cargo.toml", True),
        ("go.mod", True),
        ("anything/foo.nuspec", True),
        ("src/foo.csproj", True),
        ("src/foo.vbproj", True),
        ("src/foo.fsproj", True),
        ("specs/foo.gemspec", True),
        ("src/index.js", False),
        ("README.md", False),
        ("packages/react/src/component.tsx", False),
        ("Cargo.lock", False),  # lock files are not manifests
        ("package-lock.json", False),
        ("go.sum", False),
        # Dash-prefixed segments are rejected wholesale to prevent
        # option-flag smuggling into ``git archive`` (a tracked path
        # like ``--output=/victim/package.json`` would otherwise pass
        # the basename check and let git's argument parser override the
        # archive destination).
        ("--output=/etc/passwd/package.json", False),
        ("--remote=evil.example.com/package.json", False),
        ("legit/--output=foo/package.json", False),
        ("-rf/package.json", False),
    ],
)
def test_is_manifest_path(path: str, expected: bool) -> None:
    assert is_manifest_path(path) is expected


# ---------- Bare-repo fixture and integration tests ----------


def _git(args: list[str], cwd: Path, env_extra: dict[str, str] | None = None) -> None:
    import os

    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def bare_repo_factory(
    tmp_path: Path,
) -> Callable[[dict[str, str]], Path]:
    """Build a bare clone of a repo containing the given files.

    Mirrors how the production harvester sees a freshly-cloned repo:
    no working tree, just the bare ``.git`` internals. Every test using
    this fixture exercises the same code path as the production cloner.
    """

    def _build(files: dict[str, str]) -> Path:
        source = tmp_path / "source"
        source.mkdir()
        _git(["init", "--initial-branch=main"], source)
        _git(["config", "user.name", "Test"], source)
        _git(["config", "user.email", "test@test"], source)

        for rel, content in files.items():
            dest = source / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)

        _git(["add", "."], source)
        _git(["commit", "-m", "fixture"], source)

        bare = tmp_path / "repo.git"
        _git(["clone", "--bare", str(source), str(bare)], tmp_path)
        return bare

    return _build


async def _materialize(bare: Path) -> tuple[set[str], dict[str, str]]:
    """Materialize and return (relative-path set, path → contents map)."""
    async with checkout_manifest_files(bare) as workdir:
        rel_paths: set[str] = set()
        contents: dict[str, str] = {}
        for p in workdir.rglob("*"):
            if p.is_file():
                rel = p.relative_to(workdir).as_posix()
                rel_paths.add(rel)
                contents[rel] = p.read_text()
        return rel_paths, contents


def test_materialize_extracts_manifest_files_only(
    bare_repo_factory: Callable[[dict[str, str]], Path],
) -> None:
    bare = bare_repo_factory(
        {
            "packages/foo/package.json": '{"name": "foo"}',
            "packages/foo/src/index.js": "// noise",
            "packages/bar/package.json": '{"name": "bar"}',
            "packages/bar/README.md": "noise",
            "Cargo.toml": '[package]\nname="rs"',
        }
    )
    rel_paths, contents = asyncio.run(_materialize(bare))

    assert rel_paths == {
        "packages/foo/package.json",
        "packages/bar/package.json",
        "Cargo.toml",
    }
    assert contents["packages/foo/package.json"] == '{"name": "foo"}'
    assert contents["packages/bar/package.json"] == '{"name": "bar"}'


def test_materialize_preserves_directory_structure(
    bare_repo_factory: Callable[[dict[str, str]], Path],
) -> None:
    """The matcher needs the same path layout as the source repo."""
    bare = bare_repo_factory(
        {
            "a/b/c/package.json": '{"name": "deep"}',
            "package.json": '{"name": "root"}',
        }
    )
    rel_paths, _ = asyncio.run(_materialize(bare))
    assert "a/b/c/package.json" in rel_paths
    assert "package.json" in rel_paths


def test_materialize_empty_repo_returns_empty_dir(
    bare_repo_factory: Callable[[dict[str, str]], Path],
) -> None:
    """No manifest files → empty workdir, no crash."""
    bare = bare_repo_factory({"src/main.js": "// nothing here"})
    rel_paths, _ = asyncio.run(_materialize(bare))
    assert rel_paths == set()


def test_materialize_recognises_extension_based_manifests(
    bare_repo_factory: Callable[[dict[str, str]], Path],
) -> None:
    """NuGet projects and gemspecs match by extension, not exact name."""
    bare = bare_repo_factory(
        {
            "src/MyLib/MyLib.csproj": "<Project/>",
            "specs/cool_gem.gemspec": "Gem::Specification.new {}",
            "src/Pkg.nuspec": "<package/>",
        }
    )
    rel_paths, _ = asyncio.run(_materialize(bare))
    assert rel_paths == {
        "src/MyLib/MyLib.csproj",
        "specs/cool_gem.gemspec",
        "src/Pkg.nuspec",
    }


def test_materialize_refuses_to_extract_dash_prefixed_paths(
    bare_repo_factory: Callable[[dict[str, str]], Path],
    tmp_path: Path,
) -> None:
    """Regression: a tracked file whose path begins with ``--`` (e.g.
    ``--output=<victim>/package.json``) must not be passed to
    ``git archive`` as an argv — otherwise git would option-parse it,
    redirecting the tar destination to the attacker-chosen victim file.

    Two layers of defence are exercised here:
      1. ``is_manifest_path`` rejects dash-prefixed segments outright,
         so the path is never added to the archive argv.
      2. The ``--`` end-of-options marker on the ``git archive`` call
         would block flag parsing even if a path slipped through.

    The test attacks layer 1 by including a file whose name would smuggle
    a ``git archive --output=<victim>`` flag if let through, and asserts
    the victim file is never created.
    """
    victim = tmp_path / "victim.tar"
    # The path's basename ends with ``package.json`` (basename-only filter
    # would have accepted it); the first segment is a smuggled flag.
    attack_path = f"--output={victim}/package.json"
    bare = bare_repo_factory(
        {
            attack_path: '{"name": "evil"}',
            "legit/package.json": '{"name": "legit"}',
        }
    )

    rel_paths, _ = asyncio.run(_materialize(bare))

    # The attack path must not appear in the materialised output.
    assert attack_path not in rel_paths
    assert "legit/package.json" in rel_paths
    # And — most importantly — the victim file must not have been
    # created on disk by a redirected ``git archive`` output.
    assert not victim.exists()
