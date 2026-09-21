# ADR-0005 — Memory origin, derived support, and correction by supersession

Status: accepted · 2026-09-20 (rev 2: origin/support separation, precise mutability)

## Context

"The user prefers X" is not enough. Apollo needs to know where a belief came from, how well attested
it is, and what replaced it. The tempting shortcut — asking the model to emit `confidence: 0.94` —
produces a number that looks rigorous and means nothing, because models are not calibrated.

The earlier revision had a subtler bug: a single `provenance_tier` field whose values mixed origins
(`user_asserted`) with support states (`user_confirmed`, `inferred_repeated`). One field meaning both
"where this came from" and "how well supported it is now" has no coherent update rule, and the
confidence function silently depended on the ambiguity.

## Decision

**Three separate concepts, one of them stored.**

**`origin_tier` is stored, write-once, and records only where the claim came from**:
`user_asserted`, `model_inferred`, `connector_imported`. It never changes. Confirming a claim does
not promote its origin, because confirmation is evidence about a claim, not a new origin for it.
Phase zero writes only `user_asserted`.

**Support is derived, never stored**, from observations:
`contested` if any contradiction, `confirmed` if confirmations and no contradiction, else `asserted`.

**Confidence is derived, never stored**, from `origin_tier` plus observation counts. Because it is
not persisted, the function can change without a migration. It is displayed in context and used as a
ranking tiebreaker, and gates nothing in phase zero.

**Provenance is per observation, not per claim.** `memory_observation` records each assertion,
confirmation and contradiction with source kind, originating message and a verbatim excerpt. One
table, three relations.

**The model may propose an interpretation; it never grades the truth of its own interpretation.**

**Correction is by supersession, never mutation.** A correction inserts a new row, marks the old
`superseded` with `superseded_by_id` set, and adds an `asserts` observation to the new row.
Observations are not copied forward — the chain is the history, and duplicating evidence would
inflate support on every correction.

**Contradiction does not auto-retract.** It is evidence for the user to resolve. Automatic retraction
is how one sloppy sentence deletes something true.

### Mutability, stated precisely

The earlier revision claimed "memory rows are immutable", which was false and load-bearing for
replay. The real invariant is narrower:

| Write-once, always | Write-once, except cleared by a tombstone | Mutable |
|---|---|---|
| `scope`, `kind`, `origin_tier`, `origin`, `created_at` | `subject`, `content` | `status`, `pinned`, `superseded_by_id`, `last_confirmed_at`, `archived_at`, `tombstoned_at`, `updated_at` |

**Claim content is immutable; lifecycle metadata is not.** The one operation that removes content is
tombstone, which is deliberate and which invalidates reconstruction for turns referencing it
(ADR-0007). Archive and supersession both preserve content and therefore preserve replay.

`subject` and `content` are therefore NOT NULL for every live status and NULL only once tombstoned,
held by a CHECK rather than by a column constraint — a plain `NOT NULL` could not express "not null
while live" and would make deletion impossible.

**Provenance outlives the claim it describes.** Classification and provenance stay write-once in
every transition, the tombstone included: a tombstone clears the text and sets lifecycle fields, and
can never rewrite `origin_tier`, `scope`, `kind`, `origin` or `created_at`. Recording provenance
separately from the claim is pointless if the act of deleting the claim can rewrite it.

## Consequences

- "Where did this come from, how well attested is it now, and what replaced it" are three answerable
  questions with three distinct mechanisms.
- Confidence constants are arbitrary starting values; inspectable and cheap to change.
- Counts derive from observations rather than being denormalised — one source of truth.
- Replay correctness depends on content immutability specifically, which is now written down as such.

## Reversal cost

High. Provenance is difficult to add retroactively — the evidence no longer exists.
