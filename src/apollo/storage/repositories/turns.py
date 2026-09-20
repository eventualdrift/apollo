from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from apollo.storage.ids import uuid7
from apollo.storage.unit_of_work import UnitOfWork


class TurnRepository:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def open(
        self,
        *,
        conversation_id: uuid.UUID,
        request_message_id: uuid.UUID,
        conversation_mode: str,
        identity_version: str,
        identity_hash: str,
        now: datetime,
    ) -> uuid.UUID:
        turn_id = uuid7()
        self._uow.execute(
            "INSERT INTO turn (id, conversation_id, request_message_id, status,"
            "  conversation_mode, identity_version, identity_hash, started_at)"
            " VALUES (%s, %s, %s, 'started', %s, %s, %s, %s)",
            (
                turn_id,
                conversation_id,
                request_message_id,
                conversation_mode,
                identity_version,
                identity_hash,
                now,
            ),
        )
        return turn_id

    def complete(
        self,
        *,
        turn_id: uuid.UUID,
        response_message_id: uuid.UUID,
        now: datetime,
        latency_ms: int,
    ) -> None:
        self._uow.execute(
            "UPDATE turn SET status = 'completed', response_message_id = %s,"
            "  completed_at = %s, latency_ms = %s WHERE id = %s AND status = 'started'",
            (response_message_id, now, latency_ms, turn_id),
        )

    def fail(
        self,
        *,
        turn_id: uuid.UUID,
        now: datetime,
        error_kind: str,
        error_detail: str | None,
        latency_ms: int | None = None,
    ) -> None:
        self._uow.execute(
            "UPDATE turn SET status = 'failed', completed_at = %s, latency_ms = %s,"
            "  error_kind = %s, error_detail = %s WHERE id = %s AND status = 'started'",
            (now, latency_ms, error_kind, error_detail, turn_id),
        )

    def get(self, turn_id: uuid.UUID) -> dict[str, Any] | None:
        cur = self._uow.execute("SELECT * FROM turn WHERE id = %s", (turn_id,))
        row: dict[str, Any] | None = cur.fetchone()
        return row

    def find_by_request_message(self, message_id: uuid.UUID) -> dict[str, Any] | None:
        cur = self._uow.execute("SELECT * FROM turn WHERE request_message_id = %s", (message_id,))
        row: dict[str, Any] | None = cur.fetchone()
        return row

    def expire_orphans(self, *, older_than: datetime, now: datetime) -> int:
        """Turns left `started` beyond the window become `interrupted` (spec A.2)."""
        cur = self._uow.execute(
            "UPDATE turn SET status = 'failed', completed_at = %s, error_kind = 'interrupted'"
            " WHERE status = 'started' AND started_at < %s RETURNING id",
            (now, older_than),
        )
        return len(cur.fetchall())
