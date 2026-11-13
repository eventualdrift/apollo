"""`apollo backup`: a dump that is only ever a stream, a restore that is really performed.

    apollo backup dump | age -r "$RECIPIENT" -o "$DIR/$(apollo backup name age)"
    age -d -i key.txt "$DIR/apollo-....dump.age" | apollo backup verify
    apollo backup prune "$DIR"

`dump` writes a `pg_dump` archive to a pipe and refuses a file or a terminal,
so the only backup that can be kept is one that went through an encryption
tool first (spec K.8). Which tool is yours to choose; Apollo sees neither the
key nor the encrypted file.

`verify` is the restore test. It loads an archive from stdin into a scratch
database, checks the schema against a fresh install, replays every recorded
model call so that the content verifies itself, and drops the scratch
database. "An untested backup is a rumour."

`prune` destroys backups older than `backup_retention_days`, by the time in
their names, and reports any plaintext dump it finds.

`dump` and `verify` run as the owner (`APOLLO_ADMIN_DSN`). An error prints as
its kind and step, never the tools' output.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from apollo.cli.memory import Terminal
from apollo.cli.turn import summarise
from apollo.core.backups import BackupError, RestoreReport, verify_restore
from apollo.storage import backups


class MissingAdminDsn(LookupError):
    """APOLLO_ADMIN_DSN is not set."""


class NotADirectory(LookupError):
    """The path given is not a directory."""


_HINTS: dict[type[Exception], str] = {
    MissingAdminDsn: "APOLLO_ADMIN_DSN is not set; a backup is taken and tested as the "
                     "database owner",
    NotADirectory: "that is not a directory",
}


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p_backup = sub.add_parser(
        "backup", help="dump to a pipe, test a restore, and expire old backups"
    )
    backup_sub = p_backup.add_subparsers(dest="backup_command", required=True)
    backup_sub.add_parser(
        "dump", help="write a database archive to stdout, which must be a pipe into an "
                     "encryption tool (needs APOLLO_ADMIN_DSN)")
    p_name = backup_sub.add_parser(
        "name", help="print the file name for a backup taken now")
    p_name.add_argument("encryption", help="the suffix of your encryption tool, such as `age`")
    backup_sub.add_parser(
        "verify", help="restore the archive on stdin into a scratch database, verify it, "
                       "and drop it (needs APOLLO_ADMIN_DSN); exits 1 if it does not verify")
    p_prune = backup_sub.add_parser(
        "prune", help="destroy backups older than backup_retention_days; exits 1 if a "
                      "plaintext dump is found")
    p_prune.add_argument("directory")
    p_prune.add_argument("--dry-run", action="store_true",
                         help="say what would be destroyed, and destroy nothing")


def run(args: argparse.Namespace, term: Terminal, *, admin_dsn: str | None,
        retention_days: int) -> int:
    command = args.backup_command
    try:
        if command == "name":
            print(backups.backup_name(term.clock(), args.encryption), file=term.stdout)
            return 0
        if command == "prune":
            return _prune(args, term, retention_days)
        if not admin_dsn:
            raise MissingAdminDsn()
        if command == "dump":
            backups.dump(admin_dsn, sys.stdout.buffer)
            return 0
        report = verify_restore(admin_dsn, sys.stdin.buffer, now=term.clock())
        for line in describe(report):
            print(line, file=term.stdout)
        return 0 if report.verified else 1
    except BackupError as exc:
        # Apollo's own words about which step failed; never a tool's output.
        print(f"backup {command}: {exc}", file=term.stderr)
        return 1
    except Exception as exc:  # the kind only: a message may quote a row or a DSN
        hint = _HINTS.get(type(exc))
        print(f"backup {command}: {type(exc).__name__}" + (f" — {hint}" if hint else ""),
              file=term.stderr)
        return 1


def describe(report: RestoreReport) -> list[str]:
    """A performed restore, as lines. Counts, names and statuses only."""
    lines = ["restored into a scratch database, in one transaction"]
    lines += [f"  {table:<24}{count:>8} rows" for table, count in sorted(report.rows.items())]
    if report.migrations_applied:
        lines.append("migrations the backup predates, applied after the restore: "
                     + ", ".join(report.migrations_applied))
    else:
        lines.append("migrations: all present, checksums match")
    if report.schema_differences:
        lines.append("schema: DIFFERS from a fresh install in its "
                     + ", ".join(report.schema_differences))
    else:
        lines.append("schema: identical to a fresh install "
                     "(columns, constraints, triggers, functions, indexes)")
    lines.append("replay: " + summarise(report.replay)[0])
    lines += summarise(report.replay)[1:]
    lines.append("scratch database dropped")
    lines.append("RESTORE VERIFIED" if report.verified else "RESTORE NOT VERIFIED")
    return lines


def _prune(args: argparse.Namespace, term: Terminal, retention_days: int) -> int:
    directory = Path(args.directory)
    if not directory.is_dir():
        raise NotADirectory()
    found = backups.sort_backups(directory, retention_days=retention_days, now=term.clock())
    for path in found.expired:
        if not args.dry_run:
            path.unlink()
        print(f"{'would destroy' if args.dry_run else 'destroyed'}  {path.name}",
              file=term.stdout)
    for path in found.kept:
        print(f"kept       {path.name}  {_age(path, term.clock())}", file=term.stdout)
    for path in found.unrecognised:
        print(f"left alone {path.name}  (not an Apollo backup name)", file=term.stdout)
    for path in found.plaintext:
        print(f"PLAINTEXT  {path.name}: an unencrypted dump. Encrypt it or destroy it.",
              file=term.stderr)
    print(f"retention: {retention_days} days", file=term.stdout)
    return 1 if found.plaintext else 0


def _age(path: Path, now: datetime) -> str:
    named = backups.BACKUP_NAME.match(path.name)
    assert named is not None
    taken = datetime.strptime(named["taken"], backups.STAMP_FORMAT).replace(tzinfo=now.tzinfo)
    return f"{max((now - taken).days, 0)}d old"
