# ADR-0005 — Memory provenance, evidence-derived confidence, correction by supersession

Status: accepted · 2026-09-20

## Context

"The user prefers X" is not enough. Apollo needs to know where a belief came from, how well attested
it is, and what replaced it. The tempting shortcut — asking the model to emit `confidence: 0.94` —
produces a number that looks rigorous and means nothing, because models are not calibrated and will
emit 0.9 for a guess.

## Decision

**Provenance per observation, not per claim.** `memory_observation` records each assertion,
confirmation and contradiction with its source kind, originating message and a verbatim excerpt. One
table, three relations, identical shape.

**Confidence is computed in application code from evidence structure** — provenance tier, observation
counts, contradiction counts — and never stored. The model may propose an interpretation; it never
grades the truth of its own interpretation. Because the score is not persisted, the function can
change without a migration. In phase zero it is displayed in context and used as a ranking
tiebreaker, and gates nothing.

**Correction is by supersession, never mutation.** A correction inserts a new row, marks the old
`superseded` with `superseded_by_id` set, and adds an `asserts` observation to the new row.
Observations are not copied forward — the chain is the history, and duplicating evidence would
inflate confidence on every correction.

**Contradiction does not auto-retract.** It is recorded as evidence for the user to resolve.
Automatic retraction on contradiction is how one sloppy sentence deletes something true.

Four states: `active`, `superseded`, `archived`, `tombstoned`. Tombstone nulls content and excerpts,
keeps the row so chains and historical manifests do not dangle, and removes only the claim — not the
message that produced it.

## Consequences

- "Why does Apollo believe this, and what did it replace" is answerable from the schema.
- Memory rows are immutable, which is also what makes turn reconstruction work (ADR-0007).
- Confidence constants are arbitrary starting values; they are inspectable and cheap to change.
- Counts derive from observations rather than being denormalised — one source of truth, at a scale
  where the cost is irrelevant.

## Reversal cost

High. Provenance is difficult to add retroactively — the evidence no longer exists.
