"""Turn orchestration and the frozen transaction boundaries (spec A.2).

The shape of an ordinary successful turn:

    user message
        -> message + turn persisted atomically          (T1)
        -> identity loaded
        -> policy check
        -> memory searched (a failed search is stated, and does not fail the turn)
        -> context bundle compiled; what was searched and found is recorded
        -> model_invocation(status=started) committed   (Ta)
        -> brain invoked, with no transaction open
        -> invocation completed                         (Tb)
        -> Apollo response + turn completion committed  (Tf)

A transport failure adds one retry, which is a *new invocation row* with
`retry_of_invocation_id` set — never a second call inside the failed row.

When the message asks for something to be remembered, one more invocation
structures a *proposal* between the reply and the turn's completion (spec
D.3). That is all it can do: nothing on this path creates a memory, and a
proposal that fails never fails the turn.
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
from apollo.brains.base import Brain, Generation, GenerationParams
from apollo.brains.registry import BrainRegistry
from apollo.config import MODE_PERSONAL, Config
from apollo.context.budget import Budget
from apollo.context.bundle import ContextBundle, Purpose
from apollo.context.compiler import (
    CompileRequest,
    HistoryMessage,
    UnsendableContextError,
    compile_context,
)
from apollo.core import invocations
from apollo.core.identity import IdentityLoader, snapshot_identity
from apollo.core.policy import SURFACE_INTERACTIVE, check_brain_permitted
from apollo.core.retrieval import compile_input, record_for_turn, retrieve_for_turn
from apollo.errors import (
    ApolloError,
    BrainRejectedError,
    BrainTransportError,
    ErrorKind,
    FinalisationConflictError,
    InvocationRefusedError,
    PolicyRefusedError,
    TransportReason,
)
from apollo.memory import proposals
from apollo.memory.intent import detect_persistence_intent
from apollo.memory.proposals import Proposal, ProposalStructureError, ProposedClaim
from apollo.sanitise import error_detail, error_kind
from apollo.storage.db import Database
from apollo.storage.repositories import (
    ConversationRepository,
    InvocationRepository,
    MessageRepository,
    TurnRepository,
)
from apollo.storage.unit_of_work import UnitOfWork, unit_of_work

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
    ErrorKind.BRAIN_FAILED: (
        "Apollo's model service failed while handling that message. "
        "No response was generated for that message."
    ),
    ErrorKind.BRAIN_REJECTED: (
        "Apollo's model service rejected the request. "
        "No response was generated for that message."
    ),
    ErrorKind.MALFORMED_GENERATION: (
        "Apollo's model returned a response Apollo could not read. "
        "No response was generated for that message."
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


#: A transport failure says which kind it was: "could not reach" is true of a
#: refused connection, not of a timeout or a service that answered 503.
TRANSPORT_NOTES: dict[TransportReason, str] = {
    TransportReason.CONNECTION: SYSTEM_NOTES[ErrorKind.BRAIN_UNAVAILABLE],
    TransportReason.TIMEOUT: (
        "Apollo's model timed out before answering. "
        "No response was generated for that message."
    ),
    TransportReason.UNAVAILABLE: (
        "Apollo's model service was temporarily unavailable. "
        "No response was generated for that message."
    ),
    TransportReason.BUSY: (
        "Apollo's model service was too busy to answer. "
        "No response was generated for that message."
    ),
}

#: A rejection for credentials (401/403) is worth naming: it is fixed by
#: configuration, not by trying again.
SYSTEM_NOTE_REJECTED_AUTH = (
    "Apollo's model service refused its credentials. "
    "No response was generated for that message."
)


#: Written beside a reply whose memory proposal could not be made (spec L):
#: the conversation succeeded, the capture did not, and direct entry still works.
SYSTEM_NOTE_CAPTURE_FAILED = (
    "Apollo could not turn that message into a memory proposal, so nothing was saved. "
    "You can add it yourself with `apollo memory add`."
)

#: The structuring call is short and deterministic: four labelled lines.
PROPOSAL_PARAMS = GenerationParams(temperature=0.0, max_tokens=256)


def system_note_for(kind: ErrorKind) -> str:
    """The truthful note for a failure category. Total, and never derived from input."""
    return SYSTEM_NOTES.get(kind, SYSTEM_NOTE_INTERNAL)


def system_note_for_error(exc: BaseException) -> str:
    """The note for a failure, refined by its class where the kind alone is too coarse.

    Reads only the exception's type and Apollo-defined attributes, never its text.
    """
    if isinstance(exc, BrainTransportError):
        return TRANSPORT_NOTES.get(exc.reason, system_note_for(exc.kind))
    if isinstance(exc, BrainRejectedError) and exc.is_auth:
        return SYSTEM_NOTE_REJECTED_AUTH
    return system_note_for(error_kind(exc))


@dataclass(frozen=True)
class TurnResult:
    turn_id: uuid.UUID
    status: str
    response: str | None
    response_message_id: uuid.UUID | None
    invocation_ids: tuple[uuid.UUID, ...]
    error_kind: str | None = None
    replayed: bool = False
    #: The pending proposal this turn produced, for the client to offer
    #: Save / Edit and Save / Ignore. Never a memory (spec D.3/1).
    proposal: Proposal | None = None
    #: The message asked for a memory and the proposal could not be made.
    capture_failed: bool = False
    #: The model stopped at its output limit: the reply is recorded and shown,
    #: marked as cut off, never presented as whole (spec L).
    truncated: bool = False


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
            expired = TurnRepository(uow).expire_orphans(older_than=cutoff, now=now)
            turns_failed = len(expired)
            # An interrupted turn is a failed turn, and failure finalisation is
            # failed status, system_note and audit together (spec A.2).
            note = system_note_for(ErrorKind.INTERRUPTED)
            messages = MessageRepository(uow)
            for turn in expired:
                message = messages.append(
                    conversation_id=turn["conversation_id"],
                    role="system_note",
                    content=note,
                    now=now,
                    turn_id=turn["id"],
                )
                uow.record(
                    AuditEvent(
                        event_type=EventType.MESSAGE_CREATED,
                        actor=Actor.SYSTEM,
                        subject_kind="message",
                        subject_id=message["id"],
                        conversation_id=turn["conversation_id"],
                        turn_id=turn["id"],
                        occurred_at=now,
                        payload={"message_role": "system_note", "seq": message["seq"]},
                    )
                )
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
        propose_memory: bool = False,
    ) -> TurnResult:
        """Take one message through a turn.

        `propose_memory` is the explicit client action of spec D.2: it asks for
        a proposal from this message without the message having to say so.
        """
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
            history = self._history(conversation_id, before_seq=message["seq"])
        except PolicyRefusedError as exc:
            self._record_policy_refusal(conversation_id, turn_id, exc)
            return self._fail_turn(conversation_id, turn_id, exc, turn_started)
        except ApolloError as exc:
            return self._fail_turn(conversation_id, turn_id, exc, turn_started)

        # Retrieval (spec A.2 step 5). A failed search does not fail the turn:
        # the bundle says the search failed, never that nothing was found.
        found = retrieve_for_turn(
            self._db,
            current=text,
            previous=_previous_user_message(history),
            conversation_mode=conversation["mode"],
        )
        try:
            bundle_budget = Budget.for_provider(
                context_budget=provider.context_budget,
                max_context=capabilities.max_context,
                reserved_output=provider.reserved_output,
                identity_cap=self._config.identity_token_cap,
            )
            bundle = compile_context(
                CompileRequest(
                    purpose=Purpose.REPLY,
                    user_message=text,
                    user_message_ref=str(message["id"]),
                    now=now,
                    budget=bundle_budget,
                    estimator=capabilities.estimator,
                    identity=identity,
                    history=history,
                    retrieval=compile_input(found, now=now),
                )
            )
        except ApolloError as exc:
            # A context that was assembled and does not fit is kept: its
            # manifest shows what did not fit (spec L). Anything else that
            # stops compilation leaves nothing to record but the search.
            assembled = exc.bundle if isinstance(exc, UnsendableContextError) else None
            record_for_turn(self._db, turn_id=turn_id, found=found, bundle=assembled, now=now)
            unsent: tuple[uuid.UUID, ...] = ()
            if assembled is not None:
                unsent = self._record_unsent(
                    conversation_id=conversation_id, turn_id=turn_id, brain=brain,
                    provider_key=provider.key, bundle=assembled, error=exc, now=now,
                )
            return self._fail_turn(
                conversation_id, turn_id, exc, turn_started, invocation_ids=unsent
            )
        record_for_turn(self._db, turn_id=turn_id, found=found, bundle=bundle, now=now)

        outcome, attempts, refused = self._generate_with_retry(
            conversation_id=conversation_id,
            turn_id=turn_id,
            brain=brain,
            provider_key=provider.key,
            bundle=bundle,
        )
        if outcome is None:
            # A memory in the bundle was forgotten between the search and the
            # call. The database refuses to record an invocation that includes
            # it, so that call was never made; the turn fails rather than hangs.
            # Any attempt already recorded before the refusal is still reported.
            assert refused is not None
            return self._fail_turn(
                conversation_id, turn_id, refused, turn_started, invocation_ids=attempts
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

        # Step 10 (spec A.2, D.2): persistence intent is detected by code, never
        # by a model. Only then is a proposal structured, and whatever becomes
        # of it, the reply above stands (spec D.3/8).
        proposal: Proposal | None = None
        capture_failed = False
        if conversation["mode"] == MODE_PERSONAL and (
            propose_memory or detect_persistence_intent(text) is not None
        ):
            proposal, proposal_attempts = self._propose(
                conversation_id=conversation_id,
                turn_id=turn_id,
                message_id=message["id"],
                text=text,
                brain=brain,
                provider_key=provider.key,
                budget=bundle_budget,
                now=now,
            )
            attempts = (*attempts, *proposal_attempts)
            capture_failed = proposal is None

        return self._complete_turn(
            conversation_id=conversation_id,
            turn_id=turn_id,
            text=outcome.generation.text,
            truncated=outcome.generation.finish_reason == "length",
            turn_started=turn_started,
            invocation_ids=attempts,
            proposal=proposal,
            capture_failed=capture_failed,
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
        purpose: str = "reply",
        params: GenerationParams | None = None,
        interpret: invocations.Interpret | None = None,
        on_complete: invocations.OnComplete | None = None,
    ) -> tuple[
        invocations.InvocationOutcome | None, tuple[uuid.UUID, ...], InvocationRefusedError | None
    ]:
        """The outcome of the last attempt, and every attempt recorded.

        One retry per purpose, for a transport failure only, as a new
        invocation row (spec L). The outcome is None only when the database
        refused to record an attempt (the third value says why). Attempts
        recorded before that are still returned: each has a row, and the
        turn's result must not say otherwise.
        """
        params = params or GenerationParams()
        attempts: list[uuid.UUID] = []
        retry_of: uuid.UUID | None = None
        outcome = None
        for attempt in range(MAX_TRANSPORT_RETRIES + 1):
            now = self._clock()
            try:
                invocation_id = invocations.open_invocation(
                    self._db,
                    turn_id=turn_id,
                    conversation_id=conversation_id,
                    purpose=purpose,
                    brain=brain,
                    brain_alias=self._brain_alias,
                    provider_key=provider_key,
                    bundle=bundle,
                    params=params,
                    now=now,
                    retry_of=retry_of,
                )
            except InvocationRefusedError as refused:
                return None, tuple(attempts), refused
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
                interpret=interpret,
                on_complete=on_complete,
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
        return outcome, tuple(attempts), None

    def _record_unsent(
        self,
        *,
        conversation_id: uuid.UUID,
        turn_id: uuid.UUID,
        brain: Brain,
        provider_key: str,
        bundle: ContextBundle,
        error: BaseException,
        now: datetime,
    ) -> tuple[uuid.UUID, ...]:
        """Record the reply that could not be sent, with its manifest. No model is called."""
        try:
            return (
                invocations.record_unsent(
                    self._db,
                    turn_id=turn_id,
                    conversation_id=conversation_id,
                    purpose="reply",
                    brain=brain,
                    brain_alias=self._brain_alias,
                    provider_key=provider_key,
                    bundle=bundle,
                    params=GenerationParams(),
                    error=error,
                    now=now,
                ),
            )
        except InvocationRefusedError:
            # A memory in it was forgotten since the search. The database will
            # not record a manifest that includes it, and the turn fails anyway.
            return ()

    def _propose(
        self,
        *,
        conversation_id: uuid.UUID,
        turn_id: uuid.UUID,
        message_id: uuid.UUID,
        text: str,
        brain: Brain,
        provider_key: str,
        budget: Budget,
        now: datetime,
    ) -> tuple[Proposal | None, tuple[uuid.UUID, ...]]:
        """Structure one proposal from the message. Returns it, and the attempts made.

        The bundle is the structuring rules and the source message, with no
        identity and no memory (spec D.3), so existing memories cannot leak
        into a proposal. The pending row commits in the same transaction as
        the invocation's completion (spec A.2, Tp).

        Nothing here can fail the turn (spec D.3/8, L). A reply that is not a
        structured claim, a transport failure and its one retry, a refused
        row: each is a recorded failed invocation where one was made, no
        proposal, and a note in the transcript. And nothing here creates a
        memory: that takes a separate call by the person, naming the proposal.
        """
        attempts: tuple[uuid.UUID, ...] = ()
        # What the completion transaction wrote. Read only after that
        # transaction has committed (`outcome.ok`), and never fetched again: a
        # second read that failed would report a saved proposal as a failed one.
        recorded: list[Proposal] = []
        try:
            bundle = compile_context(
                CompileRequest(
                    purpose=Purpose.MEMORY_PROPOSAL,
                    user_message=text,
                    user_message_ref=str(message_id),
                    now=now,
                    budget=budget,
                    estimator=brain.capabilities().estimator,
                )
            )

            def record(
                uow: UnitOfWork, invocation_id: uuid.UUID, claim: ProposedClaim, at: datetime
            ) -> None:
                proposal = proposals.propose(
                    uow,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    model_invocation_id=invocation_id,
                    source_message_id=message_id,
                    claim=claim,
                    now=at,
                )
                recorded[:] = [proposal]
                uow.record(
                    AuditEvent(
                        event_type=EventType.MEMORY_PROPOSED,
                        actor=Actor.MODEL,
                        subject_kind="memory_proposal",
                        subject_id=proposal.id,
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        occurred_at=at,
                        payload={
                            "invocation_id": str(invocation_id),
                            "scope": str(claim.scope),
                            "kind": str(claim.kind),
                            "status": str(proposals.ProposalStatus.PENDING),
                        },
                    )
                )

            outcome, attempts, _ = self._generate_with_retry(
                conversation_id=conversation_id,
                turn_id=turn_id,
                brain=brain,
                provider_key=provider_key,
                bundle=bundle,
                purpose="memory_proposal",
                params=PROPOSAL_PARAMS,
                interpret=_structured_claim,
                on_complete=record,
            )
            if outcome is None or not outcome.ok:
                return None, attempts
            return recorded[0], attempts
        except Exception:  # noqa: BLE001 - memory capture never takes the conversation down
            log.error("proposal.failed", extra={"turn_id": str(turn_id)}, exc_info=True)
            return None, attempts

    def _complete_turn(
        self,
        *,
        conversation_id: uuid.UUID,
        turn_id: uuid.UUID,
        text: str,
        truncated: bool,
        turn_started: float,
        invocation_ids: tuple[uuid.UUID, ...],
        proposal: Proposal | None = None,
        capture_failed: bool = False,
    ) -> TurnResult:
        now = self._clock()
        latency = max(1, int((time.monotonic() - turn_started) * 1000))
        try:
            with unit_of_work(self._db) as uow:
                TurnRepository(uow).lock_started(turn_id)
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
                if capture_failed:
                    # Durable and human-visible; never re-enters model context.
                    note = MessageRepository(uow).append(
                        conversation_id=conversation_id,
                        role="system_note",
                        content=SYSTEM_NOTE_CAPTURE_FAILED,
                        now=now,
                        turn_id=turn_id,
                    )
                    uow.record(
                        AuditEvent(
                            event_type=EventType.MESSAGE_CREATED,
                            actor=Actor.SYSTEM,
                            subject_kind="message",
                            subject_id=note["id"],
                            conversation_id=conversation_id,
                            turn_id=turn_id,
                            occurred_at=now,
                            payload={"message_role": "system_note", "seq": note["seq"]},
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
        except FinalisationConflictError:
            # The reply was not recorded and is not delivered. A proposal was
            # recorded before this, in its own transaction, and still stands.
            return self._as_recorded(turn_id, invocation_ids, proposal=proposal)
        return TurnResult(
            turn_id=turn_id,
            status="completed",
            response=text,
            response_message_id=message["id"],
            invocation_ids=invocation_ids,
            proposal=proposal,
            capture_failed=capture_failed,
            truncated=truncated,
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
        note = system_note_for_error(exc)
        try:
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
        except FinalisationConflictError:
            return self._as_recorded(turn_id, invocation_ids)
        return TurnResult(
            turn_id=turn_id,
            status="failed",
            response=None,
            response_message_id=None,
            invocation_ids=invocation_ids,
            error_kind=str(kind),
        )

    def _as_recorded(
        self,
        turn_id: uuid.UUID,
        invocation_ids: tuple[uuid.UUID, ...],
        *,
        proposal: Proposal | None = None,
    ) -> TurnResult:
        """The turn was finalised before this attempt to: report what is recorded.

        Orphan recovery (spec A.2) can mark a turn interrupted while its model
        call is still running. The late finalisation rolled back whole, so the
        transcript gains no reply and no note, and the caller is told what the
        rows say rather than what the late result would have said.

        `proposal` is a pending proposal this turn had already committed. It is
        a row too, so it is reported: left out, it would sit pending with its
        text and nobody would have been offered it.
        """
        with unit_of_work(self._db, expect_audit=False) as uow:
            turn = TurnRepository(uow).get(turn_id)
        assert turn is not None
        log.warning(
            "turn.late_result_discarded",
            extra={"turn_id": str(turn_id), "status": turn["status"]},
        )
        return TurnResult(
            turn_id=turn_id,
            status=turn["status"],
            response=None,
            response_message_id=turn["response_message_id"],
            invocation_ids=invocation_ids,
            error_kind=turn["error_kind"],
            proposal=proposal,
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
            response, truncated = None, False
            if turn["response_message_id"]:
                cur = uow.execute(
                    "SELECT content, truncated FROM message WHERE id = %s",
                    (turn["response_message_id"],),
                )
                row = cur.fetchone()
                if row:
                    response, truncated = row["content"], bool(row["truncated"])
            recorded = InvocationRepository(uow).for_turn(turn["id"])
            invocation_ids = tuple(row["id"] for row in recorded)
            # Still offered if it is still pending: a proposal must not die
            # because the client asked twice (spec C.9).
            proposal = proposals.for_turn(uow, turn["id"])
            # And if it could not be made, that is said again. A proposal row
            # is never removed, so a structuring call with no row is a capture
            # that failed; the note covers a failure before any call was made.
            noted = uow.execute(
                "SELECT 1 FROM message WHERE turn_id = %s AND role = 'system_note'"
                " AND content = %s",
                (turn["id"], SYSTEM_NOTE_CAPTURE_FAILED),
            ).fetchone()
            capture_failed = (
                turn["status"] == "completed"
                and proposal is None
                and (noted is not None
                     or any(row["purpose"] == "memory_proposal" for row in recorded))
            )
        return TurnResult(
            turn_id=turn["id"],
            status=turn["status"],
            response=response,
            response_message_id=turn["response_message_id"],
            invocation_ids=invocation_ids,
            error_kind=turn["error_kind"],
            replayed=True,
            capture_failed=capture_failed,
            truncated=truncated,
            proposal=(
                proposal if proposal is not None
                and proposal.status is proposals.ProposalStatus.PENDING else None
            ),
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


def _previous_user_message(history: tuple[HistoryMessage, ...]) -> str | None:
    """Janu's last message before this one, in this conversation (spec E.3)."""
    for item in reversed(history):
        if item.role == "user":
            return item.content
    return None


def _structured_claim(generation: Generation) -> ProposedClaim:
    """The claim in a structuring reply. A cut-off reply is not a claim."""
    if generation.finish_reason == "length":
        raise ProposalStructureError("the structuring reply was cut off")
    return proposals.parse_structure(generation.text)
