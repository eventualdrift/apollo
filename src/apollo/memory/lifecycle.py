"""The memory lifecycle: which operation may apply to which status (spec D.1, D.9).

This module is where status transitions are decided. It holds the rules as
data so the database triggers (migration 0003) and the tests can be checked
against one table; the operations that execute them arrive with the
repository.

Row-level transitions, per spec D.1 as clarified by D.9:

    active     -> superseded   correct (a new active row replaces it)
    active     -> archived     archive
    archived   -> active       restore
    active     -> tombstoned   tombstone
    archived   -> tombstoned   tombstone (D.9/2)
    superseded -> tombstoned   only as part of a chain tombstone (D.9/1)

`tombstoned` is terminal. Confirm and contradict add evidence and change no
status; they apply to `active` and `archived` rows only.
"""

from __future__ import annotations

from enum import StrEnum

from apollo.errors import ApolloError
from apollo.memory.models import Status


class LifecycleError(ApolloError):
    """An operation that the lifecycle does not permit from the row's status."""


class Operation(StrEnum):
    CREATE = "create"
    CONFIRM = "confirm"
    CONTRADICT = "contradict"
    CORRECT = "correct"
    ARCHIVE = "archive"
    RESTORE = "restore"
    TOMBSTONE = "tombstone"


#: Every status change a row may undergo. The database trigger in 0003 permits
#: exactly this set; a test holds them together.
ROW_TRANSITIONS: frozenset[tuple[Status, Status]] = frozenset(
    {
        (Status.ACTIVE, Status.SUPERSEDED),
        (Status.ACTIVE, Status.ARCHIVED),
        (Status.ARCHIVED, Status.ACTIVE),
        (Status.ACTIVE, Status.TOMBSTONED),
        (Status.ARCHIVED, Status.TOMBSTONED),
        (Status.SUPERSEDED, Status.TOMBSTONED),
    }
)

#: The status a row must be in for an operation to be requested on it. For
#: `tombstone` this is the chain head; its superseded predecessors follow it.
REQUIRES: dict[Operation, frozenset[Status]] = {
    Operation.CONFIRM: frozenset({Status.ACTIVE, Status.ARCHIVED}),
    Operation.CONTRADICT: frozenset({Status.ACTIVE, Status.ARCHIVED}),
    Operation.CORRECT: frozenset({Status.ACTIVE}),
    Operation.ARCHIVE: frozenset({Status.ACTIVE}),
    Operation.RESTORE: frozenset({Status.ARCHIVED}),
    Operation.TOMBSTONE: frozenset({Status.ACTIVE, Status.ARCHIVED}),
}

#: What the named row becomes. `None`: its status does not change.
RESULTS: dict[Operation, Status | None] = {
    Operation.CONFIRM: None,
    Operation.CONTRADICT: None,
    Operation.CORRECT: Status.SUPERSEDED,
    Operation.ARCHIVE: Status.ARCHIVED,
    Operation.RESTORE: Status.ACTIVE,
    Operation.TOMBSTONE: Status.TOMBSTONED,
}

#: Every row is created active: a direct entry, an approved proposal, or the
#: replacement row of a correction.
CREATED_STATUS = Status.ACTIVE


def check(operation: Operation, status: Status) -> Status:
    """Return the row's status after `operation`, or raise if it is not permitted.

    The message names the operation and the statuses only: never the claim.
    """
    if operation is Operation.CREATE:
        raise LifecycleError("create applies to no existing row")
    allowed = REQUIRES[operation]
    if status not in allowed:
        raise LifecycleError(
            f"cannot {operation} a memory that is {status}; "
            f"it must be {' or '.join(sorted(allowed))}"
        )
    result = RESULTS[operation]
    return status if result is None else result


def check_chain_tombstone(head: Status, predecessors: list[Status]) -> None:
    """Validate a chain tombstone (spec D.9/1) before any row is touched.

    The head must be `active` or `archived`. Every predecessor must be
    `superseded`: that is what makes it a predecessor, so anything else means
    the chain is not what the caller thinks it is.
    """
    check(Operation.TOMBSTONE, head)
    for status in predecessors:
        if status is not Status.SUPERSEDED:
            raise LifecycleError(f"a chain predecessor must be {Status.SUPERSEDED}, not {status}")
