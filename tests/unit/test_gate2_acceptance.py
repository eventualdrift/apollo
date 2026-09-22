"""Replacement acceptance regressions. All run/review evidence here is synthetic."""

import json
import pathlib
import uuid
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from apollo.context.rules import COMPILER_VERSION
from apollo.core.identity import IdentityLoader
from apollo.evals.checks import run_checks
from apollo.evals.diff import diff_runs
from apollo.evals.evidence import (
    DEFAULT_CASES_DIR,
    EvidenceError,
    corpus_hash,
    document_hash,
    load_document,
    required_samples,
    validate_run,
)
from apollo.evals.gate2 import Waiver, WaiverError, evaluate_gate2, load_waivers
from apollo.evals.loader import load_cases
from apollo.evals.models import (
    CheckSpec,
    CheckType,
    HistoryTurn,
    PersonaCase,
    RunRecord,
    SampleRecord,
)
from apollo.evals.runner import PersonaRunner, compile_case_bundle, to_document
from tests.unit.test_eval_gate2 import IDENTITY, case, run


def test_legacy_single_run_cannot_claim_replacement_acceptance() -> None:
    assert not evaluate_gate2(run(), identity_hash=IDENTITY).passed


def test_partial_corpus_cannot_claim_acceptance() -> None:
    document = run(cases=[case(case_id="per_007")])
    assert not evaluate_gate2(document, identity_hash=IDENTITY).passed


def test_unreviewed_manual_samples_cannot_claim_acceptance() -> None:
    document = run(cases=[case(manual=True, samples=3)])
    assert not evaluate_gate2(document, identity_hash=IDENTITY).passed


def test_response_only_changes_cannot_pass_without_review() -> None:
    incumbent = run()
    candidate = deepcopy(incumbent)
    candidate["cases"][0]["samples"][0]["response"] = "A different visible answer."
    assert diff_runs(incumbent, candidate).changed_cases[0].reasons == ("response_changed",)
    assert not evaluate_gate2(candidate, identity_hash=IDENTITY).passed


def test_omitting_all_required_checks_cannot_claim_acceptance() -> None:
    document = run()
    document["cases"][0]["samples"][0]["check_results"] = []
    assert not evaluate_gate2(document, identity_hash=IDENTITY).passed


# Synthetic evidence uses production assembly and the current identity, but no
# Brain, provider, database, or model call. No fixture is a real acceptance record.
@pytest.fixture()
def evidence():
    identity = IdentityLoader(DEFAULT_CASES_DIR.parents[2] / "identity").load()
    cases = [
        PersonaCase(
            id="synthetic_deterministic",
            tags=("synthetic",),
            behavioural_expectation="Avoid the synthetic forbidden phrase.",
            undesired_characteristics=(),
            mode="benchmark",
            history=(),
            input="Synthetic input.",
            checks=(CheckSpec(CheckType.FORBIDDEN_PHRASES, phrases=("forbidden",)),),
        ),
        PersonaCase(
            id="synthetic_manual",
            tags=("synthetic",),
            behavioural_expectation="Synthetic human rubric.",
            undesired_characteristics=(),
            mode="benchmark",
            history=(),
            input="Synthetic question.",
            checks=(
                CheckSpec(CheckType.MANUAL, rubric="Read all synthetic samples."),
                CheckSpec(CheckType.MANUAL, rubric="Check synthetic consistency."),
            ),
        ),
    ]
    incumbent = synthetic_run(cases, identity, "incumbent")
    candidate = synthetic_run(cases, identity, "candidate")
    receipt = {
        "acceptance_version": 1,
        "kind": "gate2_replacement",
        "status": "passed",
        "binding": {
            "candidate": {"run_id": incumbent["run_id"], "content_hash": document_hash(incumbent)},
            "incumbent": {"run_id": "synthetic-prior", "content_hash": "1" * 64},
            "identity_hash": identity.content_hash,
            "corpus_hash": corpus_hash(cases),
            "comparison_hash": "2" * 64,
            "waivers_hash": document_hash([]),
            "incumbent_acceptance_hash": "3" * 64,
        },
        "review_hash": "4" * 64,
        "reviewer": "SYNTHETIC TEST REVIEWER",
        "review_date": date.today().isoformat(),
    }
    return SimpleNamespace(
        identity=identity, cases=cases, incumbent=incumbent, candidate=candidate, receipt=receipt
    )


def synthetic_run(cases, identity, label, *, context_budget=8000):
    now = datetime(2026, 9, 22, tzinfo=UTC)
    settings = dict(
        context_budget=context_budget, reserved_output=1024, max_context=9024, identity_cap=4000
    )
    record = RunRecord(
        run_id=f"synthetic-{label}",
        started_at=now,
        brain_alias=f"brain.{label}",
        provider_key=label,
        adapter_key="openai_compatible",
        render_version="chat-v1",
        compiler_version=COMPILER_VERSION,
        token_estimator="conservative-v1",
        identity_version=identity.version_label,
        identity_hash=identity.content_hash,
        generation_params={"temperature": 0.0, "max_tokens": 1024, "seed": 7},
        determinism={
            "temperature": 0.0,
            "seed": 7,
            "provider_claims_seed_support": True,
            "repeatability_observed": True if any(c.has_manual_check for c in cases) else None,
            "note": "synthetic fixture, never empirical evidence",
        },
        conversation_id=uuid.uuid4(),
        corpus_hash=corpus_hash(cases),
        evidence_kind="provider_generation",
        context_settings=settings,
    )
    assembler = object.__new__(PersonaRunner)
    for case_spec in cases:
        bundle = compile_case_bundle(
            case_spec,
            identity=identity,
            provider=SimpleNamespace(**settings),
            max_context=settings["max_context"],
            identity_cap=settings["identity_cap"],
            now=now,
        )
        samples = [
            SampleRecord(
                case_id=case_spec.id,
                sample_index=i,
                status="completed",
                response="A synthetic answer.",
                check_results=run_checks(case_spec.checks, "A synthetic answer.", case_spec.input),
                turn_id=uuid.uuid4(),
                invocation_id=uuid.uuid4(),
                bundle_hash=bundle.bundle_hash,
                model_identifier=f"synthetic-{label}-model",
                latency_ms=1,
                rendered_prompt_hash=document_hash(label),
                finish_reason="stop",
            )
            for i in range(1, required_samples(case_spec) + 1)
        ]
        record.cases.append(assembler._case_entry(case_spec, samples, {bundle.bundle_hash}))
    return to_document(record)


def evaluate(evidence, **overrides):
    options = dict(
        identity_hash=evidence.identity.content_hash,
        identity=evidence.identity,
        cases=evidence.cases,
        incumbent=evidence.incumbent,
        incumbent_acceptance=evidence.receipt,
    )
    options.update(overrides)
    candidate = options.pop("candidate", evidence.candidate)
    return evaluate_gate2(candidate, **options)


def reviewed(evidence, **overrides):
    template = deepcopy(evaluate(evidence, **overrides).review_template)
    assert template is not None
    template.update(
        reviewer="SYNTHETIC TEST REVIEWER", review_date=date.today().isoformat(), diff_reviewed=True
    )
    for decision in template["decisions"]:
        decision.update(decision="accept", date=date.today().isoformat())
    return template


def answer(evidence, case_index, sample_index, text):
    """Update the visible observation and honest deterministic results together."""
    spec = evidence.cases[case_index]
    entry = evidence.candidate["cases"][case_index]
    sample = entry["samples"][sample_index]
    sample["response"] = text
    sample["check_results"] = [r.as_dict() for r in run_checks(spec.checks, text, spec.input)]
    from apollo.evals.evidence import aggregate_status, deterministic_status

    sample["deterministic_status"] = deterministic_status(sample["check_results"])
    entry["deterministic_status"] = aggregate_status(
        [s["deterministic_status"] for s in entry["samples"]]
    )
    repeats = [
        len({s["response"] for s in c["samples"]}) == 1
        for c in evidence.candidate["cases"]
        if len(c["samples"]) > 1
    ]
    evidence.candidate["determinism"]["repeatability_observed"] = all(repeats) if repeats else None


def scoped_waiver(evidence, **overrides):
    fields = dict(
        case_id=evidence.cases[0].id,
        date=date.today(),
        reason="Synthetic deviation explicitly acknowledged for this test only.",
        brain_alias=evidence.candidate["brain_alias"],
        identity_hash=evidence.identity.content_hash,
        model_identifier=evidence.candidate["cases"][0]["samples"][0]["model_identifier"],
        candidate_hash=document_hash(evidence.candidate),
        incumbent_hash=document_hash(evidence.incumbent),
        sample_indexes=(1,),
        check_indexes=(0,),
    )
    fields.update(overrides)
    return Waiver(**fields)


def test_complete_synthetic_comparison_requires_review_and_then_passes(evidence):
    pending = evaluate(evidence)
    assert pending.candidate_valid and pending.comparison_valid and pending.incumbent_authorised
    assert pending.mechanically_eligible and pending.run_completed
    assert not pending.review_complete and not pending.passed
    report = evaluate(evidence, review=reviewed(evidence))
    assert report.passed and report.review_complete
    assert report.acceptance_record["binding"]["candidate"]["content_hash"] == document_hash(
        evidence.candidate
    )
    assert "review complete" in "\n".join(report.as_lines())
    assert report.as_dict()["gate2_passed"] is True


def test_provider_adapter_model_and_render_differences_are_allowed(evidence):
    evidence.candidate.update(adapter_key="another_adapter", render_version="other-v2")
    assert evidence.candidate["provider_key"] != evidence.incumbent["provider_key"]
    assert (
        evidence.candidate["cases"][0]["samples"][0]["rendered_prompt_hash"]
        != evidence.incumbent["cases"][0]["samples"][0]["rendered_prompt_hash"]
    )
    assert evaluate(evidence, review=reviewed(evidence)).passed


@pytest.mark.parametrize("missing", ["incumbent", "incumbent_acceptance", "review"])
def test_mandatory_acceptance_evidence_cannot_be_omitted(evidence, missing):
    review = reviewed(evidence)
    options = {"review": review, missing: None}
    report = evaluate(evidence, **options)
    assert not report.passed and report.acceptance_record is None
    assert any("missing" in reason for reason in report.blocking)


def test_self_comparison_is_rejected(evidence):
    report = evaluate(evidence, incumbent=deepcopy(evidence.candidate))
    assert not report.comparison_valid
    assert any("self-comparison" in reason for reason in report.blocking)


def test_relabeling_a_replayed_run_does_not_make_it_a_distinct_observation(evidence):
    evidence.candidate = deepcopy(evidence.incumbent)
    evidence.candidate["run_id"] = "synthetic-relabelled"
    report = evaluate(evidence)
    assert not report.comparison_valid
    assert any("reused invocation" in reason for reason in report.blocking)


def test_same_valid_inputs_with_different_provider_capacity_remain_comparable(evidence):
    evidence.candidate["context_settings"]["max_context"] += 1000
    assert evaluate(evidence, review=reviewed(evidence)).passed


def test_valid_runs_with_different_retained_context_are_not_comparable(evidence):
    evidence.cases[0] = replace(
        evidence.cases[0],
        history=(HistoryTurn("user", "history " * 1500),)
        + tuple(HistoryTurn("user", "Short recent fixture turn.") for _ in range(4)),
    )
    evidence.incumbent = synthetic_run(evidence.cases, evidence.identity, "incumbent")
    evidence.candidate = synthetic_run(
        evidence.cases, evidence.identity, "candidate", context_budget=6000
    )
    report = evaluate(evidence)
    assert report.candidate_valid
    assert validate_run(evidence.incumbent, cases=evidence.cases, identity=evidence.identity).valid
    assert not report.comparison_valid and not report.passed
    assert report.comparison["changed"][0]["bundle_hash_match"] is False


def test_a_waiver_never_selects_only_the_favourable_manual_sample(evidence):
    evidence.cases[0] = replace(
        evidence.cases[0],
        checks=evidence.cases[0].checks
        + (CheckSpec(CheckType.MANUAL, rubric="Synthetic repeated deterministic case."),),
    )
    evidence.incumbent = synthetic_run(evidence.cases, evidence.identity, "incumbent")
    evidence.candidate = synthetic_run(evidence.cases, evidence.identity, "candidate")
    answer(evidence, 0, 0, "forbidden")
    answer(evidence, 0, 2, "forbidden")
    report = evaluate(evidence, waivers=(scoped_waiver(evidence),))
    assert len(report.failures) == 2 and len(report.waived) == 1
    assert not report.mechanically_eligible and not report.passed
    assert any("sample 3" in reason and "no waiver" in reason for reason in report.blocking)


@pytest.mark.parametrize(
    "path,value",
    [
        (("suite_version",), 1),
        (("suite_version",), True),
        (("identity_hash",), "0" * 64),
        (("identity_version",), "old"),
        (("corpus_hash",), "0" * 64),
        (("compiler_version",), "old"),
        (("token_estimator",), "provider-estimator"),
        (("started_at",), "2026-09-22"),
        (("context_settings", "context_budget"), 1),
        (("generation_params", "temperature"), 0.5),
        (("generation_params", "max_tokens"), True),
        (("determinism", "provider_claims_seed_support"), None),
        (("determinism", "repeatability_observed"), False),
        (("cases", 0, "case_id"), "unexpected"),
        (("cases", 0, "bundle_hash"), "0" * 64),
        (("cases", 0, "bundle_hashes_seen"), []),
        (("cases", 0, "bundle_hash_stable_across_samples"), False),
        (("cases", 0, "bundle_hash_stable_across_samples"), 1),
        (("cases", 0, "input_context", "input"), "Changed input"),
        (("cases", 0, "case_definition", "checks"), []),
        (("cases", 0, "samples", 0, "sample_index"), 0),
        (("cases", 0, "samples", 0, "sample_index"), True),
        (("cases", 0, "samples", 0, "status"), "failed"),
        (("cases", 0, "samples", 0, "error_kind"), "brain_transport"),
        (("cases", 0, "samples", 0, "response"), " "),
        (("cases", 0, "samples", 0, "check_results"), []),
        (("cases", 0, "samples", 0, "deterministic_status"), "fail"),
        (("cases", 0, "samples", 0, "bundle_hash"), "0" * 64),
        (("cases", 0, "samples", 0, "rendered_prompt_hash"), None),
        (("cases", 0, "samples", 0, "invocation_id"), None),
        (("cases", 0, "samples", 0, "finish_reason"), None),
        (("cases", 1, "samples", 0, "check_results"), []),
        (("cases", 1, "samples", 0, "check_results", 0, "observed"), {}),
        (("cases", 1, "samples", 0, "check_results", 0, "expected"), {}),
    ],
)
def test_corrupt_candidate_evidence_is_not_eligible(evidence, path, value):
    target = evidence.candidate
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    report = evaluate(evidence)
    assert not report.candidate_valid and not report.mechanically_eligible and not report.passed


@pytest.mark.parametrize(
    "alter",
    [
        lambda r: r["cases"].pop(),
        lambda r: r["cases"].append(deepcopy(r["cases"][0])),
        lambda r: r["cases"][1]["samples"].pop(),
        lambda r: r["cases"][1]["samples"].append(deepcopy(r["cases"][1]["samples"][0])),
        lambda r: r["cases"][1]["samples"][2].update(sample_index=4),
        lambda r: r["cases"][1]["samples"][2].update(sample_index=2),
        lambda r: r["cases"][1]["samples"][2].update(
            invocation_id=r["cases"][1]["samples"][0]["invocation_id"]
        ),
    ],
)
def test_missing_duplicate_or_extra_case_sample_or_invocation_fails(evidence, alter):
    alter(evidence.candidate)
    assert not evaluate(evidence).candidate_valid


@pytest.mark.parametrize(
    "field,value",
    [
        ("evidence_kind", "offline"),
        ("adapter_key", "fake"),
        ("adapter_key", "replay"),
        ("brain_alias", "brain.fake"),
        ("provider_key", "offline"),
    ],
)
def test_offline_provenance_cannot_be_presented_as_real(evidence, field, value):
    evidence.candidate[field] = value
    assert not evaluate(evidence).real_model_evidence
    assert not evaluate(evidence).passed


def test_offline_model_identifier_and_fake_incumbent_fail(evidence):
    for c in evidence.candidate["cases"]:
        for s in c["samples"]:
            s["model_identifier"] = "fake:replay"
    assert not evaluate(evidence).real_model_evidence
    evidence.incumbent["adapter_key"] = "fake"
    assert not evaluate(evidence).comparison_valid


def test_same_id_changed_definition_or_check_configuration_invalidates_evidence(evidence):
    for changed in [
        replace(evidence.cases[0], input="Changed current fixture"),
        replace(evidence.cases[0], checks=(CheckSpec(CheckType.MAX_WORDS, value=1),)),
    ]:
        assert not evaluate(evidence, cases=[changed, evidence.cases[1]]).candidate_valid


def test_supplied_pass_cannot_hide_a_recomputed_failure(evidence):
    evidence.candidate["cases"][0]["samples"][0]["response"] = "forbidden"
    report = evaluate(evidence)
    assert not report.candidate_valid and not report.passed
    assert any("inconsistent check" in reason for reason in report.blocking)


def test_different_generation_settings_invalidate_comparison(evidence):
    evidence.candidate["generation_params"]["max_tokens"] += 1
    report = evaluate(evidence)
    assert report.candidate_valid and not report.comparison_valid


@pytest.mark.parametrize("key", ["identity_hash", "corpus_hash", "token_estimator"])
def test_stale_incumbent_is_not_comparable(evidence, key):
    evidence.incumbent[key] = "stale"
    assert not evaluate(evidence).comparison_valid


def test_response_only_change_requires_decision_for_all_samples(evidence):
    answer(evidence, 0, 0, "A different visible answer.")
    answer(evidence, 1, 2, "Variation in the third manual sample.")
    pending = evaluate(evidence)
    assert pending.candidate_valid and pending.comparison_valid and not pending.passed
    requirements = {r["case_id"]: r for r in pending.review_template["requirements"]}
    assert requirements[evidence.cases[0].id]["change_reasons"] == ["response_changed"]
    assert requirements[evidence.cases[1].id]["sample_indexes"] == [1, 2, 3]
    assert len(requirements[evidence.cases[1].id]["manual_rubrics"]) == 2
    assert evaluate(evidence, review=reviewed(evidence)).passed


def test_unchanged_manual_cases_still_need_all_rubrics_and_samples(evidence):
    report = evaluate(evidence)
    assert report.comparison["changed"] == []
    assert report.review_template["requirements"][0]["case_id"] == evidence.cases[1].id
    assert not evaluate(evidence, review=report.review_template).passed
    review = reviewed(evidence)
    review["decisions"][0]["manual_check_indexes"].pop()
    assert not evaluate(evidence, review=review).passed


@pytest.mark.parametrize(
    "alter",
    [
        lambda r: r.update(review_version=99),
        lambda r: r.update(reviewer=" "),
        lambda r: r.update(review_date="invalid"),
        lambda r: r.update(diff_reviewed=None),
        lambda r: r["decisions"].clear(),
        lambda r: r["decisions"].append(deepcopy(r["decisions"][0])),
        lambda r: r["decisions"][0].update(case_id="outside-scope"),
        lambda r: r["decisions"][0].update(decision="reject"),
        lambda r: r["decisions"][0].update(decision=None),
        lambda r: r["decisions"][0].update(date="invalid"),
        lambda r: r["decisions"][0].update(date=(date.today() + timedelta(days=1)).isoformat()),
        lambda r: r["decisions"][0].update(sample_indexes=[1]),
        lambda r: r["decisions"][0].update(manual_check_indexes=[0]),
        lambda r: r["requirements"].clear(),
        lambda r: r["binding"]["candidate"].update(content_hash="0" * 64),
        lambda r: r["binding"]["incumbent"].update(content_hash="0" * 64),
        lambda r: r["binding"].update(comparison_hash="0" * 64),
        lambda r: r["binding"].update(incumbent_acceptance_hash="0" * 64),
    ],
)
def test_missing_stale_conflicting_or_out_of_scope_reviews_fail(evidence, alter):
    review = reviewed(evidence)
    alter(review)
    report = evaluate(evidence, review=review)
    assert not report.review_complete and not report.passed and report.acceptance_record is None


@pytest.mark.parametrize("which", ["candidate", "incumbent"])
def test_any_run_artifact_content_change_invalidates_prior_review(evidence, which):
    review = reviewed(evidence)
    getattr(evidence, which)["render_version"] = "new-render-version"
    report = evaluate(evidence, review=review)
    assert report.comparison_valid and not report.passed
    assert any("review binding is stale" in reason for reason in report.blocking)


@pytest.mark.parametrize(
    "alter",
    [
        lambda r: r.update(kind="first_baseline"),
        lambda r: r["binding"]["candidate"].update(content_hash="0" * 64),
        lambda r: r["binding"].update(incumbent_acceptance_hash=None),
        lambda r: r["binding"].update(incumbent=deepcopy(r["binding"]["candidate"])),
    ],
)
def test_incumbent_receipt_cannot_be_missing_stale_or_a_bootstrap_exception(evidence, alter):
    alter(evidence.receipt)
    report = evaluate(evidence, review=reviewed(evidence))
    assert not report.incumbent_authorised and not report.passed


def test_valid_scoped_waiver_retains_failure_and_requires_human_deviation_decision(evidence):
    answer(evidence, 0, 0, "forbidden")
    waivers = (scoped_waiver(evidence),)
    review = reviewed(evidence, waivers=waivers)
    assert not evaluate(evidence, waivers=waivers, review=review).passed
    review["decisions"][0].update(decision="waive", reason="Synthetic deviation knowingly waived.")
    report = evaluate(evidence, waivers=waivers, review=review)
    assert report.passed and len(report.failures) == 1 and len(report.waived) == 1
    assert not evaluate(evidence, review=review).passed


@pytest.mark.parametrize(
    "change",
    [
        {"case_id": "unknown"},
        {"brain_alias": "brain.other"},
        {"identity_hash": "0" * 64},
        {"model_identifier": "wrong-model"},
        {"candidate_hash": "0" * 64},
        {"incumbent_hash": "0" * 64},
        {"sample_indexes": ()},
        {"sample_indexes": (2,)},
        {"sample_indexes": (1, 1)},
        {"sample_indexes": (True,)},
        {"check_indexes": ()},
        {"check_indexes": (1,)},
        {"check_indexes": (False,)},
        {"date": date.today() + timedelta(days=1)},
        {"reason": ""},
    ],
)
def test_invalid_waivers_cannot_make_a_failure_eligible(evidence, change):
    answer(evidence, 0, 0, "forbidden")
    report = evaluate(evidence, waivers=(scoped_waiver(evidence, **change),))
    assert not report.mechanically_eligible and not report.passed and report.rejected_waivers


def test_duplicate_waivers_or_waivers_for_passed_cases_are_blocking(evidence):
    assert evaluate(evidence, waivers=(scoped_waiver(evidence),)).rejected_waivers
    answer(evidence, 0, 0, "forbidden")
    waiver = scoped_waiver(evidence)
    report = evaluate(evidence, waivers=(waiver, waiver))
    assert not report.passed and report.rejected_waivers


def test_manual_waiver_requires_reason_date_and_exact_binding(evidence):
    review = reviewed(evidence)
    review["decisions"][0]["decision"] = "waive"
    assert not evaluate(evidence, review=review).passed
    review["decisions"][0]["reason"] = "Synthetic manual deviation acknowledged."
    assert evaluate(evidence, review=review).passed


def test_default_api_uses_full_repository_cases_not_submitted_subset(evidence):
    report = evaluate_gate2(evidence.candidate, identity_hash=evidence.identity.content_hash)
    assert not report.candidate_valid
    assert any("full corpus" in reason for reason in report.blocking)


def test_current_whole_corpus_schema_uses_derived_counts(evidence):
    cases = load_cases(DEFAULT_CASES_DIR)
    document = synthetic_run(cases, evidence.identity, "whole-corpus")
    validation = validate_run(document, cases=cases, identity=evidence.identity)
    assert validation.valid, validation.problems
    assert sum(len(c["samples"]) for c in document["cases"]) == sum(
        required_samples(c) for c in cases
    )
    # Responses are synthetic; deterministic failures remain failures. No review or receipt issued.
    assert not evaluate_gate2(document, identity_hash=evidence.identity.content_hash).passed


def test_receipt_can_bind_next_replacement_but_is_not_a_baseline_issuer(evidence):
    passed = evaluate(evidence, review=reviewed(evidence))
    evidence.incumbent = evidence.candidate
    evidence.receipt = passed.acceptance_record
    evidence.candidate = synthetic_run(evidence.cases, evidence.identity, "next")
    assert evaluate(evidence, review=reviewed(evidence)).passed


@pytest.mark.parametrize("text", ['{"cases": [], "cases": []}', '{"cases": NaN}', "[]"])
def test_ambiguous_or_non_json_evidence_is_rejected(tmp_path, text):
    path = tmp_path / "invalid.json"
    path.write_text(text)
    with pytest.raises((EvidenceError, ValueError)):
        load_document(path)


def test_non_json_api_input_cannot_pass(evidence):
    evidence.candidate["unexpected"] = float("nan")
    assert not evaluate(evidence).passed


@pytest.mark.parametrize(
    "text", ["waivers: []\nwaivers: []\n", "waivers: [", "waivers: []\nunknown: []\n"]
)
def test_ambiguous_or_invalid_waiver_files_fail(tmp_path, text):
    path = tmp_path / "invalid.yaml"
    path.write_text(text)
    with pytest.raises(WaiverError):
        load_waivers(path)


def test_real_missing_incumbent_cannot_be_filled_by_a_receipt_only(evidence):
    report = evaluate(evidence, incumbent=None, review=reviewed(evidence))
    assert not report.passed and not report.incumbent_authorised


@pytest.fixture()
def cli_evidence(evidence, monkeypatch, tmp_path):
    from apollo.cli import main as cli
    from apollo.evals import corpus, loader

    monkeypatch.setattr(
        cli,
        "load_config",
        lambda: SimpleNamespace(
            identity_dir=DEFAULT_CASES_DIR.parents[2] / "identity", log_level="WARNING"
        ),
    )
    # A tiny trusted synthetic repository corpus, isolated to these CLI tests.
    monkeypatch.setattr(loader, "load_cases", lambda path: evidence.cases)
    monkeypatch.setattr(corpus, "coverage", lambda cases: SimpleNamespace(complete=True))
    artifacts = {
        "candidate": evidence.candidate,
        "incumbent": evidence.incumbent,
        "receipt": evidence.receipt,
        "review": reviewed(evidence),
    }
    paths = {}
    for name, document in artifacts.items():
        path = tmp_path / f"synthetic-{name}.json"
        path.write_text(json.dumps(document))
        paths[name] = str(path)
    return paths


def cli_args(paths):
    return [
        "eval",
        "gate2",
        paths["candidate"],
        "--incumbent",
        paths["incumbent"],
        "--incumbent-acceptance",
        paths["receipt"],
        "--review",
        paths["review"],
        "--json",
    ]


@pytest.mark.parametrize("option", ["--incumbent", "--incumbent-acceptance", "--review"])
def test_cli_missing_mandatory_evidence_is_nonzero(cli_evidence, option, capsys):
    from apollo.cli.main import main

    args = cli_args(cli_evidence)
    index = args.index(option)
    del args[index : index + 2]
    assert main(args) == 1
    assert json.loads(capsys.readouterr().out)["gate2_passed"] is False


def test_cli_old_single_run_invocation_cannot_accept(cli_evidence, capsys):
    from apollo.cli.main import main

    assert main(["eval", "gate2", cli_evidence["candidate"]]) == 1
    output = capsys.readouterr().out
    assert "missing incumbent" in output and "GATE 2:              FAILED" in output


def test_cli_template_is_pending_and_not_overwritten(cli_evidence, tmp_path, capsys):
    from apollo.cli.main import main

    path = tmp_path / "pending.json"
    args = cli_args(cli_evidence)
    index = args.index("--review")
    del args[index : index + 2]
    args += ["--review-template", str(path)]
    assert main(args) == 1
    document = load_document(path)
    assert document["reviewer"] is None and all(
        d["decision"] is None for d in document["decisions"]
    )
    assert main(args + ["--review", str(path)]) == 2  # refuses overwriting evidence
    assert load_document(path) == document
    args = args[:-2] + ["--review", str(path)]
    assert main(args) == 1


def test_cli_valid_synthetic_review_writes_receipt_only_on_pass(cli_evidence, tmp_path, capsys):
    from apollo.cli.main import main

    path = tmp_path / "synthetic-accepted.json"
    args = cli_args(cli_evidence) + ["--write-acceptance", str(path)]
    assert main(args) == 0
    assert load_document(path)["status"] == "passed"
    assert main(args) == 2  # no overwriting an earlier receipt
    path2 = tmp_path / "must-not-exist.json"
    assert main(["eval", "gate2", cli_evidence["candidate"], "--write-acceptance", str(path2)]) == 1
    assert not path2.exists()


def test_cli_malformed_json_does_not_echo_evidence(cli_evidence, capsys):
    from apollo.cli.main import main

    pathlib.Path(cli_evidence["candidate"]).write_text("PRIVATE_INVALID_EVIDENCE_SENTINEL")
    assert main(cli_args(cli_evidence)) == 2
    output = capsys.readouterr()
    assert "PRIVATE_INVALID_EVIDENCE_SENTINEL" not in output.out + output.err
