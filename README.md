# Apollo

A private, persistent personal AI. Phase zero.

Architecture is frozen: see [`docs/architecture/phase-zero-spec.md`](docs/architecture/phase-zero-spec.md)
and the [ADRs](docs/adr/). The specification wins over the code; if they disagree, the code is wrong.

## Milestone 1

Apollo holds a text conversation against `brain.fake`, and every turn and model invocation is
durably recorded. No real model, no memory, no retrieval — those arrive in later milestones.

## Milestone 2

Real models can be bound (any OpenAI-compatible endpoint), and Apollo's behaviour under a model
change is measurable: the persona regression suite, its check engine, run records, `eval diff` and
the two compatibility gates. See [`docs/operations/persona-eval.md`](docs/operations/persona-eval.md).

Gate 1 is mechanical and runs today. **Gate 2 is empirical and needs a real model**: a run against
`brain.fake` exercises the machinery and says nothing about Apollo's behaviour.

## Two database roles

Apollo runs as a least-privilege role that **cannot modify the audit stream**. That is a load-bearing
property (spec H.3, ADR-0004), so the ordinary runtime path uses it rather than a role that happens
to be more convenient.

| Role | Used for | Privileges |
|---|---|---|
| `apollo_owner` | migrations, provisioning, owning schema objects | full on its own objects |
| `apollo_app` | **everything Apollo does at runtime** | `SELECT`/`INSERT`/`UPDATE` on state tables; `SELECT`/`INSERT` only on `audit_event`; no `DELETE` anywhere; not superuser; owns nothing |

Apollo has no delete path — tombstoning removes content with an `UPDATE` (spec D.7) — so `DELETE` is
withheld entirely. An accidental future delete becomes a privilege error rather than data loss.

## Running it

```sh
docker compose -f docker/docker-compose.yml up -d          # postgres only
pip install -e '.[dev]'

# Provisioning uses the owner. Do this once, and again after any upgrade that
# adds tables, so new tables acquire their grants. The admin role needs
# CREATEROLE the first time; if it does not have it (common on managed
# PostgreSQL), `apollo provision` prints the one CREATE ROLE statement a
# superuser must run, after which provisioning applies the grants itself.
export APOLLO_ADMIN_DSN="host=127.0.0.1 port=5432 user=apollo_owner dbname=apollo"
export APOLLO_RUNTIME_PASSWORD="$APOLLO_APP_PASSWORD"
apollo provision

# Everything after this point runs as the unprivileged role. Note there is no
# APOLLO_ADMIN_DSN in the runtime environment.
unset APOLLO_ADMIN_DSN
export APOLLO_DATABASE_DSN="host=127.0.0.1 port=5432 user=apollo_app dbname=apollo"

CONV=$(apollo new)
apollo say "$CONV" "You there?"
apollo show "$CONV"
```

`apollo doctor` reports the runtime role's actual privileges and exits non-zero if least privilege
has been lost. `apollo provision` performs the same check and refuses to finish if the role it
provisioned could modify its own audit stream — it verifies against the catalogue rather than
assuming, because removing `SUPERUSER` itself requires superuser and would fail on exactly the
deployments where the check matters most.

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

The test fixtures provision their throwaway database with the same `provision_runtime_role` that
`apollo provision` calls, and the `db` fixture every test uses is the runtime role — so if the
deployment stops being least-privilege, the tests stop passing.
