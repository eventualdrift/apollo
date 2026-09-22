"""Gate 1 cannot pass on static declarations and rendering alone."""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta

from apollo.brains.fake import FakeBrain
from apollo.brains.gate1 import run_gate1
from apollo.brains.registry import BrainRegistry
from apollo.config import SURFACE_EVAL, Config, ProviderConfig
from apollo.core.identity import IdentityLoader
from apollo.evals.gate1 import _representative_bundles, evaluate_gate1

REPO = pathlib.Path(__file__).resolve().parents[2]


class UnreachableBrain(FakeBrain):
    """Statically valid; any attempted provider call fails visibly."""

    def __init__(self, *, max_context: int = 9024) -> None:
        super().__init__(max_context=max_context)

    def generate(self, req, params):  # type: ignore[no-untyped-def]
        self.calls.append(req.prompt_hash)
        raise RuntimeError("provider unreachable")


class NoDatabase:
    def __init__(self) -> None:
        self.connect_calls = 0

    def connect(self):  # type: ignore[no-untyped-def]
        self.connect_calls += 1
        raise RuntimeError("database unavailable")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def config(*, allowed_modes: tuple[str, ...] = ("personal", "benchmark")) -> Config:
    provider = ProviderConfig(
        key="fake",
        kind="fake",
        allowed_modes=allowed_modes,
        eval_only=False,
        context_budget=8000,
        reserved_output=1024,
    )
    return Config(
        database_dsn="unused",
        providers={"fake": provider},
        brain_aliases={"default": "fake"},
        identity_dir=REPO / "identity",
        identity_token_cap=4000,
    )


def registry_with(cfg: Config, brain: FakeBrain) -> BrainRegistry:
    registry = BrainRegistry(cfg, surface=SURFACE_EVAL)
    registry._cache["brain.default"] = brain
    return registry


def test_static_success_cannot_claim_a_full_gate_one_pass() -> None:
    """Before the fix this exact shape returned PASS with zero provider calls."""
    cfg = config()
    brain = UnreachableBrain()
    identity = IdentityLoader(cfg.identity_dir).load()
    bundles = _representative_bundles(
        config=cfg,
        provider=cfg.provider_for_alias("brain.default"),
        brain=brain,
        identity=identity,
        now=Clock()(),
    )

    report = run_gate1(brain, cfg.provider_for_alias("brain.default"), bundles)

    assert report.static_passed
    assert report.generation_probe_passed is None
    assert not report.passed
    assert brain.calls == []
    assert "NOT RUN  recorded generation probe" in "\n".join(report.as_lines())


def test_static_failure_spends_neither_database_access_nor_provider_call() -> None:
    cfg = config()
    brain = UnreachableBrain(max_context=100)
    database = NoDatabase()

    report = evaluate_gate1(
        database,  # type: ignore[arg-type]
        cfg,
        registry_with(cfg, brain),
        IdentityLoader(cfg.identity_dir),
        brain_alias="brain.default",
        clock=Clock(),
    )

    assert not report.static_passed
    assert not report.passed
    assert report.generation_probe_passed is None
    assert database.connect_calls == 0
    assert brain.calls == []


def test_policy_failure_spends_neither_database_access_nor_provider_call() -> None:
    cfg = config(allowed_modes=("personal",))
    brain = UnreachableBrain()
    database = NoDatabase()

    report = evaluate_gate1(
        database,  # type: ignore[arg-type]
        cfg,
        registry_with(cfg, brain),
        IdentityLoader(cfg.identity_dir),
        brain_alias="brain.default",
        clock=Clock(),
    )

    assert report.static_passed
    assert report.generation_probe_passed is False
    assert not report.passed
    assert database.connect_calls == 0
    assert brain.calls == []


def test_database_failure_cannot_fall_through_to_the_provider() -> None:
    cfg = config()
    brain = UnreachableBrain()
    database = NoDatabase()

    report = evaluate_gate1(
        database,  # type: ignore[arg-type]
        cfg,
        registry_with(cfg, brain),
        IdentityLoader(cfg.identity_dir),
        brain_alias="brain.default",
        clock=Clock(),
    )

    assert report.static_passed
    assert report.generation_probe_passed is False
    assert not report.passed
    assert database.connect_calls == 1
    assert brain.calls == []
