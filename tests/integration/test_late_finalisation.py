"""A late result never overwrites orphan recovery (review fix 4 of 4).

Orphan recovery (spec A.2) marks a turn and its invocations failed with
`error_kind = interrupted` once they outlive the orphan window. It runs at the
start of every `apollo say`, so it can reach a turn whose model call is still
running in another process. Finalisation used to update `WHERE status =
'started'` without checking that a row changed. A late result then matched
nothing, and the rest of the transaction went ahead anyway: an Apollo reply,
or a second failure note, was appended to a turn the rows called interrupted,
with audit events claiming an outcome no row recorded.

Now every finalisation requires exactly one `started` row. Zero rows raise
`FinalisationConflictError`, which rolls back the whole transaction, and the
caller is told what the rows say.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from apollo.brains.base import GenerationParams, RenderedRequest
from apollo.brains.fake import FakeBrain
from apollo.core.conversations import create_conversation, transcript
from apollo.core.turns import SYSTEM_NOTES
from apollo.errors import (
    BrainServiceError,
    BrainTransportError,
    ErrorKind,
    FinalisationConflictError,
)
from apollo.storage.db import Database
from apollo.storage.repositories import InvocationRepository, TurnRepository
from apollo.storage.unit_of_work import unit_of_work

pytestmark = pytest.mark.integration

#: Past the orphan window in tests/integration/conftest.py (900 s).
PAST_THE_WINDOW = timedelta(seconds=901)


class OverrunBrain(FakeBrain):
    """A call that outlives the orphan window: recovery runs while it is out."""

    def __init__(self, recover, *, failure: Exception | None = None) -> None:  # noqa: ANN001
        super().__init__(key="fake")
        self._recover = recover
        self._late_failure = failure
        self.recovered: dict[str, int] | None = None

    def generate(self, req: RenderedRequest, params: GenerationParams):  # type: ignore[no-untyped-def]
        self.recovered = self._recover()
        if self._late_failure is not None:
            raise self._late_failure
        return super().generate(req, params)


def _rows(db: Database, sql: str, params=()) -> list[dict]:  # noqa: ANN001
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _overrun(db, service_factory, clock, *, failure: Exception | None = None):  # noqa: ANN001, ANN202
    conversation_id = create_conversation(db, now=clock())
    elsewhere = service_factory()  # another `apollo say`, starting up

    def recover() -> dict[str, int]:
        clock.now += PAST_THE_WINDOW
        return elsewhere.recover_orphans()

    brain = OverrunBrain(recover, failure=failure)
    result = service_factory(brain=brain).submit(
        conversation_id=conversation_id, text="A question that takes too long"
    )
    assert brain.recovered == {"turns": 1, "invocations": 1}
    return conversation_id, result


def _assert_left_as_recovery_recorded(db, conversation_id, result) -> None:  # noqa: ANN001
    assert result.status == "failed"
    assert result.error_kind == "interrupted"
    assert result.response is None and result.response_message_id is None

    [turn] = _rows(db, "SELECT * FROM turn WHERE id = %s", (result.turn_id,))
    assert (turn["status"], turn["error_kind"]) == ("failed", "interrupted")
    assert turn["response_message_id"] is None

    [invocation] = _rows(db, "SELECT * FROM model_invocation WHERE turn_id = %s",
                         (result.turn_id,))
    assert (invocation["status"], invocation["error_kind"]) == ("failed", "interrupted")
    assert invocation["rendered_prompt_hash"] is None
    assert invocation["model_identifier"] is None

    # Janu's message and recovery's interrupted note: no reply, no second note.
    messages = transcript(db, conversation_id)
    assert [m["role"] for m in messages] == ["user", "system_note"]
    assert messages[1]["content"] == SYSTEM_NOTES[ErrorKind.INTERRUPTED]

    # Nothing in the audit log claims an outcome the rows do not show.
    events = [r["event_type"] for r in _rows(
        db, "SELECT event_type FROM audit_event WHERE turn_id = %s ORDER BY id",
        (result.turn_id,),
    )]
    # Janu's message, the turn, the call, and recovery's note: nothing else.
    assert events == ["message.created", "turn.started", "invocation.started",
                      "message.created"]


def test_a_late_completion_does_not_overwrite_orphan_recovery(
    db, service_factory, clock
) -> None:
    conversation_id, result = _overrun(db, service_factory, clock)
    _assert_left_as_recovery_recorded(db, conversation_id, result)


def test_a_late_failure_does_not_overwrite_orphan_recovery(db, service_factory, clock) -> None:
    conversation_id, result = _overrun(
        db, service_factory, clock, failure=BrainServiceError(http_status=500)
    )
    _assert_left_as_recovery_recorded(db, conversation_id, result)


def test_a_late_transport_failure_is_not_retried_into_an_interrupted_turn(
    db, service_factory, clock
) -> None:
    """A retry would open a second invocation under a turn already finalised."""
    conversation_id, result = _overrun(
        db, service_factory, clock, failure=BrainTransportError(http_status=503)
    )
    _assert_left_as_recovery_recorded(db, conversation_id, result)
    assert len(result.invocation_ids) == 1


def test_recovery_still_records_its_own_audit_event(db, service_factory, clock) -> None:
    _overrun(db, service_factory, clock)
    [event] = _rows(
        db,
        "SELECT payload FROM audit_event WHERE event_type = 'turn.failed'"
        " AND payload->>'reason' = 'orphan_recovery'",
    )
    assert event["payload"]["count"] == 1


# -- the repositories ----------------------------------------------------------


def test_finalising_a_turn_twice_raises_and_changes_nothing(db, service, clock) -> None:
    result = service.submit(conversation_id=create_conversation(db, now=clock()), text="hi")
    assert result.status == "completed"
    [before] = _rows(db, "SELECT * FROM turn WHERE id = %s", (result.turn_id,))

    with pytest.raises(FinalisationConflictError) as caught, \
            unit_of_work(db, expect_audit=False) as uow:
        TurnRepository(uow).fail(turn_id=result.turn_id, now=clock(),
                                 error_kind="interrupted", error_detail=None)
    assert caught.value.table == "turn" and caught.value.row_id == result.turn_id
    with pytest.raises(FinalisationConflictError), unit_of_work(db, expect_audit=False) as uow:
        TurnRepository(uow).complete(turn_id=result.turn_id,
                                     response_message_id=before["response_message_id"],
                                     now=clock(), latency_ms=1)
    assert _rows(db, "SELECT * FROM turn WHERE id = %s", (result.turn_id,)) == [before]


def test_finalising_an_invocation_twice_raises_and_changes_nothing(db, service, clock) -> None:
    result = service.submit(conversation_id=create_conversation(db, now=clock()), text="hi")
    [invocation_id] = result.invocation_ids
    [before] = _rows(db, "SELECT * FROM model_invocation WHERE id = %s", (invocation_id,))

    with pytest.raises(FinalisationConflictError) as caught, \
            unit_of_work(db, expect_audit=False) as uow:
        InvocationRepository(uow).fail(invocation_id=invocation_id, now=clock(),
                                       latency_ms=1, error_kind="interrupted",
                                       error_detail=None)
    assert caught.value.table == "model_invocation"
    with pytest.raises(FinalisationConflictError), unit_of_work(db, expect_audit=False) as uow:
        InvocationRepository(uow).complete(
            invocation_id=invocation_id, now=clock(), latency_ms=1,
            model_identifier="late", rendered_prompt_hash=None, finish_reason="stop",
            prompt_tokens=None, completion_tokens=None, reasoning_tokens=None,
        )
    assert _rows(db, "SELECT * FROM model_invocation WHERE id = %s",
                 (invocation_id,)) == [before]
