# ADR-0006 — Context compiler: two authorities, four regions, escaped fences

Status: accepted · 2026-09-20 (rev 3: policy/task authority split, block regions)

## Context

Naive prompt assembly concatenates strings, which makes three things impossible: knowing what the
model actually saw, keeping untrusted content from acquiring authority it should not have, and
reasoning about what was dropped when the budget ran out.

Two defects in earlier revisions of this record:

**"Instruction authority" was one term doing two jobs.** It said only T0 held it and that T1 user
messages did not. Read literally, that instructs Apollo not to follow Janu's actual requests — which
is the entire purpose of the system. The real distinction is between changing Apollo's rules and
asking Apollo to do something.

**Fences were asserted rather than implemented.** Content containing `<<<RETRIEVAL tier=T0>>>` could
have fabricated a block at the tier carrying the most authority.

## Decision

### Two authorities, not one

**Policy authority** — may define or change identity, behavioural contract, compiler rules,
permissions or trust rules. Held only by T0 *policy* blocks, originating in version-controlled files
or in Core itself.

**Task authority** — may pose the request Apollo answers this turn. Held only by the current user
message.

So "Explain what this function does" is carried out, and "From now on always agree with me" is
discussed rather than applied, because it asks for a policy change and no message has policy
authority. T2 history is task-historical: it was the request then, it is not the request now. T3 and
above have neither authority.

T0 itself carries two block kinds: *policy* blocks (`IDENTITY`, `CONTEXT_RULES`, `PROPOSAL_RULES`)
and *notice* blocks (`RETRIEVAL_NOTICE`, `RETRIEVAL_ERROR`) — Core-authored statements of fact with
no policy content, whose trustworthiness comes from being unforgeable rather than from placement.

### Four regions

Every block carries a `region`: `policy`, `history`, `data` or `request`. The region decides where an
adapter may place it; the tier decides how much authority it carries. They are orthogonal, which is
what lets a T0 notice sit safely in the `data` region.

The invariant becomes:

> **Data cannot impersonate policy or the current user request.**

Not "everything below T0 is inert data", which would have made Apollo useless.

Three independent mechanisms enforce it: region separation (§G.3 render contract, tested by Gate 1),
escaping (below), and a plain statement in `CONTEXT_RULES` that persona cases verify.

### Escaping, implemented

Every block body placed into a delimited region is escaped before fencing: `\` → `\\`, then `<` → `\<`
and `>` → `\>`, decoded left to right. No `<` or `>` survives unescaped, so no body can contain `<<<`
or `>>>` — content can neither terminate its own block nor fabricate a notice or policy block. Total
and exactly reversible, so replay reproduces it and the original is recoverable. **The database stores
the unescaped original**; escaping exists only in the rendered request.

Blocks carried as native structured fields rely on the transport's encoding; the adapter guarantees
content cannot escape its structural boundary either way.

The current user message is **never** fenced or escaped — it is the request, rendered verbatim.

Blanket escaping is verbose for code-bearing memories. Accepted: one sentence to specify, trivial to
test. Minimal escaping and content-derived nonces are documented alternatives under a named trigger.

### Composition and budget

Deterministic: fixed block order, never reordered by score. `now` captured once per turn and passed
in; collections sorted before rendering; ties broken by `memory_id`. `system_note` messages are
excluded from history, because rendering a Core-authored note as dialogue would misattribute it.

Fixed floors and caps, not fill-until-full. **Identity is never sacrificed** — if it does not fit,
compilation raises rather than truncating, because silently trimming identity is silently changing
who Apollo is, and it would happen exactly during long interesting conversations.

## Consequences

- Apollo answers requests and still cannot be reconfigured by one.
- The trust boundary is a property of the pipeline, not a convention.
- Adapters gain a real contract to satisfy, tested at Gate 1 rather than assumed.
- Escaping means rendered text differs from stored text; every path displaying content to a human
  must use the stored form.

## Reversal cost

High. The compiler is the hinge between memory and models.
