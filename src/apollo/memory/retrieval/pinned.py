"""`pinned`: every active pinned memory, no query. Always included, never support by itself.

Spec E.3 counted any pinned result as substantive support. Spec E.6/2 (Janu,
2026-11-13) changed that: while any memory was pinned, every question had
"support" and the notice that nothing matched was never given. A pinned memory
is support only when the search also matched it, and then it is the lexical
strategy that says so.
"""

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
            substantive=False,
            duration_ms=_elapsed_ms(started),
        )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))
