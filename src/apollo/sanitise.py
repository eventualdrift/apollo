"""The H.5 sanitisation whitelist.

Error paths are the most common accidental privacy leak, because HTTP client
exception messages routinely embed the request body, the URL with query
parameters, or the provider's echo of the prompt. Nothing here ever reads
`str(exception)` or a response body.
"""

from __future__ import annotations

from typing import Any

from apollo.errors import ApolloError, BrainTransportError, ErrorKind

ERROR_DETAIL_MAX = 500

#: The only keys Core copies out of an adapter's in-memory `raw_meta`.
RAW_META_WHITELIST = frozenset(
    {
        "model_identifier",
        "finish_reason",
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "provider_request_id",
    }
)


def whitelist_meta(raw_meta: dict[str, Any] | None) -> dict[str, Any]:
    """Copy whitelisted scalars out of `raw_meta` and discard everything else.

    `raw_meta` itself is never persisted (spec H.5). Non-scalar values are
    dropped even when their key is whitelisted, because a nested structure can
    carry echoed prompt content.
    """
    if not raw_meta:
        return {}
    out: dict[str, Any] = {}
    for key in sorted(RAW_META_WHITELIST & raw_meta.keys()):
        value = raw_meta[key]
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int | float) or (isinstance(value, str) and len(value) <= 200):
            out[key] = value
    return out


def error_detail(exc: BaseException) -> str | None:
    """Assemble `error_detail` from a whitelist — never from exception text.

    Returns None when there is nothing whitelisted to say, which is the common
    case and is preferable to recording a message we have not inspected.
    """
    if isinstance(exc, BrainTransportError):
        parts: list[str] = []
        if exc.http_status is not None:
            parts.append(f"http_status={int(exc.http_status)}")
        if exc.provider_error_code:
            parts.append(f"provider_error_code={_scalar(exc.provider_error_code)}")
        if exc.provider_error_type:
            parts.append(f"provider_error_type={_scalar(exc.provider_error_type)}")
        return " ".join(parts)[:ERROR_DETAIL_MAX] if parts else None
    # For every other exception the only safe thing we know is its type.
    return f"exception_type={type(exc).__name__}"[:ERROR_DETAIL_MAX]


def _scalar(value: str) -> str:
    """Short, single-token-ish provider scalars only."""
    cleaned = "".join(ch for ch in value if ch.isalnum() or ch in "._-")
    return cleaned[:64]


def error_kind(exc: BaseException) -> ErrorKind:
    """Map an exception *type* to an Apollo error kind."""
    if isinstance(exc, ApolloError):
        return exc.kind
    return ErrorKind.INTERNAL
