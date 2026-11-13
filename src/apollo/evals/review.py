"""Versioned human-review records and acceptance receipts.

Templates contain no decisions. A receipt records a gate a person completed.
There are two kinds:

* `gate2_replacement`: a candidate accepted by comparison with an accepted
  incumbent. Every such receipt names the receipt that authorised its
  incumbent, so it cannot be the first.
* `gate2_first_baseline`: the one receipt with no incumbent (ADR-0014). It
  exists because the chain has to start somewhere. It is issued only for a
  complete real-model run a person has read and designated in their own
  words, acknowledging that no comparison was made.

Neither is issued by code deciding anything. Local evidence is operator-
trusted, not signed: hashes detect changed material, not forged human authorship.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import Field, ValidationError

from apollo.evals.diff import RunDiff
from apollo.evals.evidence import StrictEvidence, document_hash, required_samples
from apollo.evals.models import PersonaCase


class RunBinding(StrictEvidence):
    run_id: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewBinding(StrictEvidence):
    candidate: RunBinding
    incumbent: RunBinding
    identity_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    corpus_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    comparison_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    waivers_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    incumbent_acceptance_hash: str | None


class CaseDecision(StrictEvidence):
    case_id: str
    sample_indexes: list[int]
    manual_check_indexes: list[int]
    decision: Literal["accept", "waive", "reject"] | None
    date: str | None
    reason: str | None


class HumanReview(StrictEvidence):
    review_version: Literal[1]
    binding: ReviewBinding
    requirements: list[dict[str, Any]]
    reviewer: str | None
    review_date: str | None
    diff_reviewed: bool | None
    decisions: list[CaseDecision]


class AcceptanceRecord(StrictEvidence):
    acceptance_version: Literal[1]
    kind: Literal["gate2_replacement"]
    status: Literal["passed"]
    binding: ReviewBinding
    review_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer: str = Field(min_length=1)
    review_date: str


FIRST_BASELINE = "gate2_first_baseline"
#: What a waiver for a first baseline names where a replacement's names the
#: incumbent run's hash. A waiver is scoped to the evidence it was written
#: against, so one written for a comparison cannot be reused for a baseline.
NO_INCUMBENT = "first-baseline"
#: A designation note is a sentence a person wrote. This is a floor against an
#: empty or placeholder one, not a judgement of it.
MIN_NOTE_CHARS = 12


class BaselineBinding(StrictEvidence):
    candidate: RunBinding
    identity_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    corpus_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    waivers_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class BaselineDesignation(StrictEvidence):
    #: The reviewer's explicit acknowledgement that this run was compared with
    #: no incumbent. Null in a template; only a person sets it.
    no_incumbent_comparison: bool | None
    #: Their own note naming this run the first baseline.
    note: str | None


class BaselineReview(StrictEvidence):
    review_version: Literal[1]
    kind: Literal["gate2_first_baseline"]
    binding: BaselineBinding
    requirements: list[dict[str, Any]]
    reviewer: str | None
    review_date: str | None
    designation: BaselineDesignation
    decisions: list[CaseDecision]


class BaselineAcceptance(StrictEvidence):
    acceptance_version: Literal[1]
    kind: Literal["gate2_first_baseline"]
    status: Literal["passed"]
    binding: BaselineBinding
    review_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer: str = Field(min_length=1)
    review_date: str


def run_binding(run: dict[str, Any]) -> dict[str, str]:
    return {"run_id": run["run_id"], "content_hash": document_hash(run)}


def incumbent_authorisation_problems(
    document: dict[str, Any] | None, incumbent: dict[str, Any]
) -> list[str]:
    if document is None:
        return [
            "missing prior incumbent acceptance record; two run files do not authorise a baseline"
        ]
    if document.get("kind") == FIRST_BASELINE:
        return _baseline_authorisation_problems(document, incumbent)
    try:
        record = AcceptanceRecord.model_validate(document)
    except ValidationError:
        return ["invalid incumbent acceptance record"]
    binding = record.binding
    if (
        binding.candidate.run_id != incumbent.get("run_id")
        or binding.candidate.content_hash != document_hash(incumbent)
        or binding.identity_hash != incumbent.get("identity_hash")
        or binding.corpus_hash != incumbent.get("corpus_hash")
        or binding.candidate.run_id == binding.incumbent.run_id
        or binding.candidate.content_hash == binding.incumbent.content_hash
        or not is_hash(binding.incumbent_acceptance_hash)
        or not record.reviewer.strip()
        or not valid_date(record.review_date)
    ):
        return ["stale or unbound incumbent acceptance record; a replacement receipt is "
                "never a first baseline"]
    return []


def _baseline_authorisation_problems(
    document: dict[str, Any], incumbent: dict[str, Any]
) -> list[str]:
    """A first-baseline receipt authorises exactly the run it designated (ADR-0014)."""
    try:
        record = BaselineAcceptance.model_validate(document)
    except ValidationError:
        return ["invalid first-baseline acceptance record"]
    binding = record.binding
    if (
        binding.candidate.run_id != incumbent.get("run_id")
        or binding.candidate.content_hash != document_hash(incumbent)
        or binding.identity_hash != incumbent.get("identity_hash")
        or binding.corpus_hash != incumbent.get("corpus_hash")
        or not record.reviewer.strip()
        or not valid_date(record.review_date)
    ):
        return ["stale or unbound first-baseline acceptance record"]
    return []


def review_template(
    candidate: dict[str, Any],
    incumbent: dict[str, Any],
    comparison: RunDiff,
    cases: list[PersonaCase],
    *,
    incumbent_acceptance: dict[str, Any] | None,
    waivers: list[dict[str, Any]],
    failing_cases: set[str],
) -> dict[str, Any]:
    changed = {c.case_id: list(c.reasons) for c in comparison.changed_cases}
    requirements = []
    decisions = []
    for case in sorted(cases, key=lambda c: c.id):
        if case.id not in changed and not case.has_manual_check and case.id not in failing_cases:
            continue
        manual = [i for i, check in enumerate(case.checks) if check.is_manual]
        indexes = list(range(1, required_samples(case) + 1))
        requirements.append(
            {
                "case_id": case.id,
                "change_reasons": changed.get(case.id, []),
                "sample_indexes": indexes,
                "manual_rubrics": [
                    {"check_index": i, "rubric": case.checks[i].rubric} for i in manual
                ],
                "deterministic_failure": case.id in failing_cases,
            }
        )
        decisions.append(
            {
                "case_id": case.id,
                "sample_indexes": indexes,
                "manual_check_indexes": manual,
                "decision": None,
                "date": None,
                "reason": None,
            }
        )
    return {
        "review_version": 1,
        "binding": {
            "candidate": run_binding(candidate),
            "incumbent": run_binding(incumbent),
            "identity_hash": candidate["identity_hash"],
            "corpus_hash": candidate["corpus_hash"],
            "comparison_hash": document_hash(comparison.as_dict()),
            "waivers_hash": document_hash(waivers),
            "incumbent_acceptance_hash": (
                document_hash(incumbent_acceptance) if incumbent_acceptance is not None else None
            ),
        },
        "requirements": requirements,
        "reviewer": None,
        "review_date": None,
        "diff_reviewed": None,
        "decisions": decisions,
    }


def review_problems(document: dict[str, Any] | None, template: dict[str, Any]) -> list[str]:
    if document is None:
        return ["missing persisted human review"]
    try:
        review = HumanReview.model_validate(document)
    except ValidationError:
        return ["invalid human review schema"]
    problems = []
    if review.binding.model_dump() != template["binding"]:
        problems.append("review binding is stale or refers to different evidence")
    if review.requirements != template["requirements"]:
        problems.append(
            "review requirements omit or change the computed comparison/manual material"
        )
    if not review.reviewer or not review.reviewer.strip() or not valid_date(review.review_date):
        problems.append("human reviewer and valid review date are required")
    if review.diff_reviewed is not True:
        problems.append("explicit review of the computed diff is still pending")
    return problems + _decision_problems(review.decisions, review.review_date, template)


def _decision_problems(
    decisions: list[CaseDecision], review_date: str | None, template: dict[str, Any]
) -> list[str]:
    """One decision per required case, covering all of it. The same for both kinds of review."""
    problems = []
    expected = {d["case_id"]: d for d in template["decisions"]}
    ids = [d.case_id for d in decisions]
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        problems.append("missing, duplicate/conflicting or out-of-scope case decisions")
    failures = {r["case_id"] for r in template["requirements"] if r["deterministic_failure"]}
    for decision in decisions:
        if decision.case_id not in expected:
            continue
        target = expected[decision.case_id]
        if (
            decision.sample_indexes != target["sample_indexes"]
            or decision.manual_check_indexes != target["manual_check_indexes"]
        ):
            problems.append(f"{decision.case_id}: review must cover every sample and manual rubric")
        if decision.decision not in {"accept", "waive"}:
            problems.append(f"{decision.case_id}: review decision is pending or rejected")
        if not valid_date(decision.date) or (
            decision.date is not None
            and review_date is not None
            and decision.date > review_date
        ):
            problems.append(f"{decision.case_id}: invalid decision date")
        if decision.decision == "waive" and (
            not decision.reason or len(decision.reason.strip()) < 12
        ):
            problems.append(f"{decision.case_id}: a waiver needs an explicit written reason")
        if decision.case_id in failures and decision.decision != "waive":
            problems.append(f"{decision.case_id}: acceptance cannot replace a deterministic waiver")
    return problems


# -- the first baseline (ADR-0014) -----------------------------------------------------


def baseline_template(
    candidate: dict[str, Any],
    cases: list[PersonaCase],
    *,
    waivers: list[dict[str, Any]],
    failing_cases: set[str],
) -> dict[str, Any]:
    """What a person must read and decide to designate `candidate` the first baseline.

    Every case with a manual rubric, in every sample, and every case with a
    deterministic failure. There is no diff to review: nothing came before.
    Every decision field, and the designation itself, is null.
    """
    requirements = []
    decisions = []
    for case in sorted(cases, key=lambda c: c.id):
        if not case.has_manual_check and case.id not in failing_cases:
            continue
        manual = [i for i, check in enumerate(case.checks) if check.is_manual]
        indexes = list(range(1, required_samples(case) + 1))
        requirements.append(
            {
                "case_id": case.id,
                "sample_indexes": indexes,
                "manual_rubrics": [
                    {"check_index": i, "rubric": case.checks[i].rubric} for i in manual
                ],
                "deterministic_failure": case.id in failing_cases,
            }
        )
        decisions.append(
            {
                "case_id": case.id,
                "sample_indexes": indexes,
                "manual_check_indexes": manual,
                "decision": None,
                "date": None,
                "reason": None,
            }
        )
    return {
        "review_version": 1,
        "kind": FIRST_BASELINE,
        "binding": {
            "candidate": run_binding(candidate),
            "identity_hash": candidate["identity_hash"],
            "corpus_hash": candidate["corpus_hash"],
            "waivers_hash": document_hash(waivers),
        },
        "requirements": requirements,
        "reviewer": None,
        "review_date": None,
        "designation": {"no_incumbent_comparison": None, "note": None},
        "decisions": decisions,
    }


def baseline_review_problems(
    document: dict[str, Any] | None, template: dict[str, Any]
) -> list[str]:
    if document is None:
        return ["missing persisted human review"]
    try:
        review = BaselineReview.model_validate(document)
    except ValidationError:
        return ["invalid first-baseline review schema"]
    problems = []
    if review.binding.model_dump() != template["binding"]:
        problems.append("review binding is stale or refers to different evidence")
    if review.requirements != template["requirements"]:
        problems.append("review requirements omit or change the required manual material")
    if not review.reviewer or not review.reviewer.strip() or not valid_date(review.review_date):
        problems.append("human reviewer and valid review date are required")
    if review.designation.no_incumbent_comparison is not True:
        problems.append(
            "the designation must acknowledge that no incumbent comparison was made"
        )
    note = review.designation.note
    if not note or len(note.strip()) < MIN_NOTE_CHARS:
        problems.append("the designation needs a written note naming this run the first baseline")
    return problems + _decision_problems(review.decisions, review.review_date, template)


def baseline_acceptance(review: dict[str, Any]) -> dict[str, Any]:
    """Called only by the passing gate; copies attribution, never invents a decision."""
    return {
        "acceptance_version": 1,
        "kind": FIRST_BASELINE,
        "status": "passed",
        "binding": review["binding"],
        "review_hash": document_hash(review),
        "reviewer": review["reviewer"],
        "review_date": review["review_date"],
    }


def acceptance_record(review: dict[str, Any]) -> dict[str, Any]:
    """Called only by the passing gate; copies attribution, never invents a decision."""
    return {
        "acceptance_version": 1,
        "kind": "gate2_replacement",
        "status": "passed",
        "binding": review["binding"],
        "review_hash": document_hash(review),
        "reviewer": review["reviewer"],
        "review_date": review["review_date"],
    }


def valid_date(value: str | None) -> bool:
    try:
        parsed = date.fromisoformat(value or "")
        return parsed.isoformat() == value and parsed <= date.today()
    except ValueError:
        return False


def is_hash(value: str | None) -> bool:
    return value is not None and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
