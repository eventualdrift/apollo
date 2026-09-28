"""Budget allocation: fixed floors and caps, not fill-until-full (spec F.5).

Two rules are hard:

1. Identity is never sacrificed. If the policy region exceeds its cap,
   compilation raises rather than truncating — silently trimming identity is
   silently changing who Apollo is, and it would happen exactly during long
   interesting conversations.
2. If the conversation floor cannot be met, the turn fails with
   `context_overflow` rather than producing a degraded turn that looks normal.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from apollo.errors import ContextOverflowError, IdentityOverflowError

#: Share of the budget the memory region may occupy.
MEMORY_CAP_FRACTION = 0.25
#: The conversation region must fit at least this many messages (two exchanges).
CONVERSATION_FLOOR_MESSAGES = 4


@dataclass(frozen=True)
class Budget:
    total: int
    identity_cap: int

    @classmethod
    def for_provider(
        cls, *, context_budget: int, max_context: int, reserved_output: int, identity_cap: int
    ) -> Budget:
        total = min(context_budget, max_context - reserved_output)
        if total <= 0:
            raise ContextOverflowError("configured budget leaves no room for context")
        return cls(total=total, identity_cap=identity_cap)

    @property
    def memory_cap(self) -> int:
        return int(self.total * MEMORY_CAP_FRACTION)


def check_policy_region(policy_tokens: int, budget: Budget) -> None:
    if policy_tokens > budget.identity_cap:
        raise IdentityOverflowError(
            f"policy region is {policy_tokens} tokens, cap is {budget.identity_cap}; "
            "identity is never truncated"
        )


def conversation_allowance(
    *, budget: Budget, policy_tokens: int, request_tokens: int, data_tokens: int
) -> int:
    """Whatever remains after the reserved and capped regions are placed."""
    remaining = budget.total - policy_tokens - request_tokens - data_tokens
    if remaining < 0:
        raise ContextOverflowError(
            "identity, request and data regions exceed the context budget"
        )
    return remaining


def assert_conversation_floor(
    kept: Sequence[object], available: Sequence[object]
) -> None:
    """A degraded turn that looks normal is worse than an honest failure.

    `kept` and `available` identify history messages, oldest first. The floor
    is the newest `CONVERSATION_FLOOR_MESSAGES` of them (the last two
    exchanges): each must actually be in `kept`, in order at its end. Counting
    kept messages is not enough, since a count can be met by older ones while
    a recent one is missing. A conversation with no history yet is not a floor
    violation; a conversation whose recent history could not be fitted is
    exactly the case this guards.
    """
    required = min(CONVERSATION_FLOOR_MESSAGES, len(available))
    if required == 0:
        return
    if list(kept[-required:]) != list(available[-required:]):
        raise ContextOverflowError(
            f"conversation floor not met: the newest {required} history messages"
            " do not all fit"
        )
