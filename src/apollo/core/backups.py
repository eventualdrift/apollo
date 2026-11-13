"""Restore a backup into a scratch database, and verify what arrived (spec K.8).

"The phase-zero deliverable is a restore that has actually been performed
into a scratch database and verified — not a backup script. An untested
backup is a rumour."

`verify_restore` performs one. It loads the archive into a throwaway database
and then checks, in order:

1. **It restores at all**, in one transaction, with every foreign key intact.
2. **Its migrations are the ones this code knows**, by checksum; any the
   backup predates are applied, as they would be on a real restore.
3. **Its schema is the schema of a fresh install**: every column, constraint,
   trigger, function and index. The rules that make messages write-once and
   the audit stream append-only are schema, and must survive a restore.
4. **Its content verifies itself.** Every recorded model call is replayed:
   the hashes each one recorded are recomputed from the restored rows, so
   content that did not survive intact is a mismatch.

The scratch databases hold real content while they exist, and are dropped
whatever happens. Nothing is written to the database the backup came from.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import IO

from apollo.core.replay import ReplaySummary, replay_all
from apollo.storage import backups
from apollo.storage.backups import BackupError
from apollo.storage.db import Database

__all__ = ["BackupError", "RestoreReport", "verify_restore"]


@dataclass(frozen=True)
class RestoreReport:
    """What a performed restore showed. Counts, names and statuses only."""

    #: Rows per table in the restored database.
    rows: dict[str, int]
    #: Migrations the backup predated, applied after the restore.
    migrations_applied: tuple[str, ...]
    #: Kinds of schema object that differ from a fresh install. Empty is good.
    schema_differences: tuple[str, ...]
    replay: ReplaySummary

    @property
    def verified(self) -> bool:
        return not self.schema_differences and self.replay.sound


def verify_restore(admin_dsn: str, source: IO[bytes], *, now: datetime) -> RestoreReport:
    """Restore the archive on `source` into a scratch database and verify it.

    `admin_dsn` is the owner connection: it creates and drops the scratch
    databases, on the same server as the database it names.
    """
    # Unguessable as well as unique: the name is not a secret, but nothing
    # else should be able to squat on it between the create and the restore.
    suffix = f"{now.strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}"
    with backups.scratch_database(admin_dsn, f"apollo_restore_{suffix}") as restored:
        backups.restore(restored, source)
        applied = Database(restored).migrate()  # refuses a migration that has drifted
        with backups.scratch_database(admin_dsn, f"apollo_fresh_{suffix}") as fresh:
            Database(fresh).migrate()
            expected, actual = backups.schema_objects(fresh), backups.schema_objects(restored)
        differences = tuple(kind for kind in expected if expected[kind] != actual[kind])
        return RestoreReport(
            rows=backups.row_counts(restored),
            migrations_applied=tuple(applied),
            schema_differences=differences,
            replay=replay_all(Database(restored)),
        )
