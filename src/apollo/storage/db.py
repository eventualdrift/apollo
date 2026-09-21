"""Connection handling and the migration runner. Raw SQL, no ORM (ADR-0014)."""

from __future__ import annotations

import logging
import pathlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from apollo.errors import ApolloError

log = logging.getLogger(__name__)

MIGRATIONS_DIR = pathlib.Path(__file__).parent / "migrations"

#: Tracks open Apollo transactions per thread so that `assert_no_open_transaction`
#: can make "no transaction spans a model call" (spec A.2) a runtime property
#: rather than a review comment.
_open_transactions = threading.local()


def open_transaction_depth() -> int:
    return int(getattr(_open_transactions, "depth", 0))


def _enter_transaction() -> None:
    _open_transactions.depth = open_transaction_depth() + 1


def _exit_transaction() -> None:
    _open_transactions.depth = max(0, open_transaction_depth() - 1)


class RoleProvisioningError(ApolloError):
    """The runtime role could not be created, with the remedy named."""


class TransactionHeldError(ApolloError):
    """Raised when a model call is attempted with a database transaction open."""


def assert_no_open_transaction(context: str) -> None:
    depth = open_transaction_depth()
    if depth:
        raise TransactionHeldError(
            f"{context}: a database transaction is open (depth={depth}); "
            "no transaction may span a model call"
        )


class Database:
    """Owns connections. Nothing outside `storage/` opens one."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection]:
        conn = psycopg.connect(self._dsn, row_factory=dict_row, autocommit=True)
        try:
            yield conn
        finally:
            conn.close()

    def migrate(self) -> list[str]:
        """Apply pending numbered migrations. Idempotent and repeatable."""
        applied: list[str] = []
        with self.connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migration ("
                    "  name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
                )
                cur.execute("SELECT name FROM schema_migration")
                done = {row["name"] for row in cur.fetchall()}
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in done:
                    continue
                with conn.transaction(), conn.cursor() as cur:
                    cur.execute(path.read_text(encoding="utf-8"))
                    cur.execute("INSERT INTO schema_migration (name) VALUES (%s)", (path.name,))
                applied.append(path.name)
                log.info("migration.applied", extra={"migration": path.name})
        return applied


#: The runtime role's default name. The owner/migration role is separate and is
#: never used to run Apollo (spec K.4, ADR-0004).
RUNTIME_ROLE_DEFAULT = "apollo_app"

#: State tables the runtime role may write. `audit_event` is deliberately absent:
#: the audit stream is append-only at the database level, not merely by
#: application discipline (spec H.3).
_STATE_TABLES = (
    "conversation",
    "message",
    "turn",
    "model_invocation",
    "memory",
    "memory_observation",
    "memory_proposal",
    "identity_version",
    "turn_retrieval",
    "turn_retrieval_result",
)


def apply_grants(conn: psycopg.Connection, role: str) -> None:
    """Grant the runtime role exactly what Apollo needs, and revoke the rest.

    Idempotent: safe to re-run after every migration, which is how new tables
    acquire their grants.

    No `DELETE` is granted anywhere. Apollo has no delete path — tombstoning
    removes content with an UPDATE (spec D.7) — so withholding it turns an
    accidental future delete into a privilege error rather than data loss.
    """
    ident = sql.Identifier(role)
    with conn.cursor() as cur:
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident))
        for table in _STATE_TABLES:
            table_ident = sql.Identifier(table)
            cur.execute(
                sql.SQL("REVOKE ALL ON {} FROM {}").format(table_ident, ident)
            )
            cur.execute(
                sql.SQL("GRANT SELECT, INSERT, UPDATE ON {} TO {}").format(table_ident, ident)
            )
        # Append-only: read and insert, never modify or remove. Revoked from
        # PUBLIC as well, so no default privilege can reintroduce a write path.
        cur.execute(sql.SQL("REVOKE ALL ON audit_event FROM PUBLIC"))
        cur.execute(sql.SQL("REVOKE ALL ON audit_event FROM {}").format(ident))
        cur.execute(sql.SQL("GRANT SELECT, INSERT ON audit_event TO {}").format(ident))
        cur.execute(
            sql.SQL("GRANT USAGE, SELECT ON SEQUENCE audit_event_id_seq TO {}").format(ident)
        )
        # Migrations run as the owner; the runtime only needs to see what applied.
        cur.execute(sql.SQL("REVOKE ALL ON schema_migration FROM {}").format(ident))
        cur.execute(sql.SQL("GRANT SELECT ON schema_migration TO {}").format(ident))


def provision_runtime_role(
    conn: psycopg.Connection, role: str, *, password: str | None = None
) -> bool:
    """Create the runtime role if absent and apply its grants. Returns True if created.

    Called by `apollo provision` and by the test fixtures, so the documented
    deployment and the tested configuration are the same mechanism rather than
    two things that happen to resemble each other.
    """
    ident = sql.Identifier(role)
    created = False
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        if cur.fetchone() is None:
            # Roles are cluster-level, so a database owner may legitimately lack
            # CREATEROLE — common on managed PostgreSQL. Say exactly what a DBA
            # must run rather than failing with a bare privilege error.
            try:
                cur.execute(sql.SQL("CREATE ROLE {} LOGIN").format(ident))
            except psycopg.errors.InsufficientPrivilege:
                raise RoleProvisioningError(
                    f"cannot create role {role!r}: the admin role lacks CREATEROLE. "
                    f"Either grant it, or have a superuser run: "
                    f"CREATE ROLE {role} LOGIN PASSWORD '...'; "
                    f"then re-run provisioning, which will apply the grants."
                ) from None
            created = True
        if password is not None:
            cur.execute(sql.SQL("ALTER ROLE {} PASSWORD %s").format(ident), (password,))
    apply_grants(conn, role)

    # Verify rather than coerce: removing SUPERUSER requires superuser, so a
    # provisioner that tried to force it would fail on exactly the deployments
    # where the check matters most. Reading the catalogue works everywhere and
    # refuses to hand back a runtime that is not least-privilege.
    facts = describe_role_privileges(conn, role)
    if not facts.is_least_privilege:
        raise RoleProvisioningError(
            f"role {role!r} is not least-privilege after provisioning "
            f"(superuser={facts.is_superuser}, owns_audit_event={facts.owns_audit_event}, "
            f"audit_event={list(facts.audit_event_privileges)}). "
            "Apollo must not run as a role that can modify its own audit stream."
        )
    log.info("provision.runtime_role", extra={"status": "created" if created else "updated"})
    return created


@dataclass(frozen=True)
class RolePrivileges:
    """What the catalogue says about the runtime role. Read, never assumed."""

    is_superuser: bool
    can_create_db: bool
    can_create_role: bool
    audit_event_owner: str
    owns_audit_event: bool
    audit_event_privileges: tuple[str, ...]

    @property
    def is_least_privilege(self) -> bool:
        return (
            not self.is_superuser
            and not self.owns_audit_event
            and sorted(self.audit_event_privileges) == ["INSERT", "SELECT"]
        )

    def as_lines(self) -> list[str]:
        return [f"{k}: {v}" for k, v in vars(self).items()]


def describe_role_privileges(conn: psycopg.Connection, role: str) -> RolePrivileges:
    """Facts about the runtime role, read from the catalogue. Used by tests and `doctor`."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = %s",
            (role,),
        )
        row = cur.fetchone()
        if row is None:
            raise ApolloError(f"runtime role {role!r} does not exist")
        cur.execute(
            "SELECT pg_get_userbyid(relowner) AS owner FROM pg_class WHERE relname = 'audit_event'"
        )
        owner = cur.fetchone()["owner"]
        cur.execute(
            "SELECT privilege_type FROM information_schema.table_privileges"
            " WHERE grantee = %s AND table_name = 'audit_event'",
            (role,),
        )
        audit_privileges = sorted(r["privilege_type"] for r in cur.fetchall())
    return RolePrivileges(
        is_superuser=bool(row["rolsuper"]),
        can_create_db=bool(row["rolcreatedb"]),
        can_create_role=bool(row["rolcreaterole"]),
        audit_event_owner=str(owner),
        owns_audit_event=owner == role,
        audit_event_privileges=tuple(audit_privileges),
    )
