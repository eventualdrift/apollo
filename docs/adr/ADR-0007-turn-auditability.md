# ADR-0007 — Turn auditability by manifest reconstruction; no stored prompts or reasoning traces

Status: accepted · 2026-09-20

## Context

Debugging Apollo requires answering "exactly what did the model see when it produced this?" The
obvious approach — store the rendered prompt — duplicates every sensitive thing Apollo knows into a
second location that will be queried casually, exported during debugging, and included in backups
with different handling.

Separately, some models expose a reasoning channel. Storing it is tempting and wrong.

## Decision

**No rendered prompt is stored.** Each turn records a manifest: the ordered list of blocks with their
types, trust tiers, token counts, source references, and — importantly — the blocks that were
*dropped*, with the reason. Plus `identity_hash`, `compiler_version`, `token_estimator` and
`context_bundle_hash`.

Reconstruction re-renders deterministically from the manifest and the immutable source rows, then
compares hashes. A match means byte-exact reconstruction. A mismatch is itself informative: something
believed immutable changed.

This works only because messages are immutable and memory corrections create new rows rather than
mutating old ones — so ADR-0005 and this decision depend on each other.

**No hidden reasoning traces are persisted in any stream.** Adapters discard them, recording only a
token count. It is the model's scratch space; it frequently contains content the visible answer
deliberately excluded; storing it is a privacy liability out of proportion to its debugging value;
and having it tempts treating it as evidence of what Apollo "really" thinks, which it is not.

Three streams stay distinct: ephemeral operational logs (never containing content, asserted by a
sentinel test), durable turn records, and immutable audit events.

## Consequences

- Full auditability with zero duplicate storage of sensitive text.
- Dropped-block entries answer the most common memory-system question: "Apollo had that memory, why
  didn't he use it?"
- Immutability constraints propagate across the schema and must be respected everywhere.
- Reconstruction depends on keeping old compiler versions renderable.

## Reversal cost

Low to add prompt storage later; high to recover reconstructability if immutability is abandoned.
