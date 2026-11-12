"""Provider failures are classified, and each class says what really happened.

Review fix 3 of 4. Every provider failure used to be a transport error: it was
retried and its transcript note said Apollo "could not reach its model", even
when the model was reached and answered 500 or 400. The table Janu approved:

| What happened                          | Kind                 | Retried |
|----------------------------------------|----------------------|---------|
| connection refused / reset             | brain_unavailable    | once    |
| timeout, HTTP 408                      | brain_unavailable    | once    |
| HTTP 502, 503, 504                     | brain_unavailable    | once    |
| HTTP 429                               | brain_unavailable    | once    |
| HTTP 500 and other 5xx                 | brain_failed         | no      |
| other 4xx                              | brain_rejected       | no      |
| context_length_exceeded (any status)   | context_overflow     | no      |
| unreadable body, no usable choice      | malformed_generation | no      |
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from apollo.brains.base import GenerationParams
from apollo.core.turns import (
    SYSTEM_NOTE_REJECTED_AUTH,
    SYSTEM_NOTES,
    TRANSPORT_NOTES,
    system_note_for_error,
)
from apollo.errors import (
    BrainRejectedError,
    BrainResponseError,
    BrainServiceError,
    BrainTransportError,
    ErrorKind,
    MalformedGenerationError,
    ProviderContextOverflowError,
    TransportReason,
    classify_http_status,
)
from apollo.sanitise import error_detail, error_kind
from tests.unit.test_openai_compatible import (
    FakeTransport,
    _Response,
    a_brain,
    a_bundle,
    patched,
)

SECRET = "sk-CLASSIFY-SECRET"


@pytest.mark.parametrize(
    ("status", "cls", "kind"),
    [
        (408, BrainTransportError, ErrorKind.BRAIN_UNAVAILABLE),
        (429, BrainTransportError, ErrorKind.BRAIN_UNAVAILABLE),
        (502, BrainTransportError, ErrorKind.BRAIN_UNAVAILABLE),
        (503, BrainTransportError, ErrorKind.BRAIN_UNAVAILABLE),
        (504, BrainTransportError, ErrorKind.BRAIN_UNAVAILABLE),
        (500, BrainServiceError, ErrorKind.BRAIN_FAILED),
        (501, BrainServiceError, ErrorKind.BRAIN_FAILED),
        (507, BrainServiceError, ErrorKind.BRAIN_FAILED),
        (400, BrainRejectedError, ErrorKind.BRAIN_REJECTED),
        (401, BrainRejectedError, ErrorKind.BRAIN_REJECTED),
        (403, BrainRejectedError, ErrorKind.BRAIN_REJECTED),
        (404, BrainRejectedError, ErrorKind.BRAIN_REJECTED),
        (422, BrainRejectedError, ErrorKind.BRAIN_REJECTED),
    ],
)
def test_an_http_status_maps_to_its_class(status, cls, kind) -> None:
    exc = classify_http_status(status)
    assert type(exc) is cls
    assert error_kind(exc) is kind
    assert exc.http_status == status


@pytest.mark.parametrize("status", [400, 413, 500, 503])
@pytest.mark.parametrize("field", ["provider_error_code", "provider_error_type"])
def test_context_length_exceeded_wins_over_the_status(status, field) -> None:
    exc = classify_http_status(status, **{field: "context_length_exceeded"})
    assert isinstance(exc, ProviderContextOverflowError)
    assert error_kind(exc) is ErrorKind.CONTEXT_OVERFLOW


def test_only_transport_failures_are_transport_errors() -> None:
    """The retry condition in core.turns is `isinstance(error, BrainTransportError)`."""
    assert not issubclass(BrainServiceError, BrainTransportError)
    assert not issubclass(BrainRejectedError, BrainTransportError)
    assert not issubclass(ProviderContextOverflowError, BrainTransportError)
    assert not issubclass(MalformedGenerationError, BrainTransportError)
    for cls in (BrainTransportError, BrainServiceError, BrainRejectedError,
                ProviderContextOverflowError, MalformedGenerationError):
        assert issubclass(cls, BrainResponseError)


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (None, TransportReason.CONNECTION),
        (408, TransportReason.TIMEOUT),
        (429, TransportReason.BUSY),
        (502, TransportReason.UNAVAILABLE),
        (503, TransportReason.UNAVAILABLE),
        (504, TransportReason.UNAVAILABLE),
    ],
)
def test_a_transport_reason_follows_the_status(status, reason) -> None:
    assert BrainTransportError(http_status=status).reason is reason


def test_an_explicit_reason_wins() -> None:
    exc = BrainTransportError(reason=TransportReason.TIMEOUT)
    assert exc.http_status is None and exc.reason is TransportReason.TIMEOUT


@pytest.mark.parametrize("status", [401, 403])
def test_a_credentials_refusal_is_marked(status) -> None:
    assert classify_http_status(status).is_auth  # type: ignore[attr-defined]


@pytest.mark.parametrize("status", [400, 404, 422])
def test_other_refusals_are_not_credentials(status) -> None:
    assert not classify_http_status(status).is_auth  # type: ignore[attr-defined]


def test_every_class_carries_only_whitelisted_scalars_into_the_detail() -> None:
    exc = classify_http_status(
        500, provider_error_code="server_error", provider_error_type="api_error"
    )
    assert error_detail(exc) == (
        "http_status=500 provider_error_code=server_error provider_error_type=api_error"
    )
    assert "BrainServiceError" in str(exc)  # the class name only, never provider text


# -- notes -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (BrainTransportError(reason=TransportReason.CONNECTION),
         "Apollo could not reach its model."),
        (BrainTransportError(reason=TransportReason.TIMEOUT),
         "Apollo's model timed out before answering."),
        (BrainTransportError(http_status=408), "Apollo's model timed out before answering."),
        (BrainTransportError(http_status=503),
         "Apollo's model service was temporarily unavailable."),
        (BrainTransportError(http_status=429), "Apollo's model service was too busy to answer."),
        (classify_http_status(500),
         "Apollo's model service failed while handling that message."),
        (classify_http_status(400), "Apollo's model service rejected the request."),
        (classify_http_status(401), "Apollo's model service refused its credentials."),
        (classify_http_status(403), "Apollo's model service refused its credentials."),
        (classify_http_status(400, provider_error_code="context_length_exceeded"),
         "This conversation no longer fits in Apollo's context window."),
        (MalformedGenerationError(provider_error_type="no_choices"),
         "Apollo's model returned a response Apollo could not read."),
    ],
    ids=["connection", "timeout", "408", "503", "429", "500", "400", "401", "403",
         "overflow", "malformed"],
)
def test_each_failure_class_gets_its_own_truthful_note(exc, expected) -> None:
    note = system_note_for_error(exc)
    assert note.startswith(expected)
    assert note.endswith("No response was generated for that message.")


def test_only_a_connection_failure_says_the_model_could_not_be_reached() -> None:
    for reason, note in TRANSPORT_NOTES.items():
        assert ("could not reach its model" in note) == (reason is TransportReason.CONNECTION)
    assert "could not reach" not in SYSTEM_NOTE_REJECTED_AUTH
    for kind in (ErrorKind.BRAIN_FAILED, ErrorKind.BRAIN_REJECTED,
                 ErrorKind.MALFORMED_GENERATION):
        assert "could not reach" not in SYSTEM_NOTES[kind]


def test_every_transport_reason_has_a_note() -> None:
    assert set(TRANSPORT_NOTES) == set(TransportReason)


# -- the adapter ---------------------------------------------------------------


def _http_error(code: int, body: dict | None = None) -> urllib.error.HTTPError:
    payload = json.dumps(body or {"error": {"message": f"boom {SECRET}"}}).encode()
    return urllib.error.HTTPError(
        url=f"https://example.invalid/v1?key={SECRET}", code=code, msg="err",
        hdrs=None, fp=io.BytesIO(payload),
    )


def _generate(monkeypatch, transport) -> None:
    brain = a_brain()
    patched(monkeypatch, transport)
    bundle = a_bundle()
    brain.generate(brain.render(bundle), GenerationParams())


@pytest.mark.parametrize(
    ("code", "cls"),
    [(500, BrainServiceError), (400, BrainRejectedError), (401, BrainRejectedError),
     (503, BrainTransportError), (429, BrainTransportError), (408, BrainTransportError)],
)
def test_the_adapter_classifies_http_failures(monkeypatch, code, cls) -> None:
    with pytest.raises(BrainResponseError) as caught:
        _generate(monkeypatch, FakeTransport(error=_http_error(code)))
    assert type(caught.value) is cls
    assert SECRET not in repr(caught.value) + str(caught.value) + repr(caught.value.args)


@pytest.mark.parametrize(
    "error",
    [TimeoutError("timed out reading https://x/?key=" + SECRET),
     urllib.error.URLError(TimeoutError("timed out connecting"))],
    ids=["read-timeout", "connect-timeout"],
)
def test_the_adapter_reports_a_timeout_as_a_timeout(monkeypatch, error) -> None:
    with pytest.raises(BrainTransportError) as caught:
        _generate(monkeypatch, FakeTransport(error=error))
    assert caught.value.reason is TransportReason.TIMEOUT
    assert caught.value.provider_error_type == "timeout"
    assert SECRET not in repr(caught.value) + str(caught.value)


@pytest.mark.parametrize(
    "error",
    [ConnectionRefusedError("refused"), urllib.error.URLError("Name or service not known")],
    ids=["refused", "dns"],
)
def test_the_adapter_reports_other_connection_failures_as_connection(monkeypatch, error) -> None:
    with pytest.raises(BrainTransportError) as caught:
        _generate(monkeypatch, FakeTransport(error=error))
    assert caught.value.reason is TransportReason.CONNECTION
    assert caught.value.provider_error_type == "connection_failed"


@pytest.mark.parametrize("raw", [b"[1, 2]", b'"text"', b"null", b"\xff\xfe"],
                         ids=["list", "string", "null", "not-utf8"])
def test_a_body_that_is_not_a_json_object_is_malformed(monkeypatch, raw) -> None:
    class Raw(FakeTransport):
        def __call__(self, request, timeout=None):  # noqa: ANN001
            self.requests.append(request)
            return _Response(raw)

    with pytest.raises(MalformedGenerationError) as caught:
        _generate(monkeypatch, Raw())
    assert caught.value.provider_error_type == "malformed_response"
    assert error_kind(caught.value) is ErrorKind.MALFORMED_GENERATION
