# ADR-0013 — A turn contains zero or more model invocations

Status: accepted · 2026-09-20

## Context

Phase zero already performs two different kinds of model call: generating the conversational reply,
and structuring a memory proposal when the user expresses persistence intent.

The earlier schema carried one set of model fields — brain alias, provider, model identifier,
generation parameters, token usage, latency, finish reason, rendered prompt hash — directly on `turn`.
With two calls per turn, that design forces a choice between leaving the second call unaudited and
flattening two unrelated operations into one record. Both are wrong, and the fix is cheap now and a
migration later.

## Decision

Two entities with distinct meanings:

- **`turn`** — the user-facing conversational transaction. One inbound message, one outbound message,
  one status, the identity in force. No model-specific fields.
- **`model_invocation`** — one actual call to a model. Carries `purpose`, brain alias, provider,
  model identifier, `adapter_key`, `render_version`, `compiler_version`, `token_estimator`, its own
  `context_manifest` and `context_bundle_hash`, trust tier and taint, generation parameters,
  rendered prompt hash, token counts, finish reason, timing, status and sanitised error fields.

A turn has zero or more invocations, ordered by `seq`.

**Each invocation carries its own compiled bundle.** The reply bundle is the full Apollo context; the
proposal bundle is a minimal structuring context with no identity and no memory blocks, so existing
memories cannot contaminate a proposal and Apollo's character is not spent on a formatting task.

**`purpose` is a closed enum: `reply` and `memory_proposal`.** Adding a value requires a
specification change. This is not the seed of an agent framework and must not be treated as one.

**No hidden model call.** The brain registry is the only place an adapter is invoked, and it refuses
to invoke one without an `invocation_id` for a row already committed with `status=started`
(ADR-0004). The eval runner uses the same path, so eval calls are recorded exactly like interactive
ones. `memory_proposal` rows record which invocation produced them.

Identity remains denormalised on `turn` as the single exception: it is true of the turn as a whole,
persona evaluation keys on it, and "every turn under identity X" should not require a join.

## Consequences

- Every model call is auditable, replayable and attributable, including secondary ones.
- Replay operates per invocation, which is the correct granularity — the two calls in a turn have
  different bundles and may have different render versions.
- Token cost and latency per purpose become visible, which will matter when deciding whether proposal
  structuring is worth its cost.
- Slightly more schema than a single-call design, paid once, before any code exists.

## Reversal cost

High after implementation, which is why it is being made now. Retrofitting means migrating every
historical turn record.
