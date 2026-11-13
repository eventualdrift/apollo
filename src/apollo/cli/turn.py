"""`apollo turn`: list a conversation's turns, and replay one (spec H.4).

`apollo turn replay <turn-id>` rebuilds what each model call of the turn was
sent, from the manifest it recorded and the rows that manifest names, and
checks it against the recorded hashes. It reports two statuses per call, and
when a source is gone it says which and why. It never reconstructs a
forgotten memory from anything else that happens to say the same thing.

`--context` prints the rebuilt request. That is conversation and memory text,
shown on request, to the terminal only: it is not logged and not stored.

An error prints as its kind, never its text.
"""

from __future__ import annotations

import argparse
import uuid

from apollo.cli.memory import Terminal
from apollo.core.conversations import get_conversation, turns
from apollo.core.replay import (
    BundleStatus,
    RenderStatus,
    Replay,
    ReplayNotFoundError,
    replay_turn,
)
from apollo.storage.db import Database


class InvalidId(ValueError):
    """An argument that is not an id."""


class ConversationNotFound(LookupError):
    """No conversation has that id."""


_HINTS: dict[type[Exception], str] = {
    InvalidId: "that is not an id",
    ConversationNotFound: "no conversation has that id",
    ReplayNotFoundError: "no turn has that id (see `apollo turn list <conversation>`)",
}

#: What each status means, in the words of the specification.
_BUNDLE_MEANING = {
    BundleStatus.VERIFIED: "every source is present and the rebuilt context matches",
    BundleStatus.MISMATCH: "every source is present, but the rebuilt context differs: "
                           "something that should never change has changed",
    BundleStatus.SOURCE_REDACTED: "a source was deliberately forgotten; nothing is rebuilt",
    BundleStatus.SOURCE_MISSING: "a source is absent, and not because it was forgotten",
}
_RENDER_MEANING = {
    RenderStatus.VERIFIED: "the rebuilt request matches what was sent",
    RenderStatus.MISMATCH: "the rebuilt request differs from what was sent",
    RenderStatus.RENDERER_UNAVAILABLE: "this version of Apollo no longer has that renderer",
    RenderStatus.NOT_ATTEMPTED: "not checked",
}


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p_turn = sub.add_parser(
        "turn", help="list a conversation's turns, or replay what a turn's model calls were sent"
    )
    turn_sub = p_turn.add_subparsers(dest="turn_command", required=True)
    turn_sub.add_parser(
        "list", help="a conversation's turns, with the ids `replay` takes"
    ).add_argument("conversation")
    p_replay = turn_sub.add_parser(
        "replay",
        help="rebuild each model call of a turn from its record and verify it; exits 1 if "
             "anything does not reproduce and was not deliberately forgotten",
    )
    p_replay.add_argument("turn")
    p_replay.add_argument("--context", action="store_true",
                          help="also print each rebuilt request (conversation and memory text)")


def run(args: argparse.Namespace, db: Database, term: Terminal) -> int:
    command = args.turn_command
    try:
        return {"list": _list, "replay": _replay}[command](args, db, term)
    except Exception as exc:  # the kind only: a message may quote a row
        kind = type(exc).__name__
        hint = _HINTS.get(type(exc))
        print(f"turn {command}: {kind}" + (f" — {hint}" if hint else ""), file=term.stderr)
        return 1


def _id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise InvalidId() from None


def _list(args: argparse.Namespace, db: Database, term: Terminal) -> int:
    conversation_id = _id(args.conversation)
    if get_conversation(db, conversation_id) is None:
        raise ConversationNotFound()
    found = turns(db, conversation_id)
    for turn in found:
        failure = f"  {turn['error_kind']}" if turn["error_kind"] else ""
        print(
            f"{turn['id']}  [{turn['request_seq']:>3}] {turn['status']:<9} "
            f"{turn['invocations']} model call(s){failure}",
            file=term.stdout,
        )
    if not found:
        print("no turns", file=term.stderr)
    return 0


def _replay(args: argparse.Namespace, db: Database, term: Terminal) -> int:
    replays = replay_turn(db, _id(args.turn))
    if not replays:
        print("that turn made no model call, so there is nothing to replay", file=term.stderr)
        return 0
    for replay in replays:
        for line in describe(replay):
            print(line, file=term.stdout)
        if args.context:
            _print_context(replay, term)
    return 0 if all(reproduced(replay) for replay in replays) else 1


def reproduced(replay: Replay) -> bool:
    """True unless something failed to reproduce that the record does not explain.

    A forgotten source, an invocation that never completed and a retired
    renderer are honest gaps. A mismatch or an unexplained absence is not.
    """
    return (
        replay.bundle_status in {BundleStatus.VERIFIED, BundleStatus.SOURCE_REDACTED}
        and replay.render_status is not RenderStatus.MISMATCH
    )


def describe(replay: Replay) -> list[str]:
    """One invocation's replay, as lines. Ids, versions, statuses and reasons only."""
    lines = [
        f"{replay.seq}  {replay.purpose} ({replay.invocation_status})  "
        f"{replay.invocation_id}",
        f"   built by {replay.compiler_version}, rendered by "
        f"{replay.adapter_key}/{replay.render_version}",
        f"   bundle: {replay.bundle_status} — {_BUNDLE_MEANING[replay.bundle_status]}",
    ]
    for source in replay.unavailable:
        lines.append(f"     {source.source_kind}:{source.source_ref}  {source.reason}")
    if replay.hashes_redacted_reason:
        lines.append(f"     verification hashes redacted: {replay.hashes_redacted_reason}")
    render = f"   render: {replay.render_status} — {_RENDER_MEANING[replay.render_status]}"
    lines.append(render)
    if replay.detail:
        lines.append(f"     {replay.detail}")
    return lines


def _print_context(replay: Replay, term: Terminal) -> None:
    if replay.request is None:
        print("   (no context: it was not rebuilt)", file=term.stdout)
        return
    checked = (
        "as sent, verified" if replay.render_status is RenderStatus.VERIFIED
        else "as it would have been sent; nothing was recorded to verify it against"
    )
    print(f"   --- request ({checked}) ---", file=term.stdout)
    for message in replay.request.messages:
        print(f"[{message.role}]", file=term.stdout)
        print(message.content, file=term.stdout)
    print("   --- end of request ---", file=term.stdout)
