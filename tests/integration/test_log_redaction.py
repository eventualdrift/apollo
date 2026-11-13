"""O.1/25 and O.1/22, across everything Apollo now does.

"A turn with sentinel strings in identity, a memory and a message produces log
output containing none of them." `test_privacy.py` showed that for the turn as
it was in milestone 1. Since then memory, retrieval, proposals, tombstones,
replay and new failure paths have all been added, and each of them logs.

This drives one session through every one of those paths, the failing ones
included, with a different sentinel in each place text can come from: the
identity, a message, a memory's subject and content, the model's reply, the
model's wording of a proposal, a provider's error body, and the text of a
database error. Then it reads the whole log, and every persisted error field
and audit payload, for any of them.

The log is captured at DEBUG, through the same handler `apollo` installs.
"""

from __future__ import annotations

import dataclasses
import io
import json
import logging
import pathlib
from typing import Any

import psycopg
import pytest

from apollo.brains.base import Generation, GenerationParams, RenderedRequest
from apollo.brains.fake import FakeBrain
from apollo.core import memories, replay
from apollo.core import proposals as proposal_surface
from apollo.core import retrieval as core_retrieval
from apollo.core.conversations import create_conversation
from apollo.errors import BrainTransportError
from apollo.logging_setup import configure_logging
from apollo.memory import proposals as structuring
from apollo.storage.db import Database

from .conftest import make_config
from .test_memory_in_replies import _rows

pytestmark = pytest.mark.integration

IDENTITY = "ZQX-IDENTITY-7001"
MESSAGE = "ZQX-MESSAGE-7002"
MEMORY_SUBJECT = "ZQX-SUBJECT-7003"
MEMORY_CONTENT = "ZQX-MEMORY-7004"
MODEL_REPLY = "ZQX-REPLY-7005"
PROPOSAL_WORDING = "ZQX-PROPOSAL-7006"
PROVIDER_BODY = "ZQX-PROVIDER-7007"
DATABASE_ERROR = "ZQX-DBERROR-7008"
EDITED = "ZQX-EDITED-7009"
SENTINELS = (IDENTITY, MESSAGE, MEMORY_SUBJECT, MEMORY_CONTENT, MODEL_REPLY, PROPOSAL_WORDING,
             PROVIDER_BODY, DATABASE_ERROR, EDITED)


class Model(FakeBrain):
    """Replies with a sentinel, and words a proposal with another."""

    def __init__(self, structured: str | None = None) -> None:
        super().__init__(key="fake")
        self.structured = structured or (
            f"scope: user\nkind: fact\nsubject: {PROPOSAL_WORDING} topic\n"
            f"content: The door code is {PROPOSAL_WORDING}.")

    def generate(self, req: RenderedRequest, params: GenerationParams) -> Generation:
        structuring_call = "You are structuring one thing" in req.messages[0].content
        return Generation(
            text=self.structured if structuring_call else f"The answer is {MODEL_REPLY}.",
            finish_reason="stop", model_identifier="fake/model", latency_ms=1,
            rendered_prompt_hash=req.prompt_hash,
            raw_meta={"echo": f"{MESSAGE} {MODEL_REPLY}", "finish_reason": "stop"},
        )


class LeakyProviderError(BrainTransportError):
    """A provider failure whose message and response body both carry a secret (O.1/22)."""

    def __init__(self) -> None:
        super().__init__(http_status=503, provider_error_code="overloaded",
                         provider_error_type="server_error")
        self.args = (f"POST /v1/chat?key={PROVIDER_BODY} failed: body={PROVIDER_BODY} "
                     f"prompt={MESSAGE}",)


def _identity(tmp_path: pathlib.Path) -> pathlib.Path:
    directory = tmp_path / "identity"
    directory.mkdir()
    (directory / "manifest.yaml").write_text(
        'schema_version: 1\nidentity_version: "test-1"\nfragments:\n  - core.md\n')
    (directory / "core.md").write_text(f"Apollo is a system. {IDENTITY}\n")
    return directory


def _database_error(*args: Any, **kwargs: Any) -> None:
    raise psycopg.errors.CheckViolation(
        f'new row violates check constraint\nDETAIL:  Failing row contains ({DATABASE_ERROR}, '
        f"{MEMORY_CONTENT}, {MESSAGE}).")


def test_no_sentinel_reaches_a_log_line_an_error_field_or_an_audit_payload(
    db: Database, owner_db: Database, service_factory, clock, tmp_path, monkeypatch
) -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    config = dataclasses.replace(make_config(db._dsn), identity_dir=_identity(tmp_path))
    conversation = create_conversation(db, now=clock())

    def say(text: str, brain: FakeBrain | None = None, **options: Any):  # noqa: ANN202
        return service_factory(brain=brain or Model(), config=config).submit(
            conversation_id=conversation, text=text, **options)

    try:
        # A memory, by direct entry, and a turn that retrieves it.
        memory_id = memories.create_memory(
            db, scope="user", kind="fact", subject=f"door {MEMORY_SUBJECT}",
            content=f"The door code is {MEMORY_CONTENT}.", now=clock())
        answered = say(f"What is the door code? {MESSAGE}")
        assert answered.status == "completed" and MODEL_REPLY in answered.response
        assert _rows(db, "SELECT 1 FROM turn_retrieval_result") != []

        # A proposal: made, edited and saved; another ignored; another expired.
        proposed = say(f"Remember that the door code is {MESSAGE}.")
        assert proposed.proposal is not None and PROPOSAL_WORDING in proposed.proposal.content
        saved = proposal_surface.save_proposal(
            db, proposed.proposal.id, now=clock(), content=f"The code is {EDITED}.")
        ignored = say(f"Remember that it is also {MESSAGE}.")
        proposal_surface.ignore_proposal(db, ignored.proposal.id, now=clock())

        # A proposal the model words badly, and one the database refuses.
        assert say(f"Remember that {MESSAGE}.",
                   Model(f"Sure! I noted {PROPOSAL_WORDING}.")).capture_failed
        with monkeypatch.context() as patched:
            patched.setattr(structuring, "propose", _database_error)
            assert say(f"Remember that {MESSAGE} once more.").capture_failed

        # The model's server fails, with a secret in its error (O.1/22).
        failed = say(f"Hello? {MESSAGE}", FakeBrain(fail_times=9, failure=LeakyProviderError()))
        assert failed.status == "failed"
        # The model returns nothing.

        class Empty(Model):
            def generate(self, req: RenderedRequest, params: GenerationParams) -> Generation:
                return dataclasses.replace(super().generate(req, params), text="  ")

        assert say(f"And again? {MESSAGE}", Empty()).error_kind == "empty_generation"

        # The search fails with a database error that quotes a row.
        with monkeypatch.context() as patched:
            patched.setattr(core_retrieval.retrieval, "retrieve", _database_error)
            assert say(f"What is the code? {MESSAGE}").status == "completed"

        # A message too large for the context.
        small = dataclasses.replace(config, providers={
            "fake": dataclasses.replace(config.providers["fake"], context_budget=1200)})
        overflow = service_factory(brain=Model(), config=small).submit(
            conversation_id=create_conversation(db, now=clock()),
            text=f"{MESSAGE} " * 400)
        assert overflow.error_kind == "context_overflow"

        # Replay, of a good turn and of the failed ones.
        for turn in (answered, proposed, failed, overflow):
            replay.replay_turn(db, turn.turn_id)

        # Forgetting: the direct memory and the one the proposal made.
        memories.tombstone_memory(db, memory_id, now=clock())
        memories.tombstone_memory(db, saved.memory_id, now=clock())
        assert replay.replay_turn(db, answered.turn_id)[0].bundle_status.value == (
            "SOURCE_REDACTED")
        assert proposal_surface.expire_proposals(
            db, now=clock.now.replace(year=clock.now.year + 1)) == 0
    finally:
        logging.getLogger().handlers.clear()

    logs = stream.getvalue()
    events = {json.loads(line)["event"] for line in logs.splitlines()}
    # The paths above really did log: this is not a search of an empty file.
    assert {"context.compiled", "context.overflow", "invocation.finished", "retrieval.failed",
            "invocation.retrying"} <= events
    for sentinel in SENTINELS:
        assert sentinel not in logs, f"{sentinel} reached the operational log"
    assert "<<<" not in logs and "Failing row" not in logs and "POST /v1" not in logs
    # Exceptions are logged by type and frames only.
    assert '"exception_type": "CheckViolation"' in logs

    # O.1/22: nothing persisted holds a provider's or a database's words either.
    fields = json.dumps(
        _rows(owner_db, "SELECT error_kind, error_detail FROM model_invocation")
        + _rows(owner_db, "SELECT error_kind, error_detail FROM turn")
        + _rows(owner_db, "SELECT payload FROM audit_event")
        + _rows(owner_db, "SELECT generation_params, context_manifest FROM model_invocation"),
        default=str,
    )
    for sentinel in SENTINELS:
        assert sentinel not in fields, f"{sentinel} reached a persisted error or audit field"
    assert "echo" not in fields  # raw_meta is never persisted
    details = {row["error_detail"] for row in _rows(
        owner_db, "SELECT error_detail FROM model_invocation WHERE error_detail IS NOT NULL")}
    assert details == {
        "http_status=503 provider_error_code=overloaded provider_error_type=server_error",
        "exception_type=ProposalStructureError",
        "exception_type=CheckViolation",
        "exception_type=EmptyGenerationError",
        "exception_type=UnsendableContextError",
    }
