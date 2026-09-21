"""Step 7: privacy modes, the eval-only guard, and their independence (spec K.2).

M1 established the mode check inside the turn path. What M2 adds is the first
*real* counterparty — an `eval_only` hosted provider — and the second
independent mechanism the frozen spec requires: the brain registry refusing to
resolve such a provider at all on an interactive surface.

The two mechanisms are tested separately and together. A single flag whose
bypass defeats everything is not defence in depth.
"""

from __future__ import annotations

import pytest

from apollo.brains.fake import FakeBrain
from apollo.brains.registry import BrainRegistry
from apollo.config import (
    MODE_BENCHMARK,
    MODE_PERSONAL,
    SURFACE_EVAL,
    SURFACE_INTERACTIVE,
    Config,
    ProviderConfig,
)
from apollo.core.conversations import create_conversation
from apollo.core.policy import check_brain_permitted, check_mode_permitted
from apollo.errors import ErrorKind, PolicyRefusedError
from tests.integration.conftest import REPO

pytestmark = pytest.mark.integration


def provider(key: str, *, allowed_modes, eval_only: bool, kind: str = "fake") -> ProviderConfig:
    return ProviderConfig(
        key=key,
        kind=kind,
        allowed_modes=tuple(allowed_modes),
        eval_only=eval_only,
        context_budget=8000,
        reserved_output=1024,
        options={"base_url": "http://127.0.0.1:9/v1", "model": "m"} if kind != "fake" else {},
    )


LOCAL = provider("local", allowed_modes=(MODE_PERSONAL, MODE_BENCHMARK), eval_only=False)
REFERENCE = provider("reference", allowed_modes=(MODE_BENCHMARK,), eval_only=True,
                     kind="openai_compatible")


def config_with(*providers, default: str) -> Config:
    return Config(
        database_dsn="unused",
        providers={p.key: p for p in providers},
        brain_aliases={"default": default, "reference": "reference"},
        identity_dir=REPO / "identity",
    )


# -- mechanism 2: mode x allowed_modes, on its own -------------------------


@pytest.mark.parametrize("mode", [MODE_PERSONAL, MODE_BENCHMARK])
def test_local_serves_both_modes(mode: str) -> None:
    check_mode_permitted(provider=LOCAL, conversation_mode=mode)  # must not raise


def test_reference_does_not_serve_personal() -> None:
    with pytest.raises(PolicyRefusedError) as caught:
        check_mode_permitted(provider=REFERENCE, conversation_mode=MODE_PERSONAL)
    assert caught.value.kind is ErrorKind.BRAIN_MODE_NOT_PERMITTED


def test_reference_serves_benchmark_by_mode_alone() -> None:
    """Mode permits it; the surface guard is what still stops interactive use."""
    check_mode_permitted(provider=REFERENCE, conversation_mode=MODE_BENCHMARK)


# -- mechanism 1: the registry refuses to resolve, independently -----------


def test_the_interactive_registry_cannot_resolve_an_eval_only_provider() -> None:
    registry = BrainRegistry(config_with(LOCAL, REFERENCE, default="local"),
                             surface=SURFACE_INTERACTIVE)
    with pytest.raises(PolicyRefusedError) as caught:
        registry.get("brain.reference")
    assert caught.value.kind is ErrorKind.BRAIN_NOT_INTERACTIVE


def test_the_eval_registry_may_resolve_it() -> None:
    registry = BrainRegistry(config_with(LOCAL, REFERENCE, default="local"),
                             surface=SURFACE_EVAL)
    brain = registry.get("brain.reference")
    assert brain.adapter_key == "openai_compatible"


def test_the_registry_refuses_even_when_the_mode_check_would_pass() -> None:
    """The mechanisms are independent: mode says yes, the surface still says no."""
    check_mode_permitted(provider=REFERENCE, conversation_mode=MODE_BENCHMARK)  # yes
    registry = BrainRegistry(config_with(LOCAL, REFERENCE, default="local"),
                             surface=SURFACE_INTERACTIVE)
    with pytest.raises(PolicyRefusedError):
        registry.get("brain.reference")  # and yet, no


def test_an_interactive_registry_still_resolves_a_normal_provider() -> None:
    registry = BrainRegistry(config_with(LOCAL, REFERENCE, default="local"),
                             surface=SURFACE_INTERACTIVE)
    assert registry.get("brain.default").key == "local"


def test_resolvable_from_is_the_single_shared_rule() -> None:
    assert LOCAL.resolvable_from(SURFACE_INTERACTIVE) is True
    assert LOCAL.resolvable_from(SURFACE_EVAL) is True
    assert REFERENCE.resolvable_from(SURFACE_INTERACTIVE) is False
    assert REFERENCE.resolvable_from(SURFACE_EVAL) is True


# -- both, through the turn path -------------------------------------------


def test_a_refused_turn_makes_no_model_call_and_records_the_refusal(
    db, service_factory, clock
) -> None:
    """The inverse case that matters: refusal costs zero generations."""
    from tests.integration.conftest import make_config

    brain = FakeBrain()
    config = make_config(db._dsn, allowed_modes=(MODE_BENCHMARK,))
    conversation_id = create_conversation(db, now=clock())
    result = service_factory(brain=brain, config=config).submit(
        conversation_id=conversation_id, text="hello"
    )

    assert result.status == "failed"
    assert result.error_kind == ErrorKind.BRAIN_MODE_NOT_PERMITTED
    assert brain.calls == [], "a refusal must not reach the model"

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM model_invocation WHERE turn_id = %s", (result.turn_id,)
        )
        assert cur.fetchone()["n"] == 0, "no invocation row: nothing was attempted"
        cur.execute(
            "SELECT count(*) AS n FROM audit_event WHERE event_type = 'policy.refused'"
            " AND turn_id = %s",
            (result.turn_id,),
        )
        assert cur.fetchone()["n"] == 1
        cur.execute(
            "SELECT count(*) AS n FROM message WHERE conversation_id = %s AND role = 'apollo'",
            (conversation_id,),
        )
        assert cur.fetchone()["n"] == 0, "no fabricated reply, no silent substitution"


def test_refusal_is_checked_before_the_context_is_even_compiled(
    db, service_factory, clock
) -> None:
    """Spec K.2: checked *before* retrieval or generation."""
    from tests.integration.conftest import make_config

    class ExplodingRender(FakeBrain):
        def render(self, bundle):  # type: ignore[no-untyped-def]
            raise AssertionError("render must not be reached on a refused turn")

    config = make_config(db._dsn, allowed_modes=(MODE_BENCHMARK,))
    conversation_id = create_conversation(db, now=clock())
    result = service_factory(brain=ExplodingRender(), config=config).submit(
        conversation_id=conversation_id, text="hello"
    )
    assert result.error_kind == ErrorKind.BRAIN_MODE_NOT_PERMITTED


def test_check_brain_permitted_composes_both_mechanisms() -> None:
    check_brain_permitted(provider=LOCAL, conversation_mode=MODE_PERSONAL,
                          surface=SURFACE_INTERACTIVE)
    with pytest.raises(PolicyRefusedError) as surface_refusal:
        check_brain_permitted(provider=REFERENCE, conversation_mode=MODE_BENCHMARK,
                              surface=SURFACE_INTERACTIVE)
    assert surface_refusal.value.kind is ErrorKind.BRAIN_NOT_INTERACTIVE
    with pytest.raises(PolicyRefusedError) as mode_refusal:
        check_brain_permitted(provider=REFERENCE, conversation_mode=MODE_PERSONAL,
                              surface=SURFACE_EVAL)
    assert mode_refusal.value.kind is ErrorKind.BRAIN_MODE_NOT_PERMITTED


# -- conversations ----------------------------------------------------------


def test_the_interactive_surface_creates_only_personal_conversations(db, clock) -> None:
    conversation_id = create_conversation(db, now=clock())
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT mode FROM conversation WHERE id = %s", (conversation_id,))
        assert cur.fetchone()["mode"] == MODE_PERSONAL


def test_the_cli_offers_no_way_to_create_a_benchmark_conversation() -> None:
    """The interactive client must not expose mode at all."""
    import inspect

    from apollo.cli import main

    source = inspect.getsource(main)
    assert "benchmark" not in source
    assert "mode=" not in source


def test_conversation_mode_is_immutable_after_creation(db, clock) -> None:
    import psycopg

    conversation_id = create_conversation(db, now=clock())
    with db.connect() as conn, conn.cursor() as cur, \
            pytest.raises(psycopg.errors.RestrictViolation, match="mode is write-once"):
        cur.execute(
            "UPDATE conversation SET mode = 'benchmark' WHERE id = %s", (conversation_id,)
        )
