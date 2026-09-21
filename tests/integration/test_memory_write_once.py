"""Tombstoning may clear the claim text and nothing else (independent review P2-1).

0001 attached one write-once trigger with `WHEN (NEW.status <> 'tombstoned')`,
which exempted the whole UPDATE while tombstoning — so a single statement could
tombstone a claim *and* rewrite its provenance. 0002 splits the responsibility:
classification and provenance are write-once in every transition, and only the
claim text may be cleared, and only by a tombstone.

Memory lifecycle logic arrives in M3. These tests work directly against the
schema, which is where the guarantee lives.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import psycopg
import pytest

from apollo.storage.db import Database

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _insert_claim(db: Database) -> uuid.UUID:
    memory_id = uuid.uuid4()
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO memory (id, scope, kind, subject, content, origin_tier, origin,"
            " status, created_at, updated_at)"
            " VALUES (%s, 'relationship', 'preference', 'communication',"
            " 'Janu prefers direct disagreement.', 'user_asserted', 'personal', 'active', %s, %s)",
            (memory_id, NOW, NOW),
        )
    return memory_id


def _tombstone_with(db: Database, memory_id: uuid.UUID, extra: str = "") -> None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE memory SET status = 'tombstoned', subject = NULL, content = NULL,"
            f" tombstoned_at = %s, updated_at = %s {extra} WHERE id = %s",
            (NOW, NOW, memory_id),
        )


def test_a_valid_tombstone_succeeds(db: Database) -> None:
    memory_id = _insert_claim(db)
    _tombstone_with(db, memory_id)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, subject, content, origin_tier, scope FROM memory WHERE id = %s",
            (memory_id,),
        )
        row = cur.fetchone()
    assert row["status"] == "tombstoned"
    assert row["subject"] is None and row["content"] is None
    # Provenance survives the deletion of the claim it describes.
    assert row["origin_tier"] == "user_asserted"
    assert row["scope"] == "relationship"


@pytest.mark.parametrize(
    "field,value",
    [
        ("origin_tier", "'model_inferred'"),
        ("scope", "'world'"),
        ("kind", "'fact'"),
        ("origin", "'fixture'"),
        ("created_at", "'2020-01-01T00:00:00+00'"),
    ],
)
def test_tombstoning_cannot_mutate_a_write_once_field(db: Database, field: str,
                                                      value: str) -> None:
    """The reviewer's exploit: one UPDATE that tombstones and rewrites provenance."""
    memory_id = _insert_claim(db)
    with pytest.raises(psycopg.errors.RestrictViolation, match=f"memory.{field} is write-once"):
        _tombstone_with(db, memory_id, extra=f", {field} = {value}")

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT status, origin_tier FROM memory WHERE id = %s", (memory_id,))
        row = cur.fetchone()
    assert row["status"] == "active", "the failed tombstone must not have partly applied"
    assert row["origin_tier"] == "user_asserted"


def test_provenance_is_write_once_outside_a_tombstone_too(db: Database) -> None:
    memory_id = _insert_claim(db)
    with db.connect() as conn, conn.cursor() as cur, \
            pytest.raises(psycopg.errors.RestrictViolation, match="origin_tier is write-once"):
        cur.execute(
            "UPDATE memory SET origin_tier = 'model_inferred' WHERE id = %s", (memory_id,)
        )


def test_claim_text_is_write_once_outside_a_tombstone(db: Database) -> None:
    memory_id = _insert_claim(db)
    with db.connect() as conn, conn.cursor() as cur, \
            pytest.raises(psycopg.errors.RestrictViolation, match="content is write-once"):
        cur.execute("UPDATE memory SET content = 'rewritten' WHERE id = %s", (memory_id,))


def test_a_tombstone_cannot_write_new_text_instead_of_clearing(db: Database) -> None:
    """Clearing is permitted; substituting is not (held by the CHECK from 0001)."""
    memory_id = _insert_claim(db)
    with db.connect() as conn, conn.cursor() as cur, \
            pytest.raises(psycopg.errors.CheckViolation, match="memory_content_presence_ck"):
        cur.execute(
            "UPDATE memory SET status = 'tombstoned', content = 'substituted', subject = NULL,"
            " tombstoned_at = %s WHERE id = %s",
            (NOW, memory_id),
        )


def test_a_tombstone_is_terminal_for_the_claim_text(db: Database) -> None:
    """A tombstoned row cannot be revived with text — the trigger catches the return."""
    memory_id = _insert_claim(db)
    _tombstone_with(db, memory_id)
    with db.connect() as conn, conn.cursor() as cur, \
            pytest.raises(psycopg.errors.RestrictViolation, match="is write-once"):
        cur.execute(
            "UPDATE memory SET status = 'active', subject = 'communication',"
            " content = 'forged revival' WHERE id = %s",
            (memory_id,),
        )


def test_lifecycle_fields_remain_mutable(db: Database) -> None:
    """The narrow invariant: claim content write-once, lifecycle metadata not."""
    memory_id = _insert_claim(db)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE memory SET status = 'archived', archived_at = %s, pinned = true,"
            " last_confirmed_at = %s, updated_at = %s WHERE id = %s",
            (NOW, NOW, NOW, memory_id),
        )
        cur.execute("SELECT status, pinned FROM memory WHERE id = %s", (memory_id,))
        row = cur.fetchone()
    assert row["status"] == "archived" and row["pinned"] is True


def test_both_triggers_are_installed(db: Database) -> None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = 'memory'::regclass"
            " AND NOT tgisinternal"
        )
        names = {r["tgname"] for r in cur.fetchall()}
    assert "memory_provenance_write_once" in names
    assert "memory_claim_text_write_once" in names
    assert "memory_claim_write_once" not in names  # superseded by 0002
