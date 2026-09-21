"""Typed records for persona cases, check results, runs and gates."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class CheckType(StrEnum):
    """Exactly the nine frozen check types (spec J.1). Closed."""

    FORBIDDEN_PHRASES = "forbidden_phrases"
    REQUIRED_PHRASES = "required_phrases"
    NO_EVALUATIVE_OPENER = "no_evaluative_opener"
    NO_PROMPT_RESTATEMENT = "no_prompt_restatement"
    REGEX_ABSENT = "regex_absent"
    REGEX_PRESENT = "regex_present"
    MAX_WORDS = "max_words"
    LIST_RATIO_BELOW = "list_ratio_below"
    MANUAL = "manual"


class CheckStatus(StrEnum):
    """`RECORDED` is not a grade.

    A `manual` check never fabricates a pass or a fail: it captures the rubric,
    the response and the sample index for a human to read. Collapsing that into
    a boolean would invent a judgement nobody made.
    """

    PASS = "pass"
    FAIL = "fail"
    RECORDED = "recorded"


#: Deterministic statuses. `RECORDED` is deliberately absent.
GRADED = (CheckStatus.PASS, CheckStatus.FAIL)


@dataclass(frozen=True)
class CheckSpec:
    """One check as declared in a case file, validated on load."""

    type: CheckType
    phrases: tuple[str, ...] = ()
    pattern: str | None = None
    value: float | None = None
    rubric: str | None = None
    threshold: float | None = None

    @property
    def is_manual(self) -> bool:
        return self.type is CheckType.MANUAL


@dataclass(frozen=True)
class CheckResult:
    """Enough detail to understand a failure without rerunning it."""

    type: CheckType
    status: CheckStatus
    #: What the check measured, e.g. matched phrases or a word count.
    observed: Any = None
    #: What it required, e.g. the forbidden list or the limit.
    expected: Any = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": str(self.type),
            "status": str(self.status),
            "observed": self.observed,
            "expected": self.expected,
            "note": self.note,
        }


@dataclass(frozen=True)
class HistoryTurn:
    role: str
    content: str


@dataclass(frozen=True)
class PersonaCase:
    """One benchmark case. Immutable; the corpus is the experiment."""

    id: str
    tags: tuple[str, ...]
    behavioural_expectation: str
    undesired_characteristics: tuple[str, ...]
    mode: str
    history: tuple[HistoryTurn, ...]
    input: str
    checks: tuple[CheckSpec, ...]
    #: Which behavioural rules this case is claimed to cover (B1..B25). The
    #: corpus validator proves every rule is represented; a case may not
    #: silently discharge an obligation it does not declare.
    covers_rules: tuple[str, ...] = ()
    #: Which of the sixteen required probes this case covers.
    covers_probes: tuple[str, ...] = ()
    source_file: str = ""

    @property
    def has_manual_check(self) -> bool:
        return any(c.is_manual for c in self.checks)

    def history_ref(self, index: int) -> str:
        """A deterministic source reference for fixture history.

        Fixture history is *not* a conversation: it belongs to the case, not to
        a message table. Referencing it deterministically is what keeps a
        case's `bundle_hash` identical across brains and samples, which is the
        precondition for comparing their behaviour at all (spec F.5).
        """
        return f"fixture:{self.id}:h{index}"

    @property
    def input_ref(self) -> str:
        return f"fixture:{self.id}:input"


@dataclass
class SampleRecord:
    """One generation: a case run once against one brain."""

    case_id: str
    sample_index: int
    status: str  # completed | failed
    response: str | None
    check_results: list[CheckResult] = field(default_factory=list)
    error_kind: str | None = None
    turn_id: uuid.UUID | None = None
    invocation_id: uuid.UUID | None = None
    bundle_hash: str | None = None
    model_identifier: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    latency_ms: int | None = None

    @property
    def deterministic_results(self) -> list[CheckResult]:
        return [r for r in self.check_results if r.status in GRADED]

    @property
    def deterministic_status(self) -> str:
        """`pass`, `fail`, or `no_deterministic_checks` — never invented."""
        graded = self.deterministic_results
        if not graded:
            return "no_deterministic_checks"
        failed = any(r.status is CheckStatus.FAIL for r in graded)
        return "fail" if failed else "pass"


@dataclass
class RunRecord:
    """Everything needed to understand a behavioural change months later."""

    run_id: str
    started_at: datetime
    brain_alias: str
    provider_key: str
    adapter_key: str
    render_version: str
    compiler_version: str
    token_estimator: str
    identity_version: str
    identity_hash: str
    generation_params: dict[str, Any]
    determinism: dict[str, Any]
    conversation_id: uuid.UUID | None = None
    cases: list[dict[str, Any]] = field(default_factory=list)
    suite_version: int = 1

    @property
    def filename(self) -> str:
        stamp = self.started_at.strftime("%Y%m%dT%H%M%SZ")
        return f"{stamp}-{self.brain_alias.replace('.', '_')}-{self.identity_hash[:8]}.json"
