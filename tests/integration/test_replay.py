"""Replay, against real PostgreSQL and as the runtime role (spec H.4, ADR-0007).

An invocation stores no prompt. These tests record real turns, then rebuild
what each model call was sent from its manifest and the rows that manifest
names, and check both recorded hashes. They also do the things that must make
replay stop, or fail, and check that it says which and why.

Acceptance criteria met here: O.1/9, O.1/10, O.1/17, and the replay half of
O.1/19.
"""

from __future__ import annotations

import dataclasses
import shutil
import textwrap
import uuid
from pathlib import Path
from typing import Any

import pytest

from apollo.brains.base import (
    Generation,
    GenerationParams,
    ModelCapabilities,
    RenderedRequest,
    render_as,
)
from apollo.brains.fake import FakeBrain
from apollo.brains.registry import BrainRegistry
from apollo.config import SURFACE_EVAL
from apollo.context.bundle import BlockType
from apollo.context.compiler import compile_context
from apollo.context.rules import COMPILER_V1
from apollo.core import memories, replay
from apollo.core import retrieval as core_retrieval
from apollo.core import turns as core_turns
from apollo.core.conversations import create_conversation
from apollo.core.identity import IdentityLoader
from apollo.core.replay import (
    BundleStatus,
    Reason,
    RenderStatus,
    ReplayNotFoundError,
    UnavailableSource,
    replay_invocation,
    replay_turn,
)
from apollo.errors import BrainTransportError
from apollo.evals.loader import load_case_file
from apollo.evals.runner import PersonaRunner
from apollo.storage.db import Database

from .conftest import make_config
from .test_memory_in_replies import RecordingBrain, _ask, _remember, _rows
from .test_memory_proposals import ProposingBrain

pytestmark = pytest.mark.integration

SENTINEL = "zqxreplay9042"
VERIFIED = (BundleStatus.VERIFIED, RenderStatus.VERIFIED)


def _statuses(found: replay.Replay) -> tuple[BundleStatus, RenderStatus]:
    return found.bundle_status, found.render_status


def _only(db: Database, turn_id: uuid.UUID) -> replay.Replay:
    [found] = replay_turn(db, turn_id)
    return found


# -- a turn whose sources are all present (O.1/9) ------------------------------------------


def test_a_completed_turn_replays_verified_and_rebuilds_exactly_what_was_sent(
    db: Database, service_factory, clock
) -> None:
    """History, a pinned memory, a matched one, a merely recent one, and the notice."""
    _remember(db, clock, "Janu takes his coffee black.", subject="coffee")
    _remember(db, clock, "Janu's sister is called Mira.", subject="family", pinned=True)
    _remember(db, clock, "Janu cycles to work on Fridays.", subject="commute")
    conversation = create_conversation(db, now=clock())
    brain = RecordingBrain()
    _ask(db, service_factory, clock, "Good morning.", conversation=conversation, brain=brain)
    result, _ = _ask(db, service_factory, clock, "How do I take my coffee?",
                     conversation=conversation, brain=brain)

    found = _only(db, result.turn_id)
    assert _statuses(found) == VERIFIED and found.verified
    assert found.unavailable == () and found.detail is None
    assert (found.purpose, found.invocation_status, found.seq) == ("reply", "completed", 1)
    assert (found.compiler_version, found.adapter_key, found.render_version) == (
        "compiler-v2", "fake", "chat-v3",
    )
    # Byte for byte what the brain was handed, rebuilt from rows alone.
    assert found.request is not None and found.bundle is not None
    assert found.request.messages == brain.requests[1].messages
    assert [b.block_type for b in found.bundle.blocks] == [
        BlockType.IDENTITY, BlockType.CONTEXT_RULES,
        BlockType.MEMORY, BlockType.MEMORY, BlockType.MEMORY, BlockType.RETRIEVAL_NOTICE,
        BlockType.CONVERSATION_RECENT, BlockType.CONVERSATION_RECENT, BlockType.USER_MESSAGE,
    ]
    assert replay_invocation(db, result.invocation_ids[0]) == found


def test_replay_reads_and_writes_nothing(db: Database, service_factory, clock) -> None:
    result, _ = _ask(db, service_factory, clock, "Hello.")
    counts = "SELECT (SELECT count(*) FROM audit_event) AS audit," \
             " (SELECT count(*) FROM model_invocation) AS invocations," \
             " (SELECT count(*) FROM turn) AS turns, (SELECT count(*) FROM message) AS messages"
    before = _rows(db, counts)
    assert _only(db, result.turn_id).verified
    assert _rows(db, counts) == before


def test_each_invocation_of_a_turn_replays_on_its_own(
    db: Database, service_factory, clock
) -> None:
    """Per invocation, not per turn: the two calls have different bundles (spec H.4)."""
    brain = ProposingBrain()
    result = service_factory(brain=brain).submit(
        conversation_id=create_conversation(db, now=clock()),
        text="Remember that I take my coffee black.",
    )
    reply, structuring = replay_turn(db, result.turn_id)
    assert _statuses(reply) == VERIFIED and _statuses(structuring) == VERIFIED
    assert (reply.purpose, structuring.purpose) == ("reply", "memory_proposal")
    assert [b.block_type for b in structuring.bundle.blocks] == [
        BlockType.PROPOSAL_RULES, BlockType.SOURCE_MESSAGE,
    ]
    assert structuring.request.messages == brain.structurings[0].messages
    assert reply.bundle.bundle_hash != structuring.bundle.bundle_hash


def test_a_turn_whose_search_failed_replays_with_the_notice_it_was_given(
    db: Database, service_factory, clock, monkeypatch
) -> None:
    monkeypatch.setattr(core_retrieval.retrieval, "retrieve",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    result, brain = _ask(db, service_factory, clock, "What colour is my bicycle?")
    found = _only(db, result.turn_id)
    assert _statuses(found) == VERIFIED
    assert found.request.messages == brain.requests[0].messages
    assert BlockType.RETRIEVAL_ERROR in [b.block_type for b in found.bundle.blocks]


def test_a_memory_the_budget_dropped_is_not_needed_and_may_even_be_forgotten(
    db: Database, service_factory, clock
) -> None:
    """O.1/19's other half: a dropped block never entered the bundle (spec D.7)."""
    _remember(db, clock, "Janu's router is in the hallway cupboard.", subject="router")
    huge = _remember(db, clock, "Janu's router notes: " + "x" * 7000, subject="router")
    result, brain = _ask(db, service_factory, clock, "Where is my router?")
    [dropped] = [e for e in _rows(db, "SELECT context_manifest FROM model_invocation")[0][
        "context_manifest"] if not e["included"]]
    assert dropped["source_ref"] == str(huge)

    memories.tombstone_memory(db, huge, now=clock())
    found = _only(db, result.turn_id)
    assert _statuses(found) == VERIFIED and found.hashes_redacted_reason is None
    # Its notice still says a match did not fit; that is rebuilt from the
    # search's record, not from the memory.
    assert "Not shown: 1 matched" in found.request.messages[-1].content
    assert found.request.messages == brain.requests[0].messages


# -- failed attempts (O.1/10) --------------------------------------------------------------


def test_a_failed_attempt_replays_though_it_has_no_output(
    db: Database, service_factory, clock
) -> None:
    """O.1/10: what the failed attempt would have sent is rebuilt; a retry pair is diagnosable."""
    brain = FakeBrain(fail_times=1, failure=BrainTransportError(provider_error_type="timeout"))
    result = service_factory(brain=brain).submit(
        conversation_id=create_conversation(db, now=clock()), text="Are you there?"
    )
    assert result.status == "completed" and len(result.invocation_ids) == 2

    failed, retried = replay_turn(db, result.turn_id)
    assert (failed.invocation_status, retried.invocation_status) == ("failed", "completed")
    assert _statuses(failed) == (BundleStatus.VERIFIED, RenderStatus.NOT_ATTEMPTED)
    assert "did not complete" in failed.detail
    assert _statuses(retried) == VERIFIED
    # The failed attempt's request is rebuilt, and it is the request that was
    # sent: the brain saw the same bytes on both attempts.
    assert failed.request is not None
    assert failed.request.prompt_hash == brain.calls[0] == brain.calls[1]
    assert failed.request.messages == retried.request.messages


def test_a_turn_that_failed_outright_still_replays(db: Database, service_factory, clock) -> None:
    brain = FakeBrain(fail_times=2, failure=BrainTransportError(provider_error_type="timeout"))
    result = service_factory(brain=brain).submit(
        conversation_id=create_conversation(db, now=clock()), text="Are you there?"
    )
    assert result.status == "failed"
    assert [_statuses(found) for found in replay_turn(db, result.turn_id)] == [
        (BundleStatus.VERIFIED, RenderStatus.NOT_ATTEMPTED)] * 2


def test_a_turn_refused_before_any_call_has_nothing_to_replay(
    db: Database, service_factory, clock
) -> None:
    config = make_config(db._dsn, allowed_modes=("benchmark",))
    result = service_factory(config=config).submit(
        conversation_id=create_conversation(db, now=clock()), text="Hello."
    )
    assert result.status == "failed" and result.invocation_ids == ()
    assert replay_turn(db, result.turn_id) == []


def test_an_unknown_id_is_not_found(db: Database) -> None:
    with pytest.raises(ReplayNotFoundError):
        replay_turn(db, uuid.uuid4())
    with pytest.raises(ReplayNotFoundError):
        replay_invocation(db, uuid.uuid4())


# -- deletion wins over replay (O.1/9) -----------------------------------------------------


def test_a_tombstoned_memory_stops_replay_and_nothing_deleted_is_recreated(
    db: Database, service_factory, clock
) -> None:
    """O.1/9: SOURCE_REDACTED names the ref, even though a message elsewhere says the same."""
    # The claim, said in conversation: this message outlives the memory.
    said, _ = _ask(db, service_factory, clock, f"My door code is {SENTINEL}.")
    memory_id = _remember(db, clock, f"Janu's door code is {SENTINEL}.", subject="door")
    asked, brain = _ask(db, service_factory, clock, "What is my door code?")
    assert SENTINEL in brain.requests[0].messages[-1].content
    assert _statuses(_only(db, asked.turn_id)) == VERIFIED

    memories.tombstone_memory(db, memory_id, now=clock())

    found = _only(db, asked.turn_id)
    assert _statuses(found) == (BundleStatus.SOURCE_REDACTED, RenderStatus.NOT_ATTEMPTED)
    assert found.unavailable == (
        UnavailableSource("memory", str(memory_id), Reason.TOMBSTONED),
    )
    assert found.hashes_redacted_reason == "source_tombstoned"
    # Reconstruction stops: no bundle, no request, and the claim is nowhere in
    # what replay returns, although the message that first said it is intact.
    assert found.bundle is None and found.request is None
    assert SENTINEL not in repr(found)
    [message] = _rows(db, "SELECT content FROM message WHERE turn_id IS NULL"
                          " AND content LIKE %s", f"%{SENTINEL}.")
    assert SENTINEL in message["content"]

    # The boundary the other way: the turn where Janu said it replays in full,
    # because a message is not a memory and was not deleted (spec D.7).
    source = _only(db, said.turn_id)
    assert _statuses(source) == VERIFIED
    assert SENTINEL in source.request.messages[-1].content


def test_every_turn_that_used_any_version_of_a_forgotten_claim_is_redacted(
    db: Database, service_factory, clock
) -> None:
    """Spec D.9/1: the tombstone is chain-wide, and so is what replay reports."""
    old = _remember(db, clock, f"Janu's door code is {SENTINEL}.", subject="door")
    first, _ = _ask(db, service_factory, clock, "What is my door code?")
    new = memories.correct_memory(db, old, content="Janu's door code is 5512.", now=clock())
    second, _ = _ask(db, service_factory, clock, "What is my door code?")

    memories.tombstone_memory(db, new, now=clock())
    for turn, memory_id in ((first, old), (second, new)):
        found = _only(db, turn.turn_id)
        assert found.bundle_status is BundleStatus.SOURCE_REDACTED
        assert found.unavailable == (
            UnavailableSource("memory", str(memory_id), Reason.TOMBSTONED),
        )
        assert "5512" not in repr(found) and SENTINEL not in repr(found)


# -- what preserves content preserves replay (spec H.4 table; O.1/17) -----------------------


def test_correcting_a_memory_leaves_turns_that_used_the_old_one_verified(
    db: Database, service_factory, clock
) -> None:
    """O.1/17: the old row is superseded, keeps its content, and is still the source."""
    old = _remember(db, clock, "Janu's flat is on the second floor.", subject="flat")
    before, brain = _ask(db, service_factory, clock, "Which floor is my flat on?")

    new = memories.correct_memory(db, old, content="Janu's flat is on the third floor.",
                                  now=clock())
    assert memories.get_memory(db, old).status.value == "superseded"
    assert memories.get_memory(db, old).content == "Janu's flat is on the second floor."
    assert memories.get_memory(db, new).status.value == "active"

    found = _only(db, before.turn_id)
    assert _statuses(found) == VERIFIED
    assert "second floor" in found.request.messages[-1].content
    assert "third floor" not in found.request.messages[-1].content
    assert found.request.messages == brain.requests[0].messages


@pytest.mark.parametrize("operation", ["confirm", "contradict", "archive"])
def test_evidence_and_archiving_leave_replay_verified(
    db: Database, service_factory, clock, operation: str
) -> None:
    """Observations are not part of a block's body, and an archived row keeps its content.

    The header's support and observation count are read back from the
    manifest, so a later confirmation does not change what is rebuilt.
    """
    memory_id = _remember(db, clock, "Janu's flat is on the second floor.", subject="flat")
    result, brain = _ask(db, service_factory, clock, "Which floor is my flat on?")
    getattr(memories, f"{operation}_memory")(db, memory_id, now=clock())

    found = _only(db, result.turn_id)
    assert _statuses(found) == VERIFIED
    assert found.request.messages == brain.requests[0].messages
    assert "support=asserted" in found.request.messages[-1].content


def test_a_turn_replays_with_the_identity_it_was_given(
    db: Database, service_factory, clock, tmp_path
) -> None:
    """An identity change does not touch earlier turns: each version is snapshotted."""
    before, _ = _ask(db, service_factory, clock, "Who are you?")

    changed = tmp_path / "identity"
    shutil.copytree(make_config(db._dsn).identity_dir, changed)
    fragment = sorted(changed.glob("*.md"))[0]
    fragment.write_text(fragment.read_text(encoding="utf-8") + "\nApollo likes brevity.\n",
                        encoding="utf-8")
    config = dataclasses.replace(make_config(db._dsn), identity_dir=changed)
    brain = RecordingBrain()
    after = service_factory(brain=brain, config=config).submit(
        conversation_id=create_conversation(db, now=clock()), text="Who are you?"
    )

    old, new = _only(db, before.turn_id), _only(db, after.turn_id)
    assert _statuses(old) == VERIFIED and _statuses(new) == VERIFIED
    assert old.bundle.identity_hash != new.bundle.identity_hash
    assert "Apollo likes brevity." in new.request.messages[0].content
    assert "Apollo likes brevity." not in old.request.messages[0].content


# -- older vintages, and a retired one (spec H.4, ADR-0002) --------------------------------


class RenderedAs(FakeBrain):
    """brain.fake rendering with a named version, as an adapter of that vintage did."""

    def __init__(self, render_version: str) -> None:
        super().__init__(key="fake")
        self.render_version = render_version
        self.requests: list[RenderedRequest] = []

    def capabilities(self) -> ModelCapabilities:
        return dataclasses.replace(
            super().capabilities(),
            supports_system_role=not self.render_version.endswith("-nosystem"),
        )

    def render(self, bundle: Any) -> RenderedRequest:
        return render_as(bundle, self.render_version)

    def generate(self, req: RenderedRequest, params: GenerationParams) -> Generation:
        self.requests.append(req)
        return super().generate(req, params)


@pytest.mark.parametrize("render_version",
                         ["chat-v1", "chat-v2", "chat-v3", "chat-v3-nosystem", "chat-v1-nosystem"])
def test_an_invocation_is_rendered_again_with_the_version_it_recorded(
    db: Database, service_factory, clock, render_version: str
) -> None:
    """Not the current default: the renderer of its vintage, system-role placement included."""
    conversation = create_conversation(db, now=clock())
    brain = RenderedAs(render_version)
    # An unanswered message first, which chat-v1 and chat-v2 render differently.
    failing = FakeBrain(fail_times=2, failure=BrainTransportError(provider_error_type="timeout"))
    service_factory(brain=failing).submit(conversation_id=conversation, text="Hello?")
    result = service_factory(brain=brain).submit(conversation_id=conversation, text="Anyone?")

    found = _only(db, result.turn_id)
    assert found.render_version == render_version
    assert _statuses(found) == VERIFIED
    assert found.request.messages == brain.requests[0].messages
    policy_role = "user" if render_version.endswith("-nosystem") else "system"
    assert found.request.messages[0].role == policy_role


def test_a_turn_built_by_the_first_compiler_replays_with_its_texts(
    db: Database, service_factory, clock, monkeypatch
) -> None:
    """compiler-v1, as every conversation before memory was built: a fixed notice, no search."""
    monkeypatch.setattr(
        core_turns, "compile_context",
        lambda request: compile_context(
            dataclasses.replace(request, compiler_version=COMPILER_V1)),
    )
    brain = RenderedAs("chat-v2")
    result = service_factory(brain=brain).submit(
        conversation_id=create_conversation(db, now=clock()), text="Hello."
    )
    monkeypatch.undo()

    found = _only(db, result.turn_id)
    assert (found.compiler_version, found.render_version) == ("compiler-v1", "chat-v2")
    assert _statuses(found) == VERIFIED
    assert found.request.messages == brain.requests[0].messages
    assert "not yet available in this build" in found.request.messages[-1].content


def test_a_retired_render_version_is_reported_and_never_compared(
    db: Database, service_factory, clock
) -> None:
    """ADR-0002: RENDERER_UNAVAILABLE rather than a false match. The bundle still verifies."""

    class Retired(RecordingBrain):
        render_version = "chat-v0"

    result, _ = _ask(db, service_factory, clock, "Hello.", brain=Retired())
    found = _only(db, result.turn_id)
    assert _statuses(found) == (BundleStatus.VERIFIED, RenderStatus.RENDERER_UNAVAILABLE)
    assert found.bundle is not None and found.request is None


def test_an_adapter_this_code_does_not_have_is_reported_the_same_way(
    db: Database, service_factory, clock
) -> None:
    class Foreign(RecordingBrain):
        adapter_key = "some_other_adapter"

    result, _ = _ask(db, service_factory, clock, "Hello.", brain=Foreign())
    assert _statuses(_only(db, result.turn_id)) == (
        BundleStatus.VERIFIED, RenderStatus.RENDERER_UNAVAILABLE,
    )


def test_a_compiler_or_estimator_this_code_no_longer_has_is_a_missing_source(
    db: Database, owner_db: Database, service_factory, clock
) -> None:
    result, _ = _ask(db, service_factory, clock, "Hello.")
    # The owner rewrites what the invocation recorded, as a later release that
    # retired a version would find it.
    with owner_db.connect() as conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE model_invocation DISABLE TRIGGER USER")
        cur.execute("UPDATE model_invocation SET compiler_version = 'compiler-v0',"
                    " token_estimator = 'tokeniser-x'")
        cur.execute("ALTER TABLE model_invocation ENABLE TRIGGER USER")
    found = _only(db, result.turn_id)
    assert _statuses(found) == (BundleStatus.SOURCE_MISSING, RenderStatus.NOT_ATTEMPTED)
    assert set(found.unavailable) == {
        UnavailableSource("compiler", "compiler-v0", Reason.NOT_IMPLEMENTED),
        UnavailableSource("estimator", "tokeniser-x", Reason.NOT_IMPLEMENTED),
    }


# -- mismatch: something believed write-once changed ----------------------------------------


def _as_owner_without_guards(owner_db: Database, table: str, sql: str, *params: Any) -> None:
    with owner_db.connect() as conn, conn.cursor() as cur:
        cur.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
        cur.execute(sql, params)
        cur.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")


def test_a_source_that_changed_under_a_recorded_turn_is_a_mismatch(
    db: Database, owner_db: Database, service_factory, clock
) -> None:
    """Spec H.4: all sources present, hash differs. "A bug worth knowing about."."""
    memory_id = _remember(db, clock, "Janu's flat is on the second floor.", subject="flat")
    result, _ = _ask(db, service_factory, clock, "Which floor is my flat on?")
    assert _statuses(_only(db, result.turn_id)) == VERIFIED

    # Same length, so the token count still agrees: only the hash can tell.
    _as_owner_without_guards(
        owner_db, "memory", "UPDATE memory SET content = %s WHERE id = %s",
        "Janu's flat is on the sixtth floor.", memory_id,
    )
    found = _only(db, result.turn_id)
    assert _statuses(found) == (BundleStatus.MISMATCH, RenderStatus.NOT_ATTEMPTED)
    assert "hash differs" in found.detail
    assert found.bundle is None and found.request is None
    assert "sixtth" not in repr(found)


def test_a_block_that_no_longer_counts_the_same_is_a_mismatch_naming_the_field(
    db: Database, owner_db: Database, service_factory, clock
) -> None:
    result, _ = _ask(db, service_factory, clock, "Hello.")
    _as_owner_without_guards(
        owner_db, "message", "UPDATE message SET content = %s WHERE role = 'user'",
        "Hello, and a good deal more than was said at the time.",
    )
    found = _only(db, result.turn_id)
    assert found.bundle_status is BundleStatus.MISMATCH
    assert "USER_MESSAGE" in found.detail and "tokens" in found.detail
    assert "good deal more" not in repr(found)


def test_a_prompt_hash_that_does_not_match_is_a_render_mismatch(
    db: Database, owner_db: Database, service_factory, clock
) -> None:
    result, _ = _ask(db, service_factory, clock, "Hello.")
    _as_owner_without_guards(
        owner_db, "model_invocation",
        "UPDATE model_invocation SET rendered_prompt_hash = %s", "0" * 64,
    )
    found = _only(db, result.turn_id)
    assert _statuses(found) == (BundleStatus.VERIFIED, RenderStatus.MISMATCH)
    assert found.request is None  # not offered as what was sent: it is not


# -- missing, which is not redacted ----------------------------------------------------------


def test_a_source_that_is_simply_gone_is_missing_not_redacted(
    db: Database, owner_db: Database, service_factory, clock
) -> None:
    """A different fact from deliberate deletion, as a failed search differs from an empty one."""
    conversation = create_conversation(db, now=clock())
    _ask(db, service_factory, clock, "Good morning.", conversation=conversation)
    result, _ = _ask(db, service_factory, clock, "Hello again.", conversation=conversation)
    [gone] = _rows(db, "SELECT id FROM message WHERE content = 'Good morning.'")
    with owner_db.connect() as conn, conn.cursor() as cur:
        cur.execute("SET session_replication_role = replica")  # no triggers, no foreign keys
        cur.execute("DELETE FROM message WHERE id = %s", (gone["id"],))

    found = _only(db, result.turn_id)
    assert _statuses(found) == (BundleStatus.SOURCE_MISSING, RenderStatus.NOT_ATTEMPTED)
    assert found.unavailable == (UnavailableSource("message", str(gone["id"]), Reason.MISSING),)
    assert found.hashes_redacted_reason is None


def test_a_turn_without_its_retrieval_record_cannot_rebuild_its_notice(
    db: Database, owner_db: Database, service_factory, clock
) -> None:
    result, _ = _ask(db, service_factory, clock, "Hello.")
    with owner_db.connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM turn_retrieval_result")
        cur.execute("DELETE FROM turn_retrieval")
    found = _only(db, result.turn_id)
    assert found.bundle_status is BundleStatus.SOURCE_MISSING
    assert found.unavailable == (
        UnavailableSource("turn_retrieval", str(result.turn_id), Reason.NO_RETRIEVAL_RECORD),
    )


def test_redaction_is_reported_even_beside_a_missing_source(
    db: Database, owner_db: Database, service_factory, clock
) -> None:
    memory_id = _remember(db, clock, f"Janu's door code is {SENTINEL}.", subject="door")
    conversation = create_conversation(db, now=clock())
    _ask(db, service_factory, clock, "Good morning.", conversation=conversation)
    result, _ = _ask(db, service_factory, clock, "What is my door code?",
                     conversation=conversation)
    memories.tombstone_memory(db, memory_id, now=clock())
    [gone] = _rows(db, "SELECT id FROM message WHERE content = 'Good morning.'")
    with owner_db.connect() as conn, conn.cursor() as cur:
        cur.execute("SET session_replication_role = replica")
        cur.execute("DELETE FROM message WHERE id = %s", (gone["id"],))

    found = _only(db, result.turn_id)
    assert found.bundle_status is BundleStatus.SOURCE_REDACTED
    assert {(s.source_kind, s.reason) for s in found.unavailable} == {
        ("memory", Reason.TOMBSTONED), ("message", Reason.MISSING),
    }


# -- eval turns ------------------------------------------------------------------------------

CASES = """\
version: 1
cases:
  - id: per_replay
    tags: [form]
    covers_rules: [B18]
    covers_probes: [verbosity]
    behavioural_expectation: Short answer to a closed question.
    undesired_characteristics: [expands a one-line answer]
    setup:
      mode: benchmark
      memories: []
      history:
        - {role: user, content: "Hello."}
        - {role: apollo, content: "Hello, Janu."}
    input: What is the door code?
    checks:
      - type: max_words
        value: 500
"""


def test_an_eval_turn_names_its_fixtures_as_not_being_rows(
    db: Database, clock, tmp_path: Path
) -> None:
    """A persona case's sources are in its case file, by design (so bundles compare across
    brains). Replay from the database says so; Gate 2 rebuilds eval bundles from the run record.
    """
    cases = tmp_path / "cases.yaml"
    cases.write_text(textwrap.dedent(CASES), encoding="utf-8")
    config = make_config(db._dsn)
    runner = PersonaRunner(
        db, config, BrainRegistry(config, surface=SURFACE_EVAL),
        IdentityLoader(config.identity_dir), clock=clock, runs_dir=tmp_path / "runs",
    )
    runner.run(load_case_file(cases), brain_alias="brain.default")
    [turn] = _rows(db, "SELECT DISTINCT turn_id FROM model_invocation LIMIT 1")

    found = replay_turn(db, turn["turn_id"])[0]
    assert found.bundle_status is BundleStatus.SOURCE_MISSING
    assert found.unavailable and {s.reason for s in found.unavailable} == {Reason.NOT_A_ROW}
    assert {s.source_kind for s in found.unavailable} == {"message"}
