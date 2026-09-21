"""Mode, `allowed_modes` and `eval_only` checks. Nothing else lives here.

A named module because it is the seed of the future action-permission engine
and should be easy to find (spec M). Checked *before* retrieval or generation,
and a mismatch is a hard refusal — never a silent downgrade (spec K.2).
"""

from __future__ import annotations

from apollo.config import (
    SURFACE_EVAL,
    SURFACE_INTERACTIVE,
    SURFACES,
    ProviderConfig,
)
from apollo.errors import ErrorKind, PolicyRefusedError

__all__ = [
    "SURFACES",
    "SURFACE_EVAL",
    "SURFACE_INTERACTIVE",
    "check_brain_permitted",
    "check_mode_permitted",
    "check_surface_may_resolve",
]


def check_surface_may_resolve(*, provider: ProviderConfig, surface: str) -> None:
    """Mechanism 1 of spec K.2, on its own.

    The brain registry calls this when resolving an alias, so an `eval_only`
    provider cannot be constructed from the interactive surface at all —
    independently of the mode check below, which a caller might skip. Keeping
    the two as separate calls is the point: a single flag whose bypass defeats
    everything is not defence in depth.
    """
    if not provider.resolvable_from(surface):
        raise PolicyRefusedError(
            ErrorKind.BRAIN_NOT_INTERACTIVE,
            f"provider={provider.key} is eval-only and unreachable from {surface}",
        )


def check_mode_permitted(*, provider: ProviderConfig, conversation_mode: str) -> None:
    """Mechanism 2 of spec K.2, on its own. Checked before retrieval or generation."""
    if conversation_mode not in provider.allowed_modes:
        raise PolicyRefusedError(
            ErrorKind.BRAIN_MODE_NOT_PERMITTED,
            f"provider={provider.key} does not serve mode={conversation_mode}",
        )


def check_brain_permitted(
    *, provider: ProviderConfig, conversation_mode: str, surface: str
) -> None:
    """Both mechanisms, for the turn path. Each remains callable on its own."""
    check_surface_may_resolve(provider=provider, surface=surface)
    check_mode_permitted(provider=provider, conversation_mode=conversation_mode)
