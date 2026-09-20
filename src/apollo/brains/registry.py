"""Alias resolution and construction. Code refers to aliases, never model names."""

from __future__ import annotations

import pathlib

from apollo.brains.base import Brain
from apollo.brains.fake import FakeBrain
from apollo.config import Config, ProviderConfig
from apollo.errors import ConfigError


class BrainRegistry:
    """Builds adapters from configuration. Holds no Apollo state."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._cache: dict[str, Brain] = {}

    def provider_for(self, alias: str) -> ProviderConfig:
        return self._config.provider_for_alias(alias)

    def get(self, alias: str) -> Brain:
        if alias not in self._cache:
            self._cache[alias] = self._build(self.provider_for(alias))
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
        # `openai_compatible` arrives at step 8. Refusing loudly is better than
        # a stub that appears to work.
        raise ConfigError(
            f"provider kind {provider.kind!r} is not implemented in this milestone"
        )
