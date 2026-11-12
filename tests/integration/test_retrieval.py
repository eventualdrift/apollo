"""Retrieval against real PostgreSQL (spec E): scope, strategies, merge, the record.

The substantive-match rule is the point. A memory only counts as support if it
was pinned or matched the question's words above the floor; recent but
unrelated memories never do, so honest absence survives a busy memory store.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from apollo.core.conversations import create_conversation
from apollo.memory import lifecycle
from apollo.memory.models import Kind, Origin, OriginTier, Scope, SourceKind
from apollo.memory.retrieval import (
    LEXICAL,
    PINNED,
    RECENCY,
    RetrievalQuery,
    RetrievalSettings,
    origin_for_mode,
    record,
    retrieve,
)
from apollo.storage.db import Database
from apollo.storage.unit_of_work import unit_of_work

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


def _remember(
    db: Database,
    content: str,
    *,
    subject: str = "general",
    origin: Origin = Origin.PERSONAL,
    pinned: bool = False,
    at: datetime = T0,
) -> uuid.UUID:
    with unit_of_work(db, expect_audit=False) as uow:
        change = lifecycle.create(
            uow, scope=Scope.USER, kind=Kind.PREFERENCE, subject=subject, content=content,
            origin_tier=OriginTier.USER_ASSERTED, origin=origin, pinned=pinned,
            source_kind=SourceKind.USER_DIRECT_ENTRY, now=at,
        )
    return change.memory_id


def _retrieve(db: Database, current: str, *, previous: str | None = None,
              origin: Origin = Origin.PERSONAL, settings: RetrievalSettings | None = None):  # noqa: ANN202
    query = RetrievalQuery(current=current, previous=previous, origin=origin)
    with unit_of_work(db, expect_audit=False) as uow:
        return retrieve(uow, query, settings or RetrievalSettings())


def _ids(outcome, strategy: str | None = None) -> list[uuid.UUID]:  # noqa: ANN001
    if strategy is None:
        return [m.memory_id for m in outcome.merged]
    return [h.memory_id for h in outcome.result(strategy).hits]


# -- scope (spec E.2) -------------------------------------------------------------


def test_mode_decides_the_origin() -> None:
    assert origin_for_mode("personal") is Origin.PERSONAL
    assert origin_for_mode("benchmark") is Origin.FIXTURE


def test_a_benchmark_conversation_never_sees_a_personal_memory(db: Database) -> None:
    """O.1/20, the third privacy mechanism."""
    personal = _remember(db, "Janu prefers tea over coffee", pinned=True)
    fixture = _remember(db, "The fixture user prefers tea over coffee", origin=Origin.FIXTURE)
    benchmark = _retrieve(db, "tea or coffee?", origin=Origin.FIXTURE)
    assert personal not in _ids(benchmark)
    assert fixture in _ids(benchmark)
    private = _retrieve(db, "tea or coffee?")
    assert fixture not in _ids(private)
    assert personal in _ids(private)


def test_only_active_memories_are_retrieved(db: Database) -> None:
    old = _remember(db, "Janu's study group meets on Tuesdays", subject="study group")
    with unit_of_work(db, expect_audit=False) as uow:
        new = lifecycle.correct(uow, old, content="Janu's study group meets on Thursdays",
                                source_kind=SourceKind.USER_DIRECT_ENTRY,
                                now=T0 + timedelta(days=1)).replacement_id
    archived = _remember(db, "Janu's study group used to meet in the library",
                         subject="study group")
    gone = _remember(db, "Janu's study group has a secret handshake", subject="study group")
    with unit_of_work(db, expect_audit=False) as uow:
        lifecycle.archive(uow, archived, now=T0 + timedelta(days=2))
        lifecycle.tombstone(uow, gone, now=T0 + timedelta(days=2))

    outcome = _retrieve(db, "when does my study group meet?")
    found = _ids(outcome)
    assert new in found
    assert old not in found and archived not in found and gone not in found


# -- pinned -------------------------------------------------------------------------


def test_pinned_memories_are_always_included_and_are_support(db: Database) -> None:
    pinned = _remember(db, "Janu's name is pronounced YAH-noo", pinned=True)
    outcome = _retrieve(db, "what's the weather like on Mars?")
    assert _ids(outcome, PINNED) == [pinned]
    assert outcome.merged[0].memory_id == pinned and outcome.merged[0].pinned
    assert outcome.substantive_support


def test_pinned_memories_are_never_truncated(db: Database) -> None:
    pinned = [_remember(db, f"Pinned fact number {i}", pinned=True) for i in range(5)]
    _remember(db, "Janu prefers short answers about fact checking")
    outcome = _retrieve(db, "fact", settings=RetrievalSettings(k=3))
    assert sorted(pinned) == [m.memory_id for m in outcome.merged]


# -- lexical: any word, above the floor ----------------------------------------------


def test_a_natural_question_finds_a_memory_sharing_its_key_word(db: Database) -> None:
    """Every-word matching would miss this: the question and memory share one stem."""
    target = _remember(db, "Janu prefers Apollo not to restate his question before answering.",
                       subject="communication")
    outcome = _retrieve(db, "should I just answer or recap the question first?")
    assert _ids(outcome, LEXICAL)[:1] == [target]
    assert outcome.result(LEXICAL).substantive and outcome.substantive_support


def test_more_shared_words_rank_higher(db: Database) -> None:
    weak = _remember(db, "Janu's favourite colour is green")
    strong = _remember(db, "Janu's favourite programming language is Rust")
    outcome = _retrieve(db, "what is my favourite programming language?")
    assert _ids(outcome, LEXICAL)[:2] == [strong, weak]


def test_the_previous_user_message_is_searched_too(db: Database) -> None:
    target = _remember(db, "Janu's thesis is about distributed consensus")
    outcome = _retrieve(db, "remind me what it was about?", previous="my thesis")
    assert target in _ids(outcome, LEXICAL)
    assert outcome.result(LEXICAL).query_text == "my thesis\nremind me what it was about?"


@pytest.mark.parametrize("text", [
    'the "quoted" -minus OR and & | ! ( ) :* <-> words',
    "'; DROP TABLE memory; --",
    "\\x00 \\\\ backslashes \\",
], ids=["query-syntax", "sql-shaped", "backslashes"])
def test_nothing_typed_is_read_as_query_syntax(db: Database, text: str) -> None:
    _remember(db, "Janu prefers words without punctuation")
    _retrieve(db, text)  # no error; whatever it finds, it found by words


def test_a_question_of_only_stop_words_finds_nothing(db: Database) -> None:
    _remember(db, "Janu prefers tea")
    outcome = _retrieve(db, "is it the one that was?")
    assert _ids(outcome, LEXICAL) == []
    assert not outcome.result(LEXICAL).substantive


def test_the_floor_turns_a_weak_match_into_no_match(db: Database) -> None:
    _remember(db, "Janu went hiking in the mountains last summer with friends from university")
    loose = _retrieve(db, "summer", settings=RetrievalSettings(lexical_floor=0.0))
    strict = _retrieve(db, "summer", settings=RetrievalSettings(lexical_floor=0.99))
    assert _ids(loose, LEXICAL) and loose.substantive_support
    assert _ids(strict, LEXICAL) == [] and not strict.substantive_support


def test_the_simple_configuration_runs_too(db: Database) -> None:
    """Spec E.4: both configurations are evaluated; `simple` does not stem."""
    target = _remember(db, "Janu prefers concise answers")
    english = _retrieve(db, "does he prefer concise answering?")
    simple = _retrieve(db, "does he prefer concise answering?",
                       settings=RetrievalSettings(text_search_config="simple"))
    assert target in _ids(english, LEXICAL) and target in _ids(simple, LEXICAL)
    assert _ids(_retrieve(db, "preferring", settings=RetrievalSettings(
        text_search_config="simple")), LEXICAL) == []
    assert _ids(_retrieve(db, "preferring"), LEXICAL) == [target]


# -- recency: a fallback that never counts ---------------------------------------------


def test_recency_returns_the_newest_and_is_never_support(db: Database) -> None:
    """O.1/8: unrelated recent memories cannot mask absence."""
    ids = [_remember(db, f"Unrelated note {i} about gardening", at=T0 + timedelta(hours=i))
           for i in range(5)]
    outcome = _retrieve(db, "what did I score on the networks exam?")
    assert _ids(outcome, RECENCY) == [ids[4], ids[3], ids[2]]
    assert not outcome.result(RECENCY).substantive
    assert not outcome.substantive_support
    assert _ids(outcome) == [ids[4], ids[3], ids[2]]  # in context, but not as support


# -- the record (spec C.5, C.6) ----------------------------------------------------------


def test_every_strategy_is_recorded_including_empty_ones(db: Database, service, clock) -> None:
    result = service.submit(conversation_id=create_conversation(db, now=clock()), text="hi")
    found = _remember(db, "Janu prefers tea over coffee")
    outcome = _retrieve(db, "tea please")
    with unit_of_work(db, expect_audit=False) as uow:
        record(uow, turn_id=result.turn_id, outcome=outcome,
               included=frozenset({found}), now=clock())

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM turn_retrieval WHERE turn_id = %s ORDER BY strategy",
                    (result.turn_id,))
        rows = {r["strategy"]: r for r in cur.fetchall()}
        cur.execute("SELECT r.strategy, x.memory_id, x.rank, x.included_in_context"
                    " FROM turn_retrieval_result x JOIN turn_retrieval r"
                    " ON r.id = x.turn_retrieval_id WHERE r.turn_id = %s", (result.turn_id,))
        results = cur.fetchall()

    assert set(rows) == {PINNED, LEXICAL, RECENCY}
    assert rows[PINNED]["result_count"] == 0 and not rows[PINNED]["substantive"]
    assert rows[LEXICAL]["result_count"] == 1 and rows[LEXICAL]["substantive"]
    assert rows[LEXICAL]["query_text"] == "tea please"
    assert rows[RECENCY]["result_count"] == 1 and not rows[RECENCY]["substantive"]
    assert {(r["strategy"], r["memory_id"], r["rank"], r["included_in_context"])
            for r in results} == {(LEXICAL, found, 1, True), (RECENCY, found, 1, True)}


def test_a_long_previous_message_cannot_crowd_out_the_current_question(db: Database) -> None:
    """The word budget is spent on the current question first, then the previous message."""
    target = _remember(db, "Janu's bicycle is a blue Trek mountain bike")
    long_previous = " ".join(f"filler{i}" for i in range(100))
    outcome = _retrieve(db, "what colour is my bicycle?", previous=long_previous)
    assert target in _ids(outcome, LEXICAL)
