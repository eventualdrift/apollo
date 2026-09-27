"""Memory types and the derivations that are never stored (spec C.7, C.8, D.8).

The enums mirror the closed value lists in the schema's CHECK constraints; a
unit test holds the two together, so neither can drift on its own.

Support and confidence are derived from observations at read time and never
persisted (ADR-0005). Nothing here touches the database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Scope(StrEnum):
    SELF = "self"
    USER = "user"
    RELATIONSHIP = "relationship"
    WORLD = "world"


class Kind(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    PERSON = "person"
    PROJECT = "project"
    DECISION = "decision"
    CONSTRAINT = "constraint"
    EVENT = "event"


class OriginTier(StrEnum):
    """Where the claim came from. Write-once; phase zero writes only `user_asserted`."""

    USER_ASSERTED = "user_asserted"
    MODEL_INFERRED = "model_inferred"
    CONNECTOR_IMPORTED = "connector_imported"


class Origin(StrEnum):
    """The benchmark-isolation mechanism (spec K.2)."""

    PERSONAL = "personal"
    FIXTURE = "fixture"


class Status(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"
    TOMBSTONED = "tombstoned"


class Relation(StrEnum):
    ASSERTS = "asserts"
    CONFIRMS = "confirms"
    CONTRADICTS = "contradicts"


class SourceKind(StrEnum):
    USER_MESSAGE = "user_message"
    USER_DIRECT_ENTRY = "user_direct_entry"
    MODEL_PROPOSAL_APPROVED = "model_proposal_approved"
    DOCUMENT = "document"
    CONNECTOR = "connector"


class Support(StrEnum):
    """Derived from observations, never stored (spec D.8)."""

    ASSERTED = "asserted"
    CONFIRMED = "confirmed"
    CONTESTED = "contested"


@dataclass(frozen=True)
class ObservationCounts:
    confirms: int = 0
    contradicts: int = 0

    def __post_init__(self) -> None:
        if self.confirms < 0 or self.contradicts < 0:
            raise ValueError("observation counts cannot be negative")


def support(counts: ObservationCounts) -> Support:
    """`contested` on any contradiction, `confirmed` on confirmation alone, else `asserted`."""
    if counts.contradicts >= 1:
        return Support.CONTESTED
    if counts.confirms >= 1:
        return Support.CONFIRMED
    return Support.ASSERTED


#: Arbitrary starting values (spec D.8, P.2): consistent, inspectable, derived
#: from evidence. Not persisted, so they can change without a migration.
CONFIDENCE_BASE = {
    OriginTier.USER_ASSERTED: 0.70,
    OriginTier.CONNECTOR_IMPORTED: 0.50,
    OriginTier.MODEL_INFERRED: 0.30,
}
CONFIRM_STEP = 0.05
CONFIRM_CAP = 0.15
CONTRADICT_STEP = 0.25
CONFIDENCE_FLOOR = 0.05


def confidence(origin_tier: OriginTier, counts: ObservationCounts) -> float:
    """Base by origin, plus capped confirmations, minus contradictions, floored.

    No time decay: age is displayed as a fact, not a score input. Rounded to two
    places so the displayed value is exactly the arithmetic, not float noise.
    """
    value = (
        CONFIDENCE_BASE[origin_tier]
        + min(CONFIRM_STEP * counts.confirms, CONFIRM_CAP)
        - CONTRADICT_STEP * counts.contradicts
    )
    return round(max(value, CONFIDENCE_FLOOR), 2)


#: How a MEMORY block names its source in a context manifest (spec F.6). The
#: step 11 compiler must emit exactly this, because tombstone hash redaction
#: (spec D.7) finds affected invocations by containment on these values. Both
#: sides use the helpers below so the contract cannot drift.
MANIFEST_SOURCE_KIND = "memory"


def manifest_source_ref(memory_id: uuid.UUID) -> str:
    """The canonical lowercase hyphenated form: `str(uuid.UUID)`."""
    if not isinstance(memory_id, uuid.UUID):
        raise TypeError("a memory manifest ref is built from a uuid.UUID")
    return str(memory_id)


def included_manifest_entry(memory_id: uuid.UUID) -> list[dict[str, Any]]:
    """The jsonb containment pattern for "this invocation's bundle included the memory".

    Only `included = true` entries matter: a dropped block never entered the
    bundle, so the bundle hash does not depend on it (spec D.7).
    """
    return [
        {
            "source_kind": MANIFEST_SOURCE_KIND,
            "source_ref": manifest_source_ref(memory_id),
            "included": True,
        }
    ]
