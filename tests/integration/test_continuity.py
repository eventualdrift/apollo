"""Continuity across a full restart (acceptance criterion 1)."""

from __future__ import annotations

import pytest

from apollo.core.conversations import create_conversation, transcript
from apollo.storage.db import Database

pytestmark = pytest.mark.integration


def test_conversation_survives_a_process_and_connection_restart(
    db, service_factory, clock, fresh_database
) -> None:
    conv = create_conversation(db, now=clock())
    first = service_factory().submit(conversation_id=conv, text="You there?")
    second = service_factory().submit(conversation_id=conv, text="Still here?")
    assert first.status == second.status == "completed"

    # Simulate a restart: new Database object, new connections, new services,
    # a fresh identity loader and a fresh brain registry.
    from apollo.brains.registry import BrainRegistry
    from apollo.core.identity import IdentityLoader
    from apollo.core.turns import TurnService
    from tests.integration.conftest import make_config

    restarted_db = Database(fresh_database["owner_dsn"])
    config = make_config(fresh_database["owner_dsn"])
    restarted = TurnService(
        restarted_db, config, BrainRegistry(config), IdentityLoader(config.identity_dir),
        clock=clock,
    )
    assert restarted.recover_orphans() == {"turns": 0, "invocations": 0}

    third = restarted.submit(conversation_id=conv, text="And now?")
    assert third.status == "completed"

    with restarted_db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT context_manifest FROM model_invocation WHERE turn_id = %s", (third.turn_id,)
        )
        manifest = cur.fetchone()["context_manifest"]
        cur.execute("SELECT id, seq, role FROM message WHERE conversation_id = %s ORDER BY seq",
                    (conv,))
        messages = cur.fetchall()

    assert [m["seq"] for m in messages] == [1, 2, 3, 4, 5, 6]
    assert [m["role"] for m in messages] == ["user", "apollo"] * 3

    # The four messages that preceded the third turn are in its compiled context.
    history_refs = [e["source_ref"] for e in manifest if e["block_type"] == "CONVERSATION_RECENT"]
    assert history_refs == [str(m["id"]) for m in messages[:4]]


def test_one_turn_per_submitted_message(db, service, clock) -> None:
    conv = create_conversation(db, now=clock())
    for text in ("one", "two", "three"):
        service.submit(conversation_id=conv, text=text)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM turn WHERE conversation_id = %s", (conv,))
        turns = cur.fetchone()["n"]
        cur.execute(
            "SELECT count(*) AS n FROM model_invocation mi JOIN turn t ON t.id = mi.turn_id"
            " WHERE t.conversation_id = %s", (conv,)
        )
        invocations = cur.fetchone()["n"]
    assert turns == 3
    assert invocations == 3  # exactly one reply invocation per ordinary turn
    assert len([m for m in transcript(db, conv) if m["role"] == "user"]) == 3
