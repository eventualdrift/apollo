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
from apollo.storage.unit_of_work import UnitOfWorkError, unit_of_work

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
    with pytest.raises(UnitOfWorkError, match="without recording an audit event"):
        with unit_of_work(db) as uow:
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
    with psycopg.connect(app, autocommit=True) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("DELETE FROM audit_event")


def test_messages_are_write_once(db: Database) -> None:
    with unit_of_work(db, expect_audit=False) as uow:
        conv = ConversationRepository(uow).create(mode="personal", now=NOW)
        MessageRepository(uow).append(
            conversation_id=conv, role="user", content="hello", now=NOW
        )
    with db.connect() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RestrictViolation, match="write-once"):
            cur.execute("UPDATE message SET content = 'tampered'")


def test_conversation_mode_is_immutable(db: Database) -> None:
    with unit_of_work(db, expect_audit=False) as uow:
        ConversationRepository(uow).create(mode="personal", now=NOW)
    with db.connect() as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RestrictViolation, match="mode is write-once"):
            cur.execute("UPDATE conversation SET mode = 'benchmark'")
