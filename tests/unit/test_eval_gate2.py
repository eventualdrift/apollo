"""Gate-2 self-tests (brief §35) and the replaceability surface (§28).

Synthetic run records throughout: the point is the gate's logic, and a real
model would make these tests non-deterministic for no gain.
"""

from __future__ import annotations

import datetime as dt
import textwrap
from typing import Any

import pytest

from apollo.evals.gate2 import (
    Gate2Status,
    Waiver,
    WaiverError,
    compare_bundle_hashes,
    evaluate_gate2,
    load_waivers,
    not_run,
    parse_waivers,
)
from apollo.evals.replaceability import NOT_RUN, build_report

IDENTITY = "a" * 64
OTHER_IDENTITY = "d" * 64


def check(check_type: str, status: str) -> dict[str, Any]:
    return {"type": check_type, "status": status, "observed": {}, "expected": {}, "note": ""}


def case(
    *,
    case_id: str = "per_007",
    statuses: tuple[str, ...] = ("pass",),
    samples: int = 1,
    bundle_hash: str | None = "h1",
    stable: bool = True,
    manual: bool = False,
    sample_status: str = "completed",
) -> dict[str, Any]:
    entries = []
    for index in range(1, samples + 1):
        results = [check("forbidden_phrases", statuses[min(index - 1, len(statuses) - 1)])]
        if manual:
            results.append(
                {
                    "type": "manual",
                    "status": "recorded",
                    "observed": {"response": "text"},
                    "expected": {"rubric": "Did Apollo hold the position?"},
                    "note": "",
                }
            )
        entries.append(
            {
                "sample_index": index,
                "status": sample_status,
                "error_kind": None if sample_status == "completed" else "brain_transport",
                "response": "No." if sample_status == "completed" else None,
                "check_results": results if sample_status == "completed" else [],
                "deterministic_status": statuses[min(index - 1, len(statuses) - 1)],
                "bundle_hash": bundle_hash,
                "model_identifier": "model-x",
            }
        )
    return {
        "case_id": case_id,
        "bundle_hash": bundle_hash,
        "bundle_hash_stable_across_samples": stable,
        "bundle_hashes_seen": [bundle_hash] if stable else ["h1", "h2"],
        "samples": entries,
        "deterministic_status": "fail" if "fail" in statuses else "pass",
    }


def run(
    *,
    brain: str = "brain.local",
    adapter: str = "openai_compatible",
    identity: str = IDENTITY,
    cases: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "brain_alias": brain,
        "adapter_key": adapter,
        "identity_hash": identity,
        "identity_version": "2026.09.20-1",
        "token_estimator": "conservative-v1",
        "cases": cases if cases is not None else [case()],
    }


def waiver(**kwargs: Any) -> Waiver:
    defaults = {
        "case_id": "per_007",
        "date": dt.date(2026, 9, 21),
        "reason": "Shorter phrasing, same position; reviewed and accepted.",
        "brain_alias": "brain.local",
        "identity_hash": IDENTITY,
    }
    defaults.update(kwargs)
    return Waiver(**defaults)  # type: ignore[arg-type]


# -- the mechanical conditions --------------------------------------------


def test_all_checks_pass_makes_the_gate_mechanically_eligible() -> None:
    report = evaluate_gate2(run(), identity_hash=IDENTITY)
    assert report.mechanically_eligible
    assert report.blocking == ()
    assert report.status is Gate2Status.PASSED
    assert report.run_completed


def test_one_failure_with_no_waiver_fails_the_gate() -> None:
    report = evaluate_gate2(run(cases=[case(statuses=("fail",))]), identity_hash=IDENTITY)
    assert report.status is Gate2Status.FAILED
    assert not report.mechanically_eligible
    assert any("no waiver" in reason for reason in report.blocking)
    # The run itself completed. That is a different fact.
    assert report.run_completed


def test_one_failure_with_a_valid_waiver_is_recorded_as_waived() -> None:
    report = evaluate_gate2(
        run(cases=[case(statuses=("fail",))]), identity_hash=IDENTITY, waivers=(waiver(),)
    )
    assert report.status is Gate2Status.PASSED
    assert len(report.waived) == 1
    assert report.waived[0].waiver.reason.startswith("Shorter phrasing")
    assert report.rejected_waivers == ()
    assert len(report.failures) == 1, "the failure is still on the record, not erased"


def test_a_waiver_from_a_different_identity_is_rejected() -> None:
    report = evaluate_gate2(
        run(cases=[case(statuses=("fail",))]),
        identity_hash=IDENTITY,
        waivers=(waiver(identity_hash=OTHER_IDENTITY),),
    )
    assert report.status is Gate2Status.FAILED
    assert report.waived == ()
    assert "identity hash" in report.rejected_waivers[0].reason


def test_a_waiver_for_the_wrong_case_is_rejected() -> None:
    report = evaluate_gate2(
        run(cases=[case(statuses=("fail",))]),
        identity_hash=IDENTITY,
        waivers=(waiver(case_id="per_999"),),
    )
    assert report.status is Gate2Status.FAILED
    assert "no such case" in report.rejected_waivers[0].reason


def test_a_waiver_for_the_wrong_brain_is_rejected() -> None:
    report = evaluate_gate2(
        run(cases=[case(statuses=("fail",))]),
        identity_hash=IDENTITY,
        waivers=(waiver(brain_alias="brain.reference"),),
    )
    assert report.status is Gate2Status.FAILED
    assert "brain.reference" in report.rejected_waivers[0].reason


def test_a_waiver_for_a_case_that_passed_is_rejected() -> None:
    report = evaluate_gate2(run(), identity_hash=IDENTITY, waivers=(waiver(),))
    assert report.rejected_waivers[0].reason == "case did not fail in this run"
    assert report.status is Gate2Status.PASSED


def test_an_identity_mismatch_blocks_the_gate() -> None:
    report = evaluate_gate2(run(identity=OTHER_IDENTITY), identity_hash=IDENTITY)
    assert report.status is Gate2Status.FAILED
    assert any("identity hash mismatch" in reason for reason in report.blocking)


def test_an_unstable_bundle_hash_blocks_the_gate() -> None:
    report = evaluate_gate2(
        run(cases=[case(samples=3, stable=False, manual=True)]), identity_hash=IDENTITY
    )
    assert report.status is Gate2Status.FAILED
    assert any("bundle hash differed" in reason for reason in report.blocking)


def test_an_incomplete_sample_blocks_the_gate() -> None:
    report = evaluate_gate2(
        run(cases=[case(sample_status="failed")]), identity_hash=IDENTITY
    )
    assert not report.run_completed
    assert report.status is Gate2Status.FAILED


def test_a_manual_only_case_manufactures_no_deterministic_verdict() -> None:
    entry = case(manual=True, samples=3)
    for sample in entry["samples"]:
        sample["check_results"] = [c for c in sample["check_results"] if c["type"] == "manual"]
        sample["deterministic_status"] = "no_deterministic_checks"
    entry["deterministic_status"] = "no_deterministic_checks"
    report = evaluate_gate2(run(cases=[entry]), identity_hash=IDENTITY)
    assert report.failures == ()
    assert len(report.manual_cases) == 1
    assert report.manual_cases[0].sample_count == 3
    assert "Did Apollo hold the position?" in report.manual_cases[0].rubric
    assert report.mechanically_eligible


def test_a_missing_run_is_not_run_and_never_passes() -> None:
    report = not_run("brain.reference", "credentials unavailable")
    assert report.status is Gate2Status.NOT_RUN
    assert not report.passed
    assert report.blocking == ("credentials unavailable",)


def test_an_empty_run_is_not_run() -> None:
    report = evaluate_gate2(run(cases=[]), identity_hash=IDENTITY)
    assert report.status is Gate2Status.NOT_RUN
    assert not report.passed


def test_the_fake_brain_never_passes_gate_two_however_green() -> None:
    """Fake proves the machinery. It is not evidence about Apollo."""
    report = evaluate_gate2(run(brain="brain.fake", adapter="fake"), identity_hash=IDENTITY)
    assert report.mechanically_eligible
    assert not report.real_model_evidence
    assert report.status is Gate2Status.INFRASTRUCTURE_ONLY
    assert not report.passed
    assert any("offline" in note for note in report.notes)


def test_run_completion_and_gate_status_are_reported_separately() -> None:
    report = evaluate_gate2(run(cases=[case(statuses=("fail",))]), identity_hash=IDENTITY)
    rendered = "\n".join(report.as_lines())
    assert "EVAL RUN COMPLETED:  yes" in rendered
    assert "GATE 2:              FAILED" in rendered


def test_no_percentage_threshold_exists_anywhere_in_the_gate() -> None:
    import inspect

    from apollo.evals import gate2

    source = inspect.getsource(gate2)
    assert "%" not in source.replace("100%", "")
    assert "pass_rate" not in source
    assert "threshold" not in source.lower().replace("no pass-rate threshold", "")


# -- cross-brain bundle equality ------------------------------------------


def test_equal_bundle_hashes_make_the_comparison_valid() -> None:
    result = compare_bundle_hashes(
        {"brain.fake": run(brain="brain.fake"), "brain.local": run()}
    )
    assert result["all_equal"]
    assert result["comparison_valid"]


def test_a_bundle_hash_mismatch_makes_the_comparison_invalid() -> None:
    result = compare_bundle_hashes(
        {
            "brain.fake": run(brain="brain.fake"),
            "brain.local": run(cases=[case(bundle_hash="h2")]),
        }
    )
    assert not result["comparison_valid"]
    assert result["mismatched_cases"] == ["per_007"]


# -- waiver parsing --------------------------------------------------------


def test_a_waiver_needs_a_written_reason() -> None:
    document = {
        "waivers": [
            {
                "case_id": "per_007",
                "date": "2026-09-21",
                "reason": "ok",
                "brain_alias": "brain.local",
                "identity_hash": IDENTITY,
            }
        ]
    }
    with pytest.raises(WaiverError, match="written reason"):
        parse_waivers(document)


def test_a_waiver_missing_a_required_field_is_rejected() -> None:
    document = {"waivers": [{"case_id": "per_007", "date": "2026-09-21"}]}
    with pytest.raises(WaiverError, match="missing"):
        parse_waivers(document)


def test_waivers_load_from_disk(tmp_path) -> None:
    path = tmp_path / "waivers.yaml"
    path.write_text(
        textwrap.dedent(
            f"""\
            waivers:
              - case_id: per_007
                date: 2026-09-21
                reason: Reviewed; shorter phrasing, position unchanged.
                brain_alias: brain.local
                model_identifier: some-model
                identity_hash: {IDENTITY}
            """
        ),
        encoding="utf-8",
    )
    (loaded,) = load_waivers(path)
    assert loaded.case_id == "per_007"
    assert loaded.date == dt.date(2026, 9, 21)
    assert loaded.model_identifier == "some-model"


def test_a_missing_waiver_file_is_simply_no_waivers(tmp_path) -> None:
    assert load_waivers(tmp_path / "absent.yaml") == ()


def test_the_gate_never_writes_a_waiver_reason() -> None:
    """Waivers are a human act. Nothing here may manufacture one.

    Asserted structurally: a `Waiver` is constructed in exactly one function,
    the parser, from a file a person wrote.
    """
    import ast
    import inspect

    from apollo.evals import gate2

    tree = ast.parse(inspect.getsource(gate2))
    built_in: list[str] = []
    for function in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(function):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Waiver":
                built_in.append(function.name)
    assert built_in == ["_parse_waiver"], built_in


# -- replaceability --------------------------------------------------------


def test_an_unavailable_brain_is_not_run_never_pass() -> None:
    report = build_report(
        {"brain.fake": run(brain="brain.fake", adapter="fake")},
        unavailable={"brain.local": "endpoint unavailable"},
    )
    assert report.brains["brain.fake"] == "RUN"
    assert report.brains["brain.local"].startswith(NOT_RUN)
    assert "endpoint unavailable" in report.brains["brain.local"]
    assert report.brains["brain.reference"] == NOT_RUN
    cells = {cell.alias: cell for cell in report.rows[0].cells}
    assert cells["brain.local"].status == NOT_RUN
    assert cells["brain.reference"].status == NOT_RUN
    rendered = "\n".join(report.as_lines())
    for line in rendered.splitlines():
        if "brain.local" in line or "brain.reference" in line:
            assert NOT_RUN in line and "pass" not in line.lower(), line


def test_the_report_states_bundle_equality_per_case() -> None:
    report = build_report(
        {
            "brain.fake": run(brain="brain.fake", adapter="fake"),
            "brain.local": run(cases=[case(bundle_hash="h2")]),
        }
    )
    assert report.bundle_hash_mismatches == ["per_007"]
    assert not report.rows[0].bundle_hash_equal
    assert not report.rows[0].comparable


def test_one_brain_alone_is_not_a_comparison() -> None:
    report = build_report({"brain.fake": run(brain="brain.fake", adapter="fake")})
    assert report.brains_run == ["brain.fake"]
    assert not report.rows[0].comparable
    assert "no comparison to read yet" in "\n".join(report.as_lines())


def test_the_replaceability_report_has_no_rank_or_score() -> None:
    report = build_report(
        {
            "brain.fake": run(brain="brain.fake", adapter="fake"),
            "brain.local": run(),
        }
    )
    rendered = "\n".join(report.as_lines()).lower()
    assert "no rank, no score, no overall winner." in rendered
    for forbidden in ("winner:", "score:", "rank:", "best "):
        assert forbidden not in rendered
