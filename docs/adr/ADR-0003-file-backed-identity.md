# ADR-0003 — Canonical identity is file-backed, composed and hashed

Status: accepted · 2026-09-20

## Context

Apollo's identity must be inspectable, diffable, easy to experiment with, and reproducible for any
historical turn. Storing it in the database makes it hard to review; storing it only in files makes
historical turns unreconstructable once the files change.

## Decision

Identity lives in version-controlled fragments under `identity/`, ordered by `manifest.yaml`:

```
manifest.yaml   core.md   behaviour.md   relationship.md
```

Composition is deterministic: a header carrying `schema_version` and `identity_version`, then each
fragment in manifest order with a fixed delimiter. `identity_hash` is the sha256 of the **composed
text**, not of filenames — so a deliberate version bump changes the hash even when fragment content
is identical. Per-fragment hashes are stored alongside so a persona diff can name which fragment moved.

On load, the composed identity is snapshotted into `identity_version` (insert on first sight, never
updated). Files are the source of truth for *authoring*; the table is an immutable snapshot for
*auditing*.

`CONTEXT_RULES` is explicitly **not** an identity fragment. It describes the compiler's fence syntax
and is versioned with `compiler_version`. Placing it in identity would mean a fence-syntax change
dirties the identity hash and pollutes every persona diff.

No speculative fragments are created.

## Consequences

- Behaviour can be edited in a text file and diffed in a pull request.
- Every turn is reconstructable even after the files change (ADR-0007).
- Identity changes do not happen through conversation — that is a deliberate property, not a gap.
- Two version concepts must be kept straight: `identity_hash` and `compiler_version`.

## Reversal cost

Low to moderate.
