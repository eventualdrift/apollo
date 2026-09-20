"""Audit event types and payload discipline.

Spec C.10: the payload never contains message bodies, memory content, excerpts,
rendered prompts, provider response bodies or exception text. Ids, hashes,
enums, counts and durations only.

That rule is enforced here by construction: `AuditEvent.payload` is validated
against a value whitelist before it can be written, so a caller cannot leak
content by passing it in a payload.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from apollo.errors import ApolloError


class EventType(StrEnum):
    CONVERSATION_CREATED = "conversation.created"
    CONVERSATION_ARCHIVED = "conversation.archived"
    MESSAGE_CREATED = "message.created"
    TURN_STARTED = "turn.started"
    TURN_COMPLETED = "turn.completed"
    TURN_FAILED = "turn.failed"
    INVOCATION_STARTED = "invocation.started"
    INVOCATION_COMPLETED = "invocation.completed"
    INVOCATION_FAILED = "invocation.failed"
    MEMORY_PROPOSED = "memory.proposed"
    MEMORY_PROPOSAL_RESOLVED = "memory.proposal_resolved"
    MEMORY_CREATED = "memory.created"
    MEMORY_CONFIRMED = "memory.confirmed"
    MEMORY_CONTRADICTED = "memory.contradicted"
    MEMORY_SUPERSEDED = "memory.superseded"
    MEMORY_ARCHIVED = "memory.archived"
    MEMORY_RESTORED = "memory.restored"
    MEMORY_TOMBSTONED = "memory.tombstoned"
    IDENTITY_VERSION_LOADED = "identity.version_loaded"
    BRAIN_UNAVAILABLE = "brain.unavailable"
    POLICY_REFUSED = "policy.refused"


class Actor(StrEnum):
    USER = "user"
    APOLLO_CORE = "apollo_core"
    MODEL = "model"
    SYSTEM = "system"


class AuditPayloadError(ApolloError):
    """Raised when a payload carries something that is not metadata."""


#: Keys permitted in an audit payload. Everything here is an id, a hash, an
#: enum, a count or a duration. Adding a key is a privacy decision.
ALLOWED_PAYLOAD_KEYS = frozenset(
    {
        "adapter_key",
        "block_count",
        "brain_alias",
        "bundle_hash",
        "compiler_version",
        "count",
        "dropped_count",
        "error_kind",
        "finish_reason",
        "fragment_count",
        "identity_hash",
        "identity_version",
        "invocation_id",
        "invocations_redacted",
        "latency_ms",
        "max_trust_tier",
        "message_role",
        "mode",
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "provider_key",
        "purpose",
        "render_version",
        "retry_of_invocation_id",
        "schema_version",
        "seq",
        "status",
        "taint",
        "token_estimate",
        "token_estimator",
        "reason",
    }
)

#: A hard ceiling on any string value in a payload. Long free text is content.
MAX_PAYLOAD_STRING = 128


def validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for key, value in payload.items():
        if key not in ALLOWED_PAYLOAD_KEYS:
            raise AuditPayloadError(f"audit payload key {key!r} is not metadata")
        if isinstance(value, str) and len(value) > MAX_PAYLOAD_STRING:
            raise AuditPayloadError(f"audit payload value for {key!r} is too long to be metadata")
        if not isinstance(value, str | int | float | bool | type(None)):
            raise AuditPayloadError(f"audit payload value for {key!r} is not a scalar")
    return payload


@dataclass(frozen=True)
class AuditEvent:
    event_type: EventType
    actor: Actor
    subject_kind: str
    occurred_at: datetime
    subject_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    turn_id: uuid.UUID | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_payload(self.payload)
