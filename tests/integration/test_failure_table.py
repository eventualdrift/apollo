"""Spec L, row by row: what Apollo does when something fails.

One section per row of the failure table, in the table's order. Several rows
have fuller tests elsewhere (named in each section); this file states each
row's behaviour once, end to end, against real PostgreSQL as the runtime
role, so the table can be checked against one place.

A failure here is always the same shape of thing: a recorded fact, an honest
note in the transcript, no fabricated reply, and no quiet substitute.
"""

from __future__ import annotations

import io
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from apollo.brains.base import Generation, GenerationParams, RenderedRequest
from apollo.brains.fake import FakeBrain
from apollo.cli.main import TRUNCATED_MARK, TRUNCATED_NOTICE, main
from apollo.config import ProviderConfig
from apollo.context.compiler import RETRIEVAL_ERROR_NOTICE
from apollo.core import replay
from apollo.core import retrieval as core_retrieval
from apollo.core.conversations import create_conversation, transcript
from apollo.core.replay import BundleStatus, RenderStatus
from apollo.core.turns import SYSTEM_NOTE_CAPTURE_FAILED, SYSTEM_NOTES, TRANSPORT_NOTES
from apollo.errors import BrainTransportError, ErrorKind, TransportReason
from apollo.storage.db import Database

from .conftest import make_config
from .test_memory_in_replies import _remember, _rows
from .test_memory_proposals import ProposingBrain

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


class Scripted(FakeBrain):
    """brain.fake that answers with the given text and finish reason, keeping its requests."""

    def __init__(self, text: str = "A fixed reply.", *, finish_reason: str = "stop") -> None:
        super().__init__(key="fake")
        self.text, self.finish_reason = text, finish_reason
        self.requests: list[RenderedRequest] = []

    def generate(self, req: RenderedRequest, params: GenerationParams) -> Generation:
        self.requests.append(req)
        return Generation(text=self.text, finish_reason=self.finish_reason,
                          model_identifier="fake/scripted", latency_ms=1,
                          rendered_prompt_hash=req.prompt_hash)


def _turn(db: Database, turn_id: uuid.UUID) -> dict[str, Any]:
    [row] = _rows(db, "SELECT * FROM turn WHERE id = %s", turn_id)
    return row


def _invocations(db: Database, turn_id: uuid.UUID) -> list[dict[str, Any]]:
    return _rows(db, "SELECT * FROM model_invocation WHERE turn_id = %s ORDER BY seq", turn_id)


def _said(db: Database, conversation: uuid.UUID) -> list[tuple[str, str]]:
    return [(m["role"], m["content"]) for m in transcript(db, conversation)]


def _events(db: Database, turn_id: uuid.UUID) -> list[str]:
    return [e["event_type"] for e in _rows(
        db, "SELECT event_type FROM audit_event WHERE turn_id = %s ORDER BY id", turn_id)]


# -- Model unavailable / timeout -----------------------------------------------------------
# Fuller: test_turns.py, test_openai_compatible_turns.py, test_model_backend_killed.py.


def test_model_unavailable_one_retry_then_an_accurate_failure_and_no_fallback(
    db: Database, service_factory, clock
) -> None:
    down = FakeBrain(fail_times=99, failure=BrainTransportError(
        provider_error_type="connection_failed"))
    # A second provider is configured and reachable. It must never be tried.
    config = make_config(db._dsn)
    config.providers["other"] = ProviderConfig(
        key="other", kind="fake", allowed_modes=("personal", "benchmark"), eval_only=False,
        context_budget=8000, reserved_output=1024, options={})
    config.brain_aliases["other"] = "other"
    service = service_factory(brain=down, config=config)
    other = FakeBrain(key="other")
    service._registry._cache["brain.other"] = other

    conversation = create_conversation(db, now=clock())
    result = service.submit(conversation_id=conversation, text="Are you there?")

    assert (result.status, result.error_kind, result.response) == (
        "failed", "brain_unavailable", None)
    first, retry = _invocations(db, result.turn_id)
    assert [(i["status"], i["error_kind"], i["provider_key"]) for i in (first, retry)] == [
        ("failed", "brain_unavailable", "fake")] * 2
    assert retry["retry_of_invocation_id"] == first["id"]  # a new row, never a hidden call
    assert len(down.calls) == 2 and other.calls == []       # one retry; no other provider
    assert _events(db, result.turn_id).count("brain.unavailable") == 2
    # Finalised atomically: failed turn, a note, and nothing said in Apollo's name.
    assert _said(db, conversation) == [
        ("user", "Are you there?"),
        ("system_note", TRANSPORT_NOTES[TransportReason.CONNECTION]),
    ]


# -- Brain not permitted / eval-only -------------------------------------------------------
# Fuller: test_policy_modes.py.


def test_a_brain_not_permitted_is_refused_before_retrieval_or_generation(
    db: Database, service_factory, clock
) -> None:
    brain = Scripted()
    _remember(db, clock, "Janu's bike is blue.", subject="bike")
    service = service_factory(brain=brain, config=make_config(
        db._dsn, allowed_modes=("benchmark",)))
    conversation = create_conversation(db, now=clock())
    result = service.submit(conversation_id=conversation, text="What colour is my bike?")

    assert (result.status, result.error_kind) == ("failed", "brain_mode_not_permitted")
    assert brain.requests == [] and _invocations(db, result.turn_id) == []
    assert _rows(db, "SELECT 1 FROM turn_retrieval WHERE turn_id = %s", result.turn_id) == []
    assert "policy.refused" in _events(db, result.turn_id)
    assert [role for role, _ in _said(db, conversation)] == ["user", "system_note"]


# -- Database unavailable ------------------------------------------------------------------


class Unplugged(Database):
    """The real database, until `down` is set: then every connection is refused."""

    down = False

    def connect(self):  # type: ignore[no-untyped-def]
        if self.down:
            raise psycopg.OperationalError("connection refused: host=db.internal user=apollo_app")
        return super().connect()


def test_with_no_database_the_turn_did_not_happen(db: Database, service_factory, clock) -> None:
    """Hard fail: no degraded mode, no model call, nothing answered from memory."""
    conversation = create_conversation(db, now=clock())
    brain = Scripted()
    service = service_factory(brain=brain)
    service._db = Unplugged(db._dsn)
    service._db.down = True

    with pytest.raises(psycopg.OperationalError):
        service.submit(conversation_id=conversation, text="Are you there?")
    assert brain.requests == []
    assert _said(db, conversation) == [] and _rows(db, "SELECT 1 FROM turn") == []


def test_a_reply_that_could_not_be_recorded_is_not_delivered(
    db: Database, service_factory, clock
) -> None:
    """The database is lost while the model is answering. The answer goes nowhere."""
    conversation = create_conversation(db, now=clock())
    unplugged = Unplugged(db._dsn)

    class AnswersAsTheDatabaseGoes(Scripted):
        def generate(self, req: RenderedRequest, params: GenerationParams) -> Generation:
            unplugged.down = True
            return super().generate(req, params)

    service = service_factory(brain=AnswersAsTheDatabaseGoes("The answer."))
    service._db = unplugged
    with pytest.raises(psycopg.OperationalError):
        service.submit(conversation_id=conversation, text="Are you there?")

    # What T1 and Ta committed is all there is: a started turn, and no reply.
    unplugged.down = False
    assert _said(db, conversation) == [("user", "Are you there?")]
    [turn] = _rows(db, "SELECT status FROM turn")
    assert turn["status"] == "started"
    # On restart it is found and finished honestly (see "Crash mid-turn").
    clock.now += timedelta(hours=1)
    assert service_factory().recover_orphans()["turns"] == 1
    assert _said(db, conversation)[-1] == ("system_note", SYSTEM_NOTES[ErrorKind.INTERRUPTED])
    assert "The answer." not in repr(_said(db, conversation))


def test_the_terminal_says_the_database_could_not_be_used_and_no_more(
    db: Database, monkeypatch, capsys
) -> None:
    secret = "zqxdsnsecret"
    monkeypatch.setenv("APOLLO_CONFIG", str(REPO_ROOT / "apollo.toml"))
    monkeypatch.setenv("APOLLO_DATABASE_DSN",
                       f"host=127.0.0.1 port=1 user=apollo_app password={secret} dbname=x "
                       "connect_timeout=2")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    for argv in (["new"], ["say", str(uuid.uuid4()), "Hello."], ["show", str(uuid.uuid4())]):
        capsys.readouterr()
        assert main(argv) == 1
        out, err = capsys.readouterr()
        assert out == ""
        [line] = [text for text in err.splitlines() if not text.startswith("{")]
        assert line.startswith("apollo: OperationalError — the database could not be used.")
        assert secret not in err and "Traceback" not in err


# -- Retrieval failure ---------------------------------------------------------------------
# Fuller: test_memory_in_replies.py.


def test_a_failed_search_and_an_empty_search_are_told_apart(
    db: Database, service_factory, clock, monkeypatch
) -> None:
    """The turn continues either way; what the model is told is never the same."""
    brain = Scripted()
    conversation = create_conversation(db, now=clock())
    service = service_factory(brain=brain)
    empty = service.submit(conversation_id=conversation, text="What colour is my bike?")
    with monkeypatch.context() as patched:
        patched.setattr(core_retrieval.retrieval, "retrieve",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        failed = service.submit(conversation_id=conversation, text="And my car?")

    assert (empty.status, failed.status) == ("completed", "completed")
    nothing_found, search_failed = (r.messages[-1].content for r in brain.requests)
    assert "Substantive matches: 0" in nothing_found and "RETRIEVAL_ERROR" not in nothing_found
    assert "<<<RETRIEVAL_ERROR tier=T0" in search_failed
    assert "Substantive matches" not in search_failed
    assert "No stored memory bears on this" not in search_failed
    assert RETRIEVAL_ERROR_NOTICE.splitlines()[0][:40] in search_failed


# -- Empty generation ----------------------------------------------------------------------
# Fuller: test_empty_generation.py.


def test_an_empty_generation_fails_the_turn_and_persists_no_message(
    db: Database, service_factory, clock
) -> None:
    brain = Scripted("   \n")
    conversation = create_conversation(db, now=clock())
    result = service_factory(brain=brain).submit(conversation_id=conversation, text="Hello.")
    assert (result.status, result.error_kind) == ("failed", "empty_generation")
    [invocation] = _invocations(db, result.turn_id)
    assert (invocation["status"], invocation["error_kind"]) == ("failed", "empty_generation")
    assert _turn(db, result.turn_id)["response_message_id"] is None
    assert [role for role, _ in _said(db, conversation)] == ["user", "system_note"]


# -- Truncated generation ------------------------------------------------------------------


def test_a_truncated_generation_is_kept_and_marked_as_truncated(
    db: Database, service_factory, clock
) -> None:
    brain = Scripted("The answer begins, and then", finish_reason="length")
    conversation = create_conversation(db, now=clock())
    service = service_factory(brain=brain)
    result = service.submit(conversation_id=conversation, text="Explain.",
                            idempotency_key="k1")

    assert result.status == "completed" and result.truncated
    assert result.response == "The answer begins, and then"
    [message] = _rows(db, "SELECT truncated, content FROM message WHERE id = %s",
                      result.response_message_id)
    assert message["truncated"] is True
    assert _invocations(db, result.turn_id)[0]["finish_reason"] == "length"
    # Still marked when the client asks again.
    again = service.submit(conversation_id=conversation, text="Explain.", idempotency_key="k1")
    assert again.replayed and again.truncated and len(brain.requests) == 1

    whole = service_factory(brain=Scripted()).submit(conversation_id=conversation,
                                                     text="Thanks.")
    assert whole.status == "completed" and not whole.truncated


def test_the_terminal_shows_a_truncated_reply_as_cut_off(
    db: Database, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("APOLLO_CONFIG", str(REPO_ROOT / "apollo.toml"))
    monkeypatch.setenv("APOLLO_DATABASE_DSN", db._dsn)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    def cut_off(self: FakeBrain, req: RenderedRequest, params: GenerationParams) -> Generation:
        return Generation(text="It starts", finish_reason="length",
                          model_identifier="fake/echo", latency_ms=1,
                          rendered_prompt_hash=req.prompt_hash)

    assert main(["new"]) == 0
    conversation = capsys.readouterr().out.strip()
    with monkeypatch.context() as patched:
        patched.setattr(FakeBrain, "generate", cut_off)
        assert main(["say", conversation, "Explain."]) == 0
    out, err = capsys.readouterr()
    assert out == "apollo> It starts\n" and TRUNCATED_NOTICE in err

    assert main(["say", conversation, "Thanks."]) == 0
    assert TRUNCATED_NOTICE not in capsys.readouterr().err
    assert main(["show", conversation]) == 0
    shown = capsys.readouterr().out.splitlines()
    assert [TRUNCATED_MARK in line for line in shown] == [False, True, False, False]


# -- Content-level failure -----------------------------------------------------------------
# Fuller: test_turns.py, test_openai_compatible_turns.py.


@pytest.mark.parametrize("text", ["", "   "])
def test_a_content_level_failure_is_never_retried(
    db: Database, service_factory, clock, text: str
) -> None:
    """Retrying until the output looks acceptable would hide a real problem."""
    brain = Scripted(text)
    result = service_factory(brain=brain).submit(
        conversation_id=create_conversation(db, now=clock()), text="Hello.")
    assert result.status == "failed" and len(brain.requests) == 1
    assert len(_invocations(db, result.turn_id)) == 1


# -- Context overflow ----------------------------------------------------------------------


def _manifest(invocation: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(invocation["context_manifest"], key=lambda entry: entry["position"])


def test_a_request_too_large_for_the_context_fails_before_generation_with_its_manifest(
    db: Database, service_factory, clock
) -> None:
    """Raised before generation; the manifest is still persisted; identity is never dropped."""
    brain = Scripted()
    service = service_factory(brain=brain, config=make_config(db._dsn, context_budget=4300))
    conversation = create_conversation(db, now=clock())
    assert service.submit(conversation_id=conversation, text="Hello.").status == "completed"
    result = service.submit(conversation_id=conversation, text="word " * 600)

    assert (result.status, result.error_kind, result.response) == (
        "failed", "context_overflow", None)
    assert len(brain.requests) == 1  # only the first turn reached the model
    [unsent] = _invocations(db, result.turn_id)
    assert result.invocation_ids == (unsent["id"],)
    assert (unsent["status"], unsent["error_kind"], unsent["purpose"]) == (
        "failed", "context_overflow", "reply")
    assert unsent["rendered_prompt_hash"] is None and unsent["finish_reason"] is None

    # What did not fit is on record: the request alone is past what was left,
    # and the conversation that could not be carried with it is marked dropped.
    entries = _manifest(unsent)
    by_type = {e["block_type"]: e for e in entries}
    assert by_type["IDENTITY"]["included"] is True          # never dropped, never trimmed
    assert by_type["USER_MESSAGE"]["tokens"] == 1000
    assert unsent["context_token_estimate"] > 4300
    dropped = [e for e in entries if not e["included"]]
    assert [(e["block_type"], e["drop_reason"]) for e in dropped] == [
        ("CONVERSATION_RECENT", "budget:conversation")] * 2
    assert "word word" not in repr(unsent["context_manifest"])  # references, never content

    assert _events(db, result.turn_id)[-4:] == [
        "invocation.started", "invocation.failed", "message.created", "turn.failed"]
    assert _said(db, conversation)[-1] == (
        "system_note", SYSTEM_NOTES[ErrorKind.CONTEXT_OVERFLOW])
    # The search that ran is recorded too.
    assert len(_rows(db, "SELECT 1 FROM turn_retrieval WHERE turn_id = %s",
                     result.turn_id)) == 3

    # And it replays: what would have been sent can be rebuilt, though nothing was.
    [found] = replay.replay_turn(db, result.turn_id)
    assert (found.bundle_status, found.render_status) == (
        BundleStatus.VERIFIED, RenderStatus.NOT_ATTEMPTED)


def test_recent_conversation_that_no_longer_fits_fails_rather_than_degrading(
    db: Database, service_factory, clock
) -> None:
    """The floor: the last two exchanges must fit, or the turn fails and says what did not."""
    # Each message and each reply is 300 tokens; two exchanges are 1200.
    brain = Scripted("reply " * 150)
    service = service_factory(brain=brain, config=make_config(db._dsn, context_budget=5100))
    conversation = create_conversation(db, now=clock())
    for _ in range(2):
        assert service.submit(conversation_id=conversation,
                              text="said " * 180).status == "completed"
    result = service.submit(conversation_id=conversation, text="said " * 180)

    assert (result.status, result.error_kind) == ("failed", "context_overflow")
    assert len(brain.requests) == 2
    [unsent] = _invocations(db, result.turn_id)
    history = [e for e in _manifest(unsent) if e["block_type"] == "CONVERSATION_RECENT"]
    assert len(history) == 4
    assert [e["included"] for e in history].count(False) >= 1
    assert all(e["drop_reason"] == "budget:conversation" for e in history if not e["included"])
    # Oldest first: whatever was dropped is older than whatever was kept.
    included = [e["included"] for e in sorted(
        history, key=lambda e: (e["included"], e["position"]))]
    assert included == sorted(included)

    # The next, shorter message cannot fit that history either, and says so
    # again: Apollo never quietly answers from a conversation he cannot see.
    again = service.submit(conversation_id=conversation, text="Hello?")
    assert again.error_kind == "context_overflow"


def test_a_budget_with_no_room_at_all_fails_with_nothing_to_record(
    db: Database, service_factory, clock
) -> None:
    brain = Scripted()
    service = service_factory(brain=brain, config=make_config(
        db._dsn, context_budget=100, reserved_output=5000))
    brain._max_context = 1000  # less than the output it must reserve
    result = service.submit(conversation_id=create_conversation(db, now=clock()),
                            text="Hello.")
    assert (result.status, result.error_kind) == ("failed", "context_overflow")
    assert result.invocation_ids == () and brain.requests == []


# -- Proposal structuring fails ------------------------------------------------------------
# Fuller: test_memory_proposals.py.


def test_a_proposal_that_cannot_be_structured_never_fails_the_conversation(
    db: Database, service_factory, clock
) -> None:
    conversation = create_conversation(db, now=clock())
    result = service_factory(brain=ProposingBrain("Sure, noted!")).submit(
        conversation_id=conversation, text="Remember that I take my coffee black.")
    assert result.status == "completed" and result.capture_failed
    assert [(i["purpose"], i["status"]) for i in _invocations(db, result.turn_id)] == [
        ("reply", "completed"), ("memory_proposal", "failed")]
    assert _rows(db, "SELECT 1 FROM memory_proposal") == []
    assert _said(db, conversation)[-1] == ("system_note", SYSTEM_NOTE_CAPTURE_FAILED)


# -- Crash mid-turn ------------------------------------------------------------------------
# Fuller: test_turns.py (orphan recovery), test_late_finalisation.py, test_unit_of_work.py.


class ProcessKilled(BaseException):
    """Stands in for the process dying: nothing after the call gets to run."""


def _killed_mid_call(service_factory: Any, monkeypatch: Any, conversation: uuid.UUID) -> None:
    """A turn whose process dies while the adapter is running: rows are left as they were."""
    from apollo.core import turns as core_turns

    def dies(*args: Any, **kwargs: Any) -> None:
        raise ProcessKilled

    with monkeypatch.context() as patched:
        patched.setattr(core_turns.invocations, "invoke", dies)
        with pytest.raises(ProcessKilled):
            service_factory(brain=Scripted()).submit(conversation_id=conversation,
                                                     text="Hello.")


def test_a_crash_mid_call_leaves_a_started_invocation_that_restart_marks_interrupted(
    db: Database, service_factory, clock, monkeypatch
) -> None:
    conversation = create_conversation(db, now=clock())
    _killed_mid_call(service_factory, monkeypatch, conversation)
    # T1 guarantees message and turn exist together; Ta that the call is on record.
    [turn] = _rows(db, "SELECT id, status FROM turn")
    [invocation] = _invocations(db, turn["id"])
    assert (turn["status"], invocation["status"]) == ("started", "started")
    assert _said(db, conversation) == [("user", "Hello.")]

    # Within the window nothing is touched; beyond it, both are marked.
    restarted = service_factory()
    assert restarted.recover_orphans() == {"turns": 0, "invocations": 0}
    clock.now += timedelta(hours=1)
    assert restarted.recover_orphans() == {"turns": 1, "invocations": 1}
    assert (_turn(db, turn["id"])["status"], _turn(db, turn["id"])["error_kind"]) == (
        "failed", "interrupted")
    [invocation] = _invocations(db, turn["id"])
    assert (invocation["status"], invocation["error_kind"]) == ("failed", "interrupted")
    assert _said(db, conversation)[-1] == ("system_note", SYSTEM_NOTES[ErrorKind.INTERRUPTED])

    # And the conversation goes on.
    after = restarted.submit(conversation_id=conversation, text="Hello again.")
    assert after.status == "completed"


def test_stopping_a_call_at_the_keyboard_is_recorded_as_an_interruption(
    db: Database, service_factory, clock
) -> None:
    """Not a crash: the process lives, so the turn is finished at once, and truthfully."""

    class StoppedByCtrlC(Scripted):
        def generate(self, req: RenderedRequest, params: GenerationParams) -> Generation:
            raise KeyboardInterrupt

    conversation = create_conversation(db, now=clock())
    result = service_factory(brain=StoppedByCtrlC()).submit(
        conversation_id=conversation, text="Hello.")
    assert (result.status, result.error_kind) == ("failed", "interrupted")
    [invocation] = _invocations(db, result.turn_id)
    assert (invocation["status"], invocation["error_kind"], invocation["error_detail"]) == (
        "failed", "interrupted", "exception_type=KeyboardInterrupt")
    assert _said(db, conversation)[-1] == ("system_note", SYSTEM_NOTES[ErrorKind.INTERRUPTED])


# -- Duplicate submission ------------------------------------------------------------------
# Fuller: test_turns.py, test_continuity.py.


def test_a_duplicate_submission_returns_the_same_turn_and_generates_nothing(
    db: Database, service_factory, clock
) -> None:
    brain = Scripted()
    service = service_factory(brain=brain)
    conversation = create_conversation(db, now=clock())
    first = service.submit(conversation_id=conversation, text="Hello.", idempotency_key="k")
    again = service.submit(conversation_id=conversation, text="Hello.", idempotency_key="k")
    assert again.replayed and (again.turn_id, again.response, again.invocation_ids) == (
        first.turn_id, first.response, first.invocation_ids)
    assert len(brain.requests) == 1
    assert len(_rows(db, "SELECT 1 FROM model_invocation")) == 1
    assert len(_said(db, conversation)) == 2


# -- "none of them re-enters model context" ------------------------------------------------


def test_no_note_written_by_any_of_these_paths_reaches_a_later_context(
    db: Database, service_factory, clock, monkeypatch
) -> None:
    """Every `system_note` is durable and human-visible, and never model context (spec F.2)."""
    conversation = create_conversation(db, now=clock())

    def say(brain: FakeBrain, text: str = "Hello.", **config: Any):  # noqa: ANN202
        return service_factory(brain=brain, config=make_config(db._dsn, **config)).submit(
            conversation_id=conversation, text=text)

    say(FakeBrain(fail_times=9, failure=BrainTransportError(provider_error_type="timeout")))
    say(Scripted(""))                                              # empty generation
    say(ProposingBrain("no"), "Remember that I like tea.")         # capture failed
    say(Scripted(), allowed_modes=("benchmark",))                  # policy refusal
    _killed_mid_call(service_factory, monkeypatch, conversation)   # a crash, then recovery
    clock.now += timedelta(hours=1)
    service_factory().recover_orphans()

    notes = _rows(db, "SELECT id, content FROM message WHERE role = 'system_note'")
    assert len(notes) == 5 and len({n["content"] for n in notes}) == 5

    brain = Scripted()
    result = say(brain)
    assert result.status == "completed"
    sent = "\n".join(message.content for message in brain.requests[0].messages)
    for note in notes:
        assert note["content"] not in sent
    [invocation] = _invocations(db, result.turn_id)
    refs = {entry["source_ref"] for entry in invocation["context_manifest"]}
    assert not refs & {str(note["id"]) for note in notes}
    # What Janu said in those failed turns is still his conversation.
    assert "Remember that I like tea." in sent
