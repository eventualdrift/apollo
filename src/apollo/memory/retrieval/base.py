"""Retrieval's shared types and settings (spec E).

Postgres only: no vector store, no embeddings, no reranker (ADR-0011). The
strategies share one interface, so a future `SemanticStrategy` is a new module
plus a nullable column, not a redesign.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from apollo.errors import ApolloError
from apollo.memory.models import Origin
from apollo.storage.repositories import RetrievalRepository

PINNED = "pinned"
LEXICAL = "lexical"
RECENCY = "recency"
STRATEGIES = (PINNED, LEXICAL, RECENCY)


class RetrievalError(ApolloError):
    """Retrieval could not run. The turn fails as `retrieval_failed`, never silently."""


@dataclass(frozen=True)
class RetrievalSettings:
    """Spec E.3, with the two values the spec leaves open stated here.

    `lexical_floor` and `text_search_config` are chosen by the retrieval eval
    (step 11, Janu 2026-11-12): the highest `correct_empty_rate`, then the
    highest `recall@5`, judged on held-out cases. Until that run, the floor is
    provisional.
    """

    k: int = 12
    recency_n: int = 3
    lexical_weight: float = 1.0
    recency_weight: float = 0.3
    #: Provisional until the step 11 eval sets it. Scores are `ts_rank_cd`
    #: scaled to [0, 1) by r / (r + 1); this admits any single-term match.
    lexical_floor: float = 0.0
    text_search_config: str = "english"
    #: Candidates read from Postgres before the floor; well above K.
    lexical_limit: int = 50
    #: Distinct words taken from the query text, to bound the query's size.
    max_query_terms: int = 64


DEFAULT_SETTINGS = RetrievalSettings()


def origin_for_mode(mode: str) -> Origin:
    """Spec E.2: a personal conversation sees personal memories; a benchmark, fixtures."""
    if mode == "personal":
        return Origin.PERSONAL
    if mode == "benchmark":
        return Origin.FIXTURE
    raise RetrievalError(f"no retrieval scope for conversation mode {mode!r}")


@dataclass(frozen=True)
class RetrievalQuery:
    """What retrieval searches with: the current and the previous user message (spec E.3)."""

    current: str
    previous: str | None
    origin: Origin


@dataclass(frozen=True)
class Hit:
    """One memory a strategy returned, with that strategy's score in [0, 1]."""

    memory_id: uuid.UUID
    score: float
    subject: str
    content: str

    @classmethod
    def from_row(cls, row: dict[str, Any], score: float) -> Hit:
        return cls(row["id"], score, row["subject"], row["content"])


@dataclass(frozen=True)
class StrategyResult:
    strategy: str
    query_text: str | None
    hits: tuple[Hit, ...]
    #: Did this strategy contribute substantive support? Never for recency.
    substantive: bool
    duration_ms: int


@dataclass(frozen=True)
class Retrieved:
    """A memory after the merge: its merged score and the strategies that found it."""

    memory_id: uuid.UUID
    score: float
    pinned: bool
    strategies: tuple[str, ...]
    subject: str
    content: str


@dataclass(frozen=True)
class RetrievalOutcome:
    results: tuple[StrategyResult, ...]
    merged: tuple[Retrieved, ...]
    #: Spec E.3: pinned results, or lexical results above the floor. Never recency.
    substantive_support: bool

    def result(self, strategy: str) -> StrategyResult:
        return next(r for r in self.results if r.strategy == strategy)


class Strategy(Protocol):
    name: str

    def run(
        self, repo: RetrievalRepository, query: RetrievalQuery, settings: RetrievalSettings
    ) -> StrategyResult: ...
