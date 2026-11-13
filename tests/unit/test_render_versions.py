"""Render versions against each other: the evidence spec J.5 asks of a render_version change.

J.5 requires the persona suite for a render_version change.

- chat-v2 differs from chat-v1 only when history has consecutive same-role
  messages, which no persona corpus case has. So every corpus case renders
  byte-identically under both, and a persona-suite rerun would send the models
  exactly the bytes the M2 runs sent.
- chat-v3 differs from chat-v2 only when a block carries provenance
  attributes, which only a MEMORY block does. No persona corpus case has a
  memory, so every case renders byte-identically under both. (The compiler
  changed in the same step, and that does call for a persona run: see
  docs/PROJECT_STATE.md.)

Older versions are guarded directly. Invocations record the version they were
rendered with, and replay reproduces them by selecting that renderer, so its
bytes must never move. The pinned hashes below are what the renderer before
chat-v2 produced for failed-turn histories, the one case where those two
differ.
"""

from __future__ import annotations

import hashlib
import pathlib
from datetime import UTC, datetime

import pytest

from apollo.brains.base import (
    CHAT_RENDER_VERSION,
    CHAT_RENDER_VERSIONS,
    UnknownRenderVersionError,
    render_as,
    render_chat,
    render_version_for,
    split_render_version,
)
from apollo.config import MODE_BENCHMARK, ProviderConfig
from apollo.context.budget import Budget
from apollo.context.bundle import Purpose
from apollo.context.compiler import (
    CompileRequest,
    HistoryMessage,
    MemoryItem,
    RetrievalInput,
    compile_context,
)
from apollo.context.estimator import CONSERVATIVE
from apollo.context.rules import COMPILER_V1
from apollo.core.identity import Identity, compose_identity
from apollo.evals.loader import load_cases
from apollo.evals.runner import compile_case_bundle

REPO = pathlib.Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 28, tzinfo=UTC)


def test_the_default_is_chat_v3_and_the_older_versions_stay_available() -> None:
    assert CHAT_RENDER_VERSION == "chat-v3"
    assert CHAT_RENDER_VERSIONS == ("chat-v1", "chat-v2", "chat-v3")


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
        # As the M2 runs compiled them: that is what the chat-v2 evidence is about.
        bundle = compile_case_bundle(case, identity=identity, provider=provider,
                                     max_context=max_context, identity_cap=4000, now=NOW,
                                     compiler_version=COMPILER_V1)
        v1 = render_chat(bundle, render_version="chat-v1")
        v2 = render_chat(bundle, render_version="chat-v2")
        assert v1.messages == v2.messages, case.id
        assert v1.prompt_hash == v2.prompt_hash, case.id


@pytest.mark.parametrize("max_context", [9216, 16384])
@pytest.mark.parametrize("compiler_version", ["compiler-v1", "compiler-v2"])
def test_every_persona_case_renders_identically_under_chat_v2_and_chat_v3(
    max_context: int, compiler_version: str,
) -> None:
    """No corpus case carries a memory, so chat-v3's headers change none of them."""
    identity = compose_identity(REPO / "identity")
    provider = ProviderConfig(key="p", kind="fake", allowed_modes=(MODE_BENCHMARK,),
                              eval_only=True, context_budget=8000, reserved_output=1024)
    for case in load_cases(REPO / "evals/persona/cases"):
        bundle = compile_case_bundle(case, identity=identity, provider=provider,
                                     max_context=max_context, identity_cap=4000, now=NOW,
                                     compiler_version=compiler_version)
        assert not any(block.attributes for block in bundle.blocks), case.id
        v2 = render_chat(bundle, render_version="chat-v2")
        v3 = render_chat(bundle, render_version="chat-v3")
        assert (v2.messages, v2.prompt_hash) == (v3.messages, v3.prompt_hash), case.id


MEMORY_ID = __import__("uuid").UUID("018f2c00-0000-7000-8000-000000000001")
ATTRIBUTES = (("scope", "relationship"), ("origin", "user_asserted"), ("support", "confirmed"),
              ("confidence", "0.80"), ("obs", "3"), ("age", "41d"), ("match", "lexical"))


def _memory_bundle():  # type: ignore[no-untyped-def]
    return compile_context(CompileRequest(
        purpose=Purpose.REPLY, user_message="Recap first, or just answer?",
        user_message_ref="m1", now=NOW, budget=Budget(total=8000, identity_cap=4000),
        estimator=CONSERVATIVE, identity=compose_identity(REPO / "identity"),
        retrieval=RetrievalInput(
            memories=(MemoryItem(
                memory_id=MEMORY_ID, pinned=False, substantive=True, attributes=ATTRIBUTES,
                content="Janu prefers Apollo not to restate his question before answering.",
            ),),
            query_text="Recap first, or just answer?", lexical_matches=1, substantive_matches=1,
        ),
    ))


def test_chat_v3_writes_a_memorys_provenance_into_its_fence_header() -> None:
    """Spec B.4: tier, ref, scope, origin, support, observations and age, in the header."""
    bundle = _memory_bundle()
    final = render_chat(bundle).messages[-1].content
    assert (
        f"<<<MEMORY tier=T3 ref=memory:{MEMORY_ID} scope=relationship origin=user_asserted"
        " support=confirmed confidence=0.80 obs=3 age=41d match=lexical>>>\n"
        "Janu prefers Apollo not to restate his question before answering.\n"
        "<<<END MEMORY>>>"
    ) in final
    assert final.endswith("Recap first, or just answer?")


def test_chat_v2_renders_the_same_bundle_without_the_attributes() -> None:
    """The versions differ in the header and nowhere else."""
    bundle = _memory_bundle()
    v2 = render_chat(bundle, render_version="chat-v2").messages[-1].content
    v3 = render_chat(bundle, render_version="chat-v3").messages[-1].content
    header = "".join(f" {key}={value}" for key, value in ATTRIBUTES)
    assert f"<<<MEMORY tier=T3 ref=memory:{MEMORY_ID}>>>" in v2
    assert v3.replace(header, "") == v2


def _failed_turn_bundle():  # type: ignore[no-untyped-def]
    return compile_context(CompileRequest(
        purpose=Purpose.REPLY, user_message="Try again?", user_message_ref="m3", now=NOW,
        budget=Budget(total=8000, identity_cap=4000), estimator=CONSERVATIVE,
        identity=compose_identity(REPO / "identity"),
        history=(HistoryMessage("m1", "user", "question"),
                 HistoryMessage("m2", "apollo", "answer"),
                 HistoryMessage("m2b", "user", "unanswered: that turn failed")),
        retrieval=RetrievalInput(),
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
    assert FakeBrain().render(bundle) == render_chat(bundle, render_version="chat-v3")
    assert ChatV1Brain().render(bundle) == render_chat(bundle, render_version="chat-v1")


def test_an_unknown_render_version_is_refused() -> None:
    with pytest.raises(UnknownRenderVersionError, match="chat-v0"):
        render_chat(_failed_turn_bundle(), render_version="chat-v0")


# -- a render version names the system-role placement too (ADR-0002) ----------


def test_a_provider_without_a_system_role_records_a_different_render_version() -> None:
    """The policy region moves to a user message: different bytes, so a different version."""
    assert render_version_for(supports_system_role=True) == "chat-v3"
    assert render_version_for(supports_system_role=False) == "chat-v3-nosystem"
    assert render_version_for(supports_system_role=False, chat_version="chat-v1") == (
        "chat-v1-nosystem")
    for version in CHAT_RENDER_VERSIONS:
        assert split_render_version(version) == (version, True)
        assert split_render_version(f"{version}-nosystem") == (version, False)
    for unknown in ("chat-v0", "chat-v0-nosystem", "chat-v3-other", "", "-nosystem"):
        assert split_render_version(unknown) is None


def test_render_as_reproduces_exactly_what_each_version_and_placement_sent() -> None:
    bundle = _failed_turn_bundle()
    for version in CHAT_RENDER_VERSIONS:
        assert render_as(bundle, version) == render_chat(bundle, render_version=version)
        without = render_as(bundle, f"{version}-nosystem")
        assert without == render_chat(bundle, supports_system_role=False,
                                      render_version=version)
        assert without.messages[0].role == "user"
        assert without.prompt_hash != render_as(bundle, version).prompt_hash
    with pytest.raises(UnknownRenderVersionError, match="chat-v0"):
        render_as(bundle, "chat-v0")


def test_the_real_model_adapter_records_the_version_it_renders_with() -> None:
    from apollo.brains.openai_compatible import OpenAICompatibleBrain

    def brain(**options):  # type: ignore[no-untyped-def]
        return OpenAICompatibleBrain(key="local", base_url="http://127.0.0.1:1/v1",
                                     model="m", max_context=9024, **options)

    bundle = _failed_turn_bundle()
    with_system, without = brain(), brain(supports_system_role=False)
    assert (with_system.render_version, without.render_version) == (
        "chat-v3", "chat-v3-nosystem")
    assert with_system.render(bundle) == render_as(bundle, "chat-v3")
    assert without.render(bundle) == render_as(bundle, "chat-v3-nosystem")
    # One instance's setting does not leak into the class or another instance.
    assert OpenAICompatibleBrain.render_version == brain().render_version == "chat-v3"


# -- chat-v1 stays byte-stable for invocations recorded before the bump -------

_FIXED_IDENTITY_TEXT = "You are Apollo. This identity is a fixed test fixture."
#: Independent of identity/ on disk, so an identity edit cannot move these pins.
_FIXED_IDENTITY = Identity(
    version_label="guard-1", schema_version=1, content=_FIXED_IDENTITY_TEXT,
    content_hash=hashlib.sha256(_FIXED_IDENTITY_TEXT.encode()).hexdigest(), fragments=(),
)

#: name: (history, bundle_hash, chat-v1 prompt_hash). The prompt hashes are what
#: render_chat produced before chat-v2 existed, checked against that code.
CHAT_V1_PINS = {
    "one_unanswered_before_the_request": (
        (HistoryMessage("m1", "user", "question"),
         HistoryMessage("m2", "apollo", "answer"),
         HistoryMessage("m3", "user", "unanswered: that turn failed")),
        "06504c33349f64771bb71c01b4b1737f80c1ff6b3daa2f089225f6f594ce5d7d",
        "6ec7dbace243675fe94d821a06ef1d8f817b071b5dc8ac665ea93c70a9fd767f",
    ),
    "two_unanswered_mid_history": (
        (HistoryMessage("m1", "user", "first, unanswered"),
         HistoryMessage("m2", "user", "second, unanswered"),
         HistoryMessage("m3", "user", "third, answered"),
         HistoryMessage("m4", "apollo", "the answer")),
        "25f12fa573711ac26f664c7023aa55e97540d450c18255a7fdd03573aa084ec2",
        "e58880f7c3792778405cf09e143a0a4b76ffb51510a2b2a50375189c91cef8a1",
    ),
}


@pytest.mark.parametrize("name", sorted(CHAT_V1_PINS))
def test_chat_v1_still_renders_failed_turn_history_byte_for_byte(name: str) -> None:
    history, bundle_hash, v1_prompt_hash = CHAT_V1_PINS[name]
    bundle = compile_context(CompileRequest(
        purpose=Purpose.REPLY, user_message="Try again?", user_message_ref="mreq", now=NOW,
        budget=Budget(total=8000, identity_cap=4000), estimator=CONSERVATIVE,
        identity=_FIXED_IDENTITY, history=history,
        # The pins are bundles of the compiler that recorded them.
        compiler_version=COMPILER_V1,
    ))
    # If this fails, compiler-v1 moved, not the renderer. Its output is what
    # replay rebuilds old invocations from: restore it; never re-pin it.
    assert bundle.bundle_hash == bundle_hash, "compiler-v1's output moved"
    # If this fails, chat-v1 moved: every invocation recorded before chat-v2
    # would no longer reproduce. Restore chat-v1; never re-pin it.
    assert render_chat(bundle, render_version="chat-v1").prompt_hash == v1_prompt_hash
    assert render_chat(bundle).prompt_hash != v1_prompt_hash
