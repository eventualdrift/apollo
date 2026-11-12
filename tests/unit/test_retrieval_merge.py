"""The merge and the substantive-match rule, without a database (spec E.3)."""

from __future__ import annotations

import uuid

from apollo.memory.retrieval import (
    LEXICAL,
    PINNED,
    RECENCY,
    Hit,
    RetrievalSettings,
    StrategyResult,
    merge,
    substantive_support,
)
from apollo.memory.retrieval.lexical import any_word_query


def _id(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def _result(strategy: str, *scored: tuple[int, float]) -> StrategyResult:
    hits = tuple(Hit(_id(n), score, "s", f"memory {n}") for n, score in scored)
    return StrategyResult(strategy, None, hits, strategy != RECENCY and bool(hits), 0)


def test_weights_dedupe_keep_the_maximum() -> None:
    merged = merge((
        _result(LEXICAL, (1, 0.2)),               # the best lexical hit: 1.0
        _result(RECENCY, (1, 1.0), (2, 1.0)),     # 0.3 each after weighting
    ), RetrievalSettings())
    assert [(m.memory_id, round(m.score, 3)) for m in merged] == [(_id(1), 1.0), (_id(2), 0.3)]
    assert merged[0].strategies == (LEXICAL, RECENCY)


def test_a_genuine_match_outranks_an_unrelated_recent_memory() -> None:
    """Lexical scores are small absolute values; unnormalised, recency's 0.3 won."""
    merged = merge((
        _result(LEXICAL, (5, 0.04), (6, 0.02)),
        _result(RECENCY, (7, 1.0), (8, 0.67), (9, 0.33)),
    ), RetrievalSettings())
    assert [m.memory_id for m in merged][:2] == [_id(5), _id(6)]
    assert [round(m.score, 3) for m in merged] == [1.0, 0.5, 0.3, 0.201, 0.099]


def test_ties_break_by_memory_id_ascending() -> None:
    merged = merge((_result(LEXICAL, (9, 0.5), (3, 0.5), (5, 0.5)),), RetrievalSettings())
    assert [m.memory_id for m in merged] == [_id(3), _id(5), _id(9)]


def test_pinned_lead_and_truncation_is_to_k_overall() -> None:
    merged = merge((
        _result(PINNED, (7, 1.0)),
        _result(LEXICAL, *[(n, 1 - n / 100) for n in range(1, 20)]),
    ), RetrievalSettings(k=4))
    assert [m.memory_id for m in merged] == [_id(7), _id(1), _id(2), _id(3)]
    assert merged[0].pinned and not merged[1].pinned


def test_a_pinned_memory_also_found_lexically_appears_once() -> None:
    merged = merge((_result(PINNED, (1, 1.0)), _result(LEXICAL, (1, 0.9))), RetrievalSettings())
    assert [m.memory_id for m in merged] == [_id(1)]
    assert merged[0].pinned and merged[0].strategies == (PINNED, LEXICAL)


def test_recency_alone_is_never_support() -> None:
    assert not substantive_support((
        _result(PINNED), _result(LEXICAL), _result(RECENCY, (1, 1.0), (2, 0.5)),
    ))
    assert substantive_support((_result(PINNED, (1, 1.0)), _result(LEXICAL)))
    assert substantive_support((_result(PINNED), _result(LEXICAL, (1, 0.1))))


def test_the_any_word_query_is_plain_words_joined_by_or() -> None:
    assert any_word_query('Should I "recap" -first OR answer?', 64) == (
        "should or i or recap or first or answer"
    )
    assert any_word_query("tea tea Tea", 64) == "tea"
    assert any_word_query("!!! ???", 64) == ""
    assert any_word_query("a b c d e", 3) == "a or b or c"


def test_a_retrieval_error_is_classified_as_retrieval_failed() -> None:
    from apollo.errors import ErrorKind
    from apollo.memory.retrieval import RetrievalError, origin_for_mode
    from apollo.sanitise import error_kind

    try:
        origin_for_mode("unknown")
    except RetrievalError as exc:
        assert exc.kind is ErrorKind.RETRIEVAL_FAILED
        assert error_kind(exc) is ErrorKind.RETRIEVAL_FAILED
    else:
        raise AssertionError("an unknown mode must be refused")


def test_the_current_question_is_searched_first() -> None:
    from apollo.memory.models import Origin
    from apollo.memory.retrieval import RetrievalQuery
    from apollo.memory.retrieval.lexical import query_terms

    query = RetrievalQuery(current="bicycle colour", previous="a b c d", origin=Origin.PERSONAL)
    assert query_terms(query, 3) == "bicycle or colour or a"
