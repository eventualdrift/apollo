"""Transaction semantics: state and audit commit together, or neither does."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from apollo.audit.events import Actor, AuditEvent, AuditPayloadError, EventType
from apollo.storage.db import (
    Database,
    TransactionHeldError,
    assert_no_open_transaction,
    open_transaction_depth,
)
from apollo.storage.repositories import ConversationRepository, MessageRepository
from apollo.storage.unit_of_work import UnitOfWork, UnitOfWorkError, unit_of_work

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _count(db: Database, table: str) -> int:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM {table}")
        return cur.fetchone()["n"]


def test_state_and_audit_commit_together(db: Database) -> None:
    with unit_of_work(db) as uow:
        conv = ConversationRepository(uow).create(mode="personal", now=NOW)
        uow.record(
            AuditEvent(
                event_type=EventType.CONVERSATION_CREATED,
                actor=Actor.USER,
                subject_kind="conversation",
                subject_id=conv,
                conversation_id=conv,
                occurred_at=NOW,
                payload={"mode": "personal"},
            )
        )
    assert _count(db, "conversation") == 1
    assert _count(db, "audit_event") == 1


def test_rollback_discards_both(db: Database) -> None:
    with pytest.raises(RuntimeError), unit_of_work(db) as uow:
        conv = ConversationRepository(uow).create(mode="personal", now=NOW)
        uow.record(
            AuditEvent(
                event_type=EventType.CONVERSATION_CREATED,
                actor=Actor.USER,
                subject_kind="conversation",
                subject_id=conv,
                occurred_at=NOW,
                payload={},
            )
        )
        raise RuntimeError("forced failure after both writes")
    assert _count(db, "conversation") == 0
    assert _count(db, "audit_event") == 0


def test_forced_failure_between_state_and_audit_leaves_neither(db: Database) -> None:
    """The spec O.1/21 case, exercised at the point the audit flush happens."""
    with pytest.raises(RuntimeError), unit_of_work(db) as uow:
        ConversationRepository(uow).create(mode="personal", now=NOW)
        # Failure occurs after the state write, before any event is recorded.
        raise RuntimeError("crash before audit")
    assert _count(db, "conversation") == 0
    assert _count(db, "audit_event") == 0


def test_mutating_without_recording_an_event_is_refused(db: Database) -> None:
    with pytest.raises(UnitOfWorkError, match="without recording an audit event"), \
            unit_of_work(db) as uow:
        ConversationRepository(uow).create(mode="personal", now=NOW)
    assert _count(db, "conversation") == 0


def test_message_and_turn_creation_are_atomic(db: Database) -> None:
    """A committed message with no turn would be a continuity gap (spec A.2)."""
    from apollo.storage.repositories import TurnRepository

    with pytest.raises(RuntimeError), unit_of_work(db) as uow:
        conv = ConversationRepository(uow).create(mode="personal", now=NOW)
        msg = MessageRepository(uow).append(
            conversation_id=conv, role="user", content="hello", now=NOW
        )
        TurnRepository(uow).open(
            conversation_id=conv,
            request_message_id=msg["id"],
            conversation_mode="personal",
            identity_version="v1",
            identity_hash="h",
            now=NOW,
        )
        raise RuntimeError("crash between message and turn commit")
    assert _count(db, "message") == 0
    assert _count(db, "turn") == 0


def test_transaction_depth_is_tracked_and_asserts(db: Database) -> None:
    assert open_transaction_depth() == 0
    assert_no_open_transaction("outside")  # does not raise
    with unit_of_work(db, expect_audit=False) as uow:
        assert open_transaction_depth() == 1
        with pytest.raises(TransactionHeldError, match="no transaction may span a model call"):
            assert_no_open_transaction("adapter entry")
        uow.execute("SELECT 1")
    assert open_transaction_depth() == 0


def test_audit_payload_rejects_content(db: Database) -> None:
    with pytest.raises(AuditPayloadError, match="not metadata"):
        AuditEvent(
            event_type=EventType.MESSAGE_CREATED,
            actor=Actor.USER,
            subject_kind="message",
            occurred_at=NOW,
            payload={"content": "my door code is 4123"},
        )


def test_audit_payload_rejects_long_strings(db: Database) -> None:
    with pytest.raises(AuditPayloadError, match="too long to be metadata"):
        AuditEvent(
            event_type=EventType.MESSAGE_CREATED,
            actor=Actor.USER,
            subject_kind="message",
            occurred_at=NOW,
            payload={"reason": "x" * 200},
        )


def test_application_role_cannot_modify_the_audit_stream(fresh_database) -> None:
    """Append-only enforced by grants, not by application discipline (spec H.3)."""
    owner, app = fresh_database["owner_dsn"], fresh_database["app_dsn"]
    with psycopg.connect(owner, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO audit_event (event_type, occurred_at, actor, subject_kind, payload)"
            " VALUES ('turn.started', now(), 'apollo_core', 'turn', '{}'::jsonb)"
        )
    with psycopg.connect(app, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM audit_event")  # SELECT is granted
        cur.execute(
            "INSERT INTO audit_event (event_type, occurred_at, actor, subject_kind, payload)"
            " VALUES ('turn.started', now(), 'apollo_core', 'turn', '{}'::jsonb)"
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("UPDATE audit_event SET event_type = 'turn.failed'")
    with psycopg.connect(app, autocommit=True) as conn, conn.cursor() as cur, \
            pytest.raises(psycopg.errors.InsufficientPrivilege):
        cur.execute("DELETE FROM audit_event")


def test_messages_are_write_once(db: Database) -> None:
    with unit_of_work(db, expect_audit=False) as uow:
        conv = ConversationRepository(uow).create(mode="personal", now=NOW)
        MessageRepository(uow).append(
            conversation_id=conv, role="user", content="hello", now=NOW
        )
    with db.connect() as conn, conn.cursor() as cur, \
            pytest.raises(psycopg.errors.RestrictViolation, match="write-once"):
        cur.execute("UPDATE message SET content = 'tampered'")


def test_conversation_mode_is_immutable(db: Database) -> None:
    with unit_of_work(db, expect_audit=False) as uow:
        ConversationRepository(uow).create(mode="personal", now=NOW)
    with db.connect() as conn, conn.cursor() as cur, \
            pytest.raises(psycopg.errors.RestrictViolation, match="mode is write-once"):
        cur.execute("UPDATE conversation SET mode = 'benchmark'")


# ---------------------------------------------------------------------------
# P1-3: the failure mode ADR-0004 exists to prevent.
#
# The reviewer moved audit flushing outside `conn.transaction()` and every test
# above stayed green, because none of them force a failure *during* the flush.
# They fail before it (rollback discards both, trivially) or not at all (both
# commit, in either arrangement). The discriminating scenario is a failure while
# the flush is running: if the flush sits outside the state transaction, the
# state has already committed by then and survives.
# ---------------------------------------------------------------------------


def _counts(db: Database) -> tuple[int, int]:
    """Read from a *separate* connection, so nothing in-flight is visible."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM conversation")
        conversations = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM audit_event")
        audit = cur.fetchone()["n"]
    return conversations, audit


def test_a_failure_during_audit_flush_rolls_back_the_state_write(db: Database, monkeypatch):
    """State and audit must roll back together when the flush itself fails.

    This is the regression guard for ADR-0004. It fails if audit flushing is
    ever moved outside the state transaction: the conversation row would still
    be there afterwards.
    """
    observed: dict[str, object] = {}

    def exploding_flush(self) -> None:
        # Recorded, not asserted here, so a mutation produces a clear failure
        # on the count assertions rather than an opaque one inside the patch.
        observed["transaction_status"] = self.connection.info.transaction_status
        observed["called"] = True
        raise RuntimeError("forced failure during audit flush")

    monkeypatch.setattr(UnitOfWork, "_flush_audit", exploding_flush)

    with pytest.raises(RuntimeError, match="during audit flush"), unit_of_work(db) as uow:
        conversation_id = ConversationRepository(uow).create(mode="personal", now=NOW)
        uow.record(
            AuditEvent(
                event_type=EventType.CONVERSATION_CREATED,
                actor=Actor.USER,
                subject_kind="conversation",
                subject_id=conversation_id,
                conversation_id=conversation_id,
                occurred_at=NOW,
                payload={"mode": "personal"},
            )
        )

    assert observed.get("called"), "the audit flush was never reached"
    # PostgreSQL must still be inside the state transaction while flushing.
    assert observed["transaction_status"] == psycopg.pq.TransactionStatus.INTRANS, (
        "audit flushing ran outside the state transaction: the state write had "
        "already committed, so the two can no longer roll back together"
    )

    conversations, audit = _counts(db)
    assert conversations == 0, (
        "the state row survived a failed audit flush — state and audit are no "
        "longer atomic (ADR-0004)"
    )
    assert audit == 0


def test_a_failure_after_a_partial_audit_flush_rolls_back_everything(db: Database, monkeypatch):
    """Even a flush that writes some events before failing must leave nothing."""
    original = UnitOfWork._flush_audit

    def partial_flush(self) -> None:
        original(self)  # the events really are written, inside the transaction
        raise RuntimeError("forced failure after audit flush")

    monkeypatch.setattr(UnitOfWork, "_flush_audit", partial_flush)

    with pytest.raises(RuntimeError, match="after audit flush"), unit_of_work(db) as uow:
        conversation_id = ConversationRepository(uow).create(mode="personal", now=NOW)
        uow.record(
            AuditEvent(
                event_type=EventType.CONVERSATION_CREATED,
                actor=Actor.USER,
                subject_kind="conversation",
                subject_id=conversation_id,
                occurred_at=NOW,
                payload={"mode": "personal"},
            )
        )

    assert _counts(db) == (0, 0)


def test_the_happy_path_still_commits_both(db: Database) -> None:
    """The counterpart: with no failure, both halves are durable."""
    with unit_of_work(db) as uow:
        conversation_id = ConversationRepository(uow).create(mode="personal", now=NOW)
        uow.record(
            AuditEvent(
                event_type=EventType.CONVERSATION_CREATED,
                actor=Actor.USER,
                subject_kind="conversation",
                subject_id=conversation_id,
                conversation_id=conversation_id,
                occurred_at=NOW,
                payload={"mode": "personal"},
            )
        )
    assert _counts(db) == (1, 1)
