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
