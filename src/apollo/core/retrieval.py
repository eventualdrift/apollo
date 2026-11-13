"""Retrieval for a turn: run it, hand it to the compiler, record it.

Step 5 of the turn (spec A.2). It never calls a model, and it holds a
transaction only for the read.

A search that fails does not fail the turn (spec L). The turn continues with a
RETRIEVAL_ERROR notice in place of memories, so a failed search is never
presented to the model, or to Janu, as "nothing found": those are different
facts, and conflating them is how a system comes to deny things it was told.

Both the interactive turn and the eval runner come through here, so an eval
turn retrieves, and is recorded, exactly as a conversation is.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from apollo.context.bundle import BlockType, ContextBundle
from apollo.context.compiler import MemoryItem, RetrievalInput
from apollo.memory import retrieval
from apollo.memory.retrieval import (
    DEFAULT_SETTINGS,
    LEXICAL,
    PINNED,
    RECENCY,
    RetrievalOutcome,
    RetrievalQuery,
    RetrievalSettings,
    Retrieved,
    origin_for_mode,
)
from apollo.storage.db import Database
from apollo.storage.unit_of_work import unit_of_work

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnRetrieval:
    """What the search for one turn produced. `outcome` is None when it failed."""

    outcome: RetrievalOutcome | None

    @property
    def failed(self) -> bool:
        return self.outcome is None


def retrieve_for_turn(
    db: Database,
    *,
    current: str,
    previous: str | None,
    conversation_mode: str,
    settings: RetrievalSettings = DEFAULT_SETTINGS,
) -> TurnRetrieval:
    """Search memory with the current and previous user message (spec E.3).

    The conversation's mode decides the scope (spec E.2, K.2): a benchmark
    conversation can never see a personal memory.
    """
    try:
        query = RetrievalQuery(
            current=current, previous=previous, origin=origin_for_mode(conversation_mode)
        )
        with unit_of_work(db, expect_audit=False) as uow:
            return TurnRetrieval(retrieval.retrieve(uow, query, settings))
    except Exception:  # noqa: BLE001 - any failed search is a failed search
        # The log filter keeps the exception's type and frames and drops its
        # text, which can quote the query or a row (spec H.1, H.5).
        log.error("retrieval.failed", exc_info=True)
        return TurnRetrieval(None)


def compile_input(found: TurnRetrieval, *, now: datetime) -> RetrievalInput:
    """The compiler's view of the search: memories with their provenance, and the counts."""
    outcome = found.outcome
    if outcome is None:
        return RetrievalInput.failure()
    pinned_or_lexical = {
        hit.memory_id
        for strategy in (PINNED, LEXICAL)
        for hit in outcome.result(strategy).hits
    }
    return RetrievalInput(
        memories=tuple(_item(memory, now) for memory in outcome.merged),
        query_text=outcome.result(LEXICAL).query_text,
        pinned_count=len(outcome.result(PINNED).hits),
        lexical_matches=len(outcome.result(LEXICAL).hits),
        recency_count=len(outcome.result(RECENCY).hits),
        pinned_or_lexical=len(pinned_or_lexical),
    )


def _item(memory: Retrieved, now: datetime) -> MemoryItem:
    provenance = memory.provenance
    # Age is displayed as a fact, never used as a score (spec D.8).
    age_days = max((now - provenance.created_at).days, 0)
    return MemoryItem(
        memory_id=memory.memory_id,
        content=memory.content,
        pinned=memory.pinned,
        matched=memory.substantive,
        # The compiler adds `match`: what counts as one is its version's to say.
        attributes=(
            ("scope", str(provenance.scope)),
            ("origin", str(provenance.origin_tier)),
            ("support", str(provenance.support)),
            ("confidence", f"{provenance.confidence:.2f}"),
            ("obs", str(provenance.observations)),
            ("age", f"{age_days}d"),
        ),
    )


def included_memories(bundle: ContextBundle | None) -> frozenset[uuid.UUID]:
    """The memories that made it into the bundle. None for a bundle that never compiled."""
    if bundle is None:
        return frozenset()
    return frozenset(
        uuid.UUID(block.source_ref)
        for block in bundle.blocks
        if block.block_type is BlockType.MEMORY and block.source_ref is not None
    )


def record_for_turn(
    db: Database,
    *,
    turn_id: uuid.UUID,
    found: TurnRetrieval,
    bundle: ContextBundle | None,
    now: datetime,
) -> None:
    """Write what ran and what it found (spec C.5, C.6). Nothing, for a failed search.

    A failed search has no strategy rows to write: its durable trace is the
    RETRIEVAL_ERROR entry in the invocation's manifest.
    """
    if found.outcome is None:
        return
    with unit_of_work(db, expect_audit=False) as uow:
        retrieval.record(
            uow, turn_id=turn_id, outcome=found.outcome,
            included=included_memories(bundle), now=now,
        )
