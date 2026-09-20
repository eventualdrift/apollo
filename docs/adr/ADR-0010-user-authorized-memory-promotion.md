# ADR-0010 — Memory promotion requires explicit user authorization, and proposals are transient

Status: accepted · 2026-09-20 (rev 2: proposal content minimisation)

## Context

Two failure modes, opposite in direction and both fatal.

Automatic extraction — a model deciding which parts of conversation become durable memory — means one
offhand remark becomes a permanent assumption about Janu, with no audit trail worth the name.

Pure manual CRUD avoids that and introduces the other: if capture has friction it does not happen,
memory stays empty, and the phase-zero premise appears to fail when what actually failed was the
input path.

## Decision

**Detection of persistence intent is deterministic application code** — a pattern list over the user
message, plus an explicit client action. A model-based intent classifier is the door through which
"the model decided this should be remembered" walks in.

**The model structures, the user authorises.** On detected intent, a recorded `memory_proposal`
invocation produces a structured claim with status `pending`. The user chooses Save, Edit and Save,
or Ignore. Only Save creates a memory.

Invariants: a proposal is not a memory and never enters any context; the model cannot approve its own
proposal, because creation requires a separate authenticated call naming the proposal id and there is
no code path from generation to memory creation; exact provenance is retained; approved proposals are
`user_asserted` with `source_kind=model_proposal_approved`; `self`-scope proposals are rejected as
self-model mutation; at most one pending proposal per turn; phase zero never scans ordinary
conversation for candidates.

The proposal invocation's bundle carries `PROPOSAL_RULES` (T0, policy) and the source message as a
**`data`** block, not a `request` block: in that invocation the message is material to be structured,
not a request to carry out, so an instruction embedded in it is inert.

### Proposal text is transient

A pending proposal needs its content — that is what the user is shown. **On reaching any terminal
state, `content` and `subject` are cleared in the resolving transaction.**

A saved proposal would otherwise hold a second copy of a private claim the memory row already holds,
and an ignored or expired one would hold a copy of a claim Janu declined to keep. Both undermine
minimisation and the deletion story: tombstoning the memory would leave the claim sitting in the
proposal table.

Retained: references, final status, `resulting_memory_id`, timestamps, and `scope` and `kind` — closed
enums with no free text, so they cannot carry a secret, and they answer "what kinds of thing does
Janu actually save versus ignore", the capture-path signal the product gate depends on. `subject` is
cleared because it is free text.

Nothing of the model's original wording is kept for `saved_edited`. The status already records that
an edit happened, which is the quality signal; the text is exactly the duplicate content being
removed.

Nullable columns plus a check constraint, not a second table.

## Consequences

- Natural capture without autonomous promotion.
- The proposal table stops being a shadow copy of the memory table.
- Deleting a memory no longer leaves a copy behind in a resolved proposal.
- This mechanism is the foundation for future memory-learning UX; passive candidate generation later
  reuses the same surface with a different tier.

## Reversal cost

Low to extend, high to remove — downstream provenance assumes the boundary exists.
