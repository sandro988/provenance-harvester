# provenance-harvester

Standalone batch harvester for per-version contributor provenance data.
Companion project to `exodos-backend` — specifically, the Phase 2a + 2b
work documented in
`exodos-backend/docs/provenance/contributor-harvester/`.

## What it does

Takes a list of package coordinates (`ecosystem, name`) and pre-computes
per-version contributor data by cloning each repo, running a single-pass
`git log --all`, and walking tags chronologically with a Python BFS.

Output is CSV in the shape that `exodos-backend`'s
`/admin/provenance/refresh` endpoint loads into Postgres.

## Why a separate repo

- **Different lifecycle:** one-shot offline batch, not part of the runtime service
- **Different deps:** no FastAPI / SQLAlchemy / Celery / Neo4j driver
- **Different runtime:** long-running batch on AWS spot fleet
- **Personal-cloud:** runs on personal AWS, not company infrastructure
  (see `exodos-backend/docs/provenance/architecture-decisions/0001-postgres-warehouse-neo4j-graph.md`)

## Status

| Component | Status |
|---|---|
| Repo skeleton (pyproject, ruff, README) | ✅ |
| Vendored extractor from exodos-backend Phase 1b | ⏳ |
| Smoke test against react | ⏳ |
| Customer footprint extractor (Phase 2a) | ⏳ |
| Popular packages source (Phase 2b) | ⏳ |
| AWS deployment scripts | ⏳ |

## Local development

```bash
# Install dependencies
uv sync

# Run tests
uv run pytest

# Lint
uv run ruff check
uv run ruff format
```

## Architecture

See `exodos-backend/docs/provenance/architecture-decisions/0001-postgres-warehouse-neo4j-graph.md`
for the two-tier Postgres + Neo4j architecture this work feeds into.
