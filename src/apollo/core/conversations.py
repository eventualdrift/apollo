"""Conversation lifecycle. The interactive surface creates only `personal` ones."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from apollo.audit.events import Actor, AuditEvent, EventType
from apollo.config import MODE_BENCHMARK, MODE_PERSONAL
from apollo.storage.db import Database
from apollo.storage.repositories import (
    ConversationRepository,
    MessageRepository,
    TurnRepository,
)
from apollo.storage.unit_of_work import unit_of_work


def create_conversation(
    db: Database, *, now: datetime, title: str | None = None
) -> uuid.UUID:
    """The interactive surface. It creates `personal` conversations and nothing else.

    There is deliberately no `mode` parameter: benchmark conversations exist
    only on the eval path (spec K.2), and a parameter is an invitation.
    """
    return _create(db, now=now, title=title, mode=MODE_PERSONAL)


def create_benchmark_conversation(
    db: Database, *, now: datetime, title: str | None = None
) -> uuid.UUID:
    """The eval path's conversation factory. Not reachable from the CLI or API."""
    return _create(db, now=now, title=title, mode=MODE_BENCHMARK)


def _create(
    db: Database, *, now: datetime, title: str | None, mode: str
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


def started_per_day(db: Database) -> list[tuple[date, int]]:
    """Personal conversations started on each UTC day, oldest first (spec O.2)."""
    with unit_of_work(db, expect_audit=False) as uow:
        return ConversationRepository(uow).started_per_day(MODE_PERSONAL)


def transcript(db: Database, conversation_id: uuid.UUID) -> list[dict[str, Any]]:
    """The human-visible transcript, including `system_note` rows."""
    with unit_of_work(db, expect_audit=False) as uow:
        return MessageRepository(uow).transcript(conversation_id)


def turns(db: Database, conversation_id: uuid.UUID) -> list[dict[str, Any]]:
    """A conversation's turns, oldest first: id, status, and how many model calls each made.

    The ids are what `apollo turn replay` takes. No message text is returned.
    """
    with unit_of_work(db, expect_audit=False) as uow:
        return TurnRepository(uow).for_conversation(conversation_id)
