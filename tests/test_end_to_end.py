"""End-to-end test of the per-repo harvest pipeline.

Builds a real git repo on disk with two npm packages and disjoint
per-package authors, then runs ``harvest_one_repo`` against it with a
``FakeCloner`` that yields the prebuilt repo instead of fetching one
over the network. Exercises the full chain — matcher → walker →
writer — and asserts the on-disk CSVs separate per-package
contributors instead of collapsing them.

No network access. Slow only by virtue of spawning a handful of git
subprocesses; runs in well under a second on a modern laptop.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from harvester.extractor.extractor_types import ExtractorDeps
from harvester.extractor.git_runner import GitRunner
from harvester.extractor.tag_walker import TagWalker
from harvester.run import ComponentVersion, RepoWorkItem, harvest_one_repo

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


# ---------- Local git fixtures ----------


def _git(args: list[str], cwd: Path, env_extra: dict[str, str] | None = None) -> None:
    """Run git, capturing output, raising on non-zero exit.

    Wrapped so failures during fixture construction produce readable
    pytest tracebacks instead of opaque subprocess errors.
    """
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


def _commit_as(repo: Path, author_name: str, author_email: str, message: str) -> None:
    """Stage all and commit with explicit author identity.

    Setting only ``GIT_AUTHOR_*`` keeps committer fields as the
    repo-config default — we only care about author attribution for
    the contributor walk.
    """
    _git(["add", "."], repo)
    _git(
        ["commit", "-m", message],
        repo,
        env_extra={
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
        },
    )


@pytest.fixture
def multi_package_repo(tmp_path: Path) -> Path:
    """A two-package npm monorepo with distinct authors per package.

    Layout:
        packages/foo/package.json      (FooAuthor commits, tagged v1.0.0)
        packages/bar/package.json      (BarAuthor commits, tagged v2.0.0)

    The test asserts that ``foo-pkg``'s contributor rows contain
    FooAuthor and never BarAuthor — proof that ``path_filter`` is
    routing commits correctly.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "--initial-branch=main"], repo)
    _git(["config", "user.name", "Init"], repo)
    _git(["config", "user.email", "init@test"], repo)

    foo = repo / "packages" / "foo"
    foo.mkdir(parents=True)
    (foo / "package.json").write_text('{"name": "foo-pkg"}\n')
    (foo / "index.js").write_text("// foo entry\n")
    _commit_as(repo, "FooAuthor", "foo@test", "add foo package")
    _git(["tag", "v1.0.0"], repo)

    bar = repo / "packages" / "bar"
    bar.mkdir(parents=True)
    (bar / "package.json").write_text('{"name": "bar-pkg"}\n')
    (bar / "index.js").write_text("// bar entry\n")
    _commit_as(repo, "BarAuthor", "bar@test", "add bar package")
    _git(["tag", "v2.0.0"], repo)

    return repo


@pytest.fixture
def single_root_npm_repo(tmp_path: Path) -> Path:
    """A repo with a root ``package.json`` whose name matches no target.

    Used to exercise the no-match fallback: the matcher finds packages
    but none correspond to the harvester's target list, so the
    orchestrator must fall back to a repo-wide walk under the exemplar
    component name.
    """
    repo = tmp_path / "lonely"
    repo.mkdir()
    _git(["init", "--initial-branch=main"], repo)
    _git(["config", "user.name", "Init"], repo)
    _git(["config", "user.email", "init@test"], repo)
    (repo / "package.json").write_text('{"name": "actually-something-else"}\n')
    (repo / "main.js").write_text("// main\n")
    _commit_as(repo, "SoloAuthor", "solo@test", "initial")
    _git(["tag", "v1.0.0"], repo)
    return repo


# ---------- Cloner fake ----------


class _FakeCloner:
    """Yields a prebuilt repo path as if ``clone_to_temp`` had cloned it."""

    def __init__(self, repo_path: Path) -> None:
        self._repo_path = repo_path

    @contextlib.asynccontextmanager
    async def clone_to_temp(self, repo_url: str) -> AsyncIterator[Path]:
        del repo_url
        yield self._repo_path


# ---------- Helpers ----------


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def _run_harvest(repo_path: Path, work_item: RepoWorkItem, out_root: Path) -> dict:
    deps = ExtractorDeps.defaults()
    walker = TagWalker(deps, GitRunner(deps.git_executable))
    cloner = _FakeCloner(repo_path)
    return asyncio.run(
        harvest_one_repo(
            work_item=work_item,
            output_root=out_root,
            cloner=cloner,
            walker=walker,
            semaphore=asyncio.Semaphore(1),
            shutdown_event=asyncio.Event(),
        )
    )


# ---------- Tests ----------


def test_per_package_contributors_are_separated(multi_package_repo: Path, tmp_path: Path) -> None:
    """foo-pkg's contributor rows must not leak into bar-pkg's rows."""
    repo_url = "https://github.com/example/multi"
    components = (
        ComponentVersion("npm", "", "foo-pkg", "1.0.0", repo_url),
        ComponentVersion("npm", "", "bar-pkg", "2.0.0", repo_url),
        ComponentVersion("npm", "", "ghost-pkg", "0.0.1", repo_url),
    )
    work_item = RepoWorkItem(repo_url=repo_url, components=components)
    out_root = tmp_path / "out"

    result = _run_harvest(multi_package_repo, work_item, out_root)

    assert result["status"] == "ok"
    assert result["matched"] == 2
    assert result["unmatched"] == 1
    assert result["packages_written"] == 2

    repo_out = out_root / work_item.slug
    contributors = _read_csv(repo_out / "contributors.csv")
    foo_rows = [r for r in contributors if r["name"] == "foo-pkg"]
    bar_rows = [r for r in contributors if r["name"] == "bar-pkg"]

    assert foo_rows, "expected at least one foo-pkg contributor row"
    assert bar_rows, "expected at least one bar-pkg contributor row"
    foo_authors = {r["author_name"] for r in foo_rows}
    bar_authors = {r["author_name"] for r in bar_rows}
    assert "FooAuthor" in foo_authors
    assert "BarAuthor" not in foo_authors
    assert "BarAuthor" in bar_authors
    assert "FooAuthor" not in bar_authors


def test_components_csv_has_one_row_per_matched_package(
    multi_package_repo: Path, tmp_path: Path
) -> None:
    """components.csv schema: one row per package, not one per repo."""
    repo_url = "https://github.com/example/multi"
    components = (
        ComponentVersion("npm", "", "foo-pkg", "1.0.0", repo_url),
        ComponentVersion("npm", "", "bar-pkg", "2.0.0", repo_url),
    )
    work_item = RepoWorkItem(repo_url=repo_url, components=components)

    _run_harvest(multi_package_repo, work_item, tmp_path / "out")

    rows = _read_csv(tmp_path / "out" / work_item.slug / "components.csv")
    names = {r["name"] for r in rows}
    assert names == {"foo-pkg", "bar-pkg"}
    assert all(r["repo_url"] == repo_url for r in rows)


def test_zero_match_repo_falls_back_to_repo_wide(
    single_root_npm_repo: Path, tmp_path: Path
) -> None:
    """When no targets match any manifest, fall back so the data still lands."""
    repo_url = "https://github.com/example/lonely"
    exemplar = ComponentVersion("npm", "", "claimed-name", "1.0.0", repo_url)
    work_item = RepoWorkItem(repo_url=repo_url, components=(exemplar,))

    result = _run_harvest(single_root_npm_repo, work_item, tmp_path / "out")

    assert result["status"] == "ok_fallback_repo_wide"
    assert result["matched"] == 0
    assert result["unmatched"] == 1
    assert result["packages_written"] == 1

    repo_out = tmp_path / "out" / work_item.slug
    rows = _read_csv(repo_out / "components.csv")
    assert len(rows) == 1
    assert rows[0]["name"] == "claimed-name"



@pytest.fixture
def bare_clone_of_multi_package_repo(
    multi_package_repo: Path, tmp_path: Path
) -> Path:
    """Bare-clone the multi-package fixture to match production cloner shape.

    The harvester clones every repo with ``git clone --bare
    --filter=tree:0 --no-checkout``. That shape has no working tree, so
    the matcher must materialise manifest files before it can detect
    them. Running the e2e test against a bare clone closes the loop
    that the synthetic working-tree fixture missed.
    """
    bare = tmp_path / "bare-clone.git"
    subprocess.run(
        ["git", "clone", "--bare", str(multi_package_repo), str(bare)],
        check=True,
        capture_output=True,
        text=True,
    )
    return bare


def test_per_package_separation_holds_through_bare_clone(
    bare_clone_of_multi_package_repo: Path, tmp_path: Path
) -> None:
    """The full pipeline must work against the bare-clone shape used in prod."""
    repo_url = "https://github.com/example/multi"
    components = (
        ComponentVersion("npm", "", "foo-pkg", "1.0.0", repo_url),
        ComponentVersion("npm", "", "bar-pkg", "2.0.0", repo_url),
    )
    work_item = RepoWorkItem(repo_url=repo_url, components=components)
    out_root = tmp_path / "out"

    result = _run_harvest(bare_clone_of_multi_package_repo, work_item, out_root)

    assert result["status"] == "ok"
    assert result["matched"] == 2
    assert result["packages_written"] == 2

    contributors = _read_csv(out_root / work_item.slug / "contributors.csv")
    foo_authors = {r["author_name"] for r in contributors if r["name"] == "foo-pkg"}
    bar_authors = {r["author_name"] for r in contributors if r["name"] == "bar-pkg"}
    assert "FooAuthor" in foo_authors
    assert "BarAuthor" not in foo_authors
    assert "BarAuthor" in bar_authors
    assert "FooAuthor" not in bar_authors
