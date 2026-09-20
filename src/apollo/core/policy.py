"""Mode, `allowed_modes` and `eval_only` checks. Nothing else lives here.

A named module because it is the seed of the future action-permission engine
and should be easy to find (spec M). Checked *before* retrieval or generation,
and a mismatch is a hard refusal — never a silent downgrade (spec K.2).
"""

from __future__ import annotations

from apollo.config import ProviderConfig
from apollo.errors import ErrorKind, PolicyRefusedError

#: Where the request originated. An `eval_only` provider is unreachable from
#: the interactive surface; the eval runner (step 9) is the only other caller.
SURFACE_INTERACTIVE = "interactive"
SURFACE_EVAL = "eval"


def check_brain_permitted(
    *, provider: ProviderConfig, conversation_mode: str, surface: str
) -> None:
    """Raise unless this provider may serve this conversation from this surface."""
    if provider.eval_only and surface != SURFACE_EVAL:
        raise PolicyRefusedError(
            ErrorKind.BRAIN_NOT_INTERACTIVE,
            f"provider={provider.key} is eval-only and unreachable from {surface}",
        )
    if conversation_mode not in provider.allowed_modes:
        raise PolicyRefusedError(
            ErrorKind.BRAIN_MODE_NOT_PERMITTED,
            f"provider={provider.key} does not serve mode={conversation_mode}",
        )
