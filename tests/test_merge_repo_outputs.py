"""Tests for the per-repo → global CSV merge.

Covers the pure-Python ``merge_local`` function. The AWS sync wrapper
is one ``subprocess.run`` call and is exercised end-to-end at deploy
time; mocking it here would only verify the mock.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.merge_repo_outputs import (
    CSV_NAMES,
    NEEDS_REPO_URL,
    expected_header,
    merge_local,
    read_repo_url_from_components,
)


def _write_repo_subdir(
    parent: Path,
    *,
    slug: str,
    repo_url: str,
    component_rows: list[tuple[str, str]] = (("npm", "react"),),
    version_rows: list[tuple[str, ...]] = (("npm", "react", "v18.0.0"),),
    contributor_rows: list[tuple[str, ...]] = (("npm", "react", "v18.0.0", "Alice"),),
    person_rows: list[tuple[str, ...]] = (("npm", "react", "alice@example.com", "Alice"),),
) -> Path:
    """Materialise a minimal per-repo subdir on disk for merging.

    Real harvester output has many more columns; the merger doesn't
    care about column count, only about presence of repo_url in
    components.csv and identical header order across subdirs.
    """
    repo_dir = parent / slug
    repo_dir.mkdir(parents=True)

    (repo_dir / "components.csv").write_text(
        "ecosystem,name,repo_url,last_synced_at\n"
        + "\n".join(f"{eco},{name},{repo_url},2026-05-18T00:00:00" for eco, name in component_rows)
        + "\n"
    )
    (repo_dir / "versions.csv").write_text(
        "ecosystem,name,tag\n" + "\n".join(",".join(row) for row in version_rows) + "\n"
    )
    (repo_dir / "contributors.csv").write_text(
        "ecosystem,name,tag,author_name\n"
        + "\n".join(",".join(row) for row in contributor_rows)
        + "\n"
    )
    (repo_dir / "persons.csv").write_text(
        "ecosystem,name,author_email,author_name\n"
        + "\n".join(",".join(row) for row in person_rows)
        + "\n"
    )
    return repo_dir


def test_merge_concatenates_components_without_prepending_repo_url(tmp_path: Path) -> None:
    """``components.csv`` already has repo_url — passes through verbatim."""
    raw = tmp_path / "raw"
    raw.mkdir()
    _write_repo_subdir(raw, slug="repo_a", repo_url="https://github.com/a/a")
    _write_repo_subdir(raw, slug="repo_b", repo_url="https://github.com/b/b")

    final = tmp_path / "final"
    counts = merge_local(raw, final)

    merged = (final / "components.csv").read_text().splitlines()
    assert merged[0] == "ecosystem,name,repo_url,last_synced_at"
    # 2 data rows, no repo_url duplicated
    assert len(merged) == 3
    assert "https://github.com/a/a" in merged[1]
    assert "https://github.com/b/b" in merged[2]
    assert counts["components"] == 2


def test_merge_prepends_repo_url_to_versions_contributors_persons(tmp_path: Path) -> None:
    """The three lean files gain a ``repo_url`` column on the way through."""
    raw = tmp_path / "raw"
    raw.mkdir()
    _write_repo_subdir(raw, slug="repo_a", repo_url="https://github.com/a/a")
    _write_repo_subdir(raw, slug="repo_b", repo_url="https://github.com/b/b")

    final = tmp_path / "final"
    merge_local(raw, final)

    for name in NEEDS_REPO_URL:
        rows = (final / f"{name}.csv").read_text().splitlines()
        assert rows[0].startswith("repo_url,"), f"{name}.csv header missing repo_url"
        # First data row carries repo_a's URL, second carries repo_b's
        assert rows[1].startswith("https://github.com/a/a,")
        assert rows[2].startswith("https://github.com/b/b,")


def test_merge_skips_subdir_with_missing_components(tmp_path: Path) -> None:
    """No components.csv → no way to source repo_url → skip the whole subdir."""
    raw = tmp_path / "raw"
    raw.mkdir()
    good = _write_repo_subdir(raw, slug="good", repo_url="https://github.com/g/g")
    del good

    bad = raw / "bad"
    bad.mkdir()
    # Versions exists but components does not — the merger can't attach a
    # repo_url, so the subdir is skipped entirely.
    (bad / "versions.csv").write_text("ecosystem,name,tag\nnpm,react,v1\n")

    final = tmp_path / "final"
    counts = merge_local(raw, final)

    # Only "good" contributed
    assert counts["components"] == 1
    assert counts["versions"] == 1


def test_merge_empty_input_writes_empty_outputs(tmp_path: Path) -> None:
    """No subdirs at all → 4 empty files, no exception."""
    raw = tmp_path / "raw"
    raw.mkdir()
    final = tmp_path / "final"

    counts = merge_local(raw, final)

    assert all(c == 0 for c in counts.values())


def test_read_repo_url_from_missing_file_returns_empty(tmp_path: Path) -> None:
    """A missing components.csv is normal during partial harvests."""
    assert read_repo_url_from_components(tmp_path / "absent.csv") == ""


def test_expected_header_adds_repo_url_when_needed(tmp_path: Path) -> None:
    """Headers for versions/contributors/persons get repo_url prepended."""
    sample = tmp_path / "versions.csv"
    sample.write_text("ecosystem,name,tag\n")

    header = expected_header(name="versions", sample_path=sample, repo_url_needed=True)

    assert header == ["repo_url", "ecosystem", "name", "tag"]


def test_expected_header_passes_through_when_not_needed(tmp_path: Path) -> None:
    """Components header is already correct; merger doesn't touch it."""
    sample = tmp_path / "components.csv"
    sample.write_text("ecosystem,name,repo_url,last_synced_at\n")

    header = expected_header(name="components", sample_path=sample, repo_url_needed=False)

    assert header == ["ecosystem", "name", "repo_url", "last_synced_at"]


def test_csv_names_are_exactly_the_four_writer_files() -> None:
    """If the writer adds a 5th file, this test fails so the merge gets updated too."""
    assert set(CSV_NAMES) == {"components", "versions", "contributors", "persons"}


@pytest.mark.parametrize(
    "name",
    sorted(NEEDS_REPO_URL),
)
def test_needs_repo_url_membership_is_consistent_with_writer(name: str) -> None:
    """The three lean files are the ones missing repo_url at write time."""
    assert name in {"versions", "contributors", "persons"}
