"""TOML + environment configuration. No config framework.

Secrets are read from the environment in exactly one place (spec K.3): the
database DSN and the API bearer token never appear in a TOML file, and neither
is ever logged or written to the database.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apollo.errors import ConfigError

DEFAULT_CONFIG_PATH = Path("apollo.toml")

#: Conversation privacy modes (spec K.2).
MODE_PERSONAL = "personal"
MODE_BENCHMARK = "benchmark"
MODES = (MODE_PERSONAL, MODE_BENCHMARK)


@dataclass(frozen=True)
class ProviderConfig:
    """A provider binding. `allowed_modes` and `eval_only` are policy declarations."""

    key: str
    kind: str
    allowed_modes: tuple[str, ...]
    eval_only: bool
    context_budget: int
    reserved_output: int
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for mode in self.allowed_modes:
            if mode not in MODES:
                raise ConfigError(f"provider {self.key}: unknown mode {mode!r}")


@dataclass(frozen=True)
class Config:
    database_dsn: str
    providers: dict[str, ProviderConfig]
    brain_aliases: dict[str, str]
    log_level: str = "INFO"
    identity_dir: Path = Path("identity")
    #: Turns/invocations left `started` longer than this become `interrupted`.
    orphan_window_seconds: int = 900
    #: Hard cap on the policy region. Identity is never trimmed; it raises.
    identity_token_cap: int = 4000

    def provider_for_alias(self, alias: str) -> ProviderConfig:
        short = alias.removeprefix("brain.")
        try:
            provider_key = self.brain_aliases[short]
        except KeyError:
            raise ConfigError(f"unknown brain alias {alias!r}") from None
        try:
            return self.providers[provider_key]
        except KeyError:
            raise ConfigError(f"alias {alias!r} binds missing provider {provider_key!r}") from None


def load_config(path: Path | None = None, env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    path = path or Path(env.get("APOLLO_CONFIG", DEFAULT_CONFIG_PATH))
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))

    dsn = env.get("APOLLO_DATABASE_DSN")
    if not dsn:
        raise ConfigError("APOLLO_DATABASE_DSN is not set (secrets come from the environment)")

    providers: dict[str, ProviderConfig] = {}
    for key, spec in sorted(raw.get("providers", {}).items()):
        providers[key] = ProviderConfig(
            key=key,
            kind=_require(spec, "kind", key),
            allowed_modes=tuple(spec.get("allowed_modes", [MODE_PERSONAL, MODE_BENCHMARK])),
            eval_only=bool(spec.get("eval_only", False)),
            context_budget=int(spec.get("context_budget", 8000)),
            reserved_output=int(spec.get("reserved_output", 1024)),
            options={k: v for k, v in spec.items() if k not in _PROVIDER_KEYS},
        )
    if not providers:
        raise ConfigError("no providers configured")

    aliases = {str(k): str(v) for k, v in raw.get("brains", {}).items()}
    if "default" not in aliases:
        raise ConfigError("no brain.default alias configured")

    core = raw.get("core", {})
    return Config(
        database_dsn=dsn,
        providers=providers,
        brain_aliases=aliases,
        log_level=env.get("APOLLO_LOG_LEVEL", core.get("log_level", "INFO")),
        identity_dir=Path(core.get("identity_dir", "identity")),
        orphan_window_seconds=int(core.get("orphan_window_seconds", 900)),
        identity_token_cap=int(core.get("identity_token_cap", 4000)),
    )


_PROVIDER_KEYS = {"kind", "allowed_modes", "eval_only", "context_budget", "reserved_output"}


def _require(spec: dict[str, Any], key: str, owner: str) -> str:
    if key not in spec:
        raise ConfigError(f"provider {owner}: missing {key!r}")
    return str(spec[key])
