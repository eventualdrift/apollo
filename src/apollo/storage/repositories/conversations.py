from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from apollo.storage.ids import uuid7
from apollo.storage.unit_of_work import UnitOfWork


class ConversationRepository:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def create(self, *, mode: str, now: datetime, title: str | None = None) -> uuid.UUID:
        conversation_id = uuid7()
        self._uow.execute(
            "INSERT INTO conversation (id, title, mode, started_at, last_active_at)"
            " VALUES (%s, %s, %s, %s, %s)",
            (conversation_id, title, mode, now, now),
        )
        return conversation_id

    def get(self, conversation_id: uuid.UUID) -> dict[str, Any] | None:
        cur = self._uow.execute("SELECT * FROM conversation WHERE id = %s", (conversation_id,))
        return cur.fetchone()

    def touch(self, conversation_id: uuid.UUID, now: datetime) -> None:
        self._uow.execute(
            "UPDATE conversation SET last_active_at = %s WHERE id = %s", (now, conversation_id)
        )
