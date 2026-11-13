"""The proposal surface: Save, Edit and Save, Ignore (spec D.3).

This module is what a person's client calls to resolve a proposal, and it is
the only route to `apollo.memory.approval`. Creating a memory from a proposal
takes a call here that names the proposal id (spec D.3/2). Nothing on the
generation path imports this module, which an architecture test holds: the
model can structure a proposal, and cannot approve one.

Each function owns its transaction, and records the audit events in it, so
the resolution, the memory it may create and their events commit together or
not at all (spec C.10).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from apollo.audit.events import Actor, AuditEvent, EventType
from apollo.memory import proposals
from apollo.memory.approval import Approval, ClaimEdit, approve
from apollo.memory.models import (
    Kind,
    MemoryInputError,
    Origin,
    OriginTier,
    Scope,
    SourceKind,
    parse_kind,
    parse_scope,
)
from apollo.memory.proposals import (
    DEFAULT_EXPIRY,
    Proposal,
    ProposalError,
    ProposalNotFoundError,
    ProposalStatus,
)
from apollo.storage.db import Database
from apollo.storage.repositories import ProposalRepository
from apollo.storage.unit_of_work import UnitOfWork, unit_of_work

# Callers may import `core/` but not `memory/` (spec A.3), so the types they
# need are re-exported here.
__all__ = [
    "DEFAULT_EXPIRY",
    "Kind",
    "MemoryInputError",
    "Proposal",
    "ProposalError",
    "ProposalNotFoundError",
    "ProposalStatus",
    "Saved",
    "Scope",
    "capture_counts",
    "expire_proposals",
    "get_proposal",
    "ignore_proposal",
    "list_proposals",
    "save_proposal",
]


@dataclass(frozen=True)
class Saved:
    """What a save did. `memory_id` is None when the proposal had expired instead."""

    status: ProposalStatus
    memory_id: uuid.UUID | None


def _resolved(
    uow: UnitOfWork,
    proposal: Proposal,
    status: ProposalStatus,
    now: datetime,
    *,
    actor: Actor,
    memory_id: uuid.UUID | None = None,
) -> None:
    payload: dict[str, str | None] = {
        "status": str(status),
        "scope": str(proposal.scope),
        "kind": str(proposal.kind),
    }
    if memory_id is not None:
        payload["memory_id"] = str(memory_id)
    uow.record(
        AuditEvent(
            event_type=EventType.MEMORY_PROPOSAL_RESOLVED,
            actor=actor,
            subject_kind="memory_proposal",
            subject_id=proposal.id,
            conversation_id=proposal.conversation_id,
            turn_id=proposal.turn_id,
            occurred_at=now,
            payload=payload,
        )
    )


def _created(uow: UnitOfWork, approval: Approval, now: datetime) -> None:
    change = approval.change
    assert change is not None
    uow.record(
        AuditEvent(
            event_type=EventType.MEMORY_CREATED,
            actor=Actor.USER,  # the user authorised it; the structuring is in source_kind
            subject_kind="memory",
            subject_id=change.memory_id,
            conversation_id=approval.proposal.conversation_id,
            turn_id=approval.proposal.turn_id,
            occurred_at=now,
            payload={
                "status": str(change.status),
                "observation_id": str(change.observation_id),
                "source_kind": str(SourceKind.MODEL_PROPOSAL_APPROVED),
                "origin_tier": str(OriginTier.USER_ASSERTED),
                "origin": str(Origin.PERSONAL),
                "pinned": False,
            },
        )
    )


def save_proposal(
    db: Database,
    proposal_id: uuid.UUID,
    *,
    now: datetime,
    scope: str | None = None,
    kind: str | None = None,
    subject: str | None = None,
    content: str | None = None,
    expiry: timedelta = DEFAULT_EXPIRY,
) -> Saved:
    """Save a pending proposal as a memory: as proposed, or with the given edits.

    The memory is `user_asserted`, with `source_kind = model_proposal_approved`
    and an observation pointing at the originating message. A proposal past
    the expiry window is expired instead and creates nothing.
    """
    edit = ClaimEdit(
        scope=None if scope is None else parse_scope(scope),
        kind=None if kind is None else parse_kind(kind),
        subject=subject,
        content=content,
    )
    with unit_of_work(db) as uow:
        approval = approve(uow, proposal_id, now=now, edit=edit, expiry=expiry)
        if approval.change is None:
            _resolved(uow, approval.proposal, approval.status, now, actor=Actor.SYSTEM)
        else:
            _created(uow, approval, now)
            _resolved(uow, approval.proposal, approval.status, now, actor=Actor.USER,
                      memory_id=approval.memory_id)
    return Saved(approval.status, approval.memory_id)


def ignore_proposal(db: Database, proposal_id: uuid.UUID, *, now: datetime) -> None:
    """Decline a proposal. Nothing is created and its text is cleared."""
    with unit_of_work(db) as uow:
        proposal = proposals.ignore(uow, proposal_id, now=now)
        _resolved(uow, proposal, ProposalStatus.IGNORED, now, actor=Actor.USER)


def expire_proposals(
    db: Database, *, now: datetime, expiry: timedelta = DEFAULT_EXPIRY
) -> int:
    """Expire pending proposals older than the window. Returns how many."""
    with unit_of_work(db) as uow:
        due = proposals.expire(uow, now=now, expiry=expiry)
        for proposal in due:
            _resolved(uow, proposal, ProposalStatus.EXPIRED, now, actor=Actor.SYSTEM)
        if not due:
            uow.audit_only()  # nothing mutated, nothing to record
    return len(due)


def get_proposal(db: Database, proposal_id: uuid.UUID) -> Proposal:
    with unit_of_work(db, expect_audit=False) as uow:
        return proposals.get(uow, proposal_id)


def list_proposals(
    db: Database, *, statuses: tuple[ProposalStatus, ...] = (ProposalStatus.PENDING,)
) -> list[Proposal]:
    with unit_of_work(db, expect_audit=False) as uow:
        return proposals.listed(uow, statuses)


def capture_counts(db: Database) -> dict[str, int]:
    """Proposals by status: the capture-path evidence the product gate asks for (spec O.2)."""
    with unit_of_work(db, expect_audit=False) as uow:
        counted = ProposalRepository(uow).counts_by_status()
    return {str(status): counted.get(str(status), 0) for status in ProposalStatus}
