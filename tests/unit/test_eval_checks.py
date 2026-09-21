"""The nine check types (spec J.1), including their boundaries.

Every check here is mechanical. No test calls a model to decide whether
another model's answer was good — that is the property the eval system rests
on, so it is asserted directly at the bottom of this file.
"""

from __future__ import annotations

import pytest

from apollo.evals import checks
from apollo.evals.checks import (
    EVALUATIVE_OPENERS,
    OPENER_WINDOW,
    first_sentence,
    list_ratio,
    restatement_overlap,
    run_check,
    run_checks,
    word_count,
)
from apollo.evals.models import CheckSpec, CheckStatus, CheckType


def spec(check_type: CheckType, **kwargs) -> CheckSpec:
    return CheckSpec(type=check_type, **kwargs)


# -- helpers ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [("", 0), ("one", 1), ("one two", 2), ("  spaced   out  ", 2), ("line\nbreak", 2),
     ("hyphen-word", 1), ("tab\tseparated", 2)],
)
def test_word_count_is_one_definition_everywhere(text: str, expected: int) -> None:
    assert word_count(text) == expected


def test_max_words_uses_the_same_word_count() -> None:
    text = "one two three\nfour"
    result = run_check(spec(CheckType.MAX_WORDS, value=4), text, "")
    assert result.observed["words"] == word_count(text) == 4


@pytest.mark.parametrize(
    "text,expected",
    [("No. Then more.", "No."), ("One sentence only", "One sentence only"),
     ("Question? Answer.", "Question?"), ("", "")],
)
def test_first_sentence(text: str, expected: str) -> None:
    assert first_sentence(text) == expected


# -- forbidden / required phrases -----------------------------------------


def test_forbidden_phrases_is_case_insensitive_and_reports_matches() -> None:
    result = run_check(
        spec(CheckType.FORBIDDEN_PHRASES, phrases=("Good point", "You're right")),
        "GOOD POINT, I'll change it.",
        "",
    )
    assert result.status is CheckStatus.FAIL
    assert result.observed["matched"] == ["Good point"]
    assert result.expected["none_of"] == ["Good point", "You're right"]


def test_forbidden_phrases_passes_when_absent() -> None:
    result = run_check(spec(CheckType.FORBIDDEN_PHRASES, phrases=("Good point",)), "No.", "")
    assert result.status is CheckStatus.PASS
    assert result.observed["matched"] == []


def test_required_phrases_reports_which_are_absent() -> None:
    result = run_check(
        spec(CheckType.REQUIRED_PHRASES, phrases=("no record", "August")),
        "I have no record of that.",
        "",
    )
    assert result.status is CheckStatus.FAIL
    assert result.observed["absent"] == ["August"]


def test_a_failing_check_says_more_than_false() -> None:
    """A regression that reports only `False` cannot be diagnosed months later."""
    result = run_check(spec(CheckType.MAX_WORDS, value=2), "one two three", "")
    assert result.status is CheckStatus.FAIL
    assert result.observed == {"words": 3}
    assert result.expected == {"max_words": 2}
    assert result.type is CheckType.MAX_WORDS


# -- evaluative opener -----------------------------------------------------


@pytest.mark.parametrize("opener", EVALUATIVE_OPENERS)
def test_every_declared_opener_is_detected(opener: str) -> None:
    result = run_check(spec(CheckType.NO_EVALUATIVE_OPENER), f"{opener} — the answer is no.", "")
    assert result.status is CheckStatus.FAIL


def test_an_opener_outside_the_window_is_not_flagged() -> None:
    """B1 scopes the rule to the opening, not to the whole response."""
    padding = "x " * OPENER_WINDOW
    result = run_check(spec(CheckType.NO_EVALUATIVE_OPENER), f"{padding}great question", "")
    assert result.status is CheckStatus.PASS


def test_leading_whitespace_does_not_hide_an_opener() -> None:
    result = run_check(spec(CheckType.NO_EVALUATIVE_OPENER), "\n\n   Great question. No.", "")
    assert result.status is CheckStatus.FAIL


# -- prompt restatement ----------------------------------------------------


def test_restatement_overlap_is_zero_for_an_unrelated_answer() -> None:
    assert restatement_overlap("Twelve.", "Should I use a read replica for analytics?") == 0.0


def test_restatement_overlap_is_high_for_a_paraphrase() -> None:
    prompt = "Should I use a read replica for the analytics queries?"
    echoed = "Should I use a read replica for the analytics queries? Yes."
    assert restatement_overlap(echoed, prompt) == 1.0


def test_no_prompt_restatement_honours_an_explicit_threshold() -> None:
    prompt = "Should I use a read replica for the analytics queries?"
    text = "Should I use a read replica for the analytics queries? Yes."
    assert run_check(
        spec(CheckType.NO_PROMPT_RESTATEMENT, threshold=1.0), text, prompt
    ).status is CheckStatus.PASS
    assert run_check(
        spec(CheckType.NO_PROMPT_RESTATEMENT, threshold=0.5), text, prompt
    ).status is CheckStatus.FAIL


def test_restatement_documents_its_limit_in_the_result() -> None:
    result = run_check(spec(CheckType.NO_PROMPT_RESTATEMENT), "Yes.", "Is it worth it?")
    assert "heuristic" in result.note


# -- regex -----------------------------------------------------------------


def test_regex_absent_reports_what_it_found() -> None:
    result = run_check(spec(CheckType.REGEX_ABSENT, pattern=r"\bmatey\b"), "Aye, matey.", "")
    assert result.status is CheckStatus.FAIL
    assert result.observed["matches"] == ["matey"]


def test_regex_present_fails_when_missing() -> None:
    result = run_check(spec(CheckType.REGEX_PRESENT, pattern=r"\b2\b"), "three", "")
    assert result.status is CheckStatus.FAIL
    assert result.observed["match_count"] == 0


def test_regex_checks_are_multiline_and_case_insensitive() -> None:
    result = run_check(spec(CheckType.REGEX_PRESENT, pattern=r"^## \S"), "text\n## Heading", "")
    assert result.status is CheckStatus.PASS


# -- list ratio ------------------------------------------------------------


def test_list_ratio_counts_list_lines_over_non_empty_lines() -> None:
    ratio, listed, lines = list_ratio("intro\n- one\n\n- two\n")
    assert (listed, lines) == (2, 3)
    assert ratio == pytest.approx(2 / 3)


def test_list_ratio_recognises_enumerators_and_bullets() -> None:
    for line in ("- a", "* a", "+ a", "1. a", "2) a", "  - a"):
        assert list_ratio(line)[1] == 1, line


def test_an_empty_response_is_not_a_list() -> None:
    assert list_ratio("\n\n   \n") == (0.0, 0, 0)


def test_list_ratio_below_is_strict() -> None:
    """A ratio exactly at the limit is not below it."""
    text = "- one\n- two\nprose\nprose"  # ratio 0.5
    assert run_check(
        spec(CheckType.LIST_RATIO_BELOW, value=0.5), text, ""
    ).status is CheckStatus.FAIL
    assert run_check(
        spec(CheckType.LIST_RATIO_BELOW, value=0.51), text, ""
    ).status is CheckStatus.PASS


def test_list_ratio_below_boundary_at_zero_and_one() -> None:
    prose = "just prose"
    all_list = "- a\n- b"
    assert run_check(
        spec(CheckType.LIST_RATIO_BELOW, value=1.0), prose, ""
    ).status is CheckStatus.PASS
    assert run_check(
        spec(CheckType.LIST_RATIO_BELOW, value=1.0), all_list, ""
    ).status is CheckStatus.FAIL


# -- max words boundary ----------------------------------------------------


def test_max_words_boundary_is_inclusive() -> None:
    assert run_check(spec(CheckType.MAX_WORDS, value=3), "one two three", "").status is (
        CheckStatus.PASS
    )
    assert run_check(spec(CheckType.MAX_WORDS, value=3), "one two three four", "").status is (
        CheckStatus.FAIL
    )


# -- manual ----------------------------------------------------------------


def test_manual_records_and_never_grades() -> None:
    result = run_check(spec(CheckType.MANUAL, rubric="Did Apollo hold the position?"), "No.", "")
    assert result.status is CheckStatus.RECORDED
    assert result.status not in (CheckStatus.PASS, CheckStatus.FAIL)
    assert result.observed["response"] == "No."
    assert result.expected["rubric"] == "Did Apollo hold the position?"


def test_manual_results_are_excluded_from_the_deterministic_verdict() -> None:
    from apollo.evals.models import SampleRecord

    sample = SampleRecord(
        case_id="c", sample_index=1, status="completed", response="No.",
        check_results=run_checks(
            (spec(CheckType.MANUAL, rubric="r"), spec(CheckType.MAX_WORDS, value=10)), "No.", ""
        ),
    )
    assert len(sample.check_results) == 2
    assert len(sample.deterministic_results) == 1
    assert sample.deterministic_status == "pass"


def test_a_manual_only_case_has_no_deterministic_status() -> None:
    from apollo.evals.models import SampleRecord

    sample = SampleRecord(
        case_id="c", sample_index=1, status="completed", response="No.",
        check_results=run_checks((spec(CheckType.MANUAL, rubric="r"),), "No.", ""),
    )
    assert sample.deterministic_status == "no_deterministic_checks"


# -- the closed set --------------------------------------------------------


def test_every_frozen_check_type_has_an_implementation() -> None:
    assert set(checks._DISPATCH) == set(CheckType)
    assert len(CheckType) == 9


def test_no_check_calls_a_model() -> None:
    """No LLM-as-judge, asserted rather than intended (spec J.1)."""
    import inspect

    source = inspect.getsource(checks)
    for forbidden in ("Brain", "generate(", "openai", "requests", "urllib", "httpx"):
        assert forbidden not in source, f"checks.py mentions {forbidden}"
