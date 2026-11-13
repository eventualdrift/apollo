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

## Milestone 3 (built; not yet run on a real model)

Apollo remembers what he was asked to remember. A memory is created only on Janu's authority
(`apollo memory add`, below), and every reply searches them:

- **What matches reaches the model as data, with its provenance.** Each memory is one fenced
  block whose header says where it came from, how well attested it is, how old the record is and
  how it was found. Memories are global; conversation history stays in its conversation.
- **Every turn states what was searched**, whether or not anything matched. When nothing did, the
  model is told so in as many words, and recent but unrelated memories are labelled as only
  recent, so they cannot pass for a match.
- **A search that fails is stated as failed**, never as "nothing found". The turn continues.
- What each turn searched, found and actually used is recorded with the turn.

Retrieval is measured by its own suite (`evals/retrieval/`), with no model, on every commit. Its
recorded sweep chose the `english` text-search configuration and the match threshold.

When a message asks Apollo to remember something, he offers a **proposal**: the claim as he
would store it, for Janu to save, edit or ignore (see "Asking Apollo to remember" below). A
proposal is not a memory, and nothing Apollo generates can turn one into a memory.

What milestone 3 still lacks is a run against a real model: spec J.5 requires the persona suite
for the compiler change that put memory in the reply, and no proposal has been structured by a
real model yet. Everything above is proven on `brain.fake`, which shows the machinery and nothing
about a model's behaviour.

## Milestone 4 (in progress)

Honest under failure, auditable end to end. So far:

- **Replay.** Apollo stores no prompt, and can still rebuild what any model call was sent and
  prove it (see "What a turn was sent" below).
- **Failure behaviour.** Every row of the specification's failure table is implemented and
  tested (see "When something fails" below), and no log line or stored error field carries
  message, memory or provider text.

Still to come: an executed and verified backup and restore.

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

Statement logging (`log_statement`, `log_min_duration_statement`) writes a statement's text, and
its bind values on a separate `DETAIL: parameters:` line, which `terse` also drops. So with the
two settings above, statement logging shows placeholders only. That holds because Apollo sends
message, reply and memory text only as bind parameters, never inside a statement; a test
records every statement a turn and the memory commands send, and fails if any contains the
text. What `terse` can't cover is an extension that logs parameters itself, such as `pgaudit`
with `pgaudit.log_parameter`, or `auto_explain`.

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

### Asking Apollo to remember

```sh
apollo say <conversation> "Remember that I take my coffee black."
apollo say <conversation> "I take my coffee black." --remember    # whatever the wording
apollo proposal list            # what is waiting; --status saved | ignored | expired | all
apollo proposal save <id>       # save it as a memory, as proposed
apollo proposal edit <id>       # change it, then save; --scope and --kind change those
apollo proposal ignore <id>     # decline it; nothing is saved
apollo proposal stats           # how many were saved, edited, ignored or left to expire
```

Apollo offers a proposal only when the message says so in one of a fixed list of phrases
("remember that", "keep in mind", "from now on", "note that", "don't forget", "for future
reference" and a few more), or when you ask with `--remember` (in `apollo chat`, start the line
with `/remember`). The list is code, not a model's judgement, and ordinary conversation is never
scanned for things to remember.

- **The reply comes first, and stands whatever happens to the proposal.** One more recorded model
  call then structures the claim from that one message, with no identity and no memory in its
  context. If that call fails or its answer is not a usable claim, the reply is unaffected and a
  note in the transcript says nothing was saved.
- **A proposal is not a memory.** It is never searched, never shown to the model and never stated
  by Apollo as something he knows. Only `save` or `edit` makes a memory, and the database refuses
  a memory that claims an approval no saved proposal backs.
- **A saved proposal is your assertion**, recorded as one a model structured, and it points at the
  message it came from. It cannot be about Apollo himself; that is `apollo memory add --scope self`
  only.
- **Its text does not outlive the decision.** Saving, editing, ignoring and expiry each clear the
  proposal's subject and content in the same transaction. What stays is its status, scope, kind,
  timestamps and references. After an edit, nothing of the model's wording is kept.
- **Left alone, it expires** after `proposal_expiry_days` (`apollo.toml`, default 7) and cannot be
  revived. Until then it holds the claim as the model worded it.

In `apollo chat` at a terminal the proposal is asked about at once; pressing enter leaves it
pending. An edit's subject and content are read from stdin like a memory's, never from an
argument. At most one proposal is made per message.

On `brain.fake` the proposal is the message's own words under a fixed scope and kind. How well a
real model structures a claim is not something these tests show.

### Deleting a memory

`apollo memory forget <id>` tombstones the memory and every version of it (naming any version
forgets the whole correction chain), in one transaction:

- the subject and content of every version are removed, and the search index derived from them
  with them;
- every observation excerpt quoting it is removed;
- the verification hashes of every recorded model call whose context included any version are
  redacted, since a hash of known text would confirm a guess at it;
- a proposal still waiting that Apollo made from a message the memory cites is declined, and its
  text cleared: it was his own wording of the same claim. (A proposal you saved had already
  given up its text when you saved it.)
- one `memory.tombstoned` audit event per version records that it happened, with ids and counts
  only.

The row itself stays, as `tombstoned`, with its classification, provenance and timestamps: this
is **logical deletion, not physical erasure**. What it does not reach:

- **The source.** The message where you said it keeps your words. Messages are write-once, and
  phase zero has no message deletion. In the conversation where you said it, Apollo still sees
  that message in its history on later turns (while it fits in the context), so the fact stays in
  context there; a new conversation won't have it.
- **Apollo's replies.** A reply that repeated the claim keeps it.
- **Retrieval queries.** The text of a query Apollo searched memory with is recorded
  with the turn, and can contain the claim.
- **PostgreSQL below the rows.** Old row versions until vacuum reclaims them, the write-ahead log,
  replicas, and any dump or backup taken before the tombstone. Nothing overwrites them.
- **A proposal still waiting from some other message.** A pending proposal holds its own copy of
  a claim, in the model's wording. If the memory was not made from that proposal's message,
  Apollo has no way to know the two are the same claim, and leaves the proposal alone.
  `apollo proposal ignore <id>` clears it, and unanswered it expires on its own.
- **Outside Apollo.** The server log (see "PostgreSQL's own log" above), your terminal and shell
  history, and any file you piped the text from.

A test makes a memory from a message, has two real turns retrieve it, tombstones it, and then
looks for the claim in every text, json, `tsvector` and array column of every table, the logs,
the CLI's output, a persona run record and a reply rendered afterwards. It finds the claim only
in the source message and in the query recorded for that message's own turn, and it finds the
hashes of both turns that had used the memory redacted.

## What a turn was sent

```sh
apollo turn list <conversation>        # a conversation's turns, with their ids
apollo turn replay <turn>              # rebuild each model call of the turn and verify it
apollo turn replay <turn> --context    # also print each rebuilt request
```

Apollo does not store the prompts he sends. Each model call records a manifest instead: what
each block of its context was and which row its text came from, the versions that built and
rendered it, and two hashes. `apollo turn replay` reads those rows, rebuilds the context, renders
it again, and compares both hashes. It reports two statuses per call:

| `bundle` | |
|---|---|
| `VERIFIED` | Every source is present and the rebuilt context matches the recorded hash. |
| `MISMATCH` | Every source is present and the hash differs. Something that should never change has changed: a bug worth knowing about. |
| `SOURCE_REDACTED` | A memory the call used has been forgotten. Replay stops and names it. |
| `SOURCE_MISSING` | A source is absent and was not forgotten. Each is named with the reason. |

| `render` | |
|---|---|
| `VERIFIED` | The rebuilt request matches what was sent. |
| `MISMATCH` | It was rebuilt, and differs. |
| `RENDERER_UNAVAILABLE` | This version of Apollo no longer has the renderer that call used. |
| `NOT_ATTEMPTED` | The context did not verify, or the call never completed and so recorded no hash. |

- **Forgetting wins over replay.** A turn that used a forgotten memory can no longer be rebuilt,
  and Apollo does not keep the text so that it could be. The memory is never rebuilt from
  anything else that happens to say the same, such as the message you first said it in.
- **The message itself is not a memory.** The turn in which you said something replays in full
  after you forget the memory made from it, because the message is still there (see "Deleting a
  memory").
- **A failed call replays too.** Its context was built before the call, so `--context` shows what
  it would have sent. That is what makes a failure and its retry comparable.
- **Correcting, confirming, contradicting or archiving a memory changes no earlier turn.** The
  corrected row keeps its text, and a memory's provenance as it was shown is in the manifest.
- **Each call is replayed with the versions it recorded**, not today's: `compiler-v1` and
  `compiler-v2`, and the `chat-v1`, `chat-v2` and `chat-v3` renderers. A provider configured
  without a system role records its own version (`chat-v3-nosystem`), since the request it is
  sent is different.
- **Eval turns do not replay from the database.** A persona case's text lives in its case file,
  so its sources are reported as `not_a_database_row`. `apollo eval gate2` rebuilds eval contexts
  from the run record instead.

`replay` exits 1 when a call has a `MISMATCH` or a `SOURCE_MISSING`, and 0 otherwise. It only
reads: it writes no row and no audit event. `--context` prints conversation and memory text to
your terminal; nothing else does, and none of it is logged.

## When something fails

A failure is always the same shape of thing: a recorded fact, an honest note in the transcript,
no made-up reply, and no quiet substitute. `apollo show` displays the notes; the model never sees
them.

| What fails | What Apollo does |
|---|---|
| The model server is unreachable, or times out | One retry against the same model, recorded as a second call. If that fails too, the turn fails with a note. No reply is invented and no other model is tried. |
| The model is not permitted for this conversation | Refused before anything is searched or sent. |
| The database is unavailable | The command fails. Nothing is answered from memory: a turn that was not recorded did not happen. |
| The memory search fails | The turn continues, and the model is told the search failed. It is never told "nothing was found". |
| The model returns nothing | The turn fails. No empty reply is stored. |
| The model stops at its output limit | The reply is kept and shown as cut off, in `say`, `chat` and `show`. |
| The reply is not what was asked for | Never retried. Only an unreachable server is retried, once. |
| The conversation no longer fits the context | The turn fails before any call. What was assembled is recorded, so `apollo turn replay` shows what did not fit. Identity is never trimmed to make room. |
| A proposal cannot be made | The reply stands, and a note says nothing was saved. |
| Apollo is killed mid-turn | Your message and the turn are already recorded. The next start marks the turn interrupted, with a note. |
| You press Ctrl-C during a reply | The turn is recorded as interrupted, at once. |
| The same request arrives twice | The first answer is returned. Nothing is generated again. |

A command that cannot run prints the kind of failure and nothing else, because an error's text can
quote a row or a connection string. A configuration problem is the exception: it names the
missing variable or key.

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
