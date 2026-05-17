"""Build footprint.csv from cypher-shell raw PURL dumps.

Reads every ``output/raw/*_purls.txt`` produced by Phase 2a extraction,
parses each PURL into ``(ecosystem, namespace, name, version)``, unions
across files, and writes ``output/footprint.csv`` as the canonical
4-column input to the harvester.

The PURL **namespace** is essential identity data for most ecosystems:

- maven needs the groupId (``org.hdrhistogram:HdrHistogram``)
- npm scoped packages need the scope (``@types/node``)
- go modules need the host+org (``github.com/foo/bar``)
- composer needs the vendor (``symfony/console``)
- deb / apk need the distro

Dropping it silently merges distinct packages and makes most rows
unresolvable against package registries. We preserve it as its own
column so downstream consumers can re-form the qualified name in
whatever shape each registry expects.

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

Coord = tuple[str, str, str, str]  # (ecosystem, namespace, name, version)


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


def parse_to_coords(purls: list[str]) -> tuple[set[Coord], int]:
    """Parse PURLs into ``(ecosystem, namespace, name, version)`` tuples.

    PURLs that fail to parse or have no version are counted as skipped.
    Namespace is the empty string when the PURL has none.
    """
    coords: set[Coord] = set()
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
        coords.add((parsed.type, parsed.namespace or "", parsed.name, parsed.version))
    return coords, skipped


def main() -> int:
    raw_files = sorted(RAW_DIR.glob("*_purls.txt"))
    if not raw_files:
        print(f"no raw dumps in {RAW_DIR}", file=sys.stderr)
        return 1

    all_coords: set[Coord] = set()
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
        writer.writerow(["ecosystem", "namespace", "name", "version"])
        for row in sorted(all_coords):
            writer.writerow(row)

    print()
    print(f"raw total       : {total_raw}")
    print(f"deduped total   : {len(all_coords)}")
    if total_raw:
        print(f"dedup ratio     : {len(all_coords) / total_raw:.1%}")

    namespaced = sum(1 for _, ns, _, _ in all_coords if ns)
    print(f"with namespace  : {namespaced} ({namespaced / len(all_coords):.0%})")

    print()
    print("ecosystems:")
    for eco, count in Counter(eco for eco, _, _, _ in all_coords).most_common():
        print(f"  {eco:<12} {count:>8}")

    print()
    print(f"wrote {OUT_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
