"""Pure parsers for ``git for-each-ref`` and ``git log`` textual output.

These functions take the raw stdout of a git command and produce
typed objects (``TagInfo``, ``CommitRecord``). No I/O, no
subprocesses, no async — every parser is a pure transformation that
unit tests can exercise with hand-written fixture strings.

The format strings expected on input are exported as
``TAG_REF_FORMAT`` and ``COMMIT_LOG_FORMAT`` so the caller can pass
them to git verbatim. Keeping the format string and its parser in
the same module is a deliberate locality choice: if one changes,
the other has to.
"""

from __future__ import annotations

from datetime import UTC, datetime

from harvester.extractor._logger import logger
from harvester.extractor.extractor_types import (
    CommitRecord,
    TagInfo,
)

# ``%H %P`` together produce ``<sha> <parent_shas_space_separated>``.
# A root commit has an empty %P, which leaves a trailing space before
# the first tab — the parser handles that by filtering empty parent
# tokens. Tabs separate the four fields; tabs in author names are
# vanishingly rare in real package repos and we accept the rare
# false negative rather than pay for ``-z`` null-record framing.
#
# ``%an`` and ``%ae`` are the *literal* author name and email. The
# uppercase variants (``%aN``, ``%aE``) apply ``.mailmap`` rewriting
# unconditionally regardless of ``--use-mailmap`` / ``--no-use-mailmap``
# flags, and the ``.mailmap`` ships inside the cloned (attacker-
# controlled) repo — see ``tag_walker`` for the threat-model rationale.
# ── HARVESTER DIVERGENCE FROM EXODOS-BACKEND PHASE 1B SOURCE ──
# Switched the timestamp field from ``%at`` (unix epoch) to ``%aI``
# (ISO 8601 with offset, e.g. ``2012-07-16T22:23:10-07:00``) so the
# harvester can preserve the committer's local TZ offset alongside
# the UTC instant. Phase 1b doesn't need TZ because BOM-time inference
# relies on LLM guesses from name+email. The harvester uses TZ for the
# persons.csv rollup (tz_dominant, hour_histogram, night_commit_ratio),
# a stronger geo signal than name guessing for stable-TZ countries
# like Russia/China/Japan that don't observe DST. If/when this lands
# back in Phase 1b, the parser below and the CommitRecord type both
# need matching updates.
COMMIT_LOG_FORMAT = "%H %P\t%an\t%ae\t%aI"

# Four-field tab-delimited tag descriptor.
#
# Annotated tags (``git tag -a``) are git objects in their own right;
# ``%(objectname)`` returns the *tag object's* SHA, not the commit
# it points at. ``%(*objectname)`` is the dereferenced commit for
# annotated tags and an empty string for lightweight tags. The
# parser prefers the dereferenced value when present so the SHA
# emitted in ``TagInfo`` is always a commit SHA — what every
# downstream consumer expects.
TAG_REF_FORMAT = "%(refname:short)\t%(objectname)\t%(*objectname)\t%(creatordate:iso8601-strict)"


def parse_tag_lines(text: str) -> list[TagInfo]:
    """Parse the stdout of ``git for-each-ref --format=TAG_REF_FORMAT``.

    Lines whose shape does not match the expected three-tab layout
    are logged at WARNING and skipped — git is not in the habit of
    emitting malformed output, so a skip indicates either a format
    drift or a truly broken ref, neither of which should crash an
    otherwise-good extraction.
    """
    tags: list[TagInfo] = []
    for line in text.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            logger.warning("Malformed for-each-ref line, skipping", line=line)
            continue
        name, ref_sha, deref_sha, iso_date = parts
        # ``deref_sha`` is the commit for annotated tags, empty
        # otherwise. Lightweight tags already point at the commit
        # directly via ``ref_sha``.
        commit_sha = deref_sha or ref_sha
        try:
            creator_date = datetime.fromisoformat(iso_date)
        except ValueError:
            logger.warning(
                "Unparseable tag creator date, skipping",
                tag=name,
                raw=iso_date,
            )
            continue
        tags.append(TagInfo(name=name, sha=commit_sha, creator_date=creator_date))
    return tags


def parse_commit_lines(text: str) -> dict[str, CommitRecord]:
    """Parse the stdout of ``git log --pretty=format:COMMIT_LOG_FORMAT``.

    Returns a dict keyed by commit SHA so the BFS in ``commit_graph``
    can walk parent pointers in O(1) per hop. Lines that fail to
    parse are skipped with a warning rather than aborting the whole
    extraction — one bad commit should not waste the cost of cloning
    a repo with thousands of good ones.
    """
    commits: dict[str, CommitRecord] = {}
    for line in text.splitlines():
        commit = _parse_commit_line(line)
        if commit is not None:
            commits[commit.sha] = commit
    return commits


def _parse_commit_line(line: str) -> CommitRecord | None:
    """Parse one record of ``COMMIT_LOG_FORMAT``."""
    if not line:
        return None
    parts = line.split("\t")
    if len(parts) != 4:
        logger.warning("Malformed git log line, skipping", line=line[:120])
        return None
    sha_and_parents, name, email, iso_ts = parts
    tokens = sha_and_parents.split(" ")
    sha = tokens[0]
    parents = tuple(token for token in tokens[1:] if token)
    try:
        # ``%aI`` is strict ISO 8601 with a numeric offset. The parsed
        # datetime is tz-aware; we keep ``author_ts`` in UTC for backward
        # compatibility with the source extractor and stash the local
        # offset as a separate ``"+0300"``-style string for the rollup.
        local_dt = datetime.fromisoformat(iso_ts)
        author_ts = local_dt.astimezone(UTC)
        tz_offset = local_dt.strftime("%z") or "+0000"
    except (ValueError, TypeError):
        logger.warning(
            "Unparseable author timestamp, skipping commit",
            sha=sha,
            raw=iso_ts,
        )
        return None
    return CommitRecord(
        sha=sha,
        parents=parents,
        author_name=name,
        author_email=email,
        author_ts=author_ts,
        tz_offset=tz_offset,
    )
