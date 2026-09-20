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


class IdentityOverflowError(ApolloError):
    kind = ErrorKind.IDENTITY_OVERFLOW


class ContextOverflowError(ApolloError):
    kind = ErrorKind.CONTEXT_OVERFLOW


class PolicyRefusedError(ApolloError):
    """Raised by core.policy before retrieval or generation. Never a silent downgrade."""

    def __init__(self, kind: ErrorKind, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


class BrainTransportError(ApolloError):
    """A transport-level provider failure. The only class eligible for retry (spec L)."""

    kind = ErrorKind.BRAIN_UNAVAILABLE

    def __init__(
        self,
        *,
        http_status: int | None = None,
        provider_error_code: str | None = None,
        provider_error_type: str | None = None,
    ) -> None:
        # Deliberately no message argument: str(exception) from an HTTP client
        # routinely embeds the request body or a URL with query parameters (H.5).
        super().__init__("brain transport failure")
        self.http_status = http_status
        self.provider_error_code = provider_error_code
        self.provider_error_type = provider_error_type


class EmptyGenerationError(ApolloError):
    kind = ErrorKind.EMPTY_GENERATION


class InvocationContractError(ApolloError):
    """Raised when something tries to call a Brain outside the recorded path (ADR-0013)."""
