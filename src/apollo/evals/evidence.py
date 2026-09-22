"""Acceptance evidence, not a second runner. No provider calls or database access.

Hashes use the same canonical JSON convention as context.bundle. A content hash
binds evidence; it authenticates neither a provider nor a human author.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from apollo.context.estimator import CONSERVATIVE
from apollo.context.rules import COMPILER_VERSION
from apollo.core.identity import Identity
from apollo.errors import ApolloError
from apollo.evals.checks import run_checks
from apollo.evals.models import PersonaCase

# Spec J.3: retain all repeated manual observations, never pick the best one.
MANUAL_SAMPLES = 3
DETERMINISTIC_SAMPLES = 1
DEFAULT_CASES_DIR = pathlib.Path(__file__).resolve().parents[3] / "evals/persona/cases"


class EvidenceError(ApolloError):
    pass


def document_hash(document: Any) -> str:
    canonical = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_document(path: pathlib.Path) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise EvidenceError("duplicate JSON key in evidence")
            result[key] = value
        return result

    document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    if not isinstance(document, dict):
        raise EvidenceError("evidence must be a JSON object")
    document_hash(document)  # reject NaN/Infinity, which are not JSON evidence
    return document


def required_samples(case: PersonaCase) -> int:
    return MANUAL_SAMPLES if case.has_manual_check else DETERMINISTIC_SAMPLES


def case_definition(case: PersonaCase) -> dict[str, Any]:
    definition = asdict(case)
    definition.pop("source_file")  # a file rename alone does not change the experiment
    return dict(json.loads(json.dumps(definition)))


def corpus_hash(cases: list[PersonaCase]) -> str:
    return document_hash(
        {
            "cases": [case_definition(c) for c in sorted(cases, key=lambda c: c.id)],
            "manual_samples": MANUAL_SAMPLES,
            "deterministic_samples": DETERMINISTIC_SAMPLES,
        }
    )


class StrictEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ContextSettings(StrictEvidence):
    context_budget: int = Field(gt=0)
    reserved_output: int = Field(ge=0)
    max_context: int = Field(gt=0)
    identity_cap: int = Field(gt=0)


class GenerationSettings(StrictEvidence):
    temperature: float = Field(ge=0, le=0)
    max_tokens: int = Field(gt=0)
    seed: int | None


class DeterminismEvidence(StrictEvidence):
    temperature: float
    seed: int | None
    provider_claims_seed_support: bool
    repeatability_observed: bool | None
    note: str


class SampleEvidence(StrictEvidence):
    sample_index: int = Field(gt=0)
    status: Literal["completed"]
    error_kind: None
    response: str = Field(min_length=1)
    check_results: list[dict[str, Any]]
    deterministic_status: str
    turn_id: str = Field(min_length=1)
    invocation_id: str = Field(min_length=1)
    bundle_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_identifier: str = Field(min_length=1)
    rendered_prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    finish_reason: str = Field(min_length=1)
    prompt_tokens: int | None = Field(ge=0)
    completion_tokens: int | None = Field(ge=0)
    reasoning_tokens: int | None = Field(ge=0)
    latency_ms: int = Field(ge=0)


class CaseEvidence(StrictEvidence):
    case_id: str
    source_file: str
    tags: list[str]
    behavioural_expectation: str
    undesired_characteristics: list[str]
    covers_rules: list[str]
    covers_probes: list[str]
    input_context: dict[str, Any]
    case_definition: dict[str, Any]
    bundle_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    bundle_hash_stable_across_samples: Literal[True]
    bundle_hashes_seen: list[str]
    samples: list[SampleEvidence]
    deterministic_status: str


class RunEvidence(StrictEvidence):
    suite_version: Literal[2]
    run_id: str = Field(min_length=1)
    started_at: str
    brain_alias: str = Field(min_length=1)
    provider_key: str = Field(min_length=1)
    adapter_key: str = Field(min_length=1)
    render_version: str = Field(min_length=1)
    compiler_version: str
    token_estimator: str
    identity_version: str
    identity_hash: str
    corpus_hash: str
    evidence_kind: Literal["provider_generation", "offline"]
    context_settings: ContextSettings
    generation_params: GenerationSettings
    determinism: DeterminismEvidence
    conversation_id: str = Field(min_length=1)
    cases: list[CaseEvidence]


@dataclass
class EvidenceValidation:
    problems: list[str] = field(default_factory=list)
    real_model: bool = False
    completed: bool = False
    document: dict[str, Any] | None = None

    @property
    def valid(self) -> bool:
        return not self.problems


def validate_run(
    document: dict[str, Any], *, cases: list[PersonaCase], identity: Identity
) -> EvidenceValidation:
    """Recompute every check and context hash from the current case definitions."""
    from apollo.evals.runner import compile_case_bundle

    result = EvidenceValidation()
    try:
        run = RunEvidence.model_validate(document)
        started = datetime.fromisoformat(run.started_at)
        if started.tzinfo is None:
            raise ValueError
        document_hash(document)
    except (ValidationError, ValueError, TypeError):
        result.problems.append("invalid or incomplete run schema (acceptance requires version 2)")
        return result
    result.document = run.model_dump()
    if any(c.get("bundle_hash_stable_across_samples") is not True for c in document["cases"]):
        result.problems.append("bundle stability must be explicitly true")
    result.completed = bool(run.cases) and all(c.samples for c in run.cases)
    result.real_model = run.evidence_kind == "provider_generation" and not any(
        _offline(value) for value in (run.adapter_key, run.brain_alias, run.provider_key)
    )
    if not result.real_model:
        result.problems.append(
            "offline/fake/replayed evidence is not real-model acceptance evidence"
        )
    if (run.identity_hash, run.identity_version) != (identity.content_hash, identity.version_label):
        result.problems.append("identity hash/version mismatch")
    if run.compiler_version != COMPILER_VERSION:
        result.problems.append("stale compiler version")
    if run.token_estimator != CONSERVATIVE.name:
        result.problems.append("acceptance requires the common conservative-v1 token estimator")
    if run.corpus_hash != corpus_hash(cases):
        result.problems.append("corpus definition/check configuration mismatch")
    expected = {c.id: c for c in cases}
    ids = [c.case_id for c in run.cases]
    if len(expected) != len(cases) or not expected:
        result.problems.append("expected corpus is empty or contains duplicate cases")
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        result.problems.append("missing, unexpected or duplicate cases; full corpus required")
    seen_invocations: set[str] = set()
    seen_turns: set[str] = set()
    models: set[str] = set()
    for entry in run.cases:
        if entry.case_id not in expected:
            continue
        case = expected[entry.case_id]
        prefix = f"{case.id}: "
        definition = case_definition(case)
        if document_hash(entry.case_definition) != document_hash(definition) or any(
            getattr(entry, key) != definition[key]
            for key in (
                "tags",
                "behavioural_expectation",
                "undesired_characteristics",
                "covers_rules",
                "covers_probes",
            )
        ):
            result.problems.append(prefix + "case definition/check configuration mismatch")
        if entry.input_context != {
            "mode": case.mode,
            "history": definition["history"],
            "input": case.input,
        }:
            result.problems.append(prefix + "input context mismatch")
        indexes = [s.sample_index for s in entry.samples]
        if sorted(indexes) != list(range(1, required_samples(case) + 1)):
            result.problems.append(prefix + "missing, duplicate or unexpected sample indexes")
        try:
            settings = run.context_settings
            bundle = compile_case_bundle(
                case,
                identity=identity,
                provider=settings,
                max_context=settings.max_context,
                identity_cap=settings.identity_cap,
                now=started,
            )
        except ApolloError:
            result.problems.append(prefix + "recorded context settings cannot compile current case")
            continue
        if entry.bundle_hash != bundle.bundle_hash or entry.bundle_hashes_seen != [
            bundle.bundle_hash
        ]:
            result.problems.append(
                prefix + "bundle hash mismatch with reconstructed current context"
            )
        statuses = []
        for sample in entry.samples:
            if sample.invocation_id in seen_invocations or sample.turn_id in seen_turns:
                result.problems.append(prefix + "duplicate invocation or turn evidence")
            seen_invocations.add(sample.invocation_id)
            seen_turns.add(sample.turn_id)
            models.add(sample.model_identifier)
            if _offline(sample.model_identifier):
                result.real_model = False
                result.problems.append(prefix + "offline/fake model output")
            if sample.bundle_hash != bundle.bundle_hash:
                result.problems.append(prefix + "sample bundle hash mismatch")
            if not all(
                value.strip()
                for value in (
                    sample.response,
                    sample.model_identifier,
                    sample.finish_reason,
                    sample.turn_id,
                    sample.invocation_id,
                )
            ):
                result.problems.append(prefix + "empty response or generation metadata")
            recomputed = [r.as_dict() for r in run_checks(case.checks, sample.response, case.input)]
            if document_hash(sample.check_results) != document_hash(recomputed):
                result.problems.append(prefix + "omitted or inconsistent check/manual results")
            status = deterministic_status(recomputed)
            statuses.append(status)
            if sample.deterministic_status != status:
                result.problems.append(
                    prefix + "supplied sample status contradicts recomputed checks"
                )
        if entry.deterministic_status != aggregate_status(statuses):
            result.problems.append(prefix + "supplied case status contradicts recomputed checks")
    if len(models) != 1:
        result.problems.append("a run must identify one consistent answering model")
    params = run.generation_params
    repeated = [len({s.response for s in c.samples}) == 1 for c in run.cases if len(c.samples) > 1]
    if (
        run.determinism.temperature != params.temperature
        or run.determinism.seed != params.seed
        or (run.determinism.provider_claims_seed_support and params.seed is None)
        or run.determinism.repeatability_observed != (all(repeated) if repeated else None)
    ):
        result.problems.append("missing or inconsistent determinism availability")
    return result


def deterministic_status(results: list[dict[str, Any]]) -> str:
    statuses = [r["status"] for r in results if r["type"] != "manual"]
    return "fail" if "fail" in statuses else "pass" if statuses else "no_deterministic_checks"


def aggregate_status(statuses: list[str]) -> str:
    return (
        "fail"
        if "fail" in statuses
        else ("no_deterministic_checks" if set(statuses) == {"no_deterministic_checks"} else "pass")
    )


def _offline(value: str) -> bool:
    parts = value.lower().replace("/", ".").replace(":", ".").replace("-", ".").split(".")
    return any(part in {"fake", "offline", "replay", "scripted", "echo"} for part in parts)
