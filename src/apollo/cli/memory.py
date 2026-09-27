"""`apollo memory`: direct entry and the lifecycle, from the terminal (spec D.4, D.9/5).

A thin client over `apollo.core.memories`, which owns the transaction and the
audit event. Two rules keep claim text where it belongs:

* **Claim text never comes from argv.** A command-line argument lands in shell
  history and the process list, and a tombstone cannot reach either. Subject
  and content are read from stdin: prompted for at a terminal, or piped (for
  `add`, the first line is the subject and the rest is the content).
* **An error prints as its kind, never its text.** A database error's message
  can quote the row it refused ("Failing row contains (...)"), so no exception
  message reaches the terminal: only the exception's class name and a fixed
  hint for the kinds Apollo raises itself.
"""

from __future__ import annotations

import argparse
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TextIO

from apollo.core.memories import (
    Kind,
    LifecycleError,
    Memory,
    MemoryInputError,
    MemoryNotFoundError,
    Scope,
    Status,
    archive_memory,
    confirm_memory,
    contradict_memory,
    correct_memory,
    create_memory,
    get_memory,
    list_memories,
    restore_memory,
    tombstone_memory,
)
from apollo.storage.db import Database


class InvalidMemoryId(ValueError):
    """An argument that is not a memory id."""


_HINTS: dict[type[Exception], str] = {
    MemoryInputError: "the subject or content was empty or not valid text",
    LifecycleError: "not allowed from the memory's current status (see `apollo memory show`)",
    MemoryNotFoundError: "no memory has that id",
    InvalidMemoryId: "that is not a memory id",
}


@dataclass(frozen=True)
class Terminal:
    stdin: TextIO
    stdout: TextIO
    stderr: TextIO
    clock: Callable[[], datetime]


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p_memory = sub.add_parser(
        "memory",
        help="add, correct, confirm, archive or forget memories; claim text is read from stdin",
    )
    memory_sub = p_memory.add_subparsers(dest="memory_command", required=True)

    p_add = memory_sub.add_parser(
        "add",
        help="add a memory; the subject and content are prompted for, or piped "
             "(first line the subject, the rest the content)",
    )
    p_add.add_argument("--scope", required=True, choices=[s.value for s in Scope])
    p_add.add_argument("--kind", required=True, choices=[k.value for k in Kind])
    p_add.add_argument("--pinned", action="store_true")

    p_correct = memory_sub.add_parser(
        "correct", help="replace a memory's content; the new content is prompted for, or piped"
    )
    p_correct.add_argument("id")

    for name, helptext in (
        ("confirm", "record that a memory still holds"),
        ("contradict", "record evidence against a memory (it stays; support becomes contested)"),
        ("archive", "set a memory aside"),
        ("restore", "bring an archived memory back"),
        ("show", "print one memory"),
    ):
        memory_sub.add_parser(name, help=helptext).add_argument("id")

    p_forget = memory_sub.add_parser(
        "forget", help="tombstone a memory and every version of it; cannot be undone"
    )
    p_forget.add_argument("id")
    p_forget.add_argument("--yes", action="store_true",
                          help="confirm without a prompt (required when stdin is not a terminal)")

    p_list = memory_sub.add_parser("list", help="list memories (default: active)")
    p_list.add_argument("--status", action="append",
                        choices=[s.value for s in Status] + ["all"],
                        help="repeatable; `all` lists every status")


def run(args: argparse.Namespace, db: Database, term: Terminal) -> int:
    command = args.memory_command
    try:
        return _COMMANDS[command](args, db, term)
    except Exception as exc:  # the kind only: a message may quote a refused row
        kind = type(exc).__name__
        hint = _HINTS.get(type(exc))
        print(f"memory {command}: {kind}" + (f" — {hint}" if hint else ""), file=term.stderr)
        return 1


# -- input --------------------------------------------------------------------


def _memory_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise InvalidMemoryId() from None


def _prompted(term: Terminal, prompt: str) -> str:
    term.stderr.write(prompt)
    term.stderr.flush()
    return term.stdin.readline().rstrip("\r\n")


def _read_claim(term: Terminal) -> tuple[str, str]:
    if term.stdin.isatty():
        return _prompted(term, "subject: "), _prompted(term, "content: ")
    subject = term.stdin.readline().rstrip("\r\n")
    return subject, term.stdin.read().rstrip("\r\n")


def _read_content(term: Terminal) -> str:
    if term.stdin.isatty():
        return _prompted(term, "new content: ")
    return term.stdin.read().rstrip("\r\n")


# -- commands -----------------------------------------------------------------

_Command = Callable[[argparse.Namespace, Database, Terminal], int]


def _add(args: argparse.Namespace, db: Database, term: Terminal) -> int:
    subject, content = _read_claim(term)
    memory_id = create_memory(
        db, scope=args.scope, kind=args.kind, subject=subject, content=content,
        pinned=args.pinned, now=term.clock(),
    )
    print(memory_id, file=term.stdout)
    return 0


def _correct(args: argparse.Namespace, db: Database, term: Terminal) -> int:
    memory_id = _memory_id(args.id)
    content = _read_content(term)
    print(correct_memory(db, memory_id, content=content, now=term.clock()), file=term.stdout)
    return 0


def _simple(operation: Callable[..., object], done: str) -> _Command:
    def command(args: argparse.Namespace, db: Database, term: Terminal) -> int:
        memory_id = _memory_id(args.id)
        operation(db, memory_id, now=term.clock())
        print(f"{done} {memory_id}", file=term.stdout)
        return 0

    return command


def _forget(args: argparse.Namespace, db: Database, term: Terminal) -> int:
    memory_id = _memory_id(args.id)
    if not args.yes:
        if not term.stdin.isatty():
            print("memory forget: not confirmed; pass --yes when stdin is not a terminal",
                  file=term.stderr)
            return 2
        answer = _prompted(
            term, "forget this memory and every version of it? type 'forget' to confirm: "
        )
        if answer.strip() != "forget":
            print("memory forget: not confirmed; nothing changed", file=term.stderr)
            return 2
    removed = tombstone_memory(db, memory_id, now=term.clock())
    print(f"forgotten: {len(removed)} version(s)", file=term.stdout)
    for removed_id in removed:
        print(f"  {removed_id}", file=term.stdout)
    return 0


def _show(args: argparse.Namespace, db: Database, term: Terminal) -> int:
    for line in _describe(get_memory(db, _memory_id(args.id))):
        print(line, file=term.stdout)
    return 0


def _list(args: argparse.Namespace, db: Database, term: Terminal) -> int:
    wanted = args.status or [Status.ACTIVE.value]
    statuses = tuple(Status) if "all" in wanted else tuple(Status(s) for s in wanted)
    memories = list_memories(db, statuses=statuses)
    for memory in memories:
        pinned = " pinned" if memory.pinned else ""
        print(
            f"{memory.id}  {memory.status:<10} {memory.scope}/{memory.kind}{pinned}"
            f"  {memory.support} {memory.confidence:.2f}  {_text(memory.subject)}",
            file=term.stdout,
        )
    if not memories:
        print("no memories", file=term.stderr)
    return 0


def _text(value: str | None) -> str:
    return "(forgotten)" if value is None else value


def _describe(memory: Memory) -> list[str]:
    lines = [
        f"id:          {memory.id}",
        f"status:      {memory.status}",
        f"scope/kind:  {memory.scope}/{memory.kind}",
        f"origin:      {memory.origin} ({memory.origin_tier})",
        f"pinned:      {'yes' if memory.pinned else 'no'}",
        f"support:     {memory.support} (confidence {memory.confidence:.2f}; "
        f"{memory.counts.confirms} confirming, {memory.counts.contradicts} contradicting)",
        f"created:     {memory.created_at.isoformat()}",
    ]
    if memory.last_confirmed_at is not None:
        lines.append(f"confirmed:   {memory.last_confirmed_at.isoformat()}")
    if memory.superseded_by_id is not None:
        lines.append(f"replaced by: {memory.superseded_by_id}")
    lines += [f"subject:     {_text(memory.subject)}", f"content:     {_text(memory.content)}"]
    return lines


_COMMANDS: dict[str, _Command] = {
    "add": _add,
    "correct": _correct,
    "confirm": _simple(confirm_memory, "confirmed"),
    "contradict": _simple(contradict_memory, "contradicted"),
    "archive": _simple(archive_memory, "archived"),
    "restore": _simple(restore_memory, "restored"),
    "forget": _forget,
    "show": _show,
    "list": _list,
}
