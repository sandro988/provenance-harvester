"""Per-person lifetime rollup across all tag-range walks of one component.

Walks all CommitRecords and produces one ``PersonRollup`` per unique
``(author_name, author_email)`` identity, pre-computing the geo and
behaviour signals that would otherwise be expensive to query at runtime
from raw contributor rows:

- ``tz_dominant`` + ``tz_dominant_ratio`` — stable TZ vs traveller
- ``hour_histogram`` + ``night_commit_ratio`` — work pattern
- ``weekday_weekend_ratio`` — pro vs hobby signal
- ``active_span_days`` — long-term vs drive-by contributor
- ``email_domain`` — corporate vs personal identifier

Tag ranges in a clean release history don't overlap, so summing commits
across ranges gives a true lifetime count. If a project re-tagged the
same commits under multiple ranges, the same SHA would be double-counted
— that's a project-hygiene issue not worth defending against here.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from harvester.extractor.contributor_aggregator import BOT_PATTERNS
from harvester.extractor.extractor_types import CommitRecord, TagRangeWalk

# Local hour buckets considered "night" — 22:00 through 05:59 inclusive.
# Six-hour window; deliberately excludes 06:00 (early-bird) and 21:00
# (late-evening but not graveyard).
NIGHT_HOURS: frozenset[int] = frozenset({22, 23, 0, 1, 2, 3, 4, 5})

# Stable enum-ish strings for the ``tz_signal_reason`` field. Documented
# here so future queries can pattern-match against the exact strings.
SIGNAL_REASONS: frozenset[str] = frozenset(
    {
        "ok",  # Real human, real TZ, usable geo signal
        "bot",  # Matched bot heuristic (dependabot, renovate, etc.)
        "github_noreply",  # ``@users.noreply.github.com`` — anonymised
        "drive_by_likely_web_ui",  # +0000 with ≤3 commits — likely web-UI edit
        "all_utc_ambiguous",  # +0000 100% with real history — Iceland/UK/misconfig
        "mostly_utc",  # +0000 dominant but mixed — server-side merge mix
    }
)


@dataclass(frozen=True, slots=True)
class PersonRollup:
    """Lifetime stats for one ``(name, email)`` identity in one component."""

    author_name: str
    author_email: str
    commits_lifetime: int
    versions_contributed: int
    first_ever_commit: datetime
    last_ever_commit: datetime
    active_span_days: int
    tz_dominant: str
    tz_dominant_ratio: float
    tz_offsets_distinct: int
    tz_offsets_seen: tuple[str, ...]  # All unique TZs, ordered most→least frequent
    tz_is_likely_unknown: bool  # True when the TZ data is weak/ambiguous
    tz_signal_reason: str  # Machine-readable enum — see SIGNAL_REASONS
    tz_explanation: str  # Human-readable sentence the UI displays as-is
    night_commit_ratio: float
    weekday_weekend_ratio: float
    email_domain: str
    hour_histogram: tuple[int, ...]  # 24 buckets, index = local hour


def aggregate_persons(walks: list[TagRangeWalk]) -> list[PersonRollup]:
    """Build one ``PersonRollup`` per unique identity across all walks."""
    per_person_commits: dict[tuple[str, str], list[CommitRecord]] = {}
    per_person_tags: dict[tuple[str, str], set[str]] = {}

    for walk in walks:
        for commit in walk.commits:
            key = (commit.author_name, commit.author_email)
            per_person_commits.setdefault(key, []).append(commit)
            per_person_tags.setdefault(key, set()).add(walk.this.name)

    return [
        _rollup_one(name, email, commits, per_person_tags[(name, email)])
        for (name, email), commits in per_person_commits.items()
    ]


def _rollup_one(
    name: str,
    email: str,
    commits: list[CommitRecord],
    tags: set[str],
) -> PersonRollup:
    """Reduce a single person's commits into the rollup fields."""
    tz_counter: Counter[str] = Counter(c.tz_offset for c in commits)
    tz_dominant, tz_dominant_count = tz_counter.most_common(1)[0]
    tz_offsets_seen = tuple(tz for tz, _ in tz_counter.most_common())

    hour_buckets = [0] * 24
    night_count = 0
    weekday_count = 0
    for commit in commits:
        local_hour = _local_hour(commit.author_ts, commit.tz_offset)
        hour_buckets[local_hour] += 1
        if local_hour in NIGHT_HOURS:
            night_count += 1
        if _local_weekday(commit.author_ts, commit.tz_offset) < 5:
            weekday_count += 1

    ts_values = [c.author_ts for c in commits]
    first_ts = min(ts_values)
    last_ts = max(ts_values)
    weekend_count = len(commits) - weekday_count

    tz_dominant_ratio = round(tz_dominant_count / len(commits), 4)
    is_unknown, reason = _classify_tz_signal(
        author_name=name,
        author_email=email,
        tz_dominant=tz_dominant,
        tz_dominant_ratio=tz_dominant_ratio,
        commits_lifetime=len(commits),
    )
    explanation = _explain_signal(
        reason=reason,
        tz_dominant=tz_dominant,
        tz_offsets_distinct=len(tz_counter),
    )

    return PersonRollup(
        author_name=name,
        author_email=email,
        commits_lifetime=len(commits),
        versions_contributed=len(tags),
        first_ever_commit=first_ts,
        last_ever_commit=last_ts,
        active_span_days=(last_ts - first_ts).days,
        tz_dominant=tz_dominant,
        tz_dominant_ratio=tz_dominant_ratio,
        tz_offsets_distinct=len(tz_counter),
        tz_offsets_seen=tz_offsets_seen,
        tz_is_likely_unknown=is_unknown,
        tz_signal_reason=reason,
        tz_explanation=explanation,
        night_commit_ratio=round(night_count / len(commits), 4),
        weekday_weekend_ratio=(
            round(weekday_count / weekend_count, 4) if weekend_count else float(weekday_count)
        ),
        email_domain=email.split("@", 1)[1].lower() if "@" in email else "",
        hour_histogram=tuple(hour_buckets),
    )


def _explain_signal(
    *,
    reason: str,
    tz_dominant: str,
    tz_offsets_distinct: int,
) -> str:
    """Map a ``tz_signal_reason`` to a UI-ready sentence.

    "ok" cases are templated because the reason alone doesn't carry the
    nuance — a stable single-TZ contributor reads very differently from
    a 6-TZ traveller, even though both are classified ``ok``.
    """
    if reason == "bot":
        return "Automated bot account — geographic data is not applicable."
    if reason == "github_noreply":
        return "Uses GitHub's anonymized email address; real location cannot be determined."
    if reason == "drive_by_likely_web_ui":
        return "Single contribution via GitHub's web interface — no reliable location data."
    if reason == "all_utc_ambiguous":
        return (
            "All commits in UTC, which may indicate Iceland, the UK, "
            "West Africa, or a misconfigured machine."
        )
    if reason == "mostly_utc":
        return (
            "Most commits in UTC — possibly server-side merges rather than personal contributions."
        )
    # ``ok`` cases — vary by how many TZs they've used.
    if tz_offsets_distinct == 1:
        return f"Consistent contributor from timezone {tz_dominant}."
    if tz_offsets_distinct == 2:
        return f"Stable location: timezone {tz_dominant}, with seasonal DST shifts."
    return (
        f"Contributes from {tz_offsets_distinct} different timezones "
        f"(mostly {tz_dominant}) — possibly a traveller or shared account."
    )


def _classify_tz_signal(
    *,
    author_name: str,
    author_email: str,
    tz_dominant: str,
    tz_dominant_ratio: float,
    commits_lifetime: int,
) -> tuple[bool, str]:
    """Decide whether the TZ data is trustworthy and label why.

    Heuristics are ordered most-specific first. Anything reaching the
    bottom of the function gets the ``"ok"`` signal — usable for geo
    queries. The returned reason is one of ``SIGNAL_REASONS``.
    """
    email_lower = author_email.lower()
    haystack = f"{author_name} {email_lower}"
    if any(pattern in haystack for pattern in BOT_PATTERNS):
        return True, "bot"
    if "noreply.github.com" in email_lower:
        return True, "github_noreply"
    if tz_dominant == "+0000":
        if commits_lifetime <= 3:
            return True, "drive_by_likely_web_ui"
        if tz_dominant_ratio >= 0.95:
            return True, "all_utc_ambiguous"
        return True, "mostly_utc"
    return False, "ok"


def _local_hour(utc_ts: datetime, tz_offset: str) -> int:
    """Compute the committer's local hour from UTC timestamp + ``%az`` offset."""
    return (utc_ts + _tz_offset_to_timedelta(tz_offset)).hour


def _local_weekday(utc_ts: datetime, tz_offset: str) -> int:
    """0 = Monday … 6 = Sunday in the committer's local time."""
    return (utc_ts + _tz_offset_to_timedelta(tz_offset)).weekday()


def _tz_offset_to_timedelta(tz_offset: str) -> timedelta:
    """Parse ``+0300`` / ``-0700`` to a ``timedelta``."""
    if not tz_offset or len(tz_offset) != 5:
        return timedelta()
    sign = 1 if tz_offset[0] == "+" else -1
    hours = int(tz_offset[1:3])
    minutes = int(tz_offset[3:5])
    return timedelta(minutes=sign * (hours * 60 + minutes))
