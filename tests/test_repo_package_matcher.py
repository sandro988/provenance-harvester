"""Unit tests for ``repo_package_matcher.match_packages_in_repo``.

The matcher's job is to join two sides — popular.csv rows and
manifests in a cloned repo — across the per-ecosystem name conventions
that differ between them. Tests cover the normalisation table row by
row, plus the unmatched / out-of-roster paths the orchestrator
depends on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harvester.repo_package_matcher import (
    CSV_TO_DETECTOR_ECOSYSTEM,
    match_packages_in_repo,
)
from tests.conftest import make_component

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def test_ecosystem_map_covers_every_detector() -> None:
    """The CSV→detector map must list every detector's ecosystem.

    If a new detector lands and we forget to extend the map, targets
    of that ecosystem would silently land in ``unmatched`` — a quiet
    regression the suite should catch loudly.
    """
    from harvester.manifest_discovery import DETECTORS

    detector_ecosystems = {d.ecosystem for d in DETECTORS}
    mapped_detector_ecosystems = set(CSV_TO_DETECTOR_ECOSYSTEM.values())
    assert detector_ecosystems == mapped_detector_ecosystems


def test_npm_unscoped_match(repo_files_factory: Callable[[dict[str, str]], Path]) -> None:
    repo = repo_files_factory({"packages/react-dom/package.json": '{"name": "react-dom"}'})
    result = match_packages_in_repo(repo, [make_component(ecosystem="npm", name="react-dom")])
    assert len(result.matched) == 1
    assert result.matched[0].relative_path == "packages/react-dom"
    assert result.unmatched == ()


def test_npm_scoped_match(repo_files_factory: Callable[[dict[str, str]], Path]) -> None:
    repo = repo_files_factory({"packages/types-node/package.json": '{"name": "@types/node"}'})
    result = match_packages_in_repo(
        repo, [make_component(ecosystem="npm", namespace="@types", name="node")]
    )
    assert len(result.matched) == 1
    assert result.matched[0].relative_path == "packages/types-node"


def test_pypi_pep503_normalisation(
    repo_files_factory: Callable[[dict[str, str]], Path],
) -> None:
    """``Foo_Bar`` in popular.csv must match a manifest declaring ``foo-bar``."""
    repo = repo_files_factory({"libs/foo-bar/pyproject.toml": '[project]\nname = "foo-bar"\n'})
    result = match_packages_in_repo(repo, [make_component(ecosystem="pypi", name="Foo_Bar")])
    assert len(result.matched) == 1
    assert result.matched[0].relative_path == "libs/foo-bar"


def test_maven_groupid_artifactid_join(
    repo_files_factory: Callable[[dict[str, str]], Path],
) -> None:
    pom = (
        '<?xml version="1.0"?>'
        '<project xmlns="http://maven.apache.org/POM/4.0.0">'
        "<modelVersion>4.0.0</modelVersion>"
        "<groupId>org.example</groupId>"
        "<artifactId>core</artifactId>"
        "<version>1.0</version>"
        "</project>"
    )
    repo = repo_files_factory({"modules/core/pom.xml": pom})
    result = match_packages_in_repo(
        repo,
        [make_component(ecosystem="maven", namespace="org.example", name="core")],
    )
    assert len(result.matched) == 1
    assert result.matched[0].relative_path == "modules/core"


def test_nuget_case_insensitive_match(
    repo_files_factory: Callable[[dict[str, str]], Path],
) -> None:
    nuspec = (
        '<?xml version="1.0"?>'
        '<package xmlns="http://schemas.microsoft.com/packaging/2010/07/nuspec.xsd">'
        "<metadata><id>AWSSDK.Core</id><version>1</version>"
        "<authors>x</authors><description>x</description></metadata>"
        "</package>"
    )
    repo = repo_files_factory({"src/aws/AWSSDK.Core.nuspec": nuspec})
    result = match_packages_in_repo(repo, [make_component(ecosystem="nuget", name="awssdk.core")])
    assert len(result.matched) == 1


def test_cargo_crate_match(repo_files_factory: Callable[[dict[str, str]], Path]) -> None:
    repo = repo_files_factory(
        {"crates/serde/Cargo.toml": '[package]\nname = "serde"\nversion = "1.0"\n'}
    )
    result = match_packages_in_repo(repo, [make_component(ecosystem="cargo", name="serde")])
    assert len(result.matched) == 1
    assert result.matched[0].relative_path == "crates/serde"


def test_unmatched_target_recorded(
    repo_files_factory: Callable[[dict[str, str]], Path],
) -> None:
    """A target with no manifest in the repo lands in ``unmatched``."""
    repo = repo_files_factory({"packages/react-dom/package.json": '{"name": "react-dom"}'})
    targets = [
        make_component(ecosystem="npm", name="react-dom"),
        make_component(ecosystem="npm", name="ghost-package"),
    ]
    result = match_packages_in_repo(repo, targets)
    assert len(result.matched) == 1
    assert len(result.unmatched) == 1
    assert result.unmatched[0].name == "ghost-package"


def test_out_of_roster_ecosystem_is_unmatched(
    repo_files_factory: Callable[[dict[str, str]], Path],
) -> None:
    """Ecosystems without a detector (apk, deb, composer, ...) skip detection."""
    repo = repo_files_factory({"placeholder.txt": "x"})
    target = make_component(ecosystem="composer", name="vendor/pkg")
    result = match_packages_in_repo(repo, [target])
    assert result.matched == ()
    assert result.unmatched == (target,)


def test_multi_ecosystem_repo(repo_files_factory: Callable[[dict[str, str]], Path]) -> None:
    """A single repo hosting npm + cargo + maven must match each."""
    pom = (
        '<?xml version="1.0"?>'
        '<project xmlns="http://maven.apache.org/POM/4.0.0">'
        "<modelVersion>4.0.0</modelVersion>"
        "<groupId>org.example</groupId><artifactId>core</artifactId>"
        "<version>1.0</version></project>"
    )
    repo = repo_files_factory(
        {
            "node/package.json": '{"name": "frontend"}',
            "rs/Cargo.toml": '[package]\nname = "engine"\nversion = "1.0"\n',
            "jvm/pom.xml": pom,
        }
    )
    targets = [
        make_component(ecosystem="npm", name="frontend"),
        make_component(ecosystem="cargo", name="engine"),
        make_component(ecosystem="maven", namespace="org.example", name="core"),
    ]
    result = match_packages_in_repo(repo, targets)
    assert len(result.matched) == 3
    paths = {m.component.ecosystem: m.relative_path for m in result.matched}
    assert paths == {"npm": "node", "cargo": "rs", "maven": "jvm"}


def test_duplicate_normalised_name_keeps_shortest_path(
    repo_files_factory: Callable[[dict[str, str]], Path],
) -> None:
    """Two manifests with the same normalised name keep the shorter path."""
    repo = repo_files_factory(
        {
            "vendor/sub/dist/package.json": '{"name": "react"}',
            "packages/react/package.json": '{"name": "react"}',
        }
    )
    result = match_packages_in_repo(repo, [make_component(ecosystem="npm", name="react")])
    assert len(result.matched) == 1
    assert result.matched[0].relative_path == "packages/react"


def test_whole_repo_manifest_returns_dot(
    repo_files_factory: Callable[[dict[str, str]], Path],
) -> None:
    """Single-package repo with manifest at root yields ``"."`` as the path."""
    repo = repo_files_factory({"package.json": '{"name": "alone"}'})
    result = match_packages_in_repo(repo, [make_component(ecosystem="npm", name="alone")])
    assert len(result.matched) == 1
    assert result.matched[0].relative_path == "."
