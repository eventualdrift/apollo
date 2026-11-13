"""`apollo proposal`, `apollo say --remember` and `/remember` in `apollo chat` (spec D.2, D.3).

The capture flow as Janu meets it at a terminal, on `brain.fake`: a message
that asks Apollo to remember something ends with an offer, and nothing is a
memory until one of three commands says so. What a real model makes of the
structuring request is not shown here; `brain.fake` answers it from the
message's own words.
"""

from __future__ import annotations

import argparse
import io
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from apollo.cli import memory as memory_cli
from apollo.cli import proposal as proposal_cli
from apollo.cli.main import main
from apollo.core.memories import get_memory
from apollo.memory.models import Kind, OriginTier, Scope
from apollo.storage.db import Database

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET = "zqxsecret7731"
ASK = f"Remember that my locker code is {SECRET}"
ID = re.compile(r"memory proposal ([0-9a-f-]{36})")


class Tty(io.StringIO):
    """Typed input at a terminal."""

    def isatty(self) -> bool:
        return True


@pytest.fixture()
def apollo(db: Database, monkeypatch, capsys):
    """Run `apollo ...` against the test database; returns (status, stdout, stderr)."""
    monkeypatch.setenv("APOLLO_CONFIG", str(REPO_ROOT / "apollo.toml"))
    monkeypatch.setenv("APOLLO_DATABASE_DSN", db._dsn)

    def run(*argv: str, stdin: io.StringIO | None = None) -> tuple[int, str, str]:
        monkeypatch.setattr("sys.stdin", stdin if stdin is not None else io.StringIO(""))
        capsys.readouterr()
        status = main(list(argv))
        out, err = capsys.readouterr()
        return status, out, err

    return run


def _conversation(apollo: Any) -> str:
    status, out, _ = apollo("new")
    assert status == 0
    return out.strip()


def _propose(apollo: Any, text: str = ASK, *flags: str) -> uuid.UUID:
    status, out, _ = apollo("say", _conversation(apollo), text, *flags)
    assert status == 0
    found = ID.search(out)
    assert found is not None, out
    return uuid.UUID(found.group(1))


def _count(db: Database, table: str) -> int:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM {table}")
        row = cur.fetchone()
    assert row is not None
    return int(row["n"])


# -- the offer ---------------------------------------------------------------------------


def test_a_message_that_asks_to_be_remembered_ends_with_an_offer(db: Database, apollo) -> None:
    status, out, err = apollo("say", _conversation(apollo), ASK)
    assert status == 0
    reply, offer = out.split("memory proposal ", 1)
    assert reply.startswith("apollo> ")
    proposal_id = offer.split()[0]
    assert "[user/fact]" in offer and ASK in offer
    for command in ("save", "edit", "ignore"):
        assert f"{command}: apollo proposal {command} {proposal_id}" in offer
    # Offered, not saved.
    assert _count(db, "memory") == 0 and _count(db, "memory_proposal") == 1


def test_an_ordinary_message_ends_with_no_offer(db: Database, apollo) -> None:
    status, out, _ = apollo("say", _conversation(apollo), f"My locker code is {SECRET}")
    assert status == 0 and "memory proposal" not in out
    assert _count(db, "memory_proposal") == 0


def test_the_remember_flag_proposes_whatever_the_wording(db: Database, apollo) -> None:
    proposal_id = _propose(apollo, f"My locker code is {SECRET}", "--remember")
    status, out, _ = apollo("proposal", "show", str(proposal_id))
    assert status == 0 and "status:      pending" in out and SECRET in out


def test_a_proposal_that_could_not_be_made_is_said_and_the_reply_still_arrives(
    db: Database, apollo, monkeypatch
) -> None:
    monkeypatch.setattr("apollo.brains.fake._structured_claim", lambda source: "Sure, noted!")
    status, out, err = apollo("say", _conversation(apollo), ASK)
    assert status == 0 and out.startswith("apollo> ") and "memory proposal" not in out
    assert "could not make a memory proposal" in err and "apollo memory add" in err
    assert _count(db, "memory_proposal") == 0 and _count(db, "memory") == 0


# -- save, edit, ignore ------------------------------------------------------------------


def test_save_makes_the_memory_and_clears_the_proposal(db: Database, apollo) -> None:
    proposal_id = _propose(apollo)
    status, out, err = apollo("proposal", "save", str(proposal_id))
    assert (status, err) == (0, "saved\n")
    memory = get_memory(db, uuid.UUID(out.strip()))
    assert (memory.scope, memory.kind, memory.origin_tier) == (
        Scope.USER, Kind.FACT, OriginTier.USER_ASSERTED,
    )
    assert SECRET in memory.content

    status, out, err = apollo("proposal", "list")
    assert (status, out, err) == (0, "", "no proposals\n")
    status, out, _ = apollo("proposal", "list", "--status", "saved")
    assert str(proposal_id) in out and "(cleared)" in out and SECRET not in out
    status, out, _ = apollo("proposal", "show", str(proposal_id))
    assert f"memory:      {memory.id}" in out and "content:     (cleared)" in out


def test_edit_reads_the_change_from_stdin_and_blank_keeps(db: Database, apollo) -> None:
    proposal_id = _propose(apollo)
    status, out, err = apollo(
        "proposal", "edit", str(proposal_id), "--kind", "constraint",
        stdin=io.StringIO("\nThe locker code changes monthly.\n"),
    )
    assert (status, err) == (0, "saved, as edited\n")
    memory = get_memory(db, uuid.UUID(out.strip()))
    assert memory.kind is Kind.CONSTRAINT
    assert memory.subject == "Remember that my locker"  # the blank first line kept it
    assert memory.content == "The locker code changes monthly."


def test_edit_at_a_terminal_prompts_with_the_current_subject(db: Database, apollo) -> None:
    proposal_id = _propose(apollo)
    status, out, err = apollo("proposal", "edit", str(proposal_id),
                              stdin=Tty("locker\n\n"))
    assert status == 0 and "subject [Remember that my locker]: " in err
    memory = get_memory(db, uuid.UUID(out.strip()))
    assert memory.subject == "locker" and SECRET in memory.content


def test_edit_cannot_ask_for_self_scope(db: Database, apollo, capsys) -> None:
    """O.1/16 at the terminal: the option is not offered."""
    proposal_id = _propose(apollo)
    with pytest.raises(SystemExit) as refused:
        apollo("proposal", "edit", str(proposal_id), "--scope", "self")
    assert refused.value.code == 2 and "invalid choice: 'self'" in capsys.readouterr().err
    assert _count(db, "memory") == 0


def test_ignore_saves_nothing(db: Database, apollo) -> None:
    proposal_id = _propose(apollo)
    status, out, _ = apollo("proposal", "ignore", str(proposal_id))
    assert (status, out) == (0, f"ignored {proposal_id}\n")
    assert _count(db, "memory") == 0
    status, out, _ = apollo("proposal", "show", str(proposal_id))
    assert "status:      ignored" in out and SECRET not in out


def test_stats_counts_what_happened_to_every_proposal(db: Database, apollo) -> None:
    saved, edited, ignored, _left = (_propose(apollo) for _ in range(4))
    apollo("proposal", "save", str(saved))
    apollo("proposal", "edit", str(edited), stdin=io.StringIO("\nedited\n"))
    apollo("proposal", "ignore", str(ignored))
    status, out, _ = apollo("proposal", "stats")
    assert status == 0
    by_status, by_turn, by_day = out.split("\n\n")
    assert dict(line.split() for line in by_status.splitlines()) == {
        "pending": "1", "saved": "1", "saved_edited": "1", "ignored": "1", "expired": "0",
    }
    # Spec O.2: capture rate, and conversations started per day. Counts and dates only.
    assert by_turn.splitlines() == [
        "answered turns in your conversations: 4",
        "  made a proposal                           4",
        "  asked for one that could not be made      0",
        "  asked for none                            0",
    ]
    heading, *days = by_day.splitlines()
    assert heading == "conversations started, by day (UTC)"
    # Each `_propose` started its own conversation, all on the test clock's one day.
    assert [line.split()[1] for line in days] == ["4"] and SECRET not in out


# -- expiry ------------------------------------------------------------------------------


def _later(db: Database, days: int, *argv: str) -> tuple[int, str, str]:
    """`apollo proposal ...` run `days` from now."""
    parser = argparse.ArgumentParser()
    proposal_cli.add_parser(parser.add_subparsers(dest="command"))
    out, err = io.StringIO(), io.StringIO()
    term = memory_cli.Terminal(io.StringIO(""), out, err,
                               lambda: datetime.now(UTC) + timedelta(days=days))
    status = proposal_cli.run(parser.parse_args(["proposal", *argv]), db, term,
                              expiry=timedelta(days=7))
    return status, out.getvalue(), err.getvalue()


def test_a_proposal_left_for_a_week_is_gone_from_the_list_and_cannot_be_saved(
    db: Database, apollo
) -> None:
    proposal_id = _propose(apollo)
    assert str(proposal_id) in _later(db, 6, "list")[1]

    status, out, err = _later(db, 8, "list")
    assert (status, out, err) == (0, "", "no proposals\n")
    status, out, err = _later(db, 8, "save", str(proposal_id))
    assert status == 1 and out == "" and err.startswith("proposal save: ProposalError")
    assert _count(db, "memory") == 0
    status, out, _ = _later(db, 8, "show", str(proposal_id))
    assert "status:      expired" in out and SECRET not in out


# -- errors print as their kind ----------------------------------------------------------


@pytest.mark.parametrize(("argv", "kind"), [
    (("save", "not-an-id"), "InvalidProposalId"),
    (("show", "not-an-id"), "InvalidProposalId"),
    (("save", "00000000-0000-7000-8000-000000000000"), "ProposalNotFoundError"),
    (("ignore", "00000000-0000-7000-8000-000000000000"), "ProposalNotFoundError"),
])
def test_a_bad_id_is_refused_by_kind(apollo, argv: tuple[str, ...], kind: str) -> None:
    status, out, err = apollo("proposal", *argv)
    assert status == 1 and out == ""
    assert err.startswith(f"proposal {argv[0]}: {kind} — ")


def test_a_proposal_cannot_be_resolved_twice(db: Database, apollo) -> None:
    proposal_id = _propose(apollo)
    assert apollo("proposal", "save", str(proposal_id))[0] == 0
    for command in ("save", "ignore"):
        status, out, err = apollo("proposal", command, str(proposal_id))
        assert status == 1 and err.startswith(f"proposal {command}: ProposalError — ")
    assert _count(db, "memory") == 1


def test_a_database_error_prints_its_kind_and_never_the_row(
    db: Database, apollo, monkeypatch
) -> None:
    proposal_id = _propose(apollo)

    def refused(*args: Any, **kwargs: Any) -> None:
        raise psycopg.errors.CheckViolation(
            f'new row violates check constraint\nDETAIL:  Failing row contains ({SECRET}).'
        )

    monkeypatch.setattr(proposal_cli, "save_proposal", refused)
    status, out, err = apollo("proposal", "save", str(proposal_id))
    assert (status, out) == (1, "")
    assert err == "proposal save: CheckViolation\n"
    assert SECRET not in err and "Failing row" not in err


# -- apollo chat -------------------------------------------------------------------------


def test_chat_at_a_terminal_asks_at_once_and_saves_on_s(db: Database, apollo) -> None:
    conversation = _conversation(apollo)
    status, out, err = apollo("chat", conversation, stdin=Tty(f"{ASK}\ns\n"))
    assert status == 0
    assert "memory proposal " in out and "  saved as a memory" in out
    assert "[s]ave, [e]dit, [i]gnore, or enter to decide later: " in err
    assert _count(db, "memory") == 1
    # The answer was an answer, not a second message to Apollo.
    assert _count(db, "turn") == 1


def test_chat_edits_inline(db: Database, apollo) -> None:
    conversation = _conversation(apollo)
    status, out, _ = apollo(
        "chat", conversation, stdin=Tty(f"{ASK}\ne\nlocker\nThe code is on the card.\n")
    )
    assert status == 0 and "saved as a memory, as edited" in out
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT subject, content FROM memory")
        [memory] = cur.fetchall()
    assert (memory["subject"], memory["content"]) == ("locker", "The code is on the card.")


def test_chat_leaves_a_proposal_pending_when_the_answer_is_enter(db: Database, apollo) -> None:
    conversation = _conversation(apollo)
    status, out, _ = apollo("chat", conversation, stdin=Tty(f"{ASK}\n\n"))
    assert status == 0 and "left pending: apollo proposal list (expires in 7 days)" in out
    assert _count(db, "memory") == 0
    status, out, _ = apollo("proposal", "list")
    assert SECRET in out


def test_the_remember_command_in_chat_proposes_from_the_rest_of_the_line(
    db: Database, apollo
) -> None:
    conversation = _conversation(apollo)
    status, out, _ = apollo(
        "chat", conversation,
        stdin=io.StringIO(f"/remember my locker code is {SECRET}\n/rememberthis is not it\n"),
    )
    assert status == 0
    # Piped, so it is offered with its commands rather than asked.
    assert out.count("memory proposal ") == 1 and "save: apollo proposal save " in out
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT content FROM message WHERE role = 'user' ORDER BY seq")
        said = [row["content"] for row in cur.fetchall()]
    # The command is the client's; the message is what followed it.
    assert said == [f"my locker code is {SECRET}", "/rememberthis is not it"]
