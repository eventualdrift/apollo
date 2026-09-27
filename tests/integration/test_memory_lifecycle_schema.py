"""The memory lifecycle held by the database (migration 0003; spec D.1 as clarified by D.9).

Every test here runs raw SQL as the least-privilege runtime role, which is what
Apollo runs as: the point is that no code path — the lifecycle module, a bug,
or a hand-written UPDATE — can produce a state the lifecycle forbids.
"""

from __future__ import annotations

import itertools
import json
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from apollo.memory.lifecycle import ROW_TRANSITIONS
from apollo.memory.models import Status
from apollo.storage.db import Database

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
Conn = psycopg.Connection[Any]


# ---------------------------------------------------------------------------
# helpers: raw SQL, each a single legal step unless a test says otherwise
# ---------------------------------------------------------------------------


def _insert_memory(cur: psycopg.Cursor[Any], *, origin_tier: str = "user_asserted") -> uuid.UUID:
    memory_id = uuid.uuid4()
    cur.execute(
        "INSERT INTO memory (id, scope, kind, subject, content, origin_tier, origin,"
        " status, created_at, updated_at)"
        " VALUES (%s, 'user', 'fact', 'subject', 'content', %s, 'personal', 'active', %s, %s)",
        (memory_id, origin_tier, NOW, NOW),
    )
    return memory_id


def _observe(
    cur: psycopg.Cursor[Any],
    memory_id: uuid.UUID,
    relation: str = "asserts",
    source_kind: str = "user_direct_entry",
    message_id: uuid.UUID | None = None,
    excerpt: str | None = None,
) -> uuid.UUID:
    observation_id = uuid.uuid4()
    cur.execute(
        "INSERT INTO memory_observation (id, memory_id, relation, source_kind, message_id,"
        " excerpt, observed_at, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (observation_id, memory_id, relation, source_kind, message_id, excerpt, NOW, NOW),
    )
    return observation_id


def create(db: Database, *, excerpt: str | None = None) -> uuid.UUID:
    """A memory and its asserts observation, in one transaction."""
    with db.connect() as conn, conn.transaction(), conn.cursor() as cur:
        memory_id = _insert_memory(cur)
        _observe(cur, memory_id, excerpt=excerpt)
    return memory_id


def correct(db: Database, old: uuid.UUID) -> uuid.UUID:
    """Spec D.5: a new active row, then the old row superseded by it."""
    with db.connect() as conn, conn.transaction(), conn.cursor() as cur:
        new = _insert_memory(cur)
        _observe(cur, new)
        cur.execute(
            "UPDATE memory SET status = 'superseded', superseded_by_id = %s, updated_at = %s"
            " WHERE id = %s",
            (new, NOW, old),
        )
    return new


def archive(cur: psycopg.Cursor[Any], memory_id: uuid.UUID) -> None:
    cur.execute(
        "UPDATE memory SET status = 'archived', archived_at = %s WHERE id = %s", (NOW, memory_id)
    )


def restore(cur: psycopg.Cursor[Any], memory_id: uuid.UUID) -> None:
    cur.execute(
        "UPDATE memory SET status = 'active', archived_at = NULL WHERE id = %s", (memory_id,)
    )


def tombstone(cur: psycopg.Cursor[Any], memory_id: uuid.UUID) -> None:
    cur.execute(
        "UPDATE memory SET status = 'tombstoned', subject = NULL, content = NULL,"
        " tombstoned_at = %s WHERE id = %s",
        (NOW, memory_id),
    )


def status_of(db: Database, memory_id: uuid.UUID) -> str:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM memory WHERE id = %s", (memory_id,))
        return str(cur.fetchone()["status"])


def rejected(match: str) -> Any:
    return pytest.raises(psycopg.errors.RestrictViolation, match=match)


def in_status(db: Database, status: Status) -> tuple[uuid.UUID, uuid.UUID | None]:
    """A row in `status`, reached legally. Returns (row, its successor if superseded)."""
    memory_id = create(db)
    if status is Status.SUPERSEDED:
        return memory_id, correct(db, memory_id)
    with db.connect() as conn, conn.cursor() as cur:
        if status is Status.ARCHIVED:
            archive(cur, memory_id)
        elif status is Status.TOMBSTONED:
            tombstone(cur, memory_id)
    return memory_id, None


# ---------------------------------------------------------------------------
# creation and provenance
# ---------------------------------------------------------------------------


def test_phase_zero_writes_only_user_asserted(db: Database) -> None:
    with (
        db.connect() as conn,
        conn.cursor() as cur,
        pytest.raises(psycopg.errors.CheckViolation, match="memory_phase0_origin_tier_ck"),
    ):
        _insert_memory(cur, origin_tier="model_inferred")


@pytest.mark.parametrize(
    "column,value",
    [
        ("status", "'archived'"),
        ("superseded_by_id", "gen_random_uuid()"),
        ("archived_at", "now()"),
        ("tombstoned_at", "now()"),
    ],
)
def test_rows_are_created_active_and_bare(db: Database, column: str, value: str) -> None:
    with db.connect() as conn, conn.cursor() as cur, rejected("created active"):
        cur.execute(
            "INSERT INTO memory (id, scope, kind, subject, content, origin_tier, origin,"
            f" status, created_at, updated_at, {column})"
            " VALUES (gen_random_uuid(), 'user', 'fact', 's', 'c', 'user_asserted',"
            f" 'personal', 'active', now(), now(), {value})"
            if column != "status"
            else "INSERT INTO memory (id, scope, kind, subject, content, origin_tier, origin,"
            " status, created_at, updated_at) VALUES (gen_random_uuid(), 'user', 'fact',"
            " 's', 'c', 'user_asserted', 'personal', 'archived', now(), now())"
        )


def test_a_memory_without_an_assertion_cannot_commit(db: Database) -> None:
    with (
        rejected("without an asserts observation"),
        db.connect() as conn,
        conn.transaction(),  # the deferred check fires as this commits
        conn.cursor() as cur,
    ):
        memory_id = _insert_memory(cur)
        _observe(cur, memory_id, relation="confirms")  # evidence, but not an assertion
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM memory")
        assert cur.fetchone()["n"] == 0


def test_a_memory_with_its_assertion_commits(db: Database) -> None:
    assert status_of(db, create(db)) == "active"


# ---------------------------------------------------------------------------
# transitions: the database permits exactly ROW_TRANSITIONS
# ---------------------------------------------------------------------------


def _attempt(db: Database, source: Status, target: Status) -> bool:
    row, successor = in_status(db, source)
    try:
        with db.connect() as conn, conn.transaction(), conn.cursor() as cur:
            if target is Status.ACTIVE:
                restore(cur, row)
            elif target is Status.ARCHIVED:
                archive(cur, row)
            elif target is Status.TOMBSTONED:
                if successor is not None:
                    tombstone(cur, successor)  # a chain goes head first (D.9/1)
                tombstone(cur, row)
            else:
                head = _insert_memory(cur)
                _observe(cur, head)
                cur.execute(
                    "UPDATE memory SET status = 'superseded', superseded_by_id = %s WHERE id = %s",
                    (head, row),
                )
    except psycopg.errors.RestrictViolation:
        return False
    assert status_of(db, row) == target
    return True


@pytest.mark.parametrize(
    ("source", "target"),
    [(s, t) for s, t in itertools.product(Status, Status) if s is not t],
)
def test_the_database_permits_exactly_the_lifecycle_transitions(
    db: Database, source: Status, target: Status
) -> None:
    assert _attempt(db, source, target) is ((source, target) in ROW_TRANSITIONS)


def test_restore_must_clear_archived_at(db: Database) -> None:
    row, _ = in_status(db, Status.ARCHIVED)
    with db.connect() as conn, conn.cursor() as cur, rejected("wrong successor, archive"):
        cur.execute("UPDATE memory SET status = 'active' WHERE id = %s", (row,))


def test_archive_needs_archived_at_and_tombstone_needs_tombstoned_at(db: Database) -> None:
    row = create(db)
    with db.connect() as conn, conn.cursor() as cur:
        with rejected("wrong successor, archive"):
            cur.execute("UPDATE memory SET status = 'archived' WHERE id = %s", (row,))
        with rejected("wrong successor, archive"):
            cur.execute(
                "UPDATE memory SET status = 'tombstoned', subject = NULL, content = NULL"
                " WHERE id = %s",
                (row,),
            )


def test_a_tombstoned_row_is_terminal_in_every_field(db: Database) -> None:
    row, _ = in_status(db, Status.TOMBSTONED)
    with db.connect() as conn, conn.cursor() as cur:
        for change in ("pinned = true", "tombstoned_at = now()", "updated_at = now()"):
            with rejected("terminal"):
                cur.execute(f"UPDATE memory SET {change} WHERE id = %s", (row,))


# ---------------------------------------------------------------------------
# supersession chains: one head, at most one active, and it is the head (D.9/3)
# ---------------------------------------------------------------------------


def _supersede(cur: psycopg.Cursor[Any], row: uuid.UUID, by: uuid.UUID) -> None:
    cur.execute(
        "UPDATE memory SET status = 'superseded', superseded_by_id = %s WHERE id = %s", (by, row)
    )


def test_an_active_row_cannot_carry_a_successor(db: Database) -> None:
    a, b = create(db), create(db)
    with db.connect() as conn, conn.cursor() as cur, rejected("set only by a correction"):
        cur.execute("UPDATE memory SET superseded_by_id = %s WHERE id = %s", (b, a))


@pytest.mark.parametrize("source", [Status.ACTIVE, Status.ARCHIVED])
def test_a_tombstone_cannot_attach_a_successor(db: Database, source: Status) -> None:
    """Codex PR #8: a successor set while tombstoning would hang a dead row on a live head."""
    row, _ = in_status(db, source)
    head = create(db)
    with db.connect() as conn, conn.cursor() as cur, rejected("set only by a correction"):
        cur.execute(
            "UPDATE memory SET status = 'tombstoned', subject = NULL, content = NULL,"
            " tombstoned_at = %s, superseded_by_id = %s WHERE id = %s",
            (NOW, head, row),
        )
    assert status_of(db, row) == source


def test_a_row_cannot_supersede_itself(db: Database) -> None:
    a = create(db)
    with db.connect() as conn, conn.cursor() as cur, rejected("other than itself"):
        _supersede(cur, a, a)


@pytest.mark.parametrize("successor_status", [Status.ARCHIVED, Status.SUPERSEDED])
def test_the_successor_must_be_an_active_head(db: Database, successor_status: Status) -> None:
    a = create(db)
    successor, _ = in_status(db, successor_status)
    with db.connect() as conn, conn.cursor() as cur, rejected("active chain head"):
        _supersede(cur, a, successor)


def test_two_rows_cannot_share_a_successor(db: Database) -> None:
    a, b, c = create(db), create(db), create(db)
    with db.connect() as conn, conn.cursor() as cur:
        _supersede(cur, a, c)
        with pytest.raises(psycopg.errors.UniqueViolation, match="memory_superseded_by_uq"):
            _supersede(cur, b, c)


def test_a_chain_cannot_close_into_a_cycle(db: Database) -> None:
    a = create(db)
    b = correct(db, a)
    with db.connect() as conn, conn.cursor() as cur, rejected("active chain head"):
        _supersede(cur, b, a)


def test_the_successor_pointer_is_write_once(db: Database) -> None:
    a = create(db)
    correct(db, a)
    other = create(db)
    with db.connect() as conn, conn.cursor() as cur, rejected("superseded_by_id is write-once"):
        cur.execute("UPDATE memory SET superseded_by_id = %s WHERE id = %s", (other, a))


def test_a_superseded_row_cannot_be_revived(db: Database) -> None:
    a = create(db)
    correct(db, a)
    with db.connect() as conn, conn.cursor() as cur, rejected("not a lifecycle transition"):
        cur.execute("UPDATE memory SET status = 'active' WHERE id = %s", (a,))
    with db.connect() as conn, conn.cursor() as cur, rejected("superseded_by_id is write-once"):
        cur.execute(
            "UPDATE memory SET status = 'active', superseded_by_id = NULL WHERE id = %s", (a,)
        )


# ---------------------------------------------------------------------------
# chain-wide tombstone (D.9/1)
# ---------------------------------------------------------------------------


def _chain(db: Database, length: int) -> list[uuid.UUID]:
    rows = [create(db)]
    for _ in range(length - 1):
        rows.append(correct(db, rows[-1]))
    return rows  # oldest first; the last is the head


def test_a_predecessor_cannot_be_tombstoned_before_its_successor(db: Database) -> None:
    oldest, _head = _chain(db, 2)
    with db.connect() as conn, conn.cursor() as cur, rejected("only with its chain"):
        tombstone(cur, oldest)


def test_a_head_cannot_be_tombstoned_alone(db: Database) -> None:
    oldest, middle, head = _chain(db, 3)
    with (
        rejected("together with every predecessor"),
        db.connect() as conn,
        conn.transaction(),  # the deferred check fires as this commits
        conn.cursor() as cur,
    ):
        tombstone(cur, head)
        tombstone(cur, middle)  # the oldest is left holding its text
    assert [status_of(db, r) for r in (oldest, middle, head)] == [
        "superseded",
        "superseded",
        "active",
    ]


@pytest.mark.parametrize("archived_head", [False, True])
def test_a_whole_chain_tombstones_head_first(db: Database, archived_head: bool) -> None:
    rows = _chain(db, 3)
    with db.connect() as conn, conn.cursor() as cur:
        if archived_head:
            archive(cur, rows[-1])
    with db.connect() as conn, conn.transaction(), conn.cursor() as cur:
        for row in reversed(rows):
            tombstone(cur, row)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, subject, content, superseded_by_id FROM memory WHERE id = ANY(%s)",
            (rows,),
        )
        states = cur.fetchall()
    assert {s["status"] for s in states} == {"tombstoned"}
    assert all(s["subject"] is None and s["content"] is None for s in states)
    # The chain's links survive, so historical references do not dangle.
    assert sum(s["superseded_by_id"] is not None for s in states) == 2


def test_every_chain_keeps_one_head_and_at_most_one_active_row(db: Database) -> None:
    """D.9/3 over a mix of chains in every state the lifecycle can reach."""
    _chain(db, 3)
    archived = _chain(db, 2)
    tombstoned = _chain(db, 2)
    with db.connect() as conn, conn.cursor() as cur:
        archive(cur, archived[-1])
    with db.connect() as conn, conn.transaction(), conn.cursor() as cur:
        for row in reversed(tombstoned):
            tombstone(cur, row)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
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
        )
        chains = cur.fetchall()
    assert len(chains) == 3
    for chain in chains:
        assert chain["heads"] == 1
        assert chain["active"] <= 1
        assert chain["active_is_head"] is True


# ---------------------------------------------------------------------------
# observations
# ---------------------------------------------------------------------------


def _message(db: Database) -> uuid.UUID:
    conversation_id, message_id = uuid.uuid4(), uuid.uuid4()
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversation (id, mode, started_at, last_active_at)"
            " VALUES (%s, 'personal', %s, %s)",
            (conversation_id, NOW, NOW),
        )
        cur.execute(
            "INSERT INTO message (id, conversation_id, seq, role, content, created_at)"
            " VALUES (%s, %s, 1, 'user', 'remember this', %s)",
            (message_id, conversation_id, NOW),
        )
    return message_id


@pytest.mark.parametrize(
    ("status", "relation", "allowed"),
    [
        (Status.ACTIVE, "confirms", True),
        (Status.ACTIVE, "contradicts", True),
        (Status.ARCHIVED, "confirms", True),
        (Status.ARCHIVED, "contradicts", True),
        (Status.SUPERSEDED, "confirms", False),
        (Status.SUPERSEDED, "contradicts", False),
        (Status.TOMBSTONED, "confirms", False),
        (Status.TOMBSTONED, "contradicts", False),
        (Status.ARCHIVED, "asserts", False),
        (Status.SUPERSEDED, "asserts", False),
        (Status.TOMBSTONED, "asserts", False),
    ],
)
def test_which_rows_accept_new_evidence(
    db: Database, status: Status, relation: str, allowed: bool
) -> None:
    row, _ = in_status(db, status)
    with db.connect() as conn, conn.cursor() as cur:
        if allowed:
            _observe(cur, row, relation=relation)
        else:
            with rejected("observation|applies to|asserts observation"):
                _observe(cur, row, relation=relation)


@pytest.mark.parametrize(
    ("source_kind", "with_message", "allowed"),
    [
        ("user_message", True, True),
        ("model_proposal_approved", True, True),
        ("user_direct_entry", False, True),
        ("user_message", False, False),
        ("model_proposal_approved", False, False),
        ("user_direct_entry", True, False),
    ],
)
def test_a_conversational_source_names_its_message(
    db: Database, source_kind: str, with_message: bool, allowed: bool
) -> None:
    row = create(db)
    message_id = _message(db) if with_message else None
    with db.connect() as conn, conn.cursor() as cur:
        if allowed:
            _observe(cur, row, "confirms", source_kind, message_id)
        else:
            with rejected("message_id"):
                _observe(cur, row, "confirms", source_kind, message_id)


def _observation(db: Database, memory_id: uuid.UUID) -> uuid.UUID:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM memory_observation WHERE memory_id = %s", (memory_id,))
        return uuid.UUID(str(cur.fetchone()["id"]))


@pytest.mark.parametrize(
    "change",
    [
        "relation = 'contradicts'",
        "source_kind = 'document'",
        "observed_at = now()",
        "external_ref = 'x'",
    ],
)
def test_observations_are_write_once(db: Database, change: str) -> None:
    row = create(db, excerpt="the words")
    observation = _observation(db, row)
    with db.connect() as conn, conn.cursor() as cur, rejected("write-once"):
        cur.execute(f"UPDATE memory_observation SET {change} WHERE id = %s", (observation,))


def test_an_excerpt_cannot_be_cleared_while_its_memory_is_live(db: Database) -> None:
    row = create(db, excerpt="the words")
    with db.connect() as conn, conn.cursor() as cur, rejected("only ever cleared"):
        cur.execute("UPDATE memory_observation SET excerpt = NULL WHERE memory_id = %s", (row,))


def test_a_tombstone_that_leaves_an_excerpt_cannot_commit(db: Database) -> None:
    """Codex PR #8: forgetting the excerpt update must not quietly keep the words."""
    row = create(db, excerpt="the words")
    with (
        rejected("clears every observation excerpt"),
        db.connect() as conn,
        conn.transaction(),  # the deferred check fires as this commits
        conn.cursor() as cur,
    ):
        tombstone(cur, row)
    assert status_of(db, row) == "active"


def test_a_tombstone_clears_an_excerpt_and_cannot_rewrite_it(db: Database) -> None:
    row = create(db, excerpt="the words")
    with db.connect() as conn, conn.transaction(), conn.cursor() as cur:
        tombstone(cur, row)
        with rejected("only ever cleared"), conn.transaction():
            cur.execute(
                "UPDATE memory_observation SET excerpt = 'other words' WHERE memory_id = %s",
                (row,),
            )
        cur.execute("UPDATE memory_observation SET excerpt = NULL WHERE memory_id = %s", (row,))
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT excerpt FROM memory_observation WHERE memory_id = %s", (row,))
        assert cur.fetchone()["excerpt"] is None


# ---------------------------------------------------------------------------
# model_invocation: verification-hash redaction is one-way
# ---------------------------------------------------------------------------


def _invocation(
    db: Database,
    *,
    status: str = "started",
    prompt_hash: str | None = None,
    manifest: list[dict[str, Any]] | None = None,
) -> uuid.UUID:
    """An invocation recorded as Apollo records one: started, then completed if asked."""
    conversation_id, message_id = uuid.uuid4(), uuid.uuid4()
    turn_id, invocation_id = uuid.uuid4(), uuid.uuid4()
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
            " status, started_at)"
            " VALUES (%s, %s, 1, 'reply', 'brain.fake', 'fake', 'fake', 'r1', 'c1', 'e1',"
            " %s::jsonb, 'sha256:bundle', 10, 'T1', '{}'::jsonb, 'started', %s)",
            (invocation_id, turn_id, json.dumps(manifest or []), NOW),
        )
        if status != "started":
            cur.execute(
                "UPDATE model_invocation SET status = %s, rendered_prompt_hash = %s WHERE id = %s",
                (status, prompt_hash, invocation_id),
            )
    return invocation_id


def _update_invocation(db: Database, invocation_id: uuid.UUID, assignments: str) -> None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE model_invocation SET {assignments} WHERE id = %s", (invocation_id,))


REDACT = (
    "context_bundle_hash = NULL, rendered_prompt_hash = NULL, hashes_redacted_at = now(),"
    " hashes_redacted_reason = 'source_tombstoned'"
)


def test_completion_records_the_prompt_hash_once(db: Database) -> None:
    invocation = _invocation(db)
    _update_invocation(
        db, invocation, "status = 'completed', rendered_prompt_hash = 'sha256:prompt'"
    )
    with rejected("rendered_prompt_hash is set once"):
        _update_invocation(db, invocation, "rendered_prompt_hash = 'sha256:other'")


@pytest.mark.parametrize(
    "assignments",
    ["context_bundle_hash = 'sha256:other'", "context_bundle_hash = NULL"],
)
def test_the_bundle_hash_moves_only_by_redaction(db: Database, assignments: str) -> None:
    invocation = _invocation(db)
    with rejected("context_bundle_hash changes only by tombstone redaction"):
        _update_invocation(db, invocation, assignments)


def test_a_completed_prompt_hash_cannot_be_nulled_quietly(db: Database) -> None:
    invocation = _invocation(db, status="completed", prompt_hash="sha256:prompt")
    with rejected("rendered_prompt_hash is set once"):
        _update_invocation(db, invocation, "rendered_prompt_hash = NULL")


def test_a_redaction_nulls_both_hashes_and_records_why(db: Database) -> None:
    invocation = _invocation(db, status="completed", prompt_hash="sha256:prompt")
    _update_invocation(db, invocation, REDACT)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM model_invocation WHERE id = %s", (invocation,))
        row = cur.fetchone()
    assert row["context_bundle_hash"] is None and row["rendered_prompt_hash"] is None
    assert row["hashes_redacted_reason"] == "source_tombstoned"
    assert row["hashes_redacted_at"] is not None


@pytest.mark.parametrize(
    "assignments",
    [
        # only one hash nulled
        "context_bundle_hash = NULL, hashes_redacted_at = now(),"
        " hashes_redacted_reason = 'source_tombstoned'",
        # a reason other than the one phase zero has
        "context_bundle_hash = NULL, rendered_prompt_hash = NULL, hashes_redacted_at = now(),"
        " hashes_redacted_reason = 'tidying'",
        # a time without a reason
        "context_bundle_hash = NULL, rendered_prompt_hash = NULL, hashes_redacted_at = now()",
    ],
)
def test_a_partial_or_unexplained_redaction_is_refused(db: Database, assignments: str) -> None:
    invocation = _invocation(db, status="completed", prompt_hash="sha256:prompt")
    with pytest.raises(psycopg.errors.CheckViolation, match="invocation_redaction_ck"):
        _update_invocation(db, invocation, assignments)


@pytest.mark.parametrize(
    ("assignments", "message"),
    [
        ("context_bundle_hash = 'sha256:bundle'", "context_bundle_hash changes only"),
        ("rendered_prompt_hash = 'sha256:prompt'", "rendered_prompt_hash is set once"),
        ("hashes_redacted_at = now() + interval '1 day'", "redaction is permanent"),
        ("hashes_redacted_at = NULL, hashes_redacted_reason = NULL", "redaction is permanent"),
    ],
)
def test_a_redaction_is_permanent(db: Database, assignments: str, message: str) -> None:
    invocation = _invocation(db, status="completed", prompt_hash="sha256:prompt")
    _update_invocation(db, invocation, REDACT)
    with rejected(message):
        _update_invocation(db, invocation, assignments)


def test_the_migration_installs_its_rules(db: Database) -> None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tgrelid::regclass::text AS tbl, tgname FROM pg_trigger WHERE NOT tgisinternal"
        )
        triggers = {(r["tbl"], r["tgname"]) for r in cur.fetchall()}
        cur.execute(
            "SELECT conname FROM pg_constraint WHERE conname IN"
            " ('memory_phase0_origin_tier_ck', 'memory_superseded_by_uq',"
            "  'invocation_redaction_ck')"
        )
        constraints = {r["conname"] for r in cur.fetchall()}
    assert {
        ("memory", "memory_lifecycle_guard"),
        ("memory", "memory_requires_assertion"),
        ("memory", "memory_tombstone_complete"),
        ("memory_observation", "observation_guard"),
        ("model_invocation", "invocation_hash_guard"),
        ("model_invocation", "invocation_insert_guard"),
    } <= triggers
    assert constraints == {
        "memory_phase0_origin_tier_ck",
        "memory_superseded_by_uq",
        "invocation_redaction_ck",
    }


# ---------------------------------------------------------------------------
# concurrency: a tombstone serialises with evidence and corrections (Codex PR #8)
# ---------------------------------------------------------------------------


def _in_background(db: Database, statements: list[tuple[str, tuple[Any, ...]]]) -> dict[str, Any]:
    """Run statements in one transaction on a thread; report whether it waited and how it ended."""
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            with db.connect() as conn, conn.transaction(), conn.cursor() as cur:
                for query, params in statements:
                    cur.execute(query, params)
            outcome["result"] = "committed"
        except psycopg.errors.RestrictViolation as exc:
            outcome["result"] = "refused"
            outcome["message"] = str(exc)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(1.0)
    outcome["waited"] = thread.is_alive()
    outcome["thread"] = thread
    return outcome


TOMBSTONE_ALL = [
    (
        "UPDATE memory SET status = 'tombstoned', subject = NULL, content = NULL,"
        " tombstoned_at = now() WHERE id = %s",
        (),
    ),
    ("UPDATE memory_observation SET excerpt = NULL WHERE memory_id = %s", ()),
]


def _tombstone_statements(row: uuid.UUID) -> list[tuple[str, tuple[Any, ...]]]:
    return [(query, (row,)) for query, _ in TOMBSTONE_ALL]


def test_evidence_added_first_is_cleared_by_a_waiting_tombstone(db: Database) -> None:
    row = create(db)
    with db.connect() as conn, conn.transaction(), conn.cursor() as cur:
        _observe(cur, row, relation="confirms", excerpt="late words")
        background = _in_background(db, _tombstone_statements(row))
        assert background["waited"], "the tombstone must wait for the open insert"
    background["thread"].join(5)
    assert background["result"] == "committed"
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT excerpt FROM memory_observation WHERE memory_id = %s", (row,))
        assert [r["excerpt"] for r in cur.fetchall()] == [None, None]


def test_evidence_added_during_a_tombstone_is_refused(db: Database) -> None:
    row = create(db)
    with db.connect() as conn, conn.transaction(), conn.cursor() as cur:
        for query, params in _tombstone_statements(row):
            cur.execute(query, params)
        background = _in_background(
            db,
            [
                (
                    "INSERT INTO memory_observation (id, memory_id, relation, source_kind,"
                    " excerpt, observed_at, created_at) VALUES (gen_random_uuid(), %s,"
                    " 'confirms', 'user_direct_entry', 'late words', now(), now())",
                    (row,),
                )
            ],
        )
        assert background["waited"], "the insert must wait for the open tombstone"
    background["thread"].join(5)
    assert background["result"] == "refused"
    assert "tombstoned memory" in background["message"]


def test_a_correction_first_makes_a_waiting_tombstone_of_its_head_fail(db: Database) -> None:
    old, head = create(db), create(db)
    with db.connect() as conn, conn.transaction(), conn.cursor() as cur:
        _supersede(cur, old, head)
        background = _in_background(db, [(TOMBSTONE_ALL[0][0], (head,))])
        assert background["waited"], "the tombstone must wait for the open correction"
    background["thread"].join(5)
    assert background["result"] == "refused"
    assert "every predecessor" in background["message"]
    assert (status_of(db, old), status_of(db, head)) == ("superseded", "active")


def test_a_correction_during_a_tombstone_of_its_head_is_refused(db: Database) -> None:
    old, head = create(db), create(db)
    with db.connect() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(TOMBSTONE_ALL[0][0], (head,))
        background = _in_background(
            db,
            [
                (
                    "UPDATE memory SET status = 'superseded', superseded_by_id = %s WHERE id = %s",
                    (head, old),
                )
            ],
        )
        assert background["waited"], "the correction must wait for the open tombstone"
    background["thread"].join(5)
    assert background["result"] == "refused"
    assert (status_of(db, old), status_of(db, head)) == ("active", "tombstoned")


# ---------------------------------------------------------------------------
# the rules cannot be fooled or pre-empted (Codex PR #8, second review)
# ---------------------------------------------------------------------------


def test_a_prompt_hash_is_accepted_only_by_the_completing_update(db: Database) -> None:
    invocation = _invocation(db)
    with rejected("rendered_prompt_hash is set once"):
        _update_invocation(db, invocation, "rendered_prompt_hash = 'sha256:forged'")
    _update_invocation(db, invocation, "status = 'completed', rendered_prompt_hash = 'sha256:real'")
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT rendered_prompt_hash FROM model_invocation WHERE id = %s", (invocation,)
        )
        assert cur.fetchone()["rendered_prompt_hash"] == "sha256:real"


def test_a_failed_call_records_no_prompt_hash(db: Database) -> None:
    invocation = _invocation(db)
    with rejected("rendered_prompt_hash is set once"):
        _update_invocation(db, invocation, "status = 'failed', rendered_prompt_hash = 'sha256:x'")


def _shadow(cur: psycopg.Cursor[Any], table: str, row: dict[str, Any]) -> None:
    """A session-private table of the same name, which pg_temp would resolve first."""
    columns = {
        "memory": "id uuid, status text, superseded_by_id uuid",
        "memory_observation": "id uuid, memory_id uuid, relation text, excerpt text",
    }
    cur.execute(f"CREATE TEMP TABLE {table} ({columns[table]})")
    cur.execute(
        f"INSERT INTO pg_temp.{table} ({', '.join(row)}) VALUES ({', '.join(['%s'] * len(row))})",
        tuple(row.values()),
    )


def test_a_temporary_memory_table_cannot_vouch_for_a_tombstoned_parent(db: Database) -> None:
    row, _ = in_status(db, Status.TOMBSTONED)
    with db.connect() as conn, conn.cursor() as cur:
        _shadow(cur, "memory", {"id": row, "status": "active", "superseded_by_id": None})
        with rejected("tombstoned memory"):
            cur.execute(
                "INSERT INTO public.memory_observation (id, memory_id, relation, source_kind,"
                " excerpt, observed_at, created_at) VALUES (gen_random_uuid(), %s, 'confirms',"
                " 'user_direct_entry', 'words', now(), now())",
                (row,),
            )


def test_a_temporary_memory_table_cannot_vouch_for_a_successor(db: Database) -> None:
    old = create(db)
    head, _ = in_status(db, Status.TOMBSTONED)
    with db.connect() as conn, conn.cursor() as cur:
        _shadow(cur, "memory", {"id": head, "status": "active", "superseded_by_id": None})
        with rejected("active chain head"):
            cur.execute(
                "UPDATE public.memory SET status = 'superseded', superseded_by_id = %s"
                " WHERE id = %s",
                (head, old),
            )


def test_a_temporary_observation_table_cannot_supply_an_assertion(db: Database) -> None:
    memory_id = uuid.uuid4()
    with (
        rejected("without an asserts observation"),
        db.connect() as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _shadow(
            cur,
            "memory_observation",
            {"id": uuid.uuid4(), "memory_id": memory_id, "relation": "asserts", "excerpt": None},
        )
        cur.execute(
            "INSERT INTO public.memory (id, scope, kind, subject, content, origin_tier, origin,"
            " status, created_at, updated_at) VALUES (%s, 'user', 'fact', 's', 'c',"
            " 'user_asserted', 'personal', 'active', now(), now())",
            (memory_id,),
        )


def test_a_temporary_observation_table_cannot_hide_an_excerpt(db: Database) -> None:
    row = create(db, excerpt="the words")
    with (
        rejected("clears every observation excerpt"),
        db.connect() as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _shadow(
            cur,
            "memory_observation",
            {"id": uuid.uuid4(), "memory_id": row, "relation": "asserts", "excerpt": None},
        )
        cur.execute(
            "UPDATE public.memory SET status = 'tombstoned', subject = NULL, content = NULL,"
            " tombstoned_at = now() WHERE id = %s",
            (row,),
        )


def test_every_lifecycle_function_pins_its_search_path(db: Database) -> None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT proname, proconfig FROM pg_proc WHERE proname IN"
            " ('apollo_memory_lifecycle_guard', 'apollo_memory_requires_assertion',"
            "  'apollo_memory_tombstone_complete', 'apollo_observation_guard',"
            "  'apollo_invocation_hash_guard', 'apollo_invocation_insert_guard')"
        )
        configs = {r["proname"]: r["proconfig"] for r in cur.fetchall()}
    assert len(configs) == 6
    for name, config in configs.items():
        assert config == ["search_path=pg_catalog, public, pg_temp"], name


# ---------------------------------------------------------------------------
# a tombstone removes the verification-hash oracle (Codex PR #8, third review)
# ---------------------------------------------------------------------------


def _entry(memory_id: uuid.UUID, included: bool = True) -> dict[str, Any]:
    return {"source_kind": "memory", "source_ref": str(memory_id), "included": included}


def _redact_for(cur: psycopg.Cursor[Any], memory_id: uuid.UUID) -> None:
    cur.execute(
        f"UPDATE model_invocation SET {REDACT} WHERE hashes_redacted_at IS NULL"
        " AND context_manifest @> %s::jsonb",
        (json.dumps([_entry(memory_id)]),),
    )


def _hashes(db: Database, invocation_id: uuid.UUID) -> tuple[Any, Any]:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT context_bundle_hash, hashes_redacted_reason FROM model_invocation"
            " WHERE id = %s",
            (invocation_id,),
        )
        row = cur.fetchone()
    return row["context_bundle_hash"], row["hashes_redacted_reason"]


def test_a_tombstone_that_leaves_an_included_hash_cannot_commit(db: Database) -> None:
    row = create(db)
    invocation = _invocation(db, manifest=[_entry(row)])
    with (
        rejected("redacts the verification hashes"),
        db.connect() as conn,
        conn.transaction(),  # the deferred check fires as this commits
        conn.cursor() as cur,
    ):
        tombstone(cur, row)
    assert _hashes(db, invocation) == ("sha256:bundle", None)


def test_a_tombstone_with_its_redaction_commits_and_spares_dropped_blocks(
    db: Database,
) -> None:
    row, other = create(db), create(db)
    included = _invocation(db, status="completed", prompt_hash="sha256:p", manifest=[_entry(row)])
    dropped = _invocation(db, manifest=[_entry(row, included=False)])
    unrelated = _invocation(db, manifest=[_entry(other)])
    with db.connect() as conn, conn.transaction(), conn.cursor() as cur:
        tombstone(cur, row)
        _redact_for(cur, row)
    assert _hashes(db, included) == (None, "source_tombstoned")
    assert _hashes(db, dropped) == ("sha256:bundle", None)
    assert _hashes(db, unrelated) == ("sha256:bundle", None)


@pytest.mark.parametrize(
    "column,value",
    [
        ("rendered_prompt_hash", "'sha256:forged'"),
        ("status", "'completed'"),
        ("hashes_redacted_at", "now()"),
    ],
)
def test_an_invocation_is_recorded_started_and_bare(db: Database, column: str, value: str) -> None:
    invocation = _invocation(db)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT turn_id FROM model_invocation WHERE id = %s", (invocation,))
        turn_id = cur.fetchone()["turn_id"]
        columns = (
            "id, turn_id, seq, purpose, brain_alias, provider_key, adapter_key,"
            " render_version, compiler_version, token_estimator, context_manifest,"
            " context_token_estimate, max_trust_tier, generation_params, status, started_at"
        )
        values = (
            "gen_random_uuid(), %s, 2, 'reply', 'b', 'p', 'a', 'r', 'c', 'e', '[]', 1,"
            " 'T1', '{}', 'started', now()"
        )
        if column == "status":
            values = values.replace("'started'", value)
        else:
            columns += f", {column}"
            values += f", {value}"
        with rejected("recorded started"):
            cur.execute(f"INSERT INTO model_invocation ({columns}) VALUES ({values})", (turn_id,))


@pytest.mark.parametrize("state", ["tombstoned", "missing", "malformed"])
def test_an_invocation_cannot_include_a_forgotten_memory(db: Database, state: str) -> None:
    if state == "tombstoned":
        ref: Any = in_status(db, Status.TOMBSTONED)[0]
        entry = _entry(ref)
        message = "tombstoned or missing"
    elif state == "missing":
        entry = _entry(uuid.uuid4())
        message = "tombstoned or missing"
    else:
        entry = {"source_kind": "memory", "source_ref": "not-a-uuid", "included": True}
        message = "canonical uuid"
    with rejected(message):
        _invocation(db, manifest=[entry])


def test_a_dropped_block_of_a_forgotten_memory_may_still_be_recorded(db: Database) -> None:
    row, _ = in_status(db, Status.TOMBSTONED)
    _invocation(db, manifest=[_entry(row, included=False)])


def test_an_invocation_recorded_first_is_redacted_by_a_waiting_tombstone(db: Database) -> None:
    row = create(db)
    turn_invocation = _invocation(db)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT turn_id FROM model_invocation WHERE id = %s", (turn_invocation,))
        turn_id = cur.fetchone()["turn_id"]
    insert = (
        "INSERT INTO model_invocation (id, turn_id, seq, purpose, brain_alias, provider_key,"
        " adapter_key, render_version, compiler_version, token_estimator, context_manifest,"
        " context_bundle_hash, context_token_estimate, max_trust_tier, generation_params,"
        " status, started_at) VALUES (%s, %s, 2, 'reply', 'b', 'p', 'a', 'r', 'c', 'e',"
        " %s::jsonb, 'sha256:bundle', 1, 'T3', '{}', 'started', now())"
    )
    invocation = uuid.uuid4()
    with db.connect() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(insert, (invocation, turn_id, json.dumps([_entry(row)])))
        background = _in_background(
            db,
            [
                (TOMBSTONE_ALL[0][0], (row,)),
                (
                    f"UPDATE model_invocation SET {REDACT} WHERE hashes_redacted_at IS NULL"
                    " AND context_manifest @> %s::jsonb",
                    (json.dumps([_entry(row)]),),
                ),
            ],
        )
        assert background["waited"], "the tombstone must wait for the open insert"
    background["thread"].join(5)
    assert background["result"] == "committed"
    assert _hashes(db, invocation) == (None, "source_tombstoned")


def test_an_invocation_recorded_during_a_tombstone_is_refused(db: Database) -> None:
    row = create(db)
    turn_invocation = _invocation(db)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT turn_id FROM model_invocation WHERE id = %s", (turn_invocation,))
        turn_id = cur.fetchone()["turn_id"]
    with db.connect() as conn, conn.transaction(), conn.cursor() as cur:
        tombstone(cur, row)
        _redact_for(cur, row)
        background = _in_background(
            db,
            [
                (
                    "INSERT INTO model_invocation (id, turn_id, seq, purpose, brain_alias,"
                    " provider_key, adapter_key, render_version, compiler_version,"
                    " token_estimator, context_manifest, context_bundle_hash,"
                    " context_token_estimate, max_trust_tier, generation_params, status,"
                    " started_at) VALUES (gen_random_uuid(), %s, 2, 'reply', 'b', 'p', 'a',"
                    " 'r', 'c', 'e', %s::jsonb, 'sha256:bundle', 1, 'T3', '{}', 'started', now())",
                    (turn_id, json.dumps([_entry(row)])),
                )
            ],
        )
        assert background["waited"], "the insert must wait for the open tombstone"
    background["thread"].join(5)
    assert background["result"] == "refused"
    assert "tombstoned or missing" in background["message"]


# ---------------------------------------------------------------------------
# Janu's review of 0003
# ---------------------------------------------------------------------------


def _create_with(db: Database, **fields: str) -> uuid.UUID:
    """A memory with the helper's defaults except `fields`, and its assertion."""
    values = {"scope": "user", "kind": "fact", "subject": "subject", "origin": "personal"}
    values.update(fields)
    memory_id = uuid.uuid4()
    with db.connect() as conn, conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO memory (id, scope, kind, subject, content, origin_tier, origin,"
            " status, created_at, updated_at) VALUES (%s, %s, %s, %s, 'content',"
            " 'user_asserted', %s, 'active', %s, %s)",
            (
                memory_id,
                values["scope"],
                values["kind"],
                values["subject"],
                values["origin"],
                NOW,
                NOW,
            ),
        )
        _observe(cur, memory_id)
    return memory_id


@pytest.mark.parametrize(
    "field,value",
    [
        ("scope", "relationship"),
        ("kind", "preference"),
        ("subject", "communication"),
        ("origin", "fixture"),
        # origin_tier is compared too, but a differing successor cannot be built
        # while memory_phase0_origin_tier_ck allows only user_asserted.
    ],
)
def test_a_correction_cannot_link_an_unrelated_memory(db: Database, field: str, value: str) -> None:
    old = _create_with(db)
    unrelated = _create_with(db, **{field: value})
    with db.connect() as conn, conn.cursor() as cur, rejected("keeps scope, kind, subject"):
        _supersede(cur, old, unrelated)
    assert status_of(db, old) == "active"


def test_a_correction_may_change_content_and_pinned(db: Database) -> None:
    old = _create_with(db)
    new = _create_with(db)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE memory SET pinned = true WHERE id = %s", (new,))
        _supersede(cur, old, new)
    assert status_of(db, old) == "superseded"


def test_a_redacted_call_in_flight_still_completes_without_a_prompt_hash(db: Database) -> None:
    invocation = _invocation(db)
    _update_invocation(db, invocation, REDACT)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT hashes_redacted_at, hashes_redacted_reason FROM model_invocation WHERE id = %s",
            (invocation,),
        )
        before = cur.fetchone()
    _update_invocation(
        db, invocation, "status = 'completed', rendered_prompt_hash = 'sha256:prompt'"
    )
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM model_invocation WHERE id = %s", (invocation,))
        after = cur.fetchone()
    assert after["status"] == "completed"
    assert after["rendered_prompt_hash"] is None and after["context_bundle_hash"] is None
    assert after["hashes_redacted_at"] == before["hashes_redacted_at"]
    assert after["hashes_redacted_reason"] == before["hashes_redacted_reason"]


def test_a_redacted_completed_call_still_refuses_a_prompt_hash(db: Database) -> None:
    invocation = _invocation(db)
    _update_invocation(db, invocation, REDACT)
    _update_invocation(db, invocation, "status = 'completed'")
    with rejected("rendered_prompt_hash is set once"):
        _update_invocation(db, invocation, "rendered_prompt_hash = 'sha256:late'")


# ---------------------------------------------------------------------------
# the manifest is in the form the redaction matches (Codex PR #8, fourth review)
# ---------------------------------------------------------------------------


def _raw_invocation(db: Database, manifest: Any, bundle_hash: str | None = "sha256:b") -> None:
    turn_invocation = _invocation(db)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT turn_id FROM model_invocation WHERE id = %s", (turn_invocation,))
        turn_id = cur.fetchone()["turn_id"]
        cur.execute(
            "INSERT INTO model_invocation (id, turn_id, seq, purpose, brain_alias, provider_key,"
            " adapter_key, render_version, compiler_version, token_estimator, context_manifest,"
            " context_bundle_hash, context_token_estimate, max_trust_tier, generation_params,"
            " status, started_at) VALUES (gen_random_uuid(), %s, 2, 'reply', 'b', 'p', 'a',"
            " 'r', 'c', 'e', %s::jsonb, %s, 1, 'T3', '{}', 'started', now())",
            (turn_id, json.dumps(manifest), bundle_hash),
        )


def test_an_invocation_is_recorded_with_its_bundle_hash(db: Database) -> None:
    with rejected("with its bundle hash"):
        _raw_invocation(db, [], bundle_hash=None)


@pytest.mark.parametrize(
    "shape",
    ["object", "scalar", "non-object entry"],
)
def test_a_manifest_is_an_array_of_entry_objects(db: Database, shape: str) -> None:
    row, _ = in_status(db, Status.TOMBSTONED)
    manifest: Any = {
        "object": _entry(row),
        "scalar": "memory",
        "non-object entry": [_entry(create(db)), "memory"],
    }[shape]
    with rejected("context manifest"):
        _raw_invocation(db, manifest)


@pytest.mark.parametrize("included", ["true", 1, None])
def test_a_memory_entry_says_whether_it_was_included_as_a_boolean(
    db: Database, included: Any
) -> None:
    row, _ = in_status(db, Status.TOMBSTONED)
    entry: dict[str, Any] = {"source_kind": "memory", "source_ref": str(row)}
    if included is not None:
        entry["included"] = included
    with rejected("says whether it was included"):
        _raw_invocation(db, [entry])


def test_a_dropped_memory_entry_still_names_a_canonical_uuid(db: Database) -> None:
    with rejected("canonical uuid"):
        _raw_invocation(
            db, [{"source_kind": "memory", "source_ref": "NOT-A-UUID", "included": False}]
        )


def test_0004_is_the_insert_guard_in_force(db: Database) -> None:
    """The live function is 0004's: pinned search_path, strict manifest shape."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM schema_migration WHERE name = '0004_invocation_manifest_shape.sql'"
        )
        assert cur.fetchone() is not None
        cur.execute(
            "SELECT proconfig, prosrc FROM pg_proc WHERE proname = 'apollo_invocation_insert_guard'"
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["proconfig"] == ["search_path=pg_catalog, public, pg_temp"]
    assert "a context manifest is an array of entries" in rows[0]["prosrc"]
    assert "FROM public.memory" in rows[0]["prosrc"]
