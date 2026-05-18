"""Merge per-repo harvester output CSVs into 4 final concatenated CSVs.

The harvester writes one subdirectory per repo, each with four CSVs
(``components``, ``versions``, ``contributors``, ``persons``). That
layout is the right shape for parallel workers — no shared files
means no contention, and each subdir is its own resumability unit.
But every downstream consumer (warehouse loader, ad-hoc analysis,
data export) wants the data stacked into one file per kind. This
script does the stacking.

Three CSVs (``versions``, ``contributors``, ``persons``) don't
carry a ``repo_url`` column natively; the writer relied on the
filesystem-level subdir to disambiguate. After merging into one
file, the subdir hint is gone — so this script prepends ``repo_url``
to those three on the way through, sourced from the matching
``components.csv`` in the same subdir. ``components.csv`` already
has the column and passes through unchanged.

The AWS-friendly entry point uses ``aws s3 sync`` for transfer (no
new boto3 dependency; the worker AMI already has the CLI). The
merge logic itself is pure Python over a local directory tree and
is unit-tested directly via :func:`merge_local`.

Typical run on an EC2 instance::

    uv run python scripts/merge_repo_outputs.py \\
        --bucket harvester-output \\
        --input-prefix results/ \\
        --output-prefix results/final/ \\
        --work-dir /mnt/work/merge
"""

from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

CSV_NAMES: tuple[str, ...] = (
    "components",
    "versions",
    "contributors",
    "persons",
)

# Three of the four files don't carry repo_url natively. The merge step
# prepends it so the concatenated file is self-describing without
# needing the per-repo subdir context.
NEEDS_REPO_URL: frozenset[str] = frozenset({"versions", "contributors", "persons"})


def iter_repo_dirs(input_root: Path) -> Iterator[Path]:
    """Yield each immediate subdirectory of ``input_root``.

    Each subdir corresponds to one harvested repo. The walk is shallow
    by design — the harvester writes flat, one subdir per repo with no
    deeper nesting.
    """
    for entry in sorted(input_root.iterdir()):
        if entry.is_dir():
            yield entry


def read_repo_url_from_components(components_path: Path) -> str:
    """Return the ``repo_url`` recorded in this subdir's components.csv.

    Every row in components.csv shares the same repo_url (the writer
    writes them all from the same source), so reading the first data
    row is enough. Missing file or empty rows produce an empty string,
    which the caller treats as "skip this subdir".
    """
    try:
        with components_path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                return row.get("repo_url", "").strip()
    except FileNotFoundError:
        return ""
    return ""


def append_csv_rows(
    source_path: Path,
    writer: csv.writer,
    repo_url: str | None,
) -> int:
    """Stream rows from ``source_path`` onto an already-open writer.

    The source CSV's header is skipped because the global file's header
    was written once at the start. When ``repo_url`` is provided it is
    prepended to every emitted row — used for the three CSVs that don't
    carry the column natively. Returns the number of data rows written.
    """
    rows_written = 0
    with source_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        next(reader, None)  # skip header
        for row in reader:
            if repo_url is not None:
                writer.writerow([repo_url, *row])
            else:
                writer.writerow(row)
            rows_written += 1
    return rows_written


def expected_header(name: str, sample_path: Path, repo_url_needed: bool) -> list[str]:
    """Read a sample subdir's header and prepend ``repo_url`` when needed.

    We can't hard-code the headers because the writer evolves over time
    and we want the merge to stay in sync with whatever the harvester
    emits. The sample subdir is just the first repo we encounter that
    has the file — any subdir would work since headers are identical
    within a harvester version.
    """
    del name  # diagnostic-only; sample_path carries the truth
    with sample_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        original = next(reader)
    return ["repo_url", *original] if repo_url_needed else list(original)


def merge_local(input_root: Path, output_dir: Path) -> dict[str, int]:
    """Walk per-repo subdirs in ``input_root``, write 4 merged CSVs to ``output_dir``.

    Returns a dict mapping each CSV name to the count of data rows
    written. Pure-Python — no AWS, no subprocess — so the whole thing
    is unit-testable with a tmp directory.

    Subdirs missing one of the four expected CSVs are skipped with a
    structured log line; partial harvests don't corrupt the output.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    repo_dirs = list(iter_repo_dirs(input_root))
    if not repo_dirs:
        return dict.fromkeys(CSV_NAMES, 0)

    # Pick a sample subdir per file kind to derive headers. Different
    # subdirs are sampled per kind because a partial harvest may have
    # some files but not others; we prefer a complete reference.
    samples: dict[str, Path] = {}
    for repo_dir in repo_dirs:
        for name in CSV_NAMES:
            if name not in samples and (repo_dir / f"{name}.csv").exists():
                samples[name] = repo_dir / f"{name}.csv"
        if len(samples) == len(CSV_NAMES):
            break

    # Open four output files and write headers up front.
    open_files = {}
    writers: dict[str, csv.writer] = {}
    counts: dict[str, int] = dict.fromkeys(CSV_NAMES, 0)
    try:
        for name in CSV_NAMES:
            sample = samples.get(name)
            if sample is None:
                # Every subdir was missing this file; emit an empty
                # placeholder so downstream loaders don't have to
                # special-case the absent file.
                (output_dir / f"{name}.csv").write_text("")
                continue
            header = expected_header(
                name=name,
                sample_path=sample,
                repo_url_needed=name in NEEDS_REPO_URL,
            )
            fh = (output_dir / f"{name}.csv").open("w", newline="", encoding="utf-8")
            open_files[name] = fh
            writers[name] = csv.writer(fh)
            writers[name].writerow(header)

        skipped_no_components = 0
        for repo_dir in repo_dirs:
            components_path = repo_dir / "components.csv"
            repo_url = read_repo_url_from_components(components_path)
            if not repo_url:
                skipped_no_components += 1
                continue
            for name in CSV_NAMES:
                source = repo_dir / f"{name}.csv"
                if not source.exists() or name not in writers:
                    continue
                prefix = repo_url if name in NEEDS_REPO_URL else None
                counts[name] += append_csv_rows(source, writers[name], prefix)

    finally:
        for fh in open_files.values():
            fh.close()

    return counts


def aws_sync(source: str, destination: str, *, quiet: bool = False) -> None:
    """Wrap ``aws s3 sync`` for either direction (S3↔local).

    Separate from the merge logic so tests can exercise the merge in
    isolation. The AWS CLI is preferred over boto3 because the worker
    AMI already has it and the cp/sync semantics are well-known.
    """
    cmd = ["aws", "s3", "sync", source, destination]
    if quiet:
        cmd.append("--quiet")
    subprocess.run(cmd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge per-repo harvester output CSVs into 4 final CSVs."
    )
    parser.add_argument("--bucket", required=True, help="S3 bucket name.")
    parser.add_argument(
        "--input-prefix",
        required=True,
        help="S3 key prefix containing one subdirectory per harvested repo.",
    )
    parser.add_argument(
        "--output-prefix",
        required=True,
        help="S3 key prefix that the 4 merged CSVs will be uploaded under.",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path("/mnt/work/merge"),
        help="Local scratch directory for the sync + merge. Wiped on start.",
    )
    parser.add_argument(
        "--keep-work-dir",
        action="store_true",
        help="Don't wipe the work directory on start. Useful for reruns.",
    )
    args = parser.parse_args()

    raw_dir = args.work_dir / "raw"
    final_dir = args.work_dir / "final"

    if not args.keep_work_dir and args.work_dir.exists():
        shutil.rmtree(args.work_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    final_dir.mkdir(parents=True, exist_ok=True)

    s3_input = f"s3://{args.bucket}/{args.input_prefix.lstrip('/')}"
    s3_output = f"s3://{args.bucket}/{args.output_prefix.lstrip('/')}"

    started = time.perf_counter()
    print(f"[merge] syncing {s3_input} → {raw_dir} ...", flush=True)
    aws_sync(s3_input, str(raw_dir))

    print(f"[merge] merging per-repo subdirs into {final_dir} ...", flush=True)
    counts = merge_local(raw_dir, final_dir)

    print(f"[merge] uploading {final_dir} → {s3_output} ...", flush=True)
    aws_sync(str(final_dir), s3_output)

    elapsed = time.perf_counter() - started
    print()
    print(f"[merge] done in {elapsed / 60:.1f} min")
    for name, count in counts.items():
        print(f"  {name + '.csv':<18} {count:>10,} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
