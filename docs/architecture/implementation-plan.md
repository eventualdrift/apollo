# Apollo — Phase Zero Implementation Plan

Status: **proposed**, pending authorization to begin implementation.
Depends on: [`phase-zero-spec.md`](./phase-zero-spec.md) · [ADRs](../adr/)

Fifteen steps in five milestones. The ordering is chosen to de-risk in a specific sequence: an
end-to-end recorded turn exists before anything clever; behaviour is measured before memory is added,
so persona work is not entangled with retrieval bugs; and privacy enforcement lands before any
hosted model is ever called.

---

## M1 — Apollo talks, and every turn is recorded

### Step 1. Skeleton
`pyproject.toml`, `docker/docker-compose.yml` (Postgres only), config loading (TOML + env), logging
setup with the redaction filter, pytest layout, CI running the unit suite.
**Also now:** `tests/architecture/test_imports.py` asserting the §A.3 dependency rules. It is written
first so it can never be retrofitted around violations that already exist.

### Step 2. Storage and schema
Numbered SQL migrations for all ten tables, the migration runner, repository modules, and the
**unit of work** that makes state mutation and audit event commit in one transaction (ADR-0004).
Integration tests against a real Postgres.
*Satisfies: O.1/12.*

### Step 3. Identity
`manifest.yaml` plus `core.md`, `behaviour.md`, `relationship.md`, extracted from the behaviour
contract. Deterministic composition, hashing over composed text, snapshot into `identity_version`.
*Satisfies: O.1/15 (cap failure).*

### Step 4. Context bundle, rules, estimator
`ContextBundle`/`ContextBlock` types with trust tier and taint. `CONTEXT_RULES` owned by the
compiler. `conservative-v1` estimator. Budget allocator with floors, caps and drop reasons. Manifest
emission. Compiler produces identity + rules + user message + an empty-retrieval notice — no memory
yet. Determinism tests.

### Step 5. Brain abstraction and `brain.fake`
`Brain` protocol, `ModelCapabilities`, `TokenEstimator`, registry and alias resolution.
`brain.fake` in `echo` and `scripted` modes.

### Step 6. Turn orchestrator — **first end-to-end turn**
Conversations, messages with `seq` and idempotency, the turn lifecycle, `system_note` handling, and
`cli/chat.py`. Apollo holds a conversation against `brain.fake` with a complete turn record and
manifest for every exchange.
*Satisfies: O.1/1.*

> **Milestone M1:** a talking loop with full auditability and no intelligence. Everything after this
> adds depth to a system that already works end to end.

---

## M2 — Real models, and behaviour under measurement

### Step 7. Policy and privacy modes
`core/policy.py`: conversation modes, provider `allowed_modes`, the pre-retrieval check, refusal
path, `policy.refused` audit. Benchmark-mode visibility in the client.
**Before any hosted model is configured**, so the control exists before the thing it controls.
*Satisfies: O.1/11 (first mechanism).*

### Step 8. OpenAI-compatible adapter
Rendering (system-role rules, fenced non-T0 blocks), generation, capability reporting, reasoning
channel discard, `finish_reason` and token-count handling. Gate 1 harness (§G.5). Bind
`brain.reference` (benchmark only) and `brain.local` against llama.cpp when available.

### Step 9. Persona suite
Runner, the nine check types, YAML case format, run records storing responses verbatim, and
`evals diff`. Initial case corpus: one per behavioural rule plus the sixteen probes in the contract's
§11. First runs against `fake` and `reference`; recordings captured for `brain.fake` replay mode.
*Satisfies: O.1/6, O.1/18.*

> **Milestone M2:** Apollo's behaviour is measurable and comparable across brains before any
> memory complicates the picture. Persona failures here are persona failures, not retrieval bugs.

---

## M3 — Apollo remembers

### Step 10. Memory core
Tables already exist; now the lifecycle: create, confirm, contradict, correct/supersede, archive,
restore, tombstone. Derived confidence. Direct-entry API and `cli/memory.py`. Invariant tests
(single active per chain, provenance required, tombstone completeness).
*Satisfies: O.1/9, O.1/10.*

### Step 11. Retrieval and its eval
Strategies (pinned, lexical, recency), merge and ranking, the `origin`/mode scoping filter, and the
**substantive-match rule**. Retrieval eval fixtures covering all nine required case types, runner,
metrics including `correct_empty_rate`. Wire `MEMORY` and `RETRIEVAL_NOTICE` blocks into the compiler.
*Satisfies: O.1/2, O.1/3, O.1/4, O.1/11 (second mechanism), O.1/13.*

### Step 12. Memory proposals
Deterministic intent detection, proposal creation, the Save / Edit and Save / Ignore surface,
expiry, and the invariants — proposals never retrieved, no code path from generation to memory
creation, `self` scope rejected.
*Satisfies: O.1/7, O.1/8.*

> **Milestone M3:** Apollo remembers what he was asked to remember, retrieval is measured, and
> absence is reported honestly.

---

## M4 — Honest under failure, auditable end to end

### Step 13. Replay
`apollo turn replay <id>`: reconstruct from manifest and immutable sources, re-render, compare hash.
*Satisfies: O.1/5.*

### Step 14. Failure behaviour and redaction
Every row of spec §L implemented and tested: unavailable brain, single same-brain retry, mode
refusal, database loss, retrieval failure vs. emptiness, empty and truncated generations, context
overflow, proposal-structuring failure, idempotent resubmission. The sentinel log-redaction test.
*Satisfies: O.1/14, O.1/16.*

### Step 15. Backup and restore
`pg_dump` procedure, documented restore, and **one executed restore into a scratch database with the
result verified**.
*Satisfies: O.1/19.*

> **Milestone M4:** every technical acceptance criterion in §O.1 passes.

---

## M5 — The gates

### Step 16. Acceptance run
Full technical gate (§O.1) executed and recorded. Gate 1 and gate 2 run for whichever brain is bound
to `brain.default`; waivers dated and reasoned.

### Step 17. Product trial
Several days of ordinary use, answering §O.2. Capture rate and conversations-per-day recorded as
supporting evidence.

> **Milestone M5:** phase zero is complete, or it has failed as a product experiment and the
> response is to fix the experience — not to proceed to connectors on a green test suite.

---

## Ordering constraints worth stating

- **The import test is written first** (step 1), not retrofitted.
- **Policy precedes the hosted adapter** (step 7 before 8). A privacy control added after the thing
  it controls is a control that was absent for a while.
- **Persona measurement precedes memory** (step 9 before 10). Otherwise the first persona failures
  arrive tangled with retrieval behaviour and neither is diagnosable.
- **Retrieval eval ships with retrieval** (step 11), not after. Retrieval tuned without a fixture is
  guessing.
- **Replay comes after memory** (step 13), because reconstruction is only meaningfully tested once
  memory rows are among the sources.

## What is explicitly not in this plan

Any work listed in spec §0.2. If a step appears to require one of those, the step is wrong.
