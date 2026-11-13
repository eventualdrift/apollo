"""Approving a proposal: the one place a proposal becomes a memory (spec D.3).

Deliberately its own module. "The model cannot approve its own proposal"
(spec D.3/2) is a property of what can reach this function: the turn, the
invocation path, the compiler and the adapters import none of it, which an
architecture test holds, and the database refuses an approved-proposal memory
that no saved proposal accounts for (migration 0005). Creation needs a
separate call naming the proposal id, made by the person.

Saving creates a `user_asserted` memory whose `asserts` observation records
`model_proposal_approved`, the originating message's id and an excerpt of it:
the structuring was a model's, the authorisation the user's, and both stay
visible (spec D.3/3, D.3/4). In the same transaction the proposal's text is
cleared (spec C.9): the memory row now holds the claim, and a second copy
would undermine both minimisation and deletion.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from apollo.memory import lifecycle
from apollo.memory.lifecycle import Change
from apollo.memory.models import Kind, Origin, OriginTier, Scope, SourceKind
from apollo.memory.proposals import (
    DEFAULT_EXPIRY,
    PROPOSAL_SCOPES,
    Proposal,
    ProposalError,
    ProposalStatus,
    locked_pending,
)
from apollo.storage.repositories import ProposalRepository
from apollo.storage.unit_of_work import UnitOfWork

#: How much of the originating message the observation quotes.
EXCERPT_MAX = 1000


@dataclass(frozen=True)
class ClaimEdit:
    """What the person changed before saving. None means "as proposed"."""

    scope: Scope | None = None
    kind: Kind | None = None
    subject: str | None = None
    content: str | None = None


@dataclass(frozen=True)
class Approval:
    proposal: Proposal
    status: ProposalStatus
    #: The memory's creation, or None when the proposal had expired instead.
    change: Change | None

    @property
    def memory_id(self) -> uuid.UUID | None:
        return None if self.change is None else self.change.memory_id


def approve(
    uow: UnitOfWork,
    proposal_id: uuid.UUID,
    *,
    now: datetime,
    edit: ClaimEdit | None = None,
    expiry: timedelta = DEFAULT_EXPIRY,
) -> Approval:
    """Save a pending proposal as a memory, as proposed or as edited.

    A proposal past the expiry window is expired here rather than saved: it is
    never revived (spec D.3/7), and the caller is told which happened.
    """
    repo = ProposalRepository(uow)
    proposal = locked_pending(repo, proposal_id)
    if proposal.expired_by(now, expiry):
        repo.resolve(
            proposal_id, status=ProposalStatus.EXPIRED, resulting_memory_id=None, now=now
        )
        return Approval(proposal, ProposalStatus.EXPIRED, None)

    edit = edit or ClaimEdit()
    scope = edit.scope or proposal.scope
    kind = edit.kind or proposal.kind
    subject = edit.subject if edit.subject is not None else proposal.subject
    content = edit.content if edit.content is not None else proposal.content
    if scope not in PROPOSAL_SCOPES:
        # Also when the edit asks for it: self-model mutation is direct entry only.
        raise ProposalError("a proposal cannot create a self-scope memory; use direct entry")
    if subject is None or content is None:  # a pending proposal always has both
        raise ProposalError("that proposal has no text to save")
    edited = (scope, kind, subject, content) != (
        proposal.scope, proposal.kind, proposal.subject, proposal.content,
    )

    source = repo.source_message_content(proposal.source_message_id)
    change = lifecycle.create(
        uow,
        scope=scope,
        kind=kind,
        subject=subject,
        content=content,
        origin_tier=OriginTier.USER_ASSERTED,
        origin=Origin.PERSONAL,
        pinned=False,
        source_kind=SourceKind.MODEL_PROPOSAL_APPROVED,
        now=now,
        message_id=proposal.source_message_id,
        excerpt=None if source is None else source[:EXCERPT_MAX],
    )
    status = ProposalStatus.SAVED_EDITED if edited else ProposalStatus.SAVED
    repo.resolve(proposal_id, status=status, resulting_memory_id=change.memory_id, now=now)
    return Approval(proposal, status, change)
