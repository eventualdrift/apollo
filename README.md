# Apollo

A private, persistent personal AI. Phase zero.

Architecture is frozen: see [`docs/architecture/phase-zero-spec.md`](docs/architecture/phase-zero-spec.md)
and the [ADRs](docs/adr/). The specification wins over the code; if they disagree, the code is wrong.

## Milestone 1

Apollo holds a text conversation against `brain.fake`, and every turn and model invocation is
durably recorded. No real model, no memory, no retrieval — those arrive in later milestones.

## Running it

```sh
docker compose -f docker/docker-compose.yml up -d          # postgres only
export APOLLO_DATABASE_DSN="host=127.0.0.1 port=5432 user=apollo_owner dbname=apollo"
pip install -e '.[dev]'

apollo migrate
CONV=$(apollo new)
apollo say "$CONV" "You there?"
apollo show "$CONV"
```

Secrets come from the environment and are never written to a TOML file, a log, or the database.

## Checks

```sh
ruff check src tests
mypy
pytest -q                                   # unit + architecture
APOLLO_TEST_ADMIN_DSN="host=127.0.0.1 port=5432 user=postgres dbname=postgres" pytest -q
```

Integration tests need real PostgreSQL. There is no SQLite fallback: the schema relies on triggers,
generated `tsvector` columns, `jsonb` containment and role grants.
