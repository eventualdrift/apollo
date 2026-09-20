"""The Brain protocol and the four-region render contract (ADR-0002, spec G.3).

Adapters are stateless and hold no Apollo state. They cannot reach storage —
that is asserted by the architecture test, and it is what makes "Apollo is not
the model" (ADR-0001) structurally true rather than aspirational.

`render_version` identifies the exact bundle-to-request transformation: block
ordering, system-role placement, fence application, escaping, message-array
shape, parameter mapping. It is bumped whenever that transformation changes in
a way that alters the bytes sent. It is not a package version.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from apollo.context.bundle import ContextBundle, Region
from apollo.context.escaping import contains_fence_delimiter, escape, fence
from apollo.context.estimator import TokenEstimator
from apollo.errors import ApolloError


class RenderContractError(ApolloError):
    """Raised when a rendered request would violate the region contract."""


@dataclass(frozen=True)
class ModelCapabilities:
    max_context: int
    estimator: TokenEstimator
    supports_system_role: bool = True
    supports_seed: bool = False
    supports_temperature_zero: bool = True
    reports_token_counts: bool = False


@dataclass(frozen=True)
class RenderedMessage:
    role: str  # system | user | assistant
    content: str


@dataclass(frozen=True)
class RenderedRequest:
    """A provider-shaped request plus the region accounting that proves it is legal."""

    messages: tuple[RenderedMessage, ...]
    #: Which bundle block positions landed in which rendered message index.
    placement: tuple[tuple[int, int], ...]
    prompt_hash: str

    def content_of(self, role: str) -> str:
        return "\n".join(m.content for m in self.messages if m.role == role)


@dataclass(frozen=True)
class GenerationParams:
    temperature: float = 0.0
    max_tokens: int = 1024
    seed: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"temperature": self.temperature, "max_tokens": self.max_tokens, "seed": self.seed}


@dataclass(frozen=True)
class Generation:
    text: str
    finish_reason: str
    model_identifier: str
    latency_ms: int
    rendered_prompt_hash: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    #: Never persisted. Core copies out a whitelist of scalars (spec H.5).
    raw_meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Brain(Protocol):
    key: str
    adapter_key: str
    render_version: str

    def capabilities(self) -> ModelCapabilities: ...

    def render(self, bundle: ContextBundle) -> RenderedRequest: ...

    def generate(self, req: RenderedRequest, params: GenerationParams) -> Generation: ...


# ---------------------------------------------------------------------------
# Shared chat-shaped rendering. Every chat adapter uses this, so the region
# contract is implemented once rather than re-derived per provider.
# ---------------------------------------------------------------------------

#: Bumped whenever the transformation below changes the bytes sent.
CHAT_RENDER_VERSION = "chat-v1"


def render_chat(bundle: ContextBundle, *, supports_system_role: bool = True) -> RenderedRequest:
    """Render a bundle into system / history / (data + request) messages.

    Data and the request share the final user message because consecutive user
    messages are rejected by some providers. The fencing, not the message
    boundary, is what separates them.
    """
    messages: list[RenderedMessage] = []
    placement: list[tuple[int, int]] = []

    policy = bundle.blocks_in(Region.POLICY)
    if policy:
        text = "\n\n".join(b.content.strip() for b in policy)
        role = "system" if supports_system_role else "user"
        messages.append(RenderedMessage(role=role, content=text))
        placement.extend((b.position, 0) for b in policy)

    for block in bundle.blocks_in(Region.HISTORY):
        role = "assistant" if block.role == "apollo" else "user"
        messages.append(RenderedMessage(role=role, content=block.content))
        placement.append((block.position, len(messages) - 1))

    tail: list[str] = []
    tail_positions: list[int] = []
    for block in bundle.blocks_in(Region.DATA):
        header = f"{block.block_type} tier={block.trust_tier}"
        if block.source_ref:
            header += f" ref={block.source_kind}:{block.source_ref}"
        tail.append(fence(header, block.content, label=str(block.block_type)))
        tail_positions.append(block.position)

    request_blocks = bundle.blocks_in(Region.REQUEST)
    for block in request_blocks:
        # Verbatim and unfenced. This is the request, not data.
        tail.append(block.content)
        tail_positions.append(block.position)

    if tail:
        messages.append(RenderedMessage(role="user", content="\n\n".join(tail)))
        index = len(messages) - 1
        placement.extend((position, index) for position in tail_positions)

    request = RenderedRequest(
        messages=tuple(messages),
        placement=tuple(placement),
        prompt_hash=prompt_hash(messages),
    )
    verify_region_contract(bundle, request)
    return request


def prompt_hash(messages: list[RenderedMessage] | tuple[RenderedMessage, ...]) -> str:
    payload = [{"role": m.role, "content": m.content} for m in messages]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_region_contract(bundle: ContextBundle, request: RenderedRequest) -> None:
    """The three hard rules of spec G.3, checked on every render.

    Cheap enough to run always, and running always is what makes it a property
    rather than something Gate 1 happens to catch.
    """
    index_of = {position: message_index for position, message_index in request.placement}
    policy_indexes = {
        index_of[b.position] for b in bundle.blocks_in(Region.POLICY) if b.position in index_of
    }
    for block in bundle.blocks_in(Region.DATA):
        target = index_of.get(block.position)
        if target is None:
            raise RenderContractError(f"data block {block.block_type} was not rendered")
        if target in policy_indexes:
            raise RenderContractError(
                f"data block {block.block_type} landed in the policy region"
            )
        rendered = request.messages[target].content
        if block.content and escape(block.content) not in rendered:
            raise RenderContractError(
                f"data block {block.block_type} was not escaped before fencing"
            )
        if contains_fence_delimiter(block.content) and block.content in rendered:
            raise RenderContractError(
                f"data block {block.block_type} kept a fence delimiter after rendering"
            )

    for block in bundle.blocks_in(Region.REQUEST):
        target = index_of.get(block.position)
        if target is None:
            raise RenderContractError("the request block was not rendered")
        if target in policy_indexes:
            raise RenderContractError("the request block landed in the policy region")
        if block.content not in request.messages[target].content:
            raise RenderContractError("the request block was not rendered verbatim")
        if request.messages[target].role != "user":
            raise RenderContractError("the request block must be the final user turn")
