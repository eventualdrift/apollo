# ADR-0006 — Context compiler: typed trust-labelled blocks, deterministic composition

Status: accepted · 2026-09-20

## Context

Naive prompt assembly concatenates strings, which makes three things impossible: knowing what the
model actually saw, keeping untrusted content from acquiring instruction authority, and reasoning
about what was dropped when the budget ran out.

## Decision

Context is compiled into a typed `ContextBundle` of `ContextBlock`s. Every block carries a trust
tier, a taint level, a source kind and a resolvable source reference. No anonymous text enters context.

**Trust tiers** are ordered: T0 canonical, T1 user-direct, T2 Apollo-prior, T3 curated memory,
T4 derived, T5 external. **Only T0 carries instruction authority.** No non-T0 content is ever placed
in a system-instruction region. Non-T0 blocks render inside labelled fences with a provenance header.

**Taint** is computed per turn as the highest tier index at T4 or above. Phase zero has no T4/T5
sources, so every turn is taint 0 — but the field is computed, recorded and tested from the first
commit, so the mechanism exists before it is needed. Retrofitting taint through an existing pipeline
is miserable.

**Composition is deterministic.** Fixed block order, never reordered by score. The three practical
determinism hazards are handled explicitly: `now` is captured once per turn and passed in, no module
calls the clock during compilation; collections are sorted before rendering; score ties break by
`memory_id`.

**Budget uses fixed floors and caps, not fill-until-full.** Identity is never sacrificed — if it does
not fit, compilation raises rather than truncating, because silently trimming identity is silently
changing who Apollo is, and it would happen exactly during long interesting conversations.

Rendering to a wire format belongs to the adapter (ADR-0002).

## Consequences

- "What did the model see" is answerable, and answerable structurally.
- The trust boundary is a property of the pipeline rather than a convention.
- The compiler is a central chokepoint that must be well tested; `compiler_version` becomes a
  first-class version that turns record.

## Reversal cost

High. The compiler is the hinge between memory and models.
