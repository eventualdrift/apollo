"""The runtime role is genuinely least-privilege (independent review P1-2).

These tests start from the documented provisioning path — the same
`provision_runtime_role` that `apollo provision` calls — and prove the role
Apollo actually runs as cannot tamper with the audit stream.

The point of the finding was that a least-privilege role existing only in tests
proves nothing. So the `db` fixture, used by every other integration test, is
now that same runtime role.
"""

from __future__ import annotations

import psycopg
import pytest

from apollo.audit.events import Actor, AuditEvent, EventType
from apollo.core.conversations import create_conversation
from apollo.storage.db import Database, describe_role_privileges
from apollo.storage.repositories import ConversationRepository
from apollo.storage.unit_of_work import unit_of_work

pytestmark = pytest.mark.integration


def test_apollo_runs_as_the_runtime_role_not_the_owner(db: Database, fresh_database) -> None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT current_user AS who")
        assert cur.fetchone()["who"] == fresh_database["role"]


def test_the_runtime_role_is_not_superuser_and_owns_nothing(db, fresh_database) -> None:
    with db.connect() as conn:
        facts = describe_role_privileges(conn, fresh_database["role"])
    assert facts.is_superuser is False
    assert facts.can_create_db is False
    assert facts.can_create_role is False
    assert facts.owns_audit_event is False
    assert facts.audit_event_owner != fresh_database["role"]
    assert facts.audit_event_privileges == ("INSERT", "SELECT")


def test_normal_apollo_mechanisms_can_write_audit_events(db: Database, clock) -> None:
    """Insert works through the ordinary path — least privilege is not over-tight."""
    conversation_id = create_conversation(db, now=clock())
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM audit_event WHERE conversation_id = %s",
                    (conversation_id,))
        assert cur.fetchone()["n"] == 1

    with unit_of_work(db) as uow:
        ConversationRepository(uow).touch(conversation_id, clock())
        uow.record(
            AuditEvent(
                event_type=EventType.TURN_STARTED,
                actor=Actor.APOLLO_CORE,
                subject_kind="turn",
                occurred_at=clock(),
                conversation_id=conversation_id,
                payload={"mode": "personal"},
            )
        )
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM audit_event")
        assert cur.fetchone()["n"] == 2


def test_the_runtime_role_cannot_update_the_audit_stream(db: Database, clock) -> None:
    create_conversation(db, now=clock())
    with db.connect() as conn, conn.cursor() as cur, \
            pytest.raises(psycopg.errors.InsufficientPrivilege):
        cur.execute("UPDATE audit_event SET event_type = 'turn.failed'")


def test_the_runtime_role_cannot_delete_from_the_audit_stream(db: Database, clock) -> None:
    create_conversation(db, now=clock())
    with db.connect() as conn, conn.cursor() as cur, \
            pytest.raises(psycopg.errors.InsufficientPrivilege):
        cur.execute("DELETE FROM audit_event")


def test_the_runtime_role_cannot_truncate_or_drop_the_audit_stream(db: Database) -> None:
    for statement in ("TRUNCATE audit_event", "DROP TABLE audit_event"):
        with db.connect() as conn, conn.cursor() as cur, \
                pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(statement)


def test_the_runtime_role_has_no_delete_on_state_tables_either(db: Database, clock) -> None:
    """Apollo has no delete path; withholding DELETE makes an accidental one fail loudly."""
    create_conversation(db, now=clock())
    with db.connect() as conn, conn.cursor() as cur, \
            pytest.raises(psycopg.errors.InsufficientPrivilege):
        cur.execute("DELETE FROM conversation")


def test_the_owner_is_a_different_role_that_can_migrate(owner_db: Database,
                                                        fresh_database) -> None:
    with owner_db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT current_user AS who")
        owner = cur.fetchone()["who"]
    assert owner != fresh_database["role"]
    assert owner_db.migrate() == []  # the owner may, and it is idempotent


def test_provisioning_is_idempotent(owner_db: Database, fresh_database) -> None:
    from apollo.storage.db import provision_runtime_role

    role = fresh_database["role"]
    with owner_db.connect() as conn:
        assert provision_runtime_role(conn, role) is False  # already exists
        facts = describe_role_privileges(conn, role)
    assert facts.audit_event_privileges == ("INSERT", "SELECT")
