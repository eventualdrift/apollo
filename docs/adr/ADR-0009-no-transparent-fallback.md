# ADR-0009 — No transparent cross-provider fallback

Status: accepted · 2026-09-20 (rev 2)

## Context

When the local model is unavailable, silently retrying against another provider keeps Apollo
answering. Most systems do this and call it resilience.

## Decision

No automatic cross-provider fallback in phase zero. Transport-level failures get **one automatic
retry against the same configured brain**. After that the turn fails visibly: `brain_unavailable`, an
accurate error to the client, and a `system_note` in the transcript, finalised atomically. No
fabricated response.

Two independent reasons, the second stronger:

1. **Identity.** A silently substituted model makes behavioural continuity untestable — you cannot
   tell which Apollo answered, and the persona suite's meaning evaporates.
2. **Privacy.** Falling back from `brain.local` to `brain.reference` would route personal context to
   an `eval_only` provider whose `allowed_modes` forbids it (ADR-0008). The fallback path would be a
   hole straight through the privacy control, and it would open exactly when something is already
   going wrong.

Any future fallback must be explicitly configured, policy-aware and observable — a declared degraded
brain with compatible data-mode permissions, visible in the response.

## Consequences

- Apollo is unavailable when his inference backend is. Accepted for phase zero.
- Failures are legible rather than mysteriously degraded.
- The eventual availability design has to be built deliberately, which is the point.

## Reversal cost

Low, and deliberately so — a policy decision, revisited when a real availability requirement exists.
