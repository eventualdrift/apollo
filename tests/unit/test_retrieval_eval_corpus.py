"""The retrieval corpus loads strictly and covers what spec E.5 requires."""

from __future__ import annotations

import pathlib

import pytest

from apollo.evals.retrieval import (
    CASE_TYPES,
    SPLITS,
    RetrievalFixtureError,
    load_case_file,
    load_case_files,
)

CASES = pathlib.Path(__file__).resolve().parents[2] / "evals" / "retrieval" / "cases"


def test_every_required_case_type_is_in_both_splits() -> None:
    """Nine types (spec E.5, O.1/24); each tuned on some cases and judged on others."""
    cases = [c for f in load_case_files(CASES) for c in f.cases]
    for split in SPLITS:
        covered = {c.type for c in cases if c.split == split}
        assert covered == set(CASE_TYPES), (split, sorted(set(CASE_TYPES) - covered))


def test_some_cases_expect_honest_absence_in_both_splits() -> None:
    cases = [c for f in load_case_files(CASES) for c in f.cases]
    for split in SPLITS:
        assert sum(c.substantive_match_expected is False
                   for c in cases if c.split == split) >= 3, split


def _write(tmp_path: pathlib.Path, text: str) -> pathlib.Path:
    path = tmp_path / "case.yaml"
    path.write_text(text)
    return path


GOOD_MEMORY = (
    "    - {ref: a, scope: user, kind: fact, subject: s, content: c, origin_tier: user_asserted}\n"
)


@pytest.mark.parametrize(("case", "error"), [
    ("{id: x, type: vibes, split: tune, description: d, query: q}", "unknown type"),
    ("{id: x, type: no_result, split: train, description: d, query: q}", "split must be"),
    ("{id: x, type: no_result, split: tune, description: d, query: q, must_retrieve: [zz]}",
     "unknown refs"),
    ("{id: x, type: no_result, split: tune, description: d, query: q, must_retrieve: [a],"
     " must_not_retrieve: [a]}", "both must and must-not"),
    ("{id: x, type: no_result, split: tune, description: d, query: q, must_retrieve: [a],"
     " substantive_match_expected: false}", "no substantive match expected"),
    ("{id: x, type: no_result, split: tune, description: d, query: q, surprise: 1}",
     "unknown fields"),
], ids=["type", "split", "ref", "overlap", "contradiction", "field"])
def test_a_malformed_case_is_refused(tmp_path: pathlib.Path, case: str, error: str) -> None:
    path = _write(tmp_path, f"version: 1\nfixture:\n  memories:\n{GOOD_MEMORY}cases:\n  - {case}\n")
    with pytest.raises(RetrievalFixtureError, match=error):
        load_case_file(path)


def test_a_superseded_memory_needs_its_correction(tmp_path: pathlib.Path) -> None:
    path = _write(tmp_path, (
        "version: 1\nfixture:\n  memories:\n"
        "    - {ref: a, scope: user, kind: fact, subject: s, content: c,"
        " origin_tier: user_asserted, status: superseded}\n"
        "cases:\n  - {id: x, type: no_result, split: tune, description: d, query: q}\n"
    ))
    with pytest.raises(RetrievalFixtureError, match="superseded by nothing"):
        load_case_file(path)
