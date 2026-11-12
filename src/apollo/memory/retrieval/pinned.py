"""`pinned`: every active pinned memory, no query. Substantive whenever non-empty (spec E.3)."""

from __future__ import annotations

import time

from apollo.memory.retrieval.base import (
    PINNED,
    Hit,
    RetrievalQuery,
    RetrievalSettings,
    StrategyResult,
)
from apollo.storage.repositories import RetrievalRepository


class PinnedStrategy:
    name = PINNED

    def run(
        self, repo: RetrievalRepository, query: RetrievalQuery, settings: RetrievalSettings
    ) -> StrategyResult:
        started = time.monotonic()
        hits = tuple(Hit.from_row(row, 1.0) for row in repo.pinned(str(query.origin)))
        return StrategyResult(
            strategy=PINNED,
            query_text=None,
            hits=hits,
            substantive=bool(hits),
            duration_ms=_elapsed_ms(started),
        )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))
