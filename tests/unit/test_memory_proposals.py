"""Reading a structuring reply into a claim (spec D.3). No database here."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apollo.errors import ErrorKind
from apollo.memory.models import Kind, Scope
from apollo.memory.proposals import (
    CONTENT_MAX,
    DEFAULT_EXPIRY,
    PROPOSAL_SCOPES,
    SUBJECT_MAX,
    ProposalStatus,
    ProposalStructureError,
    ProposedClaim,
    parse_structure,
)

GOOD = """\
scope: relationship
kind: preference
subject: communication
content: Janu prefers Apollo not to restate his question before answering.
"""
CLAIM = ProposedClaim(
    Scope.RELATIONSHIP, Kind.PREFERENCE, "communication",
    "Janu prefers Apollo not to restate his question before answering.",
)


def test_the_four_labelled_lines_are_read_as_a_claim() -> None:
    assert parse_structure(GOOD) == CLAIM


@pytest.mark.parametrize("reply", [
    "Sure, here it is:\n\n" + GOOD + "\nLet me know if you want changes.",
    "```\n" + GOOD + "```",
    GOOD.replace("scope:", "- **Scope**:").replace("kind:", "- **Kind**:")
        .replace("subject:", "- **Subject**:").replace("content:", "- **Content**:"),
    GOOD.replace("relationship", "Relationship.").replace("preference", "PREFERENCE"),
    GOOD.replace("content: Janu", 'content: "Janu').replace("answering.", 'answering."'),
    "\n\n".join(GOOD.splitlines()),
    GOOD.replace(": ", ":   "),
], ids=["preamble", "code-fence", "markdown-list", "capitalised", "quoted", "spaced-out",
        "extra-spaces"])
def test_the_dressing_a_chat_model_adds_does_not_cost_the_capture(reply: str) -> None:
    assert parse_structure(reply) == CLAIM


@pytest.mark.parametrize(("reply", "why"), [
    ("Janu prefers short answers.", "no scope, kind, subject, content"),
    (GOOD.replace("kind: preference\n", ""), "no kind"),
    (GOOD.replace("subject: communication", "subject:"), "no subject"),
    (GOOD + "content: and a second claim\n", "more than once"),
    (GOOD.replace("relationship", "self"), "scope is not one a proposal may have"),
    (GOOD.replace("relationship", "everything"), "scope is not one a proposal may have"),
    (GOOD.replace("preference", "opinion"), "kind is not a memory kind"),
    (GOOD.replace("communication", "c" * (SUBJECT_MAX + 1)), "subject is too long"),
    (GOOD.replace("Janu prefers", "x" * CONTENT_MAX), "content is too long"),
    ("", "no scope"),
], ids=["prose", "missing-kind", "empty-subject", "repeated", "self", "unknown-scope",
        "unknown-kind", "long-subject", "long-content", "empty"])
def test_a_reply_that_is_not_the_structure_is_refused_not_guessed_at(
    reply: str, why: str
) -> None:
    with pytest.raises(ProposalStructureError, match=why):
        parse_structure(reply)


def test_a_refusal_never_quotes_the_reply() -> None:
    """The reply is model output about a private message (spec H.1)."""
    secret = "the door code is 4123"
    with pytest.raises(ProposalStructureError) as raised:
        parse_structure(f"scope: self\nkind: fact\nsubject: door\ncontent: {secret}")
    assert secret not in str(raised.value) and "4123" not in str(raised.value)
    assert raised.value.kind is ErrorKind.MALFORMED_GENERATION


def test_a_proposal_can_never_be_about_apollo_himself() -> None:
    """Spec D.3/5: self-scope is direct entry only."""
    assert Scope.SELF not in PROPOSAL_SCOPES
    assert set(PROPOSAL_SCOPES) == set(Scope) - {Scope.SELF}


def test_statuses_and_the_default_window_are_the_specs() -> None:
    assert [s.value for s in ProposalStatus] == [
        "pending", "saved", "saved_edited", "ignored", "expired",
    ]
    assert timedelta(days=7) == DEFAULT_EXPIRY


def test_a_nul_in_the_claim_is_refused_before_any_sql() -> None:
    with pytest.raises(ProposalStructureError, match="cannot be stored"):
        parse_structure(GOOD.replace("communication", "comm\x00unication"))


def test_expiry_is_judged_from_creation() -> None:
    from apollo.memory.proposals import Proposal

    created = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
    proposal = Proposal(
        id=None, conversation_id=None, turn_id=None, model_invocation_id=None,  # type: ignore[arg-type]
        source_message_id=None, scope=Scope.USER, kind=Kind.FACT, subject="s",  # type: ignore[arg-type]
        content="c", status=ProposalStatus.PENDING, resulting_memory_id=None,
        created_at=created, resolved_at=None,
    )
    assert not proposal.expired_by(created + timedelta(days=6, hours=23))
    assert proposal.expired_by(created + timedelta(days=7))
