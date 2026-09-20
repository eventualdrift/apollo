# ADR-0012 — Behavioural compatibility is a separate gate from protocol compatibility

Status: accepted · 2026-09-20

## Context

ADR-0001 guarantees that Apollo's data survives a model change. That guarantee is easy to
over-read into "models are drop-in replaceable".

They are not. What survives a swap is data. What does not survive is behaviour: the same memories and
the same identity file, run through different weights, produce a different character. That is the
part Janu will actually notice, and it is exactly the part no schema protects.

## Decision

Two distinct, named concepts:

- **State continuity** — identity, memories, history and relationship state survive. Architectural,
  guaranteed by design.
- **Behavioural continuity** — the new model still behaves recognisably as Apollo. Empirical,
  established only by running the persona suite.

And two gates, both of which a brain must pass before it may be bound to `brain.default`:

**Gate 1 — protocol compatibility.** Mechanical. Implements `Brain`; sufficient context length;
renders a representative bundle; returns a well-formed `Generation`; declares `allowed_modes`.

**Gate 2 — behavioural compatibility.** Empirical. The persona suite runs against the candidate at
the current identity hash; the diff against the incumbent is reviewed; every changed case is accepted
or recorded as a dated waiver with a reason.

Eval runs pin the conservative estimator across all brains so bundles are byte-identical and
differences are attributable to the model rather than to budgeting.

Apollo is never described as supporting "drop-in" model replacement. Passing gate 1 means Apollo can
technically use a model. Only gate 2 means Apollo still behaves like Apollo through it.

## Consequences

- The persona suite is infrastructure, not a nice-to-have — it is the only instrument measuring the
  claim Apollo is built on.
- Model upgrades acquire a review step. That cost is the point.
- Waivers must be dated and reasoned, so drift accumulates visibly rather than silently.

## Reversal cost

Low mechanically, high in consequence — dropping gate 2 means never knowing whether Apollo survived
an upgrade.
