from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from apollo.brains.registry import BrainRegistry
from apollo.config import Config, ProviderConfig
from apollo.core.identity import IdentityLoader
from apollo.core.turns import TurnService

REPO = pathlib.Path(__file__).resolve().parents[2]
START = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


class Clock:
    """Deterministic, monotonic, and passed in — never read from the wall (spec F.4)."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def make_config(dsn: str, **provider_options) -> Config:
    provider = ProviderConfig(
        key="fake",
        kind="fake",
        allowed_modes=provider_options.pop("allowed_modes", ("personal", "benchmark")),
        eval_only=provider_options.pop("eval_only", False),
        context_budget=provider_options.pop("context_budget", 8000),
        reserved_output=provider_options.pop("reserved_output", 1024),
        options=provider_options,
    )
    return Config(
        database_dsn=dsn,
        providers={"fake": provider},
        brain_aliases={"default": "fake"},
        identity_dir=REPO / "identity",
        identity_token_cap=4000,
        orphan_window_seconds=900,
    )


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def service_factory(db, clock):
    def build(*, brain=None, config=None):
        cfg = config or make_config(db._dsn)
        registry = BrainRegistry(cfg)
        if brain is not None:
            registry._cache["brain.default"] = brain
        return TurnService(db, cfg, registry, IdentityLoader(cfg.identity_dir), clock=clock)

    return build


@pytest.fixture()
def service(service_factory):
    return service_factory()
