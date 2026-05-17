"""Write the four harvester CSVs from an extractor run.

The CSV shape mirrors ADR 0001's Postgres schema — these files load
directly via ``COPY ... FROM`` into the production warehouse tables.

The writer consumes raw ``TagRangeWalk`` objects (not the orchestrator's
aggregated ``TagRangeContributors``) because contributor-level fields
like ``tz_offset`` need access to individual commits, which the standard
``ContributorAggregator`` collapses away.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from harvester.extractor.contributor_aggregator import BOT_PATTERNS
from harvester.extractor.extractor_types import CommitRecord, TagRangeWalk
from harvester.writer.person_aggregator import PersonRollup, aggregate_persons


@dataclass(frozen=True, slots=True)
class WriterOutput:
    """Paths and row counts after a write."""

    components_path: Path
    versions_path: Path
    contributors_path: Path
    persons_path: Path
    versions_rows: int
    contributors_rows: int
    persons_rows: int


@dataclass(frozen=True, slots=True)
class CsvWriter:
    """Stateless writer — call ``write_all`` per component."""

    out_dir: Path

    def write_all(
        self,
        *,
        ecosystem: str,
        name: str,
        repo_url: str,
        synced_at: str,
        walks: list[TagRangeWalk],
    ) -> WriterOutput:
        """Write all four CSVs for one component."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        components_path = self.out_dir / "components.csv"
        versions_path = self.out_dir / "versions.csv"
        contributors_path = self.out_dir / "contributors.csv"
        persons_path = self.out_dir / "persons.csv"

        self._write_components(components_path, ecosystem, name, repo_url, synced_at)
        versions_rows = self._write_versions(versions_path, ecosystem, name, walks)
        contributors_rows = self._write_contributors(contributors_path, ecosystem, name, walks)
        persons_rows = self._write_persons(persons_path, ecosystem, name, aggregate_persons(walks))

        return WriterOutput(
            components_path=components_path,
            versions_path=versions_path,
            contributors_path=contributors_path,
            persons_path=persons_path,
            versions_rows=versions_rows,
            contributors_rows=contributors_rows,
            persons_rows=persons_rows,
        )

    @staticmethod
    def _write_components(
        path: Path, ecosystem: str, name: str, repo_url: str, synced_at: str
    ) -> None:
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["ecosystem", "name", "repo_url", "last_synced_at"])
            writer.writerow([ecosystem, name, repo_url, synced_at])

    @staticmethod
    def _write_versions(path: Path, ecosystem: str, name: str, walks: list[TagRangeWalk]) -> int:
        rows = 0
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "ecosystem",
                    "name",
                    "tag",
                    "released_at",
                    "commit_sha",
                    "prev_tag",
                    "is_prerelease",
                ]
            )
            for walk in walks:
                writer.writerow(
                    [
                        ecosystem,
                        name,
                        walk.this.name,
                        walk.this.creator_date.isoformat(),
                        walk.this.sha,
                        walk.prev.name if walk.prev is not None else "",
                        "true" if _is_prerelease(walk.this.name) else "false",
                    ]
                )
                rows += 1
        return rows

    @staticmethod
    def _write_contributors(
        path: Path, ecosystem: str, name: str, walks: list[TagRangeWalk]
    ) -> int:
        rows = 0
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "ecosystem",
                    "name",
                    "tag",
                    "author_name",
                    "author_email",
                    "commits",
                    "is_bot",
                    "tz_offset",
                    "first_commit_ts",
                    "last_commit_ts",
                    "commit_sha_first",
                    "commit_sha_last",
                ]
            )
            for walk in walks:
                for row in _contributors_for_range(walk.commits):
                    writer.writerow(
                        [
                            ecosystem,
                            name,
                            walk.this.name,
                            row.author_name,
                            row.author_email,
                            row.commits,
                            "true" if row.is_bot else "false",
                            row.tz_dominant,
                            row.first_commit_ts.isoformat(),
                            row.last_commit_ts.isoformat(),
                            row.commit_sha_first,
                            row.commit_sha_last,
                        ]
                    )
                    rows += 1
        return rows

    @staticmethod
    def _write_persons(path: Path, ecosystem: str, name: str, persons: list[PersonRollup]) -> int:
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "ecosystem",
                    "name",
                    "author_email",
                    "author_name",
                    "commits_lifetime",
                    "versions_contributed",
                    "first_ever_commit",
                    "last_ever_commit",
                    "active_span_days",
                    "tz_dominant",
                    "tz_dominant_ratio",
                    "tz_offsets_distinct",
                    "tz_offsets_seen",
                    "tz_is_likely_unknown",
                    "tz_signal_reason",
                    "tz_explanation",
                    "night_commit_ratio",
                    "weekday_weekend_ratio",
                    "email_domain",
                    "hour_histogram",
                ]
            )
            for p in persons:
                writer.writerow(
                    [
                        ecosystem,
                        name,
                        p.author_email,
                        p.author_name,
                        p.commits_lifetime,
                        p.versions_contributed,
                        p.first_ever_commit.isoformat(),
                        p.last_ever_commit.isoformat(),
                        p.active_span_days,
                        p.tz_dominant,
                        p.tz_dominant_ratio,
                        p.tz_offsets_distinct,
                        ",".join(p.tz_offsets_seen),
                        "true" if p.tz_is_likely_unknown else "false",
                        p.tz_signal_reason,
                        p.tz_explanation,
                        p.night_commit_ratio,
                        p.weekday_weekend_ratio,
                        p.email_domain,
                        ",".join(str(n) for n in p.hour_histogram),
                    ]
                )
        return len(persons)


# ── internal helpers ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _ContribRow:
    author_name: str
    author_email: str
    commits: int
    is_bot: bool
    tz_dominant: str
    first_commit_ts: object
    last_commit_ts: object
    commit_sha_first: str
    commit_sha_last: str


def _contributors_for_range(commits: list[CommitRecord]) -> list[_ContribRow]:
    """Aggregate one tag range's commits into per-contributor rows.

    Unlike ``ContributorAggregator``, this version also tracks the
    dominant TZ offset within the range and the first/last commit SHAs.
    """
    grouped: dict[tuple[str, str], list[CommitRecord]] = {}
    for c in commits:
        grouped.setdefault((c.author_name, c.author_email), []).append(c)

    rows: list[_ContribRow] = []
    for (name, email), person_commits in grouped.items():
        ordered = sorted(person_commits, key=lambda c: c.author_ts)
        tz_dominant = Counter(c.tz_offset for c in person_commits).most_common(1)[0][0]
        rows.append(
            _ContribRow(
                author_name=name,
                author_email=email,
                commits=len(person_commits),
                is_bot=_looks_like_bot(name, email),
                tz_dominant=tz_dominant,
                first_commit_ts=ordered[0].author_ts,
                last_commit_ts=ordered[-1].author_ts,
                commit_sha_first=ordered[0].sha,
                commit_sha_last=ordered[-1].sha,
            )
        )
    return rows


def _looks_like_bot(name: str, email: str) -> bool:
    """Same bot heuristic as the vendored ``ContributorAggregator``."""
    haystack = f"{name} {email}".lower()
    return any(pattern in haystack for pattern in BOT_PATTERNS)


_PRERELEASE_MARKERS: tuple[str, ...] = (
    "-alpha",
    "-beta",
    "-rc",
    "-dev",
    "-pre",
    "-nightly",
    ".alpha",
    ".beta",
    ".rc",
    ".dev",
    ".pre",
    ".nightly",
)


def _is_prerelease(tag: str) -> bool:
    """Cheap prerelease detection from common semver tag suffixes."""
    lower = tag.lower()
    return any(marker in lower for marker in _PRERELEASE_MARKERS)
