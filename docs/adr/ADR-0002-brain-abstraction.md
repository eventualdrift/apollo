# ADR-0002 — Brain abstraction: aliases, adapter-owned rendering and tokenisation

Status: accepted · 2026-09-20

## Context

Apollo must not be coupled to any provider, runtime or model family. Models differ in ways that leak
upward if the seam is drawn badly: chat models want a messages array with a system role, base models
want one string with a chat template, some servers reject system roles, and every family tokenises
differently.

The initial local deployment is `llama.cpp`'s server. That is a deployment choice made on the merits
of the current hardware, not an architectural commitment.

## Decision

Code and configuration refer to **aliases** — `brain.default`, `brain.reference`, `brain.fake` —
never to model names. Configuration binds an alias to a provider.

Two responsibilities belong to the adapter, not to Core:

1. **Rendering.** The context compiler emits a typed `ContextBundle`. The adapter renders it into
   that model's wire format. If Core rendered, every wire-format difference would be a Core change.
2. **Token counting.** Adapters supply a `TokenEstimator` through `ModelCapabilities`. Core owns
   budget *policy* — what has priority, what is dropped, what is reserved — and asks the adapter for
   counts. Core is never taught a model family's tokenisation rules.

A documented conservative fallback estimator (`ceil(len/3)`) applies when no exact counter exists.
Three characters per token rather than four is deliberate: over-estimating truncates slightly more
history, under-estimating overflows the context, and only one of those is recoverable.

No llama.cpp-specific string, parameter or assumption may appear outside
`brains/openai_compatible.py` and configuration.

## Consequences

- Moving to Ollama, vLLM, SGLang, another machine or dedicated hardware is a config change.
- A runtime that cannot satisfy the contract gets a new adapter, never a change in Core.
- Because estimators differ per adapter, the same conversation can budget differently per brain.
  Eval runs therefore pin `conservative-v1` for all brains so bundles are byte-identical and
  behavioural differences are attributable to the model. `turn.token_estimator` records which applied.

## Reversal cost

Moderate. The seam could be moved, but every adapter would be rewritten.
