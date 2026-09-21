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

from apollo.context.bundle import BlockType, ContextBundle, Region
from apollo.context.escaping import FENCE_CLOSE, FENCE_OPEN, escape, fence
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


#: Labels that may legitimately open a fence. Used to parse rendered bytes back
#: into fenced regions without trusting any adapter bookkeeping.
_FENCE_LABELS = frozenset(bt.value for bt in BlockType)


def parse_fences(text: str) -> tuple[list[tuple[str, str]], str]:
    """Split rendered text into (label, body) fences and the unfenced remainder.

    Parsed from the bytes the provider will actually receive. A malformed or
    unmatched delimiter is not a fence: `<<<END MEMORY>>>` sitting in ordinary
    prose opens nothing, which is why a user may type one in a request without
    creating a region.
    """
    fences: list[tuple[str, str]] = []
    remainder: list[str] = []
    i = 0
    while True:
        start = text.find(FENCE_OPEN, i)
        if start == -1:
            remainder.append(text[i:])
            break
        header_end = text.find(FENCE_CLOSE, start + len(FENCE_OPEN))
        if header_end == -1:
            remainder.append(text[i:])
            break
        header = text[start + len(FENCE_OPEN) : header_end]
        label = header.split(" ", 1)[0].strip()
        closing = f"{FENCE_OPEN}END {label}{FENCE_CLOSE}"
        body_start = header_end + len(FENCE_CLOSE)
        close_at = text.find(closing, body_start) if label in _FENCE_LABELS else -1
        if close_at == -1:
            # Not a well-formed fence; treat the delimiter as ordinary text.
            remainder.append(text[i : start + len(FENCE_OPEN)])
            i = start + len(FENCE_OPEN)
            continue
        remainder.append(text[i:start])
        body = text[body_start:close_at]
        # fence() writes exactly one newline either side of the escaped body.
        if body.startswith("\n"):
            body = body[1:]
        if body.endswith("\n"):
            body = body[:-1]
        fences.append((label, body))
        i = close_at + len(closing)
    return fences, "".join(remainder)


def policy_message_indexes(request: RenderedRequest) -> list[int]:
    """Which rendered messages constitute the policy region, read from the request.

    The policy region is every system-role message; where a provider has no
    system role, it is the first message — the highest-authority position that
    provider offers. Derived from the rendered request, never declared by the
    adapter.
    """
    system = [i for i, m in enumerate(request.messages) if m.role == "system"]
    if system:
        return system
    return [0] if request.messages else []


def verify_region_contract(bundle: ContextBundle, request: RenderedRequest) -> None:
    """The hard rules of spec G.3, checked against the actual rendered output.

    This function is Gate 1's teeth, so it reads only `request.messages` — the
    bytes the provider receives. `RenderedRequest.placement` is adapter-supplied
    bookkeeping and is deliberately **not** consulted: an adapter that placed a
    T3 memory inside the system message while reporting it elsewhere would
    otherwise verify clean, which is exactly the hole this closes.

    Cheap enough to run on every render, and running always is what makes the
    contract a property rather than something Gate 1 happens to catch.
    """
    if not request.messages:
        raise RenderContractError("rendered request has no messages")

    policy_indexes = set(policy_message_indexes(request))
    policy_text = "\n".join(
        request.messages[i].content for i in sorted(policy_indexes)
    )
    non_policy = [
        (i, m) for i, m in enumerate(request.messages) if i not in policy_indexes
    ]

    _verify_policy_blocks_present(bundle, policy_text)
    _verify_no_data_in_policy(bundle, policy_text)
    _verify_data_is_fenced_and_escaped(bundle, non_policy)
    _verify_history_roles(bundle, non_policy)
    _verify_request(bundle, request, policy_indexes)


def _verify_policy_blocks_present(bundle: ContextBundle, policy_text: str) -> None:
    """Every policy block must actually have been rendered into the policy region."""
    for block in bundle.blocks_in(Region.POLICY):
        if block.content.strip() and block.content.strip() not in policy_text:
            raise RenderContractError(
                f"policy block {block.block_type} was not rendered into the policy region"
            )


def _verify_no_data_in_policy(bundle: ContextBundle, policy_text: str) -> None:
    """No data block may appear in the policy region, in any form.

    Known limitation, deferred deliberately: this is a substring test, so a very
    short future MEMORY block whose content also occurs inside the identity text
    — a claim that is literally "Apollo", say — would false-positive. M1 renders
    no MEMORY blocks in normal operation, and the right fix depends on what real
    memory rendering looks like, so it is revisited at step 11 rather than
    guessed at now. See docs/architecture/implementation-plan.md, step 11.
    """
    for block in bundle.blocks_in(Region.DATA):
        if not block.content.strip():
            continue
        if f"{FENCE_OPEN}{block.block_type}" in policy_text:
            raise RenderContractError(
                f"a {block.block_type} fence was opened inside the policy region"
            )
        for form, how in ((block.content, "verbatim"), (escape(block.content), "escaped")):
            if form.strip() and form in policy_text:
                raise RenderContractError(
                    f"data block {block.block_type} appears {how} in the policy region"
                )


def _verify_data_is_fenced_and_escaped(
    bundle: ContextBundle, non_policy: list[tuple[int, RenderedMessage]]
) -> None:
    """Each data block must appear as a properly fenced, escaped body outside policy."""
    bodies: dict[str, list[str]] = {}
    for _, message in non_policy:
        for label, body in parse_fences(message.content)[0]:
            bodies.setdefault(label, []).append(body)

    for block in bundle.blocks_in(Region.DATA):
        found = bodies.get(str(block.block_type), [])
        if escape(block.content) not in found:
            raise RenderContractError(
                f"data block {block.block_type} was not rendered as a fenced, escaped "
                "body outside the policy region"
            )


def _data_fence_spans(bundle: ContextBundle, text: str) -> list[tuple[int, int]]:
    """Character spans in `text` occupied by a fence carrying a real data block.

    These are the only spans that count as "fenced" when locating a request or a
    history block. The distinction is provenance, not syntax: a fence has
    structural meaning only when it actually carries a `data` block from this
    bundle. Fence-shaped bytes that came from a message — because Janu typed
    them, or because Apollo quoted them back — are ordinary dialogue rendered
    verbatim, and treating them as structure is what bricked conversations that
    contained one.
    """
    spans: list[tuple[int, int]] = []
    for block in bundle.blocks_in(Region.DATA):
        label = str(block.block_type)
        needle = escape(block.content)
        closing = f"{FENCE_OPEN}END {label}{FENCE_CLOSE}"
        cursor = 0
        while True:
            start = text.find(f"{FENCE_OPEN}{label}", cursor)
            if start == -1:
                break
            close_at = text.find(closing, start)
            if close_at == -1:
                break
            end = close_at + len(closing)
            if needle in text[start:end]:
                spans.append((start, end))
            cursor = end
    return spans


def _verify_history_roles(
    bundle: ContextBundle, non_policy: list[tuple[int, RenderedMessage]]
) -> None:
    """Prior turns keep their conversational roles and are not swallowed by a data fence.

    History content may be anything a person or Apollo said, including prose
    that looks exactly like a compiler fence. So the check asks the same
    question `_verify_request` asks: is the block present outside the spans
    occupied by *real* data blocks? Parsing history with generic fence syntax
    would let a message's own bytes hide it from the verifier, which is a
    continuity defect rather than a security one — every later turn in that
    conversation fails until the message ages out of the window.
    """
    for block in bundle.blocks_in(Region.HISTORY):
        expected = "assistant" if block.role == "apollo" else "user"
        if not any(
            m.role == expected
            and block.content in _excise(m.content, _data_fence_spans(bundle, m.content))
            for _, m in non_policy
        ):
            raise RenderContractError(
                f"history block {block.source_ref} was not rendered outside the data "
                f"fences as a {expected} turn"
            )


def _verify_request(
    bundle: ContextBundle, request: RenderedRequest, policy_indexes: set[int]
) -> None:
    """The request is the final user turn, verbatim and unfenced."""
    blocks = bundle.blocks_in(Region.REQUEST)
    if not blocks:
        return
    last_index = len(request.messages) - 1
    last = request.messages[last_index]
    if last_index in policy_indexes:
        raise RenderContractError("the request block landed in the policy region")
    if last.role != "user":
        raise RenderContractError("the request block must be the final user turn")

    spans = _data_fence_spans(bundle, last.content)
    outside = _excise(last.content, spans)
    for block in blocks:
        if block.content not in last.content:
            raise RenderContractError("the request block was not rendered verbatim")
        if block.content not in outside:
            raise RenderContractError("the request block was merged into a data fence")
        if escape(block.content) != block.content and escape(block.content) in outside:
            raise RenderContractError("the request block was escaped")


def _excise(text: str, spans: list[tuple[int, int]]) -> str:
    """Remove the given character spans, so what remains is genuinely outside them."""
    if not spans:
        return text
    kept: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        if start >= cursor:
            kept.append(text[cursor:start])
            cursor = end
    kept.append(text[cursor:])
    return "".join(kept)
