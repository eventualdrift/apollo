"""The direct-entry memory API (spec D.4, as clarified by D.9/5).

This module is the surface the CLI calls and any later HTTP route must call.
It owns the transaction: it opens the unit of work, asks the lifecycle to make
the change, and records the audit event in the same transaction, so both
commit or neither does (spec C.10, ADR-0004).

Direct entry is always `origin_tier = user_asserted`,
`source_kind = user_direct_entry`, `origin = personal`, and it is the only route
to `scope = self`. Proposal-based creation arrives with step 12; fixture
memories for the retrieval eval arrive with step 11. Neither is reachable here.

Nothing here retrieves or renders a memory into context: that is step 11.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from apollo.audit.events import Actor, AuditEvent, EventType
from apollo.memory import lifecycle
from apollo.memory.lifecycle import Change, MemoryNotFoundError, Operation
from apollo.memory.models import (
    Memory,
    Origin,
    OriginTier,
    SourceKind,
    Status,
    parse_kind,
    parse_scope,
)
from apollo.storage.db import Database
from apollo.storage.repositories import MemoryRepository
from apollo.storage.unit_of_work import UnitOfWork, unit_of_work

DIRECT = SourceKind.USER_DIRECT_ENTRY

_EVENTS = {
    Operation.CREATE: EventType.MEMORY_CREATED,
    Operation.CONFIRM: EventType.MEMORY_CONFIRMED,
    Operation.CONTRADICT: EventType.MEMORY_CONTRADICTED,
    Operation.CORRECT: EventType.MEMORY_SUPERSEDED,
    Operation.ARCHIVE: EventType.MEMORY_ARCHIVED,
    Operation.RESTORE: EventType.MEMORY_RESTORED,
}


def _record(uow: UnitOfWork, change: Change, now: datetime, **extra: Any) -> None:
    payload: dict[str, Any] = {"status": str(change.status), **extra}
    if change.observation_id is not None:
        payload["observation_id"] = str(change.observation_id)
        payload["source_kind"] = str(DIRECT)
    if change.replacement_id is not None:
        payload["replacement_id"] = str(change.replacement_id)
    uow.record(
        AuditEvent(
            event_type=_EVENTS[change.operation],
            actor=Actor.USER,
            subject_kind="memory",
            subject_id=change.memory_id,
            occurred_at=now,
            payload=payload,
        )
    )


def _required(value: uuid.UUID | None) -> uuid.UUID:
    if value is None:  # the lifecycle always sets it for these operations
        raise RuntimeError("lifecycle change is missing an id it always sets")
    return value


def create_memory(
    db: Database,
    *,
    scope: str,
    kind: str,
    subject: str,
    content: str,
    now: datetime,
    pinned: bool = False,
) -> uuid.UUID:
    """A user-asserted memory from direct entry. Returns its id."""
    parsed_scope, parsed_kind = parse_scope(scope), parse_kind(kind)
    with unit_of_work(db) as uow:
        change = lifecycle.create(
            uow,
            scope=parsed_scope,
            kind=parsed_kind,
            subject=subject,
            content=content,
            origin_tier=OriginTier.USER_ASSERTED,
            origin=Origin.PERSONAL,
            pinned=pinned,
            source_kind=DIRECT,
            now=now,
        )
        _record(
            uow,
            change,
            now,
            scope=str(parsed_scope),
            kind=str(parsed_kind),
            origin_tier=str(OriginTier.USER_ASSERTED),
            origin=str(Origin.PERSONAL),
            pinned=pinned,
        )
    return change.memory_id


def confirm_memory(db: Database, memory_id: uuid.UUID, *, now: datetime) -> uuid.UUID:
    """Adds a `confirms` observation. Returns the observation id."""
    with unit_of_work(db) as uow:
        change = lifecycle.confirm(uow, memory_id, source_kind=DIRECT, now=now)
        _record(uow, change, now)
    return _required(change.observation_id)


def contradict_memory(db: Database, memory_id: uuid.UUID, *, now: datetime) -> uuid.UUID:
    """Adds a `contradicts` observation; the status does not change. Returns its id."""
    with unit_of_work(db) as uow:
        change = lifecycle.contradict(uow, memory_id, source_kind=DIRECT, now=now)
        _record(uow, change, now)
    return _required(change.observation_id)


def correct_memory(db: Database, memory_id: uuid.UUID, *, content: str, now: datetime) -> uuid.UUID:
    """Supersedes the memory with corrected content. Returns the new row's id."""
    with unit_of_work(db) as uow:
        change = lifecycle.correct(uow, memory_id, content=content, source_kind=DIRECT, now=now)
        _record(uow, change, now)
    return _required(change.replacement_id)


def archive_memory(db: Database, memory_id: uuid.UUID, *, now: datetime) -> None:
    with unit_of_work(db) as uow:
        _record(uow, lifecycle.archive(uow, memory_id, now=now), now)


def restore_memory(db: Database, memory_id: uuid.UUID, *, now: datetime) -> None:
    with unit_of_work(db) as uow:
        _record(uow, lifecycle.restore(uow, memory_id, now=now), now)


def get_memory(db: Database, memory_id: uuid.UUID) -> Memory:
    with unit_of_work(db, expect_audit=False) as uow:
        repo = MemoryRepository(uow)
        row = repo.get(memory_id)
        if row is None:
            raise MemoryNotFoundError("no memory has that id")
        return Memory.from_row(row, repo.observation_counts(memory_id))


def list_memories(db: Database, *, statuses: tuple[Status, ...] = (Status.ACTIVE,)) -> list[Memory]:
    with unit_of_work(db, expect_audit=False) as uow:
        repo = MemoryRepository(uow)
        return [
            Memory.from_row(row, repo.observation_counts(row["id"]))
            for row in repo.list(tuple(str(s) for s in statuses))
        ]
