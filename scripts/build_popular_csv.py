"""Build popular.csv — top-downloaded packages across language ecosystems.

Complements `footprint.csv` (which is what customers actually use) with
the most popular packages globally per ecosystem — so new customer BOMs
have a high probability of hitting pre-computed provenance data even
for packages no existing customer has uploaded yet.

For each supported ecosystem, queries ecosyste.ms's
``/registries/<reg>/packages?sort=downloads&order=desc`` endpoint up to
a per-ecosystem cap, emits one row per package using the registry's
latest_release_number as the version. Rows already in ``footprint.csv``
are excluded so the two files are disjoint.

The per-ecosystem caps are sized proportionally to registry activity:
npm and Maven get the biggest slice (they have the largest meaningful
download tails), small ecosystems get smaller caps. Beyond the cap is
mostly zero-download abandoned packages that won't show up in real
customer BOMs anyway.

Output schema matches ``footprint.csv``: ``ecosystem, namespace, name,
version`` (4 columns), so the same downstream harvester can consume
either file interchangeably.
"""

from __future__ import annotations

import csv
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

from harvester.ecosystems_client import EcosystemsClient
from harvester.registry_sources import top_packages as native_top_packages

REPO_ROOT = Path(__file__).resolve().parent.parent
FOOTPRINT_CSV = REPO_ROOT / "output" / "footprint.csv"
OUT_CSV = REPO_ROOT / "output" / "popular.csv"

# Per-ecosystem caps sized proportionally to the meaningful download tail.
# Order is the order of fetch (so we fail fast if early ecosystems break).
ECOSYSTEM_CAPS: list[tuple[str, int]] = [
    ("npm", 50_000),
    ("maven", 50_000),
    ("pypi", 30_000),
    ("golang", 30_000),
    ("nuget", 30_000),
    ("composer", 20_000),
    ("cargo", 20_000),
    ("gem", 15_000),
    ("cocoapods", 10_000),
    ("pub", 10_000),
    ("hex", 5_000),
    ("conan", 5_000),
    ("luarocks", 2_000),
    ("pear", 1_000),
]

Coord = tuple[str, str, str, str, str]  # (ecosystem, namespace, name, version, repo_url)


def load_footprint_identity_keys(path: Path) -> set[tuple[str, str, str, str]]:
    """Return ``(ecosystem, namespace, name, version)`` keys already in footprint.

    Excluded from popular.csv so the two files are disjoint. We key on
    the identity quadruple regardless of repo URL — if the same package-
    version is in footprint, we don't need it in popular even if URLs
    differ.
    """
    if not path.exists():
        return set()
    keys: set[tuple[str, str, str, str]] = set()
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            keys.add((row["ecosystem"], row["namespace"], row["name"], row["version"]))
    return keys


def split_qualified_name(ecosystem: str, qualified: str) -> tuple[str, str]:
    """Split ecosyste.ms's combined ``name`` field into ``(namespace, name)``.

    Mirrors how packageurl-python parses PURLs so popular.csv stays
    consistent with footprint.csv's namespace/name split:

    - npm: ``@scope/pkg`` → ``("@scope", "pkg")``
    - maven: ``groupId:artifactId`` → ``("groupId", "artifactId")``
    - golang: ``host/org/repo`` → ``("host/org", "repo")``
    - composer: ``vendor/package`` → ``("vendor", "package")``
    - everything else without a separator: ``("", qualified)``
    """
    if ecosystem == "maven" and ":" in qualified:
        namespace, _, name = qualified.partition(":")
        return namespace, name
    if "/" not in qualified:
        return "", qualified
    if qualified.startswith("@"):
        # npm scoped: keep the @ in the namespace, split on first /
        namespace, _, name = qualified.partition("/")
        return namespace, name
    if ecosystem == "golang":
        # Go modules: namespace is everything up to the last slash
        namespace, _, name = qualified.rpartition("/")
        return namespace, name
    # composer, github, cocoapods etc — namespace/name (single slash)
    namespace, _, name = qualified.partition("/")
    return namespace, name


# Ecosystems with a direct registry-API source. The rest fall back to
# ecosyste.ms — necessary today for maven/golang/composer/cocoapods/pub/
# hex/conan/luarocks/pear which don't expose clean top-by-downloads.
NATIVE_ECOSYSTEMS = frozenset({"npm", "pypi", "cargo", "gem", "nuget"})


def fetch_ecosystem_top_native(
    http_client: httpx.Client, ecosystem: str, cap: int
) -> tuple[list[Coord], int]:
    """Use a per-registry direct API for an ecosystem we have a native source for."""
    coords: list[Coord] = []
    skipped_no_version = 0
    for namespace, name, version, repo_url in native_top_packages(http_client, ecosystem, cap):
        if not version:
            skipped_no_version += 1
            continue
        coords.append((ecosystem, namespace, name, version, repo_url or ""))
    return coords, skipped_no_version


def fetch_ecosystem_top_ecosystems(
    client: EcosystemsClient, ecosystem: str, cap: int
) -> tuple[list[Coord], int]:
    """Fall back to ecosyste.ms for ecosystems without a native source."""
    coords: list[Coord] = []
    skipped_no_version = 0
    for qualified, latest_version, _downloads, repo_url in client.top_packages(ecosystem, cap):
        if not latest_version:
            skipped_no_version += 1
            continue
        namespace, name = split_qualified_name(ecosystem, qualified)
        coords.append((ecosystem, namespace, name, latest_version, repo_url or ""))
    return coords, skipped_no_version


def main() -> int:
    footprint_keys = load_footprint_identity_keys(FOOTPRINT_CSV)
    print(f"[popular] footprint has {len(footprint_keys)} (eco,ns,name,version) keys to exclude")
    print()

    all_coords: set[Coord] = set()
    per_ecosystem_stats: list[tuple[str, int, int, int, float]] = []

    with (
        EcosystemsClient() as ecosystems_client,
        httpx.Client(timeout=30.0) as http_client,
    ):
        for ecosystem, cap in ECOSYSTEM_CAPS:
            started = time.perf_counter()
            if ecosystem in NATIVE_ECOSYSTEMS:
                coords, skipped = fetch_ecosystem_top_native(http_client, ecosystem, cap)
                source = "native"
            else:
                coords, skipped = fetch_ecosystem_top_ecosystems(ecosystems_client, ecosystem, cap)
                source = "ecosyste.ms"
            elapsed = time.perf_counter() - started

            before_count = len(all_coords)
            for coord in coords:
                # Identity key is the 4-tuple; repo_url is metadata only.
                if coord[:4] not in footprint_keys:
                    all_coords.add(coord)
            added = len(all_coords) - before_count

            per_ecosystem_stats.append((ecosystem, len(coords), skipped, added, elapsed))
            print(
                f"  {ecosystem:<11} ({source:<11}) fetched={len(coords):>6}  "
                f"skipped_no_ver={skipped:>4}  new_after_exclude={added:>6}  "
                f"({elapsed:>5.1f}s)",
                flush=True,
            )

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ecosystem", "namespace", "name", "version", "repo_url"])
        for row in sorted(all_coords):
            writer.writerow(row)

    print()
    print(f"[popular] total rows written: {len(all_coords)}")
    eco_counts = Counter(eco for eco, _, _, _, _ in all_coords)
    with_url = sum(1 for _, _, _, _, url in all_coords if url)
    print(f"[popular] rows with repo_url : {with_url} ({with_url / max(len(all_coords), 1):.0%})")
    print("[popular] per-ecosystem in output:")
    for eco, count in eco_counts.most_common():
        print(f"  {eco:<11} {count:>6}")
    print()
    print(f"[popular] wrote {OUT_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
