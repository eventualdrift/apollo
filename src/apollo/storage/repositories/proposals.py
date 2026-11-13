"""SQL for `memory_proposal` (spec C.9).

Plain rows in, plain rows out: the rules live in `apollo.memory.proposals`
and `apollo.memory.approval`, and the database holds them again (migration
0005). Proposal text is only ever sent as a bind parameter.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from apollo.storage.ids import uuid7
from apollo.storage.unit_of_work import UnitOfWork

_COLUMNS = (
    "id, conversation_id, turn_id, model_invocation_id, source_message_id, scope, kind,"
    " subject, content, status, resulting_memory_id, created_at, resolved_at"
)


class ProposalRepository:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def insert_pending(
        self,
        *,
        conversation_id: uuid.UUID,
        turn_id: uuid.UUID,
        model_invocation_id: uuid.UUID,
        source_message_id: uuid.UUID,
        scope: str,
        kind: str,
        subject: str,
        content: str,
        now: datetime,
    ) -> uuid.UUID:
        proposal_id = uuid7()
        self._uow.execute(
            "INSERT INTO memory_proposal (id, conversation_id, turn_id, model_invocation_id,"
            "  source_message_id, scope, kind, subject, content, status, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)",
            (proposal_id, conversation_id, turn_id, model_invocation_id, source_message_id,
             scope, kind, subject, content, now),
        )
        return proposal_id

    def resolve(
        self,
        proposal_id: uuid.UUID,
        *,
        status: str,
        resulting_memory_id: uuid.UUID | None,
        now: datetime,
    ) -> None:
        """Record the resolution and clear the text, in one statement (spec C.9)."""
        self._uow.execute(
            "UPDATE memory_proposal SET status = %s, resulting_memory_id = %s, resolved_at = %s,"
            "  subject = NULL, content = NULL WHERE id = %s",
            (status, resulting_memory_id, now, proposal_id),
        )

    def lock(self, proposal_id: uuid.UUID) -> dict[str, Any] | None:
        cur = self._uow.execute(
            f"SELECT {_COLUMNS} FROM memory_proposal WHERE id = %s FOR UPDATE", (proposal_id,)
        )
        row: dict[str, Any] | None = cur.fetchone()
        return row

    def lock_pending_before(self, cutoff: datetime) -> list[dict[str, Any]]:
        """Pending proposals created at or before the cutoff, locked, oldest first."""
        cur = self._uow.execute(
            f"SELECT {_COLUMNS} FROM memory_proposal"
            " WHERE status = 'pending' AND created_at <= %s ORDER BY created_at, id FOR UPDATE",
            (cutoff,),
        )
        rows: list[dict[str, Any]] = cur.fetchall()
        return rows

    def lock_pending_from_sources_of(self, memory_ids: list[uuid.UUID]) -> list[dict[str, Any]]:
        """Pending proposals structured from a message any of these memories cites, locked."""
        cur = self._uow.execute(
            f"SELECT {_COLUMNS} FROM memory_proposal"
            " WHERE status = 'pending' AND source_message_id IN ("
            "   SELECT message_id FROM memory_observation"
            "    WHERE memory_id = ANY(%s) AND message_id IS NOT NULL)"
            " ORDER BY created_at, id FOR UPDATE",
            (memory_ids,),
        )
        rows: list[dict[str, Any]] = cur.fetchall()
        return rows

    def get(self, proposal_id: uuid.UUID) -> dict[str, Any] | None:
        cur = self._uow.execute(
            f"SELECT {_COLUMNS} FROM memory_proposal WHERE id = %s", (proposal_id,)
        )
        row: dict[str, Any] | None = cur.fetchone()
        return row

    def for_turn(self, turn_id: uuid.UUID) -> dict[str, Any] | None:
        cur = self._uow.execute(
            f"SELECT {_COLUMNS} FROM memory_proposal WHERE turn_id = %s", (turn_id,)
        )
        row: dict[str, Any] | None = cur.fetchone()
        return row

    def list(self, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
        cur = self._uow.execute(
            f"SELECT {_COLUMNS} FROM memory_proposal WHERE status = ANY(%s)"
            " ORDER BY created_at, id",
            (list(statuses),),
        )
        rows: list[dict[str, Any]] = cur.fetchall()
        return rows

    def counts_by_status(self) -> dict[str, int]:
        """The capture-path signal (spec O.2): how many were saved, edited, ignored, expired."""
        cur = self._uow.execute(
            "SELECT status, count(*) AS n FROM memory_proposal GROUP BY status"
        )
        return {row["status"]: row["n"] for row in cur.fetchall()}

    def source_message_content(self, message_id: uuid.UUID) -> str | None:
        cur = self._uow.execute("SELECT content FROM message WHERE id = %s", (message_id,))
        row = cur.fetchone()
        return None if row is None else str(row["content"])
