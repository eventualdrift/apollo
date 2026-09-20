"""The Unit of Work — what makes state-plus-audit atomicity a mechanism.

Spec C.10 and ADR-0004: every audit event describing a mutation of Apollo's own
Postgres state is written in the same transaction as that mutation. Both commit
or neither does.

The mechanism, rather than the habit:

* A repository can only be constructed around a live `UnitOfWork`, so there is
  no way to write state outside a transaction.
* Audit events are buffered on the unit of work and flushed *inside* the same
  transaction, immediately before commit. A rollback discards both.
* A unit of work refuses to commit if it mutated state and recorded nothing,
  which catches the "forgot the audit event" mistake at the point it is made
  rather than during a later investigation.
* While any unit of work is open, `assert_no_open_transaction` raises, which is
  how "no transaction spans a model call" is enforced.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

import psycopg

from apollo.errors import ApolloError
from apollo.storage.db import Database, _enter_transaction, _exit_transaction

log = logging.getLogger(__name__)


@runtime_checkable
class AuditRecord(Protocol):
    """What `storage/` needs to know about an audit event, and no more.

    Spec A.3 holds `storage/ -> nothing internal`, while spec M places the unit
    of work — the thing that makes state-plus-audit atomicity a mechanism — in
    `storage/`. A structural protocol satisfies both: storage knows the shape of
    a row it must write, and imports nothing from `audit/`. The concrete
    `apollo.audit.events.AuditEvent` satisfies this without declaring it.
    """

    event_type: Any
    occurred_at: datetime
    actor: Any
    subject_kind: str
    subject_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    turn_id: uuid.UUID | None
    payload: dict[str, Any]


class UnitOfWorkError(ApolloError):
    pass


class UnitOfWork:
    """A single database transaction plus its audit events."""

    def __init__(self, conn: psycopg.Connection, *, expect_audit: bool = True) -> None:
        self._conn = conn
        self._events: list[AuditRecord] = []
        self._mutated = False
        self._closed = False
        self._expect_audit = expect_audit

    @property
    def connection(self) -> psycopg.Connection:
        self._check_open()
        return self._conn

    def record(self, event: AuditRecord) -> None:
        """Buffer an audit event. It commits with the state change or not at all."""
        self._check_open()
        self._events.append(event)

    def execute(self, query: str, params: tuple[Any, ...] | dict[str, Any] | None = None) -> Any:
        """Run a statement, marking the unit of work as having mutated state."""
        self._check_open()
        upper = query.lstrip()[:6].upper()
        if upper in {"INSERT", "UPDATE", "DELETE"}:
            self._mutated = True
        cur = self._conn.cursor()
        cur.execute(query, params)  # type: ignore[arg-type]
        return cur

    def _check_open(self) -> None:
        if self._closed:
            raise UnitOfWorkError("unit of work is closed")

    def _flush_audit(self) -> None:
        if not self._events:
            return
        with self._conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO audit_event (event_type, occurred_at, actor, subject_kind,"
                " subject_id, conversation_id, turn_id, payload)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        str(e.event_type),
                        e.occurred_at,
                        str(e.actor),
                        e.subject_kind,
                        e.subject_id,
                        e.conversation_id,
                        e.turn_id,
                        json.dumps(e.payload, sort_keys=True),
                    )
                    for e in self._events
                ],
            )

    def _before_commit(self) -> None:
        if self._mutated and self._expect_audit and not self._events:
            raise UnitOfWorkError(
                "state was mutated without recording an audit event; "
                "pass expect_audit=False only for genuinely non-auditable work"
            )
        self._flush_audit()


@contextmanager
def unit_of_work(db: Database, *, expect_audit: bool = True) -> Iterator[UnitOfWork]:
    """Open a transaction. Audit events flush inside it, immediately before commit."""
    with db.connect() as conn:
        uow = UnitOfWork(conn, expect_audit=expect_audit)
        _enter_transaction()
        try:
            with conn.transaction():
                yield uow
                uow._before_commit()
        finally:
            uow._closed = True
            _exit_transaction()


@contextmanager
def unit_of_work_on(
    conn: psycopg.Connection, *, expect_audit: bool = True
) -> Iterator[UnitOfWork]:
    """A unit of work on a caller-owned connection, for tests and CLI sessions."""
    uow = UnitOfWork(conn, expect_audit=expect_audit)
    _enter_transaction()
    try:
        with conn.transaction():
            yield uow
            uow._before_commit()
    finally:
        uow._closed = True
        _exit_transaction()
