"""The real adapter through the real turn path (Step 8).

Transport is faked at the `urlopen` boundary and nothing above it is: the
compiler, the region contract, the invocation recording, the retry policy and
the empty-generation classification are all the shipping code. This is where
M1's guarantees are re-proved against a real adapter rather than `brain.fake`.
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any

import pytest

from apollo.brains.openai_compatible import OpenAICompatibleBrain
from apollo.core.conversations import create_conversation, transcript
from apollo.core.turns import SYSTEM_NOTE_REJECTED_AUTH, SYSTEM_NOTES, TRANSPORT_NOTES
from apollo.errors import ErrorKind, TransportReason

pytestmark = pytest.mark.integration

SECRET = "sk-INTEGRATION-SECRET"
REASONING = "REASONING-SENTINEL internal deliberation"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Transport:
    """Scripted responses, one per call, with the requests recorded."""

    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.requests: list[Any] = []

    def __call__(self, request, timeout=None):  # noqa: ANN001
        self.requests.append(request)
        item = self._responses.pop(0) if self._responses else self._responses_exhausted()
        if isinstance(item, Exception):
            raise item
        return _Response(json.dumps(item).encode())

    def _responses_exhausted(self):
        raise AssertionError("the adapter made more calls than the script allows")


def body(text="an answer", **extra) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": text}
    message.update(extra.pop("message", {}))
    return {
        "id": "chatcmpl-int-1",
        "model": "served-model-v1",
        "choices": [{"message": message, "finish_reason": extra.pop("finish_reason", "stop")}],
        "usage": extra.pop("usage", {"prompt_tokens": 120, "completion_tokens": 9}),
    }


def http_error(code: int) -> urllib.error.HTTPError:
    payload = json.dumps(
        {"error": {"message": f"boom {SECRET}", "code": "overloaded",
                   "type": "server_error"}}
    ).encode()
    return urllib.error.HTTPError(
        url=f"https://example.invalid/v1?key={SECRET}", code=code, msg="err",
        hdrs=None, fp=io.BytesIO(payload),
    )


@pytest.fixture()
def adapter_service(db, service_factory, monkeypatch):
    def build(*responses):
        transport = Transport(*responses)
        monkeypatch.setattr(
            "apollo.brains.openai_compatible.urllib.request.urlopen", transport
        )
        brain = OpenAICompatibleBrain(
            key="local", base_url="http://127.0.0.1:8080/v1", model="test-model",
            max_context=9024, api_key=SECRET,
        )
        return service_factory(brain=brain), transport

    return build


def test_an_ordinary_turn_through_the_real_adapter(db, adapter_service, clock) -> None:
    service, transport = adapter_service(body())
    conversation_id = create_conversation(db, now=clock())
    result = service.submit(conversation_id=conversation_id, text="What is 2+2?")

    assert result.status == "completed"
    assert result.response == "an answer"

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM model_invocation WHERE turn_id = %s", (result.turn_id,)
        )
        invocation = cur.fetchone()
    assert invocation["adapter_key"] == "openai_compatible"
    assert invocation["render_version"] == "chat-v3"
    assert invocation["model_identifier"] == "served-model-v1"
    assert invocation["prompt_tokens"] == 120
    assert invocation["completion_tokens"] == 9
    assert invocation["finish_reason"] == "stop"
    assert invocation["status"] == "completed"
    assert invocation["rendered_prompt_hash"]

    # The request actually sent was a legal four-region render.
    payload = json.loads(transport.requests[-1].data)
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][-1]["role"] == "user"
    assert payload["messages"][-1]["content"].endswith("What is 2+2?")


@pytest.mark.parametrize("text", ["", "   \n\t "], ids=["empty", "whitespace"])
def test_empty_generation_semantics_hold_with_the_real_adapter(
    db, adapter_service, clock, text
) -> None:
    """M1's empty-generation contract, re-proved against a real provider shape."""
    service, transport = adapter_service(body(text=text))
    conversation_id = create_conversation(db, now=clock())
    result = service.submit(conversation_id=conversation_id, text="say something")

    assert result.status == "failed"
    assert result.error_kind == ErrorKind.EMPTY_GENERATION
    assert len(transport.requests) == 1, "content-level failure must not retry"

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT status, error_kind FROM turn WHERE id = %s", (result.turn_id,))
        turn = cur.fetchone()
        cur.execute(
            "SELECT count(*) AS n FROM model_invocation WHERE turn_id = %s", (result.turn_id,)
        )
        invocations = cur.fetchone()["n"]
        cur.execute(
            "SELECT count(*) AS n FROM message WHERE conversation_id = %s AND role = 'apollo'",
            (conversation_id,),
        )
        replies = cur.fetchone()["n"]
    assert turn["status"] == "failed" and turn["error_kind"] == ErrorKind.EMPTY_GENERATION
    assert invocations == 1 and replies == 0

    notes = [m["content"] for m in transcript(db, conversation_id)
             if m["role"] == "system_note"]
    assert notes == [SYSTEM_NOTES[ErrorKind.EMPTY_GENERATION]]


def test_a_transport_failure_retries_once_against_the_same_brain(
    db, adapter_service, clock
) -> None:
    service, transport = adapter_service(http_error(503), body())
    conversation_id = create_conversation(db, now=clock())
    result = service.submit(conversation_id=conversation_id, text="hello")

    assert result.status == "completed"
    assert len(transport.requests) == 2

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, status, error_kind, error_detail, retry_of_invocation_id"
            " FROM model_invocation WHERE turn_id = %s ORDER BY seq",
            (result.turn_id,),
        )
        rows = cur.fetchall()
    assert [r["status"] for r in rows] == ["failed", "completed"]
    assert rows[0]["error_kind"] == ErrorKind.BRAIN_UNAVAILABLE
    assert rows[1]["retry_of_invocation_id"] == rows[0]["id"]
    # The persisted detail is the whitelist, and nothing else.
    assert rows[0]["error_detail"] == (
        "http_status=503 provider_error_code=overloaded provider_error_type=server_error"
    )
    assert SECRET not in repr(rows)


def test_two_transport_failures_fail_the_turn_without_fallback(
    db, adapter_service, clock
) -> None:
    service, transport = adapter_service(http_error(502), http_error(502))
    conversation_id = create_conversation(db, now=clock())
    result = service.submit(conversation_id=conversation_id, text="hello")

    assert result.status == "failed"
    assert result.error_kind == ErrorKind.BRAIN_UNAVAILABLE
    assert len(transport.requests) == 2, "one retry, never a third attempt"
    notes = [m["content"] for m in transcript(db, conversation_id)
             if m["role"] == "system_note"]
    assert notes == [TRANSPORT_NOTES[TransportReason.UNAVAILABLE]]


def test_a_reasoning_channel_never_reaches_the_database(db, adapter_service, clock) -> None:
    service, _ = adapter_service(
        body(message={"reasoning_content": REASONING},
             usage={"prompt_tokens": 10, "completion_tokens": 2,
                    "completion_tokens_details": {"reasoning_tokens": 321}})
    )
    conversation_id = create_conversation(db, now=clock())
    result = service.submit(conversation_id=conversation_id, text="think about it")
    assert result.status == "completed"

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT reasoning_tokens FROM model_invocation WHERE turn_id = %s",
            (result.turn_id,),
        )
        assert cur.fetchone()["reasoning_tokens"] == 321
        for table in ("model_invocation", "message", "audit_event"):
            cur.execute(f"SELECT count(*) AS n FROM {table}")  # noqa: S608 - fixed names
            assert cur.fetchone()["n"] >= 0
        cur.execute(
            "SELECT count(*) AS n FROM message WHERE content LIKE %s", (f"%{REASONING}%",)
        )
        assert cur.fetchone()["n"] == 0
        cur.execute(
            "SELECT count(*) AS n FROM model_invocation"
            " WHERE coalesce(error_detail,'') || context_manifest::text LIKE %s",
            (f"%{REASONING}%",),
        )
        assert cur.fetchone()["n"] == 0
        cur.execute(
            "SELECT count(*) AS n FROM audit_event WHERE payload::text LIKE %s",
            (f"%{REASONING}%",),
        )
        assert cur.fetchone()["n"] == 0


def overflow_error() -> urllib.error.HTTPError:
    payload = json.dumps(
        {"error": {"message": f"too long {SECRET}", "code": "context_length_exceeded",
                   "type": "invalid_request_error"}}
    ).encode()
    return urllib.error.HTTPError(
        url="https://example.invalid/v1", code=400, msg="err", hdrs=None,
        fp=io.BytesIO(payload),
    )


#: Review fix 3: every provider failure class, the kind it records, the note
#: Janu reads, and whether it is retried. Factories, so each run gets a fresh
#: exception with an unread body.
FAILURE_CLASSES = [
    ("connection", lambda: urllib.error.URLError(f"refused {SECRET}"),
     ErrorKind.BRAIN_UNAVAILABLE, TRANSPORT_NOTES[TransportReason.CONNECTION], True),
    ("timeout", lambda: TimeoutError(f"timed out {SECRET}"),
     ErrorKind.BRAIN_UNAVAILABLE, TRANSPORT_NOTES[TransportReason.TIMEOUT], True),
    ("http-408", lambda: http_error(408),
     ErrorKind.BRAIN_UNAVAILABLE, TRANSPORT_NOTES[TransportReason.TIMEOUT], True),
    ("http-429", lambda: http_error(429),
     ErrorKind.BRAIN_UNAVAILABLE, TRANSPORT_NOTES[TransportReason.BUSY], True),
    ("http-502", lambda: http_error(502),
     ErrorKind.BRAIN_UNAVAILABLE, TRANSPORT_NOTES[TransportReason.UNAVAILABLE], True),
    ("http-503", lambda: http_error(503),
     ErrorKind.BRAIN_UNAVAILABLE, TRANSPORT_NOTES[TransportReason.UNAVAILABLE], True),
    ("http-504", lambda: http_error(504),
     ErrorKind.BRAIN_UNAVAILABLE, TRANSPORT_NOTES[TransportReason.UNAVAILABLE], True),
    ("http-500", lambda: http_error(500),
     ErrorKind.BRAIN_FAILED, SYSTEM_NOTES[ErrorKind.BRAIN_FAILED], False),
    ("http-400", lambda: http_error(400),
     ErrorKind.BRAIN_REJECTED, SYSTEM_NOTES[ErrorKind.BRAIN_REJECTED], False),
    ("http-401", lambda: http_error(401),
     ErrorKind.BRAIN_REJECTED, SYSTEM_NOTE_REJECTED_AUTH, False),
    ("context-overflow", overflow_error,
     ErrorKind.CONTEXT_OVERFLOW, SYSTEM_NOTES[ErrorKind.CONTEXT_OVERFLOW], False),
    ("no-choices", lambda: {"choices": []},
     ErrorKind.MALFORMED_GENERATION, SYSTEM_NOTES[ErrorKind.MALFORMED_GENERATION], False),
]


@pytest.mark.parametrize(
    ("make_failure", "kind", "note", "retried"),
    [case[1:] for case in FAILURE_CLASSES],
    ids=[case[0] for case in FAILURE_CLASSES],
)
def test_each_failure_class_records_its_kind_note_and_retry(
    db, adapter_service, clock, make_failure, kind, note, retried
) -> None:
    service, transport = adapter_service(make_failure(), make_failure())
    conversation_id = create_conversation(db, now=clock())
    result = service.submit(conversation_id=conversation_id, text="hello")

    assert result.status == "failed"
    assert result.error_kind == kind
    assert len(transport.requests) == (2 if retried else 1)

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT error_kind, error_detail FROM model_invocation WHERE turn_id = %s"
            " ORDER BY seq",
            (result.turn_id,),
        )
        invocations = cur.fetchall()
    assert [r["error_kind"] for r in invocations] == [kind] * (2 if retried else 1)
    assert SECRET not in repr(invocations)

    notes = [m["content"] for m in transcript(db, conversation_id)
             if m["role"] == "system_note"]
    assert notes == [note]


@pytest.mark.parametrize(
    ("make_failure", "retried"),
    [(case[1], case[4]) for case in FAILURE_CLASSES],
    ids=[case[0] for case in FAILURE_CLASSES],
)
def test_only_a_transport_failure_recovers_on_the_retry(
    db, adapter_service, clock, make_failure, retried
) -> None:
    service, transport = adapter_service(make_failure(), body())
    conversation_id = create_conversation(db, now=clock())
    result = service.submit(conversation_id=conversation_id, text="hello")

    if retried:
        assert result.status == "completed"
        assert len(transport.requests) == 2
    else:
        assert result.status == "failed"
        assert len(transport.requests) == 1, "a non-transport failure is never retried"


def test_no_secret_reaches_the_database(db, adapter_service, clock) -> None:
    service, _ = adapter_service(http_error(500), http_error(500))
    conversation_id = create_conversation(db, now=clock())
    service.submit(conversation_id=conversation_id, text="hello")

    with db.connect() as conn, conn.cursor() as cur:
        for table, column in (("model_invocation", "error_detail"),
                              ("message", "content")):
            cur.execute(
                f"SELECT count(*) AS n FROM {table} WHERE coalesce({column},'') LIKE %s",  # noqa: S608
                (f"%{SECRET}%",),
            )
            assert cur.fetchone()["n"] == 0, f"{table}.{column} leaked the key"
        cur.execute(
            "SELECT count(*) AS n FROM audit_event WHERE payload::text LIKE %s",
            (f"%{SECRET}%",),
        )
        assert cur.fetchone()["n"] == 0
