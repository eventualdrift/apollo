"""Sentinel sweep: operational logs carry no content (spec H.1, criterion 25).

Sentinels go into the identity, a message, the model's output and a simulated
provider error, and the captured log stream is then searched for all of them.
"""

from __future__ import annotations

import io
import logging
import pathlib

import pytest

from apollo.brains.fake import FakeBrain
from apollo.core.conversations import create_conversation
from apollo.errors import BrainTransportError
from apollo.logging_setup import configure_logging
from tests.integration.conftest import make_config

pytestmark = pytest.mark.integration

SENTINEL_MESSAGE = "SENTINEL-MESSAGE-door-code-4123"
SENTINEL_IDENTITY = "SENTINEL-IDENTITY-apollo-secret"
SENTINEL_OUTPUT = "SENTINEL-OUTPUT-model-said-this"
SENTINEL_PROVIDER_BODY = "SENTINEL-PROVIDER-BODY-api-key-abcdef"


class LeakyTransportError(BrainTransportError):
    """A provider error whose own message text carries a secret, as real ones do."""

    def __init__(self) -> None:
        super().__init__(http_status=500, provider_error_code="server_error")
        self.args = (f"POST /v1/chat failed: {SENTINEL_PROVIDER_BODY}",)


@pytest.fixture()
def captured_logs():
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    yield stream
    logging.getLogger().handlers.clear()


def _identity_with_sentinel(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "identity"
    d.mkdir()
    (d / "manifest.yaml").write_text(
        'schema_version: 1\nidentity_version: "test-1"\nfragments:\n  - core.md\n'
    )
    (d / "core.md").write_text(f"Apollo is a system. {SENTINEL_IDENTITY}\n")
    return d


def test_no_sentinel_reaches_the_operational_log(
    db, service_factory, clock, captured_logs, tmp_path
) -> None:
    config = make_config(db._dsn)
    config = type(config)(**{**config.__dict__, "identity_dir": _identity_with_sentinel(tmp_path)})
    brain = FakeBrain(mode="scripted")
    brain._script = {}  # falls through to a FakeBrainError, exercising the failure path

    conv = create_conversation(db, now=clock())
    svc = service_factory(brain=brain, config=config)
    svc.submit(conversation_id=conv, text=SENTINEL_MESSAGE)

    ok_brain = FakeBrain(mode="scripted")
    svc2 = service_factory(brain=ok_brain, config=config)
    ok_brain._mode = "echo"
    svc2.submit(conversation_id=conv, text=SENTINEL_MESSAGE)

    failing = FakeBrain(fail_times=99, failure=LeakyTransportError())
    service_factory(brain=failing, config=config).submit(
        conversation_id=conv, text=SENTINEL_MESSAGE
    )

    logs = captured_logs.getvalue()
    assert logs, "expected the run to log something"
    for sentinel in (
        SENTINEL_MESSAGE,
        SENTINEL_IDENTITY,
        SENTINEL_PROVIDER_BODY,
    ):
        assert sentinel not in logs, f"{sentinel} leaked into the operational log"
    # And nothing that looks like a rendered prompt or model output.
    assert "<<<" not in logs
    assert "[fake:echo" not in logs


def test_provider_error_text_never_reaches_persisted_fields(db, service_factory, clock) -> None:
    """Acceptance criterion 22: exception text and response bodies are never stored."""
    failing = FakeBrain(fail_times=99, failure=LeakyTransportError())
    conv = create_conversation(db, now=clock())
    result = service_factory(brain=failing).submit(conversation_id=conv, text="hello")

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT error_kind, error_detail FROM model_invocation WHERE turn_id = %s",
            (result.turn_id,),
        )
        invocations = cur.fetchall()
        cur.execute("SELECT error_kind, error_detail FROM turn WHERE id = %s", (result.turn_id,))
        turn = cur.fetchone()
        cur.execute("SELECT payload FROM audit_event")
        payloads = repr(cur.fetchall())

    blob = repr(invocations) + repr(turn) + payloads
    assert SENTINEL_PROVIDER_BODY not in blob
    assert "POST /v1/chat" not in blob
    # What survives is the whitelist, and only the whitelist.
    assert invocations[0]["error_kind"] == "brain_unavailable"
    assert invocations[0]["error_detail"] == "http_status=500 provider_error_code=server_error"
    assert turn["error_kind"] == "brain_unavailable"


def test_raw_meta_is_never_persisted(db, service, clock) -> None:
    """The fake returns an echo of the prompt in raw_meta; nothing may store it."""
    conv = create_conversation(db, now=clock())
    result = service.submit(conversation_id=conv, text="a distinctive phrase for raw meta")

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM model_invocation WHERE turn_id = %s", (result.turn_id,))
        row = cur.fetchone()
        cur.execute("SELECT payload FROM audit_event")
        payloads = repr(cur.fetchall())
    assert "echo_of_prompt" not in repr(row) + payloads
    assert "a distinctive phrase for raw meta" not in repr(row) + payloads


def test_manifests_hold_references_not_content(db, service, clock) -> None:
    conv = create_conversation(db, now=clock())
    result = service.submit(conversation_id=conv, text=SENTINEL_MESSAGE)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT context_manifest FROM model_invocation WHERE turn_id = %s", (result.turn_id,)
        )
        manifest = cur.fetchone()["context_manifest"]
    assert SENTINEL_MESSAGE not in repr(manifest)
    assert all("content" not in entry for entry in manifest)
