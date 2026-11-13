"""The retrieval corpus loads strictly and covers what spec E.5 requires."""

from __future__ import annotations

import pathlib

import pytest

from apollo.evals.retrieval import (
    CASE_TYPES,
    SPLITS,
    SWEEP_CONFIGS,
    SWEEP_FLOORS,
    CaseResult,
    Metrics,
    RetrievalCase,
    RetrievalFixtureError,
    load_case_file,
    load_case_files,
    select_settings,
    sweep_grid,
    unexpected_failures,
)
from apollo.memory.retrieval import DEFAULT_SETTINGS, RetrievalSettings

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


@pytest.mark.parametrize(("memory", "error"), [
    ("{ref: a, scope: user, kind: fact, subject: s, content: 123, origin_tier: user_asserted}",
     "content must be text"),
    ("{ref: a, scope: user, kind: fact, subject: {name: v}, content: c,"
     " origin_tier: user_asserted}", "subject must be text"),
    ("{ref: true, scope: user, kind: fact, subject: s, content: c, origin_tier: user_asserted}",
     "ref must be text"),
], ids=["number-content", "mapping-subject", "boolean-ref"])
def test_a_non_text_memory_field_is_refused(
    tmp_path: pathlib.Path, memory: str, error: str
) -> None:
    path = _write(tmp_path, (
        f"version: 1\nfixture:\n  memories:\n    - {memory}\n"
        "cases:\n  - {id: x, type: no_result, split: tune, description: d, query: q}\n"
    ))
    with pytest.raises(RetrievalFixtureError, match=error):
        load_case_file(path)


@pytest.mark.parametrize(("field", "value"), [
    ("id", "7"), ("description", "[a, b]"), ("query", "42"), ("previous", "{x: 1}"),
])
def test_a_non_text_case_field_is_refused(tmp_path: pathlib.Path, field: str, value: str) -> None:
    case = {"id": "x", "type": "no_result", "split": "tune", "description": "d", "query": "q"}
    case[field] = value
    rendered = ", ".join(f"{k}: {v}" for k, v in case.items())
    path = _write(
        tmp_path, f"version: 1\nfixture:\n  memories:\n{GOOD_MEMORY}cases:\n  - {{{rendered}}}\n"
    )
    with pytest.raises(RetrievalFixtureError, match=f"{field} must be text"):
        load_case_file(path)


@pytest.mark.parametrize("change", [
    "scope: world", "kind: event", "subject: other", "pinned: true",
], ids=["scope", "kind", "subject", "pinned"])
def test_a_correction_must_keep_its_predecessors_metadata(tmp_path: pathlib.Path,
                                                          change: str) -> None:
    """The lifecycle carries these forward; a fixture saying otherwise would not run as written."""
    base = "scope: user, kind: fact, subject: s, origin_tier: user_asserted"
    new = ", ".join(
        change if part.split(":")[0] == change.split(":")[0] else part
        for part in base.split(", ")
    )
    if change.startswith("pinned"):
        new += ", pinned: true"
    path = _write(tmp_path, (
        "version: 1\nfixture:\n  memories:\n"
        f"    - {{ref: old, {base}, content: c, status: superseded}}\n"
        f"    - {{ref: new, {new}, content: d, corrects: old}}\n"
        "cases:\n  - {id: x, type: no_result, split: tune, description: d, query: q}\n"
    ))
    with pytest.raises(RetrievalFixtureError, match="must keep"):
        load_case_file(path)


def test_a_memory_cannot_correct_itself(tmp_path: pathlib.Path) -> None:
    path = _write(tmp_path, (
        "version: 1\nfixture:\n  memories:\n"
        "    - {ref: a, scope: user, kind: fact, subject: s, content: c,"
        " origin_tier: user_asserted, status: superseded, corrects: a}\n"
        "cases:\n  - {id: x, type: no_result, split: tune, description: d, query: q}\n"
    ))
    with pytest.raises(RetrievalFixtureError, match="must be listed first"):
        load_case_file(path)


@pytest.mark.parametrize(
    "value", ["false", "{}", "", "a"], ids=["false", "mapping", "blank", "bare"]
)
def test_an_expectation_that_is_not_a_list_is_refused(tmp_path: pathlib.Path, value: str) -> None:
    path = _write(tmp_path, (
        f"version: 1\nfixture:\n  memories:\n{GOOD_MEMORY}cases:\n"
        "  - id: x\n    type: no_result\n    split: tune\n    description: d\n    query: q\n"
        f"    must_retrieve: {value}\n"
    ))
    with pytest.raises(RetrievalFixtureError, match="must_retrieve must be a list"):
        load_case_file(path)


def test_a_ref_both_allowed_and_forbidden_is_refused(tmp_path: pathlib.Path) -> None:
    path = _write(tmp_path, (
        f"version: 1\nfixture:\n  memories:\n{GOOD_MEMORY}cases:\n"
        "  - {id: x, type: no_result, split: tune, description: d, query: q,"
        " may_retrieve: [a], must_not_retrieve: [a]}\n"
    ))
    with pytest.raises(RetrievalFixtureError, match="both allowed and forbidden"):
        load_case_file(path)


def test_a_ref_repeated_in_one_expectation_is_refused(tmp_path: pathlib.Path) -> None:
    """A repeat would count twice in recall's numerator and denominator."""
    path = _write(tmp_path, (
        f"version: 1\nfixture:\n  memories:\n{GOOD_MEMORY}cases:\n"
        "  - {id: x, type: direct_fact, split: tune, description: d, query: q,"
        " must_retrieve: [a, a]}\n"
    ))
    with pytest.raises(RetrievalFixtureError, match="repeats"):
        load_case_file(path)


def test_the_corpus_digest_changes_with_any_case_or_memory(tmp_path: pathlib.Path) -> None:
    from apollo.evals.retrieval import corpus_digest

    def digest(case: str) -> str:
        text = f"version: 1\nfixture:\n  memories:\n{GOOD_MEMORY}cases:\n  - {case}\n"
        path = _write(tmp_path, text)
        return corpus_digest([load_case_file(path)])

    base = "{id: x, type: direct_fact, split: tune, description: d, query: q, must_retrieve: [a]"
    original = digest(base + "}")
    assert digest(base + "}") == original
    assert digest(base + ", max_rank: 3}") != original
    assert digest(base.replace("must_retrieve: [a]", "may_retrieve: [a]") + "}") != original
    assert digest(base.replace("query: q", "query: r") + "}") != original


def test_a_memory_can_be_corrected_only_once(tmp_path: pathlib.Path) -> None:
    """A chain does not branch: the first correction supersedes, the second has nothing to."""
    base = "scope: user, kind: fact, subject: s, origin_tier: user_asserted"
    path = _write(tmp_path, (
        "version: 1\nfixture:\n  memories:\n"
        f"    - {{ref: old, {base}, content: c, status: superseded}}\n"
        f"    - {{ref: one, {base}, content: d, corrects: old}}\n"
        f"    - {{ref: two, {base}, content: e, corrects: old}}\n"
        "cases:\n  - {id: x, type: no_result, split: tune, description: d, query: q}\n"
    ))
    with pytest.raises(RetrievalFixtureError, match="corrected more than once"):
        load_case_file(path)


@pytest.mark.parametrize(("memory", "error"), [
    ("{ref: a, scope: user, kind: fact, subject: '', content: c, origin_tier: user_asserted}",
     "subject must be non-empty"),
    ("{ref: a, scope: user, kind: fact, subject: s, content: '   ', origin_tier: user_asserted}",
     "content must be non-empty"),
    ('{ref: a, scope: user, kind: fact, subject: s, content: "a\\0b", origin_tier: user_asserted}',
     "NUL"),
], ids=["empty-subject", "blank-content", "nul-content"])
def test_a_claim_the_memory_store_would_refuse_is_refused_at_load(
    tmp_path: pathlib.Path, memory: str, error: str
) -> None:
    """The loader's error, not a database-time MemoryInputError, names the bad fixture."""
    path = _write(tmp_path, (
        f"version: 1\nfixture:\n  memories:\n    - {memory}\n"
        "cases:\n  - {id: x, type: no_result, split: tune, description: d, query: q}\n"
    ))
    with pytest.raises(RetrievalFixtureError, match=error):
        load_case_file(path)


@pytest.mark.parametrize("text", [
    # A repeated expectation key: the later, weaker one would silently win.
    "version: 1\nfixture:\n  memories:\n" + GOOD_MEMORY + "cases:\n"
    "  - id: x\n    type: direct_fact\n    split: tune\n    description: d\n    query: q\n"
    "    must_retrieve: [a]\n    must_retrieve: []\n",
    # A repeated top-level key.
    "version: 1\nversion: 1\nfixture:\n  memories:\n" + GOOD_MEMORY
    + "cases:\n  - {id: x, type: no_result, split: tune, description: d, query: q}\n",
], ids=["case-key", "top-level-key"])
def test_a_repeated_mapping_key_is_refused(tmp_path: pathlib.Path, text: str) -> None:
    with pytest.raises(RetrievalFixtureError, match="repeats the key"):
        load_case_file(_write(tmp_path, text))


def test_unparseable_yaml_is_a_fixture_error(tmp_path: pathlib.Path) -> None:
    with pytest.raises(RetrievalFixtureError, match="not valid YAML"):
        load_case_file(_write(tmp_path, "version: 1\nfixture: [unclosed\n"))


@pytest.mark.parametrize("version", ["true", "1.0", "'1'"], ids=["boolean", "float", "string"])
def test_the_version_must_be_the_whole_number_one(tmp_path: pathlib.Path, version: str) -> None:
    """`True == 1.0 == 1` in Python; the file must say 1, not something equal to it."""
    path = _write(tmp_path, (
        f"version: {version}\nfixture:\n  memories:\n{GOOD_MEMORY}"
        "cases:\n  - {id: x, type: no_result, split: tune, description: d, query: q}\n"
    ))
    with pytest.raises(RetrievalFixtureError, match="unsupported version"):
        load_case_file(path)


def _result(case_id: str, *failures: str) -> CaseResult:
    case = RetrievalCase(
        id=case_id, type="recency", split="tune", description="d", query="q", previous=None,
        must_retrieve=("new",), may_retrieve=(), must_not_retrieve=(), max_rank=1,
        substantive_match_expected=True,
    )
    return CaseResult(case, merged=("old", "new"), support=("old", "new"),
                      substantive_support=True, failures=failures)


def test_a_known_failing_case_is_excused_only_its_recorded_failures() -> None:
    """A new failure in a case already failing for another reason still counts."""
    known = {"rec": ["new at rank 2 > 1"]}
    assert unexpected_failures([_result("rec", "new at rank 2 > 1")], known) == {}
    assert unexpected_failures(
        [_result("rec", "new at rank 2 > 1", "substantive_support False, expected True")], known
    ) == {"rec": ["substantive_support False, expected True"]}
    # A known failure that gets worse is a different failure.
    assert unexpected_failures([_result("rec", "new at rank 3 > 1")], known) == {
        "rec": ["new at rank 3 > 1"]
    }


def test_a_case_with_no_recorded_failures_is_never_excused() -> None:
    assert unexpected_failures([_result("other", "new not retrieved")], {}) == {
        "other": ["new not retrieved"]
    }
    assert unexpected_failures([_result("passing")], {}) == {}


def test_a_correction_cannot_be_tombstoned(tmp_path: pathlib.Path) -> None:
    """A tombstone forgets the whole chain (spec D.9/1), not only the head.

    The predecessor, declared `superseded`, would be tombstoned too, so the
    database would not hold what the file says.
    """
    base = "scope: user, kind: fact, subject: s, origin_tier: user_asserted"
    path = _write(tmp_path, (
        "version: 1\nfixture:\n  memories:\n"
        f"    - {{ref: old, {base}, content: c, status: superseded}}\n"
        f"    - {{ref: new, {base}, content: d, corrects: old, status: tombstoned}}\n"
        "cases:\n  - {id: x, type: no_result, split: tune, description: d, query: q}\n"
    ))
    with pytest.raises(RetrievalFixtureError, match="cannot be tombstoned"):
        load_case_file(path)


@pytest.mark.parametrize("tier", ["model_inferred", "connector_imported"])
def test_only_user_asserted_claims_load_in_phase_zero(tmp_path: pathlib.Path, tier: str) -> None:
    """The database stores only `user_asserted` today (memory_phase0_origin_tier_ck)."""
    path = _write(tmp_path, (
        "version: 1\nfixture:\n  memories:\n"
        f"    - {{ref: a, scope: user, kind: fact, subject: s, content: c, origin_tier: {tier}}}\n"
        "cases:\n  - {id: x, type: no_result, split: tune, description: d, query: q}\n"
    ))
    with pytest.raises(RetrievalFixtureError, match="only user_asserted"):
        load_case_file(path)


# -- the selection rule (spec E.4) ---------------------------------------------------


def _metrics(recall: float, empty: float) -> Metrics:
    return Metrics(cases=10, passed=0, recall_at_5=recall, precision_at_5=1.0,
                   mean_rank_of_expected=1.0, correct_empty_rate=empty)


def _settings(floor: float, config: str = "english") -> RetrievalSettings:
    return RetrievalSettings(lexical_floor=floor, text_search_config=config)


def test_selection_takes_the_best_empty_rate_among_candidates_that_still_recall() -> None:
    chosen = select_settings({
        _settings(0.0): _metrics(recall=1.0, empty=0.6),
        _settings(0.05): _metrics(recall=0.95, empty=0.9),
        _settings(0.1): _metrics(recall=0.92, empty=0.9),
    })
    # 0.05 and 0.1 tie on the empty rate; the higher recall wins.
    assert chosen == _settings(0.05)


def test_selection_never_buys_a_perfect_empty_rate_with_no_recall() -> None:
    """Strict "correct empty first" would pick the floor that rejects everything."""
    chosen = select_settings({
        _settings(0.0): _metrics(recall=1.0, empty=0.8),
        _settings(0.9): _metrics(recall=0.0, empty=1.0),
        _settings(0.5): _metrics(recall=0.89, empty=1.0),  # just under 90% of the best
    })
    assert chosen == _settings(0.0)


def test_selection_breaks_a_full_tie_by_the_lower_floor_then_english() -> None:
    same = _metrics(recall=1.0, empty=1.0)
    assert select_settings({
        _settings(0.05): same, _settings(0.0, "simple"): same,
        _settings(0.0): same, _settings(0.025): same,
    }) == _settings(0.0)


def test_selection_prefers_the_better_configuration_over_the_baseline() -> None:
    assert select_settings({
        _settings(0.0): _metrics(recall=1.0, empty=0.5),
        _settings(0.0, "simple"): _metrics(recall=1.0, empty=1.0),
    }) == _settings(0.0, "simple")


def test_the_sweep_grid_covers_both_configurations_and_the_defaults() -> None:
    grid = sweep_grid()
    assert len(grid) == len(set(grid)) == len(SWEEP_FLOORS) * len(SWEEP_CONFIGS)
    assert {s.text_search_config for s in grid} == {"english", "simple"}
    assert DEFAULT_SETTINGS in grid
    with pytest.raises(ValueError, match="nothing to select"):
        select_settings({})
