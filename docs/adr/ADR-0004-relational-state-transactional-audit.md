# ADR-0004 — Relational state of record with a transactional audit stream

Status: accepted · 2026-09-20 (rev 2: explicit transaction boundaries)

## Context

Full event sourcing gives auditability, replay and painless schema evolution — at the cost of
projection machinery and a mental model that is heavy for a solo-maintained system that has not yet
proven itself useful. Conventional relational state is simple but loses history: an UPDATE erases the
previous belief and the reason for it.

## Decision

**Conventional relational tables are the state of record.** Normal operation never rebuilds anything
from a log.

**An append-only `audit_event` stream records meaningful state changes.** It is advisory: where it
disagrees with relational state, the relational state wins.

**Every audit event describing a mutation of Apollo's own Postgres state is written in the same
transaction as that mutation.** Both commit or neither does. An audit stream that can silently
diverge is worse than none, because it will be trusted.

### Transaction boundaries

Three groupings are atomic, and one thing is forbidden:

- **Turn opening** — request message, turn row (`status=started`) and their audit events commit
  together. A committed message with no turn would be a continuity gap in a system whose purpose is
  continuity.
- **Invocation opening** — each `model_invocation` row is committed with `status=started` *before*
  the model is called. This is what makes "no hidden model call" a property: a crash mid-call leaves
  a `started` invocation, not silence.
- **Finalisation** — response message, turn completion and audit commit together. Failure
  finalisation likewise: failed status, `system_note` and audit together.
- **Forbidden:** holding a database transaction open across a model call. Asserted by a test that
  fails if a connection is checked out when an adapter is entered.

On startup, turns and invocations left `started` beyond a configured window are marked `failed` with
`error_kind=interrupted`.

The audit `payload` never contains message bodies, memory content, excerpts, rendered prompts,
provider response bodies or exception text — ids, hashes, enums, counts and durations only, under the
same whitelist as ADR-0007.

Append-only is enforced by application discipline plus permissions: the application role holds no
`UPDATE` or `DELETE` grant on `audit_event`. **Cryptographic hash chaining is deferred** — it defends
against an attacker who already holds privileged database write access, which is not the phase-zero
threat model. Cryptography is not added because it looks rigorous.

## Consequences

- Queries stay legible. A new contributor, or Janu in a year, reads SQL rather than a projection
  pipeline.
- Same-transaction writing constrains how mutations are coded: every lifecycle operation goes through
  a unit of work that carries its events.
- The rule is scoped to **database-owned state only.** Events about external or non-transactional
  systems — future connectors, device actions — cannot use it and will need their own semantics (an
  outbox, or explicit at-least-once records). The rule must not be extended to them by reflex.
- Historical replay is limited to what the audit stream and the source rows capture; accepted.

## Reversal cost

Moving to full event sourcing later is expensive but tractable, and the audit stream is a head start.
