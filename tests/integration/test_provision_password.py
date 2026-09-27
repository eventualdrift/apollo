"""`apollo provision` with a runtime password (the `ALTER ROLE ... PASSWORD` path).

PostgreSQL rejects a bound parameter in a utility statement, and interpolating
the plaintext would put it in the statement text and any statement log. So the
password is hashed client-side into a SCRAM-SHA-256 verifier and only that is
sent. These tests prove the verifier is what the catalogue holds, that it is
the verifier of the given password, and that no statement carries the plaintext.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

from apollo.cli.main import main
from apollo.storage.db import Database, RoleProvisioningError, provision_runtime_role

pytestmark = pytest.mark.integration

# Quote, dollar and comment characters: a naive interpolation would break on these.
PASSWORD = "correct horse 'battery' $staple --"
NEW_PASSWORD = "a different one; entirely"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _stored_verifier(owner_db: Database, role: str) -> str | None:
    with owner_db.connect() as conn, conn.cursor() as cur:
        try:
            cur.execute("SELECT rolpassword FROM pg_authid WHERE rolname = %s", (role,))
        except psycopg.errors.InsufficientPrivilege:
            pytest.skip("reading pg_authid needs a superuser APOLLO_TEST_ADMIN_DSN")
        row = cur.fetchone()
    assert row is not None
    value = row["rolpassword"]
    return None if value is None else str(value)


def _verifier_matches(verifier: str, password: str) -> bool:
    """Recompute the SCRAM-SHA-256 keys (RFC 5802/7677) from the stored salt.

    Independent of pg_hba.conf: it proves the verifier belongs to this password
    even where the test server authenticates by trust.
    """
    mechanism, rest = verifier.split("$", 1)
    assert mechanism == "SCRAM-SHA-256"
    iterations_salt, keys = rest.split("$")
    iterations, salt = iterations_salt.split(":")
    stored_key, server_key = keys.split(":")
    salted = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), base64.b64decode(salt), int(iterations)
    )
    client = hmac.new(salted, b"Client Key", "sha256").digest()
    server = hmac.new(salted, b"Server Key", "sha256").digest()
    return hmac.compare_digest(
        hashlib.sha256(client).digest(), base64.b64decode(stored_key)
    ) and hmac.compare_digest(server, base64.b64decode(server_key))


def _recording(conn: psycopg.Connection[Any]) -> list[tuple[str, Any]]:
    """Record every statement's text, as sent, and its parameters."""
    executed: list[tuple[str, Any]] = []

    class RecordingCursor(psycopg.Cursor[Any]):
        def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
            text = query.as_string(conn) if isinstance(query, sql.Composable) else str(query)
            executed.append((text, params))
            return super().execute(query, params, **kwargs)

    conn.cursor_factory = RecordingCursor
    return executed


@pytest.fixture()
def extra_role(owner_db: Database):
    """A role name no one has created yet; dropped afterwards with its grants."""
    role = f"apollo_test_{uuid.uuid4().hex[:12]}_pw"
    yield role
    with owner_db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        if cur.fetchone() is not None:
            cur.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            cur.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_password_on_an_existing_role_stores_its_scram_verifier(
    owner_db: Database, fresh_database
) -> None:
    role = fresh_database["role"]
    with owner_db.connect() as conn:
        assert provision_runtime_role(conn, role, password=PASSWORD) is False
    verifier = _stored_verifier(owner_db, role)
    assert verifier is not None
    assert verifier.startswith("SCRAM-SHA-256$")
    assert _verifier_matches(verifier, PASSWORD)
    assert not _verifier_matches(verifier, PASSWORD + "x")


def test_no_statement_carries_the_plaintext(owner_db: Database, fresh_database) -> None:
    role = fresh_database["role"]
    with owner_db.connect() as conn:
        executed = _recording(conn)
        provision_runtime_role(conn, role, password=PASSWORD)
    verifier = _stored_verifier(owner_db, role)
    assert verifier is not None

    alter = [text for text, _ in executed if text.startswith("ALTER ROLE")]
    assert len(alter) == 1
    assert "PASSWORD" in alter[0] and verifier in alter[0]  # sent the verifier itself
    for text, params in executed:
        assert PASSWORD not in text
        assert PASSWORD not in repr(params)


def test_a_new_password_replaces_the_old_one(owner_db: Database, fresh_database) -> None:
    role = fresh_database["role"]
    with owner_db.connect() as conn:
        provision_runtime_role(conn, role, password=PASSWORD)
    first = _stored_verifier(owner_db, role)
    with owner_db.connect() as conn:
        assert provision_runtime_role(conn, role, password=NEW_PASSWORD) is False
    second = _stored_verifier(owner_db, role)
    assert first is not None and second is not None
    assert second != first
    assert _verifier_matches(second, NEW_PASSWORD)
    assert not _verifier_matches(second, PASSWORD)


def test_omitting_the_password_leaves_an_existing_one_untouched(
    owner_db: Database, fresh_database
) -> None:
    role = fresh_database["role"]
    with owner_db.connect() as conn:
        provision_runtime_role(conn, role, password=PASSWORD)
        provision_runtime_role(conn, role)
    verifier = _stored_verifier(owner_db, role)
    assert verifier is not None and _verifier_matches(verifier, PASSWORD)


def test_a_new_role_gets_the_password_or_none(owner_db: Database, extra_role: str) -> None:
    with owner_db.connect() as conn:
        assert provision_runtime_role(conn, extra_role) is True
    assert _stored_verifier(owner_db, extra_role) is None  # trust/peer auth

    other = extra_role + "2"
    try:
        with owner_db.connect() as conn:
            assert provision_runtime_role(conn, other, password=PASSWORD) is True
        verifier = _stored_verifier(owner_db, other)
        assert verifier is not None and _verifier_matches(verifier, PASSWORD)
    finally:
        with owner_db.connect() as conn, conn.cursor() as cur:
            cur.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(other)))
            cur.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(other)))


@pytest.mark.parametrize("bad", ["", "before\x00after"])
def test_empty_or_nul_passwords_are_refused_before_any_change(
    owner_db: Database, fresh_database, bad: str
) -> None:
    role = fresh_database["role"]
    with owner_db.connect() as conn:
        executed = _recording(conn)
        with pytest.raises(RoleProvisioningError, match="non-empty and contain no NUL"):
            provision_runtime_role(conn, role, password=bad)
    assert executed == []
    assert _stored_verifier(owner_db, role) is None


def test_the_role_can_log_in_with_the_password(
    owner_db: Database, fresh_database, extra_role: str
) -> None:
    """End to end, where pg_hba.conf enforces password auth for this role.

    Uses a role of its own (named `*_pw`) so a server can require SCRAM for it
    while the password-less fixture role keeps trust auth.
    """
    with owner_db.connect() as conn:
        provision_runtime_role(conn, extra_role, password=PASSWORD)
    params = dict(p.split("=", 1) for p in fresh_database["app_dsn"].split())
    params["user"] = extra_role
    params.pop("password", None)
    try:
        psycopg.connect(**params, password=PASSWORD + "wrong").close()
    except psycopg.OperationalError:
        pass
    else:
        pytest.skip("the test server does not enforce password auth for this role")
    psycopg.connect(**params, password=PASSWORD).close()


def test_the_cli_reads_the_password_from_stdin(
    fresh_database, owner_db: Database, monkeypatch, capsys
) -> None:
    role = fresh_database["role"]
    monkeypatch.setenv("APOLLO_CONFIG", str(REPO_ROOT / "apollo.toml"))
    monkeypatch.setenv("APOLLO_ADMIN_DSN", fresh_database["owner_dsn"])
    monkeypatch.setenv("APOLLO_DATABASE_DSN", fresh_database["app_dsn"])
    monkeypatch.setenv("APOLLO_RUNTIME_ROLE", role)
    monkeypatch.delenv("APOLLO_RUNTIME_PASSWORD", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))

    assert main(["provision", "--password-stdin"]) == 0
    out = capsys.readouterr()
    assert f"runtime role {role}: updated" in out.out
    assert PASSWORD not in out.out + out.err
    verifier = _stored_verifier(owner_db, role)
    assert verifier is not None and _verifier_matches(verifier, PASSWORD)


def test_the_cli_refuses_an_empty_password(
    fresh_database, owner_db: Database, monkeypatch, capsys
) -> None:
    role = fresh_database["role"]
    monkeypatch.setenv("APOLLO_CONFIG", str(REPO_ROOT / "apollo.toml"))
    monkeypatch.setenv("APOLLO_ADMIN_DSN", fresh_database["owner_dsn"])
    monkeypatch.setenv("APOLLO_DATABASE_DSN", fresh_database["app_dsn"])
    monkeypatch.setenv("APOLLO_RUNTIME_ROLE", role)
    monkeypatch.delenv("APOLLO_RUNTIME_PASSWORD", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    assert main(["provision", "--password-stdin"]) == 2
    assert "non-empty" in capsys.readouterr().err
    assert _stored_verifier(owner_db, role) is None
