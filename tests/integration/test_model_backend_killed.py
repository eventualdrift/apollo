"""O.1/27: the model backend is killed mid-session, and comes back.

Real sockets, the real adapter and the real turn path. A small HTTP server
stands in for a model server (it speaks the OpenAI-compatible chat API). It is
shut down between turns, so the adapter meets a refused connection exactly as
it would if `llama-server` had died, and then started again on the same port.

What this shows is Apollo's side of the criterion: the failure is recorded and
visible, nothing is made up in the model's place, no other provider is tried,
and the conversation carries on when the server returns. It is not a
real-model run; `docs/operations/acceptance.md` has that procedure.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from apollo.brains.openai_compatible import OpenAICompatibleBrain
from apollo.config import ProviderConfig
from apollo.core import replay
from apollo.core.conversations import create_conversation, transcript
from apollo.core.replay import BundleStatus, RenderStatus
from apollo.core.turns import TRANSPORT_NOTES
from apollo.errors import TransportReason
from apollo.storage.db import Database

from .conftest import make_config
from .test_memory_in_replies import _rows

pytestmark = pytest.mark.integration


class ModelServer:
    """A stand-in model server on a real port. `kill` closes it; `start` reopens it."""

    def __init__(self, reply: str, port: int = 0) -> None:
        self.reply = reply
        self.port = port
        self.requests: list[dict[str, Any]] = []
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> ModelServer:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the http.server name
                length = int(self.headers.get("Content-Length", "0"))
                outer.requests.append(json.loads(self.rfile.read(length)))
                body = json.dumps({
                    "id": f"chatcmpl-{len(outer.requests)}",
                    "model": "served-model-v1",
                    "choices": [{"message": {"role": "assistant", "content": outer.reply},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 5},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self._server = HTTPServer(("127.0.0.1", self.port), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def kill(self) -> None:
        assert self._server is not None and self._thread is not None
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        self._server = self._thread = None


@pytest.fixture()
def servers(monkeypatch):
    # The adapter must reach these directly, whatever proxy the environment sets.
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "127.0.0.1,localhost")
    local = ModelServer("An answer from the model.").start()
    other = ModelServer("An answer from some other model.").start()
    try:
        yield local, other
    finally:
        for server in (local, other):
            if server._server is not None:
                server.kill()


def _brain(key: str, server: ModelServer) -> OpenAICompatibleBrain:
    return OpenAICompatibleBrain(key=key, base_url=f"http://127.0.0.1:{server.port}/v1",
                                 model="test-model", max_context=9024, timeout_seconds=5)


def test_a_killed_backend_fails_visibly_and_the_next_turn_succeeds_once_it_returns(
    db: Database, service_factory, servers
) -> None:
    local, other = servers
    config = make_config(db._dsn)
    config.providers["other"] = ProviderConfig(
        key="other", kind="fake", allowed_modes=("personal", "benchmark"), eval_only=False,
        context_budget=8000, reserved_output=1024, options={})
    config.brain_aliases["other"] = "other"
    service = service_factory(brain=_brain("local", local), config=config)
    service._registry._cache["brain.other"] = _brain("other", other)
    conversation = create_conversation(db, now=service._clock())

    # A session in progress.
    first = service.submit(conversation_id=conversation, text="Are you there?")
    assert (first.status, first.response) == ("completed", "An answer from the model.")

    # The backend dies.
    local.kill()
    second = service.submit(conversation_id=conversation, text="And now?")
    assert (second.status, second.error_kind, second.response) == (
        "failed", "brain_unavailable", None)
    attempts = _rows(db, "SELECT * FROM model_invocation WHERE turn_id = %s ORDER BY seq",
                     second.turn_id)
    # One retry against the same brain, as a second row. Nothing hidden.
    assert [(a["status"], a["error_kind"], a["provider_key"], a["error_detail"])
            for a in attempts] == [
        ("failed", "brain_unavailable", "fake", "provider_error_type=connection_failed")] * 2
    assert attempts[1]["retry_of_invocation_id"] == attempts[0]["id"]
    [turn] = _rows(db, "SELECT status, error_kind, response_message_id FROM turn WHERE id = %s",
                   second.turn_id)
    assert (turn["status"], turn["error_kind"], turn["response_message_id"]) == (
        "failed", "brain_unavailable", None)
    # No fallback: the other provider was reachable the whole time and never asked.
    assert other.requests == [] and len(local.requests) == 1
    # No fabricated content: Apollo said nothing for that message, and a note says why.
    said = [(m["role"], m["content"]) for m in transcript(db, conversation)]
    assert said == [
        ("user", "Are you there?"),
        ("apollo", "An answer from the model."),
        ("user", "And now?"),
        ("system_note", TRANSPORT_NOTES[TransportReason.CONNECTION]),
    ]
    events = [e["event_type"] for e in _rows(
        db, "SELECT event_type FROM audit_event WHERE turn_id = %s ORDER BY id",
        second.turn_id)]
    assert events.count("brain.unavailable") == 2 and events[-1] == "turn.failed"

    # The backend returns, on the same address.
    local.start()
    third = service.submit(conversation_id=conversation, text="Back?")
    assert (third.status, third.response) == ("completed", "An answer from the model.")
    assert other.requests == []
    # It was sent the conversation as it stands: the unanswered message is
    # there in Janu's voice, and the failure note is not.
    sent = json.dumps(local.requests[-1]["messages"])
    assert "And now?" in sent and "Are you there?" in sent
    assert TRANSPORT_NOTES[TransportReason.CONNECTION] not in sent
    assert [m["role"] for m in local.requests[-1]["messages"]] == [
        "system", "user", "assistant", "user"]

    # What each failed attempt would have sent can still be rebuilt.
    for found in replay.replay_turn(db, second.turn_id):
        assert (found.bundle_status, found.render_status) == (
            BundleStatus.VERIFIED, RenderStatus.NOT_ATTEMPTED)
    [replayed] = replay.replay_turn(db, third.turn_id)
    assert replayed.verified
