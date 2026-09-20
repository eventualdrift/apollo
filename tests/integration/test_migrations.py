"""Migrations against a real, empty PostgreSQL database."""

from __future__ import annotations

import pytest

from apollo.storage.db import Database

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "audit_event",
    "conversation",
    "identity_version",
    "memory",
    "memory_observation",
    "memory_proposal",
    "message",
    "model_invocation",
    "turn",
    "turn_retrieval",
    "turn_retrieval_result",
}


def _rows(db: Database, sql: str, params=()) -> list[dict]:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def test_empty_database_gets_the_eleven_table_schema(db: Database) -> None:
    names = {
        r["tablename"]
        for r in _rows(db, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    }
    assert EXPECTED_TABLES <= names
    assert len(EXPECTED_TABLES & names) == 11


def test_migrations_are_repeatable(db: Database) -> None:
    assert db.migrate() == []  # already applied by the fixture
    assert db.migrate() == []


def test_key_constraints_and_indexes_exist(db: Database) -> None:
    constraints = {
        r["conname"] for r in _rows(db, "SELECT conname FROM pg_constraint WHERE connamespace = "
                                        "'public'::regnamespace")
    }
    for expected in (
        "message_seq_uq",
        "message_idem_uq",
        "invocation_seq_uq",
        "invocation_purpose_ck",
        "proposal_text_transient_ck",
        "retrieval_recency_not_substantive_ck",
        "memory_content_presence_ck",
        "conversation_mode_ck",
    ):
        assert expected in constraints, expected

    indexes = {r["indexname"] for r in _rows(db, "SELECT indexname FROM pg_indexes "
                                                 "WHERE schemaname = 'public'")}
    assert "invocation_manifest_gin" in indexes  # tombstone hash-redaction query
    assert "memory_search_gin" in indexes

    fks = {
        r["conname"]
        for r in _rows(
            db,
            "SELECT conname FROM pg_constraint WHERE contype = 'f'"
            " AND connamespace = 'public'::regnamespace",
        )
    }
    assert len(fks) >= 12, fks


def test_recency_can_never_be_recorded_as_substantive(db: Database) -> None:
    """The anti-confabulation rule (spec E.3), held by the database too."""
    import psycopg

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversation (id, mode, started_at, last_active_at)"
            " VALUES (gen_random_uuid(), 'personal', now(), now()) RETURNING id"
        )
        conv = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO message (id, conversation_id, seq, role, content, created_at)"
            " VALUES (gen_random_uuid(), %s, 1, 'user', 'hello', now()) RETURNING id",
            (conv,),
        )
        msg = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO turn (id, conversation_id, request_message_id, status,"
            " conversation_mode, identity_version, identity_hash, started_at)"
            " VALUES (gen_random_uuid(), %s, %s, 'started', 'personal', 'v', 'h', now())"
            " RETURNING id",
            (conv, msg),
        )
        turn = cur.fetchone()["id"]
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO turn_retrieval (id, turn_id, strategy, result_count,"
                " substantive, duration_ms, executed_at)"
                " VALUES (gen_random_uuid(), %s, 'recency', 3, true, 1, now())",
                (turn,),
            )
