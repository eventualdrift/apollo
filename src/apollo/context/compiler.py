"""The context compiler (spec F).

Returns a *structure, not a string*: rendering to a model's wire format is the
adapter's job (ADR-0002). `purpose` selects the block set — `reply` builds the
full Apollo context, `memory_proposal` builds the minimal structuring context.

Determinism (spec F.4) is achieved by handling the three practical hazards
explicitly: `now` is passed in and never read from the clock here; collections
are sorted before assembly; ties break on a stable key.

M1 note: retrieval does not exist yet (it arrives at step 11), so the reply
bundle carries the honest "nothing was searched" notice rather than inventing
future retrieval behaviour.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from apollo.context.budget import (
    Budget,
    assert_conversation_floor,
    check_policy_region,
    conversation_allowance,
)
from apollo.context.bundle import (
    TAINT_THRESHOLD,
    BlockType,
    ContextBlock,
    ContextBundle,
    Purpose,
    Region,
    TrustTier,
    compute_bundle_hash,
)
from apollo.context.estimator import TokenEstimator
from apollo.context.rules import COMPILER_VERSION, CONTEXT_RULES, PROPOSAL_RULES
from apollo.core.identity import Identity

log = logging.getLogger(__name__)

#: Emitted on every turn. A block that appears only on failure is a block the
#: model learns to read as an alarm (spec E.3).
NO_RETRIEVAL_NOTICE = """\
Memory searched: not yet available in this build.
Substantive matches: 0

Apollo has no memory store in this build, so nothing was searched and nothing was found. \
Absence of a memory means nothing was recorded or nothing matched — it is not evidence that \
something did not happen. Say so plainly rather than reconstructing a plausible recollection.
"""


@dataclass(frozen=True)
class HistoryMessage:
    message_id: str
    role: str
    content: str


@dataclass(frozen=True)
class CompileRequest:
    purpose: Purpose
    user_message: str
    user_message_ref: str | None
    now: datetime
    budget: Budget
    estimator: TokenEstimator
    identity: Identity | None = None
    history: tuple[HistoryMessage, ...] = ()


def compile_context(request: CompileRequest) -> ContextBundle:
    if request.purpose is Purpose.REPLY:
        return _compile_reply(request)
    return _compile_memory_proposal(request)


def _compile_reply(req: CompileRequest) -> ContextBundle:
    if req.identity is None:
        raise ValueError("a reply bundle requires an identity")
    est = req.estimator
    policy: list[ContextBlock] = [
        _block(BlockType.IDENTITY, TrustTier.CANONICAL, Region.POLICY, "identity",
               req.identity.content_hash, req.identity.content, est),
        _block(BlockType.CONTEXT_RULES, TrustTier.CANONICAL, Region.POLICY, "compiler",
               COMPILER_VERSION, CONTEXT_RULES, est),
    ]
    policy_tokens = sum(b.token_estimate for b in policy)
    check_policy_region(policy_tokens, req.budget)

    # No MEMORY blocks in M1: retrieval arrives at step 11. The notice is still
    # present, because absence must be stated rather than left as silence.
    data: list[ContextBlock] = [
        _block(BlockType.RETRIEVAL_NOTICE, TrustTier.CANONICAL, Region.DATA, "compiler",
               COMPILER_VERSION, NO_RETRIEVAL_NOTICE, est),
    ]
    data_tokens = sum(b.token_estimate for b in data)

    request_block = _block(
        BlockType.USER_MESSAGE, TrustTier.USER_DIRECT, Region.REQUEST, "message",
        req.user_message_ref, req.user_message, est,
    )

    allowance = conversation_allowance(
        budget=req.budget,
        policy_tokens=policy_tokens,
        request_tokens=request_block.token_estimate,
        data_tokens=data_tokens,
    )

    # History is oldest-first; truncation removes from the oldest end.
    history_blocks = [
        _block(BlockType.CONVERSATION_RECENT,
               TrustTier.USER_DIRECT if m.role == "user" else TrustTier.APOLLO_PRIOR,
               Region.HISTORY, "message", m.message_id, m.content, est, role=m.role)
        for m in req.history
    ]
    kept, dropped = _fit_history(history_blocks, allowance)
    assert_conversation_floor(len(kept), len(history_blocks))

    ordered = [*policy, *data, *kept, request_block]
    return _finalise(req, ordered, dropped, identity=req.identity)


def _compile_memory_proposal(req: CompileRequest) -> ContextBundle:
    """Minimal structuring context: no identity block, no memory blocks (spec D.3).

    The source message is `data`, not `request`: here it is material to be
    structured, not a request to be carried out, so an instruction embedded in
    it is inert even though the same message carries task authority in a reply.
    """
    est = req.estimator
    policy = [
        _block(BlockType.PROPOSAL_RULES, TrustTier.CANONICAL, Region.POLICY, "compiler",
               COMPILER_VERSION, PROPOSAL_RULES, est)
    ]
    check_policy_region(sum(b.token_estimate for b in policy), req.budget)
    data = [
        _block(BlockType.SOURCE_MESSAGE, TrustTier.USER_DIRECT, Region.DATA, "message",
               req.user_message_ref, req.user_message, est)
    ]
    return _finalise(req, [*policy, *data], [], identity=None)


def _fit_history(
    blocks: list[ContextBlock], allowance: int
) -> tuple[list[ContextBlock], list[tuple[ContextBlock, str]]]:
    kept: list[ContextBlock] = []
    dropped: list[tuple[ContextBlock, str]] = []
    used = 0
    for block in reversed(blocks):  # newest first, so the oldest are dropped
        if used + block.token_estimate <= allowance:
            kept.append(block)
            used += block.token_estimate
        else:
            dropped.append((block, "budget:conversation"))
    kept.reverse()
    dropped.reverse()
    return kept, dropped


def _block(
    block_type: BlockType,
    tier: TrustTier,
    region: Region,
    source_kind: str,
    source_ref: str | None,
    content: str,
    estimator: TokenEstimator,
    *,
    role: str | None = None,
) -> ContextBlock:
    return ContextBlock(
        position=0,  # assigned during finalisation, so ordering is one decision
        block_type=block_type,
        trust_tier=tier,
        region=region,
        source_kind=source_kind,
        source_ref=source_ref,
        content=content,
        token_estimate=estimator.count(content),
        role=role,
        taint=tier.level if tier.level >= TAINT_THRESHOLD else 0,
    )


def _finalise(
    req: CompileRequest,
    ordered: list[ContextBlock],
    dropped: list[tuple[ContextBlock, str]],
    *,
    identity: Identity | None,
) -> ContextBundle:
    blocks = tuple(
        ContextBlock(**{**b.__dict__, "position": i}) for i, b in enumerate(ordered)
    )
    manifest: list[dict[str, Any]] = [b.manifest_entry(included=True) for b in blocks]
    dropped_entries: list[dict[str, Any]] = []
    for offset, (block, reason) in enumerate(dropped, start=len(blocks)):
        entry = ContextBlock(**{**block.__dict__, "position": offset}).manifest_entry(
            included=False, drop_reason=reason
        )
        manifest.append(entry)
        dropped_entries.append(entry)

    max_tier = max((b.trust_tier for b in blocks), key=lambda t: t.level)
    taint = max((b.taint for b in blocks), default=0)
    total = sum(b.token_estimate for b in blocks)
    bundle = ContextBundle(
        purpose=req.purpose,
        compiler_version=COMPILER_VERSION,
        estimator_name=req.estimator.name,
        blocks=blocks,
        manifest=tuple(manifest),
        total_token_estimate=total,
        max_trust_tier=max_tier,
        taint=taint,
        bundle_hash=compute_bundle_hash(manifest, list(blocks)),
        identity_version=identity.version_label if identity else None,
        identity_hash=identity.content_hash if identity else None,
        dropped=tuple(dropped_entries),
    )
    log.info(
        "context.compiled",
        extra={
            "purpose": str(req.purpose),
            "compiler_version": COMPILER_VERSION,
            "bundle_hash": bundle.bundle_hash,
            "block_count": len(blocks),
            "dropped": len(dropped_entries),
            "token_estimate": total,
            "trust_tier": str(max_tier),
        },
    )
    return bundle
