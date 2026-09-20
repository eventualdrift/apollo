# Architecture Decision Records

Thirteen records, covering the decisions judged expensive to reverse or likely to be re-litigated.
One page each: context, decision, consequences, reversal cost.

Source: [`../architecture/phase-zero-spec.md`](../architecture/phase-zero-spec.md).

| ADR | Decision | Status |
|---|---|---|
| [0001](./ADR-0001-apollo-is-not-the-model.md) | Apollo is not the model: Core owns all durable state | accepted |
| [0002](./ADR-0002-brain-abstraction.md) | Brain abstraction: aliases, adapter-owned rendering and tokenisation, `render_version` | accepted |
| [0003](./ADR-0003-file-backed-identity.md) | Canonical identity is file-backed, composed and hashed | accepted |
| [0004](./ADR-0004-relational-state-transactional-audit.md) | Relational state of record with a transactional audit stream | accepted |
| [0005](./ADR-0005-memory-provenance-supersession.md) | Memory origin, derived support, correction by supersession | accepted |
| [0006](./ADR-0006-context-compiler.md) | Context compiler: typed trust-labelled blocks, deterministic composition, escaped fences | accepted |
| [0007](./ADR-0007-turn-auditability.md) | Replay by manifest reconstruction; deletion wins over reconstructability | accepted |
| [0008](./ADR-0008-conversation-privacy-modes.md) | Privacy modes and an eval-only reference surface | accepted |
| [0009](./ADR-0009-no-transparent-fallback.md) | No transparent cross-provider fallback | accepted |
| [0010](./ADR-0010-user-authorized-memory-promotion.md) | Memory promotion requires explicit user authorization | accepted |
| [0011](./ADR-0011-lexical-retrieval-only.md) | Lexical retrieval only; embeddings gated on retrieval-eval evidence | accepted |
| [0012](./ADR-0012-behavioural-compatibility-gate.md) | Behavioural compatibility is a separate gate from protocol compatibility | accepted |
| [0013](./ADR-0013-turns-contain-model-invocations.md) | A turn contains zero or more model invocations | accepted |

## Mapping to nominated topics

Janu nominated nine topics. Eight map one-to-one (0001, 0002, 0003, 0004, 0005, 0006, 0008, 0009,
0010 — memory provenance and supersession merged into one record because they are one decision).
Four additions, each justified rather than manufactured:

- **0007 — turn auditability.** The "why did Apollo say this" guarantee constrains immutability
  across the schema and is expensive to retrofit. It also depends on 0005, and the dependency should
  be written down.
- **0011 — lexical retrieval only.** Low reversal cost, but it encodes a *decision procedure* for
  how "shouldn't we add a vector DB?" gets answered in future, which is what ADRs are for.
- **0012 — behavioural compatibility gate.** The operational form of the state-vs-behavioural
  continuity distinction. Without it, ADR-0001 gets over-read as "drop-in replaceable".
- **0013 — turns contain model invocations.** Added in the rev-3 correction pass. Phase zero already
  makes two kinds of model call, and a single set of model fields on `turn` would leave the second
  unaudited. Cheap to decide now, a migration of every historical turn later.

## Not ADR-worthy

Library choices (FastAPI, psycopg, pytest, no ORM), ID format, retrieval K, budget percentages,
confidence constants, intent patterns and proposal expiry are **safe defaults** listed in §P.2 of the
specification. They are cheap to change and do not get records.
