from __future__ import annotations

import os
import uuid

import pytest

pytest_plugins: list[str] = []


def _admin_dsn() -> str | None:
    return os.environ.get("APOLLO_TEST_ADMIN_DSN")


@pytest.fixture(scope="session")
def admin_dsn() -> str:
    dsn = _admin_dsn()
    if not dsn:
        pytest.skip("APOLLO_TEST_ADMIN_DSN not set; integration tests need live PostgreSQL")
    return dsn


@pytest.fixture()
def fresh_database(admin_dsn: str):
    """A throwaway database with migrations applied and the app role granted.

    Real PostgreSQL. No SQLite substitution: the schema relies on triggers,
    generated tsvector columns, jsonb containment and role grants, none of
    which SQLite has.
    """
    import psycopg

    from apollo.storage.db import Database, apply_grants

    name = f"apollo_test_{uuid.uuid4().hex[:12]}"
    role = f"{name}_app"
    with psycopg.connect(admin_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
        cur.execute(f'CREATE ROLE "{role}" LOGIN')

    owner_dsn = _with_dbname(admin_dsn, name)
    db = Database(owner_dsn)
    db.migrate()
    with psycopg.connect(owner_dsn, autocommit=True) as conn:
        apply_grants(conn, role)

    app_dsn = _with_dbname(admin_dsn, name).replace("user=postgres", f"user={role}")
    try:
        yield {"owner_dsn": owner_dsn, "app_dsn": app_dsn, "role": role, "name": name}
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (name,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
            cur.execute(f'DROP ROLE IF EXISTS "{role}"')


@pytest.fixture()
def db(fresh_database):
    from apollo.storage.db import Database

    return Database(fresh_database["owner_dsn"])


def _with_dbname(dsn: str, name: str) -> str:
    parts = [p for p in dsn.split() if not p.startswith("dbname=")]
    parts.append(f"dbname={name}")
    return " ".join(parts)
