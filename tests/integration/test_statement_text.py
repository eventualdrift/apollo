"""Claim and conversation text travel only as bind parameters, never in SQL text.

PostgreSQL's statement logging (`log_statement`, `log_min_duration_statement`)
writes the statement text; bind values go on a separate `DETAIL: parameters:`
line, which `log_error_verbosity = terse` drops (README, "PostgreSQL's own
log"). That makes terse sufficient only while Apollo never puts user or model
text into the statement itself. This test records the text of every statement
Apollo sends during a real turn and the memory commands, and holds it.
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

from apollo.cli.main import main
from apollo.core.conversations import create_conversation
from apollo.storage.db import Database

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SENTINEL = "zqxstatement4417"


@pytest.fixture()
def statements(monkeypatch) -> list[tuple[str, str]]:
    """(statement text, parameters) for everything any psycopg cursor executes."""
    sent: list[tuple[str, str]] = []

    def text_of(cursor: psycopg.Cursor[Any], query: Any) -> str:
        if isinstance(query, sql.Composable):
            return query.as_string(cursor.connection)
        return query.decode() if isinstance(query, bytes) else str(query)

    execute, executemany = psycopg.Cursor.execute, psycopg.Cursor.executemany

    def recording_execute(self: psycopg.Cursor[Any], query: Any, params: Any = None, **kw: Any):
        sent.append((text_of(self, query), repr(params)))
        return execute(self, query, params, **kw)

    def recording_executemany(self: psycopg.Cursor[Any], query: Any, params_seq: Any, **kw: Any):
        params_seq = list(params_seq)
        sent.append((text_of(self, query), repr(params_seq)))
        return executemany(self, query, params_seq, **kw)

    monkeypatch.setattr(psycopg.Cursor, "execute", recording_execute)
    monkeypatch.setattr(psycopg.Cursor, "executemany", recording_executemany)
    return sent


def test_no_user_or_model_text_is_ever_part_of_a_statement(
    db: Database, service, clock, statements: list[tuple[str, str]], monkeypatch, capsys
) -> None:
    # A real turn: the user's message, and a brain.fake (echo) reply that repeats it.
    conversation_id = create_conversation(db, now=clock())
    result = service.submit(
        conversation_id=conversation_id, text=f"Please remember the code {SENTINEL}"
    )
    assert result.status == "completed" and SENTINEL in result.response

    # The memory commands, through the CLI.
    monkeypatch.setenv("APOLLO_CONFIG", str(REPO_ROOT / "apollo.toml"))
    monkeypatch.setenv("APOLLO_DATABASE_DSN", db._dsn)

    def cli(*argv: str, stdin: str = "") -> str:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        capsys.readouterr()
        assert main(["memory", *argv]) == 0
        return capsys.readouterr().out

    old = uuid.UUID(cli("add", "--scope", "user", "--kind", "fact",
                        stdin=f"code {SENTINEL}\nthe code is {SENTINEL}\n").strip())
    new = uuid.UUID(cli("correct", str(old), stdin=f"it is {SENTINEL} now\n").strip())
    cli("confirm", str(new))
    cli("contradict", str(new))
    cli("forget", str(old), "--yes")

    carrying = [(text, params) for text, params in statements if SENTINEL in params]
    # The text really went through the recorder: the user's message, Apollo's
    # reply, and the memory's insert and correction all carried it as parameters.
    assert sum("INSERT INTO message" in text for text, _ in carrying) == 2
    assert sum("INSERT INTO memory " in text for text, _ in carrying) == 2
    leaked = [text for text, _ in statements if SENTINEL in text]
    assert leaked == []
    assert len(statements) > 20  # sanity: the whole flow was recorded
