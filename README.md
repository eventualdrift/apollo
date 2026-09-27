# Apollo

A private, persistent personal AI. Phase zero.

Architecture is frozen: see [`docs/architecture/phase-zero-spec.md`](docs/architecture/phase-zero-spec.md)
and the [ADRs](docs/adr/). The specification wins over the code; if they disagree, the code is wrong.
Current implementation continuity is recorded in [`docs/PROJECT_STATE.md`](docs/PROJECT_STATE.md).

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
| `apollo_app` | **everything Apollo does at runtime** | `SELECT`/`INSERT`/`UPDATE` on state tables; `SELECT`/`INSERT` only on `audit_event`; no `DELETE` anywhere; no temporary tables; not superuser; owns nothing |

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
# The runtime password is read from stdin (a no-echo prompt at a terminal) and
# hashed client-side into a SCRAM-SHA-256 verifier; the plaintext never appears
# in a command line, the SQL sent or the server log. Omit --password-stdin for
# trust/peer auth.
export APOLLO_ADMIN_DSN="host=127.0.0.1 port=5432 user=apollo_owner dbname=apollo"
printf '%s\n' "$APOLLO_APP_PASSWORD" | apollo provision --password-stdin

# Once per database, as a PostgreSQL superuser: keep refused rows out of the
# server log (see "PostgreSQL's own log" below). `apollo provision` prints these
# statements while the settings are not in effect, and `apollo doctor` reports them.
psql -d apollo -c "ALTER DATABASE apollo SET log_error_verbosity = 'terse'"
psql -d apollo -c "ALTER DATABASE apollo SET log_min_error_statement = 'panic'"

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

Provisioning also revokes `TEMPORARY` on the Apollo database from `PUBLIC`, through which every role
holds it by default. A session-private table named like an Apollo table is what an unpinned
`search_path` would resolve first; the lifecycle triggers pin theirs, and with no temporary tables
there is nothing left to shadow them.

### PostgreSQL's own log

When PostgreSQL refuses a row, its error can quote the row (`DETAIL: Failing row contains (...)`),
and with the default `log_min_error_statement = error` it also logs the statement that failed.
Apollo never prints or logs an exception's text, but the server writes its own log, which a
tombstone cannot reach. `log_error_verbosity = terse` drops the `DETAIL` line, and
`log_min_error_statement = panic` stops failed statements being logged. Both are superuser
settings, so provisioning can't set them itself. Set them per database; a setting on a role
(`ALTER ROLE ... SET`) overrides the database's for that role's sessions. So `apollo provision`
and `apollo doctor` check them as the runtime role, through `APOLLO_DATABASE_DSN`, and while
they are not in effect print the statements that fix them, including `ALTER ROLE ... RESET` for
an override on the runtime role. If they can't connect as the runtime role, they say so, and
`apollo provision` prints the statements that make both settings certain.

## Memories

```sh
apollo memory add --scope user --kind fact     # prompts for the subject, then the content
apollo memory add --scope user --kind fact < claim.txt   # first line subject, the rest content
apollo memory correct <id>      # prompts for the new content (or reads it from stdin)
apollo memory confirm <id>      # also: contradict, archive, restore, show
apollo memory list              # --status archived | superseded | tombstoned | all
apollo memory forget <id>       # the memory and every version of it; --yes when piped
```

A memory's subject and content are read from stdin, prompted at a terminal or piped (for `add`,
the first line is the subject and the rest the content). No argument takes them, so they stay
out of shell history and the process list. A pipe is only as private as its source: a file
stays on disk, and an `echo` or `printf` in your shell puts the text in its history. Errors
print as their kind only: a database error's message can quote the row it refused.

### Deleting a memory

`apollo memory forget <id>` tombstones the memory and every version of it (naming any version
forgets the whole correction chain), in one transaction:

- the subject and content of every version are removed, and the search index derived from them
  with them;
- every observation excerpt quoting it is removed;
- the verification hashes of every recorded model call whose context included any version are
  redacted, since a hash of known text would confirm a guess at it;
- one `memory.tombstoned` audit event per version records that it happened, with ids and counts
  only.

The row itself stays, as `tombstoned`, with its classification, provenance and timestamps: this
is **logical deletion, not physical erasure**. What it does not reach:

- **The source.** The message where you said it keeps your words. Messages are write-once, and
  phase zero has no message deletion. In the conversation where you said it, Apollo still sees
  that message in its history on later turns (while it fits in the context), so the fact stays in
  context there; a new conversation won't have it.
- **Apollo's replies.** A reply that repeated the claim keeps it.
- **Retrieval queries.** From step 11, the text of a query Apollo searched memory with is recorded
  with the turn, and can contain the claim.
- **PostgreSQL below the rows.** Old row versions until vacuum reclaims them, the write-ahead log,
  replicas, and any dump or backup taken before the tombstone. Nothing overwrites them.
- **Outside Apollo.** The server log (see "PostgreSQL's own log" above), your terminal and shell
  history, and any file you piped the text from.

A test tombstones a memory made from a message and then looks for the claim in every text,
json, `tsvector` and array column of every table, the logs, the CLI's output, a persona run
record and a reply rendered afterwards: only the source message still holds it.

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
