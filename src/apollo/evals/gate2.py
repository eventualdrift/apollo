"""Gate 2 — behavioural compatibility (spec G.5, ADR-0012).

Gate 1 says Apollo *can* use a model. Gate 2 says Apollo still behaves like
Apollo through it. The distinction this module exists to keep visible is that
a run completing is not a gate passing: a model can produce every output
successfully and fail the behavioural contract in all of them.

Three refusals are deliberate:

* no pass-rate threshold. Inventing one before the first real run would be a
  guess dressed as a target (spec O.1 criterion 29).
* no auto-generated waiver. A waiver acknowledges a specific deviation with
  a human-supplied date and reason; it does not say the behaviour meets the
  contract. Code cannot author that decision.
* no behavioural claim for an offline brain. `brain.fake` proves the machinery,
  not Apollo — it is not intelligent, and a green fake run is evidence about
  the harness only.
"""

from __future__ import annotations

import datetime as dt
import pathlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import yaml

from apollo.core.identity import Identity, IdentityLoader
from apollo.errors import ApolloError
from apollo.evals import review as review_module
from apollo.evals.diff import diff_runs
from apollo.evals.evidence import DEFAULT_CASES_DIR, document_hash, validate_run
from apollo.evals.loader import load_cases
from apollo.evals.models import PersonaCase

#: Adapters that cannot constitute behavioural evidence about a real model.
OFFLINE_ADAPTER_KEYS = frozenset({"fake"})

WAIVER_FIELDS = {
    "case_id",
    "date",
    "reason",
    "brain_alias",
    "model_identifier",
    "identity_hash",
    "candidate_hash",
    "incumbent_hash",
    "sample_indexes",
    "check_indexes",
}
#: A waiver reason is a sentence a person wrote. This is a floor against an
#: empty or placeholder one, not a judgement of its quality.
MIN_REASON_CHARS = 12


class WaiverError(ApolloError):
    pass


class Gate2Status(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"
    #: The run exercised the evaluation machinery but says nothing about how a
    #: real model behaves. `brain.fake` lands here even when every check passes.
    INFRASTRUCTURE_ONLY = "infrastructure_only"


@dataclass(frozen=True)
class Waiver:
    case_id: str
    date: dt.date
    reason: str
    brain_alias: str
    identity_hash: str
    model_identifier: str | None = None
    candidate_hash: str = ""
    incumbent_hash: str = ""
    sample_indexes: tuple[int, ...] = ()
    check_indexes: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "date": self.date.isoformat(),
            "reason": self.reason,
            "brain_alias": self.brain_alias,
            "model_identifier": self.model_identifier,
            "identity_hash": self.identity_hash,
            "candidate_hash": self.candidate_hash,
            "incumbent_hash": self.incumbent_hash,
            "sample_indexes": list(self.sample_indexes),
            "check_indexes": list(self.check_indexes),
        }


@dataclass(frozen=True)
class DeterministicFailure:
    case_id: str
    sample_index: int
    failing_checks: tuple[str, ...]
    check_indexes: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "sample_index": self.sample_index,
            "failing_checks": list(self.failing_checks),
            "check_indexes": list(self.check_indexes),
        }


@dataclass(frozen=True)
class WaivedFailure:
    failure: DeterministicFailure
    waiver: Waiver

    def as_dict(self) -> dict[str, Any]:
        return {"failure": self.failure.as_dict(), "waiver": self.waiver.as_dict()}


@dataclass(frozen=True)
class RejectedWaiver:
    waiver: Waiver
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"waiver": self.waiver.as_dict(), "reason": self.reason}


@dataclass(frozen=True)
class ManualCase:
    """A manual case is presented, never graded."""

    case_id: str
    rubric: str
    sample_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "rubric": self.rubric,
            "sample_count": self.sample_count,
        }


@dataclass
class Gate2Report:
    brain_alias: str
    adapter_key: str
    status: Gate2Status
    run_completed: bool
    expected_identity_hash: str
    observed_identity_hash: str | None
    #: Every deterministic check passed, or each failure carries a valid
    #: waiver. Mechanical eligibility only — it is not the gate.
    mechanically_eligible: bool = False
    #: Whether this run can say anything about a real model at all.
    real_model_evidence: bool = False
    blocking: tuple[str, ...] = ()
    failures: tuple[DeterministicFailure, ...] = ()
    waived: tuple[WaivedFailure, ...] = ()
    rejected_waivers: tuple[RejectedWaiver, ...] = ()
    manual_cases: tuple[ManualCase, ...] = ()
    case_count: int = 0
    sample_count: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)
    candidate_valid: bool = False
    comparison_valid: bool = False
    incumbent_authorised: bool = False
    review_complete: bool = False
    comparison: dict[str, Any] | None = None
    review_template: dict[str, Any] | None = None
    acceptance_record: dict[str, Any] | None = None

    @property
    def passed(self) -> bool:
        return self.status is Gate2Status.PASSED

    def as_dict(self) -> dict[str, Any]:
        return {
            "brain_alias": self.brain_alias,
            "adapter_key": self.adapter_key,
            "status": str(self.status),
            "eval_run_completed": self.run_completed,
            "gate2_passed": self.passed,
            "mechanically_eligible": self.mechanically_eligible,
            "real_model_evidence": self.real_model_evidence,
            "expected_identity_hash": self.expected_identity_hash,
            "observed_identity_hash": self.observed_identity_hash,
            "blocking": list(self.blocking),
            "failures": [f.as_dict() for f in self.failures],
            "waived": [w.as_dict() for w in self.waived],
            "rejected_waivers": [r.as_dict() for r in self.rejected_waivers],
            "manual_cases": [m.as_dict() for m in self.manual_cases],
            "case_count": self.case_count,
            "sample_count": self.sample_count,
            "notes": list(self.notes),
            "candidate_valid": self.candidate_valid,
            "comparison_valid": self.comparison_valid,
            "incumbent_authorised": self.incumbent_authorised,
            "review_complete": self.review_complete,
            "comparison": self.comparison,
            "review_template": self.review_template,
            "acceptance_record": self.acceptance_record,
        }

    def as_lines(self) -> list[str]:
        lines = [
            f"brain:               {self.brain_alias} ({self.adapter_key})",
            f"EVAL RUN COMPLETED:  {'yes' if self.run_completed else 'no'}",
            f"GATE 2:              {str(self.status).upper()}",
            f"cases/samples:       {self.case_count}/{self.sample_count}",
            f"identity hash:       {self.observed_identity_hash or '<none>'}",
            f"candidate valid:     {self.candidate_valid}",
            f"comparison valid:    {self.comparison_valid}",
            f"incumbent authorised:{self.incumbent_authorised}",
            f"review complete:     {self.review_complete}",
        ]
        if self.blocking:
            lines.append("blocking:")
            lines += [f"  - {reason}" for reason in self.blocking]
        if self.failures:
            lines.append(f"deterministic failures: {len(self.failures)}")
            for failure in self.failures:
                lines.append(
                    f"  - {failure.case_id} sample {failure.sample_index}: "
                    f"{', '.join(failure.failing_checks)}"
                )
        if self.waived:
            lines.append("waived deviations (human-attributed records, with a date and reason):")
            for waived in self.waived:
                lines.append(
                    f"  - {waived.failure.case_id} sample {waived.failure.sample_index} "
                    f"[{waived.waiver.date.isoformat()}] {waived.waiver.reason}"
                )
        if self.rejected_waivers:
            lines.append("rejected waivers:")
            for rejected in self.rejected_waivers:
                lines.append(f"  - {rejected.waiver.case_id}: {rejected.reason}")
        if self.manual_cases:
            total = sum(m.sample_count for m in self.manual_cases)
            lines.append(
                f"manual cases: {len(self.manual_cases)} ({total} samples); "
                f"review {'complete' if self.review_complete else 'pending'}. "
                "These are not automatically graded."
            )
        lines += [f"note: {note}" for note in self.notes]
        return lines


def not_run(brain_alias: str, reason: str) -> Gate2Report:
    """A brain with no run is NOT RUN. It is never PASS."""
    return Gate2Report(
        brain_alias=brain_alias,
        adapter_key="",
        status=Gate2Status.NOT_RUN,
        run_completed=False,
        expected_identity_hash="",
        observed_identity_hash=None,
        blocking=(reason,),
        notes=("absence of evidence is recorded as absence, never as a pass",),
    )


def evaluate_gate2(
    run: dict[str, Any],
    *,
    identity_hash: str,
    waivers: tuple[Waiver, ...] = (),
    identity: Identity | None = None,
    cases: list[PersonaCase] | None = None,
    incumbent: dict[str, Any] | None = None,
    incumbent_acceptance: dict[str, Any] | None = None,
    review: dict[str, Any] | None = None,
) -> Gate2Report:
    """Replacement acceptance, never merely a green recorded subset.

    `cases`/`identity` are trusted current repository inputs, not material read
    from the submitted run. The CLI always loads the complete repository corpus.
    """
    brain_alias = str(run.get("brain_alias", ""))
    adapter_key = str(run.get("adapter_key", ""))
    observed_identity = run.get("identity_hash")
    try:
        candidate_hash = document_hash(run)
        incumbent_hash = document_hash(incumbent) if incumbent is not None else ""
    except (ValueError, TypeError):
        report = not_run(brain_alias, "run evidence must be finite JSON data")
        report.status = Gate2Status.FAILED
        return report
    expected_cases = cases if cases is not None else load_cases(DEFAULT_CASES_DIR)
    current_identity = identity or IdentityLoader(DEFAULT_CASES_DIR.parents[2] / "identity").load()
    evidence = validate_run(run, cases=expected_cases, identity=current_identity)
    blocking = [f"candidate: {problem}" for problem in evidence.problems]
    if identity_hash != current_identity.content_hash:
        blocking.append("supplied current identity hash does not match repository identity")
    candidate_valid = not blocking
    entries = evidence.document["cases"] if evidence.document is not None else []
    samples = [s for c in entries for s in c["samples"]]
    failures = tuple(_failures(entries))
    waived, unwaived, rejected = _apply_waivers(
        failures,
        waivers,
        brain_alias=brain_alias,
        identity_hash=identity_hash,
        cases=entries,
        candidate_hash=candidate_hash,
        incumbent_hash=incumbent_hash,
    )
    for failure in unwaived:
        blocking.append(
            f"{failure.case_id} sample {failure.sample_index}: deterministic check(s) "
            f"{', '.join(failure.failing_checks)} failed with no waiver"
        )

    blocking.extend(f"waiver rejected: {r.reason}" for r in rejected)
    mechanically_eligible = candidate_valid and not unwaived and not rejected
    comparison = None
    comparison_valid = False
    incumbent_authorised = False
    template = None
    review_complete = False
    if incumbent is None:
        blocking.append(
            "missing incumbent: replacement acceptance requires an explicit distinct run"
        )
    else:
        incumbent_evidence = validate_run(
            incumbent, cases=expected_cases, identity=current_identity
        )
        comparison_problems = [f"incumbent: {p}" for p in incumbent_evidence.problems]
        if run.get("run_id") == incumbent.get("run_id") or candidate_hash == incumbent_hash:
            comparison_problems.append("self-comparison is not replacement evidence")
        if run.get("generation_params") != incumbent.get("generation_params"):
            comparison_problems.append("generation parameters differ; comparison is incompatible")
        if candidate_valid and incumbent_evidence.valid:
            prior_samples = [s for c in incumbent["cases"] for s in c["samples"]]
            if any(
                {s[key] for s in samples} & {s[key] for s in prior_samples}
                for key in ("invocation_id", "turn_id")
            ) or (run["conversation_id"] == incumbent["conversation_id"]):
                comparison_problems.append(
                    "reused invocation/turn/conversation evidence across runs"
                )
            comparison = diff_runs(incumbent, run)
            if not comparison.comparison_valid:
                comparison_problems.append("per-case input bundle comparison is incompatible")
        comparison_valid = candidate_valid and not comparison_problems
        blocking.extend(comparison_problems)
        auth_problems = review_module.incumbent_authorisation_problems(
            incumbent_acceptance, incumbent
        )
        incumbent_authorised = not auth_problems
        blocking.extend(auth_problems)
        if comparison_valid and comparison is not None:
            template = review_module.review_template(
                run,
                incumbent,
                comparison,
                expected_cases,
                incumbent_acceptance=incumbent_acceptance,
                waivers=[w.as_dict() for w in waivers],
                failing_cases={failure.case_id for failure in failures},
            )
            review_errors = review_module.review_problems(review, template)
            review_complete = not review_errors
            blocking.extend(review_errors)
    if template is None:
        blocking.append("human review cannot be validated without valid comparable evidence")

    if not run.get("cases"):
        status = Gate2Status.NOT_RUN
    elif adapter_key in OFFLINE_ADAPTER_KEYS or run.get("evidence_kind") == "offline":
        status = Gate2Status.INFRASTRUCTURE_ONLY
    else:
        status = Gate2Status.FAILED if blocking else Gate2Status.PASSED

    return Gate2Report(
        brain_alias=brain_alias,
        adapter_key=adapter_key,
        status=status,
        run_completed=evidence.completed,
        expected_identity_hash=identity_hash,
        observed_identity_hash=observed_identity if isinstance(observed_identity, str) else None,
        mechanically_eligible=mechanically_eligible,
        real_model_evidence=evidence.real_model,
        blocking=tuple(blocking),
        failures=failures,
        waived=tuple(waived),
        rejected_waivers=tuple(rejected),
        manual_cases=tuple(_manual_cases(entries)),
        case_count=len(entries),
        sample_count=len(samples),
        notes=(
            "hashes bind evidence; they do not authenticate its author or provider",
            "offline runs prove infrastructure only; no first-baseline bypass exists",
        ),
        candidate_valid=candidate_valid,
        comparison_valid=comparison_valid,
        incumbent_authorised=incumbent_authorised,
        review_complete=review_complete,
        comparison=comparison.as_dict() if comparison is not None else None,
        review_template=template,
        acceptance_record=(
            review_module.acceptance_record(review)
            if status is Gate2Status.PASSED and review is not None
            else None
        ),
    )


def compare_bundle_hashes(runs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Cross-brain bundle equality (spec F.5, O.1 criterion 11).

    Every brain must have been asked the same compiled question for a case, or
    the comparison of their answers is not an experiment.
    """
    per_case: dict[str, dict[str, str | None]] = {}
    for alias, run in runs.items():
        for case in run.get("cases", []):
            per_case.setdefault(case["case_id"], {})[alias] = case.get("bundle_hash")
    rows = []
    mismatched = []
    for case_id in sorted(per_case):
        hashes = per_case[case_id]
        distinct = {value for value in hashes.values() if value is not None}
        equal = len(distinct) <= 1
        if not equal:
            mismatched.append(case_id)
        rows.append({"case_id": case_id, "hashes": hashes, "equal": equal})
    return {
        "brains": sorted(runs),
        "cases": rows,
        "all_equal": not mismatched,
        "mismatched_cases": mismatched,
        "comparison_valid": not mismatched,
    }


# --------------------------------------------------------------------------
# Waiver loading — strict, because a malformed waiver is an accepted failure
# --------------------------------------------------------------------------


def load_waivers(path: pathlib.Path) -> tuple[Waiver, ...]:
    if not path.exists():
        return ()

    class UniqueLoader(yaml.SafeLoader):
        pass

    def mapping(loader: Any, node: Any) -> dict[Any, Any]:
        pairs = loader.construct_pairs(node, deep=True)
        result = {}
        for key, value in pairs:
            if not isinstance(key, str) or key in result:
                raise WaiverError("duplicate or invalid waiver mapping key")
            result[key] = value
        return result

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    try:
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    except yaml.YAMLError:
        raise WaiverError("invalid waiver YAML") from None
    return parse_waivers(document, where=path.name)


def parse_waivers(document: Any, *, where: str = "<waivers>") -> tuple[Waiver, ...]:
    if document is None:
        return ()
    if not isinstance(document, dict) or set(document) != {"waivers"}:
        raise WaiverError(f"{where}: expected a mapping with a `waivers` list")
    raw = document["waivers"]
    if not isinstance(raw, list):
        raise WaiverError(f"{where}: `waivers` must be a list")
    return tuple(_parse_waiver(item, where, index) for index, item in enumerate(raw))


def _parse_waiver(item: Any, where: str, index: int) -> Waiver:
    if not isinstance(item, dict):
        raise WaiverError(f"{where}: waivers[{index}] must be a mapping")
    unknown = set(item) - WAIVER_FIELDS
    if unknown:
        raise WaiverError(f"{where}: waivers[{index}] unknown fields {sorted(unknown)}")
    for required in ("case_id", "date", "reason", "brain_alias", "identity_hash"):
        if required not in item:
            raise WaiverError(f"{where}: waivers[{index}] is missing `{required}`")
    reason = item["reason"]
    if not isinstance(reason, str) or len(reason.strip()) < MIN_REASON_CHARS:
        raise WaiverError(
            f"{where}: waivers[{index}] needs a written reason of at least "
            f"{MIN_REASON_CHARS} characters; a waiver without one accepts a behavioural "
            "failure and records nothing about why"
        )
    date = item["date"]
    if isinstance(date, dt.datetime):
        date = date.date()
    elif isinstance(date, str):
        try:
            date = dt.date.fromisoformat(date)
        except ValueError:
            raise WaiverError(f"{where}: waivers[{index}] date is not ISO-8601") from None
    elif not isinstance(date, dt.date):
        raise WaiverError(f"{where}: waivers[{index}] date must be a date")
    return Waiver(
        case_id=str(item["case_id"]),
        date=date,
        reason=reason.strip(),
        brain_alias=str(item["brain_alias"]),
        identity_hash=str(item["identity_hash"]),
        model_identifier=(
            str(item["model_identifier"]) if item.get("model_identifier") is not None else None
        ),
        candidate_hash=str(item.get("candidate_hash", "")),
        incumbent_hash=str(item.get("incumbent_hash", "")),
        sample_indexes=_indexes(item.get("sample_indexes", []), where, minimum=1),
        check_indexes=_indexes(item.get("check_indexes", []), where, minimum=0),
    )


def _indexes(value: Any, where: str, *, minimum: int) -> tuple[int, ...]:
    if not isinstance(value, list) or any(type(i) is not int or i < minimum for i in value):
        raise WaiverError(f"{where}: invalid waiver index scope")
    if len(set(value)) != len(value):
        raise WaiverError(f"{where}: duplicate waiver index scope")
    return tuple(value)


# --------------------------------------------------------------------------


def _failures(cases: list[dict[str, Any]]) -> list[DeterministicFailure]:
    out: list[DeterministicFailure] = []
    for case in cases:
        for sample in case.get("samples", []):
            failing = tuple(
                str(result.get("type"))
                for result in sample.get("check_results", [])
                if result.get("status") == "fail"
            )
            if failing:
                out.append(
                    DeterministicFailure(
                        case_id=str(case["case_id"]),
                        sample_index=int(sample.get("sample_index", 0)),
                        failing_checks=failing,
                        check_indexes=tuple(
                            i
                            for i, r in enumerate(sample["check_results"])
                            if r.get("status") == "fail"
                        ),
                    )
                )
    return out


def _apply_waivers(
    failures: tuple[DeterministicFailure, ...],
    waivers: tuple[Waiver, ...],
    *,
    brain_alias: str,
    identity_hash: str,
    cases: list[dict[str, Any]],
    candidate_hash: str,
    incumbent_hash: str,
) -> tuple[list[WaivedFailure], list[DeterministicFailure], list[RejectedWaiver]]:
    known_cases = {str(case["case_id"]) for case in cases}
    failing_cases = {failure.case_id for failure in failures}

    valid: dict[tuple[str, int], Waiver] = {}
    failure_map = {(f.case_id, f.sample_index): f for f in failures}
    models = {
        (c["case_id"], s["sample_index"]): s.get("model_identifier")
        for c in cases
        for s in c["samples"]
    }
    rejected: list[RejectedWaiver] = []
    for waiver in waivers:
        if waiver.identity_hash != identity_hash:
            rejected.append(
                RejectedWaiver(
                    waiver,
                    f"identity hash {waiver.identity_hash} is not the identity under test "
                    f"({identity_hash}); a waiver does not survive an identity change",
                )
            )
            continue
        if waiver.brain_alias != brain_alias:
            rejected.append(
                RejectedWaiver(
                    waiver, f"waiver is for {waiver.brain_alias}, this run is {brain_alias}"
                )
            )
            continue
        if waiver.case_id not in known_cases:
            rejected.append(RejectedWaiver(waiver, "no such case in this run"))
            continue
        if waiver.case_id not in failing_cases:
            rejected.append(RejectedWaiver(waiver, "case did not fail in this run"))
            continue
        keys = [(waiver.case_id, i) for i in waiver.sample_indexes]
        if (
            waiver.candidate_hash != candidate_hash
            or waiver.incumbent_hash != incumbent_hash
            or not incumbent_hash
            or not keys
            or not waiver.check_indexes
            or len(set(keys)) != len(keys)
            or len(set(waiver.check_indexes)) != len(waiver.check_indexes)
            or any(type(i) is not int or i < 1 for i in waiver.sample_indexes)
            or any(type(i) is not int or i < 0 for i in waiver.check_indexes)
            or not review_module.valid_date(waiver.date.isoformat())
            or len(waiver.reason.strip()) < MIN_REASON_CHARS
            or any(
                key not in failure_map
                or tuple(sorted(waiver.check_indexes)) != failure_map[key].check_indexes
                or models[key] != waiver.model_identifier
                for key in keys
            )
        ):
            rejected.append(
                RejectedWaiver(waiver, "stale or out-of-scope artifact/sample/check/model waiver")
            )
            continue
        if any(key in valid for key in keys):
            rejected.append(RejectedWaiver(waiver, "duplicate/conflicting waiver scope"))
            continue
        valid.update({key: waiver for key in keys})

    waived: list[WaivedFailure] = []
    unwaived: list[DeterministicFailure] = []
    for failure in failures:
        matched = valid.get((failure.case_id, failure.sample_index))
        if matched is None:
            unwaived.append(failure)
        else:
            waived.append(WaivedFailure(failure=failure, waiver=matched))
    return waived, unwaived, rejected


def _manual_cases(cases: list[dict[str, Any]]) -> list[ManualCase]:
    out: list[ManualCase] = []
    for case in cases:
        rubrics: list[str] = []
        samples = 0
        for sample in case.get("samples", []):
            manual = [r for r in sample.get("check_results", []) if r.get("type") == "manual"]
            if manual:
                samples += 1
                for result in manual:
                    rubric = (result.get("expected") or {}).get("rubric")
                    if rubric and rubric not in rubrics:
                        rubrics.append(str(rubric))
        if samples:
            out.append(
                ManualCase(
                    case_id=str(case["case_id"]),
                    rubric=" | ".join(rubrics),
                    sample_count=samples,
                )
            )
    return out
