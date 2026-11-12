"""`recency`: the N most recent active memories. A weak fallback, **never** substantive.

Spec E.3: if recency counted as support, the compiler would hand the model
unrelated recent memories and no "nothing matched" signal, indistinguishable
from a genuine hit. The database holds the same rule (`turn_retrieval` CHECK).
"""

from __future__ import annotations

import time

from apollo.memory.retrieval.base import (
    RECENCY,
    Hit,
    RetrievalQuery,
    RetrievalSettings,
    StrategyResult,
)
from apollo.storage.repositories import RetrievalRepository


class RecencyStrategy:
    name = RECENCY

    def run(
        self, repo: RetrievalRepository, query: RetrievalQuery, settings: RetrievalSettings
    ) -> StrategyResult:
        started = time.monotonic()
        rows = repo.recent(str(query.origin), settings.recency_n)
        # Newest scores 1.0, then evenly down: normalised to [0, 1] per spec E.3.
        n = max(len(rows), 1)
        hits = tuple(Hit.from_row(row, (n - i) / n) for i, row in enumerate(rows))
        return StrategyResult(
            strategy=RECENCY,
            query_text=None,
            hits=hits,
            substantive=False,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        )
