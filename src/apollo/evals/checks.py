"""The nine check types. Mechanical, deterministic, no LLM judge (spec J.1).

Every check returns a structured `CheckResult` carrying what it measured and
what it required, because "False" tells you a case regressed and nothing about
why. None of them records hidden model reasoning — they see only the visible
response.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from apollo.evals.models import CheckResult, CheckSpec, CheckStatus, CheckType

#: The evaluative openers the behavioural contract forbids (B1). Kept here
#: rather than derived from identity, so the model never sees its answer key.
EVALUATIVE_OPENERS = (
    "great question",
    "good question",
    "excellent",
    "absolutely",
    "that's a great",
    "that is a great",
    "i love that",
    "interesting question",
    "good catch",
    "fair point",
    "good point",
    "what a great",
)

#: B1 scopes the opener rule to the start of the response.
OPENER_WINDOW = 80

#: B2's heuristic: overlap between the response's first sentence and the input.
RESTATEMENT_NGRAM = 3
RESTATEMENT_DEFAULT_THRESHOLD = 0.5

_SENTENCE_END = re.compile(r"(?<=[.!?])\s")
_LIST_LINE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")
_WORD = re.compile(r"\S+")


def word_count(text: str) -> int:
    """One definition of a word, used everywhere: a run of non-whitespace."""
    return len(_WORD.findall(text))


def first_sentence(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    return _SENTENCE_END.split(stripped, maxsplit=1)[0]


def _ngrams(text: str, n: int) -> set[tuple[str, ...]]:
    words = [w.lower().strip(".,!?;:\"'()") for w in _WORD.findall(text)]
    words = [w for w in words if w]
    if len(words) < n:
        return {tuple(words)} if words else set()
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def restatement_overlap(response: str, prompt: str) -> float:
    """Fraction of the first sentence's trigrams that also appear in the input.

    Limitation, stated rather than hidden: this is a lexical heuristic. A
    response that restates the question in different words scores low, and a
    response that legitimately quotes a distinctive phrase scores high. It
    catches the common mechanical restatement — "You're asking whether X" — and
    nothing subtler. It is a signal for a human reading a diff, not a verdict.
    """
    opening = _ngrams(first_sentence(response), RESTATEMENT_NGRAM)
    if not opening:
        return 0.0
    source = _ngrams(prompt, RESTATEMENT_NGRAM)
    if not source:
        return 0.0
    return len(opening & source) / len(opening)


def list_ratio(text: str) -> tuple[float, int, int]:
    """(ratio, list_lines, non_empty_lines).

    Numerator: lines beginning with a bullet or an enumerator. Denominator:
    non-empty lines. A response with no non-empty lines has ratio 0.0 — an
    empty answer is not a list.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return 0.0, 0, 0
    listed = sum(1 for line in lines if _LIST_LINE.match(line))
    return listed / len(lines), listed, len(lines)


# --------------------------------------------------------------------------
# The nine checks
# --------------------------------------------------------------------------


def _forbidden_phrases(spec: CheckSpec, response: str, prompt: str) -> CheckResult:
    lowered = response.lower()
    matched = [p for p in spec.phrases if p.lower() in lowered]
    return CheckResult(
        type=spec.type,
        status=CheckStatus.FAIL if matched else CheckStatus.PASS,
        observed={"matched": matched},
        expected={"none_of": list(spec.phrases)},
        note="case-insensitive substring match",
    )


def _required_phrases(spec: CheckSpec, response: str, prompt: str) -> CheckResult:
    lowered = response.lower()
    absent = [p for p in spec.phrases if p.lower() not in lowered]
    return CheckResult(
        type=spec.type,
        status=CheckStatus.FAIL if absent else CheckStatus.PASS,
        observed={"absent": absent},
        expected={"all_of": list(spec.phrases)},
        note="case-insensitive substring match",
    )


def _no_evaluative_opener(spec: CheckSpec, response: str, prompt: str) -> CheckResult:
    window = response.strip()[:OPENER_WINDOW].lower()
    matched = [opener for opener in EVALUATIVE_OPENERS if opener in window]
    return CheckResult(
        type=spec.type,
        status=CheckStatus.FAIL if matched else CheckStatus.PASS,
        observed={"matched": matched, "window": window},
        expected={"no_opener_within_chars": OPENER_WINDOW},
    )


def _no_prompt_restatement(spec: CheckSpec, response: str, prompt: str) -> CheckResult:
    threshold = spec.threshold if spec.threshold is not None else RESTATEMENT_DEFAULT_THRESHOLD
    overlap = restatement_overlap(response, prompt)
    return CheckResult(
        type=spec.type,
        status=CheckStatus.FAIL if overlap > threshold else CheckStatus.PASS,
        observed={"trigram_overlap": round(overlap, 4),
                  "first_sentence": first_sentence(response)},
        expected={"max_overlap": threshold, "ngram": RESTATEMENT_NGRAM},
        note="lexical heuristic; see restatement_overlap for its limits",
    )


def _regex_absent(spec: CheckSpec, response: str, prompt: str) -> CheckResult:
    pattern = re.compile(spec.pattern or "", re.IGNORECASE | re.MULTILINE)
    found = [m.group(0) for m in pattern.finditer(response)]
    return CheckResult(
        type=spec.type,
        status=CheckStatus.FAIL if found else CheckStatus.PASS,
        observed={"matches": found[:5], "match_count": len(found)},
        expected={"absent_pattern": spec.pattern},
    )


def _regex_present(spec: CheckSpec, response: str, prompt: str) -> CheckResult:
    pattern = re.compile(spec.pattern or "", re.IGNORECASE | re.MULTILINE)
    found = [m.group(0) for m in pattern.finditer(response)]
    return CheckResult(
        type=spec.type,
        status=CheckStatus.PASS if found else CheckStatus.FAIL,
        observed={"matches": found[:5], "match_count": len(found)},
        expected={"present_pattern": spec.pattern},
    )


def _max_words(spec: CheckSpec, response: str, prompt: str) -> CheckResult:
    counted = word_count(response)
    limit = int(spec.value or 0)
    return CheckResult(
        type=spec.type,
        status=CheckStatus.FAIL if counted > limit else CheckStatus.PASS,
        observed={"words": counted},
        expected={"max_words": limit},
    )


def _list_ratio_below(spec: CheckSpec, response: str, prompt: str) -> CheckResult:
    ratio, listed, lines = list_ratio(response)
    limit = float(spec.value if spec.value is not None else 1.0)
    return CheckResult(
        type=spec.type,
        # Strictly below: a ratio exactly at the limit is not "below" it.
        status=CheckStatus.PASS if ratio < limit else CheckStatus.FAIL,
        observed={"ratio": round(ratio, 4), "list_lines": listed, "non_empty_lines": lines},
        expected={"ratio_below": limit},
    )


def _manual(spec: CheckSpec, response: str, prompt: str) -> CheckResult:
    """Records for human review. Never grades — that is the whole point."""
    return CheckResult(
        type=spec.type,
        status=CheckStatus.RECORDED,
        observed={"response": response},
        expected={"rubric": spec.rubric},
        note="recorded for human review; no automatic grade",
    )


_DISPATCH: dict[CheckType, Callable[[CheckSpec, str, str], CheckResult]] = {
    CheckType.FORBIDDEN_PHRASES: _forbidden_phrases,
    CheckType.REQUIRED_PHRASES: _required_phrases,
    CheckType.NO_EVALUATIVE_OPENER: _no_evaluative_opener,
    CheckType.NO_PROMPT_RESTATEMENT: _no_prompt_restatement,
    CheckType.REGEX_ABSENT: _regex_absent,
    CheckType.REGEX_PRESENT: _regex_present,
    CheckType.MAX_WORDS: _max_words,
    CheckType.LIST_RATIO_BELOW: _list_ratio_below,
    CheckType.MANUAL: _manual,
}

#: Every frozen type has an implementation, asserted at import and by test.
assert set(_DISPATCH) == set(CheckType), "a frozen check type has no implementation"


def run_check(spec: CheckSpec, response: str, prompt: str) -> CheckResult:
    return _DISPATCH[spec.type](spec, response, prompt)


def run_checks(specs: tuple[CheckSpec, ...], response: str, prompt: str) -> list[CheckResult]:
    return [run_check(spec, response, prompt) for spec in specs]
