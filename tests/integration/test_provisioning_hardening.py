"""Provisioning hardening for step 10: no temporary tables, and quiet server logs.

* `apply_grants` revokes TEMP on the Apollo database from PUBLIC (every role
  holds it there by default) and from the runtime role, and least privilege now
  includes "cannot create temporary tables".
* `log_error_verbosity = terse` and `log_min_error_statement = panic` keep a
  refused row's text out of PostgreSQL's own log. Only a superuser can set
  them, so `apollo provision` prints the statements when they are not in
  effect and `apollo doctor` reports them; neither fails because of them.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

from apollo.cli.main import main
from apollo.storage.db import (
    Database,
    describe_log_settings,
    describe_role_privileges,
    provision_runtime_role,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def _set_log_settings(fresh_database: dict[str, Any], admin_dsn: str) -> None:
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        for name, value in (("log_error_verbosity", "terse"), ("log_min_error_statement", "panic")):
            conn.execute(
                sql.SQL("ALTER DATABASE {} SET {} = {}").format(
                    sql.Identifier(fresh_database["name"]), sql.Identifier(name), sql.Literal(value)
                )
            )


@pytest.fixture()
def cli_env(fresh_database: dict[str, Any], monkeypatch) -> None:
    monkeypatch.setenv("APOLLO_CONFIG", str(REPO_ROOT / "apollo.toml"))
    monkeypatch.setenv("APOLLO_ADMIN_DSN", fresh_database["owner_dsn"])
    monkeypatch.setenv("APOLLO_DATABASE_DSN", fresh_database["app_dsn"])
    monkeypatch.setenv("APOLLO_RUNTIME_ROLE", fresh_database["role"])
    monkeypatch.delenv("APOLLO_RUNTIME_PASSWORD", raising=False)


# ---------------------------------------------------------------------------
# TEMP
# ---------------------------------------------------------------------------


def test_the_runtime_role_cannot_create_a_temporary_table(db: Database, fresh_database) -> None:
    with db.connect() as conn:
        facts = describe_role_privileges(conn, fresh_database["role"])
        assert facts.can_create_temp is False and facts.is_least_privilege
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE memory (id uuid)")


def test_public_no_longer_carries_temp_on_the_apollo_database(
    owner_db: Database, fresh_database, admin_dsn: str
) -> None:
    """A role with no grants of its own gets TEMP only through PUBLIC."""
    bystander = f"apollo_test_bystander_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(bystander)))
    try:
        with owner_db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT has_database_privilege(%s, current_database(), 'TEMPORARY') AS temp",
                (bystander,),
            )
            assert cur.fetchone()["temp"] is False
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(bystander)))


def test_a_role_that_regains_temp_is_not_least_privilege_until_reprovisioned(
    owner_db: Database, fresh_database
) -> None:
    role = fresh_database["role"]
    with owner_db.connect() as conn:
        conn.execute(
            sql.SQL("GRANT TEMPORARY ON DATABASE {} TO {}").format(
                sql.Identifier(fresh_database["name"]), sql.Identifier(role)
            )
        )
        facts = describe_role_privileges(conn, role)
        assert facts.can_create_temp is True and not facts.is_least_privilege
        provision_runtime_role(conn, role)  # revokes it again, then verifies
        assert describe_role_privileges(conn, role).can_create_temp is False


# ---------------------------------------------------------------------------
# PostgreSQL log settings
# ---------------------------------------------------------------------------


def test_the_log_settings_are_reported_and_the_fix_is_named(
    owner_db: Database, fresh_database, admin_dsn: str
) -> None:
    with owner_db.connect() as conn:
        before = describe_log_settings(conn)
    assert before.readable and not before.in_effect
    name = fresh_database["name"]
    assert before.missing() == [
        f"ALTER DATABASE \"{name}\" SET log_error_verbosity = 'terse';",
        f"ALTER DATABASE \"{name}\" SET log_min_error_statement = 'panic';",
    ]

    _set_log_settings(fresh_database, admin_dsn)
    with owner_db.connect() as conn:  # a new session sees the per-database settings
        after = describe_log_settings(conn)
    assert after.in_effect and after.missing() == []
    assert after.values == {"log_error_verbosity": "terse", "log_min_error_statement": "panic"}


def test_the_runtime_role_can_read_the_log_settings(db: Database) -> None:
    with db.connect() as conn:
        assert describe_log_settings(conn).readable


def test_provision_prints_the_settings_only_when_they_are_not_in_effect(
    cli_env, fresh_database, admin_dsn: str, monkeypatch, capsys
) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(["provision"]) == 0
    out = capsys.readouterr().out
    assert "can create temp tables: False" in out
    assert "PostgreSQL log settings not in effect" in out
    assert "SET log_error_verbosity = 'terse';" in out
    assert "SET log_min_error_statement = 'panic';" in out

    _set_log_settings(fresh_database, admin_dsn)
    assert main(["provision"]) == 0
    assert "log settings not in effect" not in capsys.readouterr().out


def test_doctor_reports_the_settings_without_failing_on_them(
    cli_env, fresh_database, admin_dsn: str, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("APOLLO_ADMIN_DSN")  # doctor as the runtime role
    assert main(["doctor"]) == 0  # least privilege holds; the settings are advice
    out = capsys.readouterr().out
    assert "can_create_temp: False" in out
    assert "log_error_verbosity: default (want terse)" in out
    assert "log_min_error_statement: error (want panic)" in out
    assert "log settings: NOT IN EFFECT" in out

    _set_log_settings(fresh_database, admin_dsn)
    assert main(["doctor"]) == 0
    assert "log settings: OK" in capsys.readouterr().out
