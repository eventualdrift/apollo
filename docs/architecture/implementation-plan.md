# Apollo — Phase Zero Implementation Plan

Status: **proposed**, pending authorization to begin implementation.
Depends on: [`phase-zero-spec.md`](./phase-zero-spec.md) · [ADRs](../adr/)

**Seventeen steps in five milestones.** The ordering de-risks in a specific sequence: an end-to-end
recorded turn exists before anything clever; behaviour is measured before memory is added, so persona
work is not entangled with retrieval bugs; and privacy enforcement lands before any hosted model is
reachable.

---

## M1 — Apollo talks, and every turn is recorded

### Step 1. Skeleton
`pyproject.toml`, `docker/docker-compose.yml` (Postgres only), config loading (TOML + env), logging
with the redaction filter, pytest layout, CI running the unit suite.
**Also now:** `tests/architecture/test_imports.py` asserting the §A.3 dependency rules — written
first so it can never be retrofitted around violations that already exist.

### Step 2. Storage, schema and the unit of work
Numbered SQL migrations for all eleven tables, the migration runner, repository modules, and
`storage/unit_of_work.py` — the mechanism making state mutation and audit event commit together
(ADR-0004). Integration tests against real Postgres, including the forced-failure atomicity cases and
the rule that no transaction spans a model call.
*Satisfies: O.1/13.*

### Step 3. Identity
`manifest.yaml` plus `core.md`, `behaviour.md`, `relationship.md`, extracted from the behaviour
contract. Deterministic composition, hashing over composed text, snapshot into `identity_version`.
*Satisfies: O.1/18.*

### Step 4. Context bundle, rules, escaping, estimator
`ContextBundle`/`ContextBlock` types with trust tier and taint. `context/escaping.py` implementing
the §B.4 encode/decode with deliberate-collision and round-trip tests. `CONTEXT_RULES` including the
escaping legend. `conservative-v1` estimator. Budget allocator with floors, caps and drop reasons.
Manifest emission. Determinism tests. Compiler produces the `reply` block set without memory, and the
`memory_proposal` block set.
*Satisfies: O.1/15.*

### Step 5. Brain abstraction and `brain.fake`
`Brain` protocol with `adapter_key` and `render_version`, `ModelCapabilities`, `TokenEstimator`,
registry and alias resolution. `brain.fake` in `echo` and `scripted` modes.

### Step 6. Turn orchestration and invocations — **first end-to-end turn**
Conversations, messages with `seq` and idempotency, the turn lifecycle with the §A.2 transaction
boundaries, `core/invocations.py` as the only place an adapter is invoked, orphan recovery,
`system_note` handling, and `cli/chat.py`. Apollo holds a conversation against `brain.fake` with a
complete turn record, invocation record and manifest for every exchange.
*Satisfies: O.1/1, O.1/7 (reply invocation).*

> **M1:** a talking loop with full auditability and no intelligence. Everything after adds depth to a
> system that already works end to end.

---

## M2 — Real models, and behaviour under measurement

### Step 7. Policy, privacy modes and the eval-only guard
`core/policy.py`: conversation modes, provider `allowed_modes`, `eval_only` registry refusal, the
pre-retrieval check, refusal paths, `policy.refused` audit. The interactive API restricted to
`personal` conversations.
**Before any hosted model is configured**, so the control exists before the thing it controls.
*Satisfies: O.1/12 (first two mechanisms).*

### Step 8. OpenAI-compatible adapter
Rendering with system-role rules and fence escaping applied, `render_version` declaration,
generation, capability reporting, reasoning-channel discard, `finish_reason` and token-count
handling, and the §H.5 sanitisation whitelist. Gate 1 harness. Bind `brain.reference` (eval-only) and
`brain.local` against llama.cpp when available.
*Satisfies: O.1/14.*

### Step 9. Persona suite
Runner using the same invocation-recording path, the nine check types, YAML case format, run records
storing responses verbatim, and `evals diff`. Initial corpus: one case per behavioural rule plus the
sixteen probes in the contract's §11. First runs against `fake` and `reference`; recordings captured
for `brain.fake` replay.
*Satisfies: O.1/6, O.1/21.*

> **M2:** behaviour is measurable and comparable across brains before memory complicates the picture.
> Persona failures here are persona failures, not retrieval bugs.

---

## M3 — Apollo remembers

### Step 10. Memory core
The lifecycle: create, confirm, contradict, correct/supersede, archive, restore, tombstone. Derived
support and confidence. Direct-entry API and `cli/memory.py`. Invariant tests — single active per
chain, provenance required, `origin_tier` never mutated, tombstone completeness including the
sentinel sweep across every table and log sink.
*Satisfies: O.1/10, O.1/11.*

### Step 11. Retrieval and its eval
Strategies (pinned, lexical, recency), merge and ranking, `origin`/mode scoping, and the
**substantive-match rule**. Fixtures covering all nine required case types, runner, metrics including
`correct_empty_rate`. `MEMORY` and `RETRIEVAL_NOTICE` blocks wired into the compiler.
**Run the suite under both `english` and `simple` text-search configurations and keep the better one,
recording the result** (§E.4).
*Satisfies: O.1/2, O.1/3, O.1/4, O.1/12 (third mechanism), O.1/16.*

### Step 12. Memory proposals
Deterministic intent detection, the `memory_proposal` invocation with its minimal bundle, proposal
creation, the Save / Edit and Save / Ignore surface, expiry, and the invariants — proposals never
retrieved, no code path from generation to memory creation, `self` scope rejected.
*Satisfies: O.1/7 (both invocations), O.1/8, O.1/9.*

> **M3:** Apollo remembers what he was asked to remember, retrieval is measured, and absence is
> reported honestly.

---

## M4 — Honest under failure, auditable end to end

### Step 13. Replay
`core/replay.py` and `apollo turn replay <id>`: rebuild from manifest and source rows, verify the
bundle hash, re-render by `adapter_key` + `render_version`, verify the rendered prompt hash. Both
status axes, including `SOURCE_REDACTED` against a tombstoned source and `RENDERER_UNAVAILABLE`
against a retired render version.
*Satisfies: O.1/5.*

### Step 14. Failure behaviour and redaction
Every row of spec §L implemented and tested: unavailable brain, single same-brain retry, mode and
eval-only refusals, database loss, retrieval failure vs. emptiness, empty and truncated generations,
context overflow, proposal-structuring failure not failing the turn, crash recovery, idempotent
resubmission. The sentinel log-redaction test and the simulated-provider-error sanitisation test.
*Satisfies: O.1/17, O.1/19.*

### Step 15. Backup and restore
`pg_dump` procedure, documented restore, and **one executed restore into a scratch database with the
result verified** — with the dump either confined to encrypted storage and destroyed or encrypted
before retention. Retention window configured.
*Satisfies: O.1/22.*

> **M4:** every technical acceptance criterion in §O.1 passes.

---

## M5 — The gates

### Step 16. Acceptance run
Full technical gate executed and recorded. Gate 1 and gate 2 run for whichever brain is bound to
`brain.default`; waivers dated and reasoned.

### Step 17. Product trial
Several days of ordinary use, answering §O.2. Capture rate and conversations-per-day recorded as
supporting evidence.

> **M5:** phase zero is complete, or it has failed as a product experiment and the response is to fix
> the experience — not to proceed to connectors on a green test suite.

---

## Ordering constraints worth stating

- **The import test is written first** (step 1), not retrofitted.
- **The unit of work precedes anything that writes** (step 2). Atomicity added later is atomicity
  that was absent for a while.
- **Escaping ships with the bundle** (step 4), not with the adapter. A fence without escaping is a
  fence that does not hold.
- **Policy precedes the hosted adapter** (step 7 before 8). A privacy control added after the thing
  it controls is a control that was absent for a while.
- **Persona measurement precedes memory** (step 9 before 10), so the first persona failures are
  diagnosable rather than tangled with retrieval behaviour.
- **The retrieval eval ships with retrieval** (step 11). Retrieval tuned without a fixture is guessing.
- **Replay comes after memory** (step 13), because reconstruction and its deletion semantics are only
  meaningfully tested once memory rows are among the sources.

## What is explicitly not in this plan

Any work listed in spec §0.2. If a step appears to require one of those, the step is wrong.
