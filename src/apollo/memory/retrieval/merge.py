"""Merge and the substantive-match rule (spec E.3).

- `pinned` bypasses ranking and is always included, ahead of everything else.
- Every other strategy's scores are normalised to [0, 1] by dividing by that
  strategy's best score on this query, so its best result is 1.0. (Lexical
  scores are small absolute values, around 0.03 to 0.1 for a typical match;
  unnormalised, recency's 0.3 outranked every genuine match.) The floor has
  already been applied to the absolute score, so normalising never turns a
  weak match into a strong one: it only orders what passed.
- Each result is then weighted (`lexical` 1.0, `recency` 0.3), deduplicated
  keeping its maximum, sorted descending with ties broken by `memory_id`
  ascending, and the whole list is truncated to K. Pinned memories are never
  truncated: "always know this" is a promise, so if there were more than K of
  them they would all still be included.
- `substantive_support` is pinned results or lexical results above the floor.
  Recency never counts and can never suppress the negative signal.
"""

from __future__ import annotations

import uuid

from apollo.memory.retrieval.base import (
    LEXICAL,
    PINNED,
    RECENCY,
    Hit,
    RetrievalSettings,
    Retrieved,
    StrategyResult,
)


def substantive_support(results: tuple[StrategyResult, ...]) -> bool:
    return any(r.substantive for r in results if r.strategy in (PINNED, LEXICAL))


def merge(
    results: tuple[StrategyResult, ...], settings: RetrievalSettings
) -> tuple[Retrieved, ...]:
    weights = {LEXICAL: settings.lexical_weight, RECENCY: settings.recency_weight}
    pinned: dict[uuid.UUID, Hit] = {}
    best: dict[uuid.UUID, tuple[float, Hit]] = {}
    found_by: dict[uuid.UUID, list[str]] = {}

    for result in results:
        top = max((hit.score for hit in result.hits), default=0.0)
        for hit in result.hits:
            found_by.setdefault(hit.memory_id, []).append(result.strategy)
            if result.strategy == PINNED:
                pinned[hit.memory_id] = hit
                continue
            normalised = hit.score / top if top > 0 else 0.0
            weighted = normalised * weights[result.strategy]
            if hit.memory_id not in best or weighted > best[hit.memory_id][0]:
                best[hit.memory_id] = (weighted, hit)

    always = [
        Retrieved(memory_id, 1.0, True, tuple(found_by[memory_id]), hit.subject, hit.content)
        for memory_id, hit in sorted(pinned.items())
    ]
    ranked = sorted(
        ((score, memory_id, hit) for memory_id, (score, hit) in best.items()
         if memory_id not in pinned),
        key=lambda item: (-item[0], item[1]),
    )
    room = max(settings.k - len(always), 0)
    rest = [
        Retrieved(memory_id, score, False, tuple(found_by[memory_id]), hit.subject, hit.content)
        for score, memory_id, hit in ranked[:room]
    ]
    return (*always, *rest)
