"""Tombstone: Apollo forgetting a claim (spec D.7, D.9/1; O.1/18 and O.1/19, database side).

Through `apollo.core.memories.tombstone_memory`, as the runtime role: the whole
chain loses its text, excerpts and the verification hashes of every invocation
that included any of its rows, in one transaction with one audit event per
row, and the redacted hash survives in no audit payload and no log line.
"""

from __future__ import annotations

import io
import json
import logging
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from apollo.core.conversations import create_conversation
from apollo.core.memories import (
    archive_memory,
    correct_memory,
    create_memory,
    get_memory,
    tombstone_memory,
)
from apollo.logging_setup import configure_logging
from apollo.memory.lifecycle import LifecycleError, MemoryNotFoundError
from apollo.memory.models import Status, included_manifest_entry
from apollo.storage.db import Database
from apollo.storage.repositories import MemoryRepository

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
SECRET = "zqxsecret7731"  # survives text-search stemming


def _later(minutes: int) -> datetime:
    return NOW + timedelta(minutes=minutes)


def _rows(db: Database, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return list(cur.fetchall())


def _new(db: Database, content: str = f"the door code is {SECRET}") -> uuid.UUID:
    return create_memory(
        db, scope="user", kind="fact", subject=f"door {SECRET}", content=content, now=NOW
    )


def _excerpt(db: Database, memory_id: uuid.UUID, text: str) -> None:
    """Evidence with an excerpt, as a proposal-approved observation will carry (step 12)."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO memory_observation (id, memory_id, relation, source_kind, excerpt,"
            " observed_at, created_at) VALUES (gen_random_uuid(), %s, 'confirms',"
            " 'user_direct_entry', %s, %s, %s)",
            (memory_id, text, NOW, NOW),
        )


def _invocation(db: Database, manifest: list[dict[str, Any]], bundle_hash: str) -> uuid.UUID:
    """A completed invocation whose manifest names memory rows (step 11 will make these)."""
    conversation_id, message_id, turn_id, invocation_id = (uuid.uuid4() for _ in range(4))
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversation (id, mode, started_at, last_active_at)"
            " VALUES (%s, 'personal', %s, %s)",
            (conversation_id, NOW, NOW),
        )
        cur.execute(
            "INSERT INTO message (id, conversation_id, seq, role, content, created_at)"
            " VALUES (%s, %s, 1, 'user', 'hello', %s)",
            (message_id, conversation_id, NOW),
        )
        cur.execute(
            "INSERT INTO turn (id, conversation_id, request_message_id, status,"
            " conversation_mode, identity_version, identity_hash, started_at)"
            " VALUES (%s, %s, %s, 'started', 'personal', 'v', 'h', %s)",
            (turn_id, conversation_id, message_id, NOW),
        )
        cur.execute(
            "INSERT INTO model_invocation (id, turn_id, seq, purpose, brain_alias, provider_key,"
            " adapter_key, render_version, compiler_version, token_estimator, context_manifest,"
            " context_bundle_hash, context_token_estimate, max_trust_tier, generation_params,"
            " status, started_at) VALUES (%s, %s, 1, 'reply', 'b', 'p', 'a', 'r', 'c', 'e',"
            " %s::jsonb, %s, 1, 'T3', '{}', 'started', %s)",
            (invocation_id, turn_id, json.dumps(manifest), bundle_hash, NOW),
        )
        cur.execute(
            "UPDATE model_invocation SET status = 'completed', rendered_prompt_hash = %s"
            " WHERE id = %s",
            (f"prompt-{bundle_hash}", invocation_id),
        )
    return invocation_id


def _hashes(db: Database, invocation_id: uuid.UUID) -> dict[str, Any]:
    return _rows(
        db,
        "SELECT context_bundle_hash, rendered_prompt_hash, hashes_redacted_reason"
        " FROM model_invocation WHERE id = %s",
        (invocation_id,),
    )[0]


def _tombstone_events(db: Database) -> dict[uuid.UUID, dict[str, Any]]:
    return {
        row["subject_id"]: row["payload"]
        for row in _rows(
            db,
            "SELECT subject_id, payload FROM audit_event WHERE event_type = 'memory.tombstoned'",
        )
    }


def _assert_gone(db: Database, memory_id: uuid.UUID) -> None:
    [row] = _rows(
        db,
        "SELECT status, subject, content, tombstoned_at, search_vector::text AS lexemes,"
        " scope, kind, origin_tier, origin FROM memory WHERE id = %s",
        (memory_id,),
    )
    assert row["status"] == "tombstoned" and row["tombstoned_at"] is not None
    assert row["subject"] is None and row["content"] is None
    assert row["lexemes"] == ""  # the generated search vector empties with the text
    # Provenance outlives the claim it describes (spec C.7).
    assert (row["scope"], row["kind"], row["origin_tier"], row["origin"]) == (
        "user",
        "fact",
        "user_asserted",
        "personal",
    )
    assert not any(
        r["excerpt"]
        for r in _rows(
            db, "SELECT excerpt FROM memory_observation WHERE memory_id = %s", (memory_id,)
        )
    )


# ---------------------------------------------------------------------------
# a single row
# ---------------------------------------------------------------------------


def test_a_tombstone_removes_the_claim_and_its_excerpts(db: Database) -> None:
    memory_id = _new(db)
    _excerpt(db, memory_id, f"he said {SECRET}")
    assert tombstone_memory(db, memory_id, now=_later(1)) == [memory_id]
    _assert_gone(db, memory_id)
    assert _tombstone_events(db) == {
        memory_id: {
            "status": "tombstoned",
            "rows_tombstoned": 1,
            "observations_redacted": 1,
            "invocations_redacted": 0,
        }
    }


def test_an_archived_memory_can_be_tombstoned(db: Database) -> None:
    memory_id = _new(db)
    archive_memory(db, memory_id, now=_later(1))
    tombstone_memory(db, memory_id, now=_later(2))
    _assert_gone(db, memory_id)


def test_a_tombstone_is_terminal_and_an_unknown_id_is_not_found(db: Database) -> None:
    memory_id = _new(db)
    tombstone_memory(db, memory_id, now=_later(1))
    with pytest.raises(LifecycleError, match="memory that is tombstoned"):
        tombstone_memory(db, memory_id, now=_later(2))
    with pytest.raises(MemoryNotFoundError):
        tombstone_memory(db, uuid.uuid4(), now=_later(3))
    assert len(_tombstone_events(db)) == 1


# ---------------------------------------------------------------------------
# chains (D.9/1): a request naming any row forgets them all
# ---------------------------------------------------------------------------


def test_naming_the_corrected_away_row_forgets_the_whole_chain(db: Database) -> None:
    """The "4123" case: the user names the old row, the one they want gone."""
    old = _new(db, content=f"the door code is 4123 {SECRET}")
    new = correct_memory(db, old, content=f"the door code is 5555 {SECRET}", now=_later(1))
    assert tombstone_memory(db, old, now=_later(2)) == [new, old]  # head first
    _assert_gone(db, old)
    _assert_gone(db, new)
    events = _tombstone_events(db)
    assert set(events) == {old, new}
    assert all(e["rows_tombstoned"] == 2 for e in events.values())
    assert not _rows(db, "SELECT 1 FROM memory WHERE content LIKE %s", (f"%{SECRET}%",))


@pytest.mark.parametrize("named", [0, 1, 2])
def test_any_row_of_a_three_row_chain_resolves_to_its_head(db: Database, named: int) -> None:
    chain = [_new(db, content="version 0")]
    for minute in (1, 2):
        chain.append(correct_memory(db, chain[-1], content=f"version {minute}", now=_later(minute)))
    removed = tombstone_memory(db, chain[named], now=_later(3))
    assert removed == list(reversed(chain))
    for memory_id in chain:
        assert get_memory(db, memory_id).status is Status.TOMBSTONED


def test_an_archived_head_brings_its_predecessors(db: Database) -> None:
    old = _new(db)
    head = correct_memory(db, old, content="second", now=_later(1))
    archive_memory(db, head, now=_later(2))
    assert tombstone_memory(db, old, now=_later(3)) == [head, old]


def test_separate_chains_are_untouched(db: Database) -> None:
    forgotten, kept = _new(db), _new(db, content="another claim")
    tombstone_memory(db, forgotten, now=_later(1))
    assert get_memory(db, kept).status is Status.ACTIVE
    assert get_memory(db, kept).content == "another claim"


# ---------------------------------------------------------------------------
# concurrency: the chain is locked in one order and checked again under the locks
# ---------------------------------------------------------------------------


def _in_thread(target: Any, *args: Any) -> None:
    """Run in another thread: its own connection and unit of work, committed on return."""
    worker = threading.Thread(target=target, args=args)
    worker.start()
    worker.join(10)
    assert not worker.is_alive()


def test_concurrent_tombstones_naming_different_rows_do_not_deadlock(
    db: Database, monkeypatch
) -> None:
    """Codex finding on #11: naming the old row and the head used to lock in opposite orders."""
    old = _new(db)
    head = correct_memory(db, old, content="second", now=_later(1))
    barrier = threading.Barrier(2, timeout=10)
    lock_many = MemoryRepository.lock_many

    def lock_after_both_found_the_chain(
        self: MemoryRepository, memory_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, dict[str, Any]]:
        if not getattr(waited, "done", False):
            waited.done = True
            barrier.wait()  # both requests have found the chain; neither holds a lock
        return lock_many(self, memory_ids)

    waited = threading.local()
    monkeypatch.setattr(MemoryRepository, "lock_many", lock_after_both_found_the_chain)
    outcome: dict[str, Any] = {}

    def run(label: str, memory_id: uuid.UUID) -> None:
        try:
            outcome[label] = tombstone_memory(db, memory_id, now=_later(2))
        except Exception as exc:  # recorded and asserted below
            outcome[label] = exc

    threads = [
        threading.Thread(target=run, args=("naming old", old)),
        threading.Thread(target=run, args=("naming head", head)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    monkeypatch.undo()

    results = list(outcome.values())
    assert [head, old] in results  # one request tombstoned the whole chain
    [refused] = [r for r in results if isinstance(r, Exception)]
    assert isinstance(refused, LifecycleError)  # not DeadlockDetected
    assert "tombstoned" in str(refused)
    events = _tombstone_events(db)
    assert set(events) == {old, head}  # one event per row, from the winner only
    assert len(_rows(db, "SELECT 1 FROM audit_event WHERE event_type = 'memory.tombstoned'")) == 2


def test_a_correction_between_finding_and_locking_the_chain_is_tombstoned_too(
    db: Database, monkeypatch
) -> None:
    old = _new(db)
    head = correct_memory(db, old, content="second", now=_later(1))
    lock_many = MemoryRepository.lock_many
    added: list[uuid.UUID] = []

    def correct_first(
        self: MemoryRepository, memory_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, dict[str, Any]]:
        if not added:
            _in_thread(
                lambda: added.append(correct_memory(db, head, content="third", now=_later(2)))
            )
        return lock_many(self, memory_ids)

    monkeypatch.setattr(MemoryRepository, "lock_many", correct_first)
    removed = tombstone_memory(db, old, now=_later(3))
    monkeypatch.undo()
    assert removed == [added[0], head, old]
    for memory_id in removed:
        _assert_gone(db, memory_id)


def test_a_chain_that_keeps_changing_is_refused_and_nothing_changes(
    db: Database, monkeypatch
) -> None:
    old = _new(db)
    chain = [old, correct_memory(db, old, content="second", now=_later(1))]
    lock_many = MemoryRepository.lock_many

    def correct_every_time(
        self: MemoryRepository, memory_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, dict[str, Any]]:
        _in_thread(
            lambda: chain.append(
                correct_memory(db, chain[-1], content="again", now=_later(len(chain)))
            )
        )
        return lock_many(self, memory_ids)

    monkeypatch.setattr(MemoryRepository, "lock_many", correct_every_time)
    with pytest.raises(LifecycleError, match="kept changing"):
        tombstone_memory(db, old, now=_later(10))
    monkeypatch.undo()
    assert len(chain) == 5  # three attempts, each outrun by a correction
    assert get_memory(db, chain[-1]).status is Status.ACTIVE
    assert get_memory(db, old).content == f"the door code is {SECRET}"
    assert _tombstone_events(db) == {}


# ---------------------------------------------------------------------------
# verification-hash redaction (O.1/19, database side)
# ---------------------------------------------------------------------------


def test_every_invocation_that_included_any_chain_row_is_redacted(db: Database) -> None:
    old = _new(db)
    head = correct_memory(db, old, content="second", now=_later(1))
    unrelated = _new(db, content="unrelated")
    included_old = _invocation(db, included_manifest_entry(old), "sha256:included-old")
    included_head = _invocation(db, included_manifest_entry(head), "sha256:included-head")
    dropped = _invocation(
        db,
        [{"source_kind": "memory", "source_ref": str(head), "included": False}],
        "sha256:dropped",
    )
    other = _invocation(db, included_manifest_entry(unrelated), "sha256:other")

    tombstone_memory(db, head, now=_later(2))

    for invocation in (included_old, included_head):
        assert _hashes(db, invocation) == {
            "context_bundle_hash": None,
            "rendered_prompt_hash": None,
            "hashes_redacted_reason": "source_tombstoned",
        }
    # A dropped block never entered the bundle, so its hash is no oracle (D.7).
    assert _hashes(db, dropped)["context_bundle_hash"] == "sha256:dropped"
    assert _hashes(db, other)["context_bundle_hash"] == "sha256:other"
    events = _tombstone_events(db)
    assert events[head]["invocations_redacted"] == 1
    assert events[old]["invocations_redacted"] == 1


def test_a_redacted_hash_appears_in_no_audit_payload_and_no_log_line(db: Database, service) -> None:
    """The oracle D.7 removes must not survive in the append-only audit stream or the logs."""
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    try:
        # A real turn: its bundle hash is what invocation.started and
        # context.compiled used to copy.
        conversation_id = create_conversation(db, now=NOW)
        service.submit(conversation_id=conversation_id, text="hello there")
        [real] = _rows(db, "SELECT context_bundle_hash FROM model_invocation")
        real_hash = real["context_bundle_hash"]
        assert real_hash

        memory_id = _new(db)
        invocation = _invocation(db, included_manifest_entry(memory_id), "sha256:about-to-go")
        tombstone_memory(db, memory_id, now=_later(1))
    finally:
        logging.getLogger().handlers.clear()

    assert _hashes(db, invocation)["context_bundle_hash"] is None
    payloads = " ".join(
        json.dumps(row["payload"]) for row in _rows(db, "SELECT payload FROM audit_event")
    )
    logs = stream.getvalue()
    assert (
        "invocation.started"
        in _rows(db, "SELECT string_agg(event_type, ' ') AS t FROM audit_event")[0]["t"]
    )
    assert "context.compiled" in logs
    for secret_hash in (real_hash, "sha256:about-to-go", "prompt-sha256:about-to-go"):
        assert secret_hash not in payloads
        assert secret_hash not in logs
    assert "bundle_hash" not in payloads and '"bundle_hash"' not in logs
    assert SECRET not in payloads and SECRET not in logs


def test_a_failed_tombstone_changes_nothing(db: Database, monkeypatch) -> None:
    from apollo.storage.unit_of_work import UnitOfWork

    old = _new(db)
    head = correct_memory(db, old, content="second", now=_later(1))
    invocation = _invocation(db, included_manifest_entry(old), "sha256:kept")

    def exploding_flush(self: UnitOfWork) -> None:
        raise RuntimeError("forced failure during audit flush")

    monkeypatch.setattr(UnitOfWork, "_flush_audit", exploding_flush)
    with pytest.raises(RuntimeError, match="forced failure"):
        tombstone_memory(db, old, now=_later(2))
    monkeypatch.undo()
    assert get_memory(db, head).status is Status.ACTIVE
    assert get_memory(db, old).status is Status.SUPERSEDED
    assert _hashes(db, invocation)["context_bundle_hash"] == "sha256:kept"
    assert _tombstone_events(db) == {}
