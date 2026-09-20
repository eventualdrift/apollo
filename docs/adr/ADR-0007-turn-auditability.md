# ADR-0007 — Replay by manifest reconstruction; deletion wins over reconstructability

Status: accepted · 2026-09-20 (rev 2: deletion semantics, sanitisation whitelist)

## Context

Debugging Apollo requires answering "exactly what did the model see?" Storing the rendered prompt
would duplicate every sensitive thing Apollo knows into a second location that gets queried casually,
exported during debugging, and swept into backups with different handling.

The earlier revision then created a contradiction it did not notice: it promised byte-exact
reconstruction of *any* completed turn while also promising that tombstoning really removes content.
Both cannot hold. One of them had to be weakened, and the choice of which is a values decision, not a
technical one.

Separately, some models expose a reasoning channel, and provider error paths routinely embed request
bodies in exception messages.

## Decision

**No rendered prompt is stored.** Each model invocation records a manifest — ordered blocks with
types, tiers, token counts, source references and drop reasons — plus `identity_hash`,
`compiler_version`, `adapter_key`, `render_version`, `token_estimator` and `context_bundle_hash`.
Replay re-renders deterministically from the manifest and the source rows and compares hashes.

**The manifest carries references and metadata, never content and never per-block content digests.**
The only digest over block content is the whole-bundle hash, which covers everything combined and is
therefore not a useful handle on any single short claim.

**Deletion wins over replay.** Apollo does not retain deleted plaintext so reconstruction stays
possible. Fake deletion would be worse than an honest gap.

Replay reports two independent statuses rather than one optimistic boolean:

- `bundle_status`: `VERIFIED` · `MISMATCH` · `SOURCE_REDACTED` (deliberate deletion; names the
  unavailable refs, recreates nothing) · `SOURCE_MISSING` (unexplained absence — a different fact
  from deliberate redaction, just as a failed search differs from an empty one).
- `render_status`: `VERIFIED` · `MISMATCH` · `RENDERER_UNAVAILABLE` (the recorded `render_version` is
  no longer implemented) · `NOT_ATTEMPTED`.

The guarantee, stated precisely:

> Apollo can reproduce a turn while its source material still exists. Deliberate deletion may make
> historical content reconstruction impossible, and Apollo reports exactly which sources are
> unavailable and why.

Archive and supersession preserve content and therefore preserve replay. Only tombstone — and, later,
message deletion under the same principle — produces `SOURCE_REDACTED`. This works because *claim
content* and *message content* are write-once (ADR-0005), not because rows are wholly immutable.

**No hidden reasoning traces are persisted in any stream.** Adapters discard them, recording only
`reasoning_tokens`. It is the model's scratch space; it frequently contains content the visible
answer deliberately excluded; storing it is a privacy liability out of proportion to its debugging
value; and having it stored tempts treating it as evidence of what Apollo "really" thinks.

**Provider metadata and errors are whitelisted, never captured wholesale.** `raw_meta` exists on the
in-memory `Generation` and is never persisted; Core copies out fixed scalars only. `error_kind` is an
Apollo enum. `error_detail` is assembled from HTTP status, provider error code and provider error
type, truncated — never from `str(exception)` or a response body, because HTTP client exception
messages routinely embed the request body or a URL with query parameters. Exceptions are mapped from
their **type** to an error kind; their text is never recorded.

## Consequences

- Full auditability with zero duplicate storage of sensitive text, and no conflict with deletion.
- Dropped-block entries answer "Apollo had that memory, why didn't he use it?"
- Replay depends on keeping old `compiler_version` and `render_version` renderers available; when one
  is retired, replay says so instead of silently comparing against the wrong renderer.
- Some historical turns become permanently unreconstructable. Accepted deliberately.

## Reversal cost

Low to add prompt storage later; high to recover reconstructability if content immutability is
abandoned. Reversing the deletion decision would mean retaining deleted content, which is not on the
table.
