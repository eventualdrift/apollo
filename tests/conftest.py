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
    """A throwaway database provisioned exactly as the documented deployment is.

    This fixture calls the same `provision_runtime_role` the `apollo provision`
    command calls, so the tested configuration and the documented one are one
    mechanism rather than two that happen to resemble each other. If the
    deployment stops being least-privilege, these tests stop passing.

    Real PostgreSQL. No SQLite substitution: the schema relies on triggers,
    generated tsvector columns, jsonb containment and role grants, none of
    which SQLite has.
    """
    import psycopg

    from apollo.storage.db import Database, provision_runtime_role

    name = f"apollo_test_{uuid.uuid4().hex[:12]}"
    role = f"{name}_app"
    with psycopg.connect(admin_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')

    owner_dsn = _with_dbname(admin_dsn, name)
    Database(owner_dsn).migrate()
    with psycopg.connect(owner_dsn, autocommit=True) as conn:
        provision_runtime_role(conn, role)

    app_dsn = _with_role(_with_dbname(admin_dsn, name), role)
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
    """Apollo's own connection: the least-privilege runtime role, as in production."""
    from apollo.storage.db import Database

    return Database(fresh_database["app_dsn"])


@pytest.fixture()
def owner_db(fresh_database):
    """The owner connection. Only migrations, provisioning and test setup use it."""
    from apollo.storage.db import Database

    return Database(fresh_database["owner_dsn"])


def _with_dbname(dsn: str, name: str) -> str:
    parts = [p for p in dsn.split() if not p.startswith("dbname=")]
    parts.append(f"dbname={name}")
    return " ".join(parts)


def _with_role(dsn: str, role: str) -> str:
    parts = [p for p in dsn.split() if not p.startswith("user=")]
    parts.append(f"user={role}")
    return " ".join(parts)
