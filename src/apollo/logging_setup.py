"""Structured JSON logging with redaction as a mechanism, not a guideline.

Spec H.1: operational logs carry ids, hashes, timings, counts, Apollo error
kinds and exception *types*. They never carry message content, memory content,
proposal content, excerpts, rendered prompts, model output, provider response
bodies, or exception message text.

A rule this easy to break by accident needs a mechanism. Two of them:

1. `ApolloLogRecordFilter` drops `exc_info` and any extra field whose key is not
   on the allowlist, so a caller cannot leak content by passing it as an extra.
2. `JsonFormatter` serialises only allowlisted fields, so even a log record
   assembled elsewhere cannot smuggle content through.

The formatter never renders `record.getMessage()` arguments either: messages are
short static strings and everything variable travels as an allowlisted extra.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from typing import Any

#: Extra fields a log record may carry. Ids, hashes, counts, durations, enums.
#: Nothing here can hold free text that originated from a user, a model or a
#: provider. Adding a key to this set is a privacy decision, not a convenience.
ALLOWED_EXTRA_KEYS = frozenset(
    {
        "adapter_key",
        "attempt",
        "block_count",
        "block_type",
        "brain_alias",
        "bundle_hash",
        "compiler_version",
        "conversation_id",
        "count",
        "duration_ms",
        "dropped",
        "error_kind",
        "event_type",
        "exception_type",
        "finish_reason",
        "identity_hash",
        "identity_version",
        "invocation_id",
        "latency_ms",
        "message_id",
        "migration",
        "mode",
        "provider_key",
        "purpose",
        "region",
        "render_version",
        "retry_of",
        "seq",
        "status",
        "token_estimate",
        "token_estimator",
        "trust_tier",
        "turn_id",
    }
)

_STANDARD = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class ApolloLogRecordFilter(logging.Filter):
    """Strip exception text and non-allowlisted extras before formatting."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            exc_type = record.exc_info[0]
            record.exception_type = exc_type.__name__ if exc_type else "unknown"
            # The traceback is kept without locals; the message text is dropped.
            record.traceback_frames = _frames(record.exc_info[2])
            record.exc_info = None
        record.exc_text = None
        for key in list(record.__dict__):
            if key in _STANDARD or key in ALLOWED_EXTRA_KEYS:
                continue
            if key in {"exception_type", "traceback_frames"}:
                continue
            del record.__dict__[key]
        return True


def _frames(tb: Any) -> list[str]:
    """`file:line:function` only. No source lines, no locals, no values."""
    return [f"{f.filename}:{f.lineno}:{f.name}" for f in traceback.extract_tb(tb)][-12:]


class JsonFormatter(logging.Formatter):
    """Serialise only allowlisted fields. The record's message is a static string."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "event": str(record.msg),
        }
        for key in sorted(ALLOWED_EXTRA_KEYS | {"exception_type", "traceback_frames"}):
            if key in record.__dict__:
                payload[key] = record.__dict__[key]
        return json.dumps(payload, default=str, sort_keys=False)


def configure_logging(level: str = "INFO", stream: Any | None = None) -> None:
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ApolloLogRecordFilter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
