"""Materialize manifest files of a bare clone into a tmp working tree.

The harvester clones with ``--bare --filter=blob:none --no-checkout``.
That gives us a small, network-efficient clone we can run ``git log``
against — but it has no working tree on disk, so the manifest
detectors (which walk a ``Path`` looking for ``package.json``,
``pom.xml``, …) find nothing.

This module reads the file list at ``HEAD`` via ``git ls-tree``,
filters to the manifest names and extensions our detectors recognise,
and uses ``git archive | tar -x`` to extract just those files into a
tmp directory while preserving their relative paths. The matcher then
walks the tmp directory like any other working tree.

Cost: typically a few hundred KB of blob fetches per repo — even a
large monorepo has only a few hundred manifest files. Far cheaper
than a full ``git checkout HEAD`` which would fetch every blob at
``HEAD``.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import posixpath
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# Exact basenames the manifest detectors parse.
_EXACT_MANIFEST_NAMES: frozenset[str] = frozenset(
    {
        "package.json",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "pom.xml",
        "Cargo.toml",
        "go.mod",
    }
)

# Files matched by extension (nuget projects, gemspecs).
_MANIFEST_EXTENSIONS: tuple[str, ...] = (
    ".nuspec",
    ".csproj",
    ".vbproj",
    ".fsproj",
    ".gemspec",
)

# Cap on how many paths we hand to a single ``git archive`` invocation.
# ARG_MAX on macOS is ~256 KB; even with path lengths averaging 80 chars
# a few thousand paths fit comfortably under that. Chunking at 1000
# leaves the headroom and keeps the invocation count low for repos with
# tens of thousands of files.
_PATHS_PER_ARCHIVE_CALL = 1000


def is_manifest_path(relative_path: str) -> bool:
    """Return ``True`` if ``relative_path`` is a manifest our detectors parse."""
    name = posixpath.basename(relative_path)
    if name in _EXACT_MANIFEST_NAMES:
        return True
    return any(name.endswith(ext) for ext in _MANIFEST_EXTENSIONS)


@contextlib.asynccontextmanager
async def checkout_manifest_files(bare_repo: Path) -> AsyncIterator[Path]:
    """Yield a tmp directory holding the bare clone's manifest files at HEAD.

    The tmp directory is destroyed when the context exits. Safe to
    enter even on repos with zero manifest files — the yielded
    directory is just empty.

    Raises ``RuntimeError`` if any git invocation exits non-zero.
    """
    workdir = Path(tempfile.mkdtemp(prefix="harvester-manifests-"))
    try:
        manifests = await _list_manifest_paths(bare_repo)
        for chunk in itertools.batched(manifests, _PATHS_PER_ARCHIVE_CALL):
            await _archive_into(bare_repo, chunk, workdir)
        yield workdir
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _list_manifest_paths(bare_repo: Path) -> list[str]:
    """Enumerate manifest paths at ``HEAD`` via ``git ls-tree``."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(bare_repo),
        "ls-tree",
        "-r",
        "--name-only",
        "HEAD",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"git ls-tree exited {proc.returncode}: {stderr.decode('utf-8', 'replace')}"
        )
    return [p for p in stdout.decode("utf-8").splitlines() if p and is_manifest_path(p)]


async def _archive_into(bare_repo: Path, paths: tuple[str, ...] | list[str], dest: Path) -> None:
    """Extract ``paths`` at HEAD into ``dest`` via ``git archive`` + ``tar -xf``.

    Goes through a tmp tarball on disk rather than piping the two
    subprocesses together — asyncio's subprocess transport cannot have
    one process's ``StreamReader.stdout`` plugged into another
    process's stdin (the underlying pipe needs a real fileno, which
    ``StreamReader`` doesn't expose). The tmp tarball is removed after
    extraction.
    """
    if not paths:
        return
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as fh:
        tar_path = Path(fh.name)
    try:
        archive_proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(bare_repo),
            "archive",
            "--format=tar",
            f"--output={tar_path}",
            "HEAD",
            *paths,
            stderr=asyncio.subprocess.PIPE,
        )
        _, archive_err = await archive_proc.communicate()
        if archive_proc.returncode != 0:
            raise RuntimeError(
                f"git archive exited {archive_proc.returncode}: "
                f"{archive_err.decode('utf-8', 'replace')}"
            )
        extract_proc = await asyncio.create_subprocess_exec(
            "tar",
            "-x",
            "-C",
            str(dest),
            "-f",
            str(tar_path),
            stderr=asyncio.subprocess.PIPE,
        )
        _, tar_err = await extract_proc.communicate()
        if extract_proc.returncode != 0:
            raise RuntimeError(
                f"tar exited {extract_proc.returncode}: "
                f"{tar_err.decode('utf-8', 'replace')}"
            )
    finally:
        tar_path.unlink(missing_ok=True)

