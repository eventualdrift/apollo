"""A thin terminal client. Captures input and renders output; owns nothing."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import UTC, datetime

from apollo.brains.registry import BrainRegistry
from apollo.config import load_config
from apollo.core.conversations import create_conversation, get_conversation, transcript
from apollo.core.identity import IdentityLoader
from apollo.core.turns import TurnService
from apollo.logging_setup import configure_logging
from apollo.storage.db import (
    Database,
    RoleProvisioningError,
    describe_role_privileges,
    provision_runtime_role,
)


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

    sub.add_parser("migrate", help="apply pending migrations (needs APOLLO_ADMIN_DSN)")
    p_prov = sub.add_parser(
        "provision",
        help="migrate, then create and grant the least-privilege runtime role "
             "(needs APOLLO_ADMIN_DSN)",
    )
    p_prov.add_argument("--password", default=None,
                        help="runtime role password; omit for trust/peer auth")
    sub.add_parser("doctor", help="report the runtime role's actual privileges")

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

    if args.command in {"migrate", "provision"}:
        if not config.admin_dsn:
            print(
                "APOLLO_ADMIN_DSN is not set. Migrations and provisioning run as the"
                " database owner; the runtime DSN is the unprivileged application role.",
                file=sys.stderr,
            )
            return 2
        admin = Database(config.admin_dsn)
        applied = admin.migrate()
        print("\n".join(applied) if applied else "schema already up to date")
        if args.command == "provision":
            password = args.password or os.environ.get("APOLLO_RUNTIME_PASSWORD")
            try:
                with admin.connect() as conn:
                    created = provision_runtime_role(conn, config.runtime_role, password=password)
                    facts = describe_role_privileges(conn, config.runtime_role)
            except RoleProvisioningError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            print(f"runtime role {config.runtime_role}: {'created' if created else 'updated'}")
            print(f"  superuser:              {facts.is_superuser}")
            print(f"  owns audit_event:       {facts.owns_audit_event}")
            print(f"  audit_event privileges: {', '.join(facts.audit_event_privileges)}")
            if not facts.is_least_privilege:
                print("  WARNING: runtime role is not least-privilege", file=sys.stderr)
                return 1
        return 0

    if args.command == "doctor":
        target = Database(config.admin_dsn or config.database_dsn)
        with target.connect() as conn:
            facts = describe_role_privileges(conn, config.runtime_role)
        for line in facts.as_lines():
            print(line)
        print("least privilege:", "OK" if facts.is_least_privilege else "VIOLATED")
        return 0 if facts.is_least_privilege else 1

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
