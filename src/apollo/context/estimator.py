"""Token estimation. Core owns budget *policy*; adapters own *counting* (ADR-0002).

The conservative fallback deliberately assumes three characters per token
rather than four: over-estimating truncates a little more history,
under-estimating overflows the model's context mid-conversation. Bias toward
the recoverable error.
"""

from __future__ import annotations

import math
from typing import Protocol, runtime_checkable


@runtime_checkable
class TokenEstimator(Protocol):
    name: str
    is_exact: bool

    def count(self, text: str) -> int: ...


class ConservativeEstimator:
    """`conservative-v1`. Model-agnostic, so Core never learns a tokenisation rule.

    Eval runs pin this estimator for every brain so bundles are byte-identical
    across brains and behavioural differences are attributable to the model
    (spec F.5).
    """

    name = "conservative-v1"
    is_exact = False

    def count(self, text: str) -> int:
        return math.ceil(len(text) / 3.0)


CONSERVATIVE = ConservativeEstimator()
