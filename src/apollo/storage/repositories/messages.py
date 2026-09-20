from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from apollo.storage.ids import uuid7
from apollo.storage.unit_of_work import UnitOfWork

#: History is filtered to these roles (spec F.2). `system_note` is durable and
#: human-visible but never re-enters model context.
HISTORY_ROLES = ("user", "apollo")


class MessageRepository:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def append(
        self,
        *,
        conversation_id: uuid.UUID,
        role: str,
        content: str,
        now: datetime,
        turn_id: uuid.UUID | None = None,
        device_id: str | None = None,
        idempotency_key: str | None = None,
        truncated: bool = False,
    ) -> dict[str, Any]:
        message_id = uuid7()
        cur = self._uow.execute(
            "INSERT INTO message (id, conversation_id, seq, role, content, created_at,"
            "  device_id, turn_id, client_idempotency_key, truncated)"
            " SELECT %s, %s, coalesce(max(seq), 0) + 1, %s, %s, %s, %s, %s, %s, %s"
            "   FROM message WHERE conversation_id = %s"
            " RETURNING id, seq, role, created_at",
            (
                message_id,
                conversation_id,
                role,
                content,
                now,
                device_id,
                turn_id,
                idempotency_key,
                truncated,
                conversation_id,
            ),
        )
        row: dict[str, Any] = cur.fetchone()
        return row

    def find_by_idempotency_key(
        self, conversation_id: uuid.UUID, key: str
    ) -> dict[str, Any] | None:
        cur = self._uow.execute(
            "SELECT * FROM message WHERE conversation_id = %s AND client_idempotency_key = %s",
            (conversation_id, key),
        )
        row: dict[str, Any] | None = cur.fetchone()
        return row

    def history(
        self, conversation_id: uuid.UUID, *, before_seq: int | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Conversational history only: `system_note` rows are excluded (spec F.2)."""
        cur = self._uow.execute(
            "SELECT id, seq, role, content, created_at FROM message"
            " WHERE conversation_id = %s AND role = ANY(%s)"
            "   AND (%s::bigint IS NULL OR seq < %s::bigint)"
            " ORDER BY seq DESC LIMIT %s",
            (conversation_id, list(HISTORY_ROLES), before_seq, before_seq, limit),
        )
        rows: list[dict[str, Any]] = cur.fetchall()
        return list(reversed(rows))

    def transcript(self, conversation_id: uuid.UUID) -> list[dict[str, Any]]:
        """Everything, including `system_note`. For the human-visible transcript only."""
        cur = self._uow.execute(
            "SELECT id, seq, role, content, created_at, truncated FROM message"
            " WHERE conversation_id = %s ORDER BY seq",
            (conversation_id,),
        )
        rows: list[dict[str, Any]] = cur.fetchall()
        return rows
