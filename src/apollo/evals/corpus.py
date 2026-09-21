"""Corpus coverage — proved by code, not by counting YAML files (spec J.1).

The contract has twenty-five rules and the spec names sixteen probes. A suite
that quietly drops one still runs green, which is the failure mode this module
exists to prevent: coverage is asserted mechanically, and a case only
discharges a rule it explicitly declares in `covers_rules`.

Inverse coverage is held to the same standard. A suite that only punishes
sycophancy teaches contrarianism; one that only punishes verbosity teaches
terseness everywhere; one that only punishes levity in a crisis teaches
humourlessness. Each of those has a required tag below, so the corpus cannot
drift into a one-sided caricature without the validator noticing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from apollo.config import MODE_BENCHMARK
from apollo.evals.models import CheckType, PersonaCase

#: B1..B25, every rule in the frozen behavioural contract.
REQUIRED_RULES: tuple[str, ...] = tuple(f"B{n}" for n in range(1, 26))

#: The sixteen probes spec J.1 requires, as stable slugs.
REQUIRED_PROBES: tuple[str, ...] = (
    "sycophancy",
    "false_claim_correction",
    "unnecessary_praise",
    "unnecessary_restatement",
    "disagreement",
    "uncertainty",
    "casual_conversation",
    "unsolicited_advice",
    "verbosity",
    "list_overuse",
    "customer_service_phrasing",
    "fabricated_memory",
    "user_pressure_to_agree",
    "answer_change_under_pressure",
    "contrarianism_when_agreement_correct",
    "inappropriate_humour_serious_context",
)

#: Tags that must appear somewhere in the corpus so the suite measures the
#: absence of an overcorrection as well as the presence of the fault.
REQUIRED_INVERSE_TAGS: tuple[str, ...] = (
    "inverse_agreement_correct",
    "inverse_length_appropriate",
    "inverse_register_light_ok",
    "inverse_position_moves_on_argument",
)


@dataclass
class CoverageReport:
    case_count: int
    rules_covered: dict[str, list[str]] = field(default_factory=dict)
    probes_covered: dict[str, list[str]] = field(default_factory=dict)
    inverse_covered: dict[str, list[str]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def missing_rules(self) -> list[str]:
        return [rule for rule in REQUIRED_RULES if not self.rules_covered.get(rule)]

    @property
    def missing_probes(self) -> list[str]:
        return [probe for probe in REQUIRED_PROBES if not self.probes_covered.get(probe)]

    @property
    def missing_inverse(self) -> list[str]:
        return [tag for tag in REQUIRED_INVERSE_TAGS if not self.inverse_covered.get(tag)]

    @property
    def complete(self) -> bool:
        return not (
            self.missing_rules or self.missing_probes or self.missing_inverse or self.problems
        )

    def as_lines(self) -> list[str]:
        lines = [
            f"cases:            {self.case_count}",
            f"B1-B25 covered:   {len(REQUIRED_RULES) - len(self.missing_rules)}"
            f"/{len(REQUIRED_RULES)}",
            f"probes covered:   {len(REQUIRED_PROBES) - len(self.missing_probes)}"
            f"/{len(REQUIRED_PROBES)}",
            f"inverse covered:  {len(REQUIRED_INVERSE_TAGS) - len(self.missing_inverse)}"
            f"/{len(REQUIRED_INVERSE_TAGS)}",
        ]
        if self.missing_rules:
            lines.append(f"missing rules:    {self.missing_rules}")
        if self.missing_probes:
            lines.append(f"missing probes:   {self.missing_probes}")
        if self.missing_inverse:
            lines.append(f"missing inverse:  {self.missing_inverse}")
        for problem in self.problems:
            lines.append(f"problem: {problem}")
        lines.append("corpus: " + ("COMPLETE" if self.complete else "INCOMPLETE"))
        return lines


def coverage(cases: list[PersonaCase]) -> CoverageReport:
    report = CoverageReport(case_count=len(cases))
    seen_ids: dict[str, str] = {}

    for case in cases:
        if case.id in seen_ids:
            report.problems.append(
                f"duplicate case id {case.id} ({case.source_file} and {seen_ids[case.id]})"
            )
        seen_ids[case.id] = case.source_file

        if case.mode != MODE_BENCHMARK:
            report.problems.append(f"{case.id}: mode {case.mode!r} is not benchmark")
        if not case.checks:
            report.problems.append(f"{case.id}: no checks")
        for check in case.checks:
            if check.type is CheckType.MANUAL and not (check.rubric or "").strip():
                report.problems.append(f"{case.id}: manual check without a rubric")

        for rule in case.covers_rules:
            report.rules_covered.setdefault(rule, []).append(case.id)
            if rule not in REQUIRED_RULES:
                report.problems.append(f"{case.id}: {rule} is not a contract rule")
        for probe in case.covers_probes:
            report.probes_covered.setdefault(probe, []).append(case.id)
            if probe not in REQUIRED_PROBES:
                report.problems.append(f"{case.id}: {probe!r} is not a required probe")
        for tag in case.tags:
            if tag in REQUIRED_INVERSE_TAGS:
                report.inverse_covered.setdefault(tag, []).append(case.id)

    return report
