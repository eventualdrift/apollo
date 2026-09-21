"""`apollo eval diff` — inspection tooling, not judgement (spec J.2).

The diff answers one question: *what changed in Apollo's behaviour between
these two runs?* It produces no score, no pass rate and no winner. A number
would invite optimising the number; the point is that a human reads the two
responses side by side and decides whether Apollo is still Apollo.

Two mismatches are surfaced before anything else, because they invalidate the
comparison rather than participate in it: a different identity hash means the
two runs were different Apollos, and a different `bundle_hash` for the same
case means the two models were not asked the same thing (spec F.5).
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any

#: Why a case appears in the diff. Ordered by how much it invalidates.
REASON_MISSING = "missing_in_one_run"
REASON_BUNDLE_HASH = "bundle_hash_mismatch"
REASON_STATUS = "deterministic_status_changed"
REASON_CHECK = "check_status_changed"
REASON_RESPONSE = "response_changed"
REASON_SAMPLES = "sample_count_changed"


@dataclass(frozen=True)
class CheckDelta:
    sample_index: int
    check_index: int
    check_type: str
    old_status: str | None
    new_status: str | None
    old_observed: Any = None
    new_observed: Any = None

    @property
    def changed(self) -> bool:
        return self.old_status != self.new_status


@dataclass(frozen=True)
class SamplePair:
    """One sample from each run, kept whole so manual cases stay readable."""

    sample_index: int
    old_response: str | None
    new_response: str | None
    old_status: str | None
    new_status: str | None

    @property
    def response_changed(self) -> bool:
        return self.old_response != self.new_response


@dataclass(frozen=True)
class CaseDiff:
    case_id: str
    reasons: tuple[str, ...]
    old_status: str | None
    new_status: str | None
    old_bundle_hash: str | None
    new_bundle_hash: str | None
    samples: tuple[SamplePair, ...] = ()
    check_deltas: tuple[CheckDelta, ...] = ()
    manual_samples: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.reasons)

    @property
    def bundle_hash_match(self) -> bool:
        return self.old_bundle_hash == self.new_bundle_hash

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "changed": self.changed,
            "reasons": list(self.reasons),
            "old_deterministic_status": self.old_status,
            "new_deterministic_status": self.new_status,
            "old_bundle_hash": self.old_bundle_hash,
            "new_bundle_hash": self.new_bundle_hash,
            "bundle_hash_match": self.bundle_hash_match,
            "manual_samples": self.manual_samples,
            "check_deltas": [
                {
                    "sample_index": d.sample_index,
                    "check_index": d.check_index,
                    "check_type": d.check_type,
                    "old_status": d.old_status,
                    "new_status": d.new_status,
                    "old_observed": d.old_observed,
                    "new_observed": d.new_observed,
                }
                for d in self.check_deltas
            ],
            "samples": [
                {
                    "sample_index": s.sample_index,
                    "old_status": s.old_status,
                    "new_status": s.new_status,
                    "old_response": s.old_response,
                    "new_response": s.new_response,
                }
                for s in self.samples
            ],
        }


@dataclass
class RunDiff:
    run_a: dict[str, Any]
    run_b: dict[str, Any]
    cases: list[CaseDiff] = field(default_factory=list)
    only_in_a: tuple[str, ...] = ()
    only_in_b: tuple[str, ...] = ()

    @property
    def identity_hash_match(self) -> bool:
        return self.run_a.get("identity_hash") == self.run_b.get("identity_hash")

    @property
    def estimator_match(self) -> bool:
        return self.run_a.get("token_estimator") == self.run_b.get("token_estimator")

    @property
    def compiler_version_match(self) -> bool:
        return self.run_a.get("compiler_version") == self.run_b.get("compiler_version")

    @property
    def changed_cases(self) -> list[CaseDiff]:
        return [case for case in self.cases if case.changed]

    @property
    def unchanged_cases(self) -> list[CaseDiff]:
        return [case for case in self.cases if not case.changed]

    @property
    def bundle_hash_mismatches(self) -> list[CaseDiff]:
        return [case for case in self.cases if not case.bundle_hash_match]

    @property
    def comparison_valid(self) -> bool:
        """Whether the two runs are comparable at all.

        Not a verdict on either model: a mismatch means the experiment was not
        controlled, so the responses below are not evidence of anything.
        """
        return (
            self.identity_hash_match
            and self.estimator_match
            and not self.bundle_hash_mismatches
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_a": _meta(self.run_a),
            "run_b": _meta(self.run_b),
            "identity_hash_match": self.identity_hash_match,
            "token_estimator_match": self.estimator_match,
            "compiler_version_match": self.compiler_version_match,
            "comparison_valid": self.comparison_valid,
            "only_in_a": list(self.only_in_a),
            "only_in_b": list(self.only_in_b),
            "changed": [case.as_dict() for case in self.changed_cases],
            "unchanged": [case.case_id for case in self.unchanged_cases],
        }

    def as_lines(self) -> list[str]:
        a, b = _meta(self.run_a), _meta(self.run_b)
        lines = [
            f"A  {a['brain_alias']}  {a['model_identifiers']}  {a['started_at']}",
            f"B  {b['brain_alias']}  {b['model_identifiers']}  {b['started_at']}",
        ]
        if not self.identity_hash_match:
            lines += [
                "",
                "!! IDENTITY HASH MISMATCH — these runs are different Apollos.",
                f"   A: {self.run_a.get('identity_hash')}",
                f"   B: {self.run_b.get('identity_hash')}",
                "   Behavioural differences below cannot be attributed to the model.",
            ]
        if not self.estimator_match:
            lines += [
                "",
                "!! TOKEN ESTIMATOR MISMATCH — the two runs compiled different contexts.",
                f"   A: {self.run_a.get('token_estimator')}"
                f"   B: {self.run_b.get('token_estimator')}",
            ]
        if self.bundle_hash_mismatches:
            lines += ["", "!! BUNDLE HASH MISMATCH — the models were not asked the same thing:"]
            for case in self.bundle_hash_mismatches:
                lines.append(
                    f"   {case.case_id}: A={case.old_bundle_hash} B={case.new_bundle_hash}"
                )
        if self.only_in_a or self.only_in_b:
            lines += [
                "",
                f"only in A: {list(self.only_in_a)}",
                f"only in B: {list(self.only_in_b)}",
            ]

        lines += [
            "",
            f"changed cases: {len(self.changed_cases)}"
            f"   unchanged: {len(self.unchanged_cases)}",
        ]
        for case in self.changed_cases:
            lines += ["", f"--- {case.case_id}  [{', '.join(case.reasons)}]",
                      f"    deterministic: {case.old_status} -> {case.new_status}"]
            for delta in case.check_deltas:
                lines.append(
                    f"    check[{delta.check_index}] {delta.check_type} "
                    f"(sample {delta.sample_index}): {delta.old_status} -> {delta.new_status}"
                )
                if delta.old_observed != delta.new_observed:
                    lines.append(f"      observed A: {_short(delta.old_observed)}")
                    lines.append(f"      observed B: {_short(delta.new_observed)}")
            for sample in case.samples:
                marker = "  (unchanged)" if not sample.response_changed else ""
                lines.append(f"    sample {sample.sample_index}{marker}")
                lines.append(f"      A: {_short(sample.old_response)}")
                lines.append(f"      B: {_short(sample.new_response)}")
            if case.manual_samples:
                lines.append(
                    f"    {case.manual_samples} manual sample(s) above are for human reading; "
                    "no automatic grade"
                )
        if self.unchanged_cases:
            lines += ["", "unchanged: " + ", ".join(c.case_id for c in self.unchanged_cases)]
        lines += ["", "No score, no winner. Read the responses."]
        return lines


def diff_runs(run_a: dict[str, Any], run_b: dict[str, Any]) -> RunDiff:
    cases_a = {case["case_id"]: case for case in run_a.get("cases", [])}
    cases_b = {case["case_id"]: case for case in run_b.get("cases", [])}
    diff = RunDiff(run_a=run_a, run_b=run_b)
    diff.only_in_a = tuple(sorted(set(cases_a) - set(cases_b)))
    diff.only_in_b = tuple(sorted(set(cases_b) - set(cases_a)))
    for case_id in sorted(set(cases_a) & set(cases_b)):
        diff.cases.append(_diff_case(case_id, cases_a[case_id], cases_b[case_id]))
    return diff


def _diff_case(case_id: str, a: dict[str, Any], b: dict[str, Any]) -> CaseDiff:
    samples_a = {s["sample_index"]: s for s in a.get("samples", [])}
    samples_b = {s["sample_index"]: s for s in b.get("samples", [])}
    indexes = sorted(set(samples_a) | set(samples_b))

    pairs: list[SamplePair] = []
    deltas: list[CheckDelta] = []
    manual_samples = 0
    for index in indexes:
        sa, sb = samples_a.get(index), samples_b.get(index)
        pairs.append(
            SamplePair(
                sample_index=index,
                old_response=(sa or {}).get("response"),
                new_response=(sb or {}).get("response"),
                old_status=(sa or {}).get("deterministic_status"),
                new_status=(sb or {}).get("deterministic_status"),
            )
        )
        checks_a = (sa or {}).get("check_results", [])
        checks_b = (sb or {}).get("check_results", [])
        manual_samples += sum(1 for c in checks_a if c.get("type") == "manual")
        for position in range(max(len(checks_a), len(checks_b))):
            ca = checks_a[position] if position < len(checks_a) else None
            cb = checks_b[position] if position < len(checks_b) else None
            delta = CheckDelta(
                sample_index=index,
                check_index=position,
                check_type=str((ca or cb or {}).get("type", "?")),
                old_status=(ca or {}).get("status"),
                new_status=(cb or {}).get("status"),
                old_observed=(ca or {}).get("observed"),
                new_observed=(cb or {}).get("observed"),
            )
            if delta.changed:
                deltas.append(delta)

    reasons: list[str] = []
    if len(samples_a) != len(samples_b):
        reasons.append(REASON_SAMPLES)
    if a.get("bundle_hash") != b.get("bundle_hash"):
        reasons.append(REASON_BUNDLE_HASH)
    if a.get("deterministic_status") != b.get("deterministic_status"):
        reasons.append(REASON_STATUS)
    if deltas:
        reasons.append(REASON_CHECK)
    if any(pair.response_changed for pair in pairs):
        reasons.append(REASON_RESPONSE)

    return CaseDiff(
        case_id=case_id,
        reasons=tuple(reasons),
        old_status=a.get("deterministic_status"),
        new_status=b.get("deterministic_status"),
        old_bundle_hash=a.get("bundle_hash"),
        new_bundle_hash=b.get("bundle_hash"),
        samples=tuple(pairs),
        check_deltas=tuple(deltas),
        manual_samples=manual_samples,
    )


def load_run_document(path: pathlib.Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def _meta(run: dict[str, Any]) -> dict[str, Any]:
    models = sorted(
        {
            str(sample.get("model_identifier"))
            for case in run.get("cases", [])
            for sample in case.get("samples", [])
            if sample.get("model_identifier")
        }
    )
    return {
        "run_id": run.get("run_id"),
        "started_at": run.get("started_at"),
        "brain_alias": run.get("brain_alias"),
        "provider_key": run.get("provider_key"),
        "adapter_key": run.get("adapter_key"),
        "render_version": run.get("render_version"),
        "compiler_version": run.get("compiler_version"),
        "token_estimator": run.get("token_estimator"),
        "identity_hash": run.get("identity_hash"),
        "identity_version": run.get("identity_version"),
        "model_identifiers": models,
    }


def _short(value: Any, limit: int = 400) -> str:
    if value is None:
        return "<none>"
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + f"… (+{len(text) - limit} chars)"
