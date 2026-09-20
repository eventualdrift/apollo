# Apollo — Phase Zero Architecture Freeze Report

Date: 2026-09-20
Branch: `claude/apollo-architecture-5xd22k`
Specification commit: `d2dce6639c506d90cb05c7d0fff792143ebe91a7`

For review. Implementation is **not** authorized by this document.

---

## 1. Contradictions found and resolved

Nine, surfaced while folding the rev-2 decisions into the specification. Each is a place where two
decisions were individually correct and jointly inconsistent.

**1. Adapter-owned token counting vs. cross-brain comparison.**
Token counting belongs behind the adapter (your §6). But the budget depends on token counts, so the
same conversation budgeted for two brains can drop different blocks — meaning the replaceability
report would compare two different bundles and attribute the difference to the model.
*Resolved:* production turns use the adapter's estimator; **eval runs pin `conservative-v1` for every
brain**, so bundles are byte-identical and behavioural differences are attributable to the model.
`turn.token_estimator` records which applied. Spec §F.5; acceptance criterion O.1/6 now requires
identical bundle hashes across brains.

**2. `CONTEXT_RULES` ownership.**
Rev 1 placed the fence-syntax rules in the T0 identity region. But those rules describe the
*compiler's* output format. A fence-syntax change would have dirtied the identity hash and polluted
every persona diff with a change that has nothing to do with Apollo's character.
*Resolved:* `CONTEXT_RULES` is compiler-owned and versioned with `compiler_version`. Identity says
*treat fenced content as data*; the compiler says *here is what a fence looks like*. This also keeps
your §7 identity fragment list at exactly the three you named. Spec §B.4, §I.1.

**3. Humour "not graded" vs. the inappropriate-humour probe.**
Your §13 asks for a probe on inappropriate humour in serious contexts; the contract said humour is
not graded.
*Resolved:* the contract never asks for humour and never grades it for presence, but **intrusive
levity in a serious context is graded as a failure** (B24). Those are consistent — one is a request
for wit, the other is a request to read the room. Contract §8, §9.

**4. Benchmark mode blocked brains but not data.**
Rev 1 guarded which *brain* a benchmark conversation could use. Your §9 requires benchmark mode to
disable access to personal memories, which is a different mechanism entirely and was missing.
*Resolved:* `memory.origin` (`personal` | `fixture`) filters at the retrieval layer, independent of
the brain check. Two mechanisms, two tests — the guarantee survives either one being wrong. Spec
§E.2, §K.2, ADR-0008; acceptance criterion O.1/11 tests both.

**5. "The model cannot approve its own proposal" was unenforceable.**
Without server-side proposal state, the API receives only "create this memory" and cannot distinguish
content the user typed from content a model produced that a client auto-submitted. The rule would
have rested on client good behaviour.
*Resolved:* `memory_proposal` exists, approval is a separate call naming a pending row, and there is
no code path from generation to memory creation. This is the primary justification for the table;
the other two (proposals surviving the terminal closing, and measuring capture rate) are secondary.
Spec §C.8, §D.3.

**6. Your worked example did not map onto the schema.**
`Scope: relationship / Kind: communication_preference` had nowhere to live: rev 1 had `kind` and a
free-text `subject`, no scope.
*Resolved:* added `memory.scope` (`self | user | relationship | world`), kept `kind` coarse, and let
`subject` carry specificity. Your example becomes
`scope=relationship, kind=preference, subject=communication`. This also seeds the self / user /
relationship separation from your original brief without building an entity model. Spec §C.6.

**7. `self`-scope memories from proposals would be self-model mutation.**
Which you ruled out, but nothing in the flow prevented it.
*Resolved:* the proposal endpoint rejects `scope=self`. Direct entry is the only route. Spec §D.3
rule 5; acceptance criterion O.1/8.

**8. Acceptance criteria referenced the removed `is_benchmark` flag.** Rewritten for modes.

**9. Identity fragment naming.** Rev 1 used numeric prefixes and a fourth context-rules fragment;
both are gone, superseded by manifest ordering and resolution 2.

One further point, not a contradiction but a gap worth naming: **the identity hash now covers the
version label**, because composition includes a header carrying it. Without that, a deliberate
version bump with unchanged fragment content produced the same hash, and `identity_version` is keyed
by hash. Spec §I.1.

---

## 2. Decisions frozen

Everything in specification §A–§O. The load-bearing ones:

**Structure.** Apollo Core owns all durable state; adapters own nothing and cannot read the database;
module dependency rules asserted by test.

**Persistence.** Relational state of record plus an advisory append-only audit stream, written in the
same transaction as the mutation it describes — scoped to database-owned state only, with external
systems explicitly excluded from the rule. Ten tables.

**Memory.** Provenance per observation. Confidence computed from evidence in application code, never
stored, never emitted by a model. Correction by supersession; messages and memory rows immutable.
Contradiction records evidence and does not auto-retract. Four states including tombstone.

**Capture.** Deterministic persistence-intent detection; model structures, user authorises; proposals
are not memories; no path from generation to memory creation; no background extraction.

**Boundaries.** Conversations bound history, not knowledge — memories are global, history is
conversation-local. Six trust tiers, only T0 carrying instruction authority. Taint computed and
recorded from the first commit though no tainted sources exist yet.

**Privacy.** Conversation modes with provider `allowed_modes`, enforced twice and independently.
Hard fail, never a silent downgrade. No cross-provider fallback — for the identity reason and, more
strongly, because fallback would route personal context past the mode control.

**Retrieval.** Lexical, pinned and recency only. Recency never counts as substantive support and can
never suppress the negative retrieval signal. Embeddings gated on retrieval-eval evidence.

**Models.** Aliases, not model names. Rendering and token counting behind the adapter; budget policy
in Core. llama.cpp is a deployment choice with no presence outside one adapter module and config.

**Audit.** Manifest reconstruction rather than stored prompts. No hidden reasoning traces anywhere.
Three distinct streams with content barred from operational logs, asserted by a sentinel test.

**Identity.** Manifest-ordered fragments, hashed over composed text, snapshotted on load.

**Continuity.** State continuity and behavioural continuity named separately, with two compatibility
gates. Apollo is never described as supporting drop-in model replacement.

---

## 3. Deferred, with named triggers

| Deferred | Revisit when |
|---|---|
| Embeddings / semantic retrieval | The retrieval suite shows lexical failing cases embeddings would catch |
| Conversation summarisation | Truncation is demonstrably losing needed context |
| Cross-conversation episodic context | Continuity feels thin in real use — designed deliberately, not emergent |
| Passive memory candidate generation | The explicit path is proven and its precision understood |
| Cryptographic audit chaining | A threat model involving privileged database tampering exists |
| LLM judge in evals | Deterministic checks stop discriminating |
| Entity model | `subject` labels show stable clustering worth normalising |
| Time decay on confidence | Enough corrections exist to calibrate a rate |
| Action policy engine | Any action capability is proposed |
| Device auth, remote access, multi-user | A non-local client exists |
| Full event sourcing | The advisory audit stream proves insufficient — deliberately, not by drift |

Everything in specification §0.2 remains out of scope.

---

## 4. Remaining blockers

**No architectural blockers.** The specification is complete enough to implement against.

Three operational items, none blocking the start:

1. **Hosted reference provider** — endpoint and credentials. Needed at step 8 (M2), not before.
   Until then `brain.fake` covers everything.
2. **Local model and llama.cpp build** — needed to bind `brain.local`, also step 8. Steps 1–7 need
   no model at all, and gate 2 can first run against `brain.reference`. Your RAM upgrade is not on
   the critical path for M1 or M2.
3. **GitHub push access** — `eventualdrift/apollo` rejects pushes for this session (the Claude GitHub
   App is not installed for the org). Both commits are sound locally; history has not been rewritten.
   This blocks publishing, not building.

One decision deliberately left to first contact rather than guessed now: the **persona suite pass
threshold**. Setting a number before seeing a single run would be a guess dressed as a target.
Acceptance criterion O.1/18 requires every deterministic check to pass or carry a dated, reasoned
waiver — which is stricter than a percentage and does not require inventing one.

---

## 5. ADRs written

Twelve, in `docs/adr/`. Eight of your nine nominated topics map one-to-one; memory provenance and
supersession merged into ADR-0005 because they are one decision.

| ADR | Records |
|---|---|
| 0001 | Core owns all durable state; adapters own nothing and cannot read the database |
| 0002 | Aliases, adapter-owned rendering and tokenisation, llama.cpp as deployment not architecture |
| 0003 | File-backed composed identity, hashed over composed text, snapshotted on load |
| 0004 | Relational state of record, transactional audit stream, hash chaining deferred |
| 0005 | Per-observation provenance, evidence-derived confidence, correction by supersession |
| 0006 | Typed trust-labelled context blocks, deterministic composition, identity never truncated |
| 0007 | Manifest reconstruction instead of stored prompts; no reasoning traces persisted |
| 0008 | Conversation privacy modes, enforced twice and independently |
| 0009 | No transparent cross-provider fallback — identity and privacy reasons |
| 0010 | Deterministic intent detection; model structures, user authorises |
| 0011 | Lexical retrieval only; embeddings gated on evidence |
| 0012 | Behavioural compatibility as a gate separate from protocol compatibility |

Three additions beyond your list, justified in `docs/adr/README.md`: 0007 (turn auditability
constrains immutability across the schema and is expensive to retrofit), 0011 (encodes a decision
*procedure* for a question certain to recur), 0012 (without it, ADR-0001 gets over-read as
"drop-in replaceable").

Library choices, ID format, retrieval K, budget percentages, confidence constants, intent patterns
and proposal expiry are safe defaults and got no records.

---

## 6. Build sequence

Full detail in [`implementation-plan.md`](./implementation-plan.md). Seventeen steps, five milestones.

| | Steps | Outcome |
|---|---|---|
| **M1** | 1 skeleton + import test · 2 storage + transactional audit · 3 identity · 4 context bundle + budget · 5 brain abstraction + fake · 6 turn orchestrator + CLI | Apollo holds a conversation against `brain.fake` with a complete turn record and manifest for every exchange |
| **M2** | 7 policy and modes · 8 OpenAI-compatible adapter + gate 1 · 9 persona suite + diff | Behaviour measurable and comparable across brains, before memory complicates it |
| **M3** | 10 memory lifecycle · 11 retrieval + its eval · 12 proposals | Apollo remembers what he was asked to remember; absence reported honestly |
| **M4** | 13 replay · 14 failure behaviour + redaction · 15 backup and verified restore | Every technical criterion in §O.1 passes |
| **M5** | 16 acceptance run · 17 product trial | Both gates answered |

Five ordering constraints that are not arbitrary:

- The import test is written in step 1, never retrofitted around existing violations.
- Policy precedes the hosted adapter. A privacy control added after the thing it controls is a
  control that was absent for a while.
- Persona measurement precedes memory, so the first persona failures are diagnosable as persona
  failures rather than arriving tangled with retrieval bugs.
- The retrieval eval ships with retrieval, not after. Retrieval tuned without a fixture is guessing.
- Replay comes after memory, because reconstruction is only meaningfully tested once memory rows are
  among the sources.

---

## 7. Acceptance gate

Nineteen technical criteria in specification §O.1, plus one product gate in §O.2.

The technical criteria are ordinary verification work. Five are worth naming because they test
something a normal test suite would not:

- **O.1/4** — with unrelated recent memories present and no lexical or pinned match, the negative
  retrieval signal is still emitted. This is the anti-confabulation mechanism tested directly.
- **O.1/6** — the persona suite across three brains produces *identical bundle hashes* per case, so
  the comparison is honest.
- **O.1/7** — there is no code path from generation to memory creation.
- **O.1/11** — mode enforcement tested through *both* mechanisms independently.
- **O.1/12** — a forced failure between a memory write and its audit event leaves neither committed.

The product gate is the one that decides whether the phase succeeded. After the technical gate
passes, several days of ordinary use, answering: *do I actually prefer talking to this over a generic
AI chat, for at least some everyday conversations?* Supporting evidence recorded but not pass/fail:
memory capture rate and conversations started per day. Near-zero memory creation over several days
means the capture path failed, whatever the tests say.

**If every technical criterion passes and Apollo is unpleasant or pointless to use, phase zero has
failed as a product experiment**, and the response is to fix the experience rather than proceed to
connectors on the strength of a green suite.

---

## 8. Repository state

```
branch            claude/apollo-architecture-5xd22k
spec commit       d2dce6639c506d90cb05c7d0fff792143ebe91a7
parent            989a9d8  (rev 1, superseded)
working tree      clean at time of report, except this report
remote            origin https://github.com/eventualdrift/apollo  — push rejected, 403
source code       none. No application code has been written.
```

```
docs/
├── adr/                    README.md + ADR-0001 … ADR-0012
└── architecture/
    ├── phase-zero-spec.md        §A–§P, the frozen candidate
    ├── behaviour-contract.md     B1–B24, splits into three identity fragments at freeze
    ├── implementation-plan.md    17 steps, 5 milestones
    └── freeze-report.md          this document
```

No `src/`, no `identity/`, no `evals/`, no `tests/`. Those arrive with step 1, once implementation is
authorized.
