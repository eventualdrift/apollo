"""Backups: the parts of spec K.8 that can be mechanisms rather than habits.

Three rules live here.

**A dump is a stream, never a plaintext file.** `dump` writes the archive to
a pipe and refuses a regular file or a terminal, so the only way to keep a
backup is to pipe it through an encryption tool first. Which tool, and whose
key, is the operator's choice; Apollo never sees either.

**A restore is performed, not assumed.** `scratch_database` creates a
throwaway database, `restore` loads an archive into it, and the caller
(`apollo.core.backups`) verifies what arrived. The scratch database holds
real content while it exists and is always dropped.

**Backups expire.** `sort_backups` names the files older than the retention
window by the time in their name, and names any plaintext dump it finds.

Connection details reach `pg_dump` and `pg_restore` through the environment,
never the command line: a DSN can carry a password, and a command line is
visible to every process on the machine.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from apollo.errors import ApolloError

#: Roles and grants are not in the archive: they belong to the cluster, and
#: `apollo provision` recreates them. So ownership and privileges are left out
#: of both the dump and the restore, and a restore never depends on a role
#: that happens to exist where it was taken.
PG_DUMP_ARGS = ("--format=custom", "--no-owner", "--no-privileges")
PG_RESTORE_ARGS = ("--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction")

#: `apollo-20261113T074500Z.dump.age`: the UTC time the dump was taken, and
#: the encryption it is under. The time in the name is what retention goes by:
#: a file's own modification time changes whenever it is copied.
BACKUP_NAME = re.compile(r"^apollo-(?P<taken>\d{8}T\d{6}Z)\.dump\.(?P<encryption>[a-z0-9]+)$")
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
#: A dump with no encryption suffix. It should not exist (spec K.8).
PLAINTEXT_NAME = re.compile(r"^apollo-.*\.dump$")

#: libpq reads each of these connection keywords from the named variable.
_LIBPQ_ENV = {
    "host": "PGHOST", "hostaddr": "PGHOSTADDR", "port": "PGPORT", "dbname": "PGDATABASE",
    "user": "PGUSER", "password": "PGPASSWORD", "passfile": "PGPASSFILE",
    "sslmode": "PGSSLMODE", "sslrootcert": "PGSSLROOTCERT", "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY", "connect_timeout": "PGCONNECT_TIMEOUT", "options": "PGOPTIONS",
    "application_name": "PGAPPNAME", "channel_binding": "PGCHANNELBINDING",
    "gssencmode": "PGGSSENCMODE", "target_session_attrs": "PGTARGETSESSIONATTRS",
    "service": "PGSERVICE",
}


class BackupError(ApolloError):
    """A dump or a restore did not complete. The message names the step, never its output."""


def libpq_environment(dsn: str) -> dict[str, str]:
    """The process environment that makes libpq connect as `dsn` does.

    Every keyword in the DSN must map to a variable: a keyword silently left
    out could send a dump to, or take one from, a different server.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    for keyword, value in conninfo_to_dict(dsn).items():
        variable = _LIBPQ_ENV.get(keyword)
        if variable is None:
            raise BackupError(
                f"the connection keyword {keyword!r} cannot be passed to pg_dump or pg_restore"
            )
        env[variable] = str(value)
    return env


def refuse_plaintext_destination(out: IO[bytes]) -> None:
    """A dump may go to a pipe, and nowhere a plaintext copy would be kept or shown."""
    try:
        mode = os.fstat(out.fileno()).st_mode
    except (OSError, ValueError):
        return  # not a real descriptor (a test's buffer): nothing is being kept
    if stat.S_ISREG(mode):
        raise BackupError(
            "a dump is not written to a file: pipe it into your encryption tool, "
            "and write that tool's output"
        )
    if out.isatty():
        raise BackupError("a dump is binary and private: pipe it into your encryption tool")


def dump(admin_dsn: str, out: IO[bytes]) -> None:
    """Write a `pg_dump` custom-format archive of the Apollo database to `out`."""
    refuse_plaintext_destination(out)
    _run(["pg_dump", *PG_DUMP_ARGS], libpq_environment(admin_dsn), stdout=out, step="pg_dump")


def restore(owner_dsn: str, source: IO[bytes]) -> None:
    """Load an archive from `source` into the (empty) database `owner_dsn` names.

    One transaction, stopping at the first error: a partial restore is not a
    restore, and is not left behind looking like one.
    """
    env = libpq_environment(owner_dsn)
    args = ["pg_restore", *PG_RESTORE_ARGS, f"--dbname={env['PGDATABASE']}"]
    _run(args, env, stdin=source, step="pg_restore")


def _run(args: list[str], env: dict[str, str], *, step: str, **streams: Any) -> None:
    try:
        # stderr is discarded: a failed statement's error can quote a row.
        done = subprocess.run(args, env=env, stderr=subprocess.DEVNULL, check=False, **streams)
    except FileNotFoundError:
        raise BackupError(f"{step} is not installed, or not on the PATH") from None
    if done.returncode != 0:
        raise BackupError(f"{step} failed with exit status {done.returncode}")


@contextmanager
def scratch_database(admin_dsn: str, name: str) -> Iterator[str]:
    """A new, empty database on the same server, dropped when the block ends.

    Yields the DSN that connects to it as the role `admin_dsn` names. Whatever
    is restored into it is real content, so it is dropped whatever happens
    inside the block.
    """
    identifier = sql.Identifier(name)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(identifier))
    try:
        yield make_conninfo(admin_dsn, dbname=name)
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(identifier))


def schema_objects(dsn: str) -> dict[str, list[str]]:
    """Every table column, constraint, trigger, function and index of the schema.

    Two databases with the same answer have the same rules: the triggers that
    make messages write-once and the audit stream append-only are schema, and
    a restore that lost one would still look like Apollo.
    """
    queries = {
        "columns": "SELECT table_name || '.' || column_name || ' ' || data_type || ' '"
                   "  || is_nullable || ' ' || coalesce(generation_expression, '') AS item"
                   " FROM information_schema.columns WHERE table_schema = 'public'",
        "constraints": "SELECT conrelid::regclass || ' ' || conname || ' '"
                       "  || pg_get_constraintdef(oid) AS item FROM pg_constraint"
                       " WHERE connamespace = 'public'::regnamespace",
        "triggers": "SELECT pg_get_triggerdef(oid) AS item FROM pg_trigger"
                    " WHERE NOT tgisinternal AND tgrelid::regclass::text NOT LIKE 'pg_%'",
        "functions": "SELECT p.proname || ' ' || md5(p.prosrc) || ' '"
                     "  || coalesce(array_to_string(p.proconfig, ','), '') AS item"
                     " FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace",
        "indexes": "SELECT indexdef AS item FROM pg_indexes WHERE schemaname = 'public'",
    }
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return {
            kind: sorted(str(row["item"]) for row in conn.execute(query).fetchall())
            for kind, query in queries.items()
        }


def row_counts(dsn: str) -> dict[str, int]:
    """How many rows each table of the schema holds."""
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        tables = [row["tablename"] for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        ).fetchall()]
        return {
            table: int(conn.execute(
                sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))
            ).fetchone()["n"])  # type: ignore[index]
            for table in tables
        }


# -- names and retention ---------------------------------------------------------------


def backup_name(taken: datetime, encryption: str) -> str:
    """The file name for a backup taken at `taken`, encrypted by the named tool."""
    name = f"apollo-{taken.astimezone(UTC).strftime(STAMP_FORMAT)}.dump.{encryption}"
    if BACKUP_NAME.match(name) is None:
        raise BackupError("an encryption suffix is lowercase letters and digits, such as `age`")
    return name


@dataclass(frozen=True)
class SortedBackups:
    """A backup directory, read by name."""

    #: Older than the retention window: to be destroyed.
    expired: tuple[Path, ...]
    kept: tuple[Path, ...]
    #: Dumps with no encryption suffix. None should exist.
    plaintext: tuple[Path, ...]
    #: Anything else. Left alone, and said to be.
    unrecognised: tuple[Path, ...]


def sort_backups(directory: Path, *, retention_days: int, now: datetime) -> SortedBackups:
    """Which backups in `directory` have outlived the retention window (spec K.8).

    Age is read from the name. A file this does not recognise is never
    removed: pruning the wrong directory must not cost anything.
    """
    cutoff = now.astimezone(UTC) - timedelta(days=retention_days)
    expired: list[Path] = []
    kept: list[Path] = []
    plaintext: list[Path] = []
    unrecognised: list[Path] = []
    for path in sorted(p for p in directory.iterdir() if p.is_file()):
        named = BACKUP_NAME.match(path.name)
        if named is None:
            (plaintext if PLAINTEXT_NAME.match(path.name) else unrecognised).append(path)
            continue
        try:
            taken = datetime.strptime(named["taken"], STAMP_FORMAT).replace(tzinfo=UTC)
        except ValueError:
            unrecognised.append(path)
            continue
        (expired if taken < cutoff else kept).append(path)
    return SortedBackups(tuple(expired), tuple(kept), tuple(plaintext), tuple(unrecognised))
