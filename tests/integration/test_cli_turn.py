"""`apollo turn list` and `apollo turn replay` (spec H.4), on `brain.fake`.

What Janu sees when he asks what a turn was actually sent: two statuses per
model call, the references that are gone and why, and (only when he asks for
it) the rebuilt request itself.
"""

from __future__ import annotations

import io
import re
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from apollo.cli import turn as turn_cli
from apollo.cli.main import main
from apollo.storage.db import Database

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET = "zqxsecret7731"
TURN_LINE = re.compile(r"^([0-9a-f-]{36})  \[ *(\d+)\] (\w+) +(\d+) model call\(s\)(?:  (\w+))?$")


@pytest.fixture()
def apollo(db: Database, monkeypatch, capsys):
    """Run `apollo ...` against the test database; returns (status, stdout, stderr)."""
    monkeypatch.setenv("APOLLO_CONFIG", str(REPO_ROOT / "apollo.toml"))
    monkeypatch.setenv("APOLLO_DATABASE_DSN", db._dsn)

    def run(*argv: str, stdin: str = "") -> tuple[int, str, str]:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        capsys.readouterr()
        status = main(list(argv))
        out, err = capsys.readouterr()
        return status, out, err

    return run


def _conversation(apollo: Any) -> str:
    return apollo("new")[1].strip()


def _turns(apollo: Any, conversation: str) -> list[tuple[str, ...]]:
    status, out, _ = apollo("turn", "list", conversation)
    assert status == 0
    return [TURN_LINE.match(line).groups() for line in out.splitlines()]


def _said(apollo: Any, text: str, *flags: str) -> str:
    """Say one thing in a new conversation; the id of the turn it made."""
    conversation = _conversation(apollo)
    assert apollo("say", conversation, text, *flags)[0] == 0
    [turn] = _turns(apollo, conversation)
    return turn[0]


def test_turn_list_gives_the_ids_replay_takes_and_no_message_text(apollo) -> None:
    conversation = _conversation(apollo)
    assert apollo("turn", "list", conversation) == (0, "", "no turns\n")
    apollo("say", conversation, f"My locker code is {SECRET}.")
    apollo("say", conversation, "Remember that I cycle to work.")

    status, out, _ = apollo("turn", "list", conversation)
    first, second = _turns(apollo, conversation)
    assert (first[2], first[3], first[4]) == ("completed", "1", None)
    assert (second[2], second[3]) == ("completed", "2")  # the reply, and the proposal
    assert int(first[1]) < int(second[1])
    assert SECRET not in out and "cycle" not in out


def test_replay_reports_both_statuses_for_each_model_call(apollo) -> None:
    turn = _said(apollo, f"Remember that my locker code is {SECRET}.")
    status, out, err = apollo("turn", "replay", turn)
    assert (status, err) == (0, "")
    assert out.count("bundle: VERIFIED") == 2 and out.count("render: VERIFIED") == 2
    assert "1  reply (completed)" in out and "2  memory_proposal (completed)" in out
    assert "built by compiler-v3, rendered by fake/chat-v3" in out
    # Statuses and ids only. The conversation's text is not printed unasked.
    assert SECRET not in out


def test_context_prints_the_rebuilt_request_only_when_asked(apollo) -> None:
    turn = _said(apollo, f"My locker code is {SECRET}.")
    status, out, _ = apollo("turn", "replay", turn, "--context")
    assert status == 0
    assert "--- request (as sent, verified) ---" in out and "--- end of request ---" in out
    assert "[system]" in out and "[user]" in out
    assert f"My locker code is {SECRET}." in out
    assert "<<<RETRIEVAL_NOTICE tier=T0" in out


def test_a_forgotten_memory_is_named_and_nothing_of_it_is_printed(apollo) -> None:
    """O.1/9 at the terminal: SOURCE_REDACTED, the ref, the reason, and no text."""
    status, out, _ = apollo("memory", "add", "--scope", "user", "--kind", "fact",
                            stdin=f"locker\nJanu's locker code is {SECRET}.\n")
    memory_id = out.strip()
    turn = _said(apollo, "What is my locker code?")
    assert SECRET in apollo("turn", "replay", turn, "--context")[1]

    assert apollo("memory", "forget", memory_id, "--yes")[0] == 0
    status, out, err = apollo("turn", "replay", turn, "--context")
    assert status == 0  # an honest gap, not a failure to reproduce
    assert "bundle: SOURCE_REDACTED — a source was deliberately forgotten" in out
    assert f"memory:{memory_id}  tombstoned" in out
    assert "verification hashes redacted: source_tombstoned" in out
    assert "render: NOT_ATTEMPTED" in out and "(no context: it was not rebuilt)" in out
    assert SECRET not in out and SECRET not in err


def test_a_turn_that_does_not_reproduce_exits_1(apollo, owner_db: Database) -> None:
    turn = _said(apollo, "Hello.")
    with owner_db.connect() as conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE message DISABLE TRIGGER USER")
        cur.execute("UPDATE message SET content = 'Hullo.' WHERE role = 'user'")
        cur.execute("ALTER TABLE message ENABLE TRIGGER USER")
    status, out, _ = apollo("turn", "replay", turn)
    assert status == 1
    assert "bundle: MISMATCH" in out and "something that should never change has changed" in out
    assert "Hullo" not in out


def test_a_failed_attempt_shows_what_it_would_have_sent(apollo, monkeypatch) -> None:
    """O.1/10 at the terminal."""
    from apollo.brains.fake import FakeBrain
    from apollo.errors import BrainTransportError

    def unavailable(self: FakeBrain, req: Any, params: Any) -> None:
        raise BrainTransportError(provider_error_type="connection_failed")

    conversation = _conversation(apollo)
    with monkeypatch.context() as patched:
        patched.setattr(FakeBrain, "generate", unavailable)
        assert apollo("say", conversation, f"The code is {SECRET}.")[0] == 1
    [turn] = _turns(apollo, conversation)
    assert (turn[2], turn[3], turn[4]) == ("failed", "2", "brain_unavailable")

    status, out, _ = apollo("turn", "replay", turn[0], "--context")
    assert status == 0
    assert out.count("bundle: VERIFIED") == 2 and out.count("render: NOT_ATTEMPTED") == 2
    assert "no prompt hash was recorded: the invocation did not complete" in out
    assert out.count("as it would have been sent; nothing was recorded to verify it") == 2
    assert f"The code is {SECRET}." in out


def test_replay_all_counts_every_recorded_call_and_prints_no_text(apollo) -> None:
    """What a restored database is checked with (docs/operations/backup-restore.md)."""
    assert apollo("turn", "replay", "--all") == (0, "0 recorded model call(s)\n", "")
    status, out, _ = apollo("memory", "add", "--scope", "user", "--kind", "fact",
                            stdin=f"locker\nJanu's locker code is {SECRET}.\n")
    memory_id = out.strip()
    _said(apollo, f"Remember that my locker code is {SECRET}.")
    _said(apollo, "What is my locker code?")
    assert apollo("turn", "replay", "--all") == (
        0, "3 recorded model call(s)\n       3  bundle VERIFIED, render VERIFIED\n", "")

    apollo("memory", "forget", memory_id, "--yes")
    status, out, err = apollo("turn", "replay", "--all")
    assert status == 0  # forgotten sources are honest gaps
    assert out.splitlines()[0] == "3 recorded model call(s)"
    assert "bundle SOURCE_REDACTED, render NOT_ATTEMPTED" in out
    assert SECRET not in out + err


def test_replay_all_exits_1_and_names_a_call_that_does_not_reproduce(
    apollo, owner_db: Database
) -> None:
    _said(apollo, "Hello.")
    with owner_db.connect() as conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE message DISABLE TRIGGER USER")
        cur.execute("UPDATE message SET content = 'Hullo.' WHERE role = 'user'")
        cur.execute("ALTER TABLE message ENABLE TRIGGER USER")
        cur.execute("SELECT id FROM model_invocation")
        [invocation] = cur.fetchall()
    status, out, _ = apollo("turn", "replay", "--all")
    assert status == 1
    assert f"DOES NOT REPRODUCE: invocation {invocation['id']}" in out and "Hullo" not in out


def test_replay_takes_one_turn_or_all_and_not_both(apollo) -> None:
    for argv in (("replay",), ("replay", str(uuid.UUID(int=7)), "--all")):
        assert apollo("turn", *argv) == (
            1, "", "turn replay: NothingToReplay — give one turn id, or --all\n")


@pytest.mark.parametrize(("argv", "said"), [
    (("replay", "not-an-id"), "turn replay: InvalidId — that is not an id\n"),
    (("list", "not-an-id"), "turn list: InvalidId — that is not an id\n"),
    (("replay", str(uuid.UUID(int=7))),
     "turn replay: ReplayNotFoundError — no turn has that id "
     "(see `apollo turn list <conversation>`)\n"),
    (("list", str(uuid.UUID(int=7))),
     "turn list: ConversationNotFound — no conversation has that id\n"),
])
def test_a_bad_id_is_refused_by_kind(apollo, argv: tuple[str, ...], said: str) -> None:
    assert apollo("turn", *argv) == (1, "", said)


def test_a_database_error_prints_its_kind_and_never_the_row(apollo, monkeypatch) -> None:
    turn = _said(apollo, f"My locker code is {SECRET}.")

    def refused(*args: Any, **kwargs: Any) -> None:
        raise psycopg.errors.DataError(f"invalid input: {SECRET}")

    monkeypatch.setattr(turn_cli, "replay_turn", refused)
    assert apollo("turn", "replay", turn) == (1, "", "turn replay: DataError\n")
