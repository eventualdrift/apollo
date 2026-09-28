"""Roles alternate in every rendered request, even after a failed turn (spec G.3).

A failed turn leaves Janu's message in the transcript with no Apollo reply
after it. Rendered naively, the next request is system, user, user, and
some providers reject consecutive user messages. The renderer coalesces
consecutive same-role messages into one, keeping each message's words and
role, so the request alternates.
"""

from __future__ import annotations

import pytest

from apollo.brains.base import GenerationParams, RenderedRequest
from apollo.brains.fake import FakeBrain
from apollo.core.conversations import create_conversation
from apollo.errors import BrainTransportError

pytestmark = pytest.mark.integration


class RecordingBrain(FakeBrain):
    def __init__(self) -> None:
        super().__init__(key="fake")
        self.requests: list[RenderedRequest] = []

    def generate(self, req: RenderedRequest, params: GenerationParams):  # type: ignore[no-untyped-def]
        self.requests.append(req)
        return super().generate(req, params)


def _roles(req: RenderedRequest) -> list[str]:
    return [m.role for m in req.messages]


def _alternates(roles: list[str]) -> bool:
    turns = [r for r in roles if r != "system"]
    return turns[0] == "user" and all(a != b for a, b in zip(turns, turns[1:], strict=False))


def test_the_request_after_a_failed_turn_alternates(db, service_factory, clock) -> None:
    conversation_id = create_conversation(db, now=clock())
    service_factory().submit(conversation_id=conversation_id, text="What is Rust's borrow checker?")
    failing = FakeBrain(fail_times=99, failure=BrainTransportError(http_status=503))
    failed = service_factory(brain=failing).submit(
        conversation_id=conversation_id, text="And how do lifetimes relate?"
    )
    assert failed.status == "failed"

    brain = RecordingBrain()
    result = service_factory(brain=brain).submit(conversation_id=conversation_id, text="Try again?")
    assert result.status == "completed"
    [req] = brain.requests
    assert _roles(req) == ["system", "user", "assistant", "user"]
    assert _alternates(_roles(req))
    # The unanswered message is still there, as Janu's words, before the request.
    last = req.messages[-1].content
    assert "And how do lifetimes relate?" in last
    assert last.index("And how do lifetimes relate?") < last.index("Try again?")
    assert last.endswith("Try again?")


def test_two_failed_turns_in_the_middle_of_history_still_alternate(
    db, service_factory, clock
) -> None:
    conversation_id = create_conversation(db, now=clock())
    failing = FakeBrain(fail_times=99, failure=BrainTransportError(http_status=503))
    for text in ("first, unanswered", "second, unanswered"):
        assert service_factory(brain=failing).submit(
            conversation_id=conversation_id, text=text
        ).status == "failed"
    service_factory().submit(conversation_id=conversation_id, text="third, answered")

    brain = RecordingBrain()
    service_factory(brain=brain).submit(conversation_id=conversation_id, text="fourth")
    [req] = brain.requests
    assert _roles(req) == ["system", "user", "assistant", "user"]
    first_user = req.messages[1].content
    assert first_user.index("first, unanswered") < first_user.index("second, unanswered")
    assert first_user.index("second, unanswered") < first_user.index("third, answered")
