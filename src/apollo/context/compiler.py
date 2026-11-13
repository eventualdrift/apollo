"""The context compiler (spec F).

Returns a *structure, not a string*: rendering to a model's wire format is the
adapter's job (ADR-0002). `purpose` selects the block set — `reply` builds the
full Apollo context, `memory_proposal` builds the minimal structuring context.

Determinism (spec F.4) is achieved by handling the three practical hazards
explicitly: `now` is passed in and never read from the clock here; collections
are sorted before assembly; ties break on a stable key.

Every compiler version is still buildable (`CompileRequest.compiler_version`),
because replay rebuilds an invocation's bundle with the builder of its vintage
(spec H.4). `compiler-v1` reply bundles carry no memory and a fixed notice;
`compiler-v2` carries what retrieval found, or says that it failed.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, replace
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
from apollo.context.rules import (
    COMPILER_V1,
    COMPILER_V2,
    COMPILER_VERSION,
    COMPILER_VERSIONS,
    CONTEXT_RULES_BY_VERSION,
    PROPOSAL_RULES_BY_VERSION,
)
from apollo.core.identity import Identity
from apollo.errors import ApolloError, ContextOverflowError, HistoryRoleError
from apollo.memory.models import MANIFEST_SOURCE_KIND, manifest_source_ref

log = logging.getLogger(__name__)

#: The only roles that may appear in CONVERSATION_RECENT (spec F.2). This is
#: deliberately not a mapping: an unknown role is rejected, never coerced into
#: the nearest conversational one, and `system_note` has no model-facing role to
#: be mapped to.
CONVERSATIONAL_ROLES = frozenset({"user", "apollo"})

#: compiler-v1's notice: retrieval did not exist yet, so it said so. Kept byte
#: for byte; bundles recorded under compiler-v1 hash over it.
NO_RETRIEVAL_NOTICE = """\
Memory searched: not yet available in this build.
Substantive matches: 0

Apollo has no memory store in this build, so nothing was searched and nothing was found. \
Absence of a memory means nothing was recorded or nothing matched — it is not evidence that \
something did not happen. Say so plainly rather than reconstructing a plausible recollection.
"""

#: The closing paragraph of the notice (spec F.3). The first is the spec's own
#: wording for honest absence; its last clause guards the opposite failure, a
#: system pushed so hard on "don't fabricate" that it denies things that
#: happened but were never written down.
NOTICE_NOTHING_MATCHED = (
    "No stored memory bears on this. Absence of a memory means nothing was recorded or nothing "
    "matched — it is not evidence that something did not happen. Say so plainly rather than "
    "reconstructing a plausible recollection."
)
#: compiler-v2's wording, when pinned counted as a match. Kept for replay.
NOTICE_MATCHED_V2 = (
    "The memories above marked match=pinned or match=lexical are what memory holds for this "
    "message. Use them where they bear on it. If they do not answer what was asked, say so "
    "plainly rather than reconstructing a plausible recollection."
)
NOTICE_MATCHED = (
    "The memories above marked match=lexical are what memory holds for this message. Use them "
    "where they bear on it. If they do not answer what was asked, say so plainly rather than "
    "reconstructing a plausible recollection."
)
#: Nothing matched, and pinned memories are shown (spec E.6/2). The spec's
#: first sentence, "No stored memory bears on this", would be false here: a
#: pinned memory may bear on the message without the search having matched it.
NOTICE_NOTHING_MATCHED_PINNED_SHOWN = (
    "Nothing in memory was matched to this message. The memories above marked match=pinned "
    "are present whatever is asked: use one only where it bears on the message. Absence of a "
    "memory means nothing was recorded or nothing matched — it is not evidence that something "
    "did not happen. Say so plainly rather than reconstructing a plausible recollection."
)
NOTICE_MATCHED_NOT_SHOWN = (
    "Memories matched this message, but none of them fits in this context, so none is shown. "
    "Say that memory could not be consulted in full this time rather than reconstructing a "
    "plausible recollection."
)

#: What the model is told when the search itself failed (spec L): a failed
#: search and an empty one are different facts, and this is never worded as
#: "nothing found".
RETRIEVAL_ERROR_NOTICE = """\
Memory search failed on this turn, so Apollo's memory is unavailable for it. Nothing was \
searched. This is not a finding that no memory exists.

If the message depends on something Apollo may have been asked to remember, say that memory \
could not be checked this time. Do not answer from a guess, and do not say nothing is recorded.
"""

#: Attribute values are closed words and numbers. Anything else would let a
#: fence header carry text Core did not author.
_ATTRIBUTE = re.compile(r"[a-z0-9_.]+")


class CompileInputError(ApolloError):
    """The compiler was handed something it must not build a bundle from."""


class UnsendableContextError(ContextOverflowError):
    """The context was assembled, and it does not fit (spec L).

    `bundle` is what the compiler had built when it stopped: every block it
    would have sent, and every block the budget dropped, with the reason. It
    was never sent. The caller records it with the failed turn, so that what
    did not fit is on record rather than only "it did not fit".
    """

    def __init__(self, message: str, bundle: ContextBundle) -> None:
        super().__init__(message)
        self.bundle = bundle


@dataclass(frozen=True)
class HistoryMessage:
    message_id: str
    role: str
    content: str


@dataclass(frozen=True)
class MemoryItem:
    """One retrieved memory, as the compiler needs it (spec F.2, B.4).

    `attributes` is the block's provenance header in order: scope, origin,
    support, confidence, obs, age. The compiler adds `match` last, since what
    counts as a match is the compiler version's to say (`match_label`).
    `pinned` puts it ahead of the ranked ones, and first in line for the
    memory budget.
    """

    memory_id: uuid.UUID
    content: str
    pinned: bool
    #: The search matched it to this message, above the floor (spec E.3).
    matched: bool
    attributes: tuple[tuple[str, str], ...]


MATCH_LEXICAL = "lexical"
MATCH_PINNED = "pinned"
MATCH_RECENCY = "recency"


def match_label(*, pinned: bool, matched: bool, compiler_version: str) -> str:
    """A memory's `match` attribute: why it is in this context.

    compiler-v2 labelled by the strongest strategy that found it, pinned
    first. From compiler-v3 a memory the search matched is `lexical` whether
    or not it is pinned, and `pinned` means present but not matched (spec
    E.6/2).
    """
    if compiler_version == COMPILER_V2:
        return MATCH_PINNED if pinned else MATCH_LEXICAL if matched else MATCH_RECENCY
    return MATCH_LEXICAL if matched else MATCH_PINNED if pinned else MATCH_RECENCY


def labels_that_match(compiler_version: str) -> frozenset[str]:
    """The `match` labels this compiler version counted as substantive support."""
    if compiler_version == COMPILER_V2:
        return frozenset({MATCH_PINNED, MATCH_LEXICAL})
    return frozenset({MATCH_LEXICAL})


@dataclass(frozen=True)
class RetrievalInput:
    """What retrieval found for this turn, or that it failed (spec E.3, F.3, L).

    `memories` is in final order: pinned first, then by rank. The counts are
    facts about the search, per strategy; `lexical_matches` counts results
    above the floor, and `pinned_or_lexical` the distinct memories those two
    strategies found between them. Which of them is the number of substantive
    matches is the compiler version's to say (`substantive_matches`).
    """

    memories: tuple[MemoryItem, ...] = ()
    query_text: str | None = None
    pinned_count: int = 0
    lexical_matches: int = 0
    recency_count: int = 0
    pinned_or_lexical: int = 0
    failed: bool = False

    @classmethod
    def failure(cls) -> RetrievalInput:
        return cls(failed=True)

    def substantive_matches(self, compiler_version: str) -> int:
        """How many memories count as support: zero exactly when there is none.

        compiler-v2 counted every pinned memory. From compiler-v3 only what
        the search matched counts (spec E.6/2).
        """
        if compiler_version == COMPILER_V2:
            return self.pinned_or_lexical
        return self.lexical_matches


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
    #: Required for a reply from compiler-v2 on: absence is stated, never implied.
    retrieval: RetrievalInput | None = None
    #: The builder's vintage. Anything but the current one is for replay.
    compiler_version: str = COMPILER_VERSION


def compile_context(request: CompileRequest) -> ContextBundle:
    if request.compiler_version not in COMPILER_VERSIONS:
        raise CompileInputError(f"unknown compiler version {request.compiler_version!r}")
    _validate_history_roles(request.history)
    if request.purpose is Purpose.REPLY:
        return _compile_reply(request)
    return _compile_memory_proposal(request)


def _validate_history_roles(history: tuple[HistoryMessage, ...]) -> None:
    """Fail loudly rather than misattributing a message to Janu or to Apollo.

    Silently dropping an unknown role would hide a caller's bug; silently
    mapping `system_note` to `user` would put Core's own words in Janu's mouth
    and give them conversational weight they were never meant to carry.
    """
    for item in history:
        if item.role not in CONVERSATIONAL_ROLES:
            raise HistoryRoleError(
                f"history message {item.message_id} has role {item.role!r}; "
                f"CONVERSATION_RECENT admits only {sorted(CONVERSATIONAL_ROLES)}. "
                "System notes are transcript entries and never model context."
            )


def _compile_reply(req: CompileRequest) -> ContextBundle:
    if req.identity is None:
        raise ValueError("a reply bundle requires an identity")
    est = req.estimator
    version = req.compiler_version
    policy: list[ContextBlock] = [
        _block(BlockType.IDENTITY, TrustTier.CANONICAL, Region.POLICY, "identity",
               req.identity.content_hash, req.identity.content, est),
        _block(BlockType.CONTEXT_RULES, TrustTier.CANONICAL, Region.POLICY, "compiler",
               version, CONTEXT_RULES_BY_VERSION[version], est),
    ]
    policy_tokens = sum(b.token_estimate for b in policy)
    check_policy_region(policy_tokens, req.budget)

    data, dropped = _data_region(req)
    data_tokens = sum(b.token_estimate for b in data)

    request_block = _block(
        BlockType.USER_MESSAGE, TrustTier.USER_DIRECT, Region.REQUEST, "message",
        req.user_message_ref, req.user_message, est,
    )

    # History is oldest-first; truncation removes from the oldest end.
    history_blocks = [
        _block(BlockType.CONVERSATION_RECENT,
               TrustTier.USER_DIRECT if m.role == "user" else TrustTier.APOLLO_PRIOR,
               Region.HISTORY, "message", m.message_id, m.content, est, role=m.role)
        for m in req.history
    ]

    def unsendable(
        overflow: ContextOverflowError, kept: list[ContextBlock],
        dropped_history: list[tuple[ContextBlock, str]],
    ) -> UnsendableContextError:
        assembled = _finalise(
            req, [*policy, *data, *kept, request_block], [*dropped, *dropped_history],
            identity=req.identity, sendable=False,
        )
        return UnsendableContextError(str(overflow), assembled)

    try:
        allowance = conversation_allowance(
            budget=req.budget,
            policy_tokens=policy_tokens,
            request_tokens=request_block.token_estimate,
            data_tokens=data_tokens,
        )
    except ContextOverflowError as overflow:
        # What must be sent is already too much: no history fits beside it.
        raise unsendable(
            overflow, [], [(block, "budget:conversation") for block in history_blocks]
        ) from None

    kept, dropped_history = _fit_history(history_blocks, allowance)
    try:
        assert_conversation_floor(
            [b.source_ref for b in kept], [b.source_ref for b in history_blocks]
        )
    except ContextOverflowError as overflow:
        raise unsendable(overflow, kept, dropped_history) from None

    ordered = [*policy, *data, *kept, request_block]
    return _finalise(req, ordered, [*dropped, *dropped_history], identity=req.identity)


def _data_region(
    req: CompileRequest,
) -> tuple[list[ContextBlock], list[tuple[ContextBlock, str]]]:
    """The reply's data blocks, and the memories the budget dropped (spec F.2, F.5).

    compiler-v1 had no retrieval: one fixed notice. From compiler-v2 the
    region is MEMORY blocks then RETRIEVAL_NOTICE, which is present on every
    turn, or a single RETRIEVAL_ERROR when the search failed. A failed search
    is never rendered as an empty one (spec L).
    """
    est = req.estimator
    version = req.compiler_version
    if version == COMPILER_V1:
        return [_notice(BlockType.RETRIEVAL_NOTICE, NO_RETRIEVAL_NOTICE, version, est)], []
    retrieval = req.retrieval
    if retrieval is None:
        raise CompileInputError(
            "a reply bundle states what retrieval found: pass the retrieval result, "
            "or RetrievalInput.failure() when the search failed"
        )
    if retrieval.failed:
        return [_notice(BlockType.RETRIEVAL_ERROR, RETRIEVAL_ERROR_NOTICE, version, est)], []

    memories, dropped = _fit_memories(retrieval.memories, req.budget.memory_cap, est, version)
    shown_labels = [dict(block.attributes)["match"] for block in memories]
    text = retrieval_notice(
        retrieval,
        shown=sum(label in labels_that_match(version) for label in shown_labels),
        pinned_shown=shown_labels.count(MATCH_PINNED),
        compiler_version=version,
    )
    notice = _notice(BlockType.RETRIEVAL_NOTICE, text, version, est)
    return [*memories, notice], dropped


def retrieval_notice(
    retrieval: RetrievalInput,
    *,
    shown: int,
    pinned_shown: int = 0,
    compiler_version: str = COMPILER_VERSION,
) -> str:
    """The notice body (spec F.3): what was searched, per strategy, and what it means.

    The same shape whether or not anything matched, so the block is a fact the
    model reads every turn rather than an alarm it learns to react to.

    `shown` is how many of the matched memories are in this bundle. Fewer than
    matched means the merge's K or the memory budget left some out, and the
    notice says so: "Apollo had that memory, why didn't he use it" should be
    answerable from the context itself. `pinned_shown` is how many memories
    are in the bundle only because they are pinned.

    Each compiler version's wording is kept: compiler-v2 counted a pinned
    memory as a match, and its bundles hash over the text that said so.
    """
    v2 = compiler_version == COMPILER_V2
    query = _one_line(retrieval.query_text or "")
    searched = (
        f'Memory searched: "{query}"' if query else "Memory searched: (no words to search with)"
    )
    # With nothing pinned the line is the spec's own (F.3). With something
    # pinned, compiler-v3 says what that does and does not mean (E.6/2).
    pinned = f"pinned({retrieval.pinned_count})"
    if retrieval.pinned_count and not v2:
        pinned = pinned[:-1] + ", always present — support only if also matched)"
    strategies = (
        f"Strategies: {pinned}, "
        f"lexical({retrieval.lexical_matches} above threshold), "
        f"recency({retrieval.recency_count}, fallback only — not support)"
    )
    matched = retrieval.substantive_matches(compiler_version)
    lines = [searched, strategies, f"Substantive matches: {matched}"]
    if matched and shown < matched:
        lines.append(f"Not shown: {matched - shown} matched, but did not fit in this context")
    if not matched:
        closing = (
            NOTICE_NOTHING_MATCHED_PINNED_SHOWN if pinned_shown and not v2
            else NOTICE_NOTHING_MATCHED
        )
    elif shown:
        closing = NOTICE_MATCHED_V2 if v2 else NOTICE_MATCHED
    else:
        closing = NOTICE_MATCHED_NOT_SHOWN
    return "\n".join(lines) + f"\n\n{closing}\n"


#: The searched text is quoted on one line of the notice. Longer than this, it
#: is cut: the full messages are in the context already.
NOTICE_QUERY_CHARS = 240


def _one_line(text: str) -> str:
    """Whitespace collapsed and length capped, so a long or multi-line message
    cannot reshape the notice's own lines."""
    flat = " ".join(text.split())
    if len(flat) <= NOTICE_QUERY_CHARS:
        return flat
    return flat[: NOTICE_QUERY_CHARS - 1].rstrip() + "…"


def _notice(
    block_type: BlockType, text: str, version: str, estimator: TokenEstimator
) -> ContextBlock:
    """A Core-authored statement of fact: T0, but in the data region (spec B.1)."""
    return _block(block_type, TrustTier.CANONICAL, Region.DATA, "compiler", version, text,
                  estimator)


def _fit_memories(
    memories: tuple[MemoryItem, ...], cap: int, estimator: TokenEstimator, compiler_version: str
) -> tuple[list[ContextBlock], list[tuple[ContextBlock, str]]]:
    """One block per memory, pinned first then by rank, within the memory cap.

    A memory that does not fit is dropped with its reason recorded, and the
    ones after it are still tried: unlike history, memories are independent
    claims, so a gap hides nothing (spec F.5, F.6).
    """
    ordered = sorted(enumerate(memories), key=lambda item: (not item[1].pinned, item[0]))
    kept: list[ContextBlock] = []
    dropped: list[tuple[ContextBlock, str]] = []
    used = 0
    for _, memory in ordered:
        block = _memory_block(memory, estimator, compiler_version)
        if used + block.token_estimate > cap:
            dropped.append((block, "budget:memory_cap"))
            continue
        used += block.token_estimate
        kept.append(block)
    return kept, dropped


def _memory_block(
    memory: MemoryItem, estimator: TokenEstimator, compiler_version: str
) -> ContextBlock:
    if any(key == "match" for key, _ in memory.attributes):
        raise CompileInputError("a memory's match attribute is the compiler's to write")
    label = match_label(
        pinned=memory.pinned, matched=memory.matched, compiler_version=compiler_version
    )
    attributes = (*memory.attributes, ("match", label))
    for key, value in attributes:
        if not _ATTRIBUTE.fullmatch(key) or not _ATTRIBUTE.fullmatch(value):
            raise CompileInputError(f"memory attribute {key!r} is not a closed word or number")
    ref = manifest_source_ref(memory.memory_id)
    block = _block(BlockType.MEMORY, TrustTier.CURATED, Region.DATA, MANIFEST_SOURCE_KIND, ref,
                   memory.content, estimator)
    return replace(
        block,
        attributes=attributes,
        token_estimate=block.token_estimate + memory_header_tokens(ref, attributes, estimator),
    )


def memory_header_tokens(
    ref: str, attributes: tuple[tuple[str, str], ...], estimator: TokenEstimator
) -> int:
    """A MEMORY block's fence header is sent too, so it counts against the memory budget."""
    header = " ".join([ref, *(f"{key}={value}" for key, value in attributes)])
    return estimator.count(header)


def _compile_memory_proposal(req: CompileRequest) -> ContextBundle:
    """Minimal structuring context: no identity block, no memory blocks (spec D.3).

    The source message is `data`, not `request`: here it is material to be
    structured, not a request to be carried out, so an instruction embedded in
    it is inert even though the same message carries task authority in a reply.
    """
    est = req.estimator
    version = req.compiler_version
    policy = [
        _block(BlockType.PROPOSAL_RULES, TrustTier.CANONICAL, Region.POLICY, "compiler",
               version, PROPOSAL_RULES_BY_VERSION[version], est)
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
    """Keep the newest contiguous run of history that fits (spec F.2).

    Truncation is from the oldest end: walking newest first, the first message
    that doesn't fit ends the history, and it and everything older are dropped.
    Skipping it and keeping older ones would leave a gap the model can't see.
    """
    used = 0
    cut = len(blocks)  # index of the oldest kept block
    for index in range(len(blocks) - 1, -1, -1):
        if used + blocks[index].token_estimate > allowance:
            break
        used += blocks[index].token_estimate
        cut = index
    return blocks[cut:], [(block, "budget:conversation") for block in blocks[:cut]]


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
    sendable: bool = True,
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
        compiler_version=req.compiler_version,
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
        "context.compiled" if sendable else "context.overflow",
        extra={
            "purpose": str(req.purpose),
            "compiler_version": req.compiler_version,
            "block_count": len(blocks),
            "dropped": len(dropped_entries),
            "token_estimate": total,
            "trust_tier": str(max_tier),
        },
    )
    return bundle
