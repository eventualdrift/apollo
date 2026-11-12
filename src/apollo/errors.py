"""Apollo error kinds and the exception hierarchy.

`error_kind` is a closed enum of Apollo-defined values and is never provider
text (spec H.5). Exceptions are mapped from their *type* to a kind; their
message text is never recorded.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorKind(StrEnum):
    """Closed enum. Persisted in `turn.error_kind` / `model_invocation.error_kind`."""

    BRAIN_UNAVAILABLE = "brain_unavailable"
    BRAIN_FAILED = "brain_failed"
    BRAIN_REJECTED = "brain_rejected"
    MALFORMED_GENERATION = "malformed_generation"
    BRAIN_MODE_NOT_PERMITTED = "brain_mode_not_permitted"
    BRAIN_NOT_INTERACTIVE = "brain_not_interactive"
    EMPTY_GENERATION = "empty_generation"
    CONTEXT_OVERFLOW = "context_overflow"
    IDENTITY_OVERFLOW = "identity_overflow"
    INTERRUPTED = "interrupted"
    RETRIEVAL_FAILED = "retrieval_failed"
    INTERNAL = "internal"


class ApolloError(Exception):
    """Base class. Carries a kind; never carries provider text."""

    kind: ErrorKind = ErrorKind.INTERNAL


class ConfigError(ApolloError):
    pass


class MigrationChecksumError(ApolloError):
    """An applied migration no longer matches its file. Nothing is applied.

    A migration is recorded by name and digest when applied. Editing it
    afterwards would leave the database on a schema the code no longer
    describes, so the runner refuses rather than ignore the edit.
    """


class IdentityOverflowError(ApolloError):
    kind = ErrorKind.IDENTITY_OVERFLOW


class ContextOverflowError(ApolloError):
    kind = ErrorKind.CONTEXT_OVERFLOW


class HistoryRoleError(ApolloError):
    """A history item carried a role that may never become conversation history.

    `system_note` rows are durable, human-visible transcript entries and are
    never model context (spec F.2). The repository filters them out; this is
    the second layer, for callers that build context without going through that
    query — the eval runner, replay, and whatever Core grows next.
    """


class PolicyRefusedError(ApolloError):
    """Raised by core.policy before retrieval or generation. Never a silent downgrade."""

    def __init__(self, kind: ErrorKind, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


class BrainResponseError(ApolloError):
    """A provider call that failed, carrying only whitelisted scalars (spec H.5).

    The subclass says which failure class it was, and so which note the
    transcript gets and whether the call is retried (spec L).
    """

    def __init__(
        self,
        *,
        http_status: int | None = None,
        provider_error_code: str | None = None,
        provider_error_type: str | None = None,
    ) -> None:
        # Deliberately no message argument: str(exception) from an HTTP client
        # routinely embeds the request body or a URL with query parameters (H.5).
        super().__init__(f"brain failure: {type(self).__name__}")
        self.http_status = http_status
        self.provider_error_code = provider_error_code
        self.provider_error_type = provider_error_type


class TransportReason(StrEnum):
    """Why a transport failure happened. Each has its own truthful note."""

    CONNECTION = "connection"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    BUSY = "busy"


#: HTTP statuses that mean "try again shortly": the request never reached a
#: model that could answer it. The only statuses retried (spec L).
TRANSPORT_STATUSES = frozenset({408, 429, 502, 503, 504})


class BrainTransportError(BrainResponseError):
    """A transport-level provider failure. The only class eligible for retry (spec L).

    Connection failures and timeouts, and HTTP 408, 429, 502, 503 and 504.
    """

    kind = ErrorKind.BRAIN_UNAVAILABLE

    def __init__(
        self,
        *,
        http_status: int | None = None,
        provider_error_code: str | None = None,
        provider_error_type: str | None = None,
        reason: TransportReason | None = None,
    ) -> None:
        super().__init__(
            http_status=http_status,
            provider_error_code=provider_error_code,
            provider_error_type=provider_error_type,
        )
        self.reason = reason if reason is not None else _transport_reason(http_status)


def _transport_reason(http_status: int | None) -> TransportReason:
    if http_status is None:
        return TransportReason.CONNECTION
    if http_status == 408:
        return TransportReason.TIMEOUT
    if http_status == 429:
        return TransportReason.BUSY
    return TransportReason.UNAVAILABLE


class BrainServiceError(BrainResponseError):
    """The provider failed while handling the request (HTTP 500 and other 5xx).

    Not retried: the request reached the service and it failed on it, so a
    second identical attempt is not a transport retry.
    """

    kind = ErrorKind.BRAIN_FAILED


class BrainRejectedError(BrainResponseError):
    """The provider refused the request (4xx other than 408 and 429). Not retried."""

    kind = ErrorKind.BRAIN_REJECTED

    @property
    def is_auth(self) -> bool:
        return self.http_status in (401, 403)


class ProviderContextOverflowError(BrainResponseError):
    """The provider said the request does not fit its context window. Not retried."""

    kind = ErrorKind.CONTEXT_OVERFLOW


class MalformedGenerationError(BrainResponseError):
    """The provider answered with something that is not a usable response. Not retried."""

    kind = ErrorKind.MALFORMED_GENERATION


CONTEXT_OVERFLOW_MARKERS = frozenset({"context_length_exceeded"})


def classify_http_status(
    http_status: int,
    *,
    provider_error_code: str | None = None,
    provider_error_type: str | None = None,
) -> BrainResponseError:
    """Map an HTTP failure to its class. Only the status and two scalars are read."""
    cls: type[BrainResponseError]
    if {provider_error_code, provider_error_type} & CONTEXT_OVERFLOW_MARKERS:
        cls = ProviderContextOverflowError
    elif http_status in TRANSPORT_STATUSES:
        cls = BrainTransportError
    elif http_status >= 500:
        cls = BrainServiceError
    else:
        cls = BrainRejectedError
    return cls(
        http_status=http_status,
        provider_error_code=provider_error_code,
        provider_error_type=provider_error_type,
    )


class FinalisationConflictError(ApolloError):
    """A turn or invocation was no longer `started` when its finalisation reached it.

    Orphan recovery (spec A.2) marks a turn and its invocations `interrupted`
    once they outlive the orphan window, including ones whose model call is
    still running in another process. When that call returns, its result must
    not be written over the recorded state: the late finalisation's whole
    transaction rolls back, so no message and no audit event claim an outcome
    the rows do not show.
    """

    kind = ErrorKind.INTERRUPTED

    def __init__(self, *, table: str, row_id: object) -> None:
        super().__init__(f"{table} {row_id} was no longer started at finalisation")
        self.table = table
        self.row_id = row_id


class EmptyGenerationError(ApolloError):
    kind = ErrorKind.EMPTY_GENERATION


class GenerationContractError(ApolloError):
    """A Brain returned a value that does not satisfy the Generation contract."""


class InvocationContractError(ApolloError):
    """Raised when something tries to call a Brain outside the recorded path (ADR-0013)."""
