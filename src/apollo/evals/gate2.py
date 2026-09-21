"""Gate 2 — behavioural compatibility (spec G.5, ADR-0012).

Gate 1 says Apollo *can* use a model. Gate 2 says Apollo still behaves like
Apollo through it. The distinction this module exists to keep visible is that
a run completing is not a gate passing: a model can produce every output
successfully and fail the behavioural contract in all of them.

Three refusals are deliberate:

* no pass-rate threshold. Inventing one before the first real run would be a
  guess dressed as a target (spec O.1 criterion 29).
* no auto-generated waiver. A waiver is a human saying "this changed, I looked
  at it, and I accept it" with a date and a reason. Code that writes its own
  reason has removed the only thing a waiver contains.
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

from apollo.errors import ApolloError

#: Adapters that cannot constitute behavioural evidence about a real model.
OFFLINE_ADAPTER_KEYS = frozenset({"fake"})

WAIVER_FIELDS = {"case_id", "date", "reason", "brain_alias", "model_identifier", "identity_hash"}
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "date": self.date.isoformat(),
            "reason": self.reason,
            "brain_alias": self.brain_alias,
            "model_identifier": self.model_identifier,
            "identity_hash": self.identity_hash,
        }


@dataclass(frozen=True)
class DeterministicFailure:
    case_id: str
    sample_index: int
    failing_checks: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "sample_index": self.sample_index,
            "failing_checks": list(self.failing_checks),
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
        }

    def as_lines(self) -> list[str]:
        lines = [
            f"brain:               {self.brain_alias} ({self.adapter_key})",
            f"EVAL RUN COMPLETED:  {'yes' if self.run_completed else 'no'}",
            f"GATE 2:              {str(self.status).upper()}",
            f"cases/samples:       {self.case_count}/{self.sample_count}",
            f"identity hash:       {self.observed_identity_hash or '<none>'}",
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
            lines.append("waived (a human accepted these, with a date and a reason):")
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
                f"manual cases awaiting human review: {len(self.manual_cases)} "
                f"({total} samples). These are not graded."
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
) -> Gate2Report:
    """Evaluate one persona run against the Gate-2 conditions."""
    brain_alias = str(run.get("brain_alias", ""))
    adapter_key = str(run.get("adapter_key", ""))
    observed_identity = run.get("identity_hash")
    cases = run.get("cases", [])

    blocking: list[str] = []
    notes: list[str] = []

    if not cases:
        blocking.append("the run contains no cases")

    if observed_identity != identity_hash:
        blocking.append(
            f"identity hash mismatch: run has {observed_identity}, "
            f"current identity is {identity_hash}"
        )

    samples = [sample for case in cases for sample in case.get("samples", [])]
    run_completed = bool(samples) and all(s.get("status") == "completed" for s in samples)
    if not run_completed:
        failed = [s for s in samples if s.get("status") != "completed"]
        blocking.append(f"{len(failed)} of {len(samples)} samples did not complete")

    for case in cases:
        if not case.get("bundle_hash_stable_across_samples", True):
            blocking.append(
                f"{case['case_id']}: bundle hash differed across samples "
                f"({case.get('bundle_hashes_seen')})"
            )
        elif case.get("bundle_hash") is None and case.get("samples"):
            blocking.append(f"{case['case_id']}: no bundle hash recorded")

    failures = tuple(_failures(cases))
    waived, unwaived, rejected = _apply_waivers(
        failures, waivers, brain_alias=brain_alias, identity_hash=identity_hash, cases=cases
    )
    for failure in unwaived:
        blocking.append(
            f"{failure.case_id} sample {failure.sample_index}: deterministic check(s) "
            f"{', '.join(failure.failing_checks)} failed with no waiver"
        )

    manual_cases = tuple(_manual_cases(cases))
    if manual_cases:
        notes.append(
            f"{len(manual_cases)} manual case(s) are recorded for human review and are "
            "not part of the mechanical result"
        )

    mechanically_eligible = not blocking
    real_model_evidence = adapter_key not in OFFLINE_ADAPTER_KEYS

    if not cases:
        status = Gate2Status.NOT_RUN
    elif not mechanically_eligible:
        status = Gate2Status.FAILED
    elif not real_model_evidence:
        status = Gate2Status.INFRASTRUCTURE_ONLY
        notes.append(
            f"adapter {adapter_key!r} is offline and deterministic: this run proves the "
            "evaluation machinery, not Apollo's behaviour through a real model"
        )
    else:
        status = Gate2Status.PASSED

    return Gate2Report(
        brain_alias=brain_alias,
        adapter_key=adapter_key,
        status=status,
        run_completed=run_completed,
        expected_identity_hash=identity_hash,
        observed_identity_hash=observed_identity if isinstance(observed_identity, str) else None,
        mechanically_eligible=mechanically_eligible,
        real_model_evidence=real_model_evidence,
        blocking=tuple(blocking),
        failures=failures,
        waived=tuple(waived),
        rejected_waivers=tuple(rejected),
        manual_cases=manual_cases,
        case_count=len(cases),
        sample_count=len(samples),
        notes=tuple(notes),
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
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return parse_waivers(document, where=path.name)


def parse_waivers(document: Any, *, where: str = "<waivers>") -> tuple[Waiver, ...]:
    if document is None:
        return ()
    if not isinstance(document, dict) or "waivers" not in document:
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
    )


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
) -> tuple[list[WaivedFailure], list[DeterministicFailure], list[RejectedWaiver]]:
    known_cases = {str(case["case_id"]) for case in cases}
    failing_cases = {failure.case_id for failure in failures}

    valid: dict[str, Waiver] = {}
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
        valid[waiver.case_id] = waiver

    waived: list[WaivedFailure] = []
    unwaived: list[DeterministicFailure] = []
    for failure in failures:
        matched = valid.get(failure.case_id)
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
