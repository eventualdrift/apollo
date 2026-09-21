"""Turn orchestration and the frozen transaction boundaries (spec A.2).

The shape of an ordinary successful turn:

    user message
        -> message + turn persisted atomically          (T1)
        -> identity loaded
        -> policy check
        -> context bundle compiled
        -> model_invocation(status=started) committed   (Ta)
        -> brain invoked, with no transaction open
        -> invocation completed                         (Tb)
        -> Apollo response + turn completion committed  (Tf)

A transport failure adds one retry, which is a *new invocation row* with
`retry_of_invocation_id` set — never a second call inside the failed row.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from apollo.audit.events import Actor, AuditEvent, EventType
from apollo.brains.base import Brain, GenerationParams
from apollo.brains.registry import BrainRegistry
from apollo.config import Config
from apollo.context.budget import Budget
from apollo.context.bundle import ContextBundle, Purpose
from apollo.context.compiler import CompileRequest, HistoryMessage, compile_context
from apollo.core import invocations
from apollo.core.identity import IdentityLoader, snapshot_identity
from apollo.core.policy import SURFACE_INTERACTIVE, check_brain_permitted
from apollo.errors import ApolloError, BrainTransportError, ErrorKind, PolicyRefusedError
from apollo.sanitise import error_detail, error_kind
from apollo.storage.db import Database
from apollo.storage.repositories import (
    ConversationRepository,
    InvocationRepository,
    MessageRepository,
    TurnRepository,
)
from apollo.storage.unit_of_work import unit_of_work

log = logging.getLogger(__name__)

#: Phase zero permits exactly one retry per purpose, and only for transport
#: failures. Content-level failures are never retried: retrying until the output
#: looks acceptable hides a real problem (spec L).
MAX_TRANSPORT_RETRIES = 1

#: The durable, human-visible note written into the transcript when a turn
#: fails. Keyed on the closed `ErrorKind` enum, so the note states the failure
#: *category* truthfully: saying the model was unreachable when Core failed
#: before ever calling it would be a lie in the one record Janu reads.
#:
#: Every string here is a fixed constant. Nothing derived from an exception, a
#: provider response or the conversation reaches the transcript (spec H.5).
SYSTEM_NOTES: dict[ErrorKind, str] = {
    ErrorKind.BRAIN_UNAVAILABLE: (
        "Apollo could not reach its model. No response was generated for that message."
    ),
    ErrorKind.EMPTY_GENERATION: (
        "Apollo's model returned nothing. No response was generated for that message."
    ),
    ErrorKind.CONTEXT_OVERFLOW: (
        "This conversation no longer fits in Apollo's context window. "
        "No response was generated for that message."
    ),
    ErrorKind.IDENTITY_OVERFLOW: (
        "Apollo could not assemble its own identity for this turn. "
        "No response was generated for that message."
    ),
    ErrorKind.BRAIN_MODE_NOT_PERMITTED: (
        "Apollo refused this turn: the configured model is not permitted for this "
        "conversation. No response was generated for that message."
    ),
    ErrorKind.BRAIN_NOT_INTERACTIVE: (
        "Apollo refused this turn: the configured model is an evaluation-only surface "
        "and cannot be used interactively. No response was generated for that message."
    ),
    ErrorKind.RETRIEVAL_FAILED: (
        "Apollo could not search its memory for this turn. "
        "No response was generated for that message."
    ),
    ErrorKind.INTERRUPTED: (
        "This turn was interrupted before it completed. "
        "No response was generated for that message."
    ),
}

#: Anything else — a render-contract violation, a bug, an unmapped failure —
#: gets the neutral truthful note. It does not claim the model was reachable or
#: unreachable, because at that point Core does not know.
SYSTEM_NOTE_INTERNAL = (
    "Apollo could not complete this turn because of an internal processing error. "
    "No response was generated for that message."
)


def system_note_for(kind: ErrorKind) -> str:
    """The truthful note for a failure category. Total, and never derived from input."""
    return SYSTEM_NOTES.get(kind, SYSTEM_NOTE_INTERNAL)


@dataclass(frozen=True)
class TurnResult:
    turn_id: uuid.UUID
    status: str
    response: str | None
    response_message_id: uuid.UUID | None
    invocation_ids: tuple[uuid.UUID, ...]
    error_kind: str | None = None
    replayed: bool = False


class TurnService:
    """Owns the turn. The only orchestration surface in M1."""

    def __init__(
        self,
        db: Database,
        config: Config,
        registry: BrainRegistry,
        identity_loader: IdentityLoader,
        *,
        clock: Callable[[], datetime] | None = None,
        brain_alias: str = "brain.default",
    ) -> None:
        self._db = db
        self._config = config
        self._registry = registry
        self._identity = identity_loader
        self._clock = clock or (lambda: datetime.now(UTC))
        self._brain_alias = brain_alias

    # -- orphan recovery ---------------------------------------------------

    def recover_orphans(self) -> dict[str, int]:
        """Turns and invocations left `started` beyond the window (spec A.2)."""
        now = self._clock()
        cutoff = now - timedelta(seconds=self._config.orphan_window_seconds)
        with unit_of_work(self._db) as uow:
            invocations_failed = InvocationRepository(uow).expire_orphans(
                older_than=cutoff, now=now
            )
            turns_failed = TurnRepository(uow).expire_orphans(older_than=cutoff, now=now)
            if invocations_failed or turns_failed:
                uow.record(
                    AuditEvent(
                        event_type=EventType.TURN_FAILED,
                        actor=Actor.SYSTEM,
                        subject_kind="turn",
                        occurred_at=now,
                        payload={
                            "error_kind": str(ErrorKind.INTERRUPTED),
                            "count": turns_failed,
                            "reason": "orphan_recovery",
                        },
                    )
                )
            else:
                uow.audit_only()  # nothing mutated, nothing to record
        return {"turns": turns_failed, "invocations": invocations_failed}

    # -- the turn ----------------------------------------------------------

    def submit(
        self,
        *,
        conversation_id: uuid.UUID,
        text: str,
        idempotency_key: str | None = None,
        device_id: str | None = None,
    ) -> TurnResult:
        now = self._clock()
        turn_started = time.monotonic()

        existing = self._replayed_turn(conversation_id, idempotency_key)
        if existing is not None:
            return existing

        identity = self._identity.load()
        conversation = self._require_conversation(conversation_id)
        provider = self._registry.provider_for(self._brain_alias)

        # T1: request message and turn commit together.
        with unit_of_work(self._db) as uow:
            if snapshot_identity(uow, identity, now):
                uow.record(
                    AuditEvent(
                        event_type=EventType.IDENTITY_VERSION_LOADED,
                        actor=Actor.APOLLO_CORE,
                        subject_kind="identity_version",
                        occurred_at=now,
                        payload={
                            "identity_version": identity.version_label,
                            "identity_hash": identity.content_hash,
                            "schema_version": identity.schema_version,
                            "fragment_count": len(identity.fragments),
                        },
                    )
                )
            message = MessageRepository(uow).append(
                conversation_id=conversation_id,
                role="user",
                content=text,
                now=now,
                device_id=device_id,
                idempotency_key=idempotency_key,
            )
            turn_id = TurnRepository(uow).open(
                conversation_id=conversation_id,
                request_message_id=message["id"],
                conversation_mode=conversation["mode"],
                identity_version=identity.version_label,
                identity_hash=identity.content_hash,
                now=now,
            )
            ConversationRepository(uow).touch(conversation_id, now)
            uow.record(
                AuditEvent(
                    event_type=EventType.MESSAGE_CREATED,
                    actor=Actor.USER,
                    subject_kind="message",
                    subject_id=message["id"],
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    occurred_at=now,
                    payload={"message_role": "user", "seq": message["seq"]},
                )
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
                    payload={
                        "identity_version": identity.version_label,
                        "identity_hash": identity.content_hash,
                        "mode": conversation["mode"],
                    },
                )
            )

        try:
            # Policy check before retrieval or generation (spec K.2).
            check_brain_permitted(
                provider=provider,
                conversation_mode=conversation["mode"],
                surface=SURFACE_INTERACTIVE,
            )
            brain = self._registry.get(self._brain_alias)
            capabilities = brain.capabilities()
            bundle = compile_context(
                CompileRequest(
                    purpose=Purpose.REPLY,
                    user_message=text,
                    user_message_ref=str(message["id"]),
                    now=now,
                    budget=Budget.for_provider(
                        context_budget=provider.context_budget,
                        max_context=capabilities.max_context,
                        reserved_output=provider.reserved_output,
                        identity_cap=self._config.identity_token_cap,
                    ),
                    estimator=capabilities.estimator,
                    identity=identity,
                    history=self._history(conversation_id, before_seq=message["seq"]),
                )
            )
        except PolicyRefusedError as exc:
            self._record_policy_refusal(conversation_id, turn_id, exc)
            return self._fail_turn(conversation_id, turn_id, exc, turn_started)
        except ApolloError as exc:
            return self._fail_turn(conversation_id, turn_id, exc, turn_started)

        outcome, attempts = self._generate_with_retry(
            conversation_id=conversation_id,
            turn_id=turn_id,
            brain=brain,
            provider_key=provider.key,
            bundle=bundle,
        )

        if not outcome.ok:
            assert outcome.error is not None
            return self._fail_turn(
                conversation_id,
                turn_id,
                outcome.error,
                turn_started,
                invocation_ids=attempts,
            )

        assert outcome.generation is not None
        return self._complete_turn(
            conversation_id=conversation_id,
            turn_id=turn_id,
            text=outcome.generation.text,
            truncated=outcome.generation.finish_reason == "length",
            turn_started=turn_started,
            invocation_ids=attempts,
        )

    # -- internals ---------------------------------------------------------

    def _generate_with_retry(
        self,
        *,
        conversation_id: uuid.UUID,
        turn_id: uuid.UUID,
        brain: Brain,
        provider_key: str,
        bundle: ContextBundle,
    ) -> tuple[invocations.InvocationOutcome, tuple[uuid.UUID, ...]]:
        params = GenerationParams()
        attempts: list[uuid.UUID] = []
        retry_of: uuid.UUID | None = None
        outcome = None
        for attempt in range(MAX_TRANSPORT_RETRIES + 1):
            now = self._clock()
            invocation_id = invocations.open_invocation(
                self._db,
                turn_id=turn_id,
                conversation_id=conversation_id,
                purpose="reply",
                brain=brain,
                brain_alias=self._brain_alias,
                provider_key=provider_key,
                bundle=bundle,
                params=params,
                now=now,
                retry_of=retry_of,
            )
            attempts.append(invocation_id)
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
            if outcome.ok:
                break
            # Only transport-level failures are retried, and only once.
            if not isinstance(outcome.error, BrainTransportError):
                break
            if attempt >= MAX_TRANSPORT_RETRIES:
                break
            retry_of = invocation_id
            log.info("invocation.retrying", extra={"turn_id": str(turn_id),
                                                   "retry_of": str(invocation_id)})
        assert outcome is not None
        return outcome, tuple(attempts)

    def _complete_turn(
        self,
        *,
        conversation_id: uuid.UUID,
        turn_id: uuid.UUID,
        text: str,
        truncated: bool,
        turn_started: float,
        invocation_ids: tuple[uuid.UUID, ...],
    ) -> TurnResult:
        now = self._clock()
        latency = max(1, int((time.monotonic() - turn_started) * 1000))
        with unit_of_work(self._db) as uow:
            message = MessageRepository(uow).append(
                conversation_id=conversation_id,
                role="apollo",
                content=text,
                now=now,
                turn_id=turn_id,
                truncated=truncated,
            )
            TurnRepository(uow).complete(
                turn_id=turn_id,
                response_message_id=message["id"],
                now=now,
                latency_ms=latency,
            )
            ConversationRepository(uow).touch(conversation_id, now)
            uow.record(
                AuditEvent(
                    event_type=EventType.MESSAGE_CREATED,
                    actor=Actor.APOLLO_CORE,
                    subject_kind="message",
                    subject_id=message["id"],
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    occurred_at=now,
                    payload={"message_role": "apollo", "seq": message["seq"]},
                )
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
                    payload={"latency_ms": latency, "status": "completed"},
                )
            )
        return TurnResult(
            turn_id=turn_id,
            status="completed",
            response=text,
            response_message_id=message["id"],
            invocation_ids=invocation_ids,
        )

    def _fail_turn(
        self,
        conversation_id: uuid.UUID,
        turn_id: uuid.UUID,
        exc: BaseException,
        turn_started: float,
        *,
        invocation_ids: tuple[uuid.UUID, ...] = (),
    ) -> TurnResult:
        now = self._clock()
        latency = max(1, int((time.monotonic() - turn_started) * 1000))
        kind = error_kind(exc)
        note = system_note_for(kind)
        with unit_of_work(self._db) as uow:
            TurnRepository(uow).fail(
                turn_id=turn_id,
                now=now,
                error_kind=str(kind),
                error_detail=error_detail(exc),
                latency_ms=latency,
            )
            if note:
                # Durable and human-visible; never re-enters model context (spec F.2).
                system_message = MessageRepository(uow).append(
                    conversation_id=conversation_id,
                    role="system_note",
                    content=note,
                    now=now,
                    turn_id=turn_id,
                )
                uow.record(
                    AuditEvent(
                        event_type=EventType.MESSAGE_CREATED,
                        actor=Actor.SYSTEM,
                        subject_kind="message",
                        subject_id=system_message["id"],
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        occurred_at=now,
                        payload={"message_role": "system_note", "seq": system_message["seq"]},
                    )
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
                    payload={"error_kind": str(kind), "latency_ms": latency},
                )
            )
        return TurnResult(
            turn_id=turn_id,
            status="failed",
            response=None,
            response_message_id=None,
            invocation_ids=invocation_ids,
            error_kind=str(kind),
        )

    def _record_policy_refusal(
        self, conversation_id: uuid.UUID, turn_id: uuid.UUID, exc: PolicyRefusedError
    ) -> None:
        now = self._clock()
        with unit_of_work(self._db) as uow:
            uow.audit_only()
            uow.record(
                AuditEvent(
                    event_type=EventType.POLICY_REFUSED,
                    actor=Actor.APOLLO_CORE,
                    subject_kind="turn",
                    subject_id=turn_id,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    occurred_at=now,
                    payload={"error_kind": str(exc.kind), "brain_alias": self._brain_alias},
                )
            )

    def _replayed_turn(
        self, conversation_id: uuid.UUID, idempotency_key: str | None
    ) -> TurnResult | None:
        """A duplicate submission returns the existing turn. No second generation."""
        if not idempotency_key:
            return None
        with unit_of_work(self._db, expect_audit=False) as uow:
            prior = MessageRepository(uow).find_by_idempotency_key(
                conversation_id, idempotency_key
            )
            if prior is None:
                return None
            turn = TurnRepository(uow).find_by_request_message(prior["id"])
            if turn is None:
                return None
            response = None
            if turn["response_message_id"]:
                cur = uow.execute(
                    "SELECT content FROM message WHERE id = %s", (turn["response_message_id"],)
                )
                row = cur.fetchone()
                response = row["content"] if row else None
            invocation_ids = tuple(
                row["id"] for row in InvocationRepository(uow).for_turn(turn["id"])
            )
        return TurnResult(
            turn_id=turn["id"],
            status=turn["status"],
            response=response,
            response_message_id=turn["response_message_id"],
            invocation_ids=invocation_ids,
            error_kind=turn["error_kind"],
            replayed=True,
        )

    def _require_conversation(self, conversation_id: uuid.UUID) -> dict[str, Any]:
        with unit_of_work(self._db, expect_audit=False) as uow:
            conversation = ConversationRepository(uow).get(conversation_id)
        if conversation is None:
            raise ApolloError(f"no such conversation: {conversation_id}")
        return conversation

    def _history(
        self, conversation_id: uuid.UUID, *, before_seq: int
    ) -> tuple[HistoryMessage, ...]:
        with unit_of_work(self._db, expect_audit=False) as uow:
            rows = MessageRepository(uow).history(conversation_id, before_seq=before_seq)
        return tuple(
            HistoryMessage(message_id=str(r["id"]), role=r["role"], content=r["content"])
            for r in rows
        )
