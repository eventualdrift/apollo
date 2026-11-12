"""`lexical`: Postgres full-text search over the current and previous user message.

**Any word, not every word** (Janu, 2026-11-12). Postgres's default query form
requires every term, so a natural question ("should I just answer or recap the
question first?") would find nothing unless the memory contained all of its
words. The query instead matches a memory sharing any of its words, ranks by
`ts_rank_cd` (cover density: more terms, closer together, rank higher), and the
floor keeps one weak shared word from counting as a match.

The words are taken from the text as plain tokens and joined with `or` for
`websearch_to_tsquery`, so nothing a person types is read as query syntax:
quotes, minus signs and operators are not tokens. Stop words and stemming are
the text search configuration's (spec E.4).
"""

from __future__ import annotations

import re
import time

from apollo.memory.retrieval.base import (
    LEXICAL,
    Hit,
    RetrievalQuery,
    RetrievalSettings,
    StrategyResult,
)
from apollo.storage.repositories import RetrievalRepository

_WORD = re.compile(r"\w+")


def query_text(query: RetrievalQuery) -> str:
    """The text searched: the previous user message, then the current one."""
    return "\n".join(t for t in (query.previous, query.current) if t and t.strip())


def any_word_query(text: str, max_terms: int) -> str:
    """Distinct words joined by `or`, in first-seen order. Empty if there are none."""
    seen: dict[str, None] = {}
    for word in _WORD.findall(text):
        lowered = word.lower()
        if lowered != "or" and lowered not in seen:
            seen[lowered] = None
        if len(seen) >= max_terms:
            break
    return " or ".join(seen)


class LexicalStrategy:
    name = LEXICAL

    def run(
        self, repo: RetrievalRepository, query: RetrievalQuery, settings: RetrievalSettings
    ) -> StrategyResult:
        started = time.monotonic()
        text = query_text(query)
        terms = any_word_query(text, settings.max_query_terms)
        rows = (
            repo.lexical(
                str(query.origin), terms,
                config=settings.text_search_config, limit=settings.lexical_limit,
            )
            if terms else []
        )
        hits = tuple(
            Hit.from_row(row, float(row["score"]))
            for row in rows
            if float(row["score"]) > settings.lexical_floor
        )
        return StrategyResult(
            strategy=LEXICAL,
            query_text=text or None,
            hits=hits,
            substantive=bool(hits),
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        )
