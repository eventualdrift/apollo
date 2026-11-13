"""Persistence-intent detection (spec D.2): a pattern list, never a judgement."""

from __future__ import annotations

import pytest

from apollo.memory.intent import INTENT_PATTERNS, detect_persistence_intent

#: Spec D.2's list, as written there.
SPEC_PHRASES = [
    "remember that", "remember this", "remember:", "keep in mind", "bear in mind",
    "from now on", "note that", "make a note", "don't forget", "i want you to remember",
    "for future reference",
]


@pytest.mark.parametrize("phrase", SPEC_PHRASES)
def test_every_phrase_the_spec_lists_is_detected(phrase: str) -> None:
    assert detect_persistence_intent(f"Okay, {phrase} I take my coffee black.") is not None
    assert detect_persistence_intent(f"okay, {phrase.upper()} I TAKE MY COFFEE BLACK") is not None


def test_the_list_is_exactly_the_specs() -> None:
    assert len(INTENT_PATTERNS) == len(SPEC_PHRASES) == 11
    assert len(set(INTENT_PATTERNS)) == 11


@pytest.mark.parametrize("message", [
    "What do you remember about my thesis?",
    "I can't remember where I put the keys.",
    "Remembering this is harder than it looks.",
    "I misremember that every time.",
    "That was a footnote that nobody read.",
    "He noted the build was red.",
    "I never forget a face.",
    "Mind the gap.",
    "Explain what this function does.",
    "",
])
def test_ordinary_conversation_is_never_a_candidate(message: str) -> None:
    """Spec D.2: phase zero never scans ordinary conversation for memory candidates."""
    assert detect_persistence_intent(message) is None


@pytest.mark.parametrize(("message", "name"), [
    ("please   remember \n that the door is green", "remember_that"),
    ("Remember: buy oat milk", "remember_colon"),
    ("remember : buy oat milk", "remember_colon"),
    ("Dont forget the meeting", "dont_forget"),
    ("Don’t forget the meeting", "dont_forget"),
    ("From now on, answer in metric.", "from_now_on"),
    ("I want you to remember this: I'm vegetarian", "i_want_you_to_remember"),
])
def test_spacing_case_and_apostrophes_do_not_matter(message: str, name: str) -> None:
    assert detect_persistence_intent(message) == name


def test_detection_is_deterministic_and_reports_a_closed_name() -> None:
    message = "For future reference, and from now on, note that I use vim."
    first = detect_persistence_intent(message)
    assert first == detect_persistence_intent(message) == "from_now_on"  # list order, not text
    assert first in INTENT_PATTERNS
