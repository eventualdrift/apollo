"""The lifecycle transition rules, checked exhaustively (spec D.1 as clarified by D.9)."""

from __future__ import annotations

import itertools

import pytest

from apollo.memory.lifecycle import (
    CREATED_STATUS,
    REQUIRES,
    RESULTS,
    ROW_TRANSITIONS,
    LifecycleError,
    Operation,
    check,
    check_chain_tombstone,
)
from apollo.memory.models import Status

A, S, R, T = Status.ACTIVE, Status.SUPERSEDED, Status.ARCHIVED, Status.TOMBSTONED

#: The whole table, written out independently of the module under test.
EXPECTED = {
    (Operation.CONFIRM, A): A,
    (Operation.CONFIRM, R): R,
    (Operation.CONTRADICT, A): A,
    (Operation.CONTRADICT, R): R,
    (Operation.CORRECT, A): S,
    (Operation.ARCHIVE, A): R,
    (Operation.RESTORE, R): A,
    (Operation.TOMBSTONE, A): T,
    (Operation.TOMBSTONE, R): T,  # D.9/2
}

OPERATIONS = [op for op in Operation if op is not Operation.CREATE]


@pytest.mark.parametrize(("operation", "status"), list(itertools.product(OPERATIONS, Status)))
def test_every_operation_and_status(operation: Operation, status: Status) -> None:
    if (operation, status) in EXPECTED:
        assert check(operation, status) is EXPECTED[(operation, status)]
    else:
        with pytest.raises(LifecycleError):
            check(operation, status)


def test_row_transitions_are_exactly_d1_plus_d9() -> None:
    expected = {
        (A, S),
        (A, R),
        (R, A),
        (A, T),
        (R, T),
        (S, T),  # only as part of a chain tombstone, D.9/1
    }
    assert expected == ROW_TRANSITIONS


def test_every_status_changing_operation_is_a_permitted_row_transition() -> None:
    for (operation, status), result in EXPECTED.items():
        if result is not status:
            assert (status, result) in ROW_TRANSITIONS, operation


def test_tombstoned_is_terminal() -> None:
    assert not any(src is T for src, _ in ROW_TRANSITIONS)
    for operation in OPERATIONS:
        with pytest.raises(LifecycleError):
            check(operation, T)


def test_a_superseded_row_changes_only_by_chain_tombstone() -> None:
    assert {dst for src, dst in ROW_TRANSITIONS if src is S} == {T}
    for operation in OPERATIONS:
        with pytest.raises(LifecycleError):
            check(operation, S)


def test_rows_are_created_active_and_tables_cover_every_operation() -> None:
    assert CREATED_STATUS is A
    assert set(REQUIRES) == set(RESULTS) == set(OPERATIONS)
    with pytest.raises(LifecycleError):
        check(Operation.CREATE, A)


@pytest.mark.parametrize("head", [A, R])
def test_a_chain_tombstone_accepts_an_active_or_archived_head(head: Status) -> None:
    check_chain_tombstone(head, [S, S])
    check_chain_tombstone(head, [])


@pytest.mark.parametrize("head", [S, T])
def test_a_chain_tombstone_needs_a_live_head(head: Status) -> None:
    with pytest.raises(LifecycleError):
        check_chain_tombstone(head, [])


@pytest.mark.parametrize("bad", [A, R, T])
def test_every_predecessor_must_be_superseded(bad: Status) -> None:
    with pytest.raises(LifecycleError):
        check_chain_tombstone(A, [S, bad])


def test_refusals_name_statuses_not_content() -> None:
    with pytest.raises(LifecycleError) as exc:
        check(Operation.RESTORE, A)
    assert str(exc.value) == "cannot restore a memory that is active; it must be archived"
