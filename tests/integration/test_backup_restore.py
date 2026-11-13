"""A backup is encrypted, really restored, and verified (spec K.8, O.1/30).

"The phase-zero deliverable is a restore that has actually been performed into
a scratch database and verified — not a backup script. An untested backup is a
rumour."

These tests perform it, with the real `pg_dump`, `pg_restore` and `age`:
a database with a conversation, memories, a proposal, a forgotten memory and
a failed call is dumped through a pipe into the encryption tool, the encrypted
file is decrypted through a pipe into `verify_restore`, and what arrives is
checked against what was there. No plaintext dump exists at any point.

They also hold the two boundaries K.8 states: a backup taken before something
was forgotten still contains it, and backups past the retention window are
destroyed.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from apollo.brains.fake import FakeBrain
from apollo.cli.main import main
from apollo.core import memories
from apollo.core import proposals as proposal_surface
from apollo.core.backups import RestoreReport, verify_restore
from apollo.core.conversations import create_conversation, transcript
from apollo.core.replay import BundleStatus, RenderStatus, replay_all
from apollo.errors import BrainTransportError
from apollo.storage import backups
from apollo.storage.backups import BackupError
from apollo.storage.db import Database, provision_runtime_role

from .test_memory_proposals import ProposingBrain
from .test_tombstone_sweep import _sweep

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not all(shutil.which(tool) for tool in ("pg_dump", "pg_restore", "age", "age-keygen")),
        reason="a real restore needs pg_dump, pg_restore and age on the PATH",
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
SENTINEL = "zqxbackup6120"
NOW = datetime(2026, 11, 13, 8, 0, tzinfo=UTC)


class Vault:
    """An `age` key pair and a directory of backups, as an operator would keep them."""

    def __init__(self, root: Path) -> None:
        self.directory = root / "backups"
        self.directory.mkdir()
        self.key = root / "key.txt"
        made = subprocess.run(["age-keygen", "-o", str(self.key)], capture_output=True,
                              text=True, check=True)
        self.recipient = made.stderr.split("Public key: ")[1].strip()

    def back_up(self, owner_dsn: str, *, taken: datetime = NOW) -> Path:
        """`apollo backup dump | age -r RECIPIENT -o FILE`: the dump never touches a disk."""
        path = self.directory / backups.backup_name(taken, "age")
        with subprocess.Popen(["age", "-r", self.recipient, "-o", str(path)],
                              stdin=subprocess.PIPE) as encrypt:
            assert encrypt.stdin is not None
            backups.dump(owner_dsn, encrypt.stdin)
            encrypt.stdin.close()
        assert encrypt.returncode == 0
        return path

    def decrypted(self, path: Path) -> subprocess.Popen[bytes]:
        """`age -d -i KEY FILE |`: the plaintext exists only in the pipe."""
        return subprocess.Popen(["age", "-d", "-i", str(self.key), str(path)],
                                stdout=subprocess.PIPE)

    def verify(self, owner_dsn: str, path: Path) -> RestoreReport:
        with self.decrypted(path) as decrypt:
            assert decrypt.stdout is not None
            return verify_restore(owner_dsn, decrypt.stdout, now=NOW)


@pytest.fixture()
def vault(tmp_path: Path) -> Vault:
    return Vault(tmp_path)


def _scratch_databases(owner_dsn: str) -> list[str]:
    with psycopg.connect(owner_dsn) as conn:
        return [row[0] for row in conn.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE 'apollo_restore_%'"
            " OR datname LIKE 'apollo_fresh_%'").fetchall()]


def _lived_in(db: Database, service_factory: Any, clock: Any) -> dict[str, Any]:
    """A database with something of everything in it."""
    conversation = create_conversation(db, now=clock())
    service = service_factory(brain=ProposingBrain())
    service.submit(conversation_id=conversation, text="Good morning.")
    # A turn that failed, before any memory existed: two failed calls and a note.
    service_factory(brain=FakeBrain(
        fail_times=9, failure=BrainTransportError(provider_error_type="timeout"),
    )).submit(conversation_id=conversation, text="Are you there?")
    proposed = service.submit(conversation_id=conversation,
                              text="Remember that I take my coffee black.")
    proposal_surface.save_proposal(db, proposed.proposal.id, now=clock())
    kept = memories.create_memory(db, scope="user", kind="fact", subject="bike",
                                  content="Janu's bike is blue.", now=clock())
    forgotten = memories.create_memory(db, scope="user", kind="fact", subject="door",
                                       content=f"The door code is {SENTINEL}.", now=clock())
    # This turn is sent all three memories: one matched, two merely recent.
    asked = service.submit(conversation_id=conversation, text="What is the door code?")
    return {"conversation": conversation, "kept": kept, "forgotten": forgotten, "asked": asked}


def _fingerprint(dsn: str) -> dict[str, tuple[int, str]]:
    """Per table: its row count, and a digest of every row. Order does not matter."""
    out = {}
    with psycopg.connect(dsn) as conn:
        conn.execute("SET TIME ZONE 'UTC'")
        tables = [row[0] for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1").fetchall()]
        for table in tables:
            # `applied_at` is when a migration ran, which a restore keeps; nothing excluded.
            count, digest = conn.execute(
                f"SELECT count(*), md5(coalesce(string_agg(md5(t::text), '' ORDER BY "
                f"md5(t::text)), '')) FROM {table} t").fetchone()
            out[table] = (count, digest)
    return out


# -- the restore, performed -----------------------------------------------------------------


def test_a_backup_is_encrypted_restored_into_a_scratch_database_and_verified(
    db: Database, fresh_database, service_factory, clock, vault: Vault
) -> None:
    """O.1/30. The dump is encrypted before it is retained; the restore is real."""
    owner_dsn = fresh_database["owner_dsn"]
    world = _lived_in(db, service_factory, clock)
    memories.tombstone_memory(db, world["forgotten"], now=clock())
    before = replay_all(db)
    assert before.sound and before.total == 6

    backup = vault.back_up(owner_dsn)

    # Encrypted before it was retained: one file, under the encryption's name,
    # and no plaintext dump beside it or anywhere under the directory.
    assert [p.name for p in vault.directory.iterdir()] == ["apollo-20261113T080000Z.dump.age"]
    ciphertext = backup.read_bytes()
    assert ciphertext.startswith(b"age-encryption.org/v1")
    assert b"PGDMP" not in ciphertext and b"coffee" not in ciphertext

    report = vault.verify(owner_dsn, backup)

    assert report.verified
    assert report.schema_differences == () and report.migrations_applied == ()
    # Everything arrived.
    assert report.rows == backups.row_counts(owner_dsn)
    assert report.rows["message"] == 8 and report.rows["memory"] == 3
    # And it verifies itself: every recorded hash recomputes from the restored
    # rows, the forgotten memory's turn is still honestly redacted, and the
    # failed calls still replay.
    assert report.replay == before
    assert report.replay.counts == {
        (BundleStatus.VERIFIED, RenderStatus.VERIFIED): 3,
        (BundleStatus.VERIFIED, RenderStatus.NOT_ATTEMPTED): 2,
        (BundleStatus.SOURCE_REDACTED, RenderStatus.NOT_ATTEMPTED): 1,
    }
    # The scratch databases held real content, and are gone.
    assert _scratch_databases(owner_dsn) == []
    # Nothing was written to the database the backup came from.
    assert replay_all(db) == before


def test_what_is_restored_is_row_for_row_what_was_backed_up(
    db: Database, fresh_database, service_factory, clock, vault: Vault
) -> None:
    owner_dsn = fresh_database["owner_dsn"]
    _lived_in(db, service_factory, clock)
    backup = vault.back_up(owner_dsn)

    with backups.scratch_database(owner_dsn, f"apollo_restore_{uuid.uuid4().hex[:8]}") as scratch:
        with vault.decrypted(backup) as decrypt:
            backups.restore(scratch, decrypt.stdout)
        assert _fingerprint(scratch) == _fingerprint(owner_dsn)
        assert backups.schema_objects(scratch) == backups.schema_objects(owner_dsn)
    assert _scratch_databases(owner_dsn) == []


def test_a_restored_database_still_enforces_its_rules_and_carries_on(
    db: Database, fresh_database, service_factory, clock, vault: Vault
) -> None:
    """The documented restore: load, `provision`, and Apollo runs as `apollo_app` again."""
    owner_dsn = fresh_database["owner_dsn"]
    world = _lived_in(db, service_factory, clock)
    backup = vault.back_up(owner_dsn)
    name = f"apollo_restore_{uuid.uuid4().hex[:8]}"

    with backups.scratch_database(owner_dsn, name) as scratch:
        with vault.decrypted(backup) as decrypt:
            backups.restore(scratch, decrypt.stdout)
        # Roles and grants are not in the archive; provisioning applies them.
        with psycopg.connect(scratch, autocommit=True) as conn:
            provision_runtime_role(conn, fresh_database["role"])
        restored = Database(psycopg.conninfo.make_conninfo(
            fresh_database["app_dsn"], dbname=name))

        # Write-once and append-only are schema, and came back with it.
        with restored.connect() as conn:
            with pytest.raises(psycopg.errors.Error):
                conn.execute("UPDATE message SET content = 'edited'")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("UPDATE audit_event SET actor = 'user'")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("DELETE FROM memory")

        # And the conversation continues where it was, as the runtime role.
        from apollo.brains.registry import BrainRegistry
        from apollo.core.identity import IdentityLoader
        from apollo.core.turns import TurnService

        from .conftest import make_config

        config = make_config(restored._dsn)
        brain = ProposingBrain()
        registry = BrainRegistry(config)
        registry._cache["brain.default"] = brain
        service = TurnService(restored, config, registry, IdentityLoader(config.identity_dir),
                              clock=clock)
        result = service.submit(conversation_id=world["conversation"],
                                text="What colour is my bike?")
        assert result.status == "completed"
        sent = "\n".join(message.content for message in brain.replies[0].messages)
        assert "Good morning." in sent and "Janu's bike is blue." in sent
        assert len(transcript(restored, world["conversation"])) == 10
    # The source never saw that turn.
    assert len(transcript(db, world["conversation"])) == 8


# -- what a restore must refuse to call verified ---------------------------------------------


def test_something_that_is_not_a_backup_is_refused_and_leaves_nothing_behind(
    fresh_database
) -> None:
    owner_dsn = fresh_database["owner_dsn"]
    read, write = os.pipe()
    with open(write, "wb") as feed:
        feed.write(b"this is not a pg_dump archive\n" * 50)  # well within a pipe's buffer
    with open(read, "rb") as garbage, pytest.raises(
            BackupError, match="pg_restore failed with exit status"):
        verify_restore(owner_dsn, garbage, now=NOW)
    assert _scratch_databases(owner_dsn) == []


def test_a_backup_whose_content_does_not_match_its_own_record_is_not_verified(
    db: Database, owner_db: Database, fresh_database, service_factory, clock, vault: Vault
) -> None:
    """Replay is what makes the content verify itself: a changed row is a mismatch.

    Its reach is what was sent to a model. A row no model call ever used has
    no recorded hash to disagree with; for those, the restore's own integrity
    (one transaction, every foreign key, the row counts) is the evidence.
    """
    owner_dsn = fresh_database["owner_dsn"]
    world = _lived_in(db, service_factory, clock)
    unused = memories.create_memory(db, scope="user", kind="fact", subject="car",
                                    content="Janu's car is grey.", now=clock())

    def rewrite(memory_id: uuid.UUID, content: str) -> None:
        with owner_db.connect() as conn, conn.cursor() as cur:
            cur.execute("ALTER TABLE memory DISABLE TRIGGER USER")
            cur.execute("UPDATE memory SET content = %s WHERE id = %s", (content, memory_id))
            cur.execute("ALTER TABLE memory ENABLE TRIGGER USER")

    rewrite(unused, "Janu's car is pink.")
    assert vault.verify(owner_dsn, vault.back_up(owner_dsn)).verified

    # The door code was sent to the model, and that call's record knows.
    rewrite(world["forgotten"], f"The door code is {SENTINEL[::-1]}.")
    report = vault.verify(owner_dsn, vault.back_up(owner_dsn, taken=NOW + timedelta(hours=1)))
    assert not report.verified and not report.replay.sound
    assert list(report.replay.unexplained) == [world["asked"].invocation_ids[0]]
    assert report.replay.counts[(BundleStatus.MISMATCH, RenderStatus.NOT_ATTEMPTED)] == 1


def test_an_older_backup_is_brought_up_to_date_by_the_restore(
    admin_dsn: str, tmp_path: Path, monkeypatch, vault: Vault
) -> None:
    """A backup taken before a migration existed restores, and the migration is applied."""
    from apollo.storage import db as db_module

    name = f"apollo_test_{uuid.uuid4().hex[:12]}"
    with backups.scratch_database(admin_dsn, name) as old:
        then = tmp_path / "migrations-then"
        then.mkdir()
        for migration in sorted(db_module.MIGRATIONS_DIR.glob("*.sql"))[:4]:
            shutil.copy(migration, then / migration.name)
        with monkeypatch.context() as patched:
            patched.setattr(db_module, "MIGRATIONS_DIR", then)
            assert len(Database(old).migrate()) == 4
        backup = vault.back_up(old)
        report = vault.verify(old, backup)
    assert report.migrations_applied == ("0005_memory_proposals.sql",)
    assert report.verified and report.schema_differences == ()


# -- K.8's two boundaries -------------------------------------------------------------------


def test_a_backup_from_before_a_forget_still_holds_the_claim_and_a_later_one_does_not(
    db: Database, fresh_database, service_factory, clock, vault: Vault
) -> None:
    """ "Deleted and tombstoned content may still exist in historical backups until those
    backups expire." Asserted both ways, so the limit is on record and not just in prose."""
    owner_dsn = fresh_database["owner_dsn"]
    world = _lived_in(db, service_factory, clock)
    before = vault.back_up(owner_dsn, taken=NOW)
    memories.tombstone_memory(db, world["forgotten"], now=clock())
    after = vault.back_up(owner_dsn, taken=NOW + timedelta(days=1))

    def holders(backup: Path) -> set[tuple[str, str]]:
        with backups.scratch_database(
                owner_dsn, f"apollo_restore_{uuid.uuid4().hex[:8]}") as scratch:
            with vault.decrypted(backup) as decrypt:
                backups.restore(scratch, decrypt.stdout)
            return {(table, column) for table, column, _ in _sweep(Database(scratch), SENTINEL)}

    assert holders(before) == {("memory", "content"), ("memory", "search_vector")}
    assert holders(after) == set()

    # Which is why backups expire: after the window, the old one is destroyed.
    later = NOW + timedelta(days=31)
    found = backups.sort_backups(vault.directory, retention_days=30, now=later)
    assert [p.name for p in found.expired] == [before.name]
    assert [p.name for p in found.kept] == [after.name]


# -- the dump is a stream --------------------------------------------------------------------


def test_a_dump_goes_to_a_pipe_and_never_to_a_file(fresh_database, tmp_path: Path) -> None:
    """Spec K.8, as a mechanism: there is no way to ask Apollo for a plaintext dump on disk."""
    plain = tmp_path / "apollo.dump"
    with plain.open("wb") as out, pytest.raises(BackupError, match="not written to a file"):
        backups.dump(fresh_database["owner_dsn"], out)
    assert plain.read_bytes() == b""

    # Into a pipe, with a reader on the other end as an encryption tool would be.
    with subprocess.Popen(["cat"], stdin=subprocess.PIPE, stdout=subprocess.PIPE) as reader:
        archive: list[bytes] = []
        drain = threading.Thread(target=lambda: archive.append(reader.stdout.read()))
        drain.start()
        backups.dump(fresh_database["owner_dsn"], reader.stdin)
        reader.stdin.close()
        drain.join(timeout=30)
    assert archive and archive[0][:5] == b"PGDMP"


def test_connection_details_reach_the_tools_through_the_environment_only(
    monkeypatch
) -> None:
    """A DSN can carry a password, and a command line is visible to every process."""
    seen: list[tuple[list[str], dict[str, str]]] = []

    def record(args: list[str], *, env: dict[str, str], **kwargs: Any) -> Any:
        seen.append((args, env))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(backups.subprocess, "run", record)
    monkeypatch.setenv("PGPASSWORD", "from-the-shell")
    dsn = "host=db.internal port=5433 user=apollo_owner password=s3cret dbname=apollo"
    backups.dump(dsn, io.BytesIO())
    backups.restore(dsn, io.BytesIO())

    (dump_args, dump_env), (restore_args, restore_env) = seen
    assert dump_args == ["pg_dump", "--format=custom", "--no-owner", "--no-privileges"]
    assert restore_args == ["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error",
                            "--single-transaction", "--dbname=apollo"]
    for env in (dump_env, restore_env):
        assert (env["PGHOST"], env["PGPORT"], env["PGUSER"], env["PGPASSWORD"],
                env["PGDATABASE"]) == ("db.internal", "5433", "apollo_owner", "s3cret", "apollo")
    assert "s3cret" not in " ".join(dump_args + restore_args)

    with pytest.raises(BackupError, match="keepalives"):
        backups.libpq_environment("host=x dbname=y keepalives=1")


# -- retention -------------------------------------------------------------------------------


def _touch(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_bytes(b"x")
    return path


def test_backups_past_the_retention_window_are_destroyed_and_nothing_else_is(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("APOLLO_CONFIG", str(REPO_ROOT / "apollo.toml"))
    monkeypatch.delenv("APOLLO_DATABASE_DSN", raising=False)
    monkeypatch.delenv("APOLLO_ADMIN_DSN", raising=False)
    now = datetime.now(UTC)
    old = _touch(tmp_path, backups.backup_name(now - timedelta(days=31), "age"))
    recent = _touch(tmp_path, backups.backup_name(now - timedelta(days=29), "age"))
    other = _touch(tmp_path, "notes.txt")

    assert main(["backup", "prune", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"would destroy  {old.name}" in out and old.exists()

    assert main(["backup", "prune", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert f"destroyed  {old.name}" in out and "retention: 30 days" in out
    assert f"kept       {recent.name}  29d old" in out
    assert "left alone notes.txt" in out
    assert not old.exists() and recent.exists() and other.exists()


def test_a_plaintext_dump_in_the_backup_directory_is_called_out(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("APOLLO_CONFIG", str(REPO_ROOT / "apollo.toml"))
    monkeypatch.delenv("APOLLO_DATABASE_DSN", raising=False)
    plain = _touch(tmp_path, "apollo-20261113T080000Z.dump")
    assert main(["backup", "prune", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert f"PLAINTEXT  {plain.name}: an unencrypted dump." in err
    assert plain.exists()  # his to encrypt or destroy; never removed silently


def test_the_backup_commands_say_what_they_need(monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.setenv("APOLLO_CONFIG", str(REPO_ROOT / "apollo.toml"))
    for name in ("APOLLO_DATABASE_DSN", "APOLLO_ADMIN_DSN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    assert main(["backup", "name", "age"]) == 0
    assert backups.BACKUP_NAME.match(capsys.readouterr().out.strip())
    assert main(["backup", "name", "AGE!"]) == 1
    assert "lowercase letters and digits" in capsys.readouterr().err

    for command in ("dump", "verify"):
        assert main(["backup", command]) == 1
        assert capsys.readouterr().err.splitlines()[-1].startswith(
            f"backup {command}: MissingAdminDsn — APOLLO_ADMIN_DSN is not set")
    assert main(["backup", "prune", str(tmp_path / "nowhere")]) == 1
    assert "NotADirectory" in capsys.readouterr().err


def test_the_cli_verifies_a_backup_from_stdin(
    db: Database, fresh_database, service_factory, clock, vault: Vault, monkeypatch, capsys
) -> None:
    """`age -d -i key.txt FILE | apollo backup verify`, as it is typed."""
    _lived_in(db, service_factory, clock)
    backup = vault.back_up(fresh_database["owner_dsn"])
    monkeypatch.setenv("APOLLO_CONFIG", str(REPO_ROOT / "apollo.toml"))
    monkeypatch.setenv("APOLLO_ADMIN_DSN", fresh_database["owner_dsn"])
    monkeypatch.delenv("APOLLO_DATABASE_DSN", raising=False)

    with vault.decrypted(backup) as decrypt:
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(decrypt.stdout))
        status = main(["backup", "verify"])
    out = capsys.readouterr().out
    assert status == 0
    assert out.splitlines()[0] == "restored into a scratch database, in one transaction"
    assert "schema: identical to a fresh install" in out
    assert "replay: 6 recorded model call(s)" in out
    assert out.splitlines()[-2:] == ["scratch database dropped", "RESTORE VERIFIED"]
    assert SENTINEL not in out and "coffee" not in out
    assert _scratch_databases(fresh_database["owner_dsn"]) == []
