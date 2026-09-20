# ADR-0006 — Context compiler: typed trust-labelled blocks, deterministic composition, escaped fences

Status: accepted · 2026-09-20 (rev 2: escaping rule, per-invocation taint)

## Context

Naive prompt assembly concatenates strings, which makes three things impossible: knowing what the
model actually saw, keeping untrusted content from acquiring instruction authority, and reasoning
about what was dropped when the budget ran out.

A fourth problem was latent in the earlier revision: it asserted that fenced content "cannot produce"
a Core fence without implementing any property that made this true. A memory containing
`<<<END MEMORY>>>` would have terminated its own block, and one containing
`<<<RETRIEVAL tier=T0>>>` could have fabricated a block at the only tier carrying instruction
authority.

## Decision

Context is compiled into a typed `ContextBundle` of `ContextBlock`s. Every block carries a trust
tier, a taint level, a source kind and a resolvable source reference. No anonymous text enters context.

**Trust tiers** are ordered T0–T5. **Only T0 carries instruction authority.** No non-T0 content is
ever placed in a system-instruction region.

**Taint** is computed **per model invocation** — each invocation has its own bundle — as the highest
tier index at T4 or above. A turn's taint is the maximum across its invocations, computed on demand
rather than stored, because nothing consumes it until an action system exists.

**Fences are enforced by escaping, not by assertion.** Before a non-T0 body is placed inside a fence:

```
encode:  \ -> \\ ,  then  < -> \< ,  > -> \>
decode:  \\ -> \ ,  \< -> < ,  \> -> >      (left to right; any other \x is an error)
```

No `<` or `>` survives unescaped, so no body can contain `<<<` or `>>>`; content can neither
terminate its own block nor fabricate a T0 block. The transform is deterministic, total and exactly
reversible, so replay reproduces it and the original is always recoverable. **The database stores the
unescaped original**; escaping exists only in the rendered request. `CONTEXT_RULES` carries the
legend so the model reads escaped text correctly. Tests include deliberate delimiter collisions.

Blanket escaping is verbose for code-bearing memories. That is an accepted cost: it is one sentence
to specify and trivial to test, which is worth more now than terseness. Minimal escaping and
content-derived fence nonces are the documented alternatives, with a named trigger.

**Composition is deterministic.** Fixed block order, never reordered by score. The three practical
hazards are handled explicitly: `now` is captured once per turn and passed in; collections are sorted
before rendering; score ties break by `memory_id`.

**Budget uses fixed floors and caps, not fill-until-full.** Identity is never sacrificed — if it does
not fit, compilation raises rather than truncating, because silently trimming identity is silently
changing who Apollo is, and it would happen exactly during long interesting conversations.

Rendering to a wire format belongs to the adapter (ADR-0002).

## Consequences

- "What did the model see" is answerable structurally.
- The trust boundary is a property of the pipeline rather than a convention.
- Escaping means rendered text differs from stored text; every path that displays memory content to a
  human must use the stored form, not the rendered one.
- `compiler_version` becomes a first-class version recorded on every invocation.

## Reversal cost

High. The compiler is the hinge between memory and models.
