"""chat-v2 against chat-v1: the evidence spec J.5 asks of a render_version change.

J.5 requires the persona suite for a render_version change. chat-v2 differs
from chat-v1 only when history has consecutive same-role messages, which no
persona corpus case has. So every corpus case renders byte-identically under
both, and a persona-suite rerun would send the models exactly the bytes the
M2 runs sent. This test is that evidence; the replay half of J.5 is deferred
to step 13, which builds replay (docs/PROJECT_STATE.md).
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime

import pytest

from apollo.brains.base import (
    CHAT_RENDER_VERSION,
    CHAT_RENDER_VERSIONS,
    UnknownRenderVersionError,
    render_chat,
)
from apollo.config import MODE_BENCHMARK, ProviderConfig
from apollo.context.budget import Budget
from apollo.context.bundle import Purpose
from apollo.context.compiler import CompileRequest, HistoryMessage, compile_context
from apollo.context.estimator import CONSERVATIVE
from apollo.core.identity import compose_identity
from apollo.evals.loader import load_cases
from apollo.evals.runner import compile_case_bundle

REPO = pathlib.Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 28, tzinfo=UTC)


def test_the_default_is_chat_v2_and_chat_v1_stays_available() -> None:
    assert CHAT_RENDER_VERSION == "chat-v2"
    assert CHAT_RENDER_VERSIONS == ("chat-v1", "chat-v2")


@pytest.mark.parametrize("max_context", [9216, 16384])
def test_every_persona_case_renders_identically_under_chat_v1_and_chat_v2(
    max_context: int,
) -> None:
    identity = compose_identity(REPO / "identity")
    cases = load_cases(REPO / "evals/persona/cases")
    assert len(cases) == 30
    provider = ProviderConfig(key="p", kind="fake", allowed_modes=(MODE_BENCHMARK,),
                              eval_only=True, context_budget=8000, reserved_output=1024)
    for case in cases:
        bundle = compile_case_bundle(case, identity=identity, provider=provider,
                                     max_context=max_context, identity_cap=4000, now=NOW)
        v1 = render_chat(bundle, render_version="chat-v1")
        v2 = render_chat(bundle, render_version="chat-v2")
        assert v1.messages == v2.messages, case.id
        assert v1.prompt_hash == v2.prompt_hash, case.id


def _failed_turn_bundle():  # type: ignore[no-untyped-def]
    return compile_context(CompileRequest(
        purpose=Purpose.REPLY, user_message="Try again?", user_message_ref="m3", now=NOW,
        budget=Budget(total=8000, identity_cap=4000), estimator=CONSERVATIVE,
        identity=compose_identity(REPO / "identity"),
        history=(HistoryMessage("m1", "user", "question"),
                 HistoryMessage("m2", "apollo", "answer"),
                 HistoryMessage("m2b", "user", "unanswered: that turn failed")),
    ))


def test_the_versions_differ_exactly_where_chat_v2_coalesces() -> None:
    bundle = _failed_turn_bundle()
    v1 = render_chat(bundle, render_version="chat-v1")
    v2 = render_chat(bundle, render_version="chat-v2")
    assert [m.role for m in v1.messages] == ["system", "user", "assistant", "user", "user"]
    assert [m.role for m in v2.messages] == ["system", "user", "assistant", "user"]
    assert v1.prompt_hash != v2.prompt_hash


def test_an_adapter_renders_with_the_version_it_declares() -> None:
    """The recorded render_version is the one actually used, never just a label."""
    from apollo.brains.fake import FakeBrain

    class ChatV1Brain(FakeBrain):
        render_version = "chat-v1"

    bundle = _failed_turn_bundle()
    assert FakeBrain().render(bundle) == render_chat(bundle, render_version="chat-v2")
    assert ChatV1Brain().render(bundle) == render_chat(bundle, render_version="chat-v1")


def test_an_unknown_render_version_is_refused() -> None:
    with pytest.raises(UnknownRenderVersionError, match="chat-v0"):
        render_chat(_failed_turn_bundle(), render_version="chat-v0")
