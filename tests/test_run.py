"""Unit tests for the harvester's per-repo orchestration helper.

``_walk_matched_or_fallback`` is the function that decides whether
each in-flight repo gets per-package walks or a single repo-wide
fallback walk. The decision tree is small but consequential — when it
mis-fires we lose either granularity (forces fallback unnecessarily)
or data (no fallback when matches dry up). Tests cover every branch
with a fake walker recording its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from harvester.extractor.extractor_types import TagInfo, TagRangeWalk
from harvester.repo_package_matcher import MatchResult, PackageMatch
from harvester.run import (
    ComponentVersion,
    RepoWorkItem,
    _canonical_name,
    _walk_matched_or_fallback,
    load_work_for_single_repo,
)

if TYPE_CHECKING:
    pass


def _make_walk(tag_name: str) -> TagRangeWalk:
    """Build a minimal ``TagRangeWalk`` placeholder for assertion shape only."""
    info = TagInfo(
        name=tag_name,
        sha="deadbeef" * 5,
        creator_date=datetime(2024, 1, 1, tzinfo=UTC),
    )
    return TagRangeWalk(prev=None, this=info, commits=[])


def _make_component(name: str = "react-dom", namespace: str = "") -> ComponentVersion:
    return ComponentVersion(
        ecosystem="npm",
        namespace=namespace,
        name=name,
        version="19.1.0",
        repo_url="https://github.com/facebook/react",
    )


def _make_work_item(components: tuple[ComponentVersion, ...]) -> RepoWorkItem:
    return RepoWorkItem(repo_url=components[0].repo_url, components=components)


@dataclass
class FakeWalker:
    """Records each ``walk`` invocation; returns the canned per-path output.

    ``walks_by_path`` maps the ``path_filter`` argument (``None`` for
    repo-wide) to the list of TagRangeWalk objects to return. Paths not
    listed yield an empty list — which mirrors the real walker for
    directories with no tag-scoped commits.
    """

    walks_by_path: dict[str | None, list[TagRangeWalk]] = field(default_factory=dict)
    calls: list[str | None] = field(default_factory=list)

    async def walk(
        self,
        repo_dir: Path,
        *,
        path_filter: str | None = None,
    ) -> list[TagRangeWalk]:
        del repo_dir
        self.calls.append(path_filter)
        return list(self.walks_by_path.get(path_filter, []))


def _make_match(component: ComponentVersion, path: str) -> PackageMatch:
    return PackageMatch(component=component, relative_path=path)


async def test_per_package_walks_emit_one_harvest_per_match(tmp_path: Path) -> None:
    """Every matched package gets its own walk and its own PackageHarvest."""
    react_dom = _make_component(name="react-dom")
    scheduler = _make_component(name="scheduler")
    matches = (
        _make_match(react_dom, "packages/react-dom"),
        _make_match(scheduler, "packages/scheduler"),
    )
    walker = FakeWalker(
        walks_by_path={
            "packages/react-dom": [_make_walk("v19.0.0")],
            "packages/scheduler": [_make_walk("v0.23.0"), _make_walk("v0.24.0")],
        }
    )

    packages = await _walk_matched_or_fallback(
        walker=walker,
        clone_path=tmp_path,
        match_result=MatchResult(matched=matches, unmatched=()),
        work_item=_make_work_item((react_dom, scheduler)),
    )

    assert walker.calls == ["packages/react-dom", "packages/scheduler"]
    assert [p.name for p in packages] == ["react-dom", "scheduler"]
    assert [len(p.walks) for p in packages] == [1, 2]


async def test_empty_matched_walks_are_dropped(tmp_path: Path) -> None:
    """A match whose path saw no tag activity must not pollute the output."""
    pkg = _make_component(name="lonely")
    matches = (_make_match(pkg, "packages/lonely"),)
    walker = FakeWalker(walks_by_path={"packages/lonely": []})

    packages = await _walk_matched_or_fallback(
        walker=walker,
        clone_path=tmp_path,
        match_result=MatchResult(matched=matches, unmatched=()),
        work_item=_make_work_item((pkg,)),
    )

    assert packages == []
    assert walker.calls == ["packages/lonely"]


async def test_zero_matches_falls_back_to_repo_wide(tmp_path: Path) -> None:
    """No matches → exactly one repo-wide walk, exemplar component used."""
    exemplar = _make_component(name="react-dom")
    walker = FakeWalker(walks_by_path={None: [_make_walk("v1.0.0")]})

    packages = await _walk_matched_or_fallback(
        walker=walker,
        clone_path=tmp_path,
        match_result=MatchResult(matched=(), unmatched=(exemplar,)),
        work_item=_make_work_item((exemplar,)),
    )

    assert walker.calls == [None]
    assert len(packages) == 1
    assert packages[0].name == "react-dom"
    assert packages[0].ecosystem == "npm"


async def test_zero_matches_and_empty_repo_walk_returns_no_packages(
    tmp_path: Path,
) -> None:
    """Fallback that produces nothing must propagate as 'no packages'."""
    exemplar = _make_component(name="dead-package")
    walker = FakeWalker(walks_by_path={None: []})

    packages = await _walk_matched_or_fallback(
        walker=walker,
        clone_path=tmp_path,
        match_result=MatchResult(matched=(), unmatched=(exemplar,)),
        work_item=_make_work_item((exemplar,)),
    )

    assert packages == []
    assert walker.calls == [None]


async def test_fallback_uses_exemplar_namespace_in_name(tmp_path: Path) -> None:
    """A scoped npm exemplar must be glued into ``@scope/name``."""
    exemplar = _make_component(namespace="@types", name="node")
    walker = FakeWalker(walks_by_path={None: [_make_walk("v1.0.0")]})

    packages = await _walk_matched_or_fallback(
        walker=walker,
        clone_path=tmp_path,
        match_result=MatchResult(matched=(), unmatched=(exemplar,)),
        work_item=_make_work_item((exemplar,)),
    )

    assert packages[0].name == "@types/node"


@pytest.mark.parametrize(
    ("namespace", "name", "expected"),
    [
        ("", "react", "react"),
        ("@types", "node", "@types/node"),
        ("org.example", "core", "org.example/core"),
    ],
)
def test_canonical_name(namespace: str, name: str, expected: str) -> None:
    assert _canonical_name(namespace, name) == expected


# --- load_work_for_single_repo ------------------------------------------------

_HEADER = "ecosystem,namespace,name,version,repo_url,resolution_source\n"


def _write_csv(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "footprint_resolved.csv"
    path.write_text(_HEADER + body)
    return path


def test_single_repo_filters_csv_to_one_url(tmp_path: Path) -> None:
    """All matching rows collapse into one work item; others are filtered out."""
    csv_body = (
        "npm,,react,19.1.0,https://github.com/facebook/react,deps_dev\n"
        "npm,,react-dom,19.1.0,https://github.com/facebook/react,deps_dev\n"
        "npm,,vue,3.5.0,https://github.com/vuejs/core,deps_dev\n"
    )
    csv_path = _write_csv(tmp_path, csv_body)

    work = load_work_for_single_repo(csv_path, "https://github.com/facebook/react")

    assert len(work) == 1
    assert work[0].repo_url == "https://github.com/facebook/react"
    assert len(work[0].components) == 2
    assert {comp.name for comp in work[0].components} == {"react", "react-dom"}


def test_single_repo_url_not_in_csv_returns_empty(tmp_path: Path) -> None:
    """Missing URL is not a crash — caller will print 'nothing to do' and exit."""
    csv_body = "npm,,react,19.1.0,https://github.com/facebook/react,deps_dev\n"
    csv_path = _write_csv(tmp_path, csv_body)

    work = load_work_for_single_repo(csv_path, "https://github.com/never/heard-of-it")

    assert work == []


def test_single_repo_rows_with_blank_repo_url_are_skipped(tmp_path: Path) -> None:
    """A blank ``repo_url`` row is dropped regardless of how it matches by name."""
    csv_body = (
        "npm,,react,19.1.0,,deps_dev\n"
        "npm,,react,19.1.0,https://github.com/facebook/react,deps_dev\n"
    )
    csv_path = _write_csv(tmp_path, csv_body)

    work = load_work_for_single_repo(csv_path, "https://github.com/facebook/react")

    assert len(work) == 1
    assert len(work[0].components) == 1


def test_single_repo_empty_url_argument_raises(tmp_path: Path) -> None:
    """An empty ``--single-repo`` argument is a programming error, not a no-op."""
    csv_path = _write_csv(tmp_path, "")

    with pytest.raises(ValueError, match="empty"):
        load_work_for_single_repo(csv_path, "   ")


def test_single_repo_whitespace_is_trimmed(tmp_path: Path) -> None:
    """Leading/trailing whitespace on the URL argument matches trimmed CSV rows."""
    csv_body = "npm,,react,19.1.0,https://github.com/facebook/react,deps_dev\n"
    csv_path = _write_csv(tmp_path, csv_body)

    work = load_work_for_single_repo(csv_path, "  https://github.com/facebook/react  ")

    assert len(work) == 1
    assert work[0].repo_url == "https://github.com/facebook/react"
