"""Memory proposals: structuring, and every resolution that creates nothing (spec C.9, D.3).

A proposal is what a model made of one message Janu asked to be remembered.
It is not a memory: it is never retrieved, never enters a context, and nothing
in this module can turn one into a memory. That single step is
`apollo.memory.approval.approve`, which only the user-facing surface reaches
(spec D.3/2: the model cannot approve its own proposal).

What lives here:

- reading the model's structuring reply into a claim, strictly enough that a
  reply which is not a claim is a recorded failure rather than a guess;
- creating the pending row;
- ignoring and expiring, which end a proposal without creating anything.

Proposal text is transient (spec C.9). Every resolution clears it in the
resolving statement; what survives is metadata that cannot carry a secret.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from apollo.errors import ApolloError, ErrorKind
from apollo.memory.models import Kind, MemoryInputError, Scope, validate_claim
from apollo.storage.repositories import ProposalRepository
from apollo.storage.unit_of_work import UnitOfWork

#: Proposals older than this are expired and never revived (spec D.3/7, P.2).
DEFAULT_EXPIRY = timedelta(days=7)

#: The audit reason on a proposal declined because a memory citing its source
#: message was forgotten, so it can be told apart from one Janu ignored himself.
DECLINED_BY_TOMBSTONE = "memory_tombstoned"

#: A proposal can never be about Apollo himself: that would be self-model
#: mutation by way of a model's output. Direct entry only (spec D.3/5).
PROPOSAL_SCOPES = (Scope.USER, Scope.RELATIONSHIP, Scope.WORLD)

#: A subject is a short topic label and a claim is one sentence. A reply far
#: outside these is not the structure that was asked for.
SUBJECT_MAX = 80
CONTENT_MAX = 600


class ProposalStatus(StrEnum):
    PENDING = "pending"
    SAVED = "saved"
    SAVED_EDITED = "saved_edited"
    IGNORED = "ignored"
    EXPIRED = "expired"


class ProposalStructureError(ApolloError):
    """The model's reply is not the structured claim that was asked for.

    Content-level, so it is never retried (spec L). The message names what was
    wrong with the shape and never quotes the reply.
    """

    kind = ErrorKind.MALFORMED_GENERATION


class ProposalError(ApolloError):
    """A proposal cannot be resolved as asked. The message never repeats its text."""


class ProposalNotFoundError(ApolloError):
    """No proposal has that id."""


@dataclass(frozen=True)
class ProposedClaim:
    scope: Scope
    kind: Kind
    subject: str
    content: str


@dataclass(frozen=True)
class Proposal:
    """A proposal as read back. `subject` and `content` are None once it is resolved."""

    id: uuid.UUID
    conversation_id: uuid.UUID
    turn_id: uuid.UUID
    model_invocation_id: uuid.UUID
    source_message_id: uuid.UUID
    scope: Scope
    kind: Kind
    subject: str | None
    content: str | None
    status: ProposalStatus
    resulting_memory_id: uuid.UUID | None
    created_at: datetime
    resolved_at: datetime | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Proposal:
        return cls(
            id=row["id"],
            conversation_id=row["conversation_id"],
            turn_id=row["turn_id"],
            model_invocation_id=row["model_invocation_id"],
            source_message_id=row["source_message_id"],
            scope=Scope(row["scope"]),
            kind=Kind(row["kind"]),
            subject=row["subject"],
            content=row["content"],
            status=ProposalStatus(row["status"]),
            resulting_memory_id=row["resulting_memory_id"],
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
        )

    def expired_by(self, now: datetime, expiry: timedelta = DEFAULT_EXPIRY) -> bool:
        return self.status is ProposalStatus.PENDING and self.created_at <= now - expiry


# -- reading the structuring reply -----------------------------------------------------

_LABELS = ("scope", "kind", "subject", "content")
#: `label: value`, allowing the list marker or emphasis a chat model tends to add.
_LINE = re.compile(
    r"^\s*(?:[-*•]\s*)?[*_`]*\s*(scope|kind|subject|content)\s*[*_`]*\s*:\s*[*_`]*\s*(.*?)\s*$",
    re.IGNORECASE,
)
_QUOTES = (('"', '"'), ("'", "'"), ("“", "”"), ("`", "`"))


def parse_structure(text: str) -> ProposedClaim:
    """The claim in a structuring reply, or `ProposalStructureError`.

    The reply was asked for as four labelled lines. Each label must appear
    exactly once with a value; lines that carry no label are ignored, so a
    stray preamble does not cost Janu the capture, but a reply that leaves a
    field out, repeats one, or names a scope or kind outside the closed lists
    is not guessed at.
    """
    found: dict[str, str] = {}
    for line in text.splitlines():
        match = _LINE.match(line)
        if match is None:
            continue
        label, value = match.group(1).lower(), _unquoted(match.group(2))
        if label in found:
            raise ProposalStructureError(f"the reply gives `{label}` more than once")
        found[label] = value
    missing = [label for label in _LABELS if not found.get(label)]
    if missing:
        raise ProposalStructureError(f"the reply has no {', '.join(missing)}")

    scope, kind = _enum_word(found["scope"]), _enum_word(found["kind"])
    if scope not in {s.value for s in PROPOSAL_SCOPES}:
        # `self` lands here too: the model is never offered it, and may not choose it.
        raise ProposalStructureError("the reply's scope is not one a proposal may have")
    if kind not in {k.value for k in Kind}:
        raise ProposalStructureError("the reply's kind is not a memory kind")
    subject, content = found["subject"], found["content"]
    if len(subject) > SUBJECT_MAX:
        raise ProposalStructureError("the reply's subject is too long to be a topic label")
    if len(content) > CONTENT_MAX:
        raise ProposalStructureError("the reply's content is too long to be one claim")
    try:
        validate_claim(subject, content)
    except MemoryInputError:
        raise ProposalStructureError("the reply's claim cannot be stored as given") from None
    return ProposedClaim(Scope(scope), Kind(kind), subject, content)


def _unquoted(value: str) -> str:
    value = value.strip().rstrip("*_").strip()
    for opening, closing in _QUOTES:
        if len(value) >= 2 and value.startswith(opening) and value.endswith(closing):
            return value[1:-1].strip()
    return value


def _enum_word(value: str) -> str:
    return value.strip().strip(".,;").lower()


# -- the pending row -------------------------------------------------------------------


def propose(
    uow: UnitOfWork,
    *,
    conversation_id: uuid.UUID,
    turn_id: uuid.UUID,
    model_invocation_id: uuid.UUID,
    source_message_id: uuid.UUID,
    claim: ProposedClaim,
    now: datetime,
) -> uuid.UUID:
    """Record a pending proposal. At most one per turn; the database refuses a second."""
    if claim.scope not in PROPOSAL_SCOPES:
        raise ProposalError("a proposal cannot have scope self; use direct entry")
    return ProposalRepository(uow).insert_pending(
        conversation_id=conversation_id,
        turn_id=turn_id,
        model_invocation_id=model_invocation_id,
        source_message_id=source_message_id,
        scope=claim.scope,
        kind=claim.kind,
        subject=claim.subject,
        content=claim.content,
        now=now,
    )


# -- resolutions that create nothing ---------------------------------------------------


def locked_pending(repo: ProposalRepository, proposal_id: uuid.UUID) -> Proposal:
    """The proposal, locked, and still pending. Otherwise it cannot be resolved."""
    row = repo.lock(proposal_id)
    if row is None:
        raise ProposalNotFoundError("no proposal has that id")
    proposal = Proposal.from_row(row)
    if proposal.status is not ProposalStatus.PENDING:
        raise ProposalError(f"that proposal is already {proposal.status}")
    return proposal


def ignore(uow: UnitOfWork, proposal_id: uuid.UUID, *, now: datetime) -> Proposal:
    """Decline a proposal. No memory is created and its text is cleared."""
    repo = ProposalRepository(uow)
    proposal = locked_pending(repo, proposal_id)
    repo.resolve(proposal_id, status=ProposalStatus.IGNORED, resulting_memory_id=None, now=now)
    return proposal


def expire(
    uow: UnitOfWork, *, now: datetime, expiry: timedelta = DEFAULT_EXPIRY
) -> list[Proposal]:
    """Expire every pending proposal older than the window. Never revived (spec D.3/7)."""
    repo = ProposalRepository(uow)
    due = [Proposal.from_row(row) for row in repo.lock_pending_before(now - expiry)]
    for proposal in due:
        repo.resolve(
            proposal.id, status=ProposalStatus.EXPIRED, resulting_memory_id=None, now=now
        )
    return due


def decline_for_forgotten(
    uow: UnitOfWork, memory_ids: list[uuid.UUID], *, now: datetime
) -> list[Proposal]:
    """Decline every pending proposal structured from a message these memories cite.

    Called by the tombstone, in its transaction (spec D.7/3). Such a proposal
    is Apollo's own wording of the claim being forgotten, made from the same
    message: left pending it would keep the claim, and offer it back. It is
    resolved as `ignored`, the status for a claim Janu declined to keep, and
    its text is cleared with it. A proposal from any other message is not
    touched: nothing says it is the same claim, and nothing here guesses.
    """
    repo = ProposalRepository(uow)
    due = [Proposal.from_row(row) for row in repo.lock_pending_from_sources_of(memory_ids)]
    for proposal in due:
        repo.resolve(
            proposal.id, status=ProposalStatus.IGNORED, resulting_memory_id=None, now=now
        )
    return due


def get(uow: UnitOfWork, proposal_id: uuid.UUID) -> Proposal:
    row = ProposalRepository(uow).get(proposal_id)
    if row is None:
        raise ProposalNotFoundError("no proposal has that id")
    return Proposal.from_row(row)


def for_turn(uow: UnitOfWork, turn_id: uuid.UUID) -> Proposal | None:
    row = ProposalRepository(uow).for_turn(turn_id)
    return None if row is None else Proposal.from_row(row)


def listed(uow: UnitOfWork, statuses: tuple[ProposalStatus, ...]) -> list[Proposal]:
    rows = ProposalRepository(uow).list(tuple(str(s) for s in statuses))
    return [Proposal.from_row(row) for row in rows]
