# ADR-0008 — Privacy modes and an eval-only reference surface

Status: accepted · 2026-09-20 (rev 2: eval-only reference, explicit limit of the guarantee)

## Context

A hosted frontier model is genuinely useful as a behavioural reference point, but personal memories
must never reach it. "Only use the hosted model for non-sensitive testing" enforced by developer
memory is not a control — it is an intention that fails the first time someone is in a hurry.

The earlier revision permitted interactive benchmark conversations, which created a subtler problem:
it invited the claim that benchmark mode makes hosted calls safe. It does not, and cannot, because a
human can type anything into a benchmark conversation.

## Decision

**`brain.reference` is an evaluation and reference facility, not a normal interactive Apollo brain.**

Three independent mechanisms. Each costs a few lines; the failure they prevent is unrecoverable,
because data that left the machine cannot be recalled.

1. **`eval_only` providers are unreachable from the interactive API.** The brain registry refuses to
   resolve a provider declared `eval_only` outside the eval runner entry point. Ordinary chat runs in
   `personal` mode and cannot route to `brain.reference`.
2. **Mode × `allowed_modes`**, checked in `core/policy.py` before retrieval or generation. A mismatch
   is a hard refusal with an audit event.
3. **`memory.origin` filtering** in the retrieval layer, so a benchmark context cannot see personal
   memories even if the first two checks were bypassed.

The interactive API creates only `personal` conversations. Benchmark conversations exist only in the
eval path. `conversation.mode` is immutable, because changing it would retroactively alter what data
was permitted in turns already taken.

**Hard fail, never a silent downgrade.** Apollo does not quietly answer with a different brain and
does not quietly drop memories to make a brain permissible.

### What this guarantees, and what it does not

**Guaranteed:** no stored personal memory, no personal conversation history, and no connector data
(when connectors exist) is ever *automatically* copied into a context sent to a hosted or eval-only
provider.

**Not claimed:** that text a human typed into an eval fixture contains no personal information.
Apollo cannot prove that arbitrary prose is non-sensitive. Such text is **user-authorised input to an
external provider**, and responsibility for its content rests with whoever wrote it.

The guarantee covers automatic flows, which the architecture controls. It does not cover human input,
which it cannot. Stating the limit is part of the decision: a privacy claim that overreaches is worse
than a narrower one that holds, because it gets relied on.

## Consequences

- The privacy rule is a property the system checks, not a habit developers maintain.
- Three mechanisms means three tests and a guarantee that survives any one of them being wrong.
- Reference comparison happens through the eval workflow, which is where it belongs anyway.
- This is deliberately small. It is not a data-classification framework and must not become one.

## Reversal cost

Low. The enforcement points are three named places.
