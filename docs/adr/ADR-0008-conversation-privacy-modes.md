# ADR-0008 — Conversation privacy modes gate which brains may be used

Status: accepted · 2026-09-20

## Context

A hosted frontier model is genuinely useful as a behavioural reference point, but personal memories
must never reach it. "Only use the hosted model for non-sensitive testing" enforced by developer
memory is not a control — it is an intention that fails the first time someone is in a hurry.

## Decision

Conversations carry a **mode**, immutable after creation:

| Mode | Memory access | Connectors (future) |
|---|---|---|
| `personal` | `origin='personal'` only | permitted |
| `benchmark` | `origin='fixture'` only | denied |

Providers declare `allowed_modes`. `brain.reference` declares `["benchmark"]`; local and fake declare
both.

Enforcement is by **two independent mechanisms**:

1. A check in `core/policy.py`, before retrieval or generation: if the conversation's mode is not in
   the provider's `allowed_modes`, the turn is refused with `brain_mode_not_permitted` and a
   `policy.refused` audit event.
2. A `memory.origin` filter in the retrieval layer, so benchmark conversations cannot see personal
   memories even if the first check were bypassed.

**Hard fail, never a silent downgrade.** Apollo does not quietly answer with a different brain and
does not quietly drop memories to make a brain permissible. Benchmark mode is visibly distinguishable
in the client.

Mode is immutable because changing it would retroactively alter what data was permitted in turns
already taken.

## Consequences

- The privacy rule is a property the system checks, not a habit developers maintain.
- Two mechanisms means two tests and a guarantee that survives one of them being wrong.
- Benchmark conversations are genuinely useless for real work, which is correct.
- This is deliberately small. It is not a data-classification framework and must not become one in
  phase zero.

## Reversal cost

Low. Modes could be extended or replaced; the enforcement points are two named places.
