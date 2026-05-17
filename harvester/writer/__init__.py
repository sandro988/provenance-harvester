"""CSV writer for the harvester.

Takes an ``ExtractionResult`` from the extractor and writes four CSVs
matching the Postgres-load schema in ADR 0001:

- ``components.csv`` — 1 row per component
- ``versions.csv``   — 1 row per version
- ``contributors.csv`` — 1 row per (version, contributor) tag-range slice
- ``persons.csv``    — 1 row per (component, person) lifetime rollup

The persons rollup is what makes geo-risk and provenance queries fast:
it pre-computes the strong identity/origin signals (timezone-dominant,
hour histogram, night-commit ratio, etc.) that are expensive to derive
from contributor rows at query time.
"""

from harvester.writer.csv_writer import CsvWriter, PackageHarvest, WriterOutput
from harvester.writer.person_aggregator import PersonRollup, aggregate_persons

__all__ = [
    "CsvWriter",
    "PackageHarvest",
    "PersonRollup",
    "WriterOutput",
    "aggregate_persons",
]
