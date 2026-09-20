"""The typed context model: tiers, regions, blocks, manifests (spec F.1).

Two orthogonal attributes carry the trust model:

* `trust_tier` says how much authority a block carries (spec B.1).
* `region` says where an adapter may place it (spec G.3).

They are orthogonal, which is why a T0 notice can sit safely in the `data`
region. The invariant they serve together is: *data cannot impersonate policy
or the current user request*.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TrustTier(StrEnum):
    CANONICAL = "T0"
    USER_DIRECT = "T1"
    APOLLO_PRIOR = "T2"
    CURATED = "T3"
    DERIVED = "T4"
    EXTERNAL = "T5"

    @property
    def level(self) -> int:
        return int(self.value[1:])

    @property
    def has_policy_authority(self) -> bool:
        """Only T0 policy blocks may define or change Apollo's rules (spec B.1)."""
        return self is TrustTier.CANONICAL


class Region(StrEnum):
    """Where an adapter may place a block. Enforced by the render contract."""

    POLICY = "policy"
    HISTORY = "history"
    DATA = "data"
    REQUEST = "request"


class BlockType(StrEnum):
    IDENTITY = "IDENTITY"
    CONTEXT_RULES = "CONTEXT_RULES"
    PROPOSAL_RULES = "PROPOSAL_RULES"
    MEMORY = "MEMORY"
    RETRIEVAL_NOTICE = "RETRIEVAL_NOTICE"
    RETRIEVAL_ERROR = "RETRIEVAL_ERROR"
    CONVERSATION_RECENT = "CONVERSATION_RECENT"
    USER_MESSAGE = "USER_MESSAGE"
    SOURCE_MESSAGE = "SOURCE_MESSAGE"


class Purpose(StrEnum):
    """Closed enum, mirroring `model_invocation.purpose` (ADR-0013)."""

    REPLY = "reply"
    MEMORY_PROPOSAL = "memory_proposal"


#: The tier at or above which a block is considered to taint an invocation.
TAINT_THRESHOLD = TrustTier.DERIVED.level


@dataclass(frozen=True)
class ContextBlock:
    position: int
    block_type: BlockType
    trust_tier: TrustTier
    region: Region
    source_kind: str
    source_ref: str | None
    content: str
    token_estimate: int
    #: For history blocks, the conversational role the adapter must preserve.
    role: str | None = None
    taint: int = 0

    def manifest_entry(self, *, included: bool, drop_reason: str | None = None) -> dict[str, Any]:
        """Manifest entries carry references and metadata, never content (spec F.6)."""
        entry: dict[str, Any] = {
            "position": self.position,
            "block_type": str(self.block_type),
            "trust_tier": str(self.trust_tier),
            "region": str(self.region),
            "taint": self.taint,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "tokens": self.token_estimate,
            "included": included,
        }
        if drop_reason:
            entry["drop_reason"] = drop_reason
        return entry


@dataclass(frozen=True)
class ContextBundle:
    purpose: Purpose
    compiler_version: str
    estimator_name: str
    blocks: tuple[ContextBlock, ...]
    manifest: tuple[dict[str, Any], ...]
    total_token_estimate: int
    max_trust_tier: TrustTier
    taint: int
    bundle_hash: str
    identity_version: str | None = None
    identity_hash: str | None = None
    #: Not persisted; carried so the orchestrator can report what did not fit.
    dropped: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def blocks_in(self, region: Region) -> tuple[ContextBlock, ...]:
        return tuple(b for b in self.blocks if b.region is region)

    def manifest_list(self) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self.manifest]


def compute_bundle_hash(
    manifest: list[dict[str, Any]], blocks: list[ContextBlock]
) -> str:
    """sha256 over the canonical serialisation of the manifest plus block contents."""
    payload = {
        "manifest": manifest,
        "blocks": [
            {
                "position": b.position,
                "block_type": str(b.block_type),
                "trust_tier": str(b.trust_tier),
                "region": str(b.region),
                "role": b.role,
                "content": b.content,
            }
            for b in blocks
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def message_ref(message_id: uuid.UUID) -> str:
    return str(message_id)
