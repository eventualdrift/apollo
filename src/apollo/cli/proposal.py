"""`apollo proposal`: Save, Edit and Save, or Ignore what Apollo offered to remember (spec D.3).

A thin client over `apollo.core.proposals`. A proposal is created when a
message asks Apollo to remember something; it is not a memory until Janu
saves it here, and a proposal left alone expires. The same two rules as
`apollo memory` keep claim text where it belongs:

* **Claim text never comes from argv.** An edit is read from stdin: prompted
  for at a terminal, or piped (first line the subject, the rest the content).
* **An error prints as its kind, never its text.**
"""

from __future__ import annotations

import argparse
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from apollo.cli.memory import Terminal, _prompted
from apollo.core.proposals import (
    Kind,
    MemoryInputError,
    Proposal,
    ProposalError,
    ProposalNotFoundError,
    ProposalStatus,
    Scope,
    capture_counts,
    expire_proposals,
    get_proposal,
    ignore_proposal,
    list_proposals,
    save_proposal,
)
from apollo.storage.db import Database

#: A proposal may not be about Apollo himself; that is direct entry only.
_SCOPES = [s.value for s in Scope if s is not Scope.SELF]


class InvalidProposalId(ValueError):
    """An argument that is not a proposal id."""


_HINTS: dict[type[Exception], str] = {
    MemoryInputError: "the subject or content was empty or not valid text",
    ProposalError: "that proposal can no longer be resolved that way "
                   "(see `apollo proposal show`)",
    ProposalNotFoundError: "no proposal has that id",
    InvalidProposalId: "that is not a proposal id",
}


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p_proposal = sub.add_parser(
        "proposal",
        help="save, edit or ignore what Apollo offered to remember; nothing is a memory "
             "until you save it",
    )
    proposal_sub = p_proposal.add_subparsers(dest="proposal_command", required=True)

    p_list = proposal_sub.add_parser("list", help="list proposals (default: pending)")
    p_list.add_argument("--status", action="append",
                        choices=[s.value for s in ProposalStatus] + ["all"],
                        help="repeatable; `all` lists every status")
    proposal_sub.add_parser("show", help="print one proposal").add_argument("id")
    proposal_sub.add_parser(
        "save", help="save a proposal as a memory, as proposed"
    ).add_argument("id")
    p_edit = proposal_sub.add_parser(
        "edit",
        help="change a proposal and save it; the subject and content are prompted for, "
             "or piped (first line the subject, the rest the content; blank keeps)",
    )
    p_edit.add_argument("id")
    p_edit.add_argument("--scope", choices=_SCOPES)
    p_edit.add_argument("--kind", choices=[k.value for k in Kind])
    proposal_sub.add_parser(
        "ignore", help="decline a proposal; nothing is saved"
    ).add_argument("id")
    proposal_sub.add_parser(
        "stats", help="how many proposals were saved, edited, ignored or left to expire"
    )


def run(args: argparse.Namespace, db: Database, term: Terminal, *, expiry: timedelta) -> int:
    command = args.proposal_command
    try:
        # A proposal past its window is never offered or saved (spec D.3/7).
        expire_proposals(db, now=term.clock(), expiry=expiry)
        return _COMMANDS[command](args, db, term, expiry)
    except Exception as exc:  # the kind only: a message may quote a refused row
        kind = type(exc).__name__
        hint = _HINTS.get(type(exc))
        print(f"proposal {command}: {kind}" + (f" — {hint}" if hint else ""), file=term.stderr)
        return 1


# -- offering a proposal after a turn ---------------------------------------------------


def describe(proposal: Proposal) -> list[str]:
    """The proposal as it is offered: what would be saved, and how to decide."""
    return [
        f"memory proposal {proposal.id}  [{proposal.scope}/{proposal.kind}] "
        f"{_text(proposal.subject)}",
        f"  {_text(proposal.content)}",
    ]


def offer(proposal: Proposal, term: Terminal) -> None:
    """Print a pending proposal and the three commands that resolve it."""
    for line in describe(proposal):
        print(line, file=term.stdout)
    print(
        f"  save: apollo proposal save {proposal.id}\n"
        f"  edit: apollo proposal edit {proposal.id}\n"
        f"  ignore: apollo proposal ignore {proposal.id}",
        file=term.stdout,
    )


def ask(proposal: Proposal, db: Database, term: Terminal, *, expiry: timedelta) -> None:
    """In a conversation at a terminal: ask now. Anything else leaves it pending."""
    for line in describe(proposal):
        print(line, file=term.stdout)
    answer = _prompted(
        term, "  [s]ave, [e]dit, [i]gnore, or enter to decide later: "
    ).strip().lower()
    try:
        if answer in {"s", "save"}:
            _report(save_proposal(db, proposal.id, now=term.clock(), expiry=expiry).status, term)
        elif answer in {"e", "edit"}:
            subject = _prompted(term, f"  subject [{_text(proposal.subject)}]: ").strip()
            content = _prompted(term, "  content (enter keeps it): ").strip()
            saved = save_proposal(
                db, proposal.id, now=term.clock(), expiry=expiry,
                subject=subject or None, content=content or None,
            )
            _report(saved.status, term)
        elif answer in {"i", "ignore"}:
            ignore_proposal(db, proposal.id, now=term.clock())
            print("  ignored; nothing was saved", file=term.stdout)
        else:
            print(f"  left pending: apollo proposal list (expires in {expiry.days} days)",
                  file=term.stdout)
    except Exception as exc:  # the kind only
        print(f"  not resolved: {type(exc).__name__}; it stays pending", file=term.stderr)


def _report(status: ProposalStatus, term: Terminal) -> None:
    said = {
        ProposalStatus.SAVED: "saved as a memory",
        ProposalStatus.SAVED_EDITED: "saved as a memory, as edited",
        ProposalStatus.EXPIRED: "that proposal had expired; nothing was saved",
    }[status]
    print(f"  {said}", file=term.stdout)


# -- commands -----------------------------------------------------------------------------

_Command = Callable[[argparse.Namespace, Database, Terminal, timedelta], int]


def _proposal_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise InvalidProposalId() from None


def _text(value: str | None) -> str:
    return "(cleared)" if value is None else value


def _list(args: argparse.Namespace, db: Database, term: Terminal, expiry: timedelta) -> int:
    wanted = args.status or [ProposalStatus.PENDING.value]
    statuses = (tuple(ProposalStatus) if "all" in wanted
                else tuple(ProposalStatus(s) for s in wanted))
    found = list_proposals(db, statuses=statuses)
    for proposal in found:
        print(
            f"{proposal.id}  {proposal.status:<12} {proposal.scope}/{proposal.kind}"
            f"  {_age(proposal, term.clock())}  {_text(proposal.subject)}",
            file=term.stdout,
        )
        if proposal.content is not None:
            print(f"    {proposal.content}", file=term.stdout)
    if not found:
        print("no proposals", file=term.stderr)
    return 0


def _age(proposal: Proposal, now: datetime) -> str:
    return f"{max((now - proposal.created_at).days, 0)}d"


def _show(args: argparse.Namespace, db: Database, term: Terminal, expiry: timedelta) -> int:
    proposal = get_proposal(db, _proposal_id(args.id))
    lines = [
        f"id:          {proposal.id}",
        f"status:      {proposal.status}",
        f"scope/kind:  {proposal.scope}/{proposal.kind}",
        f"created:     {proposal.created_at.isoformat()}",
        f"from turn:   {proposal.turn_id}",
    ]
    if proposal.resolved_at is not None:
        lines.append(f"resolved:    {proposal.resolved_at.isoformat()}")
    if proposal.resulting_memory_id is not None:
        lines.append(f"memory:      {proposal.resulting_memory_id}")
    lines += [f"subject:     {_text(proposal.subject)}", f"content:     {_text(proposal.content)}"]
    for line in lines:
        print(line, file=term.stdout)
    return 0


def _save(args: argparse.Namespace, db: Database, term: Terminal, expiry: timedelta) -> int:
    saved = save_proposal(db, _proposal_id(args.id), now=term.clock(), expiry=expiry)
    return _print_saved(saved.status, saved.memory_id, term)


def _edit(args: argparse.Namespace, db: Database, term: Terminal, expiry: timedelta) -> int:
    proposal_id = _proposal_id(args.id)
    if term.stdin.isatty():
        proposal = get_proposal(db, proposal_id)
        subject = _prompted(term, f"subject [{_text(proposal.subject)}]: ").strip()
        content = _prompted(term, "content (enter keeps it): ").strip()
    else:
        subject = term.stdin.readline().strip()
        content = term.stdin.read().strip()
    saved = save_proposal(
        db, proposal_id, now=term.clock(), expiry=expiry, scope=args.scope, kind=args.kind,
        subject=subject or None, content=content or None,
    )
    return _print_saved(saved.status, saved.memory_id, term)


def _print_saved(status: ProposalStatus, memory_id: uuid.UUID | None, term: Terminal) -> int:
    if memory_id is None:
        print("that proposal had expired; nothing was saved", file=term.stderr)
        return 1
    print(memory_id, file=term.stdout)
    print("saved, as edited" if status is ProposalStatus.SAVED_EDITED else "saved",
          file=term.stderr)
    return 0


def _ignore(args: argparse.Namespace, db: Database, term: Terminal, expiry: timedelta) -> int:
    proposal_id = _proposal_id(args.id)
    ignore_proposal(db, proposal_id, now=term.clock())
    print(f"ignored {proposal_id}", file=term.stdout)
    return 0


def _stats(args: argparse.Namespace, db: Database, term: Terminal, expiry: timedelta) -> int:
    for status, count in capture_counts(db).items():
        print(f"{status:<12} {count}", file=term.stdout)
    return 0


_COMMANDS: dict[str, _Command] = {
    "list": _list,
    "show": _show,
    "save": _save,
    "edit": _edit,
    "ignore": _ignore,
    "stats": _stats,
}
