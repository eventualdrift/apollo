# ADR-0004 — Relational state of record with a transactional audit stream

Status: accepted · 2026-09-20

## Context

Full event sourcing gives auditability, replay and painless schema evolution — at the cost of
projection machinery, rebuild paths and a mental model that is heavy for a solo-maintained system
that has not yet proven itself useful.

Conventional relational state is simple but loses history: an UPDATE erases the previous belief and
the reason for it.

## Decision

A hybrid, with the balance set deliberately.

**Conventional relational tables are the state of record.** Ordinary queries, ordinary indexes,
ordinary maintenance. Normal operation never rebuilds anything from a log.

**An append-only `audit_event` stream records meaningful state changes** for audit, debugging,
provenance and later analysis. It is advisory: where it disagrees with relational state, the
relational state wins.

**Every audit event describing a mutation of Apollo's own Postgres state is written in the same
transaction as that mutation.** Both commit or neither does. An audit stream that can silently
diverge is worse than none, because it will be trusted.

The audit `payload` never contains message bodies, memory content, excerpts or rendered prompts —
ids, hashes, enums, counts and durations only.

Append-only is enforced by application discipline plus database permissions: the application role
holds no `UPDATE` or `DELETE` grant on `audit_event`. **Cryptographic hash chaining is deferred** —
it defends against an attacker who already holds privileged database write access, which is not the
phase-zero threat model. Cryptography is not added because it looks rigorous.

## Consequences

- Queries stay legible. A new contributor (or Janu in a year) reads SQL, not a projection pipeline.
- History survives without projection machinery.
- Same-transaction writing constrains how mutations are coded: every lifecycle operation goes through
  a unit of work that carries its events.
- The rule is scoped to **database-owned state only.** Events about external or non-transactional
  systems — future connectors, device actions — cannot use it and will need their own semantics
  (an outbox, or explicit at-least-once records). The rule must not be extended to them by reflex.
- Historical replay is limited to what the audit stream captures; this is accepted.

## Reversal cost

Moving to full event sourcing later is expensive but tractable, and the audit stream is a head start.
