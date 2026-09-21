"""Corpus coverage, proved rather than counted by hand (brief §37).

These tests read the real corpus. If a rule or a probe loses its case, the
suite fails here instead of silently measuring less than it claims to.
"""

from __future__ import annotations

import pathlib

import pytest

from apollo.config import MODE_BENCHMARK
from apollo.evals.corpus import (
    REQUIRED_INVERSE_TAGS,
    REQUIRED_PROBES,
    REQUIRED_RULES,
    coverage,
)
from apollo.evals.loader import VALID_HISTORY_ROLES, load_cases
from apollo.evals.models import CheckType, PersonaCase

CASES_DIR = pathlib.Path(__file__).resolve().parents[2] / "evals" / "persona" / "cases"


@pytest.fixture(scope="module")
def cases() -> list[PersonaCase]:
    return load_cases(CASES_DIR)


def test_the_real_corpus_loads(cases: list[PersonaCase]) -> None:
    assert len(cases) >= 25


def test_every_behavioural_rule_is_covered(cases: list[PersonaCase]) -> None:
    report = coverage(cases)
    assert report.missing_rules == [], f"rules with no case: {report.missing_rules}"
    assert len(REQUIRED_RULES) == 25


def test_every_required_probe_is_covered(cases: list[PersonaCase]) -> None:
    report = coverage(cases)
    assert report.missing_probes == [], f"probes with no case: {report.missing_probes}"
    assert len(REQUIRED_PROBES) == 16


def test_inverse_cases_exist_for_each_overcorrection(cases: list[PersonaCase]) -> None:
    """A suite that only punishes the fault trains the opposite fault."""
    report = coverage(cases)
    assert report.missing_inverse == [], f"missing inverse cases: {report.missing_inverse}"
    assert len(REQUIRED_INVERSE_TAGS) == 4


def test_the_corpus_reports_complete(cases: list[PersonaCase]) -> None:
    report = coverage(cases)
    assert report.problems == []
    assert report.complete


def test_case_ids_are_unique(cases: list[PersonaCase]) -> None:
    ids = [case.id for case in cases]
    assert len(ids) == len(set(ids))


def test_every_case_is_benchmark_mode(cases: list[PersonaCase]) -> None:
    assert {case.mode for case in cases} == {MODE_BENCHMARK}


def test_no_case_carries_a_memory_fixture(cases: list[PersonaCase]) -> None:
    """Retrieval is M3. Nothing in this corpus may pretend otherwise."""
    for path in sorted(CASES_DIR.glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("memories:"):
                assert stripped == "memories: []", f"{path.name}: {stripped}"


def test_history_roles_are_valid(cases: list[PersonaCase]) -> None:
    roles = {turn.role for case in cases for turn in case.history}
    assert roles <= set(VALID_HISTORY_ROLES)


def test_all_check_types_are_known_and_configured(cases: list[PersonaCase]) -> None:
    for case in cases:
        for check in case.checks:
            assert isinstance(check.type, CheckType)
            if check.type is CheckType.MANUAL:
                assert (check.rubric or "").strip()
            if check.type in (CheckType.REGEX_ABSENT, CheckType.REGEX_PRESENT):
                assert check.pattern
            if check.type in (CheckType.MAX_WORDS, CheckType.LIST_RATIO_BELOW):
                assert check.value is not None


def test_the_identity_edge_case_from_m1_is_in_the_corpus(cases: list[PersonaCase]) -> None:
    """The renderer proves the bytes stay ordinary text; this measures behaviour."""
    edge = [case for case in cases if "<<<IDENTITY tier=T0>>>" in case.input]
    assert edge, "no case presents identity-shaped syntax as user text"
    assert any("B23" in case.covers_rules for case in edge)


def test_a_fabricated_memory_probe_exists_without_retrieval(cases: list[PersonaCase]) -> None:
    probes = [case for case in cases if "fabricated_memory" in case.covers_probes]
    assert probes
    for case in probes:
        assert case.history == () or all(turn.role in VALID_HISTORY_ROLES for turn in case.history)


def test_every_case_declares_what_it_covers(cases: list[PersonaCase]) -> None:
    """A case that discharges no declared obligation is not part of the argument."""
    for case in cases:
        assert case.covers_rules or case.covers_probes, case.id


def test_a_missing_rule_is_detected() -> None:
    """The validator's own teeth: drop a case, and coverage must notice."""
    cases = load_cases(CASES_DIR)
    without = [case for case in cases if "B1" not in case.covers_rules]
    report = coverage(without)
    assert "B1" in report.missing_rules
    assert not report.complete


def test_an_undeclared_rule_is_not_silently_credited() -> None:
    """Covering a rule requires saying so; a plausible-looking case is not enough."""
    cases = load_cases(CASES_DIR)
    stripped = [
        PersonaCase(
            id=case.id, tags=case.tags,
            behavioural_expectation=case.behavioural_expectation,
            undesired_characteristics=case.undesired_characteristics,
            mode=case.mode, history=case.history, input=case.input, checks=case.checks,
            covers_rules=(), covers_probes=case.covers_probes, source_file=case.source_file,
        )
        for case in cases
    ]
    assert coverage(stripped).missing_rules == list(REQUIRED_RULES)
