"""The only path that may call a Brain (ADR-0013).

The invariant, stated precisely: *every actual provider generation attempt
corresponds to exactly one committed `model_invocation` row.* A retry is
another invocation, never a second call inside one row.

Two mechanisms hold it:

1. `invoke` refuses to enter an adapter without an `invocation_id` naming a row
   already committed with `status='started'`. A crash mid-call therefore leaves
   a `started` invocation, not silence.
2. Before entering the adapter it asserts no database transaction is open, so
   a transaction can never span a model call.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from apollo.audit.events import Actor, AuditEvent, EventType
from apollo.brains.base import (
    Brain,
    Generation,
    GenerationParams,
    RenderedRequest,
    verify_region_contract,
)
from apollo.context.bundle import ContextBundle
from apollo.errors import (
    EmptyGenerationError,
    ErrorKind,
    GenerationContractError,
    InvocationContractError,
)
from apollo.sanitise import error_detail, error_kind, whitelist_meta
from apollo.storage.db import Database, assert_no_open_transaction
from apollo.storage.repositories import InvocationRepository
from apollo.storage.unit_of_work import unit_of_work

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InvocationOutcome:
    invocation_id: uuid.UUID
    generation: Generation | None
    error: BaseException | None

    @property
    def ok(self) -> bool:
        """Success needs both halves: a generation, and no error.

        A provider can return a `Generation` object whose text is empty, which
        raises `EmptyGenerationError` *after* the object exists. Testing only
        for the object's presence therefore called that turn a success while
        the invocation row recorded `empty_generation` — an internally
        contradictory durable state, with a blank Apollo message persisted.
        """
        return self.generation is not None and self.error is None


def open_invocation(
    db: Database,
    *,
    turn_id: uuid.UUID,
    conversation_id: uuid.UUID,
    purpose: str,
    brain: Brain,
    brain_alias: str,
    provider_key: str,
    bundle: ContextBundle,
    params: GenerationParams,
    now: datetime,
    retry_of: uuid.UUID | None = None,
) -> uuid.UUID:
    """Commit a `started` invocation row. Must happen before the adapter is entered."""
    with unit_of_work(db) as uow:
        invocation_id = InvocationRepository(uow).start(
            turn_id=turn_id,
            purpose=purpose,
            brain_alias=brain_alias,
            provider_key=provider_key,
            adapter_key=brain.adapter_key,
            render_version=brain.render_version,
            compiler_version=bundle.compiler_version,
            token_estimator=bundle.estimator_name,
            context_manifest=bundle.manifest_list(),
            context_bundle_hash=bundle.bundle_hash,
            context_token_estimate=bundle.total_token_estimate,
            max_trust_tier=str(bundle.max_trust_tier),
            taint=bundle.taint,
            generation_params=params.as_dict(),
            now=now,
            retry_of_invocation_id=retry_of,
        )
        uow.record(
            AuditEvent(
                event_type=EventType.INVOCATION_STARTED,
                actor=Actor.APOLLO_CORE,
                subject_kind="model_invocation",
                subject_id=invocation_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                occurred_at=now,
                payload={
                    "purpose": purpose,
                    "brain_alias": brain_alias,
                    "provider_key": provider_key,
                    "adapter_key": brain.adapter_key,
                    "render_version": brain.render_version,
                    "compiler_version": bundle.compiler_version,
                    "token_estimator": bundle.estimator_name,
                    "bundle_hash": bundle.bundle_hash,
                    "token_estimate": bundle.total_token_estimate,
                    "max_trust_tier": str(bundle.max_trust_tier),
                    "taint": bundle.taint,
                    "retry_of_invocation_id": str(retry_of) if retry_of else None,
                },
            )
        )
    return invocation_id


def invoke(
    db: Database,
    *,
    invocation_id: uuid.UUID,
    turn_id: uuid.UUID,
    conversation_id: uuid.UUID,
    brain: Brain,
    bundle: ContextBundle,
    params: GenerationParams,
    now_factory: Callable[[], datetime],
) -> InvocationOutcome:
    """Render, call the adapter, and record the outcome. Nothing else calls a Brain."""
    with unit_of_work(db, expect_audit=False) as uow:
        if not InvocationRepository(uow).is_started(invocation_id):
            raise InvocationContractError(
                "refusing to call a model without a committed 'started' invocation row"
            )

    # From here to the completion transaction, no transaction may be open.
    assert_no_open_transaction("model invocation")

    started = time.monotonic()
    generation: Generation | None = None
    meta: dict[str, Any] = {}
    error: BaseException | None = None
    try:
        request = brain.render(bundle)
        verify_region_contract(bundle, request)
        generation = _validate_generation(brain.generate(request, params), request)
        # Validate and reduce provider metadata while still inside the guarded
        # adapter-result path. A malformed raw_meta must become a recorded
        # invocation failure, not escape later and leave the row started.
        meta = whitelist_meta(generation.raw_meta)
    except BaseException as exc:  # noqa: BLE001 - mapped by type, never by text
        error = exc

    latency_ms = max(1, int((time.monotonic() - started) * 1000))
    now = now_factory()

    with unit_of_work(db) as uow:
        repo = InvocationRepository(uow)
        if generation is not None and error is None:
            repo.complete(
                invocation_id=invocation_id,
                now=now,
                latency_ms=latency_ms,
                model_identifier=generation.model_identifier,
                rendered_prompt_hash=generation.rendered_prompt_hash,
                finish_reason=generation.finish_reason,
                prompt_tokens=generation.prompt_tokens,
                completion_tokens=generation.completion_tokens,
                reasoning_tokens=generation.reasoning_tokens,
            )
            uow.record(
                AuditEvent(
                    event_type=EventType.INVOCATION_COMPLETED,
                    actor=Actor.MODEL,
                    subject_kind="model_invocation",
                    subject_id=invocation_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    occurred_at=now,
                    payload={
                        "latency_ms": latency_ms,
                        "finish_reason": generation.finish_reason,
                        "prompt_tokens": generation.prompt_tokens,
                        "completion_tokens": generation.completion_tokens,
                        "reasoning_tokens": generation.reasoning_tokens,
                        **{k: v for k, v in meta.items() if k == "provider_request_id"},
                    },
                )
            )
        else:
            assert error is not None
            kind = error_kind(error)
            repo.fail(
                invocation_id=invocation_id,
                now=now,
                latency_ms=latency_ms,
                error_kind=str(kind),
                error_detail=error_detail(error),
            )
            uow.record(
                AuditEvent(
                    event_type=EventType.INVOCATION_FAILED,
                    actor=Actor.APOLLO_CORE,
                    subject_kind="model_invocation",
                    subject_id=invocation_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    occurred_at=now,
                    payload={"latency_ms": latency_ms, "error_kind": str(kind)},
                )
            )
            if kind is ErrorKind.BRAIN_UNAVAILABLE:
                uow.record(
                    AuditEvent(
                        event_type=EventType.BRAIN_UNAVAILABLE,
                        actor=Actor.SYSTEM,
                        subject_kind="model_invocation",
                        subject_id=invocation_id,
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        occurred_at=now,
                        payload={"error_kind": str(kind)},
                    )
                )

    log.info(
        "invocation.finished",
        extra={
            "invocation_id": str(invocation_id),
            "turn_id": str(turn_id),
            "status": "completed" if generation and not error else "failed",
            "latency_ms": latency_ms,
            "error_kind": str(error_kind(error)) if error else None,
        },
    )
    return InvocationOutcome(invocation_id=invocation_id, generation=generation, error=error)


def _validate_generation(value: object, request: RenderedRequest) -> Generation:
    """Validate the provider result before any field is persisted.

    `Brain` is a structural protocol, so Python cannot enforce its return type
    at runtime. Gate 1 requires a well-formed Generation, and every invocation
    benefits from rejecting malformed adapter output through the same recorded
    failure path.
    """
    if not isinstance(value, Generation):
        raise GenerationContractError("brain did not return Generation")
    if not isinstance(value.text, str):
        raise GenerationContractError("generation text is not a string")
    if not value.text.strip():
        raise EmptyGenerationError("provider returned no text")
    if not isinstance(value.model_identifier, str) or not value.model_identifier.strip():
        raise GenerationContractError("generation model identifier is missing")
    if not isinstance(value.finish_reason, str) or not value.finish_reason.strip():
        raise GenerationContractError("generation finish reason is missing")
    if not _nonnegative_int(value.latency_ms):
        raise GenerationContractError("generation latency is invalid")
    if value.rendered_prompt_hash != request.prompt_hash:
        raise GenerationContractError("generation rendered prompt hash does not match request")
    for name in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
        count = getattr(value, name)
        if count is not None and not _nonnegative_int(count):
            raise GenerationContractError(f"generation {name} is invalid")
    if not isinstance(value.raw_meta, dict):
        raise GenerationContractError("generation raw_meta is not a mapping")
    return value


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
