"""SQL for `memory` and `memory_observation` (spec C.7, C.8).

Every statement that writes these two tables lives here, and only
`apollo.memory.lifecycle` calls the writers (an architecture test holds both).
The database refuses anything the lifecycle forbids (migrations 0003, 0004);
this module states no rules of its own and returns plain rows, because
`storage/` imports nothing else from Apollo (spec A.3).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from apollo.storage.ids import uuid7
from apollo.storage.unit_of_work import UnitOfWork


class MemoryRepository:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    # -- writes ---------------------------------------------------------------

    def insert_memory(
        self,
        *,
        scope: str,
        kind: str,
        subject: str,
        content: str,
        origin_tier: str,
        origin: str,
        pinned: bool,
        now: datetime,
    ) -> uuid.UUID:
        memory_id = uuid7()
        self._uow.execute(
            "INSERT INTO memory (id, scope, kind, subject, content, origin_tier, origin,"
            "  status, pinned, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s)",
            (memory_id, scope, kind, subject, content, origin_tier, origin, pinned, now, now),
        )
        return memory_id

    def insert_observation(
        self,
        *,
        memory_id: uuid.UUID,
        relation: str,
        source_kind: str,
        now: datetime,
        message_id: uuid.UUID | None = None,
        external_ref: str | None = None,
        excerpt: str | None = None,
    ) -> uuid.UUID:
        observation_id = uuid7()
        self._uow.execute(
            "INSERT INTO memory_observation (id, memory_id, relation, source_kind,"
            "  message_id, external_ref, excerpt, observed_at, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                observation_id,
                memory_id,
                relation,
                source_kind,
                message_id,
                external_ref,
                excerpt,
                now,
                now,
            ),
        )
        return observation_id

    def mark_confirmed(self, memory_id: uuid.UUID, now: datetime) -> None:
        self._uow.execute(
            "UPDATE memory SET last_confirmed_at = %s, updated_at = %s WHERE id = %s",
            (now, now, memory_id),
        )

    def mark_contradicted(self, memory_id: uuid.UUID, now: datetime) -> None:
        self._uow.execute("UPDATE memory SET updated_at = %s WHERE id = %s", (now, memory_id))

    def mark_superseded(self, memory_id: uuid.UUID, by: uuid.UUID, now: datetime) -> None:
        self._uow.execute(
            "UPDATE memory SET status = 'superseded', superseded_by_id = %s, updated_at = %s"
            " WHERE id = %s",
            (by, now, memory_id),
        )

    def mark_archived(self, memory_id: uuid.UUID, now: datetime) -> None:
        self._uow.execute(
            "UPDATE memory SET status = 'archived', archived_at = %s, updated_at = %s"
            " WHERE id = %s",
            (now, now, memory_id),
        )

    def mark_restored(self, memory_id: uuid.UUID, now: datetime) -> None:
        self._uow.execute(
            "UPDATE memory SET status = 'active', archived_at = NULL, updated_at = %s"
            " WHERE id = %s",
            (now, memory_id),
        )

    def mark_tombstoned(self, memory_id: uuid.UUID, now: datetime) -> None:
        """Clears the claim text. The generated `search_vector` empties with it."""
        self._uow.execute(
            "UPDATE memory SET status = 'tombstoned', subject = NULL, content = NULL,"
            "  tombstoned_at = %s, updated_at = %s WHERE id = %s",
            (now, now, memory_id),
        )

    def clear_excerpts(self, memory_id: uuid.UUID) -> int:
        cur = self._uow.execute(
            "UPDATE memory_observation SET excerpt = NULL"
            " WHERE memory_id = %s AND excerpt IS NOT NULL",
            (memory_id,),
        )
        return int(cur.rowcount)

    # -- reads ----------------------------------------------------------------

    def lock(self, memory_id: uuid.UUID) -> dict[str, Any] | None:
        """The row, locked for the rest of the transaction (serialises lifecycle writes)."""
        cur = self._uow.execute("SELECT * FROM memory WHERE id = %s FOR UPDATE", (memory_id,))
        row: dict[str, Any] | None = cur.fetchone()
        return row

    def lock_predecessor(self, memory_id: uuid.UUID) -> dict[str, Any] | None:
        """The row this one superseded, locked; at most one (memory_superseded_by_uq)."""
        cur = self._uow.execute(
            "SELECT * FROM memory WHERE superseded_by_id = %s FOR UPDATE", (memory_id,)
        )
        row: dict[str, Any] | None = cur.fetchone()
        return row

    def get(self, memory_id: uuid.UUID) -> dict[str, Any] | None:
        cur = self._uow.execute("SELECT * FROM memory WHERE id = %s", (memory_id,))
        row: dict[str, Any] | None = cur.fetchone()
        return row

    def list(self, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
        cur = self._uow.execute(
            "SELECT * FROM memory WHERE status = ANY(%s) ORDER BY created_at, id",
            (list(statuses),),
        )
        rows: list[dict[str, Any]] = cur.fetchall()
        return rows

    def observation_counts(self, memory_id: uuid.UUID) -> dict[str, int]:
        cur = self._uow.execute(
            "SELECT relation, count(*) AS n FROM memory_observation WHERE memory_id = %s"
            " GROUP BY relation",
            (memory_id,),
        )
        return {row["relation"]: int(row["n"]) for row in cur.fetchall()}
