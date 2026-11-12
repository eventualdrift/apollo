"""SQL for retrieval (spec E, C.5, C.6).

Reads `memory` for the three strategies and writes the per-turn retrieval
record. Returns plain rows: the strategies, the merge and the
substantive-match rule live in `apollo.memory.retrieval`, because `storage/`
imports nothing else from Apollo (spec A.3).

Every read is scoped the same way before any strategy-specific condition
(spec E.2): `status = 'active'` and the one `origin` the conversation's mode
allows. Superseded, archived and tombstoned rows can never be returned.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from apollo.storage.ids import uuid7
from apollo.storage.unit_of_work import UnitOfWork

#: The text-search configurations retrieval may run under (spec E.4). The
#: stored `search_vector` column is built with `english`; `simple` is computed
#: on the fly, which is fine for the eval and for a corpus of a few hundred.
TEXT_SEARCH_CONFIGS = ("english", "simple")
STORED_CONFIG = "english"

_COLUMNS = "m.id, m.subject, m.content, m.created_at"

_LEXICAL_STORED = (
    f"SELECT {_COLUMNS}, ts_rank_cd(m.search_vector, q.query, 32) AS score"
    "  FROM memory m, (SELECT websearch_to_tsquery('english', %(text)s) AS query) q"
    " WHERE m.status = 'active' AND m.origin = %(origin)s AND m.search_vector @@ q.query"
    " ORDER BY score DESC, m.id ASC LIMIT %(limit)s"
)

_LEXICAL_COMPUTED = (
    f"SELECT {_COLUMNS}, ts_rank_cd(v.vector, q.query, 32) AS score"
    "  FROM memory m,"
    "       (SELECT websearch_to_tsquery(%(config)s::regconfig, %(text)s) AS query) q,"
    "       LATERAL (SELECT to_tsvector(%(config)s::regconfig,"
    "                coalesce(m.subject, '') || ' ' || coalesce(m.content, '')) AS vector) v"
    " WHERE m.status = 'active' AND m.origin = %(origin)s AND v.vector @@ q.query"
    " ORDER BY score DESC, m.id ASC LIMIT %(limit)s"
)


class RetrievalRepository:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    # -- reads: the strategies' candidates -------------------------------------

    def pinned(self, origin: str) -> list[dict[str, Any]]:
        cur = self._uow.execute(
            f"SELECT {_COLUMNS} FROM memory m"
            " WHERE m.status = 'active' AND m.origin = %s AND m.pinned ORDER BY m.id",
            (origin,),
        )
        rows: list[dict[str, Any]] = cur.fetchall()
        return rows

    def lexical(
        self, origin: str, query_text: str, *, config: str, limit: int
    ) -> list[dict[str, Any]]:
        """Matches for a websearch-syntax query, each with `ts_rank_cd` scaled to [0, 1).

        Normalisation flag 32 maps a rank r to r / (r + 1), so scores are
        comparable across queries and a fixed floor means the same thing on
        every turn.
        """
        if config not in TEXT_SEARCH_CONFIGS:
            raise ValueError(f"unsupported text search configuration {config!r}")
        sql = _LEXICAL_STORED if config == STORED_CONFIG else _LEXICAL_COMPUTED
        cur = self._uow.execute(
            sql, {"origin": origin, "text": query_text, "config": config, "limit": limit}
        )
        rows: list[dict[str, Any]] = cur.fetchall()
        return rows

    def recent(self, origin: str, limit: int) -> list[dict[str, Any]]:
        cur = self._uow.execute(
            f"SELECT {_COLUMNS} FROM memory m"
            " WHERE m.status = 'active' AND m.origin = %s"
            " ORDER BY m.created_at DESC, m.id ASC LIMIT %s",
            (origin, limit),
        )
        rows: list[dict[str, Any]] = cur.fetchall()
        return rows

    # -- writes: the per-turn record (spec C.5, C.6) -----------------------------

    def record(
        self,
        *,
        turn_id: uuid.UUID,
        strategy: str,
        query_text: str | None,
        substantive: bool,
        duration_ms: int,
        executed_at: datetime,
        results: list[tuple[uuid.UUID, int, float, bool]],
    ) -> uuid.UUID:
        """One `turn_retrieval` row and its results: (memory_id, rank, score, included)."""
        retrieval_id = uuid7()
        self._uow.execute(
            "INSERT INTO turn_retrieval (id, turn_id, strategy, query_text, result_count,"
            "  substantive, duration_ms, executed_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (retrieval_id, turn_id, strategy, query_text, len(results), substantive,
             duration_ms, executed_at),
        )
        for memory_id, rank, score, included in results:
            self._uow.execute(
                "INSERT INTO turn_retrieval_result (turn_retrieval_id, memory_id, rank, score,"
                "  included_in_context) VALUES (%s, %s, %s, %s, %s)",
                (retrieval_id, memory_id, rank, score, included),
            )
        return retrieval_id
