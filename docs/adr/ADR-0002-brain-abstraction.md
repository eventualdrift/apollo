# ADR-0002 — Brain abstraction: aliases, adapter-owned rendering and tokenisation

Status: accepted · 2026-09-20 (rev 2: `render_version`)

## Context

Apollo must not be coupled to any provider, runtime or model family. Models differ in ways that leak
upward if the seam is drawn badly: chat models want a messages array with a system role, base models
want one string with a chat template, some servers reject system roles, and every family tokenises
differently.

The initial local deployment is `llama.cpp`'s server — a deployment choice on the merits of current
hardware, not an architectural commitment.

## Decision

Code and configuration refer to **aliases** — `brain.default`, `brain.reference`, `brain.fake` —
never to model names. Configuration binds an alias to a provider.

Three responsibilities belong to the adapter, not to Core:

1. **Rendering.** The compiler emits a typed `ContextBundle`; the adapter renders it into that
   model's wire format, including applying the fence escaping of ADR-0006. If Core rendered, every
   wire-format difference would be a Core change.

   The adapter is bound by a **four-region contract**: `policy` blocks go to the highest-authority
   instruction region the provider offers; `history` keeps conversational roles; `data` is fenced,
   escaped and never authoritative; `request` is the current user message, verbatim and unfenced, as
   the final user turn. An adapter without system/user/assistant roles must preserve the same
   distinction in whatever template it uses. No `data` block may reach the policy region or be
   rendered as the request — Gate 1 tests this, including with deliberate delimiter collisions.
2. **Token counting.** Adapters supply a `TokenEstimator` through `ModelCapabilities`. Core owns
   budget *policy* — priority, reservation, what is dropped — and asks the adapter for counts. Core
   is never taught a model family's tokenisation rules. A documented conservative fallback
   (`ceil(len/3)`) applies when no exact counter exists: over-estimating truncates slightly more
   history, under-estimating overflows the context, and only one of those is recoverable.
3. **Declaring `render_version`.** Every adapter declares `adapter_key` and `render_version`, and
   both are recorded on every model invocation.

**`render_version` identifies the exact bundle-to-request transformation**: block ordering,
system-role placement, fence application, escaping, message-array shape, parameter mapping. It is
bumped whenever that transformation changes in a way that alters the bytes sent. It is not a package
version and must not be derived from one.

It exists because `compiler_version` describes only how the *bundle* was built. Since rendering is
adapter-owned, `compiler_version` alone cannot reproduce what a model was sent — a claim the earlier
revision of this architecture made incorrectly. Replay needs both:

```
sources → compiler_version → ContextBundle → verify bundle hash
        → adapter_key + render_version → RenderedRequest → verify rendered_prompt_hash
```

No llama.cpp-specific string, parameter or assumption may appear outside
`brains/openai_compatible.py` and configuration.

## Consequences

- Moving to Ollama, vLLM, SGLang, another machine or dedicated hardware is a config change.
- A runtime that cannot satisfy the contract gets a new adapter, never a change in Core.
- Changing rendering requires a deliberate `render_version` bump, and historical turns rendered under
  a retired version replay with `render_status=RENDERER_UNAVAILABLE` rather than a false match.
- Because estimators differ per adapter, eval runs pin `conservative-v1` for all brains so bundles
  are byte-identical and behavioural differences are attributable to the model.

## Reversal cost

Moderate. The seam could be moved, but every adapter would be rewritten.
