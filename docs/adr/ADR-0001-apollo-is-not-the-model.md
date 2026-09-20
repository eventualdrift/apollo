# ADR-0001 — Apollo is not the model: Core owns all durable state

Status: accepted · 2026-09-20

## Context

The obvious architecture for a personal AI is a model with a memory feature bolted on. It fails on a
multi-year horizon: identity, memory and relationship state end up entangled with a particular
model's prompt format, provider, or hosted account, and replacing the model means replacing Apollo.

## Decision

Apollo Core is the only component that owns durable state — identity, conversations, memory,
retrieval, context compilation, policy and audit. Clients own presentation only. Model adapters own
nothing: they are functions from a compiled context to generated text.

The model therefore has no ambient access to any Apollo state. It receives a bundle Core built and
returns text. It cannot read the database, cannot enumerate memories, and cannot act.

Enforced structurally by a module import test: `brains/` may not import from `storage/`, `memory/`
or `core/`.

## Consequences

- A model swap cannot lose state, because the model never held any.
- Every capability the model appears to have is a capability Core gave it, deliberately, that turn.
- Adapters stay small, which keeps the cost of supporting a new runtime low.
- Core carries more responsibility and must be correspondingly well tested.
- This guarantees *state* continuity only. Behavioural continuity is a separate, empirical matter —
  see ADR-0012.

## Reversal cost

Very high. This is the foundational decision; reversing it is a rewrite.
