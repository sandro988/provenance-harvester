"""Build footprint.csv from cypher-shell raw PURL dumps.

Reads every ``output/raw/*_purls.txt`` produced by Phase 2a extraction,
parses each PURL into ``(ecosystem, name, version)``, unions across
files, and writes ``output/footprint.csv`` as the canonical 3-column
input to the harvester.

Run after dumping PURLs from each environment:

    uv run python scripts/build_footprint_csv.py
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from pathlib import Path

from packageurl import PackageURL

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "output" / "raw"
OUT_CSV = REPO_ROOT / "output" / "footprint.csv"


def load_purls(path: Path) -> list[str]:
    """Return PURL strings from a cypher-shell ``--format plain`` dump.

    Strips the ``c.purl`` header row, surrounding double quotes, and
    blank lines.
    """
    purls = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line == "c.purl":
            continue
        if line.startswith('"') and line.endswith('"'):
            line = line[1:-1]
        purls.append(line)
    return purls


def parse_to_coords(purls: list[str]) -> tuple[set[tuple[str, str, str]], int]:
    """Parse PURLs into ``(ecosystem, name, version)`` tuples.

    PURLs that fail to parse or have no version are counted as skipped.
    """
    coords: set[tuple[str, str, str]] = set()
    skipped = 0
    for purl in purls:
        try:
            parsed = PackageURL.from_string(purl)
        except ValueError:
            skipped += 1
            continue
        if not parsed.version:
            skipped += 1
            continue
        coords.add((parsed.type, parsed.name, parsed.version))
    return coords, skipped


def main() -> int:
    raw_files = sorted(RAW_DIR.glob("*_purls.txt"))
    if not raw_files:
        print(f"no raw dumps in {RAW_DIR}", file=sys.stderr)
        return 1

    all_coords: set[tuple[str, str, str]] = set()
    total_raw = 0
    for path in raw_files:
        purls = load_purls(path)
        coords, skipped = parse_to_coords(purls)
        total_raw += len(purls)
        print(
            f"  {path.name:<24} {len(purls):>8} purls -> "
            f"{len(coords):>8} coords (skipped {skipped})"
        )
        all_coords |= coords

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ecosystem", "name", "version"])
        for row in sorted(all_coords):
            writer.writerow(row)

    print()
    print(f"raw total       : {total_raw}")
    print(f"deduped total   : {len(all_coords)}")
    if total_raw:
        print(f"dedup ratio     : {len(all_coords) / total_raw:.1%}")

    print()
    print("ecosystems:")
    for eco, count in Counter(eco for eco, _, _ in all_coords).most_common():
        print(f"  {eco:<12} {count:>8}")

    print()
    print(f"wrote {OUT_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
