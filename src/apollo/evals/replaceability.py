"""The replaceability report (spec O.1 criterion 11).

Whether Apollo survives a model swap is answered by putting the brains' answers
to the same case next to each other and reading them. This module lays that out
and stops there: no rank, no score, no overall winner. The question is not
which model is better, it is whether Apollo is still Apollo.

A brain that was not run is `NOT RUN`. It is never `PASS`. Reporting an absent
experiment as a success is the specific failure this milestone exists to make
impossible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The brains phase zero compares. Order is presentation, not preference.
BRAIN_ORDER = ("brain.fake", "brain.local", "brain.reference")

NOT_RUN = "NOT RUN"


@dataclass(frozen=True)
class BrainCell:
    alias: str
    #: `NOT RUN`, or the case's deterministic status for that brain.
    status: str
    response: str | None = None
    bundle_hash: str | None = None
    model_identifier: str | None = None
    sample_count: int = 0
    failing_checks: tuple[str, ...] = ()

    @property
    def ran(self) -> bool:
        return self.status != NOT_RUN

    def as_dict(self) -> dict[str, Any]:
        return {
            "brain_alias": self.alias,
            "status": self.status,
            "response": self.response,
            "bundle_hash": self.bundle_hash,
            "model_identifier": self.model_identifier,
            "sample_count": self.sample_count,
            "failing_checks": list(self.failing_checks),
        }


@dataclass(frozen=True)
class CaseRow:
    case_id: str
    cells: tuple[BrainCell, ...]

    @property
    def observed_bundle_hashes(self) -> list[str]:
        return sorted({cell.bundle_hash for cell in self.cells if cell.bundle_hash})

    @property
    def bundle_hash_equal(self) -> bool:
        """Equality across the brains that actually ran.

        With fewer than two runs there is nothing to compare; the report says
        so rather than claiming equality it did not observe.
        """
        return len(self.observed_bundle_hashes) <= 1

    @property
    def comparable(self) -> bool:
        return sum(1 for cell in self.cells if cell.ran) >= 2 and self.bundle_hash_equal

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "brains": [cell.as_dict() for cell in self.cells],
            "bundle_hashes": self.observed_bundle_hashes,
            "bundle_hash_equal": self.bundle_hash_equal,
            "comparable": self.comparable,
        }


@dataclass
class ReplaceabilityReport:
    brains: dict[str, str]
    rows: list[CaseRow]

    @property
    def brains_run(self) -> list[str]:
        return [alias for alias, state in self.brains.items() if state == "RUN"]

    @property
    def bundle_hash_mismatches(self) -> list[str]:
        return [row.case_id for row in self.rows if not row.bundle_hash_equal]

    def as_dict(self) -> dict[str, Any]:
        return {
            "brains": dict(self.brains),
            "brains_run": self.brains_run,
            "bundle_hash_mismatches": self.bundle_hash_mismatches,
            "cases": [row.as_dict() for row in self.rows],
            "note": "no rank, no score, no overall winner",
        }

    def as_lines(self) -> list[str]:
        lines = ["replaceability report", ""]
        for alias, state in self.brains.items():
            lines.append(f"  {alias:<18} {state}")
        if len(self.brains_run) < 2:
            lines += [
                "",
                "Fewer than two brains ran: there is no comparison to read yet. "
                "The brains above marked NOT RUN have produced no behavioural evidence.",
            ]
        if self.bundle_hash_mismatches:
            lines += ["", "!! bundle hash differs across brains for: "
                          + ", ".join(self.bundle_hash_mismatches),
                      "   Those cases are not comparable — the brains were asked "
                      "different compiled questions."]
        for row in self.rows:
            lines += ["", f"--- {row.case_id}   bundle hash equal: "
                          f"{'yes' if row.bundle_hash_equal else 'NO'}"]
            for cell in row.cells:
                if not cell.ran:
                    lines.append(f"    {cell.alias:<18} {NOT_RUN}")
                    continue
                failing = (
                    f"  failing: {', '.join(cell.failing_checks)}"
                    if cell.failing_checks
                    else ""
                )
                lines.append(
                    f"    {cell.alias:<18} {cell.status}{failing}  "
                    f"[{cell.model_identifier or '?'}]"
                )
                lines.append(f"      {_short(cell.response)}")
        lines += ["", "No rank, no score, no overall winner."]
        return lines


def build_report(
    runs: dict[str, dict[str, Any]],
    *,
    unavailable: dict[str, str] | None = None,
    brains: tuple[str, ...] = BRAIN_ORDER,
) -> ReplaceabilityReport:
    """`runs` maps a brain alias to its run document; everything else is NOT RUN."""
    unavailable = unavailable or {}
    states: dict[str, str] = {}
    for alias in brains:
        if alias in runs:
            states[alias] = "RUN"
        else:
            reason = unavailable.get(alias)
            states[alias] = f"{NOT_RUN} — {reason}" if reason else NOT_RUN

    case_ids: list[str] = []
    for alias in brains:
        for case in runs.get(alias, {}).get("cases", []):
            if case["case_id"] not in case_ids:
                case_ids.append(case["case_id"])

    rows = [
        CaseRow(
            case_id=case_id,
            cells=tuple(_cell(alias, runs.get(alias), case_id) for alias in brains),
        )
        for case_id in case_ids
    ]
    return ReplaceabilityReport(brains=states, rows=rows)


def _cell(alias: str, run: dict[str, Any] | None, case_id: str) -> BrainCell:
    if run is None:
        return BrainCell(alias=alias, status=NOT_RUN)
    for case in run.get("cases", []):
        if case["case_id"] != case_id:
            continue
        samples = case.get("samples", [])
        first = samples[0] if samples else {}
        failing = tuple(
            str(result.get("type"))
            for sample in samples
            for result in sample.get("check_results", [])
            if result.get("status") == "fail"
        )
        return BrainCell(
            alias=alias,
            status=str(case.get("deterministic_status", "unknown")),
            response=first.get("response"),
            bundle_hash=case.get("bundle_hash"),
            model_identifier=first.get("model_identifier"),
            sample_count=len(samples),
            failing_checks=tuple(dict.fromkeys(failing)),
        )
    return BrainCell(alias=alias, status=NOT_RUN)


def _short(value: str | None, limit: int = 300) -> str:
    if value is None:
        return "<no response>"
    text = value.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + f"… (+{len(text) - limit} chars)"
