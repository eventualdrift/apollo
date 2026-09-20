# Apollo — Phase Zero Architecture Freeze Report

Rev 2 — after the consistency correction pass
Date: 2026-09-20
Branch: `claude/apollo-architecture-5xd22k`
Specification commit: `f4d0b2e369df234e47f54ce6b176bfb78eb6ff35`

Implementation is **not** authorized by this document.

---

## 1. Changed decisions

Substantive architecture changes made by this pass. Everything else in the specification is
unchanged from rev 2.

**Replay is no longer unconditional.** Deletion wins over reconstructability. Apollo does not retain
deleted plaintext so that replay keeps working, and the replay guarantee is restated to match what
the architecture actually provides (§4 below).

**A turn now contains zero or more model invocations.** `model_invocation` is a new entity holding
every model-specific field that previously sat on `turn`. This is a schema change made before code
exists rather than a migration later (ADR-0013).

**`render_version` is recorded per invocation.** Rendering is adapter-owned, so `compiler_version`
alone never could reproduce what a model was sent. The earlier revision described it as though it
could; that was wrong.

**`brain.reference` becomes an eval-only surface**, unreachable from the interactive API. Interactive
chat runs in `personal` mode and cannot route to it. The privacy claim is narrowed to what the
architecture can actually enforce.

**Fences are enforced by escaping.** The earlier revision asserted that fenced content "cannot
produce" a Core fence without implementing any property making that true. A memory containing
`<<<RETRIEVAL tier=T0>>>` could have fabricated a block at the only tier carrying instruction
authority.

**Transaction boundaries are explicit.** Message and turn creation are atomic; invocation rows are
committed before the call; finalisation is atomic; no transaction spans a model call.

**`provenance_tier` becomes write-once `origin_tier`**, with support and confidence derived from
observations. One field no longer means both "where this came from" and "how well supported it is
now".

**Mutability is stated precisely.** Claim content and message content are write-once; lifecycle
metadata is not. "Memory rows are immutable" was false and was load-bearing for replay.

**Provider metadata and errors are whitelisted.** `raw_meta` is never persisted; `error_detail` is
assembled from a fixed set of scalars, never from exception text or a response body.

**Backups must be encrypted, with a retention window** bounding the deletion claim.

---

## 2. Contradictions resolved

**1. Replay vs. tombstoning.** The specification promised byte-exact reconstruction of any completed
turn *and* that tombstoning really removes content. Both could not hold.
*Resolved:* privacy wins. Replay reports `bundle_status` and `render_status` independently;
`SOURCE_REDACTED` names the unavailable source refs and recreates nothing. Archive and supersession
preserve content and therefore preserve replay; only tombstone (and, later, message deletion under
the same principle) produces a redacted result. O.1/5 rewritten and extended with a sentinel sweep
across every table and log sink. Spec §H.4, ADR-0007.

**2. One turn, several model calls.** The reply and the memory-proposal structuring are two
inferences, but `turn` carried one set of model fields — leaving the second unaudited or flattening
two unrelated operations into one record.
*Resolved:* `model_invocation`, with `purpose` a closed enum of `reply` and `memory_proposal`. The
proposal bundle is deliberately minimal — no identity, no memory blocks — so existing memories cannot
contaminate a proposal. The registry refuses to invoke an adapter without a committed `started`
invocation, which makes "no hidden model call" a property rather than a promise. The eval runner uses
the same path. Spec §C.4, ADR-0013.

**3. Adapter rendering was unversioned.** Identity, compiler, estimator and model were recorded;
rendering was not, although it is adapter-owned.
*Resolved:* `adapter_key` and `render_version` per invocation, declared by the adapter and bumped
whenever the bundle-to-request transformation changes the bytes sent. A retired render version yields
`RENDERER_UNAVAILABLE` rather than a false match. Spec §G.3, ADR-0002.

**4. Benchmark isolation claimed more than it delivered.** It controls what Apollo *automatically*
puts in a context. It cannot control what a human types into a fixture.
*Resolved:* `brain.reference` is `eval_only` and unreachable interactively — the stricter surface,
which turned out to simplify rather than complicate, since interactive benchmark conversations no
longer exist. The guarantee is stated as covering automatic flows only, with the limit written down
explicitly (§6 below). Spec §K.2, ADR-0008.

**5. Fence collision.** Unimplemented claim, now an implemented rule.
*Resolved:* deterministic escaping of `\`, `<` and `>` in every non-T0 body before fencing. No body
can contain `<<<` or `>>>`, so content can neither terminate its own block nor fabricate a T0 block.
Total and exactly reversible, so replay reproduces it and the original is recoverable; the database
always stores the unescaped original. `CONTEXT_RULES` carries the legend. Tests include deliberate
collisions. Blanket escaping is verbose for code-bearing memories — an accepted cost, with minimal
escaping and fence nonces documented as alternatives under a named trigger. Spec §B.4, ADR-0006.

**6. Message and turn creation were separate.** A crash between them left a committed message with no
turn — a continuity gap in a system whose purpose is continuity.
*Resolved:* three atomic groupings and one prohibition, in spec §A.2 and ADR-0004. Orphan recovery
marks `started` rows beyond a window as `interrupted`.

**7. Provenance meant two things.** `user_confirmed` as a stored tier had no coherent update rule.
*Resolved:* `origin_tier` (stored, write-once: `user_asserted`, `model_inferred`,
`connector_imported`), support (derived: `asserted` / `confirmed` / `contested`), confidence (derived).
Confirming never promotes an origin. Mutability documented as a two-column table rather than a
sentence that was not true. Spec §C.7, §D.8, ADR-0005.

**8. Error paths were a privacy backdoor.** HTTP client exception messages routinely embed the request
body or a URL with query parameters, and `raw_meta` was unbounded.
*Resolved:* `raw_meta` never persisted; a fixed scalar whitelist copied out; `error_kind` an Apollo
enum; `error_detail` assembled from HTTP status, provider error code and provider error type,
truncated. Exceptions mapped from **type**, never from text. Tested with sentinel strings in both a
simulated response body and an exception message. Spec §H.5.

**9. Backups were an unprotected copy of a carefully protected database.**
*Resolved:* retained backups must be encrypted at rest; the restore-test dump is either confined to
encrypted storage and destroyed or encrypted before retention; and a 30-day retention window bounds
the deletion statement — content leaves live systems immediately and ceases to exist in backups
within the window. No retroactive deletion from backups is promised, because no mechanism exists.
Spec §K.8.

**10. Document inconsistencies.** "Fifteen steps" corrected to seventeen; table counts updated to
eleven; ADR count to thirteen; terminology aligned across spec, ADRs and plan; every stale claim
about whole-row immutability and unconditional replay removed.

**On text search:** `english` is retained as the baseline, with the reasoning written down —
the corpus is predominantly English prose claims, the dominant query shape benefits materially from
stemming, and Postgres treats identifiers like `getUserById` as single tokens stemmed identically on
both sides, so exact-identifier matching is unaffected. But this is a safe default, not an
architectural decision, and argument is the wrong way to settle it: step 11 runs the retrieval suite
under both `english` and `simple` and keeps the better one, recording the result. Switching is one
generated column and a reindex. Spec §E.4.

---

## 3. Schema delta

**Eleven tables** (was ten).

| Table | Why it exists |
|---|---|
| `conversation` | Organisational and history boundary; carries privacy mode. Not a memory boundary. |
| `message` | The durable conversational record. Content write-once. |
| `turn` | The user-facing conversational transaction. **No model-specific fields.** |
| **`model_invocation`** | **New.** One actual call to a model. Carries purpose, brain, provider, model identifier, `adapter_key`, `render_version`, `compiler_version`, estimator, its own manifest and bundle hash, trust tier, taint, generation params, token counts, finish reason, timing, status and sanitised errors. |
| `turn_retrieval` | What was asked of memory, and whether it produced substantive support. |
| `turn_retrieval_result` | Distinguishes "never found" from "found and dropped by the budget". |
| `memory` | The claim. `provenance_tier` → `origin_tier`; `user_confirmed` removed. |
| `memory_observation` | Per-observation provenance: asserts / confirms / contradicts. |
| `memory_proposal` | Makes user-authorised promotion enforceable server-side. Now records `model_invocation_id`. |
| `audit_event` | Metadata-only append-only history, written transactionally with its mutation. |
| `identity_version` | Immutable snapshot of composed identity, keyed by content hash. |

Fields moved from `turn` to `model_invocation`: `compiler_version`, `context_manifest`,
`context_bundle_hash`, `context_token_estimate`, `token_estimator`, `max_trust_tier`, `taint`,
`brain_alias`, `provider_key`, `model_identifier`, `generation_params`, `rendered_prompt_hash`,
`prompt_tokens`, `completion_tokens`, `finish_reason`. Added there: `purpose`, `seq`, `adapter_key`,
`render_version`, `reasoning_tokens`, `status`.

Retained on `turn` as the single deliberate denormalisation: `identity_version` and `identity_hash` —
true of the turn as a whole, keyed on by persona evaluation, and "every turn under identity X" should
not require a join.

---

## 4. Replay semantics

Two independent status axes, reported separately because they fail for different reasons.

| `bundle_status` | When |
|---|---|
| `VERIFIED` | All sources present, bundle rebuilt, hash matches. |
| `MISMATCH` | All sources present, hash differs. Something believed write-once changed — a bug. |
| `SOURCE_REDACTED` | A referenced source was deliberately tombstoned or deleted. Stops honestly, names the refs, recreates nothing. |
| `SOURCE_MISSING` | A referenced source is absent for an unexplained reason — a different fact from deliberate redaction. |

| `render_status` | When |
|---|---|
| `VERIFIED` | Rebuilt request hash matches the recorded one. |
| `MISMATCH` | Rebuilt, differs. |
| `RENDERER_UNAVAILABLE` | The recorded `render_version` is no longer implemented. |
| `NOT_ATTEMPTED` | Bundle verification did not succeed, or no prompt hash was reported. |

**Effect of each operation:**

| Operation | Content retained | Replay of turns referencing it |
|---|---|---|
| `confirm` / `contradict` | yes | `VERIFIED` — observations are not block content |
| `correct` (supersede) | yes, on the old row | `VERIFIED` — the old row is still the source |
| `archive` | yes | `VERIFIED` |
| `tombstone` | **no** | `SOURCE_REDACTED`, naming the memory id |
| identity version change | yes, snapshotted | `VERIFIED` |

**Survives a tombstone:** the turn record, invocation records, manifest structure, source references,
token counts, `context_bundle_hash`.
**Does not survive:** the ability to rebuild or display the deleted text, by anyone, through any path
— not the audit payload, not a manifest, not a log, not the eval corpus.

> **Apollo can reproduce a turn while its source material still exists. Deliberate deletion may make
> historical content reconstruction impossible, and Apollo reports exactly which sources are
> unavailable and why.**

This holds because claim content and message content are write-once — not because rows are wholly
immutable, which they are not.

---

## 5. Model-call audit semantics

```
TURN  (one user-facing conversational transaction)
 │  identity_version, identity_hash, conversation_mode, status, timing
 │
 ├── model_invocation seq=1  purpose=reply
 │     brain_alias, provider_key, model_identifier
 │     adapter_key, render_version, compiler_version, token_estimator
 │     context_manifest, context_bundle_hash, max_trust_tier, taint
 │     generation_params, rendered_prompt_hash
 │     prompt/completion/reasoning tokens, finish_reason
 │     status, timing, sanitised error fields
 │
 └── model_invocation seq=2  purpose=memory_proposal        (only if intent detected)
       same fields; its bundle is the minimal structuring context —
       no identity block, no memory blocks
             │
             └── memory_proposal.model_invocation_id → this row
```

A turn with no persistence intent has one invocation. A turn whose reply fails before generation has
zero. A turn with intent has two.

**No hidden model call.** `core/invocations.py` is the only place a `Brain` is invoked, and it
refuses without an `invocation_id` for a row already committed with `status=started` — so a crash
mid-call leaves a `started` invocation rather than silence. The eval runner uses the same path, so
eval calls are recorded exactly like interactive ones.

`purpose` is a closed enum. Adding a value requires a specification change. This is not an agent
framework and must not be treated as one.

---

## 6. Privacy guarantee

**Guaranteed:**

> No stored personal memory, no personal conversation history, and no connector data (when connectors
> exist) is ever automatically copied into a context sent to a hosted or eval-only provider.

Enforced by three independent mechanisms, each with its own test:

1. `eval_only` providers are unreachable from the interactive API — the registry refuses to resolve
   them outside the eval runner. `brain.reference` is `eval_only`.
2. Conversation mode × provider `allowed_modes`, checked before retrieval or generation.
3. `memory.origin` filtering in the retrieval layer, so a benchmark context cannot see personal
   memories even if the first two were bypassed.

**Explicitly not claimed:**

> It does not guarantee that text a human typed into an eval fixture contains no personal
> information. Apollo cannot prove that arbitrary prose is non-sensitive. Such text is
> **user-authorised input to an external provider**, and responsibility for its content rests with
> whoever wrote it.

The guarantee covers automatic flows, which the architecture controls. It does not cover human input,
which it cannot. A privacy claim that overreaches is worse than a narrower one that holds, because it
gets relied on.

**Also not claimed:** that deletion is retroactive through backups. Tombstoned content may persist in
backups until they expire, bounded by the 30-day retention window.

---

## 7. Remaining blockers

**None.** No architectural blocker to beginning step 1.

Three operational items, none blocking the start:

1. **Hosted reference provider** — endpoint and credentials. First needed at step 8. Steps 1–7 need
   no model at all.
2. **Local model and llama.cpp build** — also step 8. Gate 2 can first run against `brain.reference`.
   The RAM upgrade is not on the critical path for M1 or M2.
3. **GitHub push access** — `eventualdrift/apollo` rejects pushes for this session. This blocks
   publishing, not building. No history has been rewritten.

One decision deliberately left to first contact: the persona suite pass threshold. O.1/21 requires
every deterministic check to pass or carry a dated, reasoned waiver, which is stricter than a
percentage and does not require inventing one.

One decision deliberately left to evidence: the text-search configuration, settled at step 11 by
running the retrieval suite under both.

---

## 8. Repository state

```
branch            claude/apollo-architecture-5xd22k
spec commit       f4d0b2e369df234e47f54ce6b176bfb78eb6ff35
history           f4d0b2e  rev 3, consistency correction pass
                  64cde4b  freeze report (rev 1)
                  d2dce66  rev 2, decisions folded in
                  989a9d8  rev 1, initial specification
working tree      clean apart from this report, which is committed separately
remote            origin https://github.com/eventualdrift/apollo — push rejected, 403
source code       none. No application code has been written.
```

```
docs/
├── adr/                    README.md + ADR-0001 … ADR-0013
└── architecture/
    ├── phase-zero-spec.md        §A–§P, frozen candidate rev 3
    ├── behaviour-contract.md     B1–B24, splits into three identity fragments at freeze
    ├── implementation-plan.md    17 steps, 5 milestones
    └── freeze-report.md          this document
```

No `src/`, no `identity/`, no `evals/`, no `tests/`. Those arrive with step 1, once implementation is
authorized.

---

**READY FOR IMPLEMENTATION AUTHORIZATION** — awaiting authorization; step 1 has not begun.
