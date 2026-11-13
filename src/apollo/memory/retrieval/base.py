"""Retrieval's shared types and settings (spec E).

Postgres only: no vector store, no embeddings, no reranker (ADR-0011). The
strategies share one interface, so a future `SemanticStrategy` is a new module
plus a nullable column, not a redesign.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from apollo.errors import ApolloError, ErrorKind
from apollo.memory.models import (
    ObservationCounts,
    Origin,
    OriginTier,
    Scope,
    Support,
    confidence,
    support,
)
from apollo.storage.repositories import RetrievalRepository

PINNED = "pinned"
LEXICAL = "lexical"
RECENCY = "recency"
STRATEGIES = (PINNED, LEXICAL, RECENCY)


class RetrievalError(ApolloError):
    """Retrieval could not run.

    The turn continues, and is told so: the bundle carries a RETRIEVAL_ERROR
    notice in place of memories (spec L). It is never reported as "nothing
    found".
    """

    kind = ErrorKind.RETRIEVAL_FAILED


@dataclass(frozen=True)
class RetrievalSettings:
    """Spec E.3, with the two values the spec leaves open stated here.

    `lexical_floor` and `text_search_config` are chosen by the retrieval eval
    (spec E.4; Janu, 2026-11-12), not by argument: `apollo.evals.retrieval.
    select_settings` picks them on the tuning cases and the held-out cases
    judge the choice. `evals/retrieval/selection.json` records the sweep, and
    CI fails if these defaults stop being what it selects.
    """

    k: int = 12
    recency_n: int = 3
    lexical_weight: float = 1.0
    recency_weight: float = 0.3
    #: Scores are `ts_rank_cd` scaled to [0, 1) by r / (r + 1). One shared word
    #: scores about 0.09, so 0.0 means any match that survives the
    #: configuration's stop words counts; what keeps weak matches out under
    #: `english` is its stop-word list, which the sweep shows is enough.
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
class Provenance:
    """What a memory's fence header and derived confidence are built from (spec B.4, D.8)."""

    scope: Scope
    origin_tier: OriginTier
    created_at: datetime
    observations: int
    counts: ObservationCounts

    @property
    def support(self) -> Support:
        return support(self.counts)

    @property
    def confidence(self) -> float:
        return confidence(self.origin_tier, self.counts)


@dataclass(frozen=True)
class Hit:
    """One memory a strategy returned, with that strategy's score in [0, 1]."""

    memory_id: uuid.UUID
    score: float
    subject: str
    content: str
    provenance: Provenance

    @classmethod
    def from_row(cls, row: dict[str, Any], score: float) -> Hit:
        return cls(
            row["id"], score, row["subject"], row["content"],
            Provenance(
                scope=Scope(row["scope"]),
                origin_tier=OriginTier(row["origin_tier"]),
                created_at=row["created_at"],
                observations=row["observations"],
                counts=ObservationCounts(row["confirms"], row["contradicts"]),
            ),
        )


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
    provenance: Provenance

    @property
    def match(self) -> str:
        """The strongest way it was found: pinned, else lexical, else recency."""
        return next(s for s in STRATEGIES if s in self.strategies)

    @property
    def substantive(self) -> bool:
        return self.match != RECENCY


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
