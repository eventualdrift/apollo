"""The acceptance record cannot drift from the code or from the spec (spec O.1).

`docs/operations/acceptance.md` says, for each of the thirty criteria of the
technical gate, whether it is proven and by which tests. A record like that
goes stale silently: a test is renamed, a criterion is reworded, a count in
the summary is not updated. These tests fail when that happens.

What they cannot check is whether a named test really proves its criterion.
That is a judgement, and the record says so.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RECORD = REPO_ROOT / "docs" / "operations" / "acceptance.md"
SPEC = REPO_ROOT / "docs" / "architecture" / "phase-zero-spec.md"

STATUSES = ("PROVEN", "PROVEN IN PART", "NOT YET PROVEN")
_STATUS = "(" + "|".join(STATUSES) + ")"
HEADING = re.compile(rf"^### (\d+)\. (.+) — {_STATUS}$")
CITATION = re.compile(r"`(tests/[\w/]+\.py)::(test_\w+)`")
SUMMARY_ROW = re.compile(rf"^\| {_STATUS} \|.*\| (\d+) \| ([\d, ]+) \|$")
SPEC_CRITERION = re.compile(r"^(\d+)\. \*\*(.+?)\.\*\*")


def _entries() -> dict[int, tuple[str, str, str]]:
    """Criterion number -> (title, status, the text under its heading)."""
    found: dict[int, tuple[str, str, str]] = {}
    section = RECORD.read_text(encoding="utf-8").split("## The thirty criteria", 1)[1]
    section = section.split("\n## ", 1)[0]
    for block in re.split(r"(?m)^(?=### )", section):
        heading = HEADING.match(block.splitlines()[0]) if block.startswith("### ") else None
        if heading is None:
            assert not block.startswith("### "), f"unreadable heading: {block.splitlines()[0]}"
            continue
        number = int(heading[1])
        assert number not in found, f"criterion {number} appears twice"
        found[number] = (heading[2], heading[3], block)
    return found


def _spec_titles() -> dict[int, str]:
    text = SPEC.read_text(encoding="utf-8")
    gate = text.split("### O.1 Technical gate", 1)[1].split("### O.2", 1)[0]
    return {int(m[1]): m[2] for line in gate.splitlines() if (m := SPEC_CRITERION.match(line))}


def test_the_record_has_exactly_the_specs_thirty_criteria_under_their_own_titles() -> None:
    spec = _spec_titles()
    assert sorted(spec) == list(range(1, 31)), "the spec's technical gate is not 30 criteria"
    recorded = {number: title for number, (title, _, _) in _entries().items()}
    assert recorded == spec


def test_every_criterion_names_a_test_and_every_named_test_exists() -> None:
    missing = []
    for number, (_, _, block) in sorted(_entries().items()):
        cited = CITATION.findall(block)
        assert cited, f"criterion {number} names no test"
        for path, name in cited:
            file = REPO_ROOT / path
            if not file.is_file() or not re.search(
                rf"(?m)^def {name}\(", file.read_text(encoding="utf-8")
            ):
                missing.append(f"O.1/{number}: {path}::{name}")
    assert not missing, "the record cites tests that do not exist:\n" + "\n".join(missing)


def test_the_summary_table_agrees_with_the_entries() -> None:
    by_status: dict[str, list[int]] = {status: [] for status in STATUSES}
    for number, (_, status, _) in sorted(_entries().items()):
        by_status[status].append(number)

    summary = {}
    for line in RECORD.read_text(encoding="utf-8").splitlines():
        if row := SUMMARY_ROW.match(line):
            summary[row[1]] = (int(row[2]), [int(n) for n in row[3].split(",")])
    assert set(summary) == set(STATUSES), "the summary table is missing a status row"
    for status in STATUSES:
        count, numbers = summary[status]
        assert numbers == by_status[status], status
        assert count == len(numbers), status


@pytest.mark.parametrize("status", ["PROVEN IN PART", "NOT YET PROVEN"])
def test_an_open_criterion_says_what_is_open_and_where_it_is_closed(status: str) -> None:
    """A criterion that is not fully proven must point at the step that would prove it."""
    for number, (_, recorded, block) in sorted(_entries().items()):
        if recorded != status:
            continue
        open_line = [line for line in block.splitlines() if line.startswith("- Open:")]
        assert len(open_line) == 1, f"criterion {number} does not say what is open"
        assert re.search(r"Step \d above", open_line[0]), f"criterion {number} names no step"


def test_the_gate_is_not_reported_as_passed_while_any_criterion_is_open() -> None:
    statuses = {status for _, status, _ in _entries().values()}
    text = RECORD.read_text(encoding="utf-8")
    if statuses != {"PROVEN"}:
        assert "**The technical gate has not passed.**" in text
