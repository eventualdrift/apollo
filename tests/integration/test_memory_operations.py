"""The direct-entry memory API and the lifecycle operations (step 10 PR 3).

Create, confirm, contradict, correct, archive and restore, through
`apollo.core.memories`, as the least-privilege runtime role: each change and its
audit event commit together or not at all, the lifecycle rules hold, and no
claim text reaches an audit payload, an error message or a log line.
"""

from __future__ import annotations

import io
import json
import logging
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from apollo.core.memories import (
    archive_memory,
    confirm_memory,
    contradict_memory,
    correct_memory,
    create_memory,
    get_memory,
    list_memories,
    restore_memory,
)
from apollo.logging_setup import configure_logging
from apollo.memory.lifecycle import LifecycleError, MemoryNotFoundError
from apollo.memory.models import MemoryInputError, Status, Support
from apollo.storage.db import Database
from apollo.storage.unit_of_work import UnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
SECRET = "zqxsecret7731"  # survives text-search stemming


def _later(minutes: int) -> datetime:
    return NOW + timedelta(minutes=minutes)


def _new(db: Database, **fields: Any) -> uuid.UUID:
    values: dict[str, Any] = {
        "scope": "user",
        "kind": "fact",
        "subject": "door code",
        "content": f"the door code is {SECRET}",
    }
    values.update(fields)
    return create_memory(db, now=NOW, **values)


def _rows(db: Database, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return list(cur.fetchall())


def _events(db: Database, memory_id: uuid.UUID) -> list[dict[str, Any]]:
    return _rows(
        db,
        "SELECT event_type, actor, subject_kind, payload FROM audit_event"
        " WHERE subject_id = %s ORDER BY id",
        (memory_id,),
    )


def _count(db: Database, table: str) -> int:
    return int(_rows(db, f"SELECT count(*) AS n FROM {table}")[0]["n"])


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_direct_entry_creates_a_user_asserted_personal_memory(db: Database) -> None:
    memory_id = _new(db, pinned=True)
    memory = get_memory(db, memory_id)
    assert memory.status is Status.ACTIVE
    assert (memory.origin_tier, memory.origin) == ("user_asserted", "personal")
    assert memory.pinned is True
    assert (memory.subject, memory.content) == ("door code", f"the door code is {SECRET}")

    observations = _rows(db, "SELECT * FROM memory_observation WHERE memory_id = %s", (memory_id,))
    assert len(observations) == 1
    assert observations[0]["relation"] == "asserts"
    assert observations[0]["source_kind"] == "user_direct_entry"
    assert observations[0]["message_id"] is None and observations[0]["excerpt"] is None

    [event] = _events(db, memory_id)
    assert (event["event_type"], event["actor"], event["subject_kind"]) == (
        "memory.created",
        "user",
        "memory",
    )
    assert event["payload"] == {
        "status": "active",
        "scope": "user",
        "kind": "fact",
        "origin_tier": "user_asserted",
        "origin": "personal",
        "pinned": True,
        "observation_id": str(observations[0]["id"]),
        "source_kind": "user_direct_entry",
    }


def test_direct_entry_is_the_route_to_self_scope(db: Database) -> None:
    assert get_memory(db, _new(db, scope="self")).scope == "self"


@pytest.mark.parametrize(
    "fields,message",
    [
        ({"subject": ""}, "subject must be non-empty"),
        ({"subject": "   "}, "subject must be non-empty"),
        ({"content": "\n\t"}, "content must be non-empty"),
        ({"content": f"{SECRET}\x00"}, "content cannot contain a NUL"),
        ({"scope": "everyone"}, "scope is one of"),
        ({"kind": "feeling"}, "kind is one of"),
    ],
)
def test_bad_input_is_refused_before_anything_is_written(
    db: Database, fields: dict[str, Any], message: str
) -> None:
    with pytest.raises(MemoryInputError, match=message) as exc:
        _new(db, **fields)
    assert SECRET not in str(exc.value)
    assert _count(db, "memory") == 0 and _count(db, "audit_event") == 0


# ---------------------------------------------------------------------------
# confirm and contradict
# ---------------------------------------------------------------------------


def test_confirm_adds_evidence_and_last_confirmed_at(db: Database) -> None:
    memory_id = _new(db)
    observation_id = confirm_memory(db, memory_id, now=_later(1))
    memory = get_memory(db, memory_id)
    assert memory.status is Status.ACTIVE
    assert memory.last_confirmed_at == _later(1)
    assert memory.support is Support.CONFIRMED and memory.confidence == 0.75
    event = _events(db, memory_id)[-1]
    assert event["event_type"] == "memory.confirmed"
    assert event["payload"] == {
        "status": "active",
        "observation_id": str(observation_id),
        "source_kind": "user_direct_entry",
    }


def test_contradict_is_evidence_not_a_retraction(db: Database) -> None:
    memory_id = _new(db)
    confirm_memory(db, memory_id, now=_later(1))
    contradict_memory(db, memory_id, now=_later(2))
    memory = get_memory(db, memory_id)
    assert memory.status is Status.ACTIVE  # spec D.6: no automatic retraction
    assert memory.support is Support.CONTESTED and memory.confidence == 0.50
    assert _events(db, memory_id)[-1]["event_type"] == "memory.contradicted"


def test_a_later_stamped_observation_that_commits_first_keeps_its_timestamps(
    db: Database,
) -> None:
    # Two confirmations stamped 12:01 and 12:02 can commit in either order; the
    # row keeps the latest stamp either way, and so does a contradiction.
    confirmed = _new(db)
    confirm_memory(db, confirmed, now=_later(2))
    confirm_memory(db, confirmed, now=_later(1))
    memory = get_memory(db, confirmed)
    assert memory.last_confirmed_at == _later(2)
    assert memory.updated_at == _later(2)
    assert memory.counts.confirms == 2

    contradicted = _new(db)
    contradict_memory(db, contradicted, now=_later(2))
    contradict_memory(db, contradicted, now=_later(1))
    confirm_memory(db, contradicted, now=_later(1))
    memory = get_memory(db, contradicted)
    assert memory.updated_at == _later(2)
    assert memory.last_confirmed_at == _later(1)  # the first confirmation still sets it


def test_evidence_can_be_added_to_an_archived_memory(db: Database) -> None:
    memory_id = _new(db)
    archive_memory(db, memory_id, now=_later(1))
    confirm_memory(db, memory_id, now=_later(2))
    contradict_memory(db, memory_id, now=_later(3))
    assert get_memory(db, memory_id).status is Status.ARCHIVED


def test_evidence_cannot_be_added_to_a_superseded_memory(db: Database) -> None:
    memory_id = _new(db)
    correct_memory(db, memory_id, content="the door code is 5555", now=_later(1))
    before = _count(db, "audit_event")
    for operation in (confirm_memory, contradict_memory):
        with pytest.raises(LifecycleError, match="superseded"):
            operation(db, memory_id, now=_later(2))
    assert _count(db, "audit_event") == before


# ---------------------------------------------------------------------------
# correct
# ---------------------------------------------------------------------------


def test_correct_supersedes_and_carries_the_claim_forward(db: Database) -> None:
    old_id = _new(db, scope="relationship", kind="preference", pinned=True)
    confirm_memory(db, old_id, now=_later(1))
    new_id = correct_memory(db, old_id, content="the door code is 5555", now=_later(2))

    old, new = get_memory(db, old_id), get_memory(db, new_id)
    assert old.status is Status.SUPERSEDED and old.superseded_by_id == new_id
    assert old.content == f"the door code is {SECRET}"  # kept: replay depends on it
    assert new.status is Status.ACTIVE and new.content == "the door code is 5555"
    for field in ("scope", "kind", "subject", "origin", "origin_tier", "pinned"):
        assert getattr(new, field) == getattr(old, field), field

    # Observations are not copied forward: the chain is the history (spec D.5).
    assert new.counts.confirms == 0 and new.support is Support.ASSERTED
    [assertion] = _rows(
        db, "SELECT relation, source_kind FROM memory_observation WHERE memory_id = %s", (new_id,)
    )
    assert (assertion["relation"], assertion["source_kind"]) == ("asserts", "user_direct_entry")

    event = _events(db, old_id)[-1]
    assert event["event_type"] == "memory.superseded"
    assert event["payload"]["replacement_id"] == str(new_id)
    assert event["payload"]["status"] == "superseded"


@pytest.mark.parametrize("state", ["superseded", "archived"])
def test_only_an_active_memory_can_be_corrected(db: Database, state: str) -> None:
    memory_id = _new(db)
    if state == "superseded":
        correct_memory(db, memory_id, content="second", now=_later(1))
    else:
        archive_memory(db, memory_id, now=_later(1))
    with pytest.raises(LifecycleError, match=f"memory that is {state}"):
        correct_memory(db, memory_id, content="third", now=_later(2))


def test_a_correction_needs_real_content(db: Database) -> None:
    memory_id = _new(db)
    with pytest.raises(MemoryInputError):
        correct_memory(db, memory_id, content="  ", now=_later(1))
    assert get_memory(db, memory_id).status is Status.ACTIVE


# ---------------------------------------------------------------------------
# archive and restore
# ---------------------------------------------------------------------------


def test_archive_and_restore(db: Database) -> None:
    memory_id = _new(db)
    archive_memory(db, memory_id, now=_later(1))
    archived = get_memory(db, memory_id)
    assert archived.status is Status.ARCHIVED and archived.archived_at == _later(1)
    assert archived.content == f"the door code is {SECRET}"  # content-preserving

    restore_memory(db, memory_id, now=_later(2))
    restored = get_memory(db, memory_id)
    assert restored.status is Status.ACTIVE and restored.archived_at is None
    assert [e["event_type"] for e in _events(db, memory_id)] == [
        "memory.created",
        "memory.archived",
        "memory.restored",
    ]


def test_restore_needs_an_archived_memory_and_archive_an_active_one(db: Database) -> None:
    memory_id = _new(db)
    with pytest.raises(LifecycleError, match="cannot restore"):
        restore_memory(db, memory_id, now=_later(1))
    archive_memory(db, memory_id, now=_later(2))
    with pytest.raises(LifecycleError, match="cannot archive"):
        archive_memory(db, memory_id, now=_later(3))


def test_an_unknown_id_is_not_found(db: Database) -> None:
    for operation in (confirm_memory, contradict_memory, archive_memory, restore_memory):
        with pytest.raises(MemoryNotFoundError):
            operation(db, uuid.uuid4(), now=NOW)
    with pytest.raises(MemoryNotFoundError):
        correct_memory(db, uuid.uuid4(), content="x", now=NOW)
    with pytest.raises(MemoryNotFoundError):
        get_memory(db, uuid.uuid4())


def test_list_defaults_to_active(db: Database) -> None:
    kept = _new(db)
    archived = _new(db, subject="other")
    archive_memory(db, archived, now=_later(1))
    assert [m.id for m in list_memories(db)] == [kept]
    assert {m.id for m in list_memories(db, statuses=(Status.ACTIVE, Status.ARCHIVED))} == {
        kept,
        archived,
    }


# ---------------------------------------------------------------------------
# atomicity (spec O.1/21) and what never leaks
# ---------------------------------------------------------------------------


def test_a_failure_between_the_memory_write_and_its_audit_event_leaves_neither(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    def exploding_flush(self: UnitOfWork) -> None:
        raise RuntimeError("forced failure during audit flush")

    monkeypatch.setattr(UnitOfWork, "_flush_audit", exploding_flush)
    with pytest.raises(RuntimeError, match="forced failure"):
        _new(db)
    assert _count(db, "memory") == 0
    assert _count(db, "memory_observation") == 0
    assert _count(db, "audit_event") == 0


def test_a_failed_correction_changes_neither_row(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_id = _new(db)

    def exploding_flush(self: UnitOfWork) -> None:
        raise RuntimeError("forced failure during audit flush")

    monkeypatch.setattr(UnitOfWork, "_flush_audit", exploding_flush)
    with pytest.raises(RuntimeError, match="forced failure"):
        correct_memory(db, memory_id, content="second", now=_later(1))
    monkeypatch.undo()
    assert _count(db, "memory") == 1
    assert get_memory(db, memory_id).status is Status.ACTIVE


def test_no_claim_text_reaches_an_audit_payload_or_a_log_line(db: Database) -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    try:
        memory_id = _new(db, subject=f"subject {SECRET}")
        confirm_memory(db, memory_id, now=_later(1))
        contradict_memory(db, memory_id, now=_later(2))
        new_id = correct_memory(db, memory_id, content=f"corrected {SECRET}", now=_later(3))
        archive_memory(db, new_id, now=_later(4))
        restore_memory(db, new_id, now=_later(5))
        with pytest.raises(LifecycleError) as exc:
            archive_memory(db, memory_id, now=_later(6))
        assert SECRET not in str(exc.value)
    finally:
        logging.getLogger().handlers.clear()
    payloads = [json.dumps(e["payload"]) for e in _rows(db, "SELECT payload FROM audit_event")]
    assert len(payloads) == 6
    assert not any(SECRET in p for p in payloads)
    assert SECRET not in stream.getvalue()


# ---------------------------------------------------------------------------
# invariants over random operation sequences (spec D.9/3, C.7)
# ---------------------------------------------------------------------------

CHAINS_QUERY = """
WITH RECURSIVE walk AS (
    SELECT id AS root, id, superseded_by_id, status FROM memory m
     WHERE NOT EXISTS (SELECT 1 FROM memory p WHERE p.superseded_by_id = m.id)
    UNION ALL
    SELECT w.root, m.id, m.superseded_by_id, m.status
      FROM walk w JOIN memory m ON m.id = w.superseded_by_id
)
SELECT root,
       count(*) FILTER (WHERE superseded_by_id IS NULL) AS heads,
       count(*) FILTER (WHERE status = 'active') AS active,
       bool_and(status <> 'active' OR superseded_by_id IS NULL) AS active_is_head
  FROM walk GROUP BY root
"""

FROZEN = ("scope", "kind", "origin_tier", "origin", "created_at")


@pytest.mark.parametrize("seed", [7, 23, 101])
def test_random_operations_keep_every_invariant(db: Database, seed: int) -> None:
    rng = random.Random(seed)
    heads = [_new(db, subject=f"subject {i}") for i in range(3)]
    frozen: dict[uuid.UUID, tuple[Any, ...]] = {}
    for minute in range(1, 41):
        index = rng.randrange(len(heads))
        head = heads[index]
        operation = rng.choice(["confirm", "contradict", "correct", "archive", "restore"])
        try:
            if operation == "confirm":
                confirm_memory(db, head, now=_later(minute))
            elif operation == "contradict":
                contradict_memory(db, head, now=_later(minute))
            elif operation == "correct":
                heads[index] = correct_memory(
                    db, head, content=f"version {minute}", now=_later(minute)
                )
            elif operation == "archive":
                archive_memory(db, head, now=_later(minute))
            else:
                restore_memory(db, head, now=_later(minute))
        except LifecycleError:
            pass  # a refused operation must leave everything as it was

        for chain in _rows(db, CHAINS_QUERY):
            assert chain["heads"] == 1
            assert chain["active"] <= 1
            assert chain["active_is_head"] is True
        for row in _rows(db, f"SELECT id, {', '.join(FROZEN)} FROM memory"):
            values = tuple(row[f] for f in FROZEN)
            assert frozen.setdefault(row["id"], values) == values
        assert (
            _count(db, "memory")
            == _rows(
                db,
                "SELECT count(DISTINCT memory_id) AS n FROM memory_observation"
                " WHERE relation = 'asserts'",
            )[0]["n"]
        )
    assert len(_rows(db, CHAINS_QUERY)) == 3
