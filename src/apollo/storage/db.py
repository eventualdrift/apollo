"""Connection handling and the migration runner. Raw SQL, no ORM (ADR-0014)."""

from __future__ import annotations

import logging
import pathlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row

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
    def connect(self) -> Iterator[psycopg.Connection[DictRow]]:
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


def _current_database(conn: psycopg.Connection[Any]) -> str:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT current_database() AS name")
        row = cur.fetchone()
    assert row is not None
    return str(row["name"])


def apply_grants(conn: psycopg.Connection[DictRow], role: str) -> None:
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
        # No temporary tables. A session-private table of the same name as an
        # Apollo table is what an unpinned search_path would resolve first; the
        # lifecycle functions pin theirs, and withholding TEMP closes the rest.
        # Every role holds TEMP through PUBLIC by default, so revoke it there.
        database = sql.Identifier(_current_database(conn))
        cur.execute(sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM PUBLIC").format(database))
        cur.execute(sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM {}").format(database, ident))


def provision_runtime_role(
    conn: psycopg.Connection[DictRow], role: str, *, password: str | None = None
) -> bool:
    """Create the runtime role if absent and apply its grants. Returns True if created.

    Called by `apollo provision` and by the test fixtures, so the documented
    deployment and the tested configuration are the same mechanism rather than
    two things that happen to resemble each other.

    `password=None` leaves the role's password untouched: a new role gets none
    (trust/peer auth), an existing role keeps whatever it has.
    """
    if password is not None and (not password or "\x00" in password):
        # libpq takes a C string, so a NUL would silently truncate the password.
        raise RoleProvisioningError(
            "the runtime role password must be non-empty and contain no NUL"
        )
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
            # ALTER ROLE is a utility statement, so PostgreSQL rejects a bound
            # parameter here, and interpolating the plaintext would put it in the
            # statement text and any statement log. libpq hashes it client-side
            # into a SCRAM-SHA-256 verifier; only the verifier leaves this process.
            verifier = conn.pgconn.encrypt_password(
                password.encode(), role.encode(), b"scram-sha-256"
            ).decode("ascii")
            cur.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(ident, sql.Literal(verifier))
            )
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
            f"audit_event={list(facts.audit_event_privileges)}, "
            f"can_create_temp={facts.can_create_temp}). "
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
    can_create_temp: bool

    @property
    def is_least_privilege(self) -> bool:
        return (
            not self.is_superuser
            and not self.owns_audit_event
            and sorted(self.audit_event_privileges) == ["INSERT", "SELECT"]
            and not self.can_create_temp
        )

    def as_lines(self) -> list[str]:
        return [f"{k}: {v}" for k, v in vars(self).items()]


def describe_role_privileges(conn: psycopg.Connection[DictRow], role: str) -> RolePrivileges:
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
        owner_row = cur.fetchone()
        if owner_row is None:
            raise ApolloError("audit_event table does not exist")
        owner = owner_row["owner"]
        cur.execute(
            "SELECT privilege_type FROM information_schema.table_privileges"
            " WHERE grantee = %s AND table_name = 'audit_event'",
            (role,),
        )
        audit_privileges = sorted(r["privilege_type"] for r in cur.fetchall())
        cur.execute(
            "SELECT has_database_privilege(%s, current_database(), 'TEMPORARY') AS temp",
            (role,),
        )
        temp_row = cur.fetchone()
        assert temp_row is not None
    return RolePrivileges(
        is_superuser=bool(row["rolsuper"]),
        can_create_db=bool(row["rolcreatedb"]),
        can_create_role=bool(row["rolcreaterole"]),
        audit_event_owner=str(owner),
        owns_audit_event=owner == role,
        audit_event_privileges=tuple(audit_privileges),
        can_create_temp=bool(temp_row["temp"]),
    )


#: The server log settings that keep claim text out of PostgreSQL's own log.
#: A refused row is quoted in an error's DETAIL ("Failing row contains (...)"),
#: which `terse` omits, and `log_min_error_statement` would log the statement
#: that failed, which `panic` stops short of. Both are superuser settings, set
#: per database: ALTER DATABASE <db> SET <name> = '<value>'.
LOG_SETTINGS: tuple[tuple[str, str], ...] = (
    ("log_error_verbosity", "terse"),
    ("log_min_error_statement", "panic"),
)


@dataclass(frozen=True)
class LogSettings:
    """The log settings on this database, for one role.

    `values` are what the reading session got, so they are the role's effective
    values only when that session is the role's own (`session_role == role`): a setting on
    the role (`ALTER ROLE ... SET`) overrides the database's for its sessions
    and nobody else's. The database and role settings themselves come from the
    catalogue, which any role can read, so the statements built from them are
    right whichever session read them. None in `values` means unreadable.
    """

    database: str
    role: str
    session_role: str
    values: dict[str, str | None]
    database_values: dict[str, str]
    #: (setting, set only for this database, value) for each `ALTER ROLE role SET`.
    role_overrides: tuple[tuple[str, bool, str], ...]

    @property
    def readable(self) -> bool:
        return all(value is not None for value in self.values.values())

    @property
    def in_effect(self) -> bool:
        return all(self.values.get(name) == want for name, want in LOG_SETTINGS)

    def missing(self) -> list[str]:
        """The statements a superuser must run, for each setting not in effect in this session."""
        wrong = {name for name, want in LOG_SETTINGS if self.values.get(name) != want}
        return self._statements(wrong)

    def statements_to_ensure(self) -> list[str]:
        """The statements that make both settings certain for the role, from the catalogue alone."""
        return self._statements({name for name, _ in LOG_SETTINGS})

    def _statements(self, names: set[str]) -> list[str]:
        database, role = _quote_ident(self.database), _quote_ident(self.role)
        statements = []
        for name, want in LOG_SETTINGS:
            if name not in names:
                continue
            for setting, in_database, value in self.role_overrides:
                if setting == name and value != want:
                    scope = f" IN DATABASE {database}" if in_database else ""
                    statements.append(f"ALTER ROLE {role}{scope} RESET {name};")
            if self.database_values.get(name) != want:
                statements.append(f"ALTER DATABASE {database} SET {name} = '{want}';")
        return statements

    def as_lines(self) -> list[str]:
        return [
            f"{name}: {self.values.get(name) or 'unreadable'} (want {want})"
            for name, want in LOG_SETTINGS
        ]


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def describe_log_settings(conn: psycopg.Connection[DictRow], *, role: str) -> LogSettings:
    """Read the log settings this session started with, and those set for `role`.

    A per-database or per-role setting applies to sessions that start after it
    is set, and `apollo provision` and `apollo doctor` each open a fresh
    connection. Only a session of `role` itself sees what `role`'s sessions
    get; `LogSettings.session_role` says whose session this was.
    """
    names = [name for name, _ in LOG_SETTINGS]
    values: dict[str, str | None] = {}
    with conn.cursor(row_factory=dict_row) as cur:
        database = _current_database(conn)
        cur.execute("SELECT current_user AS who")
        who = cur.fetchone()
        assert who is not None
        for name in names:
            try:
                with conn.transaction():  # a refused read leaves the rest usable
                    cur.execute("SELECT current_setting(%s, true) AS value", (name,))
                    row = cur.fetchone()
            except psycopg.errors.InsufficientPrivilege:
                row = None
            values[name] = None if row is None or row["value"] is None else str(row["value"])
        cur.execute(
            "SELECT s.setrole <> 0 AS for_role, s.setdatabase <> 0 AS in_database,"
            "       unnest(s.setconfig) AS item"
            "  FROM pg_catalog.pg_db_role_setting s"
            " WHERE (s.setrole = 0 AND s.setdatabase ="
            "          (SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database()))"
            "    OR (s.setrole = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = %s)"
            "        AND s.setdatabase IN (0, (SELECT oid FROM pg_catalog.pg_database"
            "                                  WHERE datname = current_database())))",
            (role,),
        )
        database_values: dict[str, str] = {}
        overrides: list[tuple[str, bool, str]] = []
        for row in cur.fetchall():
            name, _, value = str(row["item"]).partition("=")
            if name not in names:
                continue
            if row["for_role"]:
                overrides.append((name, bool(row["in_database"]), value))
            else:
                database_values[name] = value
    return LogSettings(
        database=database,
        role=role,
        session_role=str(who["who"]),
        values=values,
        database_values=database_values,
        role_overrides=tuple(sorted(overrides)),
    )
