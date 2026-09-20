"""Connection handling and the migration runner. Raw SQL, no ORM (ADR-0014)."""

from __future__ import annotations

import logging
import pathlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager

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
            with conn.cursor() as cur:
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


#: Tables the application role may write. `audit_event` is deliberately absent
#: from the UPDATE/DELETE grant: the audit stream is append-only at the database
#: level, not merely by application discipline (spec H.3).
_WRITABLE = (
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
    """Grant the application role exactly what it needs and nothing more."""
    ident = sql.Identifier(role)
    with conn.cursor() as cur:
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident))
        for table in _WRITABLE:
            cur.execute(
                sql.SQL(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}"
                ).format(sql.Identifier(table), ident)
            )
        # Append-only: insert and read, never modify or remove.
        cur.execute(
            sql.SQL("GRANT SELECT, INSERT ON audit_event TO {}").format(ident)
        )
        cur.execute(
            sql.SQL("GRANT USAGE, SELECT ON SEQUENCE audit_event_id_seq TO {}").format(
                ident
            )
        )
        cur.execute(
            sql.SQL("GRANT SELECT, INSERT ON schema_migration TO {}").format(ident)
        )
