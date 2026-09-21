"""A provider that returns empty text must fail the turn (independent review P1).

The distinction that matters: this Brain **returns** a `Generation` whose text
is empty, rather than raising. That is what a real provider does, and it is
what exposed the bug — `InvocationOutcome.ok` tested only whether a generation
object existed, so the object's presence outvoted the error raised from its
empty text. The durable result was a turn marked `completed` alongside an
invocation marked `failed`, with a blank Apollo message persisted.

The existing coverage made `FakeBrain.generate` raise, leaving
`generation = None`, which never reaches the contradictory branch.
"""

from __future__ import annotations

import pytest

from apollo.brains.base import Generation
from apollo.brains.fake import FakeBrain
from apollo.core.conversations import create_conversation, transcript
from apollo.core.turns import SYSTEM_NOTES
from apollo.errors import ErrorKind

pytestmark = pytest.mark.integration

SENTINEL = "SENTINEL-PRIVATE-door-code-4123"


class ReturnsEmptyBrain(FakeBrain):
    """Answers successfully, with nothing. It does not raise."""

    def __init__(self, text: str = "", **kw) -> None:
        super().__init__(**kw)
        self._empty_text = text

    def generate(self, req, params):  # type: ignore[no-untyped-def]
        self.calls.append(req.prompt_hash)
        return Generation(
            text=self._empty_text,
            finish_reason="stop",
            model_identifier="fake/empty",
            latency_ms=1,
            rendered_prompt_hash=req.prompt_hash,
            prompt_tokens=10,
            completion_tokens=0,
            reasoning_tokens=None,
            raw_meta={"echo_of_prompt": SENTINEL},
        )


@pytest.mark.parametrize(
    "text", ["", "   ", "\n", "\t\n  \r\n"], ids=["empty", "spaces", "newline", "mixed"]
)
def test_a_returned_empty_generation_fails_the_turn(db, service_factory, clock, text) -> None:
    brain = ReturnsEmptyBrain(text=text)
    conversation_id = create_conversation(db, now=clock())
    result = service_factory(brain=brain).submit(
        conversation_id=conversation_id, text="say something"
    )

    assert result.status == "failed"
    assert result.error_kind == ErrorKind.EMPTY_GENERATION
    assert result.response is None
    assert result.response_message_id is None

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT status, error_kind FROM turn WHERE id = %s", (result.turn_id,))
        turn = cur.fetchone()
        cur.execute(
            "SELECT status, error_kind, retry_of_invocation_id FROM model_invocation"
            " WHERE turn_id = %s ORDER BY seq",
            (result.turn_id,),
        )
        invocations = cur.fetchall()
        cur.execute(
            "SELECT role, content FROM message WHERE conversation_id = %s ORDER BY seq",
            (conversation_id,),
        )
        messages = cur.fetchall()

    # The durable state, not the returned Python values.
    assert turn["status"] == "failed"
    assert turn["error_kind"] == ErrorKind.EMPTY_GENERATION

    assert len(invocations) == 1, "empty generation is content-level: it must not be retried"
    assert invocations[0]["status"] == "failed"
    assert invocations[0]["error_kind"] == ErrorKind.EMPTY_GENERATION
    assert invocations[0]["retry_of_invocation_id"] is None

    assert len(brain.calls) == 1, "exactly one provider call"

    apollo_messages = [m for m in messages if m["role"] == "apollo"]
    assert apollo_messages == [], "no Apollo response may be persisted for a failed turn"

    notes = [m for m in messages if m["role"] == "system_note"]
    assert len(notes) == 1
    assert notes[0]["content"] == SYSTEM_NOTES[ErrorKind.EMPTY_GENERATION]
    assert "returned nothing" in notes[0]["content"]
    assert "could not reach its model" not in notes[0]["content"]


def test_the_empty_generation_note_leaks_nothing(db, service_factory, clock) -> None:
    conversation_id = create_conversation(db, now=clock())
    service_factory(brain=ReturnsEmptyBrain()).submit(
        conversation_id=conversation_id, text=SENTINEL
    )
    notes = [m["content"] for m in transcript(db, conversation_id) if m["role"] == "system_note"]
    assert notes and SENTINEL not in " ".join(notes)

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT error_detail FROM model_invocation")
        details = repr(cur.fetchall())
        cur.execute("SELECT payload FROM audit_event")
        payloads = repr(cur.fetchall())
    assert SENTINEL not in details + payloads
    assert "echo_of_prompt" not in details + payloads


def test_the_conversation_survives_an_empty_generation(db, service_factory, clock) -> None:
    """A failed turn must not brick what follows — the note stays out of context."""
    conversation_id = create_conversation(db, now=clock())
    service_factory(brain=ReturnsEmptyBrain()).submit(
        conversation_id=conversation_id, text="say something"
    )
    recovered = service_factory(brain=FakeBrain()).submit(
        conversation_id=conversation_id, text="are you back?"
    )
    assert recovered.status == "completed"

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT context_manifest FROM model_invocation WHERE turn_id = %s",
            (recovered.turn_id,),
        )
        manifest = cur.fetchone()["context_manifest"]
        cur.execute(
            "SELECT id FROM message WHERE conversation_id = %s AND role = 'system_note'",
            (conversation_id,),
        )
        note_ids = {str(r["id"]) for r in cur.fetchall()}
    assert not (note_ids & {e["source_ref"] for e in manifest})
    # The failed turn contributed no dialogue at all.
    history = [e for e in manifest if e["block_type"] == "CONVERSATION_RECENT"]
    assert len(history) == 1, "only the earlier user message, no blank Apollo turn"


def test_a_non_empty_generation_still_succeeds(db, service_factory, clock) -> None:
    """The counterpart: the fix must not turn ordinary success into failure."""
    conversation_id = create_conversation(db, now=clock())
    result = service_factory(brain=ReturnsEmptyBrain(text="a real answer")).submit(
        conversation_id=conversation_id, text="say something"
    )
    assert result.status == "completed"
    assert result.response == "a real answer"
