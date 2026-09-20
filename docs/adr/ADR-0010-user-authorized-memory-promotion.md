# ADR-0010 — Memory promotion requires explicit user authorization

Status: accepted · 2026-09-20

## Context

Two failure modes, opposite in direction and both fatal.

Automatic extraction — a model deciding which parts of conversation become durable memory — means one
offhand remark becomes a permanent assumption about Janu, with no audit trail worth the name.

Pure manual CRUD avoids that and introduces the other failure: if capture has friction, it does not
happen; memory stays empty; Apollo is a chatbot with extra steps; and the phase-zero premise appears
to fail when what actually failed was the input path.

## Decision

A middle path with the authorisation boundary drawn sharply.

**Detection of persistence intent is deterministic application code** — a pattern list over the user
message ("remember that", "from now on", "keep in mind", …), plus an explicit client action. A
model-based intent classifier is exactly the door through which "the model decided this should be
remembered" walks in.

**The model structures, the user authorises.** On detected intent, the model proposes a structured
claim into `memory_proposal` with status `pending`. The user sees it and chooses Save, Edit and Save,
or Ignore. Only Save creates a memory.

Invariants:

1. A proposal is not a memory. Proposals are never retrieved and never enter context.
2. The model cannot approve its own proposal. Creation requires a separate authenticated call naming
   the proposal id. There is no code path from generation to memory creation.
3. The proposal retains exact provenance — the verbatim originating message.
4. Approved proposals are `user_asserted` with `source_kind=model_proposal_approved`, so the
   structuring stays visible.
5. `self`-scope memories cannot be created from proposals — that would be self-model mutation. Direct
   entry only.
6. At most one pending proposal per turn. No batching.
7. Phase zero never scans ordinary conversation for candidates. No background extraction, no nightly
   learning, no confidence-based promotion.

`memory_proposal` exists as a table for three reasons, in order of weight: it makes invariant 2
enforceable server-side rather than by client good behaviour; a proposal must not die when the
terminal closes; and proposed/saved/edited/ignored counts are direct evidence about whether the
capture path works — the one thing the product gate depends on.

Direct creation via API remains available always, and is the only route to `self` scope.

## Consequences

- Natural capture without autonomous promotion.
- This mechanism is the foundation for future memory-learning UX; passive candidate generation later
  reuses the same proposal surface with a different tier.
- One state column, four terminal states. It is not a workflow engine and nothing beyond these
  transitions may be added in phase zero.

## Reversal cost

Low to extend, high to remove — downstream provenance assumes the boundary exists.
