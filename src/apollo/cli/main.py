"""A thin terminal client. Captures input and renders output; owns nothing."""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import UTC, datetime

from apollo.brains.registry import BrainRegistry
from apollo.config import load_config
from apollo.core.conversations import create_conversation, get_conversation, transcript
from apollo.core.identity import IdentityLoader
from apollo.core.turns import TurnService
from apollo.logging_setup import configure_logging
from apollo.storage.db import Database


def _service(config) -> tuple[Database, TurnService]:  # type: ignore[no-untyped-def]
    db = Database(config.database_dsn)
    service = TurnService(
        db,
        config,
        BrainRegistry(config),
        IdentityLoader(config.identity_dir),
        clock=lambda: datetime.now(UTC),
    )
    return db, service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apollo")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply pending migrations")

    p_new = sub.add_parser("new", help="start a conversation")
    p_new.add_argument("--title", default=None)

    p_say = sub.add_parser("say", help="send one message and print the reply")
    p_say.add_argument("conversation")
    p_say.add_argument("text")
    p_say.add_argument("--idempotency-key", default=None)

    p_chat = sub.add_parser("chat", help="interactive loop")
    p_chat.add_argument("conversation")

    p_show = sub.add_parser("show", help="print a conversation transcript")
    p_show.add_argument("conversation")

    args = parser.parse_args(argv)
    config = load_config()
    configure_logging(config.log_level)

    if args.command == "migrate":
        applied = Database(config.database_dsn).migrate()
        print("\n".join(applied) if applied else "already up to date")
        return 0

    db, service = _service(config)

    if args.command == "new":
        conversation_id = create_conversation(db, now=datetime.now(UTC), title=args.title)
        print(conversation_id)
        return 0

    conversation_id = uuid.UUID(args.conversation)

    if args.command == "show":
        if get_conversation(db, conversation_id) is None:
            print(f"no such conversation: {conversation_id}", file=sys.stderr)
            return 1
        for row in transcript(db, conversation_id):
            marker = {"user": "janu", "apollo": "apollo", "system_note": "----"}[row["role"]]
            print(f"[{row['seq']:>3}] {marker}: {row['content']}")
        return 0

    service.recover_orphans()

    if args.command == "say":
        return _emit(service.submit(
            conversation_id=conversation_id,
            text=args.text,
            idempotency_key=args.idempotency_key,
        ))

    if args.command == "chat":
        print("apollo — ctrl-d to leave")
        while True:
            try:
                text = input("janu> ").strip()
            except EOFError:
                print()
                return 0
            if not text:
                continue
            _emit(service.submit(conversation_id=conversation_id, text=text))
    return 0


def _emit(result) -> int:  # type: ignore[no-untyped-def]
    if result.status == "completed":
        print(f"apollo> {result.response}")
        return 0
    print(f"apollo> [turn failed: {result.error_kind}]", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
