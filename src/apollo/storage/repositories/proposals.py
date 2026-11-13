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

    def personal_turns_by_capture(self, capture_note: str) -> dict[str, int]:
        """Answered personal turns, by what the capture path did with each (spec O.2).

        Only a turn that was answered can propose anything, so a turn whose
        reply failed is not counted. A proposal row is never removed, so a
        turn with a structuring call and no row is a capture that failed.
        `capture_note` is the note written beside such a reply; it also covers
        a failure before any call was made.
        """
        cur = self._uow.execute(
            "SELECT count(*) AS turns, count(p.id) AS proposed,"
            "       count(*) FILTER (WHERE p.id IS NULL AND (EXISTS ("
            "           SELECT 1 FROM model_invocation i"
            "            WHERE i.turn_id = t.id AND i.purpose = 'memory_proposal')"
            "         OR EXISTS ("
            "           SELECT 1 FROM message m WHERE m.turn_id = t.id"
            "            AND m.role = 'system_note' AND m.content = %s))) AS failed"
            "  FROM turn t LEFT JOIN memory_proposal p ON p.turn_id = t.id"
            " WHERE t.conversation_mode = 'personal' AND t.status = 'completed'",
            (capture_note,),
        )
        row = cur.fetchone()
        assert row is not None
        return {"turns": row["turns"], "proposed": row["proposed"], "failed": row["failed"]}

    def source_message_content(self, message_id: uuid.UUID) -> str | None:
        cur = self._uow.execute("SELECT content FROM message WHERE id = %s", (message_id,))
        row = cur.fetchone()
        return None if row is None else str(row["content"])
