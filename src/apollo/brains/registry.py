"""Alias resolution and construction. Code refers to aliases, never model names.

A registry is bound to the surface that owns it (spec K.2, mechanism 1). An
interactive registry cannot construct an `eval_only` provider at all, which is
independent of the mode check in `core/policy.py`: two mechanisms that fail
separately, rather than one flag whose bypass defeats everything.
"""

from __future__ import annotations

import pathlib

from apollo.brains.base import Brain
from apollo.brains.fake import FakeBrain
from apollo.brains.openai_compatible import OpenAICompatibleBrain
from apollo.config import SURFACE_INTERACTIVE, SURFACES, Config, ProviderConfig
from apollo.errors import ConfigError, ErrorKind, PolicyRefusedError


class BrainRegistry:
    """Builds adapters from configuration. Holds no Apollo state."""

    def __init__(self, config: Config, *, surface: str = SURFACE_INTERACTIVE) -> None:
        if surface not in SURFACES:
            raise ConfigError(f"unknown surface {surface!r}")
        self._config = config
        self._surface = surface
        self._cache: dict[str, Brain] = {}

    @property
    def surface(self) -> str:
        return self._surface

    def provider_for(self, alias: str) -> ProviderConfig:
        return self._config.provider_for_alias(alias)

    def get(self, alias: str) -> Brain:
        """Resolve an alias, refusing an eval-only provider on a non-eval surface."""
        provider = self.provider_for(alias)
        if not provider.resolvable_from(self._surface):
            raise PolicyRefusedError(
                ErrorKind.BRAIN_NOT_INTERACTIVE,
                f"provider={provider.key} is eval-only and unreachable from {self._surface}",
            )
        if alias not in self._cache:
            self._cache[alias] = self._build(provider)
        return self._cache[alias]

    def _build(self, provider: ProviderConfig) -> Brain:
        if provider.kind == "fake":
            replay_dir = provider.options.get("replay_dir")
            return FakeBrain(
                key=provider.key,
                mode=str(provider.options.get("mode", "echo")),
                max_context=provider.context_budget + provider.reserved_output,
                replay_dir=pathlib.Path(replay_dir) if replay_dir else None,
            )
        if provider.kind == "openai_compatible":
            return OpenAICompatibleBrain.from_config(provider)
        raise ConfigError(f"unknown provider kind {provider.kind!r}")
