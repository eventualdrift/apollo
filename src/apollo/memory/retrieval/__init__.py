"""Retrieval: three strategies, a merge, and the substantive-match rule (spec E).

`retrieve` runs every strategy against the conversation's scope and merges
them; `record` writes what ran and what it found to `turn_retrieval` and
`turn_retrieval_result`, so "never found" and "found but dropped by the
budget" stay distinguishable (spec C.5, C.6).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from apollo.memory.retrieval.base import (
    DEFAULT_SETTINGS,
    LEXICAL,
    PINNED,
    RECENCY,
    STRATEGIES,
    Hit,
    RetrievalError,
    RetrievalOutcome,
    RetrievalQuery,
    RetrievalSettings,
    Retrieved,
    Strategy,
    StrategyResult,
    origin_for_mode,
)
from apollo.memory.retrieval.lexical import LexicalStrategy
from apollo.memory.retrieval.merge import merge, substantive_support
from apollo.memory.retrieval.pinned import PinnedStrategy
from apollo.memory.retrieval.recency import RecencyStrategy
from apollo.storage.repositories import RetrievalRepository
from apollo.storage.unit_of_work import UnitOfWork

#: Run in this order; the order is recorded, not semantic.
DEFAULT_STRATEGIES: tuple[Strategy, ...] = (PinnedStrategy(), LexicalStrategy(), RecencyStrategy())


def retrieve(
    uow: UnitOfWork,
    query: RetrievalQuery,
    settings: RetrievalSettings = DEFAULT_SETTINGS,
    strategies: tuple[Strategy, ...] = DEFAULT_STRATEGIES,
) -> RetrievalOutcome:
    repo = RetrievalRepository(uow)
    results = tuple(strategy.run(repo, query, settings) for strategy in strategies)
    return RetrievalOutcome(
        results=results,
        merged=merge(results, settings),
        substantive_support=substantive_support(results),
    )


def record(
    uow: UnitOfWork,
    *,
    turn_id: uuid.UUID,
    outcome: RetrievalOutcome,
    included: frozenset[uuid.UUID],
    now: datetime,
) -> None:
    """One row per strategy that ran, `result_count = 0` included (spec C.5).

    `included` is the set of memories that made it into the bundle; the rest
    were found but dropped, by the merge's K or by the context budget.
    """
    repo = RetrievalRepository(uow)
    for result in outcome.results:
        repo.record(
            turn_id=turn_id,
            strategy=result.strategy,
            query_text=result.query_text,
            substantive=result.substantive,
            duration_ms=result.duration_ms,
            executed_at=now,
            results=[
                (hit.memory_id, rank, hit.score, hit.memory_id in included)
                for rank, hit in enumerate(result.hits, start=1)
            ],
        )


__all__ = [
    "DEFAULT_SETTINGS",
    "DEFAULT_STRATEGIES",
    "LEXICAL",
    "PINNED",
    "RECENCY",
    "STRATEGIES",
    "Hit",
    "LexicalStrategy",
    "PinnedStrategy",
    "RecencyStrategy",
    "RetrievalError",
    "RetrievalOutcome",
    "RetrievalQuery",
    "RetrievalSettings",
    "Retrieved",
    "Strategy",
    "StrategyResult",
    "merge",
    "origin_for_mode",
    "record",
    "retrieve",
    "substantive_support",
]
