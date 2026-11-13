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
    assert names >= EXPECTED_TABLES
    assert len(EXPECTED_TABLES & names) == 11


def test_migrations_are_repeatable(owner_db: Database) -> None:
    """Migrations run as the owner, never as the runtime role."""
    assert owner_db.migrate() == []  # already applied by the fixture
    assert owner_db.migrate() == []


def test_the_runtime_role_cannot_migrate(db: Database) -> None:
    """Least privilege is real: Apollo's own connection cannot alter the schema."""
    import psycopg

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.migrate()


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


# -- a digest per applied migration ---------------------------------------------


def _copy_migrations(tmp_path, monkeypatch):  # noqa: ANN001, ANN202
    """A private copy of the migrations directory the runner will read instead."""
    import shutil

    from apollo.storage import db as db_module

    copy = tmp_path / "migrations"
    shutil.copytree(db_module.MIGRATIONS_DIR, copy)
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", copy)
    return copy


def _recorded(owner_db: Database) -> dict[str, str | None]:
    return {r["name"]: r["sha256"]
            for r in _rows(owner_db, "SELECT name, sha256 FROM schema_migration")}


def test_every_applied_migration_is_recorded_with_its_digest(owner_db: Database) -> None:
    from apollo.storage.db import MIGRATIONS_DIR, migration_sha256

    expected = {p.name: migration_sha256(p.read_text(encoding="utf-8"))
                for p in MIGRATIONS_DIR.glob("*.sql")}
    assert _recorded(owner_db) == expected


def test_an_edited_applied_migration_refuses_the_run(
    owner_db: Database, tmp_path, monkeypatch
) -> None:
    from apollo.errors import MigrationChecksumError

    copy = _copy_migrations(tmp_path, monkeypatch)
    (copy / "0099_new.sql").write_text("CREATE TABLE should_not_exist (id int);\n")
    edited = copy / "0002_tombstone_preserves_provenance.sql"
    edited.write_text(edited.read_text() + "\n-- edited after it was applied\n")
    before = _recorded(owner_db)

    with pytest.raises(MigrationChecksumError, match="0002_tombstone_preserves_provenance"):
        owner_db.migrate()
    # Nothing was applied: not even the new, unrelated migration.
    assert _recorded(owner_db) == before
    assert not _rows(owner_db, "SELECT 1 FROM pg_tables WHERE tablename = 'should_not_exist'")


def test_a_recorded_migration_whose_file_is_missing_refuses_the_run(
    owner_db: Database, tmp_path, monkeypatch
) -> None:
    from apollo.errors import MigrationChecksumError

    copy = _copy_migrations(tmp_path, monkeypatch)
    (copy / "0004_invocation_manifest_shape.sql").unlink()
    with pytest.raises(MigrationChecksumError, match="0004_invocation_manifest_shape.*missing"):
        owner_db.migrate()


def test_a_new_migration_is_applied_and_recorded_with_its_digest(
    owner_db: Database, tmp_path, monkeypatch
) -> None:
    from apollo.storage.db import migration_sha256

    copy = _copy_migrations(tmp_path, monkeypatch)
    text = "CREATE TABLE checksum_probe (id int);\n"
    (copy / "0099_probe.sql").write_text(text)
    assert owner_db.migrate() == ["0099_probe.sql"]
    assert _recorded(owner_db)["0099_probe.sql"] == migration_sha256(text)
    assert owner_db.migrate() == []


@pytest.fixture()
def pre_checksum_deployment(admin_dsn: str, tmp_path, monkeypatch):
    """A database as a deployment from before checksums left it.

    Only the migrations that existed then are applied, and they are recorded
    by name alone. Later migrations are not there at all: they could not have
    been, so a test that blanked every digest would be describing a
    deployment that never existed.
    """
    import shutil
    import uuid

    import psycopg

    from apollo.storage import db as db_module
    from apollo.storage.db import PRE_CHECKSUM_SHA256
    from tests.conftest import _with_dbname

    name = f"apollo_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    owner = Database(_with_dbname(admin_dsn, name))
    then = tmp_path / "migrations-then"
    then.mkdir()
    for migration in PRE_CHECKSUM_SHA256:
        shutil.copy(db_module.MIGRATIONS_DIR / migration, then / migration)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(db_module, "MIGRATIONS_DIR", then)
            assert owner.migrate() == list(PRE_CHECKSUM_SHA256)
        with owner.connect() as conn, conn.cursor() as cur:
            cur.execute("ALTER TABLE schema_migration DROP COLUMN sha256")
        yield owner
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (name,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_a_deployment_from_before_checksums_is_adopted_when_its_files_match(
    pre_checksum_deployment: Database,
) -> None:
    """Rows recorded by name only get their digest, after it matches the known version.

    The same run then applies every migration written since, each with its
    digest, so the upgraded deployment ends where a new one starts.
    """
    from apollo.storage.db import MIGRATIONS_DIR, PRE_CHECKSUM_SHA256, migration_sha256

    every = {p.name: migration_sha256(p.read_text(encoding="utf-8"))
             for p in sorted(MIGRATIONS_DIR.glob("*.sql"))}
    since = [name for name in every if name not in PRE_CHECKSUM_SHA256]
    assert "0005_memory_proposals.sql" in since

    assert pre_checksum_deployment.migrate() == since
    recorded = _recorded(pre_checksum_deployment)
    assert recorded == every
    assert {name: recorded[name] for name in PRE_CHECKSUM_SHA256} == PRE_CHECKSUM_SHA256
    assert pre_checksum_deployment.migrate() == []


def test_only_a_migration_from_before_checksums_may_be_recorded_without_one(
    owner_db: Database,
) -> None:
    """A later migration with no digest is not adopted on trust: the run is refused."""
    from apollo.errors import MigrationChecksumError

    with owner_db.connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE schema_migration SET sha256 = NULL"
                    " WHERE name = '0005_memory_proposals.sql'")
    with pytest.raises(MigrationChecksumError,
                       match="0005_memory_proposals.*without a checksum"):
        owner_db.migrate()
    assert _recorded(owner_db)["0005_memory_proposals.sql"] is None


def test_a_deployment_from_before_checksums_is_refused_when_a_file_changed(
    owner_db: Database, tmp_path, monkeypatch
) -> None:
    from apollo.errors import MigrationChecksumError

    copy = _copy_migrations(tmp_path, monkeypatch)
    edited = copy / "0003_memory_lifecycle.sql"
    edited.write_text(edited.read_text() + "\n-- edited\n")
    from apollo.storage.db import PRE_CHECKSUM_SHA256

    with owner_db.connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE schema_migration SET sha256 = NULL WHERE name = ANY(%s)",
                    (list(PRE_CHECKSUM_SHA256),))
    with pytest.raises(MigrationChecksumError, match="0003_memory_lifecycle.*before checksums"):
        owner_db.migrate()
    # Refused whole: no row was adopted, not even those whose files still match.
    recorded = _recorded(owner_db)
    assert {recorded[name] for name in PRE_CHECKSUM_SHA256} == {None}
