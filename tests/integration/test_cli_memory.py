"""`apollo memory`: the terminal client for direct entry (step 10 PR 5; spec D.4, D.9/5).

Claim text arrives on stdin, prompted or piped, and there is no argument that
takes it. Errors print as their kind: no exception message, which for a
database error can quote the refused row, reaches the terminal.
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from apollo.cli import memory as memory_cli
from apollo.cli.main import main
from apollo.core.memories import get_memory
from apollo.memory.models import Status, Support
from apollo.storage.db import Database

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET = "zqxsecret7731"


class Tty(io.StringIO):
    """Typed input at a terminal."""

    def isatty(self) -> bool:
        return True


@pytest.fixture()
def apollo(db: Database, monkeypatch, capsys):
    """Run `apollo memory ...` against the test database; returns (status, stdout, stderr)."""
    monkeypatch.setenv("APOLLO_CONFIG", str(REPO_ROOT / "apollo.toml"))
    monkeypatch.setenv("APOLLO_DATABASE_DSN", db._dsn)

    def run(*argv: str, stdin: io.StringIO | None = None) -> tuple[int, str, str]:
        monkeypatch.setattr("sys.stdin", stdin if stdin is not None else io.StringIO(""))
        capsys.readouterr()
        status = main(["memory", *argv])
        out, err = capsys.readouterr()
        return status, out, err

    return run


def _add(apollo: Any, subject: str = "door code", content: str = f"it is {SECRET}") -> uuid.UUID:
    status, out, _ = apollo(
        "add", "--scope", "user", "--kind", "fact", stdin=io.StringIO(f"{subject}\n{content}\n")
    )
    assert status == 0
    return uuid.UUID(out.strip())


def _audit_count(db: Database, event_type: str) -> int:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM audit_event WHERE event_type = %s", (event_type,))
        row = cur.fetchone()
    assert row is not None
    return int(row["n"])


# ---------------------------------------------------------------------------
# claim text comes from stdin, never argv
# ---------------------------------------------------------------------------


def test_piped_add_takes_the_first_line_as_subject_and_the_rest_as_content(
    db: Database, apollo: Any
) -> None:
    memory_id = _add(apollo, content=f"line one {SECRET}\nline two")
    memory = get_memory(db, memory_id)
    assert (memory.subject, memory.content) == ("door code", f"line one {SECRET}\nline two")
    assert memory.status is Status.ACTIVE and memory.support is Support.ASSERTED
    assert _audit_count(db, "memory.created") == 1


def test_add_at_a_terminal_prompts_on_stderr_and_prints_only_the_id(
    db: Database, apollo: Any
) -> None:
    status, out, err = apollo(
        "add", "--scope", "self", "--kind", "preference", "--pinned",
        stdin=Tty(f"tea\nprefers {SECRET} tea\n"),
    )
    assert status == 0
    assert "subject: " in err and "content: " in err
    memory = get_memory(db, uuid.UUID(out.strip()))
    assert memory.content == f"prefers {SECRET} tea" and memory.pinned


@pytest.mark.parametrize(
    "argv",
    [
        ["add", "--scope", "user", "--kind", "fact", f"the code is {SECRET}"],
        ["add", "--scope", "user", "--kind", "fact", "--content", SECRET],
        ["correct", str(uuid.uuid4()), f"the code is {SECRET}"],
    ],
)
def test_no_argument_takes_claim_text(apollo: Any, argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        apollo(*argv)
    assert exc.value.code == 2  # argparse refuses the extra argument


def test_no_parser_option_is_named_for_claim_text() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    memory_cli.add_parser(parser.add_subparsers())
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    memory_parser = sub.choices["memory"]
    commands = next(
        a for a in memory_parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    for name, command in commands.choices.items():
        dests = {action.dest for action in command._actions}
        assert not dests & {"subject", "content", "text", "claim"}, name


# ---------------------------------------------------------------------------
# the lifecycle through the CLI
# ---------------------------------------------------------------------------


def test_correct_reads_the_new_content_and_supersedes(db: Database, apollo: Any) -> None:
    old = _add(apollo)
    status, out, _ = apollo("correct", str(old), stdin=io.StringIO("it is 5555\n"))
    assert status == 0
    new = uuid.UUID(out.strip())
    assert get_memory(db, new).content == "it is 5555"
    assert get_memory(db, old).superseded_by_id == new
    status, out, _ = apollo("show", str(old))
    assert "status:      superseded" in out and f"replaced by: {new}" in out


def test_evidence_archive_restore_and_list(db: Database, apollo: Any) -> None:
    memory_id = _add(apollo)
    assert apollo("confirm", str(memory_id))[1] == f"confirmed {memory_id}\n"
    assert apollo("contradict", str(memory_id))[1] == f"contradicted {memory_id}\n"
    assert get_memory(db, memory_id).support is Support.CONTESTED

    assert apollo("archive", str(memory_id))[1] == f"archived {memory_id}\n"
    assert str(memory_id) not in apollo("list")[1]
    assert str(memory_id) in apollo("list", "--status", "archived")[1]
    assert apollo("restore", str(memory_id))[1] == f"restored {memory_id}\n"
    listed = apollo("list")[1]
    assert str(memory_id) in listed and "contested" in listed


def test_list_all_includes_every_status(apollo: Any) -> None:
    kept, gone = _add(apollo), _add(apollo, content="another")
    apollo("forget", str(gone), "--yes")
    listed = apollo("list", "--status", "all")[1]
    assert str(kept) in listed and str(gone) in listed and "(forgotten)" in listed


# ---------------------------------------------------------------------------
# forget
# ---------------------------------------------------------------------------


def test_forget_needs_confirmation_when_piped(db: Database, apollo: Any) -> None:
    memory_id = _add(apollo)
    status, _, err = apollo("forget", str(memory_id), stdin=io.StringIO("forget\n"))
    assert status == 2 and "--yes" in err
    assert get_memory(db, memory_id).status is Status.ACTIVE


def test_forget_at_a_terminal_needs_the_word_typed(db: Database, apollo: Any) -> None:
    memory_id = _add(apollo)
    assert apollo("forget", str(memory_id), stdin=Tty("y\n"))[0] == 2
    assert get_memory(db, memory_id).status is Status.ACTIVE
    status, out, _ = apollo("forget", str(memory_id), stdin=Tty("forget\n"))
    assert status == 0 and out.startswith("forgotten: 1 version(s)")
    assert get_memory(db, memory_id).status is Status.TOMBSTONED


def test_forget_takes_the_whole_chain_and_show_prints_no_text(
    db: Database, apollo: Any
) -> None:
    old = _add(apollo)
    new = uuid.UUID(apollo("correct", str(old), stdin=io.StringIO(f"now {SECRET}2\n"))[1].strip())
    status, out, _ = apollo("forget", str(old), "--yes")
    assert status == 0
    assert out.splitlines() == ["forgotten: 2 version(s)", f"  {new}", f"  {old}"]
    for memory_id in (old, new):
        shown = apollo("show", str(memory_id))[1]
        assert "status:      tombstoned" in shown
        assert "subject:     (forgotten)" in shown and "content:     (forgotten)" in shown
        assert SECRET not in shown


# ---------------------------------------------------------------------------
# errors print as their kind, never their text
# ---------------------------------------------------------------------------


def test_lifecycle_errors_print_the_kind_and_a_fixed_hint(apollo: Any) -> None:
    old = _add(apollo)
    apollo("correct", str(old), stdin=io.StringIO("replacement\n"))
    status, out, err = apollo("confirm", str(old))
    assert status == 1 and out == ""
    assert err == (
        "memory confirm: LifecycleError — not allowed from the memory's current status"
        " (see `apollo memory show`)\n"
    )
    assert apollo("show", str(uuid.uuid4()))[2] == (
        "memory show: MemoryNotFoundError — no memory has that id\n"
    )
    assert apollo("archive", f"not-an-id-{SECRET}")[2] == (
        "memory archive: InvalidMemoryId — that is not a memory id\n"
    )


def test_refused_input_does_not_echo_the_claim(db: Database, apollo: Any) -> None:
    status, out, err = apollo(
        "add", "--scope", "user", "--kind", "fact", stdin=io.StringIO(f"{SECRET}\n\n")
    )
    assert status == 1 and out == ""
    assert err == (
        "memory add: MemoryInputError — the subject or content was empty or not valid text\n"
    )
    assert _audit_count(db, "memory.created") == 0


def test_a_database_error_prints_its_kind_never_the_failing_row(
    apollo: Any, monkeypatch
) -> None:
    def refused(*args: Any, **kwargs: Any) -> None:
        raise psycopg.errors.CheckViolation(
            'new row for relation "memory" violates check constraint "x"\n'
            f"DETAIL:  Failing row contains (door code, it is {SECRET})."
        )

    monkeypatch.setattr(memory_cli, "create_memory", refused)
    status, out, err = apollo(
        "add", "--scope", "user", "--kind", "fact",
        stdin=io.StringIO(f"door code\nit is {SECRET}\n"),
    )
    assert status == 1 and out == ""
    assert err == "memory add: CheckViolation\n"
    assert SECRET not in err and "Failing row" not in err


def test_a_connection_error_prints_its_kind_never_the_dsn(apollo: Any, monkeypatch) -> None:
    monkeypatch.setenv(
        "APOLLO_DATABASE_DSN",
        f"host=127.0.0.1 port=1 user=nobody password={SECRET} dbname=none connect_timeout=2",
    )
    status, out, err = apollo("list")
    assert status == 1 and out == ""
    assert err == "memory list: OperationalError\n"
