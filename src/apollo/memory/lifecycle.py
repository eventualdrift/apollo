"""The memory lifecycle: which operation may apply to which status (spec D.1, D.9).

This module is where status transitions are decided and made. It holds the
rules as data, so the database triggers (migration 0003) and the tests can be
checked against one table, and it holds the operations that execute them: they
are the only callers of `MemoryRepository`'s writers (an architecture test
holds that). Each operation runs inside the caller's unit of work and returns
what it changed; the caller (`apollo.core.memories`) records the audit event in
the same transaction, because `memory/` may import only `storage/` (spec A.3).

Row-level transitions, per spec D.1 as clarified by D.9:

    active     -> superseded   correct (a new active row replaces it)
    active     -> archived     archive
    archived   -> active       restore
    active     -> tombstoned   tombstone
    archived   -> tombstoned   tombstone (D.9/2)
    superseded -> tombstoned   only as part of a chain tombstone (D.9/1)

`tombstoned` is terminal. Confirm and contradict add evidence and change no
status; they apply to `active` and `archived` rows only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from apollo.errors import ApolloError
from apollo.memory.models import (
    Kind,
    Origin,
    OriginTier,
    Relation,
    Scope,
    SourceKind,
    Status,
    included_manifest_entry,
    validate_claim,
)
from apollo.storage.repositories import InvocationRepository, MemoryRepository
from apollo.storage.unit_of_work import UnitOfWork


class LifecycleError(ApolloError):
    """An operation that the lifecycle does not permit from the row's status."""


class MemoryNotFoundError(ApolloError):
    """No memory has the requested id."""


class Operation(StrEnum):
    CREATE = "create"
    CONFIRM = "confirm"
    CONTRADICT = "contradict"
    CORRECT = "correct"
    ARCHIVE = "archive"
    RESTORE = "restore"
    TOMBSTONE = "tombstone"


#: Every status change a row may undergo. The database trigger in 0003 permits
#: exactly this set; a test holds them together.
ROW_TRANSITIONS: frozenset[tuple[Status, Status]] = frozenset(
    {
        (Status.ACTIVE, Status.SUPERSEDED),
        (Status.ACTIVE, Status.ARCHIVED),
        (Status.ARCHIVED, Status.ACTIVE),
        (Status.ACTIVE, Status.TOMBSTONED),
        (Status.ARCHIVED, Status.TOMBSTONED),
        (Status.SUPERSEDED, Status.TOMBSTONED),
    }
)

#: The status a row must be in for an operation to be requested on it. For
#: `tombstone` this is the chain head; its superseded predecessors follow it.
REQUIRES: dict[Operation, frozenset[Status]] = {
    Operation.CONFIRM: frozenset({Status.ACTIVE, Status.ARCHIVED}),
    Operation.CONTRADICT: frozenset({Status.ACTIVE, Status.ARCHIVED}),
    Operation.CORRECT: frozenset({Status.ACTIVE}),
    Operation.ARCHIVE: frozenset({Status.ACTIVE}),
    Operation.RESTORE: frozenset({Status.ARCHIVED}),
    Operation.TOMBSTONE: frozenset({Status.ACTIVE, Status.ARCHIVED}),
}

#: What the named row becomes. `None`: its status does not change.
RESULTS: dict[Operation, Status | None] = {
    Operation.CONFIRM: None,
    Operation.CONTRADICT: None,
    Operation.CORRECT: Status.SUPERSEDED,
    Operation.ARCHIVE: Status.ARCHIVED,
    Operation.RESTORE: Status.ACTIVE,
    Operation.TOMBSTONE: Status.TOMBSTONED,
}

#: Every row is created active: a direct entry, an approved proposal, or the
#: replacement row of a correction.
CREATED_STATUS = Status.ACTIVE


def check(operation: Operation, status: Status) -> Status:
    """Return the row's status after `operation`, or raise if it is not permitted.

    The message names the operation and the statuses only: never the claim.
    """
    if operation is Operation.CREATE:
        raise LifecycleError("create applies to no existing row")
    allowed = REQUIRES[operation]
    if status not in allowed:
        raise LifecycleError(
            f"cannot {operation} a memory that is {status}; "
            f"it must be {' or '.join(sorted(allowed))}"
        )
    result = RESULTS[operation]
    return status if result is None else result


def check_chain_tombstone(head: Status, predecessors: list[Status]) -> None:
    """Validate a chain tombstone (spec D.9/1) before any row is touched.

    The head must be `active` or `archived`. Every predecessor must be
    `superseded`: that is what makes it a predecessor, so anything else means
    the chain is not what the caller thinks it is.
    """
    check(Operation.TOMBSTONE, head)
    for status in predecessors:
        if status is not Status.SUPERSEDED:
            raise LifecycleError(f"a chain predecessor must be {Status.SUPERSEDED}, not {status}")


# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """What one operation changed: ids and enums only, never claim text."""

    operation: Operation
    memory_id: uuid.UUID
    status: Status
    observation_id: uuid.UUID | None = None
    replacement_id: uuid.UUID | None = None


def _locked(repo: MemoryRepository, memory_id: uuid.UUID) -> dict[str, Any]:
    row = repo.lock(memory_id)
    if row is None:
        raise MemoryNotFoundError("no memory has that id")
    return row


def create(
    uow: UnitOfWork,
    *,
    scope: Scope,
    kind: Kind,
    subject: str,
    content: str,
    origin_tier: OriginTier,
    origin: Origin,
    pinned: bool,
    source_kind: SourceKind,
    now: datetime,
    message_id: uuid.UUID | None = None,
    excerpt: str | None = None,
) -> Change:
    """A new active claim and its `asserts` observation (spec D.1: provenance required)."""
    subject, content = validate_claim(subject, content)
    repo = MemoryRepository(uow)
    memory_id = repo.insert_memory(
        scope=scope,
        kind=kind,
        subject=subject,
        content=content,
        origin_tier=origin_tier,
        origin=origin,
        pinned=pinned,
        now=now,
    )
    observation_id = repo.insert_observation(
        memory_id=memory_id,
        relation=Relation.ASSERTS,
        source_kind=source_kind,
        message_id=message_id,
        excerpt=excerpt,
        now=now,
    )
    return Change(Operation.CREATE, memory_id, CREATED_STATUS, observation_id=observation_id)


def _evidence(
    uow: UnitOfWork,
    operation: Operation,
    memory_id: uuid.UUID,
    *,
    source_kind: SourceKind,
    now: datetime,
    message_id: uuid.UUID | None,
    excerpt: str | None,
) -> Change:
    repo = MemoryRepository(uow)
    status = check(operation, Status(_locked(repo, memory_id)["status"]))
    relation = Relation.CONFIRMS if operation is Operation.CONFIRM else Relation.CONTRADICTS
    observation_id = repo.insert_observation(
        memory_id=memory_id,
        relation=relation,
        source_kind=source_kind,
        message_id=message_id,
        excerpt=excerpt,
        now=now,
    )
    if operation is Operation.CONFIRM:
        repo.mark_confirmed(memory_id, now)
    else:
        repo.mark_contradicted(memory_id, now)
    return Change(operation, memory_id, status, observation_id=observation_id)


def confirm(
    uow: UnitOfWork,
    memory_id: uuid.UUID,
    *,
    source_kind: SourceKind,
    now: datetime,
    message_id: uuid.UUID | None = None,
    excerpt: str | None = None,
) -> Change:
    """Spec D.6: a `confirms` observation and `last_confirmed_at`; no status change."""
    return _evidence(
        uow,
        Operation.CONFIRM,
        memory_id,
        source_kind=source_kind,
        now=now,
        message_id=message_id,
        excerpt=excerpt,
    )


def contradict(
    uow: UnitOfWork,
    memory_id: uuid.UUID,
    *,
    source_kind: SourceKind,
    now: datetime,
    message_id: uuid.UUID | None = None,
    excerpt: str | None = None,
) -> Change:
    """Spec D.6: a `contradicts` observation; never an automatic retraction."""
    return _evidence(
        uow,
        Operation.CONTRADICT,
        memory_id,
        source_kind=source_kind,
        now=now,
        message_id=message_id,
        excerpt=excerpt,
    )


def correct(
    uow: UnitOfWork,
    memory_id: uuid.UUID,
    *,
    content: str,
    source_kind: SourceKind,
    now: datetime,
    message_id: uuid.UUID | None = None,
    excerpt: str | None = None,
) -> Change:
    """Spec D.5: a new active row with the corrected content; the old row superseded.

    scope, kind, subject, origin, origin_tier and pinned carry forward (the
    subject cannot be corrected, D.9/4). Observations are not copied: the chain
    is the history.
    """
    repo = MemoryRepository(uow)
    old = _locked(repo, memory_id)
    check(Operation.CORRECT, Status(old["status"]))
    created = create(
        uow,
        scope=Scope(old["scope"]),
        kind=Kind(old["kind"]),
        subject=old["subject"],
        content=content,
        origin_tier=OriginTier(old["origin_tier"]),
        origin=Origin(old["origin"]),
        pinned=old["pinned"],
        source_kind=source_kind,
        message_id=message_id,
        excerpt=excerpt,
        now=now,
    )
    repo.mark_superseded(memory_id, created.memory_id, now)
    return Change(
        Operation.CORRECT,
        memory_id,
        Status.SUPERSEDED,
        observation_id=created.observation_id,
        replacement_id=created.memory_id,
    )


def archive(uow: UnitOfWork, memory_id: uuid.UUID, *, now: datetime) -> Change:
    """Spec D.7: reversible and content-preserving."""
    repo = MemoryRepository(uow)
    status = check(Operation.ARCHIVE, Status(_locked(repo, memory_id)["status"]))
    repo.mark_archived(memory_id, now)
    return Change(Operation.ARCHIVE, memory_id, status)


def restore(uow: UnitOfWork, memory_id: uuid.UUID, *, now: datetime) -> Change:
    """Back to active, with `archived_at` cleared (step 10 decision 8)."""
    repo = MemoryRepository(uow)
    status = check(Operation.RESTORE, Status(_locked(repo, memory_id)["status"]))
    repo.mark_restored(memory_id, now)
    return Change(Operation.RESTORE, memory_id, status)


@dataclass(frozen=True)
class TombstonedRow:
    memory_id: uuid.UUID
    observations_redacted: int
    invocations_redacted: int


@dataclass(frozen=True)
class Tombstone:
    """What a chain tombstone removed: ids and counts only."""

    requested_id: uuid.UUID
    rows: tuple[TombstonedRow, ...]  # the head first, then each predecessor

    @property
    def head_id(self) -> uuid.UUID:
        return self.rows[0].memory_id


_CHAIN_ATTEMPTS = 3


def _chain_ids(repo: MemoryRepository, memory_id: uuid.UUID) -> list[uuid.UUID]:
    """The chain's ids, read without locks: the head first, then newest to oldest."""
    row = repo.get(memory_id)
    if row is None:
        raise MemoryNotFoundError("no memory has that id")
    while row["superseded_by_id"] is not None:
        row = repo.get(row["superseded_by_id"])
        if row is None:  # superseded_by_id is a foreign key; no row is ever deleted
            raise MemoryNotFoundError("no memory has that id")
    ids = [row["id"]]
    while (predecessor := repo.predecessor(ids[-1])) is not None:
        ids.append(predecessor["id"])
    return ids


def _chain(repo: MemoryRepository, memory_id: uuid.UUID) -> list[dict[str, Any]]:
    """The whole supersession chain, locked: the head first, then newest to oldest.

    A request may name any row of the chain, including a superseded one: the
    user who wants the old "4123" gone will name the old row. It resolves to
    the head, and the head brings every predecessor with it (spec D.9/1).

    The chain is found without locks, then every row is locked at once in id
    order, so two tombstones naming different rows of one chain queue behind
    each other instead of deadlocking. Under the locks the chain is walked
    again; if a correction committed in between and grew it, start over.
    """
    ids = _chain_ids(repo, memory_id)
    for _ in range(_CHAIN_ATTEMPTS):
        locked = repo.lock_many(ids)
        current = _chain_ids(repo, memory_id)
        if current == ids:
            return [locked[row_id] for row_id in ids]
        ids = current
    raise LifecycleError("the memory kept changing while it was being tombstoned; try again")


def tombstone(uow: UnitOfWork, memory_id: uuid.UUID, *, now: datetime) -> Tombstone:
    """Forget a claim: the whole chain, in one transaction (spec D.7, D.9/1).

    For each row, head first (the database requires a successor to go before
    its predecessor): clear subject and content (the generated search vector
    empties with them), clear every observation excerpt, and redact the
    verification hashes of every invocation whose manifest included the row.
    The caller records `memory.tombstoned` in the same transaction; the
    database refuses to commit an incomplete tombstone (migrations 0003, 0004).
    """
    repo = MemoryRepository(uow)
    invocations = InvocationRepository(uow)
    chain = _chain(repo, memory_id)
    check_chain_tombstone(Status(chain[0]["status"]), [Status(row["status"]) for row in chain[1:]])
    rows = []
    for row in chain:
        repo.mark_tombstoned(row["id"], now)
        rows.append(
            TombstonedRow(
                memory_id=row["id"],
                observations_redacted=repo.clear_excerpts(row["id"]),
                invocations_redacted=invocations.redact_hashes_including(
                    included_manifest_entry(row["id"]), now
                ),
            )
        )
    return Tombstone(requested_id=memory_id, rows=tuple(rows))
