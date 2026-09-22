"""The persona runner.

Every eval generation goes through the same machinery an interactive turn uses
— `core.invocations.open_invocation` then `core.invocations.invoke` — because
that module is the only route into a Brain (ADR-0013). There is no evaluator
backdoor: a persona call is audited exactly like a conversation.

Two properties make comparison valid rather than merely possible:

* the estimator is pinned to `conservative-v1` for every brain (spec F.5), so
  each model receives the same compiled context rather than one budgeted to its
  own tokeniser;
* fixture history and the case input carry *deterministic* source references,
  so a case's `bundle_hash` is identical across brains and across samples.
  Referencing the message rows instead would make every run's hash unique and
  quietly invalidate the experiment.
"""

from __future__ import annotations

import json
import logging
import pathlib
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from apollo.audit.events import Actor, AuditEvent, EventType
from apollo.brains.base import Brain, GenerationParams
from apollo.brains.registry import BrainRegistry
from apollo.config import MODE_BENCHMARK, SURFACE_EVAL, Config
from apollo.context.budget import Budget
from apollo.context.bundle import ContextBundle, Purpose
from apollo.context.compiler import CompileRequest, HistoryMessage, compile_context
from apollo.context.estimator import CONSERVATIVE
from apollo.core import invocations
from apollo.core.conversations import create_benchmark_conversation
from apollo.core.identity import Identity, IdentityLoader, snapshot_identity
from apollo.core.policy import check_brain_permitted
from apollo.evals.checks import run_checks
from apollo.evals.evidence import (
    DETERMINISTIC_SAMPLES,
    MANUAL_SAMPLES,
    case_definition,
    corpus_hash,
)
from apollo.evals.models import PersonaCase, RunRecord, SampleRecord
from apollo.sanitise import error_detail, error_kind
from apollo.storage.db import Database
from apollo.storage.repositories import MessageRepository, TurnRepository
from apollo.storage.unit_of_work import unit_of_work

log = logging.getLogger(__name__)

DEFAULT_RUNS_DIR = pathlib.Path("evals/runs")


def compile_case_bundle(
    case: PersonaCase,
    *,
    identity: Identity,
    provider: Any,
    max_context: int,
    identity_cap: int,
    now: datetime,
) -> ContextBundle:
    """Compile one case exactly as a run would.

    Module level so the privacy pre-flight can inspect the *actual* bundle a
    hosted run would send, rather than a second implementation that agrees with
    this one until the day it does not.
    """
    history = tuple(
        HistoryMessage(message_id=case.history_ref(index), role=turn.role, content=turn.content)
        for index, turn in enumerate(case.history)
    )
    return compile_context(
        CompileRequest(
            purpose=Purpose.REPLY,
            user_message=case.input,
            user_message_ref=case.input_ref,
            now=now,
            budget=Budget.for_provider(
                context_budget=provider.context_budget,
                max_context=max_context,
                reserved_output=provider.reserved_output,
                identity_cap=identity_cap,
            ),
            # Pinned for every brain, so behaviour is the only variable.
            estimator=CONSERVATIVE,
            identity=identity,
            history=history,
        )
    )


class PersonaRunner:
    def __init__(
        self,
        db: Database,
        config: Config,
        registry: BrainRegistry,
        identity_loader: IdentityLoader,
        *,
        clock: Callable[[], datetime] | None = None,
        runs_dir: pathlib.Path = DEFAULT_RUNS_DIR,
    ) -> None:
        if registry.surface != SURFACE_EVAL:
            raise ValueError(
                "the persona runner requires an eval-surface registry; an interactive "
                "one cannot resolve an eval-only provider, which is the point"
            )
        self._db = db
        self._config = config
        self._registry = registry
        self._identity_loader = identity_loader
        self._clock = clock or (lambda: datetime.now(UTC))
        self._runs_dir = runs_dir

    def run(self, cases: list[PersonaCase], *, brain_alias: str) -> RunRecord:
        identity = self._identity_loader.load()
        provider = self._registry.provider_for(brain_alias)
        check_brain_permitted(
            provider=provider, conversation_mode=MODE_BENCHMARK, surface=SURFACE_EVAL
        )
        brain = self._registry.get(brain_alias)
        capabilities = brain.capabilities()
        params = GenerationParams(temperature=0.0, max_tokens=1024, seed=7)

        started = self._clock()
        conversation_id = create_benchmark_conversation(
            self._db, now=started, title=f"persona {brain_alias}"
        )
        with unit_of_work(self._db) as uow:
            if snapshot_identity(uow, identity, started):
                uow.record(
                    AuditEvent(
                        event_type=EventType.IDENTITY_VERSION_LOADED,
                        actor=Actor.APOLLO_CORE,
                        subject_kind="identity_version",
                        occurred_at=started,
                        payload={
                            "identity_version": identity.version_label,
                            "identity_hash": identity.content_hash,
                            "schema_version": identity.schema_version,
                        },
                    )
                )
            else:
                uow.audit_only()

        record = RunRecord(
            run_id=str(uuid.uuid4()),
            started_at=started,
            brain_alias=brain_alias,
            provider_key=provider.key,
            adapter_key=brain.adapter_key,
            render_version=brain.render_version,
            compiler_version="",  # filled from the first compiled bundle
            token_estimator=CONSERVATIVE.name,
            identity_version=identity.version_label,
            identity_hash=identity.content_hash,
            generation_params=params.as_dict(),
            determinism={
                "temperature": params.temperature,
                "seed": params.seed,
                "provider_claims_seed_support": capabilities.supports_seed,
                # Filled after the run: observed, never assumed. Passing a seed
                # is not the same as getting the same bytes twice.
                "repeatability_observed": None,
                "note": "a signal, not a proof; a hosted backend can change "
                        "beneath an unchanged model name",
            },
            conversation_id=conversation_id,
            corpus_hash=corpus_hash(cases),
            evidence_kind=(
                "offline"
                if provider.kind == "fake" or brain.adapter_key == "fake"
                else "provider_generation"
            ),
            context_settings={
                "context_budget": provider.context_budget,
                "reserved_output": provider.reserved_output,
                "max_context": capabilities.max_context,
                "identity_cap": self._config.identity_token_cap,
            },
        )

        for case in cases:
            samples = MANUAL_SAMPLES if case.has_manual_check else DETERMINISTIC_SAMPLES
            case_samples: list[SampleRecord] = []
            bundle_hashes: set[str] = set()
            for index in range(1, samples + 1):
                sample = self._run_sample(
                    case=case,
                    sample_index=index,
                    brain=brain,
                    brain_alias=brain_alias,
                    provider_key=provider.key,
                    conversation_id=conversation_id,
                    identity=identity,
                    capabilities_max_context=capabilities.max_context,
                    params=params,
                    record=record,
                )
                case_samples.append(sample)
                if sample.bundle_hash:
                    bundle_hashes.add(sample.bundle_hash)
            record.cases.append(self._case_entry(case, case_samples, bundle_hashes))

        record.determinism["repeatability_observed"] = _observed_repeatability(record)
        return record

    # -- one generation ----------------------------------------------------

    def _run_sample(
        self,
        *,
        case: PersonaCase,
        sample_index: int,
        brain: Brain,
        brain_alias: str,
        provider_key: str,
        conversation_id: uuid.UUID,
        identity: Identity,
        capabilities_max_context: int,
        params: GenerationParams,
        record: RunRecord,
    ) -> SampleRecord:
        now = self._clock()
        provider = self._registry.provider_for(brain_alias)
        bundle = self._compile(case, identity, provider, capabilities_max_context, now)
        if not record.compiler_version:
            record.compiler_version = bundle.compiler_version

        # A turn, so the generation is anchored in the same durable structures
        # an interactive turn uses.
        with unit_of_work(self._db) as uow:
            message = MessageRepository(uow).append(
                conversation_id=conversation_id,
                role="user",
                content=case.input,
                now=now,
            )
            turn_id = TurnRepository(uow).open(
                conversation_id=conversation_id,
                request_message_id=message["id"],
                conversation_mode=MODE_BENCHMARK,
                identity_version=identity.version_label,
                identity_hash=identity.content_hash,
                now=now,
            )
            uow.record(
                AuditEvent(
                    event_type=EventType.TURN_STARTED,
                    actor=Actor.APOLLO_CORE,
                    subject_kind="turn",
                    subject_id=turn_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    occurred_at=now,
                    payload={"mode": MODE_BENCHMARK, "seq": sample_index},
                )
            )

        invocation_id = invocations.open_invocation(
            self._db,
            turn_id=turn_id,
            conversation_id=conversation_id,
            purpose="reply",
            brain=brain,
            brain_alias=brain_alias,
            provider_key=provider_key,
            bundle=bundle,
            params=params,
            now=self._clock(),
        )
        outcome = invocations.invoke(
            self._db,
            invocation_id=invocation_id,
            turn_id=turn_id,
            conversation_id=conversation_id,
            brain=brain,
            bundle=bundle,
            params=params,
            now_factory=self._clock,
        )

        sample = SampleRecord(
            case_id=case.id,
            sample_index=sample_index,
            status="completed" if outcome.ok else "failed",
            response=outcome.generation.text if outcome.ok and outcome.generation else None,
            turn_id=turn_id,
            invocation_id=invocation_id,
            bundle_hash=bundle.bundle_hash,
        )
        if outcome.ok and outcome.generation is not None:
            generation = outcome.generation
            sample.model_identifier = generation.model_identifier
            sample.prompt_tokens = generation.prompt_tokens
            sample.completion_tokens = generation.completion_tokens
            # A count only. The reasoning text never left the adapter (spec H.3).
            sample.reasoning_tokens = generation.reasoning_tokens
            sample.latency_ms = generation.latency_ms
            sample.rendered_prompt_hash = generation.rendered_prompt_hash
            sample.finish_reason = generation.finish_reason
            sample.check_results = run_checks(case.checks, generation.text, case.input)
            self._finalise(conversation_id, turn_id, generation.text, now=self._clock())
        else:
            assert outcome.error is not None
            sample.error_kind = str(error_kind(outcome.error))
            self._fail(conversation_id, turn_id, outcome.error, now=self._clock())
        return sample

    def _compile(
        self,
        case: PersonaCase,
        identity: Identity,
        provider: Any,
        max_context: int,
        now: datetime,
    ) -> ContextBundle:
        return compile_case_bundle(
            case,
            identity=identity,
            provider=provider,
            max_context=max_context,
            identity_cap=self._config.identity_token_cap,
            now=now,
        )

    def _finalise(
        self, conversation_id: uuid.UUID, turn_id: uuid.UUID, text: str, *, now: datetime
    ) -> None:
        with unit_of_work(self._db) as uow:
            message = MessageRepository(uow).append(
                conversation_id=conversation_id, role="apollo", content=text,
                now=now, turn_id=turn_id,
            )
            TurnRepository(uow).complete(
                turn_id=turn_id, response_message_id=message["id"], now=now, latency_ms=1
            )
            uow.record(
                AuditEvent(
                    event_type=EventType.TURN_COMPLETED,
                    actor=Actor.APOLLO_CORE,
                    subject_kind="turn",
                    subject_id=turn_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    occurred_at=now,
                    payload={"status": "completed"},
                )
            )

    def _fail(
        self, conversation_id: uuid.UUID, turn_id: uuid.UUID, exc: BaseException, *,
        now: datetime,
    ) -> None:
        kind = error_kind(exc)
        with unit_of_work(self._db) as uow:
            TurnRepository(uow).fail(
                turn_id=turn_id, now=now, error_kind=str(kind),
                error_detail=error_detail(exc), latency_ms=1,
            )
            uow.record(
                AuditEvent(
                    event_type=EventType.TURN_FAILED,
                    actor=Actor.APOLLO_CORE,
                    subject_kind="turn",
                    subject_id=turn_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    occurred_at=now,
                    payload={"error_kind": str(kind)},
                )
            )

    # -- record assembly ---------------------------------------------------

    def _case_entry(
        self, case: PersonaCase, samples: list[SampleRecord], bundle_hashes: set[str]
    ) -> dict[str, Any]:
        return {
            "case_id": case.id,
            "case_definition": case_definition(case),
            "source_file": case.source_file,
            "tags": list(case.tags),
            "behavioural_expectation": case.behavioural_expectation,
            "undesired_characteristics": list(case.undesired_characteristics),
            "covers_rules": list(case.covers_rules),
            "covers_probes": list(case.covers_probes),
            # The fixture input *is* the benchmark, so it is recorded verbatim.
            "input_context": {
                "mode": case.mode,
                "history": [asdict(turn) for turn in case.history],
                "input": case.input,
            },
            "bundle_hash": sorted(bundle_hashes)[0] if len(bundle_hashes) == 1 else None,
            "bundle_hash_stable_across_samples": len(bundle_hashes) <= 1,
            "bundle_hashes_seen": sorted(bundle_hashes),
            "samples": [self._sample_entry(sample) for sample in samples],
            "deterministic_status": _case_status(samples),
        }

    def _sample_entry(self, sample: SampleRecord) -> dict[str, Any]:
        return {
            "sample_index": sample.sample_index,
            "status": sample.status,
            "error_kind": sample.error_kind,
            # The visible answer, verbatim. This is the observation.
            "response": sample.response,
            "check_results": [result.as_dict() for result in sample.check_results],
            "deterministic_status": sample.deterministic_status,
            "turn_id": str(sample.turn_id) if sample.turn_id else None,
            "invocation_id": str(sample.invocation_id) if sample.invocation_id else None,
            "bundle_hash": sample.bundle_hash,
            "model_identifier": sample.model_identifier,
            "prompt_tokens": sample.prompt_tokens,
            "completion_tokens": sample.completion_tokens,
            "reasoning_tokens": sample.reasoning_tokens,
            "latency_ms": sample.latency_ms,
            "rendered_prompt_hash": sample.rendered_prompt_hash,
            "finish_reason": sample.finish_reason,
        }

    def write(self, record: RunRecord) -> pathlib.Path:
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        path = self._runs_dir / record.filename
        path.write_text(json.dumps(to_document(record), indent=2, sort_keys=False) + "\n",
                        encoding="utf-8")
        log.info("persona.run_written", extra={"brain_alias": record.brain_alias,
                                               "count": len(record.cases)})
        return path


def to_document(record: RunRecord) -> dict[str, Any]:
    return {
        "suite_version": record.suite_version,
        "run_id": record.run_id,
        "started_at": record.started_at.isoformat(),
        "brain_alias": record.brain_alias,
        "provider_key": record.provider_key,
        "adapter_key": record.adapter_key,
        "render_version": record.render_version,
        "compiler_version": record.compiler_version,
        "token_estimator": record.token_estimator,
        "identity_version": record.identity_version,
        "identity_hash": record.identity_hash,
        "generation_params": record.generation_params,
        "determinism": record.determinism,
        "conversation_id": str(record.conversation_id) if record.conversation_id else None,
        "corpus_hash": record.corpus_hash,
        "evidence_kind": record.evidence_kind,
        "context_settings": record.context_settings,
        "cases": record.cases,
    }


def _case_status(samples: list[SampleRecord]) -> str:
    statuses = {sample.deterministic_status for sample in samples}
    if "fail" in statuses:
        return "fail"
    if statuses == {"no_deterministic_checks"}:
        return "no_deterministic_checks"
    return "pass" if statuses <= {"pass", "no_deterministic_checks"} else "fail"


def _observed_repeatability(record: RunRecord) -> bool | None:
    """Did repeated samples of the same case actually produce the same text?

    Only answerable where a case ran more than once. `None` means the run
    contained no multi-sample case, not that determinism was achieved.
    """
    verdicts: list[bool] = []
    for entry in record.cases:
        responses = [s["response"] for s in entry["samples"] if s["status"] == "completed"]
        if len(responses) > 1:
            verdicts.append(len(set(responses)) == 1)
    if not verdicts:
        return None
    return all(verdicts)


def load_run(path: pathlib.Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document
