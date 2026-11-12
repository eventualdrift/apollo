"""The retrieval regression suite (spec E.5, J.4): fixtures, runner and metrics.

Retrieval runs with no model, so the suite is fully deterministic and runs in
CI on every commit. Each case file carries its own fixture, loaded with
`origin = 'fixture'` into a database of its own; its cases then run against
exactly that memory store.

Loading is strict, as for the persona corpus: a malformed fixture is never
repaired by guessing, because the corpus is the experiment.

What a case checks, against the merged list retrieval hands to the compiler:
- every `must_retrieve` memory is present, at rank `max_rank` or better;
- no `must_not_retrieve` memory is present anywhere, recency fallback
  included (a superseded or archived row must never reach context);
- `substantive_support` equals `substantive_match_expected`, where given.

Metrics (spec E.5), over the *support* list: pinned and lexical results above
the floor, in merged order. Recency is a fallback, not a match, so it is kept
out of precision and recall.
- `recall@5`: per case with `must_retrieve`, the share found in the top 5.
- `precision@5`: per case with any support, the share of the top 5 that is in
  `must_retrieve` or `may_retrieve`.
- `mean_rank_of_expected`: the mean merged rank of the `must_retrieve`
  memories that were found.
- `correct_empty_rate`: of the cases expecting no substantive match, the
  share where retrieval reported none. The direct defence against fabricated
  memory.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import yaml

from apollo.errors import ApolloError
from apollo.memory import lifecycle
from apollo.memory.models import Kind, Origin, OriginTier, Scope, SourceKind
from apollo.memory.retrieval import (
    LEXICAL,
    PINNED,
    RetrievalOutcome,
    RetrievalQuery,
    RetrievalSettings,
    retrieve,
)
from apollo.storage.db import Database
from apollo.storage.unit_of_work import unit_of_work

#: The nine case types spec E.5 requires at least one of.
CASE_TYPES = (
    "direct_fact",
    "preference",
    "correction",
    "archived_exclusion",
    "ambiguous",
    "unrelated_recent",
    "no_result",
    "multiple_relevant",
    "recency_sensitive",
)
#: Tuning picks the floor and configuration; held-out cases judge the choice.
SPLITS = ("tune", "holdout")
FIXTURE_STATUSES = ("active", "superseded", "archived", "tombstoned")

FILE_FIELDS = {"version", "fixture", "cases"}
MEMORY_FIELDS = {"ref", "scope", "kind", "subject", "content", "origin_tier", "status",
                 "pinned", "corrects", "observations"}
CASE_FIELDS = {"id", "type", "split", "description", "query", "previous", "must_retrieve",
               "may_retrieve", "must_not_retrieve", "max_rank", "substantive_match_expected"}

#: Fixture memories are created a minute apart from here, in listed order, so
#: "later in the file" means "more recent" and recency is deterministic.
FIXTURE_EPOCH = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
TOP = 5


class RetrievalFixtureError(ApolloError):
    """A retrieval fixture is malformed. It is never repaired by guessing."""


@dataclass(frozen=True)
class FixtureMemory:
    ref: str
    scope: Scope
    kind: Kind
    subject: str
    content: str
    origin_tier: OriginTier
    status: str
    pinned: bool
    corrects: str | None


@dataclass(frozen=True)
class RetrievalCase:
    id: str
    type: str
    split: str
    description: str
    query: str
    previous: str | None
    must_retrieve: tuple[str, ...]
    may_retrieve: tuple[str, ...]
    must_not_retrieve: tuple[str, ...]
    max_rank: int
    substantive_match_expected: bool | None


@dataclass(frozen=True)
class CaseFile:
    name: str
    memories: tuple[FixtureMemory, ...]
    cases: tuple[RetrievalCase, ...]


# -- loading -------------------------------------------------------------------------


def load_case_files(directory: pathlib.Path) -> list[CaseFile]:
    files = [load_case_file(path) for path in sorted(directory.glob("*.yaml"))]
    if not files:
        raise RetrievalFixtureError(f"no retrieval case files in {directory}")
    ids = [case.id for f in files for case in f.cases]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise RetrievalFixtureError(f"duplicate case ids: {', '.join(duplicates)}")
    return files


def load_case_file(path: pathlib.Path) -> CaseFile:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    where = path.name
    _require_mapping(raw, where, FILE_FIELDS, required=FILE_FIELDS)
    if raw["version"] != 1:
        raise RetrievalFixtureError(f"{where}: unsupported version {raw['version']!r}")
    _require_mapping(raw["fixture"], f"{where}: fixture", {"memories"}, required={"memories"})
    memories = tuple(_memory(m, where) for m in _list(raw["fixture"]["memories"], where))
    refs = [m.ref for m in memories]
    if len(set(refs)) != len(refs):
        raise RetrievalFixtureError(f"{where}: duplicate memory refs")
    _check_corrections(memories, where)
    cases = tuple(_case(c, where, set(refs)) for c in _list(raw["cases"], where))
    return CaseFile(path.name, memories, cases)


def _memory(raw: Any, where: str) -> FixtureMemory:
    _require_mapping(raw, where, MEMORY_FIELDS,
                     required={"ref", "scope", "kind", "subject", "content", "origin_tier"})
    here = f"{where}: memory {_text(raw['ref'], where, 'ref')}"
    status = raw.get("status", "active")
    if status not in FIXTURE_STATUSES:
        raise RetrievalFixtureError(f"{here}: unknown status {status!r}")
    observations = raw.get("observations", [{"relation": "asserts",
                                             "source_kind": "user_direct_entry"}])
    if observations != [{"relation": "asserts", "source_kind": "user_direct_entry"}]:
        raise RetrievalFixtureError(
            f"{here}: fixtures support one direct `asserts` observation only"
        )
    try:
        return FixtureMemory(
            ref=raw["ref"], scope=Scope(_text(raw["scope"], here, "scope")),
            kind=Kind(_text(raw["kind"], here, "kind")),
            subject=_text(raw["subject"], here, "subject"),
            content=_text(raw["content"], here, "content"),
            origin_tier=OriginTier(_text(raw["origin_tier"], here, "origin_tier")),
            status=status,
            pinned=_bool(raw.get("pinned", False), here, "pinned"),
            corrects=_optional_text(raw.get("corrects"), here, "corrects"),
        )
    except ValueError as exc:
        raise RetrievalFixtureError(f"{here}: {exc}") from None


def _check_corrections(memories: tuple[FixtureMemory, ...], where: str) -> None:
    by_ref = {m.ref: m for m in memories}
    for index, memory in enumerate(memories):
        if memory.corrects is None:
            continue
        old = by_ref.get(memory.corrects)
        if old is None or memories.index(old) >= index:
            raise RetrievalFixtureError(
                f"{where}: {memory.ref} corrects {memory.corrects!r}, which must be listed first"
            )
        if old.status != "superseded":
            raise RetrievalFixtureError(
                f"{where}: {old.ref} is corrected by {memory.ref}, so its status is `superseded`"
            )
        # The lifecycle carries these forward from the corrected row (spec D.5);
        # a fixture that declares otherwise would not run as written.
        for name in ("scope", "kind", "subject", "origin_tier", "pinned"):
            if getattr(memory, name) != getattr(old, name):
                raise RetrievalFixtureError(
                    f"{where}: {memory.ref} corrects {old.ref}, so it must keep its {name}"
                )
    for memory in memories:
        if memory.status == "superseded" and not any(m.corrects == memory.ref for m in memories):
            raise RetrievalFixtureError(f"{where}: {memory.ref} is superseded by nothing")


def _case(raw: Any, where: str, refs: set[str]) -> RetrievalCase:
    _require_mapping(raw, where, CASE_FIELDS,
                     required={"id", "type", "split", "description", "query"})
    here = f"{where}: case {_text(raw['id'], where, 'id')}"
    if raw["type"] not in CASE_TYPES:
        raise RetrievalFixtureError(f"{here}: unknown type {raw['type']!r}")
    if raw["split"] not in SPLITS:
        raise RetrievalFixtureError(f"{here}: split must be one of {SPLITS}")
    lists = {}
    for name in ("must_retrieve", "may_retrieve", "must_not_retrieve"):
        given = raw.get(name, [])
        if not isinstance(given, list):
            # A blank, `false` or `{}` must not quietly become "expect nothing".
            raise RetrievalFixtureError(f"{here}: {name} must be a list")
        values = tuple(_text(v, here, f"each of {name}") for v in given)
        if len(set(values)) != len(values):
            # A repeat would count twice in recall's numerator and denominator.
            raise RetrievalFixtureError(f"{here}: {name} repeats a ref")
        unknown = sorted(set(values) - refs)
        if unknown:
            raise RetrievalFixtureError(f"{here}: {name} names unknown refs {unknown}")
        lists[name] = values
    overlap = set(lists["must_retrieve"]) & set(lists["must_not_retrieve"])
    if overlap:
        raise RetrievalFixtureError(f"{here}: both must and must-not: {sorted(overlap)}")
    overlap = set(lists["may_retrieve"]) & set(lists["must_not_retrieve"])
    if overlap:
        raise RetrievalFixtureError(f"{here}: both allowed and forbidden: {sorted(overlap)}")
    expected = raw.get("substantive_match_expected")
    if expected is not None:
        expected = _bool(expected, here, "substantive_match_expected")
    if lists["must_retrieve"] and expected is False:
        raise RetrievalFixtureError(f"{here}: must_retrieve with no substantive match expected")
    max_rank = raw.get("max_rank", TOP)
    if not isinstance(max_rank, int) or isinstance(max_rank, bool) or max_rank < 1:
        raise RetrievalFixtureError(f"{here}: max_rank must be a positive whole number")
    return RetrievalCase(
        id=raw["id"], type=raw["type"], split=raw["split"],
        description=_text(raw["description"], here, "description"),
        query=_text(raw["query"], here, "query"),
        previous=_optional_text(raw.get("previous"), here, "previous"),
        max_rank=max_rank, substantive_match_expected=expected, **lists,
    )


def _require_mapping(raw: Any, where: str, allowed: set[str], *, required: set[str]) -> None:
    if not isinstance(raw, dict):
        raise RetrievalFixtureError(f"{where}: expected a mapping")
    unknown = sorted(set(raw) - allowed)
    missing = sorted(required - set(raw))
    if unknown:
        raise RetrievalFixtureError(f"{where}: unknown fields {unknown}")
    if missing:
        raise RetrievalFixtureError(f"{where}: missing fields {missing}")


def _list(raw: Any, where: str) -> list[Any]:
    if not isinstance(raw, list) or not raw:
        raise RetrievalFixtureError(f"{where}: expected a non-empty list")
    return raw


def _text(raw: Any, where: str, name: str) -> str:
    """A field the corpus measures with must be text as written, never coerced."""
    if not isinstance(raw, str):
        raise RetrievalFixtureError(f"{where}: {name} must be text, not {type(raw).__name__}")
    return raw


def _optional_text(raw: Any, where: str, name: str) -> str | None:
    return None if raw is None else _text(raw, where, name)


def _bool(raw: Any, where: str, name: str) -> bool:
    if not isinstance(raw, bool):
        raise RetrievalFixtureError(f"{where}: {name} must be true or false")
    return raw


def corpus_digest(case_files: list[CaseFile]) -> str:
    """sha256 of the loaded corpus, canonically serialised.

    The CI baseline records it, so any change to a case or a memory (a
    loosened rank, a dropped expectation, a reworded query) fails until the
    baseline is regenerated on purpose: the suite cannot quietly get easier.
    """
    canonical = json.dumps(
        [dataclasses.asdict(f) for f in sorted(case_files, key=lambda f: f.name)],
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# -- loading a fixture into a database ---------------------------------------------------


def load_fixture(db: Database, case_file: CaseFile) -> dict[str, uuid.UUID]:
    """Create the file's memories, a minute apart in listed order. Returns ref -> id."""
    ids: dict[str, uuid.UUID] = {}
    for index, memory in enumerate(case_file.memories):
        now = FIXTURE_EPOCH + timedelta(minutes=index)
        with unit_of_work(db, expect_audit=False) as uow:
            if memory.corrects is not None:
                change = lifecycle.correct(uow, ids[memory.corrects], content=memory.content,
                                           source_kind=SourceKind.USER_DIRECT_ENTRY, now=now)
                assert change.replacement_id is not None
                ids[memory.ref] = change.replacement_id
            else:
                ids[memory.ref] = lifecycle.create(
                    uow, scope=memory.scope, kind=memory.kind, subject=memory.subject,
                    content=memory.content, origin_tier=memory.origin_tier,
                    origin=Origin.FIXTURE, pinned=memory.pinned,
                    source_kind=SourceKind.USER_DIRECT_ENTRY, now=now,
                ).memory_id
    end = FIXTURE_EPOCH + timedelta(minutes=len(case_file.memories))
    for memory in case_file.memories:
        with unit_of_work(db, expect_audit=False) as uow:
            if memory.status == "archived":
                lifecycle.archive(uow, ids[memory.ref], now=end)
            elif memory.status == "tombstoned":
                lifecycle.tombstone(uow, ids[memory.ref], now=end)
    return ids


# -- running ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseResult:
    case: RetrievalCase
    merged: tuple[str, ...]
    support: tuple[str, ...]
    substantive_support: bool
    failures: tuple[str, ...] = field(default=())

    @property
    def passed(self) -> bool:
        return not self.failures


def run_case(
    db: Database, case: RetrievalCase, ids: dict[str, uuid.UUID], settings: RetrievalSettings
) -> CaseResult:
    refs = {memory_id: ref for ref, memory_id in ids.items()}
    query = RetrievalQuery(current=case.query, previous=case.previous, origin=Origin.FIXTURE)
    with unit_of_work(db, expect_audit=False) as uow:
        outcome = retrieve(uow, query, settings)
    merged = tuple(refs[m.memory_id] for m in outcome.merged)
    support = _support(outcome, refs)

    failures: list[str] = []
    for ref in case.must_retrieve:
        if ref not in merged:
            failures.append(f"{ref} not retrieved")
        elif merged.index(ref) + 1 > case.max_rank:
            failures.append(f"{ref} at rank {merged.index(ref) + 1} > {case.max_rank}")
    failures += [f"{ref} retrieved but must not be" for ref in case.must_not_retrieve
                 if ref in merged]
    if (case.substantive_match_expected is not None
            and outcome.substantive_support != case.substantive_match_expected):
        failures.append(f"substantive_support {outcome.substantive_support}, expected "
                        f"{case.substantive_match_expected}")
    return CaseResult(case, merged, support, outcome.substantive_support, tuple(failures))


def _support(outcome: RetrievalOutcome, refs: dict[uuid.UUID, str]) -> tuple[str, ...]:
    """Pinned and lexical results in merged order: what counts as a match."""
    matched = {h.memory_id for r in outcome.results if r.strategy in (PINNED, LEXICAL)
               for h in r.hits}
    return tuple(refs[m.memory_id] for m in outcome.merged if m.memory_id in matched)


def run_file(
    db: Database, case_file: CaseFile, settings: tuple[RetrievalSettings, ...]
) -> dict[RetrievalSettings, list[CaseResult]]:
    """Load the file's fixture once, then run every case under each settings value."""
    ids = load_fixture(db, case_file)
    return {s: [run_case(db, case, ids, s) for case in case_file.cases] for s in settings}


# -- metrics ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Metrics:
    cases: int
    passed: int
    recall_at_5: float
    precision_at_5: float
    mean_rank_of_expected: float
    correct_empty_rate: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "cases": self.cases, "passed": self.passed,
            "recall@5": round(self.recall_at_5, 4),
            "precision@5": round(self.precision_at_5, 4),
            "mean_rank_of_expected": round(self.mean_rank_of_expected, 4),
            "correct_empty_rate": round(self.correct_empty_rate, 4),
        }


def metrics(results: list[CaseResult]) -> Metrics:
    recalls, precisions, ranks, empties = [], [], [], []
    for r in results:
        top = r.support[:TOP]
        if r.case.must_retrieve:
            recalls.append(sum(ref in top for ref in r.case.must_retrieve)
                           / len(r.case.must_retrieve))
            ranks += [r.merged.index(ref) + 1 for ref in r.case.must_retrieve
                      if ref in r.merged]
        if top:
            relevant = set(r.case.must_retrieve) | set(r.case.may_retrieve)
            precisions.append(sum(ref in relevant for ref in top) / len(top))
        if r.case.substantive_match_expected is False:
            empties.append(not r.substantive_support)
    return Metrics(
        cases=len(results),
        passed=sum(r.passed for r in results),
        recall_at_5=_mean(recalls),
        precision_at_5=_mean(precisions),
        mean_rank_of_expected=_mean(ranks),
        correct_empty_rate=_mean(empties),
    )


def _mean(values: list[Any]) -> float:
    return sum(values) / len(values) if values else 0.0
