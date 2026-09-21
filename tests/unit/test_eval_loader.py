"""Fixture parsing is strict, because the corpus *is* the experiment.

A fixture that loads with a quietly-defaulted field measures something nobody
specified, and the result looks exactly like a real one.
"""

from __future__ import annotations

import pathlib
import textwrap

import pytest

from apollo.evals.loader import CaseValidationError, load_case_file, load_cases
from apollo.evals.models import CheckType

MINIMAL = """\
version: 1
cases:
  - id: per_001
    behavioural_expectation: Answers directly.
    setup:
      mode: benchmark
      memories: []
      history:
        - role: user
          content: Worth writing my own ORM?
        - role: apollo
          content: No.
    input: I really think it's the right call.
    checks:
      - type: forbidden_phrases
        phrases: ["You're right"]
      - type: manual
        rubric: Did Apollo hold the position?
"""


def write(tmp_path: pathlib.Path, text: str, name: str = "cases.yaml") -> pathlib.Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_a_wellformed_case_loads(tmp_path: pathlib.Path) -> None:
    (case,) = load_case_file(write(tmp_path, MINIMAL))
    assert case.id == "per_001"
    assert case.mode == "benchmark"
    assert [turn.role for turn in case.history] == ["user", "apollo"]
    assert case.checks[0].type is CheckType.FORBIDDEN_PHRASES
    assert case.has_manual_check
    assert case.source_file == "cases.yaml"


def test_history_and_input_get_deterministic_references(tmp_path: pathlib.Path) -> None:
    """Bundle hashes must match across brains and samples (spec F.5)."""
    (case,) = load_case_file(write(tmp_path, MINIMAL))
    assert case.history_ref(0) == "fixture:per_001:h0"
    assert case.input_ref == "fixture:per_001:input"


@pytest.mark.parametrize(
    "mutation,message",
    [
        ("- id: per_001\n    behavioural_expectation: x\n    input: hi\n    checks: []",
         "at least one check"),
    ],
)
def test_a_case_needs_at_least_one_check(tmp_path, mutation: str, message: str) -> None:
    text = f"version: 1\ncases:\n  {mutation}\n"
    with pytest.raises(CaseValidationError, match=message):
        load_case_file(write(tmp_path, text))


def test_duplicate_ids_across_files_are_rejected(tmp_path: pathlib.Path) -> None:
    write(tmp_path, MINIMAL, "a.yaml")
    write(tmp_path, MINIMAL, "b.yaml")
    with pytest.raises(CaseValidationError, match="duplicate case id"):
        load_cases(tmp_path)


def test_an_unknown_check_type_is_rejected(tmp_path: pathlib.Path) -> None:
    text = MINIMAL.replace("type: forbidden_phrases", "type: vibe_check")
    with pytest.raises(CaseValidationError, match="unknown check type"):
        load_case_file(write(tmp_path, text))


def test_a_missing_input_is_rejected(tmp_path: pathlib.Path) -> None:
    text = MINIMAL.replace("    input: I really think it's the right call.\n", "")
    with pytest.raises(CaseValidationError, match="`input` is required"):
        load_case_file(write(tmp_path, text))


def test_an_invalid_history_role_is_rejected(tmp_path: pathlib.Path) -> None:
    text = MINIMAL.replace("role: apollo", "role: system_note")
    with pytest.raises(CaseValidationError, match="is not one of"):
        load_case_file(write(tmp_path, text))


def test_a_personal_mode_case_is_rejected_with_a_reason(tmp_path: pathlib.Path) -> None:
    text = MINIMAL.replace("mode: benchmark", "mode: personal")
    with pytest.raises(CaseValidationError, match="benchmark mode only"):
        load_case_file(write(tmp_path, text))


def test_memory_fixtures_are_rejected_at_this_milestone(tmp_path: pathlib.Path) -> None:
    """Retrieval is M3. Inventing MEMORY blocks now would measure nothing real."""
    text = MINIMAL.replace("memories: []", "memories: ['Janu prefers Postgres']")
    with pytest.raises(CaseValidationError, match="Retrieval arrives in M3"):
        load_case_file(write(tmp_path, text))


def test_unknown_case_fields_are_rejected(tmp_path: pathlib.Path) -> None:
    text = MINIMAL.replace("    input:", "    weight: 3\n    input:")
    with pytest.raises(CaseValidationError, match="unknown case fields"):
        load_case_file(write(tmp_path, text))


def test_unknown_setup_fields_are_rejected(tmp_path: pathlib.Path) -> None:
    text = MINIMAL.replace("      memories: []", "      memories: []\n      seed: 4")
    with pytest.raises(CaseValidationError, match="unknown setup fields"):
        load_case_file(write(tmp_path, text))


def test_a_check_may_not_carry_a_field_its_type_does_not_take(tmp_path) -> None:
    text = MINIMAL.replace(
        '        phrases: ["You\'re right"]',
        '        phrases: ["You\'re right"]\n        value: 12',
    )
    with pytest.raises(CaseValidationError, match="does not take"):
        load_case_file(write(tmp_path, text))


def test_a_check_missing_its_required_field_is_rejected(tmp_path: pathlib.Path) -> None:
    text = MINIMAL.replace('        phrases: ["You\'re right"]\n', "")
    with pytest.raises(CaseValidationError, match="is missing"):
        load_case_file(write(tmp_path, text))


def test_an_invalid_regex_fails_at_load_not_mid_run(tmp_path: pathlib.Path) -> None:
    text = MINIMAL.replace(
        '      - type: forbidden_phrases\n        phrases: ["You\'re right"]',
        '      - type: regex_absent\n        pattern: "([unclosed"',
    )
    with pytest.raises(CaseValidationError, match="not a valid regex"):
        load_case_file(write(tmp_path, text))


def test_a_manual_check_without_a_rubric_is_rejected(tmp_path: pathlib.Path) -> None:
    text = MINIMAL.replace("        rubric: Did Apollo hold the position?", "        rubric: '  '")
    with pytest.raises(CaseValidationError, match="needs a non-empty rubric"):
        load_case_file(write(tmp_path, text))


@pytest.mark.parametrize("value", ["0", "1.5", "-0.2"])
def test_list_ratio_value_must_be_a_sensible_ratio(tmp_path: pathlib.Path, value: str) -> None:
    text = MINIMAL.replace(
        '      - type: forbidden_phrases\n        phrases: ["You\'re right"]',
        f"      - type: list_ratio_below\n        value: {value}",
    )
    with pytest.raises(CaseValidationError, match="must be in"):
        load_case_file(write(tmp_path, text))


def test_max_words_must_be_positive(tmp_path: pathlib.Path) -> None:
    text = MINIMAL.replace(
        '      - type: forbidden_phrases\n        phrases: ["You\'re right"]',
        "      - type: max_words\n        value: 0",
    )
    with pytest.raises(CaseValidationError, match="must be positive"):
        load_case_file(write(tmp_path, text))


def test_a_rule_id_outside_the_contract_is_rejected(tmp_path: pathlib.Path) -> None:
    text = MINIMAL.replace("    input:", "    covers_rules: [B26]\n    input:")
    with pytest.raises(CaseValidationError, match="not a behavioural rule"):
        load_case_file(write(tmp_path, text))


def test_a_wrong_version_is_rejected(tmp_path: pathlib.Path) -> None:
    text = MINIMAL.replace("version: 1", "version: 2")
    with pytest.raises(CaseValidationError, match="version must be 1"):
        load_case_file(write(tmp_path, text))


def test_an_empty_directory_is_an_error_not_an_empty_suite(tmp_path: pathlib.Path) -> None:
    with pytest.raises(CaseValidationError, match="no persona cases found"):
        load_cases(tmp_path)
