"""Conversation lifecycle. The interactive surface creates only `personal` ones."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from apollo.audit.events import Actor, AuditEvent, EventType
from apollo.config import MODE_PERSONAL
from apollo.storage.db import Database
from apollo.storage.repositories import ConversationRepository, MessageRepository
from apollo.storage.unit_of_work import unit_of_work


def create_conversation(
    db: Database, *, now: datetime, title: str | None = None, mode: str = MODE_PERSONAL
) -> uuid.UUID:
    with unit_of_work(db) as uow:
        conversation_id = ConversationRepository(uow).create(mode=mode, now=now, title=title)
        uow.record(
            AuditEvent(
                event_type=EventType.CONVERSATION_CREATED,
                actor=Actor.USER,
                subject_kind="conversation",
                subject_id=conversation_id,
                conversation_id=conversation_id,
                occurred_at=now,
                payload={"mode": mode},
            )
        )
    return conversation_id


def get_conversation(db: Database, conversation_id: uuid.UUID) -> dict[str, Any] | None:
    with unit_of_work(db, expect_audit=False) as uow:
        return ConversationRepository(uow).get(conversation_id)


def transcript(db: Database, conversation_id: uuid.UUID) -> list[dict[str, Any]]:
    """The human-visible transcript, including `system_note` rows."""
    with unit_of_work(db, expect_audit=False) as uow:
        return MessageRepository(uow).transcript(conversation_id)
