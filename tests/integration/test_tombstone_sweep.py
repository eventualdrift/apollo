"""The sentinel sweep: after a tombstone, the claim is nowhere Apollo keeps text (O.1/18).

One scenario, end to end, as the runtime role and through the real entry
points. A user message carries a sentinel; a memory is made from it through
`apollo memory add`, given an excerpt that quotes the message, corrected (so
the chain has two versions), confirmed, and included in a recorded
invocation; then `apollo memory forget` removes it. Afterwards the sentinel
and the redacted bundle hash are looked for in:

* every character, json/jsonb, tsvector and array column of every table,
  enumerated live from `information_schema` (cast to text), so a column added
  later is swept without editing this test;
* every captured log line, from the first write to the last read;
* everything the CLI printed;
* a brain.fake persona run record written after the tombstone;
* the reply bundle rendered after the tombstone, as the brain received it.

The one place the sentinel may remain is the source message's own content:
deletion in phase zero is logical, and a tombstone removes the claim, not the
conversation it came from (README, "Deleting a memory"). The reply bundle is
rendered in a new conversation, since the source conversation's history
legitimately still holds that message.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import textwrap
import uuid
from pathlib import Path
from typing import Any

import pytest

from apollo.brains.base import Generation, GenerationParams, RenderedRequest
from apollo.brains.fake import FakeBrain
from apollo.brains.registry import BrainRegistry
from apollo.config import SURFACE_EVAL
from apollo.core.conversations import create_conversation
from apollo.core.identity import IdentityLoader
from apollo.evals.loader import load_case_file
from apollo.evals.runner import PersonaRunner
from apollo.logging_setup import configure_logging
from apollo.memory.models import included_manifest_entry
from apollo.storage.db import Database

from .conftest import make_config

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SENTINEL = "zqxsentinel5501"  # lower-case, so it survives to_tsvector as one lexeme
REDACTED_HASH = "sha256:" + hashlib.sha256(SENTINEL.encode()).hexdigest()

#: Column types the sweep reads. `information_schema.columns.data_type` names
#: arrays `ARRAY`; every other type here is named as written.
SWEPT_TYPES = (
    "text", "character varying", "character", "json", "jsonb", "tsvector", "ARRAY",
)

CASES = """\
version: 1
cases:
  - id: per_sweep
    tags: [form]
    covers_rules: [B18]
    covers_probes: [verbosity]
    behavioural_expectation: Short answer to a closed question.
    undesired_characteristics: [expands a one-line answer]
    setup:
      mode: benchmark
      memories: []
      history: []
    input: What is the door code?
    checks:
      - type: max_words
        value: 500
"""


class RecordingBrain(FakeBrain):
    """brain.fake, keeping every rendered request it was sent."""

    def __init__(self) -> None:
        super().__init__(key="fake")
        self.requests: list[RenderedRequest] = []

    def generate(self, req: RenderedRequest, params: GenerationParams) -> Generation:
        self.requests.append(req)
        return super().generate(req, params)


def _columns(conn: Any) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.table_name, c.column_name FROM information_schema.columns c"
            " JOIN information_schema.tables t"
            "   ON t.table_schema = c.table_schema AND t.table_name = c.table_name"
            " WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'"
            "   AND c.data_type = ANY(%s)"
            " ORDER BY 1, 2",
            (list(SWEPT_TYPES),),
        )
        return [(row["table_name"], row["column_name"]) for row in cur.fetchall()]


def _sweep(owner_db: Database, needle: str) -> set[tuple[str, str, str | None]]:
    """(table, column, row id) for every swept value containing the needle."""
    from psycopg import sql

    hits: set[tuple[str, str, str | None]] = set()
    with owner_db.connect() as conn, conn.cursor() as cur:
        columns = _columns(conn)
        # The enumeration must see the columns that matter, or it proves nothing.
        assert {
            ("message", "content"),
            ("memory", "subject"),
            ("memory", "content"),
            ("memory", "search_vector"),
            ("memory_observation", "excerpt"),
            ("model_invocation", "context_manifest"),
            ("model_invocation", "context_bundle_hash"),
            ("audit_event", "payload"),
        } <= set(columns)
        for table, column in columns:
            cur.execute(
                sql.SQL(
                    "SELECT to_jsonb(t) ->> 'id' AS id FROM {} t WHERE {}::text LIKE %s"
                ).format(sql.Identifier(table), sql.Identifier(column)),
                (f"%{needle}%",),
            )
            hits.update((table, column, row["id"]) for row in cur.fetchall())
    return hits


@pytest.fixture()
def cli(db: Database, monkeypatch, capsys):
    """`apollo memory ...` through `main`, logging to the test's stream; collects its output."""
    from apollo.cli.main import main

    monkeypatch.setenv("APOLLO_CONFIG", str(REPO_ROOT / "apollo.toml"))
    monkeypatch.setenv("APOLLO_DATABASE_DSN", db._dsn)
    printed: list[str] = []

    def run(*argv: str, stdin: str = "") -> str:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        capsys.readouterr()
        assert main(["memory", *argv]) == 0
        out, err = capsys.readouterr()
        printed.append(out + err)
        return out

    run.printed = printed  # type: ignore[attr-defined]
    return run


def test_after_a_tombstone_the_claim_survives_only_in_its_source_message(
    db: Database, owner_db: Database, service_factory, clock, cli, monkeypatch, tmp_path
) -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    # The CLI configures logging itself; keep it writing to the same captured sink.
    monkeypatch.setattr(
        "apollo.cli.main.configure_logging", lambda level: configure_logging("DEBUG", stream)
    )
    try:
        # A real turn: the user says it, Apollo replies.
        source_conversation = create_conversation(db, now=clock())
        result = service_factory().submit(
            conversation_id=source_conversation,
            text=f"Remember this: the door code is {SENTINEL}.\n\nThanks!",
        )
        assert result.status == "completed"
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM message WHERE conversation_id = %s AND role = 'user'",
                (source_conversation,),
            )
            source_message = cur.fetchone()["id"]

        # The memory: direct entry, an excerpt quoting the message, a correction,
        # a confirmation, and an invocation whose manifest included both versions.
        old = uuid.UUID(
            cli("add", "--scope", "user", "--kind", "fact",
                stdin=f"door code\nthe door code is {SENTINEL}\n").strip()
        )
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO memory_observation (id, memory_id, relation, source_kind,"
                " message_id, excerpt, observed_at, created_at) VALUES (%s, %s, 'confirms',"
                " 'user_message', %s, %s, now(), now())",
                (uuid.uuid4(), old, source_message, f"the door code is {SENTINEL}"),
            )
        new = uuid.UUID(cli("correct", str(old), stdin=f"it is {SENTINEL}, still\n").strip())
        cli("confirm", str(new))
        invocation = _invocation_including(db, [old, new], clock)

        # Before the tombstone the sweep finds the claim where it lives, tsvector included.
        assert {
            ("message", "content"),
            ("memory", "content"),
            ("memory", "search_vector"),
            ("memory_observation", "excerpt"),
        } <= {(table, column) for table, column, _ in _sweep(owner_db, SENTINEL)}
        assert ("model_invocation", "context_bundle_hash") in {
            (table, column) for table, column, _ in _sweep(owner_db, REDACTED_HASH)
        }

        cli("forget", str(old), "--yes")

        # Everything read back after the tombstone.
        cli("show", str(old))
        cli("show", str(new))
        cli("list", "--status", "all")

        brain = RecordingBrain()
        reply = service_factory(brain=brain).submit(
            conversation_id=create_conversation(db, now=clock()), text="What is the door code?"
        )
        assert reply.status == "completed" and brain.requests
        rendered = json.dumps(
            [[m.content for m in req.messages] for req in brain.requests], ensure_ascii=False
        )

        runs = tmp_path / "runs"
        cases_path = tmp_path / "cases.yaml"
        cases_path.write_text(textwrap.dedent(CASES), encoding="utf-8")
        config = make_config(db._dsn)
        runner = PersonaRunner(
            db, config, BrainRegistry(config, surface=SURFACE_EVAL),
            IdentityLoader(config.identity_dir), clock=clock, runs_dir=runs,
        )
        runner.write(runner.run(load_case_file(cases_path), brain_alias="brain.default"))
        run_records = "".join(p.read_text(encoding="utf-8") for p in sorted(runs.glob("*.json")))
        assert run_records
    finally:
        logging.getLogger().handlers.clear()

    # The database: only the source message still holds the claim.
    assert _sweep(owner_db, SENTINEL) == {("message", "content", str(source_message))}
    assert _sweep(owner_db, REDACTED_HASH) == set()
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT hashes_redacted_reason FROM model_invocation WHERE id = %s", (invocation,)
        )
        assert cur.fetchone()["hashes_redacted_reason"] == "source_tombstoned"

    # Everything else Apollo wrote or printed.
    logs = stream.getvalue()
    printed = "".join(cli.printed)
    for where, text in {
        "logs": logs,
        "CLI output": printed,
        "reply bundle": rendered,
        "persona run record": run_records,
    }.items():
        assert SENTINEL not in text, where
        assert REDACTED_HASH not in text, where
    assert "(forgotten)" in printed and "tombstoned" in printed
    assert "context.compiled" in logs  # the log sink really was captured


def _invocation_including(db: Database, memory_ids: list[uuid.UUID], clock: Any) -> uuid.UUID:
    """A completed invocation whose manifest included the memories (step 11 will make these)."""
    now = clock()
    conversation_id, message_id, turn_id, invocation_id = (uuid.uuid4() for _ in range(4))
    manifest = [entry for m in memory_ids for entry in included_manifest_entry(m)]
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversation (id, mode, started_at, last_active_at)"
            " VALUES (%s, 'personal', %s, %s)",
            (conversation_id, now, now),
        )
        cur.execute(
            "INSERT INTO message (id, conversation_id, seq, role, content, created_at)"
            " VALUES (%s, %s, 1, 'user', 'hello', %s)",
            (message_id, conversation_id, now),
        )
        cur.execute(
            "INSERT INTO turn (id, conversation_id, request_message_id, status,"
            " conversation_mode, identity_version, identity_hash, started_at)"
            " VALUES (%s, %s, %s, 'started', 'personal', 'v', 'h', %s)",
            (turn_id, conversation_id, message_id, now),
        )
        cur.execute(
            "INSERT INTO model_invocation (id, turn_id, seq, purpose, brain_alias, provider_key,"
            " adapter_key, render_version, compiler_version, token_estimator, context_manifest,"
            " context_bundle_hash, context_token_estimate, max_trust_tier, generation_params,"
            " status, started_at) VALUES (%s, %s, 1, 'reply', 'b', 'p', 'a', 'r', 'c', 'e',"
            " %s::jsonb, %s, 1, 'T3', '{}', 'started', %s)",
            (invocation_id, turn_id, json.dumps(manifest), REDACTED_HASH, now),
        )
        cur.execute(
            "UPDATE model_invocation SET status = 'completed', rendered_prompt_hash = %s"
            " WHERE id = %s",
            (REDACTED_HASH + "-prompt", invocation_id),
        )
    return invocation_id
