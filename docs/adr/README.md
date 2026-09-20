# Architecture Decision Records

ADRs are written at **architecture freeze**, not before. This index lists the decisions that have
been judged foundational enough to justify one — decisions that are expensive to reverse, or that
someone (including a future Janu, or a future Apollo) would otherwise re-litigate from scratch.

Each ADR will be one page: context, decision, consequences, and reversal cost.

Source: [`../architecture/phase-zero-spec.md`](../architecture/phase-zero-spec.md) §N.

| ADR | Decision | Status |
|---|---|---|
| 0001 | Apollo Core owns all durable state; model adapters are stateless and hold none | proposed |
| 0002 | Hybrid persistence: authoritative relational state plus an advisory append-only audit stream written in the same transaction — not event sourcing | proposed |
| 0003 | PostgreSQL is the only datastore in phase zero; no vector store, no search engine | proposed |
| 0004 | Confidence is computed from evidence in application code; models propose claims and never grade them | proposed |
| 0005 | Memory correction is by supersession; memory and message rows are immutable | proposed |
| 0006 | Context is a typed bundle; rendering to model wire format belongs to the adapter | proposed |
| 0007 | Trust tier and taint are first-class context metadata from phase zero | proposed |
| 0008 | Turn context is reconstructed from a manifest rather than stored as a rendered prompt | proposed |
| 0009 | Identity lives in version-controlled files and is snapshotted to the database on load | proposed |
| 0010 | Lexical retrieval only; embeddings gated on retrieval-eval evidence | proposed |
| 0011 | The behavioural regression suite is a release gate for identity, compiler and brain changes | proposed |
| 0012 | Hosted brains are restricted to benchmark conversations by a runtime guard | proposed |
| 0013 | Hidden reasoning traces are never persisted | proposed |
| 0014 | Raw SQL with numbered migrations; no ORM | proposed |
| 0015 | No automatic brain fallback on failure; failures are visible | proposed |

## Not ADR-worthy

Recorded here so the question is not reopened: library choices (FastAPI, psycopg, pytest), ID format,
retrieval K, budget percentages, and the confidence constants are all **safe defaults** listed in
§P.2 of the specification. They are cheap to change and do not need a decision record.
