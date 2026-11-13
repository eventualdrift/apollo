"""Rebuilding a bundle from its manifest and its block contents (spec H.4).

Replay has only what an invocation recorded: a manifest, and references to
the rows that held each block's content. These tests take bundles the compiler
built, keep nothing but the manifest and the contents, and check that the
rebuild is the same bundle: same blocks, same hash, same rendering. No
database: `apollo.context.rebuild` reads none.

The whole persona corpus is rebuilt under every compiler version, since those
are the bundles every recorded eval invocation carries.
"""

from __future__ import annotations

import pathlib
import uuid
from datetime import UTC, datetime

import pytest

from apollo.brains.base import render_chat
from apollo.config import MODE_BENCHMARK, ProviderConfig
from apollo.context.budget import Budget
from apollo.context.bundle import BlockType, ContextBundle, Purpose
from apollo.context.compiler import (
    NO_RETRIEVAL_NOTICE,
    RETRIEVAL_ERROR_NOTICE,
    CompileRequest,
    HistoryMessage,
    MemoryItem,
    RetrievalInput,
    compile_context,
    retrieval_notice,
)
from apollo.context.estimator import CONSERVATIVE, estimator_named
from apollo.context.rebuild import (
    fixed_text,
    rebuilt_block,
    rebuilt_bundle,
    shown_matches,
    shown_pinned,
)
from apollo.context.rules import (
    COMPILER_VERSIONS,
    CONTEXT_RULES_BY_VERSION,
    PROPOSAL_RULES_BY_VERSION,
)
from apollo.core.identity import compose_identity
from apollo.evals.loader import load_cases
from apollo.evals.runner import compile_case_bundle

REPO = pathlib.Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 28, tzinfo=UTC)
ATTRIBUTES = (("scope", "user"), ("origin", "user_asserted"), ("support", "confirmed"),
              ("confidence", "0.80"), ("obs", "3"), ("age", "41d"))


def _rebuild(bundle: ContextBundle) -> ContextBundle:
    """Through JSON, as the manifest goes through a jsonb column, and from contents alone."""
    import json

    manifest = json.loads(json.dumps(bundle.manifest_list(), sort_keys=True))
    contents = {block.position: (block.content, block.role) for block in bundle.blocks}
    blocks = [
        rebuilt_block(entry, content=contents[entry["position"]][0],
                      role=contents[entry["position"]][1], estimator=CONSERVATIVE)
        for entry in manifest if entry["included"]
    ]
    return rebuilt_bundle(purpose=bundle.purpose, compiler_version=bundle.compiler_version,
                          estimator=CONSERVATIVE, manifest=manifest, blocks=blocks)


def _memory(content: str, match: str) -> MemoryItem:
    """`match` is how it was found: the search matched it, or it is pinned, or only recent."""
    return MemoryItem(memory_id=uuid.uuid4(), content=content, pinned=match == "pinned",
                      matched=match == "lexical", attributes=ATTRIBUTES)


def _reply(retrieval: RetrievalInput, *, total: int = 8000,
           compiler_version: str = COMPILER_VERSIONS[-1]) -> ContextBundle:
    return compile_context(CompileRequest(
        purpose=Purpose.REPLY, user_message="Where is my router?", user_message_ref="m3",
        now=NOW, budget=Budget(total=total, identity_cap=4000), estimator=CONSERVATIVE,
        identity=compose_identity(REPO / "identity"),
        history=(HistoryMessage("m1", "user", "Good morning."),
                 HistoryMessage("m2", "apollo", "Morning, Janu.")),
        retrieval=retrieval, compiler_version=compiler_version,
    ))


@pytest.mark.parametrize("compiler_version", COMPILER_VERSIONS)
def test_every_persona_case_rebuilds_to_the_bundle_that_was_compiled(
    compiler_version: str,
) -> None:
    identity = compose_identity(REPO / "identity")
    provider = ProviderConfig(key="p", kind="fake", allowed_modes=(MODE_BENCHMARK,),
                              eval_only=True, context_budget=8000, reserved_output=1024)
    cases = load_cases(REPO / "evals/persona/cases")
    assert len(cases) == 30
    for case in cases:
        bundle = compile_case_bundle(case, identity=identity, provider=provider,
                                     max_context=9216, identity_cap=4000, now=NOW,
                                     compiler_version=compiler_version)
        rebuilt = _rebuild(bundle)
        assert rebuilt.bundle_hash == bundle.bundle_hash, case.id
        assert rebuilt.blocks == bundle.blocks, case.id
        assert render_chat(rebuilt).prompt_hash == render_chat(bundle).prompt_hash, case.id


@pytest.mark.parametrize(("compiler_version", "matched", "shown"), [
    ("compiler-v3", 2, 1),   # only what the search matched counts (spec E.6/2)
    ("compiler-v2", 3, 2),   # there, the pinned memory counted too
])
def test_a_bundle_with_memories_and_a_dropped_one_rebuilds_exactly(
    compiler_version: str, matched: int, shown: int
) -> None:
    """The dropped entry is in the hash, and its content is not needed (spec F.6)."""
    retrieval = RetrievalInput(
        memories=(_memory("Janu's sister is Mira.", "pinned"),
                  _memory("The router is in the hallway cupboard.", "lexical"),
                  _memory("Router notes: " + "x" * 7000, "lexical"),
                  _memory("Janu cycles on Fridays.", "recency")),
        query_text="Where is my router?", pinned_count=1, lexical_matches=2, recency_count=1,
        pinned_or_lexical=3,
    )
    bundle = _reply(retrieval, compiler_version=compiler_version)
    assert bundle.compiler_version == compiler_version
    assert [e["drop_reason"] for e in bundle.manifest if not e["included"]] == [
        "budget:memory_cap"]
    rebuilt = _rebuild(bundle)
    assert (rebuilt.bundle_hash, rebuilt.blocks) == (bundle.bundle_hash, bundle.blocks)
    assert (rebuilt.total_token_estimate, rebuilt.max_trust_tier, rebuilt.taint,
            rebuilt.identity_hash) == (bundle.total_token_estimate, bundle.max_trust_tier,
                                       bundle.taint, bundle.identity_hash)
    assert render_chat(rebuilt) == render_chat(bundle)

    # What the notice needs that is not in the retrieval record is in the
    # manifest, read by the rule of the version that built it.
    manifest = bundle.manifest_list()
    assert retrieval.substantive_matches(compiler_version) == matched
    assert (shown_matches(manifest, compiler_version), shown_pinned(manifest)) == (shown, 1)
    [notice] = [b for b in bundle.blocks if b.block_type is BlockType.RETRIEVAL_NOTICE]
    assert f"Substantive matches: {matched}\nNot shown: 1 matched" in notice.content
    assert notice.content == retrieval_notice(
        RetrievalInput(query_text="Where is my router?", pinned_count=1, lexical_matches=2,
                       recency_count=1, pinned_or_lexical=3),
        shown=shown_matches(manifest, compiler_version),
        pinned_shown=shown_pinned(manifest),
        compiler_version=compiler_version,
    )


def test_a_proposal_bundle_and_a_failed_search_bundle_rebuild_exactly() -> None:
    proposal = compile_context(CompileRequest(
        purpose=Purpose.MEMORY_PROPOSAL, user_message="Remember that I take my coffee black.",
        user_message_ref="m9", now=NOW, budget=Budget(total=8000, identity_cap=4000),
        estimator=CONSERVATIVE,
    ))
    failed = _reply(RetrievalInput.failure())
    for bundle in (proposal, failed):
        rebuilt = _rebuild(bundle)
        assert (rebuilt.bundle_hash, rebuilt.blocks) == (bundle.bundle_hash, bundle.blocks)
    assert _rebuild(proposal).identity_hash is None


def test_changed_content_changes_the_hash_and_a_changed_length_changes_the_entry() -> None:
    bundle = _reply(RetrievalInput(query_text="Where is my router?"))
    manifest = bundle.manifest_list()
    request = next(e for e in manifest if e["block_type"] == "USER_MESSAGE")

    same_length = rebuilt_block(request, content="Where is my rooter?", role=None,
                                estimator=CONSERVATIVE)
    assert same_length.manifest_entry(included=True) == request
    blocks = [same_length if b.position == request["position"] else b for b in bundle.blocks]
    assert rebuilt_bundle(purpose=bundle.purpose, compiler_version=bundle.compiler_version,
                          estimator=CONSERVATIVE, manifest=manifest,
                          blocks=blocks).bundle_hash != bundle.bundle_hash

    longer = rebuilt_block(request, content="Where is my router? " * 5, role=None,
                           estimator=CONSERVATIVE)
    assert longer.manifest_entry(included=True) != request


def test_each_compiler_version_keeps_its_own_fixed_texts() -> None:
    for version in COMPILER_VERSIONS:
        assert fixed_text(BlockType.CONTEXT_RULES, version) == CONTEXT_RULES_BY_VERSION[version]
        assert fixed_text(BlockType.PROPOSAL_RULES, version) == PROPOSAL_RULES_BY_VERSION[version]
        # Content that came from a row is never a fixed text.
        for from_a_row in (BlockType.IDENTITY, BlockType.MEMORY, BlockType.USER_MESSAGE,
                           BlockType.CONVERSATION_RECENT, BlockType.SOURCE_MESSAGE):
            assert fixed_text(from_a_row, version) is None
    # compiler-v1 had one fixed notice; from compiler-v2 the notice is what the
    # turn's search found, and only the failure notice is fixed.
    assert fixed_text(BlockType.RETRIEVAL_NOTICE, "compiler-v1") == NO_RETRIEVAL_NOTICE
    assert fixed_text(BlockType.RETRIEVAL_ERROR, "compiler-v1") is None
    for version in ("compiler-v2", "compiler-v3"):
        assert fixed_text(BlockType.RETRIEVAL_NOTICE, version) is None
        assert fixed_text(BlockType.RETRIEVAL_ERROR, version) == RETRIEVAL_ERROR_NOTICE
    assert fixed_text(BlockType.CONTEXT_RULES, "compiler-v0") is None


def test_an_estimator_is_found_by_the_name_an_invocation_records() -> None:
    assert estimator_named("conservative-v1") is CONSERVATIVE
    assert estimator_named("some-tokeniser") is None
