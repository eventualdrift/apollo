# ADR-0011 — Lexical retrieval only; embeddings gated on retrieval-eval evidence

Status: accepted · 2026-09-20

## Context

Reflex says a memory system needs a vector database. That brings a second model dependency, an
embedding-version migration problem, a full reindex whenever the embedding model changes, and an
extra service — in exchange for uncertain gain on a corpus of a few hundred manually authorised
memories.

## Decision

Postgres only. Full-text search, plus pinned and recency strategies. No embeddings, no vector store,
no reranker in phase zero.

The strategy interface stays open: adding `SemanticStrategy` later is a new module and a nullable
column.

**The gate is evidence, not opinion.** The retrieval evaluation suite is built before the corpus
grows, with hand-labelled expectations across nine required case types. Embeddings get added when
the suite shows lexical retrieval failing cases embeddings would catch. If lexical scores well, that
is a finding, not a shortcut.

Recorded as an ADR despite a low reversal cost, because it encodes a **decision procedure** — how a
future "shouldn't we just add a vector DB?" gets answered — rather than only a choice.

## Consequences

- One datastore. No embedding service, no dimension versioning, no reindex burden.
- The retrieval suite must exist early, which is independently valuable.
- Lexical retrieval will miss paraphrases. That is expected; the suite will show how much it costs.

## Reversal cost

Low, and gated on evidence rather than on appetite.
