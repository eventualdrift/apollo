"""The durable failure note states the real failure class (review P2-NEW-2).

The transcript note is the one record of a failed turn that Janu actually
reads. Saying "Apollo could not reach its model" when Core failed before ever
calling the model is a lie in that record — and it would have been the note
shown for every turn of the fence-shaped-history defect, where the model was
perfectly reachable.

The notes are fixed constants keyed on the closed `ErrorKind` enum. Nothing
derived from an exception, a provider response or the conversation reaches the
transcript.
"""

from __future__ import annotations

import pytest

from apollo.brains.base import RenderContractError
from apollo.brains.fake import FakeBrain
from apollo.core.conversations import create_conversation, transcript
from apollo.core.turns import SYSTEM_NOTE_INTERNAL, SYSTEM_NOTES, system_note_for
from apollo.errors import BrainTransportError, EmptyGenerationError, ErrorKind

pytestmark = pytest.mark.integration

SENTINEL = "SENTINEL-PRIVATE-door-code-4123"


def _notes(db, conversation_id) -> list[str]:
    return [r["content"] for r in transcript(db, conversation_id) if r["role"] == "system_note"]


class LeakyTransportError(BrainTransportError):
    def __init__(self) -> None:
        super().__init__(http_status=503, provider_error_code="unavailable")
        self.args = (f"POST /v1/chat failed: {SENTINEL}",)


def test_a_transport_failure_says_the_model_was_unreachable(db, service_factory, clock) -> None:
    brain = FakeBrain(fail_times=99, failure=LeakyTransportError())
    conversation_id = create_conversation(db, now=clock())
    result = service_factory(brain=brain).submit(conversation_id=conversation_id, text="hello")

    assert result.error_kind == ErrorKind.BRAIN_UNAVAILABLE
    notes = _notes(db, conversation_id)
    assert notes == [SYSTEM_NOTES[ErrorKind.BRAIN_UNAVAILABLE]]
    assert "could not reach its model" in notes[0]


def test_an_internal_failure_does_not_claim_the_model_was_unreachable(
    db, service_factory, clock
) -> None:
    """A render-contract violation: the model was never called at all."""

    class BrokenAdapter(FakeBrain):
        def render(self, bundle):  # type: ignore[no-untyped-def]
            raise RenderContractError("simulated render-contract violation")

    brain = BrokenAdapter()
    conversation_id = create_conversation(db, now=clock())
    result = service_factory(brain=brain).submit(conversation_id=conversation_id, text="hello")

    assert result.status == "failed"
    assert brain.calls == [], "the model was never reached, so the note must not say it was"
    notes = _notes(db, conversation_id)
    assert notes == [SYSTEM_NOTE_INTERNAL]
    assert "could not reach its model" not in notes[0]
    assert "internal processing error" in notes[0]


def test_an_empty_generation_says_so(db, service_factory, clock) -> None:
    brain = FakeBrain(fail_times=99, failure=EmptyGenerationError("nothing"))
    conversation_id = create_conversation(db, now=clock())
    result = service_factory(brain=brain).submit(conversation_id=conversation_id, text="hello")

    assert result.error_kind == ErrorKind.EMPTY_GENERATION
    notes = _notes(db, conversation_id)
    assert notes == [SYSTEM_NOTES[ErrorKind.EMPTY_GENERATION]]
    assert "could not reach its model" not in notes[0]


def test_no_note_carries_exception_provider_or_private_content(
    db, service_factory, clock
) -> None:
    conversation_id = create_conversation(db, now=clock())
    service_factory(brain=FakeBrain(fail_times=99, failure=LeakyTransportError())).submit(
        conversation_id=conversation_id, text=SENTINEL
    )

    class BrokenAdapter(FakeBrain):
        def render(self, bundle):  # type: ignore[no-untyped-def]
            raise RenderContractError(f"violation involving {SENTINEL}")

    service_factory(brain=BrokenAdapter()).submit(
        conversation_id=conversation_id, text=SENTINEL
    )

    blob = " ".join(_notes(db, conversation_id))
    assert SENTINEL not in blob
    assert "POST /v1/chat" not in blob
    assert "RenderContractError" not in blob
    assert "Traceback" not in blob
    # Every note is one of the fixed constants and nothing else.
    for note in _notes(db, conversation_id):
        assert note in set(SYSTEM_NOTES.values()) | {SYSTEM_NOTE_INTERNAL}


def test_a_policy_refusal_no_longer_writes_a_bare_enum_name(db, service_factory, clock) -> None:
    """It used to write `brain_mode_not_permitted` into the transcript verbatim."""
    from tests.integration.conftest import make_config

    config = make_config(db._dsn, allowed_modes=("benchmark",))
    conversation_id = create_conversation(db, now=clock())
    result = service_factory(config=config).submit(
        conversation_id=conversation_id, text="hello"
    )

    assert result.error_kind == ErrorKind.BRAIN_MODE_NOT_PERMITTED
    notes = _notes(db, conversation_id)
    assert notes == [SYSTEM_NOTES[ErrorKind.BRAIN_MODE_NOT_PERMITTED]]
    assert notes[0] != "brain_mode_not_permitted"
    assert notes[0].endswith("No response was generated for that message.")


def test_failure_notes_stay_out_of_later_model_context(db, service_factory, clock) -> None:
    """A note is transcript, never model context (spec F.2) — unchanged by this fix."""
    conversation_id = create_conversation(db, now=clock())
    service_factory(brain=FakeBrain(fail_times=99, failure=LeakyTransportError())).submit(
        conversation_id=conversation_id, text="are you there?"
    )
    notes = _notes(db, conversation_id)
    assert len(notes) == 1

    result = service_factory(brain=FakeBrain()).submit(
        conversation_id=conversation_id, text="back?"
    )
    assert result.status == "completed"

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT context_manifest FROM model_invocation WHERE turn_id = %s",
            (result.turn_id,),
        )
        manifest = cur.fetchone()["context_manifest"]
        cur.execute(
            "SELECT id FROM message WHERE conversation_id = %s AND role = 'system_note'",
            (conversation_id,),
        )
        note_ids = {str(r["id"]) for r in cur.fetchall()}
    assert not (note_ids & {e["source_ref"] for e in manifest})


def test_every_error_kind_has_a_truthful_note() -> None:
    """Total mapping: no failure category falls through to a misleading default."""
    for kind in ErrorKind:
        note = system_note_for(kind)
        assert note.endswith("No response was generated for that message.")
        if kind is not ErrorKind.BRAIN_UNAVAILABLE:
            assert "could not reach its model" not in note, kind
    assert system_note_for(ErrorKind.INTERNAL) == SYSTEM_NOTE_INTERNAL
