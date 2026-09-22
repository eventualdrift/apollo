"""Gate 1's provider probe uses the recorded invocation lifecycle."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

import pytest

from apollo.brains.base import Generation, GenerationParams, RenderedRequest
from apollo.brains.fake import FakeBrain
from apollo.brains.registry import BrainRegistry
from apollo.cli.main import _run_gate1
from apollo.config import SURFACE_EVAL
from apollo.core.identity import IdentityLoader
from apollo.errors import BrainTransportError, ErrorKind
from apollo.evals.gate1 import evaluate_gate1
from apollo.storage.db import open_transaction_depth
from apollo.storage.repositories import MessageRepository
from tests.integration.conftest import make_config

pytestmark = pytest.mark.integration


def run_gate(db, clock, brain, **provider_options):  # type: ignore[no-untyped-def]
    config = make_config(db._dsn, **provider_options)
    registry = BrainRegistry(config, surface=SURFACE_EVAL)
    registry._cache["brain.default"] = brain
    return evaluate_gate1(
        db,
        config,
        registry,
        IdentityLoader(config.identity_dir),
        brain_alias="brain.default",
        clock=clock,
    )


def rows(db) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM conversation ORDER BY started_at")
        conversations = cur.fetchall()
        cur.execute("SELECT * FROM turn ORDER BY started_at")
        turns = cur.fetchall()
        cur.execute("SELECT * FROM model_invocation ORDER BY started_at")
        invocations = cur.fetchall()
    return conversations, turns, invocations


def test_success_is_one_committed_invocation_and_one_provider_call(db, clock, monkeypatch) -> None:
    observed_depths: list[int] = []
    observed_connections: list[int] = []
    saw_started_row: list[bool] = []
    connections = []
    original_connect = db.connect

    @contextmanager
    def tracked_connect():
        with original_connect() as conn:
            connections.append(conn)
            yield conn

    monkeypatch.setattr(db, "connect", tracked_connect)

    class ObservingBrain(FakeBrain):
        def __init__(self) -> None:
            super().__init__(max_context=9024)

        def render(self, bundle):  # type: ignore[no-untyped-def]
            observed_depths.append(open_transaction_depth())
            observed_connections.append(sum(not conn.closed for conn in connections))
            return super().render(bundle)

        def generate(self, req, params):  # type: ignore[no-untyped-def]
            observed_depths.append(open_transaction_depth())
            observed_connections.append(sum(not conn.closed for conn in connections))
            # A separate observer connection, opened only after checking that
            # every production-path connection has already been closed.
            with original_connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT status FROM model_invocation ORDER BY started_at DESC LIMIT 1")
                row = cur.fetchone()
                saw_started_row.append(row is not None and row["status"] == "started")
            return super().generate(req, params)

    brain = ObservingBrain()
    report = run_gate(db, clock, brain)
    conversations, turns, invocations = rows(db)

    assert report.passed
    assert report.generation_probe_passed is True
    assert len(brain.calls) == 1
    assert saw_started_row == [True]
    assert observed_depths and set(observed_depths) == {0}
    assert observed_connections and set(observed_connections) == {0}
    assert connections and all(conn.closed for conn in connections)
    assert len(conversations) == len(turns) == len(invocations) == 1
    assert conversations[0]["mode"] == "benchmark"
    assert turns[0]["status"] == "completed"
    assert invocations[0]["status"] == "completed"
    assert invocations[0]["purpose"] == "reply"
    assert invocations[0]["rendered_prompt_hash"]


def test_provider_failure_is_recorded_once_and_cli_exits_nonzero(db, capsys) -> None:
    config = make_config(db._dsn, mode="scripted")

    exit_code = _run_gate1(config, "brain.default", eval_surface=True)
    output = capsys.readouterr()
    _, turns, invocations = rows(db)

    assert exit_code == 1
    assert "Gate 1: FAIL" in output.out
    assert "FAIL  recorded generation probe" in output.out
    assert len(turns) == len(invocations) == 1
    assert turns[0]["status"] == "failed"
    assert invocations[0]["status"] == "failed"
    assert invocations[0]["retry_of_invocation_id"] is None


def valid_generation(request: RenderedRequest, **changes: Any) -> Generation:
    value = Generation(
        text="Gate 1 probe acknowledged.",
        finish_reason="stop",
        model_identifier="test/gate1",
        latency_ms=1,
        rendered_prompt_hash=request.prompt_hash,
        prompt_tokens=10,
        completion_tokens=5,
        reasoning_tokens=None,
        raw_meta={},
    )
    return replace(value, **changes)


class ReturningBrain(FakeBrain):
    def __init__(self, result: Callable[[RenderedRequest], object]) -> None:
        super().__init__(max_context=9024)
        self._result = result

    def generate(self, req: RenderedRequest, params: GenerationParams) -> Generation:
        self.calls.append(req.prompt_hash)
        return self._result(req)  # type: ignore[return-value]


@pytest.mark.parametrize(
    "result,expected_kind",
    [
        (lambda req: object(), ErrorKind.INTERNAL),
        (lambda req: valid_generation(req, text=" \n\t"), ErrorKind.EMPTY_GENERATION),
        (lambda req: valid_generation(req, model_identifier=""), ErrorKind.INTERNAL),
        (lambda req: valid_generation(req, finish_reason=""), ErrorKind.INTERNAL),
        (lambda req: valid_generation(req, latency_ms=-1), ErrorKind.INTERNAL),
        (lambda req: valid_generation(req, rendered_prompt_hash="wrong"), ErrorKind.INTERNAL),
        (lambda req: valid_generation(req, raw_meta=[]), ErrorKind.INTERNAL),
    ],
    ids=[
        "wrong-type",
        "empty-text",
        "missing-model",
        "missing-finish-reason",
        "negative-latency",
        "hash-mismatch",
        "malformed-meta",
    ],
)
def test_malformed_generation_is_a_recorded_nonpassing_attempt(
    db, clock, result, expected_kind
) -> None:  # type: ignore[no-untyped-def]
    brain = ReturningBrain(result)

    report = run_gate(db, clock, brain)
    _, turns, invocations = rows(db)

    assert not report.passed
    assert report.generation_probe_passed is False
    assert len(brain.calls) == 1
    assert len(turns) == len(invocations) == 1
    assert turns[0]["status"] == "failed"
    assert invocations[0]["status"] == "failed"
    assert turns[0]["error_kind"] == expected_kind
    assert invocations[0]["error_kind"] == expected_kind
    assert invocations[0]["retry_of_invocation_id"] is None


def test_transport_failure_has_no_fallback_or_hidden_retry(db, clock) -> None:
    brain = FakeBrain(
        max_context=9024,
        fail_times=1,
        failure=BrainTransportError(http_status=503, provider_error_type="unavailable"),
    )

    report = run_gate(db, clock, brain)
    _, turns, invocations = rows(db)

    assert not report.passed
    assert len(brain.calls) == 1
    assert len(turns) == len(invocations) == 1
    assert turns[0]["status"] == "failed"
    assert invocations[0]["status"] == "failed"
    assert invocations[0]["error_kind"] == ErrorKind.BRAIN_UNAVAILABLE
    assert invocations[0]["retry_of_invocation_id"] is None


def test_actual_probe_render_is_validated_before_generation(db, clock) -> None:
    class ChangingRender(ReturningBrain):
        render_count = 0

        def render(self, bundle):
            request = super().render(bundle)
            self.render_count += 1
            # Static renders are valid, but the request used by invoke is not.
            return replace(request, messages=()) if self.render_count == 4 else request

    brain = ChangingRender(valid_generation)
    report = run_gate(db, clock, brain)
    _, turns, invocations = rows(db)

    assert report.static_passed
    assert not report.passed
    assert brain.calls == []
    assert turns[0]["status"] == invocations[0]["status"] == "failed"


def test_probe_failure_records_a_system_note_with_its_audit(db, clock) -> None:
    report = run_gate(db, clock, FakeBrain(max_context=9024, fail_times=1))
    _, turns, invocations = rows(db)
    assert not report.passed
    assert turns[0]["status"] == invocations[0]["status"] == "failed"
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM message WHERE role = 'system_note'")
        notes = cur.fetchall()
        assert len(notes) == 1
        assert notes[0]["turn_id"] == turns[0]["id"]
        cur.execute(
            "SELECT event_type FROM audit_event WHERE subject_id = %s",
            (notes[0]["id"],),
        )
        assert [row["event_type"] for row in cur.fetchall()] == ["message.created"]


def test_response_storage_failure_does_not_leave_a_started_turn(db, clock, monkeypatch) -> None:
    original_append = MessageRepository.append

    def fail_response(self, **kwargs):
        if kwargs["role"] == "apollo":
            raise RuntimeError("synthetic response persistence failure")
        return original_append(self, **kwargs)

    monkeypatch.setattr(MessageRepository, "append", fail_response)
    brain = ReturningBrain(valid_generation)
    report = run_gate(db, clock, brain)
    _, turns, invocations = rows(db)

    assert not report.passed
    assert len(brain.calls) == 1
    assert invocations[0]["status"] == "completed"  # the provider did succeed
    assert turns[0]["status"] == "failed"
    assert turns[0]["response_message_id"] is None
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT role FROM message ORDER BY seq")
        assert [row["role"] for row in cur.fetchall()] == ["user", "system_note"]


@pytest.mark.parametrize("fails", [False, True], ids=["metadata", "exception"])
def test_probe_privacy_sentinels_do_not_reach_records_or_logs(db, clock, caplog, fails) -> None:
    sentinels = ("SENTINEL-GATE1-REASONING", "SENTINEL-GATE1-BODY", "SENTINEL-GATE1-SECRET")

    def result(request):
        if fails:
            raise RuntimeError(" ".join(sentinels))
        return valid_generation(
            request,
            raw_meta={
                "reasoning_content": sentinels[0],
                "body": sentinels[1],
                "authorization": sentinels[2],
            },
        )

    caplog.set_level(logging.INFO, logger="apollo")
    report = run_gate(db, clock, ReturningBrain(result))
    _, turns, invocations = rows(db)
    assert report.passed is not fails
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT payload FROM audit_event")
        payloads = cur.fetchall()
        cur.execute("SELECT content FROM message")
        messages = cur.fetchall()
    captured = repr((turns, invocations, payloads, messages)) + caplog.text
    assert caplog.records
    assert all(sentinel not in captured for sentinel in sentinels)
    assert all(sentinel not in "\n".join(report.as_lines()) for sentinel in sentinels)
