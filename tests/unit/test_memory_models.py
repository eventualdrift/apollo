"""Memory types, derived support and confidence, and the manifest ref contract (spec D.8, F.6)."""

from __future__ import annotations

import pathlib
import re
import uuid
from enum import StrEnum

import pytest

from apollo.memory.models import (
    MANIFEST_SOURCE_KIND,
    Kind,
    Memory,
    MemoryInputError,
    ObservationCounts,
    Origin,
    OriginTier,
    Relation,
    Scope,
    SourceKind,
    Status,
    Support,
    confidence,
    included_manifest_entry,
    manifest_source_ref,
    parse_kind,
    parse_scope,
    support,
    validate_claim,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
SCHEMA = (REPO / "src" / "apollo" / "storage" / "migrations" / "0001_initial.sql").read_text(
    encoding="utf-8"
)


def _check_values(constraint: str) -> set[str]:
    """The quoted values inside a named CHECK (... IN (...)) in 0001."""
    match = re.search(rf"CONSTRAINT {constraint}\s+CHECK \((.*?)\)\)", SCHEMA, re.S)
    assert match, f"{constraint} not found in 0001"
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


@pytest.mark.parametrize(
    ("enum", "constraint"),
    [
        (Scope, "memory_scope_ck"),
        (Kind, "memory_kind_ck"),
        (OriginTier, "memory_origin_tier_ck"),
        (Origin, "memory_origin_ck"),
        (Status, "memory_status_ck"),
        (Relation, "observation_relation_ck"),
        (SourceKind, "observation_source_ck"),
    ],
)
def test_enums_match_the_schema_exactly(enum: type[StrEnum], constraint: str) -> None:
    assert {member.value for member in enum} == _check_values(constraint)


@pytest.mark.parametrize(
    ("confirms", "contradicts", "expected"),
    [
        (0, 0, Support.ASSERTED),
        (1, 0, Support.CONFIRMED),
        (5, 0, Support.CONFIRMED),
        (0, 1, Support.CONTESTED),
        (3, 1, Support.CONTESTED),  # any contradiction wins
    ],
)
def test_support_is_derived_from_observations(
    confirms: int, contradicts: int, expected: Support
) -> None:
    assert support(ObservationCounts(confirms, contradicts)) is expected


@pytest.mark.parametrize(
    ("tier", "confirms", "contradicts", "expected"),
    [
        (OriginTier.USER_ASSERTED, 0, 0, 0.70),
        (OriginTier.CONNECTOR_IMPORTED, 0, 0, 0.50),
        (OriginTier.MODEL_INFERRED, 0, 0, 0.30),
        (OriginTier.USER_ASSERTED, 1, 0, 0.75),
        (OriginTier.USER_ASSERTED, 3, 0, 0.85),
        (OriginTier.USER_ASSERTED, 10, 0, 0.85),  # confirmations capped at +0.15
        (OriginTier.USER_ASSERTED, 0, 1, 0.45),
        (OriginTier.USER_ASSERTED, 3, 1, 0.60),
        (OriginTier.USER_ASSERTED, 0, 3, 0.05),  # floored, not negative
        (OriginTier.MODEL_INFERRED, 0, 1, 0.05),
        (OriginTier.MODEL_INFERRED, 3, 1, 0.20),
    ],
)
def test_confidence_follows_d8(
    tier: OriginTier, confirms: int, contradicts: int, expected: float
) -> None:
    assert confidence(tier, ObservationCounts(confirms, contradicts)) == expected


def test_negative_counts_are_refused() -> None:
    with pytest.raises(ValueError):
        ObservationCounts(confirms=-1)


def test_manifest_ref_is_the_canonical_uuid_string() -> None:
    memory_id = uuid.UUID("018F2C4E-0000-7000-8000-00000000ABCD")
    assert manifest_source_ref(memory_id) == "018f2c4e-0000-7000-8000-00000000abcd"
    assert manifest_source_ref(memory_id) == str(memory_id)


def test_manifest_ref_refuses_anything_but_a_uuid() -> None:
    with pytest.raises(TypeError):
        manifest_source_ref("018f2c4e-0000-7000-8000-00000000abcd")  # type: ignore[arg-type]


def test_included_entry_is_the_redaction_containment_pattern() -> None:
    memory_id = uuid.uuid4()
    assert included_manifest_entry(memory_id) == [
        {"source_kind": MANIFEST_SOURCE_KIND, "source_ref": str(memory_id), "included": True}
    ]
    assert MANIFEST_SOURCE_KIND == "memory"  # spec F.6


# ---------------------------------------------------------------------------
# input validation and the read-side view (step 10 PR 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subject,content,message",
    [
        ("", "c", "subject must be non-empty"),
        ("  \n", "c", "subject must be non-empty"),
        (None, "c", "subject must be non-empty"),
        ("s", "", "content must be non-empty"),
        ("s", 42, "content must be non-empty"),
        ("s\x00", "c", "subject cannot contain a NUL"),
        ("s", "secret\x00", "content cannot contain a NUL"),
    ],
)
def test_a_bad_claim_is_refused_without_repeating_it(
    subject: object, content: object, message: str
) -> None:
    with pytest.raises(MemoryInputError, match=message) as exc:
        validate_claim(subject, content)
    assert "secret" not in str(exc.value)


def test_a_good_claim_is_returned_unchanged() -> None:
    assert validate_claim(" door code ", "4123\n") == (" door code ", "4123\n")


def test_scope_and_kind_are_parsed_or_refused() -> None:
    assert parse_scope("self") is Scope.SELF
    assert parse_kind("event") is Kind.EVENT
    with pytest.raises(MemoryInputError, match="scope is one of self, user"):
        parse_scope("everyone")
    with pytest.raises(MemoryInputError, match="kind is one of fact"):
        parse_kind("feeling")


def test_the_view_derives_support_and_confidence_from_counts() -> None:
    row = {
        "id": uuid.uuid4(),
        "scope": "user",
        "kind": "fact",
        "subject": "s",
        "content": "c",
        "origin_tier": "user_asserted",
        "origin": "personal",
        "status": "active",
        "pinned": False,
        "created_at": None,
        "updated_at": None,
        "last_confirmed_at": None,
        "superseded_by_id": None,
        "archived_at": None,
        "tombstoned_at": None,
    }
    memory = Memory.from_row(row, {"asserts": 1, "confirms": 2, "contradicts": 1})
    assert memory.counts == ObservationCounts(confirms=2, contradicts=1)
    assert memory.support is Support.CONTESTED
    assert memory.confidence == 0.55
    assert Memory.from_row(row, {"asserts": 1}).support is Support.ASSERTED
