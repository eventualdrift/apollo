"""The merge and the substantive-match rule, without a database (spec E.3)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from apollo.memory.models import ObservationCounts, OriginTier, Scope, Support
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
from apollo.memory.retrieval.base import Provenance
from apollo.memory.retrieval.lexical import any_word_query


def _id(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def _provenance(confirms: int = 0, contradicts: int = 0, *, day: int = 1) -> Provenance:
    return Provenance(
        scope=Scope.USER, origin_tier=OriginTier.USER_ASSERTED,
        created_at=datetime(2026, 11, day, tzinfo=UTC), observations=1 + confirms + contradicts,
        counts=ObservationCounts(confirms, contradicts),
    )


def _result(strategy: str, *scored: tuple[int, float]) -> StrategyResult:
    hits = tuple(Hit(_id(n), score, "s", f"memory {n}", _provenance()) for n, score in scored)
    # Only a lexical match is support (spec E.6/2): pinned and recency never are.
    return StrategyResult(strategy, None, hits, strategy == LEXICAL and bool(hits), 0)


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


def test_what_is_still_tied_breaks_by_memory_id_ascending() -> None:
    """Same score, same confidence, made at the same instant: the order is still total."""
    merged = merge((_result(LEXICAL, (9, 0.5), (3, 0.5), (5, 0.5)),), RetrievalSettings())
    assert [m.memory_id for m in merged] == [_id(3), _id(5), _id(9)]


def test_of_two_equal_matches_the_newer_memory_leads() -> None:
    """Spec E.6/1 (Janu, 2026-11-13): "my gym routine" in September, and since November.

    Both match the question equally. The older one has the lower id, and used
    to lead for that reason alone.
    """
    def hit(n: int, day: int) -> Hit:
        return Hit(_id(n), 0.5, "s", f"memory {n}", _provenance(day=day))

    merged = merge((
        StrategyResult(LEXICAL, "q", (hit(1, day=2), hit(2, day=20), hit(3, day=9)), True, 0),
    ), RetrievalSettings())
    assert [m.memory_id for m in merged] == [_id(2), _id(3), _id(1)]


def test_a_better_attested_memory_still_leads_a_newer_one() -> None:
    """Spec D.8: confidence is the first tiebreaker. Newness decides only what it leaves tied."""
    old_confirmed = Hit(_id(1), 0.5, "s", "memory 1", _provenance(confirms=2, day=2))
    new_asserted = Hit(_id(2), 0.5, "s", "memory 2", _provenance(day=20))
    merged = merge((
        StrategyResult(LEXICAL, "q", (new_asserted, old_confirmed), True, 0),
    ), RetrievalSettings())
    assert [m.memory_id for m in merged] == [_id(1), _id(2)]


def test_a_stronger_match_still_leads_a_newer_one() -> None:
    older_better = Hit(_id(1), 0.6, "s", "memory 1", _provenance(day=2))
    newer_weaker = Hit(_id(2), 0.5, "s", "memory 2", _provenance(day=20))
    merged = merge((
        StrategyResult(LEXICAL, "q", (newer_weaker, older_better), True, 0),
    ), RetrievalSettings())
    assert [m.memory_id for m in merged] == [_id(1), _id(2)]


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
    assert substantive_support((_result(PINNED), _result(LEXICAL, (1, 0.1))))


def test_a_pinned_memory_is_support_only_when_it_also_matches() -> None:
    """Spec E.6/2 (Janu, 2026-11-13). Pinned means always present, not always relevant.

    While any memory was pinned, every question had "support", so the notice
    that nothing matched was never given, whatever was asked.
    """
    pinned_only = (_result(PINNED, (1, 1.0)), _result(LEXICAL), _result(RECENCY))
    assert not substantive_support(pinned_only)
    assert [m.memory_id for m in merge(pinned_only, RetrievalSettings())] == [_id(1)]  # present

    also_matched = (_result(PINNED, (1, 1.0)), _result(LEXICAL, (1, 0.2)), _result(RECENCY))
    assert substantive_support(also_matched)
    # Even a pinned result wrongly flagged as support does not count: the rule
    # reads the lexical result alone.
    flagged = StrategyResult(PINNED, None, _result(PINNED, (1, 1.0)).hits, True, 0)
    assert not substantive_support((flagged, _result(LEXICAL), _result(RECENCY)))


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


def test_equal_scores_are_ordered_by_confidence_before_memory_id() -> None:
    """Spec D.8, ADR-0005: derived confidence is the ranking tiebreaker."""
    def hit(n: int, **counts: int) -> Hit:
        return Hit(_id(n), 0.5, "s", f"memory {n}", _provenance(**counts))

    merged = merge((
        StrategyResult(LEXICAL, "q", (hit(1), hit(2, confirms=2), hit(3, contradicts=1)), True, 0),
    ), RetrievalSettings())
    # All three normalise to 1.0. Confirmed (0.80) leads, asserted (0.70) next,
    # contested (0.45) last; the older id no longer wins by default.
    assert [m.memory_id for m in merged] == [_id(2), _id(1), _id(3)]
    assert [m.provenance.support for m in merged] == [
        Support.CONFIRMED, Support.ASSERTED, Support.CONTESTED,
    ]
    assert [m.provenance.confidence for m in merged] == [0.8, 0.7, 0.45]


def test_a_memory_is_labelled_by_whether_it_matched_this_message() -> None:
    """A match is a lexical match. Pinned says only why an unmatched memory is present."""
    merged = merge((
        _result(PINNED, (1, 1.0), (4, 1.0)),
        _result(LEXICAL, (1, 0.4), (2, 0.2)),
        _result(RECENCY, (2, 1.0), (3, 0.5)),
    ), RetrievalSettings())
    assert [(m.memory_id, m.pinned, m.match, m.substantive) for m in merged] == [
        (_id(1), True, LEXICAL, True),      # pinned, and it matched
        (_id(4), True, PINNED, False),      # pinned, and it did not
        (_id(2), False, LEXICAL, True),
        (_id(3), False, RECENCY, False),
    ]
