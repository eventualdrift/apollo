"""Turn orchestration: the M1 objective, end to end against brain.fake."""

from __future__ import annotations

import uuid

import pytest

from apollo.brains.fake import FakeBrain
from apollo.core.conversations import create_conversation, transcript
from apollo.errors import BrainTransportError
from apollo.storage.db import Database

pytestmark = pytest.mark.integration


def rows(db: Database, sql: str, params=()) -> list[dict]:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def new_conversation(db, clock) -> uuid.UUID:
    return create_conversation(db, now=clock())


def test_ordinary_turn_records_one_reply_invocation(db, service, clock) -> None:
    conv = new_conversation(db, clock)
    result = service.submit(conversation_id=conv, text="You there?")

    assert result.status == "completed"
    assert result.response.startswith("[fake:echo")

    turns = rows(db, "SELECT * FROM turn WHERE conversation_id = %s", (conv,))
    assert len(turns) == 1
    assert turns[0]["status"] == "completed"
    assert turns[0]["identity_version"] == "2026.09.20-1"
    assert turns[0]["identity_hash"]

    invocations = rows(db, "SELECT * FROM model_invocation WHERE turn_id = %s", (result.turn_id,))
    assert len(invocations) == 1
    inv = invocations[0]
    assert (inv["purpose"], inv["status"], inv["seq"]) == ("reply", "completed", 1)
    assert inv["adapter_key"] == "fake"
    assert inv["render_version"] == "chat-v1"
    assert inv["compiler_version"] == "compiler-v1"
    assert inv["token_estimator"] == "conservative-v1"
    assert inv["context_bundle_hash"] and inv["rendered_prompt_hash"]
    assert inv["context_manifest"]
    assert inv["retry_of_invocation_id"] is None
    assert inv["taint"] == 0


def test_identity_is_on_the_turn_and_not_on_the_invocation(db, service, clock) -> None:
    conv = new_conversation(db, clock)
    service.submit(conversation_id=conv, text="hello")
    columns = {
        r["column_name"]
        for r in rows(
            db,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'model_invocation'",
        )
    }
    assert "identity_hash" not in columns and "identity_version" not in columns


def test_messages_are_ordered_and_linked(db, service, clock) -> None:
    conv = new_conversation(db, clock)
    service.submit(conversation_id=conv, text="first")
    service.submit(conversation_id=conv, text="second")
    msgs = rows(db, "SELECT seq, role, content, turn_id FROM message "
                    "WHERE conversation_id = %s ORDER BY seq", (conv,))
    assert [m["seq"] for m in msgs] == [1, 2, 3, 4]
    assert [m["role"] for m in msgs] == ["user", "apollo", "user", "apollo"]
    assert msgs[0]["turn_id"] is None          # request messages do not backlink
    assert msgs[1]["turn_id"] is not None      # apollo messages do


def test_prior_history_reaches_the_next_bundle(db, service, clock) -> None:
    conv = new_conversation(db, clock)
    first = service.submit(conversation_id=conv, text="remember the number nine")
    second = service.submit(conversation_id=conv, text="and now?")

    manifest = rows(
        db, "SELECT context_manifest FROM model_invocation WHERE turn_id = %s", (second.turn_id,)
    )[0]["context_manifest"]
    history_refs = [e["source_ref"] for e in manifest if e["block_type"] == "CONVERSATION_RECENT"]
    first_msgs = rows(db, "SELECT id FROM message WHERE conversation_id = %s ORDER BY seq LIMIT 2",
                      (conv,))
    assert [str(m["id"]) for m in first_msgs] == history_refs
    assert first.turn_id != second.turn_id


def test_system_notes_never_enter_the_next_bundle(db, service_factory, clock) -> None:
    """Spec F.2 / acceptance criterion 6."""
    failing = FakeBrain(fail_times=99, failure=BrainTransportError(http_status=503))
    conv = create_conversation(db, now=clock())
    service_factory(brain=failing).submit(conversation_id=conv, text="are you there?")

    notes = rows(db, "SELECT * FROM message WHERE conversation_id = %s AND role = 'system_note'",
                 (conv,))
    assert len(notes) == 1  # durable and human-visible

    ok = service_factory(brain=FakeBrain())
    result = ok.submit(conversation_id=conv, text="back?")
    manifest = rows(
        db, "SELECT context_manifest FROM model_invocation WHERE turn_id = %s", (result.turn_id,)
    )[0]["context_manifest"]
    refs = {e["source_ref"] for e in manifest}
    assert str(notes[0]["id"]) not in refs


def test_a_transport_failure_retries_as_a_second_invocation(db, service_factory, clock) -> None:
    """Acceptance criterion 13: two provider attempts, two rows, linked."""
    brain = FakeBrain(fail_times=1, failure=BrainTransportError(http_status=503))
    conv = create_conversation(db, now=clock())
    result = service_factory(brain=brain).submit(conversation_id=conv, text="hello")

    assert result.status == "completed"
    assert len(brain.calls) == 2  # two actual provider attempts

    invocations = rows(
        db, "SELECT * FROM model_invocation WHERE turn_id = %s ORDER BY seq", (result.turn_id,)
    )
    assert len(invocations) == 2
    first, second = invocations
    assert (first["status"], second["status"]) == ("failed", "completed")
    assert first["error_kind"] == "brain_unavailable"
    assert second["retry_of_invocation_id"] == first["id"]
    assert second["seq"] == first["seq"] + 1
    # The failed attempt keeps its own bundle.
    assert first["context_bundle_hash"] == second["context_bundle_hash"]
    assert first["context_manifest"]


def test_retry_is_spent_only_once(db, service_factory, clock) -> None:
    brain = FakeBrain(fail_times=99, failure=BrainTransportError(http_status=503))
    conv = create_conversation(db, now=clock())
    result = service_factory(brain=brain).submit(conversation_id=conv, text="hello")

    assert result.status == "failed"
    assert result.error_kind == "brain_unavailable"
    assert len(brain.calls) == 2  # one attempt plus one retry, never more
    assert len(rows(db, "SELECT 1 FROM model_invocation WHERE turn_id = %s",
                    (result.turn_id,))) == 2
    notes = [m for m in transcript(db, conv) if m["role"] == "system_note"]
    assert notes and "could not reach its model" in notes[0]["content"]


def test_a_content_failure_is_never_retried(db, service_factory, clock) -> None:
    from apollo.brains.fake import FakeBrainError

    brain = FakeBrain(fail_times=99, failure=FakeBrainError("content problem"))
    conv = create_conversation(db, now=clock())
    result = service_factory(brain=brain).submit(conversation_id=conv, text="hello")

    assert result.status == "failed"
    assert len(brain.calls) == 1  # no retry for a content-level failure
    assert len(rows(db, "SELECT 1 FROM model_invocation WHERE turn_id = %s",
                    (result.turn_id,))) == 1


def test_duplicate_submission_generates_nothing_new(db, service_factory, clock) -> None:
    brain = FakeBrain()
    svc = service_factory(brain=brain)
    conv = create_conversation(db, now=clock())

    first = svc.submit(conversation_id=conv, text="only once", idempotency_key="k-1")
    second = svc.submit(conversation_id=conv, text="only once", idempotency_key="k-1")

    assert second.replayed and not first.replayed
    assert second.turn_id == first.turn_id
    assert second.response == first.response
    assert len(brain.calls) == 1
    assert len(rows(db, "SELECT 1 FROM turn WHERE conversation_id = %s", (conv,))) == 1
    assert len(rows(db, "SELECT 1 FROM message WHERE conversation_id = %s", (conv,))) == 2


def test_audit_trail_of_a_successful_turn(db, service, clock) -> None:
    conv = new_conversation(db, clock)
    result = service.submit(conversation_id=conv, text="hello")
    turn_events = [
        r["event_type"]
        for r in rows(db, "SELECT event_type FROM audit_event WHERE turn_id = %s ORDER BY id",
                      (result.turn_id,))
    ]
    assert turn_events == [
        "message.created",
        "turn.started",
        "invocation.started",
        "invocation.completed",
        "message.created",
        "turn.completed",
    ]
    # identity.version_loaded is a global event: it precedes the turn and is
    # deliberately not scoped to one.
    identity_events = rows(
        db, "SELECT turn_id FROM audit_event WHERE event_type = 'identity.version_loaded'"
    )
    assert len(identity_events) == 1 and identity_events[0]["turn_id"] is None


def test_audit_payloads_carry_no_content(db, service, clock) -> None:
    conv = new_conversation(db, clock)
    service.submit(conversation_id=conv, text="my door code is 4123")
    payloads = repr(rows(db, "SELECT payload FROM audit_event"))
    assert "4123" not in payloads


def test_identity_is_snapshotted_once(db, service, clock) -> None:
    conv = new_conversation(db, clock)
    service.submit(conversation_id=conv, text="one")
    service.submit(conversation_id=conv, text="two")
    snapshots = rows(db, "SELECT * FROM identity_version")
    assert len(snapshots) == 1
    assert snapshots[0]["version_label"] == "2026.09.20-1"
    assert "**B25" in snapshots[0]["content"]
    loaded = rows(db, "SELECT 1 FROM audit_event WHERE event_type = 'identity.version_loaded'")
    assert len(loaded) == 1


def test_orphan_recovery_marks_interrupted(db, service, clock) -> None:
    """A crash mid-turn leaves a `started` row; restart marks it interrupted."""
    from datetime import timedelta

    conv = new_conversation(db, clock)
    stale_at = clock.now - timedelta(hours=2)  # the injected clock, not the database's
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO message (id, conversation_id, seq, role, content, created_at)"
            " VALUES (gen_random_uuid(), %s, 99, 'user', 'orphaned', %s) RETURNING id",
            (conv, stale_at),
        )
        msg = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO turn (id, conversation_id, request_message_id, status, conversation_mode,"
            " identity_version, identity_hash, started_at)"
            " VALUES (gen_random_uuid(), %s, %s, 'started', 'personal', 'v', 'h', %s)",
            (conv, msg, stale_at),
        )
    assert service.recover_orphans()["turns"] == 1
    stale = rows(db, "SELECT status, error_kind FROM turn WHERE error_kind = 'interrupted'")
    assert stale and stale[0]["status"] == "failed"
