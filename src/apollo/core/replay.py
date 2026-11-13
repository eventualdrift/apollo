"""Replay: rebuild what an invocation was sent, and prove it or say why not (spec H.4).

No rendered prompt is stored. An invocation records a manifest (what each
block was and which row its content came from), the versions that built and
rendered it, and two hashes. Replay reads the rows the manifest names,
rebuilds the bundle with the compiler texts and the estimator of its vintage,
compares the bundle hash, renders it with the recorded render version, and
compares the prompt hash.

It reports two independent statuses, never one optimistic boolean:

* `bundle_status`: VERIFIED, MISMATCH, SOURCE_REDACTED or SOURCE_MISSING;
* `render_status`: VERIFIED, MISMATCH, RENDERER_UNAVAILABLE or NOT_ATTEMPTED.

Three rules are the point of the module:

**Deletion wins over replay.** A block whose memory was tombstoned is not
rebuilt, and neither is the bundle around it. Replay stops and names the
references that are gone.

**A source is never substituted.** A MEMORY block is rebuilt from the memory
row its entry names and from nothing else: not the message the claim was
first said in, not an observation excerpt, not another version of the chain.
Re-deriving a deleted claim from somewhere similar would undo the deletion
being reported.

**Replay is per invocation, and a failed one replays too.** Its bundle was
built before the call, so what the failed attempt would have sent can be
rebuilt even though nothing came back.

Replay only reads. It writes no row and no audit event, and it returns the
rebuilt text to the caller without logging it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from apollo.brains.base import RenderedRequest, render_as, split_render_version
from apollo.brains.fake import FakeBrain
from apollo.brains.openai_compatible import OpenAICompatibleBrain
from apollo.context.bundle import BlockType, ContextBlock, ContextBundle, Purpose
from apollo.context.compiler import RetrievalInput, retrieval_notice
from apollo.context.estimator import estimator_named
from apollo.context.rebuild import (
    fixed_text,
    rebuilt_block,
    rebuilt_bundle,
    shown_matches,
    shown_pinned,
)
from apollo.context.rules import COMPILER_VERSIONS
from apollo.errors import ApolloError
from apollo.memory.models import MANIFEST_SOURCE_KIND, Status
from apollo.memory.retrieval import LEXICAL, PINNED, RECENCY
from apollo.storage.db import Database
from apollo.storage.repositories import (
    IdentityVersionRepository,
    InvocationRepository,
    MemoryRepository,
    MessageRepository,
    RetrievalRepository,
    TurnRepository,
)
from apollo.storage.unit_of_work import UnitOfWork, unit_of_work

#: Adapters whose requests the shared chat renderer produced. An invocation
#: recorded by any other adapter has no renderer here.
CHAT_ADAPTER_KEYS = frozenset({FakeBrain.adapter_key, OpenAICompatibleBrain.adapter_key})


class BundleStatus(StrEnum):
    VERIFIED = "VERIFIED"
    MISMATCH = "MISMATCH"
    SOURCE_REDACTED = "SOURCE_REDACTED"
    SOURCE_MISSING = "SOURCE_MISSING"


class RenderStatus(StrEnum):
    VERIFIED = "VERIFIED"
    MISMATCH = "MISMATCH"
    RENDERER_UNAVAILABLE = "RENDERER_UNAVAILABLE"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


class Reason(StrEnum):
    """Why a source could not be used. Closed words; never text from a row."""

    #: The memory was deliberately forgotten. The only reason that is a redaction.
    TOMBSTONED = "tombstoned"
    #: The reference names a row that is not there.
    MISSING = "missing"
    #: The reference never named a row: an eval fixture or a probe, whose text
    #: is in a case file or a run record and not in the database.
    NOT_A_ROW = "not_a_database_row"
    #: The recorded compiler version or estimator is no longer implemented.
    NOT_IMPLEMENTED = "not_implemented"
    #: The turn's retrieval record, from which its notice is rebuilt, is absent.
    NO_RETRIEVAL_RECORD = "no_retrieval_record"


class ReplayNotFoundError(ApolloError):
    """No turn, or no invocation, has that id."""


@dataclass(frozen=True)
class UnavailableSource:
    """One source replay could not use: what kind, which, and why. Never its text."""

    source_kind: str
    source_ref: str
    reason: Reason

    @property
    def redacted(self) -> bool:
        return self.reason is Reason.TOMBSTONED


@dataclass(frozen=True)
class Replay:
    """The outcome of replaying one invocation."""

    invocation_id: uuid.UUID
    turn_id: uuid.UUID
    seq: int
    purpose: str
    #: The invocation's own status: a failed attempt replays like any other.
    invocation_status: str
    compiler_version: str
    adapter_key: str
    render_version: str
    bundle_status: BundleStatus
    render_status: RenderStatus
    #: Every source that could not be used, redacted or missing, each with its reason.
    unavailable: tuple[UnavailableSource, ...] = ()
    #: Set when the invocation's verification hashes were redacted (spec D.7).
    hashes_redacted_reason: str | None = None
    #: A short, content-free account of a mismatch or of why rendering was not tried.
    detail: str | None = None
    #: The rebuilt bundle, only when its hash was verified.
    bundle: ContextBundle | None = None
    #: The rebuilt request. Verified against the recorded prompt hash when
    #: `render_status` is VERIFIED; otherwise what would have been sent, with
    #: nothing recorded to check it against.
    request: RenderedRequest | None = None

    @property
    def verified(self) -> bool:
        return (self.bundle_status is BundleStatus.VERIFIED
                and self.render_status is RenderStatus.VERIFIED)


def replay_turn(db: Database, turn_id: uuid.UUID) -> list[Replay]:
    """Replay every invocation of a turn, in the order they were made."""
    with unit_of_work(db, expect_audit=False) as uow:
        if TurnRepository(uow).get(turn_id) is None:
            raise ReplayNotFoundError("no turn has that id")
        return [_replay(uow, row) for row in InvocationRepository(uow).for_turn(turn_id)]


def replay_invocation(db: Database, invocation_id: uuid.UUID) -> Replay:
    with unit_of_work(db, expect_audit=False) as uow:
        row = InvocationRepository(uow).get(invocation_id)
        if row is None:
            raise ReplayNotFoundError("no invocation has that id")
        return _replay(uow, row)


@dataclass(frozen=True)
class ReplaySummary:
    """Every recorded invocation, replayed and counted. Ids and statuses only."""

    #: (bundle status, render status) -> how many invocations replayed that way.
    counts: Mapping[tuple[BundleStatus, RenderStatus], int]
    #: Invocations whose sources are eval fixtures, by design not database rows.
    fixture_sourced: int
    #: Invocations that did not reproduce and that nothing on record explains:
    #: a mismatch, or a source missing for a reason other than being a fixture.
    unexplained: tuple[uuid.UUID, ...]

    @property
    def total(self) -> int:
        return sum(self.counts.values()) + self.fixture_sourced

    @property
    def sound(self) -> bool:
        return not self.unexplained


def replay_all(db: Database) -> ReplaySummary:
    """Replay every invocation in the database and say how each came out.

    This is how a restored database proves itself: every hash an invocation
    recorded is recomputed from the rows that were restored, so content that
    did not survive intact shows up as a mismatch (spec K.8, H.4).
    """
    counts: dict[tuple[BundleStatus, RenderStatus], int] = {}
    fixture_sourced = 0
    unexplained: list[uuid.UUID] = []
    with unit_of_work(db, expect_audit=False) as uow:
        ids = [row["id"] for row in uow.execute(
            "SELECT id FROM model_invocation ORDER BY started_at, id").fetchall()]
        repo = InvocationRepository(uow)
        for invocation_id in ids:
            row = repo.get(invocation_id)
            assert row is not None
            found = _replay(uow, row)
            if found.unavailable and all(
                source.reason is Reason.NOT_A_ROW for source in found.unavailable
            ):
                fixture_sourced += 1
                continue
            key = (found.bundle_status, found.render_status)
            counts[key] = counts.get(key, 0) + 1
            if (found.bundle_status in {BundleStatus.MISMATCH, BundleStatus.SOURCE_MISSING}
                    or found.render_status is RenderStatus.MISMATCH):
                unexplained.append(invocation_id)
    return ReplaySummary(counts, fixture_sourced, tuple(unexplained))


# -- one invocation ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Source:
    content: str
    role: str | None = None


def _replay(uow: UnitOfWork, row: Mapping[str, Any]) -> Replay:
    def outcome(
        bundle_status: BundleStatus,
        render_status: RenderStatus = RenderStatus.NOT_ATTEMPTED,
        **more: Any,
    ) -> Replay:
        return Replay(
            invocation_id=row["id"], turn_id=row["turn_id"], seq=row["seq"],
            purpose=row["purpose"], invocation_status=row["status"],
            compiler_version=row["compiler_version"], adapter_key=row["adapter_key"],
            render_version=row["render_version"], bundle_status=bundle_status,
            render_status=render_status,
            hashes_redacted_reason=row["hashes_redacted_reason"], **more,
        )

    manifest: list[dict[str, Any]] = list(row["context_manifest"])
    included = sorted((e for e in manifest if e.get("included")), key=lambda e: e["position"])
    estimator = estimator_named(row["token_estimator"])

    unavailable: list[UnavailableSource] = []
    buildable = row["compiler_version"] in COMPILER_VERSIONS
    if not buildable:
        unavailable.append(
            UnavailableSource("compiler", row["compiler_version"], Reason.NOT_IMPLEMENTED)
        )
    if estimator is None:
        unavailable.append(
            UnavailableSource("estimator", row["token_estimator"], Reason.NOT_IMPLEMENTED)
        )

    sources: list[_Source | None] = []
    for entry in included:
        if entry["source_kind"] == "compiler" and not buildable:
            sources.append(None)  # reported once, above, for the whole invocation
            continue
        found = _source(uow, entry, row, manifest)
        if isinstance(found, UnavailableSource):
            unavailable.append(found)
            sources.append(None)
        else:
            sources.append(found)

    if unavailable:
        # Deliberate deletion is reported as such even beside an unexplained
        # gap: the two are different facts, and each ref carries its own reason.
        redacted = any(source.redacted for source in unavailable)
        return outcome(
            BundleStatus.SOURCE_REDACTED if redacted else BundleStatus.SOURCE_MISSING,
            unavailable=tuple(unavailable),
        )
    if row["context_bundle_hash"] is None:
        # Redacted with every source still readable: the hash is gone on
        # purpose, so nothing can be verified, and nothing is rebuilt.
        return outcome(BundleStatus.SOURCE_REDACTED,
                       detail="the verification hashes were redacted")

    assert estimator is not None
    blocks: list[ContextBlock] = []
    for entry, source in zip(included, sources, strict=True):
        assert source is not None
        block = rebuilt_block(entry, content=source.content, role=source.role,
                              estimator=estimator)
        if block.manifest_entry(included=True) != entry:
            return outcome(BundleStatus.MISMATCH, detail=_entry_difference(entry, block))
        blocks.append(block)
    bundle = rebuilt_bundle(
        purpose=Purpose(row["purpose"]), compiler_version=row["compiler_version"],
        estimator=estimator, manifest=manifest, blocks=blocks,
    )
    if bundle.bundle_hash != row["context_bundle_hash"]:
        return outcome(
            BundleStatus.MISMATCH,
            detail="every source is present, and the rebuilt bundle's hash differs from "
                   "the one recorded",
        )

    renderable = (row["adapter_key"] in CHAT_ADAPTER_KEYS
                  and split_render_version(row["render_version"]) is not None)
    if not renderable:
        return outcome(BundleStatus.VERIFIED, RenderStatus.RENDERER_UNAVAILABLE, bundle=bundle)
    try:
        request = render_as(bundle, row["render_version"])
    except ApolloError as exc:
        # The recorded request rendered, or it would have no hash; the rebuilt
        # one does not. The type says which contract; the text is not kept.
        return outcome(BundleStatus.VERIFIED, RenderStatus.MISMATCH, bundle=bundle,
                       detail=f"the rebuilt bundle no longer renders: {type(exc).__name__}")
    if row["rendered_prompt_hash"] is None:
        return outcome(
            BundleStatus.VERIFIED, RenderStatus.NOT_ATTEMPTED, bundle=bundle, request=request,
            detail="no prompt hash was recorded: the invocation did not complete",
        )
    if request.prompt_hash != row["rendered_prompt_hash"]:
        return outcome(BundleStatus.VERIFIED, RenderStatus.MISMATCH, bundle=bundle)
    return outcome(BundleStatus.VERIFIED, RenderStatus.VERIFIED, bundle=bundle, request=request)


def _entry_difference(entry: Mapping[str, Any], block: ContextBlock) -> str:
    """Which recorded fields the rebuilt block disagrees with. Names only, no values."""
    rebuilt = block.manifest_entry(included=True)
    fields = sorted(key for key in set(entry) | set(rebuilt) if entry.get(key) != rebuilt.get(key))
    return (f"block {entry['position']} ({entry['block_type']}) rebuilds with a different "
            f"{', '.join(fields)} than was recorded")


# -- one source --------------------------------------------------------------------------


def _source(
    uow: UnitOfWork,
    entry: Mapping[str, Any],
    row: Mapping[str, Any],
    manifest: list[dict[str, Any]],
) -> _Source | UnavailableSource:
    """The content of one included block, read from the source its entry names."""
    kind, ref = str(entry["source_kind"]), entry["source_ref"]
    if kind == "identity":
        identity = IdentityVersionRepository(uow).get(str(ref))
        if identity is None:
            return UnavailableSource(kind, str(ref), Reason.MISSING)
        return _Source(identity["content"])
    if kind == "compiler":
        return _core_authored(uow, entry, row, manifest)
    if kind == "message":
        return _message(uow, entry)
    if kind == MANIFEST_SOURCE_KIND:
        return _memory(uow, entry)
    return UnavailableSource(kind, str(ref), Reason.MISSING)


def _core_authored(
    uow: UnitOfWork,
    entry: Mapping[str, Any],
    row: Mapping[str, Any],
    manifest: list[dict[str, Any]],
) -> _Source | UnavailableSource:
    """Rules and notices: Core's own text, of the compiler version that wrote it."""
    version = row["compiler_version"]
    block_type = BlockType(entry["block_type"])
    text = fixed_text(block_type, version)
    if text is not None:
        return _Source(text)
    if block_type is not BlockType.RETRIEVAL_NOTICE:
        return UnavailableSource("compiler", f"{version}:{block_type}", Reason.NOT_IMPLEMENTED)

    # The notice states what this turn's search found. It is rebuilt from the
    # search's own record and the manifest, never from the memories themselves.
    repo = RetrievalRepository(uow)
    strategies = repo.strategies_for_turn(row["turn_id"])
    if not {PINNED, LEXICAL, RECENCY} <= set(strategies):
        return UnavailableSource("turn_retrieval", str(row["turn_id"]),
                                 Reason.NO_RETRIEVAL_RECORD)
    found = RetrievalInput(
        query_text=strategies[LEXICAL]["query_text"],
        pinned_count=strategies[PINNED]["result_count"],
        lexical_matches=strategies[LEXICAL]["result_count"],
        recency_count=strategies[RECENCY]["result_count"],
        pinned_or_lexical=repo.distinct_results(row["turn_id"], (PINNED, LEXICAL)),
    )
    # Which of those counts as a match, and how the notice words it, is the
    # compiler version's: a turn is rebuilt by the rule it was built under.
    return _Source(retrieval_notice(
        found,
        shown=shown_matches(manifest, version),
        pinned_shown=shown_pinned(manifest),
        compiler_version=version,
    ))


def _message(uow: UnitOfWork, entry: Mapping[str, Any]) -> _Source | UnavailableSource:
    ref = str(entry["source_ref"])
    message_id = _as_uuid(ref)
    if message_id is None:
        return UnavailableSource("message", ref, Reason.NOT_A_ROW)
    message = MessageRepository(uow).get(message_id)
    if message is None:
        return UnavailableSource("message", ref, Reason.MISSING)
    # Only a history block carries a conversational role (spec G.3).
    history = entry["block_type"] == BlockType.CONVERSATION_RECENT
    return _Source(message["content"], message["role"] if history else None)


def _memory(uow: UnitOfWork, entry: Mapping[str, Any]) -> _Source | UnavailableSource:
    """The memory row the entry names, and only that row (spec H.4)."""
    ref = str(entry["source_ref"])
    memory_id = _as_uuid(ref)
    if memory_id is None:
        return UnavailableSource(MANIFEST_SOURCE_KIND, ref, Reason.NOT_A_ROW)
    memory = MemoryRepository(uow).get(memory_id)
    if memory is None:
        return UnavailableSource(MANIFEST_SOURCE_KIND, ref, Reason.MISSING)
    if memory["status"] == Status.TOMBSTONED or memory["content"] is None:
        return UnavailableSource(MANIFEST_SOURCE_KIND, ref, Reason.TOMBSTONED)
    return _Source(memory["content"])


def _as_uuid(ref: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(ref)
    except ValueError:
        return None


__all__ = [
    "BundleStatus",
    "Reason",
    "RenderStatus",
    "Replay",
    "ReplayNotFoundError",
    "ReplaySummary",
    "UnavailableSource",
    "replay_all",
    "replay_invocation",
    "replay_turn",
]
