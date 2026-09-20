"""The ADR-0013 invariant, and the no-transaction-during-a-model-call rule."""

from __future__ import annotations

import uuid

import pytest

from apollo.brains.base import GenerationParams
from apollo.brains.fake import FakeBrain
from apollo.context.budget import Budget
from apollo.context.bundle import Purpose
from apollo.context.compiler import CompileRequest, compile_context
from apollo.context.estimator import CONSERVATIVE
from apollo.core import invocations
from apollo.core.conversations import create_conversation
from apollo.core.identity import compose_identity
from apollo.errors import InvocationContractError
from apollo.storage.db import open_transaction_depth
from tests.integration.conftest import REPO

pytestmark = pytest.mark.integration


def a_bundle():
    return compile_context(
        CompileRequest(
            purpose=Purpose.REPLY,
            user_message="hello",
            user_message_ref="msg-1",
            now=None,
            budget=Budget(total=8000, identity_cap=4000),
            estimator=CONSERVATIVE,
            identity=compose_identity(REPO / "identity"),
        )
    )


def test_calling_a_brain_without_a_started_row_is_refused(db, clock) -> None:
    """Acceptance criterion 12: no hidden model call."""
    brain = FakeBrain()
    conv = create_conversation(db, now=clock())
    with pytest.raises(InvocationContractError, match="without a committed 'started'"):
        invocations.invoke(
            db,
            invocation_id=uuid.uuid4(),  # names no committed row
            turn_id=uuid.uuid4(),
            conversation_id=conv,
            brain=brain,
            bundle=a_bundle(),
            params=GenerationParams(),
            now_factory=clock,
        )
    assert brain.calls == []  # the adapter was never entered


def test_a_completed_invocation_cannot_be_called_again(db, service, clock) -> None:
    conv = create_conversation(db, now=clock())
    result = service.submit(conversation_id=conv, text="hello")
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM model_invocation WHERE turn_id = %s", (result.turn_id,))
        invocation_id = cur.fetchone()["id"]

    brain = FakeBrain()
    with pytest.raises(InvocationContractError):
        invocations.invoke(
            db,
            invocation_id=invocation_id,  # exists, but is no longer 'started'
            turn_id=result.turn_id,
            conversation_id=conv,
            brain=brain,
            bundle=a_bundle(),
            params=GenerationParams(),
            now_factory=clock,
        )
    assert brain.calls == []


def test_no_database_transaction_is_open_while_the_adapter_runs(db, service_factory, clock) -> None:
    """Acceptance criterion 21, checked from inside the adapter itself."""
    observed: list[int] = []

    class WatchfulBrain(FakeBrain):
        def generate(self, req, params):  # type: ignore[no-untyped-def]
            observed.append(open_transaction_depth())
            return super().generate(req, params)

        def render(self, bundle):  # type: ignore[no-untyped-def]
            observed.append(open_transaction_depth())
            return super().render(bundle)

    conv = create_conversation(db, now=clock())
    result = service_factory(brain=WatchfulBrain()).submit(conversation_id=conv, text="hello")

    assert result.status == "completed"
    assert observed, "the adapter was never entered"
    assert observed == [0] * len(observed), f"a transaction was open during the call: {observed}"


def test_every_provider_attempt_has_exactly_one_row(db, service_factory, clock) -> None:
    from apollo.errors import BrainTransportError

    brain = FakeBrain(fail_times=1, failure=BrainTransportError(http_status=502))
    conv = create_conversation(db, now=clock())
    result = service_factory(brain=brain).submit(conversation_id=conv, text="hello")

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM model_invocation WHERE turn_id = %s", (result.turn_id,)
        )
        recorded = cur.fetchone()["n"]
    assert recorded == len(brain.calls) == 2
