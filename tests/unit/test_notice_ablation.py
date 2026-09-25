"""retrieval-notice-ablation-1: isolation, one-variable proof, ceiling, bindings.

Offline only. The real invocation path is exercised against PostgreSQL in
tests/integration/test_notice_ablation_run.py.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import re
import uuid
from datetime import UTC, datetime

import pytest

from apollo.brains.base import render_chat
from apollo.brains.fake import FakeBrain
from apollo.brains.registry import BrainRegistry
from apollo.config import (
    MODE_BENCHMARK,
    MODE_PERSONAL,
    SURFACE_EVAL,
    SURFACE_INTERACTIVE,
    Config,
    ProviderConfig,
)
from apollo.context.bundle import BlockType, Region
from apollo.context.compiler import NO_RETRIEVAL_NOTICE
from apollo.context.estimator import CONSERVATIVE
from apollo.context.rules import COMPILER_VERSION
from apollo.core.identity import IdentityLoader, compose_identity
from apollo.evals import notice_ablation as na
from apollo.evals.loader import load_cases
from apollo.evals.models import SampleRecord
from apollo.evals.runner import compile_case_bundle

REPO = pathlib.Path(__file__).resolve().parents[2]
CASES_DIR = REPO / "evals/persona/cases"
COMMITTED_DIFF = REPO / "evals/ablations/retrieval-notice-ablation-1/structural-diff.json"
NOW = datetime(2026, 9, 25, tzinfo=UTC)

#: Production bundle hash and rendered-prompt hash for every corpus case,
#: captured from compiler-v1 / chat-v1 *before* the ablation existed (at
#: b3090b91, whose src/ equals a4c75492). Identical at context 9216 and 16384.
GOLDEN_FILE = pathlib.Path(__file__).with_name("golden_production_bundles.json")


def _golden() -> dict[str, tuple[str, ...]]:
    return {k: tuple(v) for k, v in json.loads(GOLDEN_FILE.read_text()).items()}


@pytest.fixture(scope="module")
def cases():
    return load_cases(CASES_DIR)


@pytest.fixture(scope="module")
def identity():
    return compose_identity(REPO / "identity")


# -- production is byte-for-byte unchanged -----------------------------------


@pytest.mark.parametrize("max_context", [9216, 16384])
def test_production_compiler_output_is_byte_for_byte_unchanged(cases, identity, max_context):
    golden = _golden()
    assert len(golden) == len(cases) == 30
    for case in cases:
        bundle = compile_case_bundle(
            case, identity=identity, max_context=max_context, identity_cap=4000, now=NOW,
            provider=ProviderConfig(key="p", kind="fake", allowed_modes=(MODE_BENCHMARK,),
                                    eval_only=True, context_budget=8000, reserved_output=1024),
        )
        assert bundle.compiler_version == COMPILER_VERSION == "compiler-v1"
        assert (bundle.bundle_hash, render_chat(bundle).prompt_hash) == golden[case.id], case.id


def test_production_notice_text_is_the_historical_one():
    assert hashlib.sha256(NO_RETRIEVAL_NOTICE.encode()).hexdigest() == (
        na.definition()["baseline_notice_sha256"]
    )
    assert "Substantive matches: 0" in NO_RETRIEVAL_NOTICE  # unchanged debt, not fixed here


def test_the_historical_baseline_bundles_are_reproduced(cases, identity):
    for model in na.MODELS.values():
        for case in na.select_cases(cases):
            baseline, _ = na.compile_pair(case, identity=identity, model=model, now=NOW)
            assert baseline.bundle_hash == na.HISTORICAL_BASELINE_BUNDLE_HASHES[case.id]


# -- isolation: eval/benchmark only -----------------------------------------


def test_personal_mode_cannot_derive_the_candidate(cases, identity):
    case = na.select_cases(cases)[0]
    baseline, _ = na.compile_pair(case, identity=identity, model=na.QWEN, now=NOW)
    with pytest.raises(na.AblationError, match="eval-only"):
        na.derive_candidate_bundle(baseline, conversation_mode=MODE_PERSONAL,
                                   budget=na.budget_for(na.QWEN))


def test_nothing_outside_evals_and_the_cli_can_reach_the_candidate():
    src = REPO / "src/apollo"
    for path in src.rglob("*.py"):
        rel = path.relative_to(src).parts
        if rel[0] in {"evals", "cli"}:
            continue
        text = path.read_text(encoding="utf-8")
        assert "notice_ablation" not in text, path
        assert na.CANDIDATE_COMPILER_VERSION not in text, path
        assert "Memory retrieval status" not in text, path


def test_the_interactive_turn_path_compiles_only_through_production():
    turns = (REPO / "src/apollo/core/turns.py").read_text(encoding="utf-8")
    assert "compile_context(" in turns
    assert "evals" not in turns


def test_the_runner_refuses_a_non_eval_registry(tmp_path):
    config = _config(tmp_path)
    with pytest.raises(ValueError, match="eval-surface"):
        na.NoticeAblationRunner(None, config, BrainRegistry(config, surface=SURFACE_INTERACTIVE),
                                IdentityLoader(REPO / "identity"))


def test_preflight_refuses_a_provider_that_does_not_serve_benchmark(tmp_path, identity, cases):
    config = _config(tmp_path, allowed_modes=(MODE_PERSONAL,))
    plan = _plan(tmp_path, identity, cases)
    failures = na.preflight(config=config, registry=_registry(config), identity=identity,
                            plan=plan, model_key="qwen", out_dir=tmp_path / "x", cases=cases)
    assert any("policy refused" in f for f in failures)


# -- scope, text and ceiling -------------------------------------------------


def test_exactly_the_five_cases(cases):
    assert na.CASE_IDS == (
        "per_020_retrieval_failure_vs_emptiness",
        "per_015_answer_change_under_repeated_pressure",
        "per_014_no_fabricated_memory",
        "per_012_dont_know_stands_alone",
        "per_016_absence_is_not_denial",
    )
    assert [c.id for c in na.select_cases(cases)] == list(na.CASE_IDS)
    assert set(na.HISTORICAL_BASELINE_BUNDLE_HASHES) == set(na.CASE_IDS)


@pytest.mark.parametrize("phrase", na.FORBIDDEN_NOTICE_PHRASES)
def test_candidate_has_no_empty_search_or_instruction_phrase(phrase):
    assert phrase not in na.CANDIDATE_RETRIEVAL_NOTICE.lower()


def test_candidate_contains_no_imperative_or_address():
    imperative = {"say", "tell", "answer", "report", "do", "don't", "never", "treat", "use",
                  "reason", "ignore", "state", "explain", "respond", "avoid", "note"}
    sentences = [s.strip() for s in re.split(r"[.\n]", na.CANDIDATE_RETRIEVAL_NOTICE) if s.strip()]
    assert sentences
    for sentence in sentences:
        assert sentence.split()[0].lower().strip(":") not in imperative, sentence
    assert not re.search(r"\byou(r)?\b", na.CANDIDATE_RETRIEVAL_NOTICE, re.I)


def test_candidate_states_the_required_semantics():
    text = na.CANDIDATE_RETRIEVAL_NOTICE
    assert "Memory retrieval status: unavailable for this turn." in text
    assert "Memory search performed: no." in text
    assert "No conclusion about stored memories can be drawn from this status." in text
    assert "Conversation history is separate from memory retrieval" in text
    for absent in ("no conversation history", "history is empty", "history is unavailable"):
        assert absent not in text.lower()


def test_generation_ceiling_is_ten_with_no_retry_or_replacement():
    d = na.definition()
    assert na.MAX_NEW_GENERATIONS == 10
    assert na.MAX_GENERATIONS_PER_MODEL * len(na.MODELS) == 10
    assert (d["samples_per_case"], d["retries"], d["replacement_generations"],
            d["gate1_generations"]) == (1, 0, 0, 0)


def test_experiment_identifier_is_distinct_and_versioned():
    assert na.EXPERIMENT_ID == "retrieval-notice-ablation-1"
    assert na.CANDIDATE_COMPILER_VERSION == "compiler-v1-rn1" != COMPILER_VERSION


# -- one-variable proof -------------------------------------------------------


def test_structural_diff_changes_only_the_notice(cases, identity):
    document = na.structural_diff_document(cases, identity)
    for case_id in na.CASE_IDS:
        for report in document["cases"][case_id].values():
            assert report["violations"] == []
            assert [c["block_type"] for c in report["changed_blocks"]] == ["RETRIEVAL_NOTICE"]
            assert set(report["changed_blocks"][0]["changed"]) <= {
                "content", "source_ref", "token_estimate"}
            unchanged = report["unchanged"]
            for key in ("identity_hash", "policy_region", "history_region", "request_region",
                        "block_order_and_regions", "trust_and_taint", "truncation",
                        "final_message_outside_notice_fence"):
                assert unchanged[key] is True, (case_id, key)
            assert report["candidate_compiler_version"] == "compiler-v1-rn1"


def test_candidate_bundles_are_identical_under_both_models_contexts(cases, identity):
    document = na.structural_diff_document(cases, identity)
    for case_id in na.CASE_IDS:
        pair = document["cases"][case_id]
        assert pair["qwen"]["candidate_bundle_hash"] == pair["gpt-oss"]["candidate_bundle_hash"]


def test_committed_structural_diff_artefact_is_current(cases, identity):
    committed = json.loads(COMMITTED_DIFF.read_text(encoding="utf-8"))
    assert committed == json.loads(json.dumps(na.structural_diff_document(cases, identity)))


@pytest.mark.parametrize("region", [Region.POLICY, Region.HISTORY, Region.REQUEST])
def test_any_other_change_stops_the_proof(cases, identity, region):
    case = next(c for c in na.select_cases(cases) if c.id.startswith("per_015"))
    baseline, candidate = na.compile_pair(case, identity=identity, model=na.QWEN, now=NOW)
    blocks = list(candidate.blocks)
    index = next(i for i, b in enumerate(blocks) if b.region is region)
    blocks[index] = dataclasses.replace(blocks[index], content=blocks[index].content + " x")
    tampered = dataclasses.replace(candidate, blocks=tuple(blocks))
    with pytest.raises(na.AblationIsolationError):
        na.structural_diff(baseline, tampered)


def test_truncation_change_is_refused(cases, identity):
    case = next(c for c in na.select_cases(cases) if c.id.startswith("per_015"))
    baseline, candidate = na.compile_pair(case, identity=identity, model=na.QWEN, now=NOW)
    exact = dataclasses.replace(na.budget_for(na.QWEN), total=candidate.total_token_estimate)
    na.derive_candidate_bundle(baseline, conversation_mode=MODE_BENCHMARK, budget=exact)
    # One token less and the candidate's history would no longer all fit.
    tight = dataclasses.replace(exact, total=candidate.total_token_estimate - 1)
    with pytest.raises(na.AblationIsolationError, match="truncation"):
        na.derive_candidate_bundle(baseline, conversation_mode=MODE_BENCHMARK, budget=tight)


def test_candidate_notice_is_not_larger_than_production():
    assert CONSERVATIVE.count(na.CANDIDATE_RETRIEVAL_NOTICE) <= CONSERVATIVE.count(
        NO_RETRIEVAL_NOTICE)


# -- frozen model configurations ---------------------------------------------


def test_model_configurations_are_frozen():
    assert na.GENERATION_PARAMS == {"temperature": 0.0, "max_tokens": 1024, "seed": 7}
    assert na.QWEN.context_settings == {"context_budget": 8000, "reserved_output": 1024,
                                        "max_context": 9216, "identity_cap": 4000}
    assert na.GPT_OSS.context_settings == {"context_budget": 8000, "reserved_output": 1024,
                                           "max_context": 16384, "identity_cap": 4000}
    pins = na.QWEN.runtime_pins
    assert pins["revision"] == "4da05a8edb55c6046cce958586c33b61da07bb79"
    assert pins["gpu_memory_utilization"] == "0.80"
    assert pins["enable_thinking"] == "false"
    assert pins["environment"] == "VLLM_USE_FLASHINFER_SAMPLER=0"
    assert pins["cpu_offload"] == "none"
    assert "exec" in pins["tmp"].split(",")
    assert na.GPT_OSS.runtime_pins["llama_server_sha256"].startswith("ed9e282e")
    with pytest.raises(dataclasses.FrozenInstanceError):
        na.QWEN.brain_alias = "brain.other"  # type: ignore[misc]


def test_reasoning_difference_is_recorded_not_matched():
    d = na.definition()
    assert d["models"]["gpt-oss"]["reasoning"]["reasoning_budget_tokens"] == 16
    assert d["models"]["qwen"]["reasoning"]["enable_thinking"] is False
    assert "differ" in d["reasoning_caveat"] and "within-model" in d["reasoning_caveat"]


# -- historical evidence ------------------------------------------------------


def test_historical_artefact_with_the_wrong_hash_is_refused_and_untouched(tmp_path):
    artefact = tmp_path / "gpt-oss-run.json"
    artefact.write_text('{"run_id": "x"}\n')
    before = artefact.read_bytes()
    with pytest.raises(na.AblationError, match="SHA-256"):
        na.bind_historical_baseline(na.GPT_OSS, artefact)
    assert artefact.read_bytes() == before


def test_historical_binding_reads_without_mutating(tmp_path):
    artefact, model = _artefact(tmp_path, na.QWEN)
    before = artefact.read_bytes()
    bound = na.bind_historical_baseline(model, artefact)
    assert artefact.read_bytes() == before
    assert list(bound["cases"]) == list(na.CASE_IDS)
    assert bound["cases"]["per_015_answer_change_under_repeated_pressure"][0]["response"] == (
        "historical qwen per_015")


def test_plan_seal_detects_tampering(tmp_path, identity, cases):
    plan = _plan(tmp_path, identity, cases)
    sealed = na.seal(plan)
    na.verify_plan(plan, sealed)
    plan["models"]["qwen"]["candidate_bundle_hashes"]["per_012_dont_know_stands_alone"] = "0"
    with pytest.raises(na.AblationError, match="seal"):
        na.verify_plan(plan, sealed)


# -- the run loop: one attempt per case, ledgered, never retried ---------------


def test_run_makes_one_attempt_per_case_and_never_retries(tmp_path, identity, cases, monkeypatch):
    plan = _plan(tmp_path, identity, cases)
    config = _config(tmp_path)
    registry = _registry(config)
    calls: list[tuple[str, int]] = []

    def fake_sample(self, *, case, sample_index, **kwargs):
        calls.append((case.id, sample_index))
        failed = case.id.startswith("per_020")  # a failure consumes its attempt
        return SampleRecord(case_id=case.id, sample_index=sample_index,
                            status="failed" if failed else "completed",
                            response=None if failed else "ok", error_kind="brain_unavailable"
                            if failed else None, turn_id=uuid.uuid4(),
                            invocation_id=uuid.uuid4(), bundle_hash="h")

    monkeypatch.setattr(na.NoticeAblationRunner, "_run_sample", fake_sample)
    monkeypatch.setattr(na, "create_benchmark_conversation", lambda db, now, title: uuid.uuid4())
    monkeypatch.setattr(na, "reconcile", lambda db, **kw: {"consistent": True})
    out = tmp_path / "exp"
    document = na.run_candidate(db=None, config=config, registry=registry,
                                identity_loader=IdentityLoader(REPO / "identity"), plan=plan,
                                model_key="qwen", cases=cases, out_dir=out,
                                clock=lambda: NOW)
    assert calls == [(case_id, 1) for case_id in na.CASE_IDS]
    assert document["provider_generations"] == 5
    assert document["run"]["compiler_version"] == "compiler-v1-rn1"
    ledger = (out / "qwen-attempts.jsonl").read_text().splitlines()
    assert [json.loads(line)["event"] for line in ledger] == [
        "attempt_opening", "attempt_closed"] * 5
    with pytest.raises(na.AblationError, match="reserved"):
        na.run_candidate(db=None, config=config, registry=registry,
                         identity_loader=IdentityLoader(REPO / "identity"), plan=plan,
                         model_key="qwen", cases=cases, out_dir=out, clock=lambda: NOW)
    assert len(calls) == 5  # the refused second call made no attempt


def test_cli_plan_seals_offline_and_never_overwrites(tmp_path, monkeypatch, capsys):
    from apollo.cli.main import main

    paths, models = {}, {}
    for model in (na.GPT_OSS, na.QWEN):
        paths[model.key], models[model.key] = _artefact(tmp_path, model)
    monkeypatch.setattr(na, "MODELS", models)
    monkeypatch.delenv("APOLLO_DATABASE_DSN", raising=False)  # offline: no config needed
    monkeypatch.chdir(REPO)
    argv = ["eval", "notice-ablation", "plan", "--gpt-oss-run", str(paths["gpt-oss"]),
            "--qwen-run", str(paths["qwen"]), "--out"]

    assert main([*argv, str(tmp_path / "exp")]) == 0
    plan = json.loads((tmp_path / "exp/plan.json").read_text())
    na.verify_plan(plan, (tmp_path / "exp/plan.json.sha256").read_text().strip())
    assert plan["status"].startswith("prepared; no provider generation")
    assert main([*argv, str(tmp_path / "exp")]) == 1  # never overwritten

    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    argv[4] = str(bad)
    assert main([*argv, str(tmp_path / "exp2")]) == 1
    assert not (tmp_path / "exp2").exists()  # a refused plan leaves nothing behind
    assert "STOP" in capsys.readouterr().err


def test_run_refuses_an_unknown_model(tmp_path, identity, cases):
    with pytest.raises(na.AblationError, match="unknown model"):
        na.run_candidate(db=None, config=_config(tmp_path), registry=None, identity_loader=None,
                         plan={}, model_key="other", cases=cases, out_dir=tmp_path,
                         clock=lambda: NOW)


# -- helpers --------------------------------------------------------------------


class _FrozenContextBrain(FakeBrain):
    def __init__(self, max_context: int) -> None:
        super().__init__(key="qwen")
        self._frozen_max = max_context

    def capabilities(self):  # type: ignore[no-untyped-def]
        return dataclasses.replace(super().capabilities(), max_context=self._frozen_max)


def _config(tmp_path, allowed_modes=(MODE_BENCHMARK,)) -> Config:
    provider = ProviderConfig(key="qwen", kind="fake", allowed_modes=allowed_modes,
                              eval_only=True, context_budget=8000, reserved_output=1024)
    return Config(database_dsn="postgresql://unused", providers={"qwen": provider},
                  brain_aliases={"default": "qwen", "qwen": "qwen"},
                  identity_dir=REPO / "identity", identity_token_cap=4000)


def _registry(config: Config) -> BrainRegistry:
    registry = BrainRegistry(config, surface=SURFACE_EVAL)
    registry._cache["brain.qwen"] = _FrozenContextBrain(9216)
    return registry


def _artefact(tmp_path, model):
    document = {
        "run_id": model.historical_run_id,
        "compiler_version": COMPILER_VERSION,
        "brain_alias": model.brain_alias,
        "provider_key": model.provider_key,
        "generation_params": dict(na.GENERATION_PARAMS),
        "context_settings": dict(model.context_settings),
        "cases": [
            {"case_id": case_id, "samples": [{
                "sample_index": 1, "status": "completed", "error_kind": None,
                "finish_reason": "stop", "invocation_id": str(uuid.uuid4()),
                "model_identifier": f"{model.key}-served", "rendered_prompt_hash": "p",
                "bundle_hash": na.HISTORICAL_BASELINE_BUNDLE_HASHES[case_id],
                "response": f"historical {model.key} {case_id[:7]}",
            }]}
            for case_id in na.CASE_IDS
        ],
    }
    path = tmp_path / f"{model.key}-{model.historical_artefact_name}"
    path.write_text(json.dumps(document))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, dataclasses.replace(model, historical_artefact_sha256=digest)


def _plan(tmp_path, identity, cases):
    """A sealed plan over synthetic stand-ins for the two preserved artefacts."""
    paths, models = {}, {}
    for model in (na.GPT_OSS, na.QWEN):
        paths[model.key], models[model.key] = _artefact(tmp_path, model)
    mp = pytest.MonkeyPatch()
    mp.setattr(na, "MODELS", models)
    try:
        plan = na.build_plan(cases=cases, identity=identity, historical=paths)
    finally:
        mp.undo()
    # The run path checks the plan against the real frozen models; bind those.
    plan["definition"] = na.definition()
    for key in plan["models"]:
        plan["models"][key]["frozen"] = na.MODELS[key].as_dict()
    return plan


def test_notice_block_stays_a_fenced_t0_data_block(cases, identity):
    case = na.select_cases(cases)[0]
    _, candidate = na.compile_pair(case, identity=identity, model=na.GPT_OSS, now=NOW)
    (notice,) = [b for b in candidate.blocks if b.block_type is BlockType.RETRIEVAL_NOTICE]
    assert (str(notice.trust_tier), notice.region) == ("T0", Region.DATA)
    last = render_chat(candidate).messages[-1].content
    assert last.startswith("<<<RETRIEVAL_NOTICE tier=T0 ref=compiler:compiler-v1-rn1>>>\n")


def test_preflight_passes_with_the_frozen_renderer(tmp_path, identity, cases):
    config = _config(tmp_path)
    plan = _plan(tmp_path, identity, cases)
    assert na.preflight(config=config, registry=_registry(config), identity=identity, plan=plan,
                        model_key="qwen", out_dir=tmp_path / "x", cases=cases) == []


class _NoSystemRoleBrain(_FrozenContextBrain):
    """An adapter configured with supports_system_role=false: policy moves to a user turn."""

    def capabilities(self):  # type: ignore[no-untyped-def]
        return dataclasses.replace(super().capabilities(), supports_system_role=False)

    def render(self, bundle):  # type: ignore[no-untyped-def]
        return render_chat(bundle, supports_system_role=False)


class _MislabelledRendererBrain(_FrozenContextBrain):
    """Claims the frozen renderer but renders differently: only the hash check sees it."""

    def render(self, bundle):  # type: ignore[no-untyped-def]
        return render_chat(bundle, supports_system_role=False)


@pytest.mark.parametrize(
    ("brain", "expected"),
    [(_NoSystemRoleBrain, ("renderer", "live rendered prompt")),
     (_MislabelledRendererBrain, ("live rendered prompt",))],
)
def test_preflight_refuses_a_renderer_that_changes_the_prompt(
    tmp_path, identity, cases, brain, expected
):
    config = _config(tmp_path)
    plan = _plan(tmp_path, identity, cases)
    registry = BrainRegistry(config, surface=SURFACE_EVAL)
    registry._cache["brain.qwen"] = brain(9216)
    failures = na.preflight(config=config, registry=registry, identity=identity, plan=plan,
                            model_key="qwen", out_dir=tmp_path / "x", cases=cases)
    for needle in expected:
        assert any(needle in f for f in failures), (needle, failures)
    assert sum("live rendered prompt" in f for f in failures) == len(na.CASE_IDS)


def test_frozen_renderer_is_outside_the_sealed_definition():
    assert "supports_system_role" not in json.dumps(na.definition())
    assert na.FROZEN_RENDERER == {"render_version": "chat-v1", "supports_system_role": True}


@pytest.mark.parametrize(("consistent", "code"), [(True, 0), (False, 1)])
def test_cli_run_exit_status_follows_reconciliation(tmp_path, monkeypatch, capsys,
                                                     consistent, code):
    from apollo.cli.main import main

    plan_dir = tmp_path / "exp"
    plan_dir.mkdir()
    (plan_dir / "plan.json").write_text("{}")
    (plan_dir / "plan.json.sha256").write_text("x\n")
    document = {
        "provider_generations": 5,
        "pairs": [{"candidate_notice": {"status": "completed"}}] * 5,
        "reconciliation": {"consistent": consistent,
                           "problems": [] if consistent else ["per_020: bundle_hash"]},
    }
    monkeypatch.setattr(na, "verify_plan", lambda plan, seal: None)
    monkeypatch.setattr(na, "run_candidate", lambda **kwargs: document)
    monkeypatch.setenv("APOLLO_DATABASE_DSN", "postgresql://unused")
    monkeypatch.chdir(REPO)
    assert main(["eval", "notice-ablation", "run", "--plan", str(plan_dir),
                 "--model", "qwen"]) == code
    err = capsys.readouterr().err
    assert ("RECONCILIATION INCONSISTENT" in err) is (not consistent)
