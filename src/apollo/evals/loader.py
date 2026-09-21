"""Strict loading and validation of persona case files.

A broken fixture is never silently reinterpreted. The corpus *is* the
experiment: a case that loads with a quietly-defaulted field measures
something nobody specified.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

import yaml

from apollo.config import MODE_BENCHMARK
from apollo.errors import ApolloError
from apollo.evals.models import CheckSpec, CheckType, HistoryTurn, PersonaCase

#: Roles a persona fixture may use. `system_note` is not dialogue and the
#: compiler rejects it anyway (spec F.2); rejecting it here names the mistake
#: at load time instead of mid-run.
VALID_HISTORY_ROLES = ("user", "apollo")

CASE_FIELDS = {
    "id", "tags", "behavioural_expectation", "undesired_characteristics",
    "setup", "input", "checks", "covers_rules", "covers_probes",
}
SETUP_FIELDS = {"mode", "memories", "history"}
CHECK_FIELDS = {"type", "phrases", "pattern", "value", "rubric", "threshold"}

#: Which keys each check type may carry, and which it must.
CHECK_SCHEMA: dict[CheckType, tuple[set[str], set[str]]] = {
    CheckType.FORBIDDEN_PHRASES: ({"phrases"}, set()),
    CheckType.REQUIRED_PHRASES: ({"phrases"}, set()),
    CheckType.NO_EVALUATIVE_OPENER: (set(), set()),
    CheckType.NO_PROMPT_RESTATEMENT: (set(), {"threshold"}),
    CheckType.REGEX_ABSENT: ({"pattern"}, set()),
    CheckType.REGEX_PRESENT: ({"pattern"}, set()),
    CheckType.MAX_WORDS: ({"value"}, set()),
    CheckType.LIST_RATIO_BELOW: ({"value"}, set()),
    CheckType.MANUAL: ({"rubric"}, set()),
}

RULE_PATTERN = re.compile(r"^B([1-9]|1\d|2[0-5])$")


class CaseValidationError(ApolloError):
    """A fixture is malformed. It is never repaired by guessing."""


def load_cases(directory: pathlib.Path) -> list[PersonaCase]:
    """Load every case file in `directory`, validated, with unique ids."""
    cases: list[PersonaCase] = []
    seen: dict[str, str] = {}
    for path in sorted(directory.glob("*.yaml")):
        for case in load_case_file(path):
            if case.id in seen:
                raise CaseValidationError(
                    f"duplicate case id {case.id!r} in {path.name}; "
                    f"already defined in {seen[case.id]}"
                )
            seen[case.id] = path.name
            cases.append(case)
    if not cases:
        raise CaseValidationError(f"no persona cases found in {directory}")
    return cases


def load_case_file(path: pathlib.Path) -> list[PersonaCase]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise CaseValidationError(f"{path.name}: a case file must be a mapping")
    unknown = set(document) - {"version", "cases"}
    if unknown:
        raise CaseValidationError(f"{path.name}: unknown top-level keys {sorted(unknown)}")
    if document.get("version") != 1:
        raise CaseValidationError(f"{path.name}: version must be 1")
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise CaseValidationError(f"{path.name}: `cases` must be a non-empty list")
    return [_parse_case(raw, path) for raw in raw_cases]


def _parse_case(raw: Any, path: pathlib.Path) -> PersonaCase:
    where = path.name
    if not isinstance(raw, dict):
        raise CaseValidationError(f"{where}: each case must be a mapping")
    case_id = raw.get("id")
    if not isinstance(case_id, str) or not case_id.strip():
        raise CaseValidationError(f"{where}: every case needs a non-empty string id")
    where = f"{path.name}:{case_id}"

    unknown = set(raw) - CASE_FIELDS
    if unknown:
        raise CaseValidationError(f"{where}: unknown case fields {sorted(unknown)}")

    user_input = raw.get("input")
    if not isinstance(user_input, str) or not user_input.strip():
        raise CaseValidationError(f"{where}: `input` is required and must be non-empty")

    expectation = raw.get("behavioural_expectation")
    if not isinstance(expectation, str) or not expectation.strip():
        raise CaseValidationError(f"{where}: `behavioural_expectation` is required")

    setup = raw.get("setup", {})
    if not isinstance(setup, dict):
        raise CaseValidationError(f"{where}: `setup` must be a mapping")
    unknown_setup = set(setup) - SETUP_FIELDS
    if unknown_setup:
        raise CaseValidationError(f"{where}: unknown setup fields {sorted(unknown_setup)}")

    mode = setup.get("mode", MODE_BENCHMARK)
    if mode != MODE_BENCHMARK:
        raise CaseValidationError(
            f"{where}: persona cases run in benchmark mode only, got {mode!r}. "
            "A personal-mode persona case would put real conversation state into a "
            "benchmark, which is exactly what the mode separation prevents."
        )

    memories = setup.get("memories", [])
    if memories:
        raise CaseValidationError(
            f"{where}: memory fixtures are not available at this milestone. "
            "Retrieval arrives in M3; inventing MEMORY blocks early would measure "
            "something that does not exist yet."
        )

    history = _parse_history(setup.get("history", []), where)
    checks = _parse_checks(raw.get("checks", []), where)
    rules = _parse_rules(raw.get("covers_rules", []), where)
    probes = tuple(_string_list(raw.get("covers_probes", []), where, "covers_probes"))

    return PersonaCase(
        id=case_id,
        tags=tuple(_string_list(raw.get("tags", []), where, "tags")),
        behavioural_expectation=expectation.strip(),
        undesired_characteristics=tuple(
            _string_list(raw.get("undesired_characteristics", []), where,
                         "undesired_characteristics")
        ),
        mode=mode,
        history=history,
        input=user_input,
        checks=checks,
        covers_rules=rules,
        covers_probes=probes,
        source_file=path.name,
    )


def _parse_history(raw: Any, where: str) -> tuple[HistoryTurn, ...]:
    if not isinstance(raw, list):
        raise CaseValidationError(f"{where}: `history` must be a list")
    turns: list[HistoryTurn] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) - {"role", "content"}:
            raise CaseValidationError(
                f"{where}: history[{index}] must be a mapping of role and content"
            )
        role, content = item.get("role"), item.get("content")
        if role not in VALID_HISTORY_ROLES:
            raise CaseValidationError(
                f"{where}: history[{index}] role {role!r} is not one of "
                f"{list(VALID_HISTORY_ROLES)}"
            )
        if not isinstance(content, str) or not content:
            raise CaseValidationError(f"{where}: history[{index}] needs non-empty content")
        turns.append(HistoryTurn(role=role, content=content))
    return tuple(turns)


def _parse_checks(raw: Any, where: str) -> tuple[CheckSpec, ...]:
    if not isinstance(raw, list) or not raw:
        raise CaseValidationError(f"{where}: at least one check is required")
    specs: list[CheckSpec] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise CaseValidationError(f"{where}: checks[{index}] must be a mapping")
        unknown = set(item) - CHECK_FIELDS
        if unknown:
            raise CaseValidationError(f"{where}: checks[{index}] unknown fields {sorted(unknown)}")
        raw_type = item.get("type")
        try:
            check_type = CheckType(str(raw_type))
        except ValueError:
            raise CaseValidationError(
                f"{where}: checks[{index}] unknown check type {raw_type!r}; "
                f"known types are {[str(t) for t in CheckType]}"
            ) from None

        required, optional = CHECK_SCHEMA[check_type]
        present = set(item) - {"type"}
        missing = required - present
        if missing:
            raise CaseValidationError(
                f"{where}: checks[{index}] ({check_type}) is missing {sorted(missing)}"
            )
        extra = present - required - optional
        if extra:
            raise CaseValidationError(
                f"{where}: checks[{index}] ({check_type}) does not take {sorted(extra)}"
            )
        specs.append(_build_spec(check_type, item, where, index))
    return tuple(specs)


def _build_spec(check_type: CheckType, item: dict[str, Any], where: str, index: int) -> CheckSpec:
    phrases: tuple[str, ...] = ()
    if "phrases" in item:
        listed = _string_list(item["phrases"], where, f"checks[{index}].phrases")
        if not listed:
            raise CaseValidationError(f"{where}: checks[{index}] needs at least one phrase")
        phrases = tuple(listed)

    pattern = item.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str) or not pattern:
            raise CaseValidationError(
                f"{where}: checks[{index}] pattern must be a non-empty string"
            )
        try:
            # Compiled at load so an invalid expression fails before any model runs.
            re.compile(pattern)
        except re.error as exc:
            raise CaseValidationError(
                f"{where}: checks[{index}] pattern is not a valid regex ({exc})"
            ) from None

    value = item.get("value")
    if value is not None and not isinstance(value, int | float) or isinstance(value, bool):
        raise CaseValidationError(f"{where}: checks[{index}] value must be a number")
    if check_type is CheckType.MAX_WORDS and value is not None and value <= 0:
        raise CaseValidationError(f"{where}: checks[{index}] max_words must be positive")
    if check_type is CheckType.LIST_RATIO_BELOW and value is not None and not 0 < value <= 1:
        raise CaseValidationError(
            f"{where}: checks[{index}] list_ratio_below value must be in (0, 1]"
        )

    rubric = item.get("rubric")
    if check_type is CheckType.MANUAL and (not isinstance(rubric, str) or not rubric.strip()):
        raise CaseValidationError(f"{where}: checks[{index}] manual needs a non-empty rubric")

    threshold = item.get("threshold")
    if threshold is not None and (not isinstance(threshold, int | float)
                                  or not 0 <= threshold <= 1):
        raise CaseValidationError(f"{where}: checks[{index}] threshold must be in [0, 1]")

    return CheckSpec(
        type=check_type,
        phrases=phrases,
        pattern=pattern,
        value=float(value) if value is not None else None,
        rubric=rubric,
        threshold=float(threshold) if threshold is not None else None,
    )


def _parse_rules(raw: Any, where: str) -> tuple[str, ...]:
    rules = _string_list(raw, where, "covers_rules")
    for rule in rules:
        if not RULE_PATTERN.match(rule):
            raise CaseValidationError(
                f"{where}: covers_rules entry {rule!r} is not a behavioural rule id (B1..B25)"
            )
    return tuple(rules)


def _string_list(raw: Any, where: str, field: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise CaseValidationError(f"{where}: `{field}` must be a list of strings")
    return [item.strip() for item in raw if item.strip()]
