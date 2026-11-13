"""Rebuild a recorded bundle from its manifest and its sources (spec H.4).

The compiler builds a bundle from live inputs: a budget, a search, a history.
Replay has none of those. It has the manifest an invocation recorded, which
says what each block was and where its content came from, and it has the rows
those references name. This module turns the two back into the bundle, with
the texts and the counting of the compiler version that built it, so that its
hash can be compared with the one recorded.

Nothing here reads a database or decides what an absent source means. The
caller (`apollo.core.replay`) supplies each included block's content, or does
not rebuild at all: a bundle is never rebuilt around a gap, and a block's
content is never taken from anywhere but the source its entry names.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from apollo.context.bundle import (
    BlockType,
    ContextBlock,
    ContextBundle,
    Purpose,
    Region,
    TrustTier,
    compute_bundle_hash,
)
from apollo.context.compiler import (
    NO_RETRIEVAL_NOTICE,
    RETRIEVAL_ERROR_NOTICE,
    memory_header_tokens,
)
from apollo.context.estimator import TokenEstimator
from apollo.context.rules import (
    COMPILER_V1,
    COMPILER_VERSIONS,
    CONTEXT_RULES_BY_VERSION,
    PROPOSAL_RULES_BY_VERSION,
)

#: A MEMORY block's `match` attribute for one found only because it is recent.
MATCH_RECENCY = "recency"


def fixed_text(block_type: BlockType, compiler_version: str) -> str | None:
    """The text a compiler of this version always wrote into a block of this type.

    None when the block's text was not fixed: it came from a row, or (the
    retrieval notice, from compiler-v2 on) from what that turn's search found.
    Also None for a version this code no longer builds.
    """
    if compiler_version not in COMPILER_VERSIONS:
        return None
    if block_type is BlockType.CONTEXT_RULES:
        return CONTEXT_RULES_BY_VERSION[compiler_version]
    if block_type is BlockType.PROPOSAL_RULES:
        return PROPOSAL_RULES_BY_VERSION[compiler_version]
    if compiler_version == COMPILER_V1:
        return NO_RETRIEVAL_NOTICE if block_type is BlockType.RETRIEVAL_NOTICE else None
    return RETRIEVAL_ERROR_NOTICE if block_type is BlockType.RETRIEVAL_ERROR else None


def shown_matches(manifest: Sequence[Mapping[str, Any]]) -> int:
    """How many memories the bundle shows that matched, rather than being merely recent.

    The notice says how many matched and how many of those are shown. The
    first is in the retrieval record; the second is here, in the manifest:
    an included MEMORY entry whose recorded `match` is not `recency`.
    """
    return sum(
        1 for entry in manifest
        if entry.get("included") and entry.get("block_type") == BlockType.MEMORY
        and dict(_attributes(entry)).get("match") != MATCH_RECENCY
    )


def rebuilt_block(
    entry: Mapping[str, Any], *, content: str, role: str | None, estimator: TokenEstimator
) -> ContextBlock:
    """The block an included manifest entry describes, with its content restored.

    Its tokens are counted again with the invocation's estimator, as the
    compiler counted them, so an estimator that has drifted shows up as a
    difference from the recorded entry rather than passing unnoticed.
    """
    block_type = BlockType(entry["block_type"])
    attributes = _attributes(entry)
    tokens = estimator.count(content)
    if block_type is BlockType.MEMORY:
        tokens += memory_header_tokens(str(entry["source_ref"]), attributes, estimator)
    return ContextBlock(
        position=int(entry["position"]),
        block_type=block_type,
        trust_tier=TrustTier(entry["trust_tier"]),
        region=Region(entry["region"]),
        source_kind=str(entry["source_kind"]),
        source_ref=entry["source_ref"],
        content=content,
        token_estimate=tokens,
        role=role,
        taint=int(entry["taint"]),
        attributes=attributes,
    )


def rebuilt_bundle(
    *,
    purpose: Purpose,
    compiler_version: str,
    estimator: TokenEstimator,
    manifest: Sequence[Mapping[str, Any]],
    blocks: Sequence[ContextBlock],
    identity_version: str | None = None,
) -> ContextBundle:
    """The bundle, hashed exactly as the compiler hashed it: the whole recorded
    manifest, dropped entries included, and the content of the included blocks."""
    recorded = [dict(entry) for entry in manifest]
    ordered = sorted(blocks, key=lambda block: block.position)
    identity = next((b for b in ordered if b.block_type is BlockType.IDENTITY), None)
    return ContextBundle(
        purpose=purpose,
        compiler_version=compiler_version,
        estimator_name=estimator.name,
        blocks=tuple(ordered),
        manifest=tuple(recorded),
        total_token_estimate=sum(b.token_estimate for b in ordered),
        max_trust_tier=max((b.trust_tier for b in ordered), key=lambda tier: tier.level,
                           default=TrustTier.CANONICAL),
        taint=max((b.taint for b in ordered), default=0),
        bundle_hash=compute_bundle_hash(recorded, list(ordered)),
        identity_version=identity_version,
        identity_hash=None if identity is None else identity.source_ref,
        dropped=tuple(entry for entry in recorded if not entry.get("included")),
    )


def _attributes(entry: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple((str(key), str(value)) for key, value in entry.get("attributes", ()))
