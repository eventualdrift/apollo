"""Persistence-intent detection (spec D.2).

Apollo proposes a memory only when the user's message asks for one in so many
words. Detection is deterministic application code, a case-insensitive pattern
list over the message, and never a model's judgement: a model-based classifier
is exactly the door through which "the model decided this should be
remembered" walks in. The list is auditable, testable, and cheap to extend
when a phrasing is seen to be missed.

Nothing here scans ordinary conversation for memory candidates. A message
either carries one of these phrases or it does not.
"""

from __future__ import annotations

import re

#: Spec D.2's list, verbatim, each with the name it is reported under. Names
#: are closed words: they may appear in a log or an audit payload; the message
#: never does.
_PHRASES: tuple[tuple[str, str], ...] = (
    ("i_want_you_to_remember", r"i want you to remember"),
    ("remember_that", r"remember that"),
    ("remember_this", r"remember this"),
    ("remember_colon", r"remember\s*:"),
    ("keep_in_mind", r"keep in mind"),
    ("bear_in_mind", r"bear in mind"),
    ("from_now_on", r"from now on"),
    ("note_that", r"note that"),
    ("make_a_note", r"make a note"),
    ("dont_forget", r"don['’]?t forget"),
    ("for_future_reference", r"for future reference"),
)

#: A phrase is matched as whole words, with any run of whitespace between them,
#: so "remember   that" matches and "misremember that" does not. A phrase that
#: ends in punctuation is complete at that mark: "remember:buy milk" matches.
def _compiled(phrase: str) -> re.Pattern[str]:
    spaced = phrase.replace(" ", r"\s+")
    ends_in_a_word = re.search(r"\w$", phrase) is not None
    return re.compile(rf"(?<!\w){spaced}" + (r"(?!\w)" if ends_in_a_word else ""), re.IGNORECASE)


_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, _compiled(phrase)) for name, phrase in _PHRASES
)

#: The names, for tests and for anything that lists what is detected.
INTENT_PATTERNS: tuple[str, ...] = tuple(name for name, _ in _PHRASES)


def detect_persistence_intent(message: str) -> str | None:
    """The name of the first listed phrase the message contains, or None.

    The first in list order, not in the message, so the answer does not depend
    on how a sentence happens to be arranged.
    """
    for name, pattern in _PATTERNS:
        if pattern.search(message):
            return name
    return None
