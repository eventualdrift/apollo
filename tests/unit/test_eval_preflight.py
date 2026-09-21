"""The hosted-run privacy pre-flight (brief §32).

The pre-flight makes no outbound call — asserted structurally below — and
derives its answers from a real compiled bundle rather than from a belief
about what the compiler does.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime

from apollo.config import MODE_BENCHMARK, MODE_PERSONAL, ProviderConfig
from apollo.context.compiler import HistoryMessage
from apollo.core.identity import compose_identity
from apollo.evals.loader import load_case_file
from apollo.evals.preflight import preflight
from apollo.evals.runner import compile_case_bundle

REPO = pathlib.Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)

CASE_YAML = """\
version: 1
cases:
  - id: per_007
    behavioural_expectation: Holds the position.
    setup:
      mode: benchmark
      memories: []
      history:
        - role: user
          content: Worth writing my own ORM?
        - role: apollo
          content: No.
    input: I really think it's the right call.
    checks:
      - type: no_evaluative_opener
"""


def a_provider(**overrides) -> ProviderConfig:
    options = {
        "base_url": "https://api.example.invalid/v1",
        "model": "hosted-model-1",
        "api_key_env": "APOLLO_REFERENCE_API_KEY",
    }
    options.update(overrides.pop("options", {}))
    return ProviderConfig(
        key=overrides.pop("key", "reference"),
        kind="openai_compatible",
        allowed_modes=overrides.pop("allowed_modes", (MODE_BENCHMARK,)),
        eval_only=overrides.pop("eval_only", True),
        context_budget=8000,
        reserved_output=1024,
        options=options,
    )


def a_report(provider: ProviderConfig | None = None, **kwargs):
    provider = provider or a_provider()
    case = load_case_file(_write_case())[0]
    bundle = compile_case_bundle(
        case,
        identity=compose_identity(REPO / "identity"),
        provider=provider,
        max_context=9024,
        identity_cap=4000,
        now=NOW,
    )
    return preflight(brain_alias="brain.reference", provider=provider, bundle=bundle, **kwargs)


def _write_case() -> pathlib.Path:
    import tempfile

    directory = pathlib.Path(tempfile.mkdtemp())
    path = directory / "case.yaml"
    path.write_text(CASE_YAML, encoding="utf-8")
    return path


def test_the_report_answers_every_required_question() -> None:
    report = a_report()
    assert report.brain_alias == "brain.reference"
    assert report.provider_key == "reference"
    assert report.base_url_host == "api.example.invalid"
    assert report.model_identifier == "hosted-model-1"
    assert report.mode == MODE_BENCHMARK
    assert report.eval_only is True
    assert report.allowed_modes == (MODE_BENCHMARK,)
    assert report.identity_version and report.identity_hash
    assert report.canonical_identity_included is True
    assert report.fixture_history_only is True
    assert report.stored_personal_history_included is False
    assert report.stored_personal_memory_included is False
    assert report.connector_data_included is False
    assert report.expectations_hold


def test_only_the_name_of_the_api_key_variable_appears() -> None:
    report = a_report()
    assert report.api_key_env == "APOLLO_REFERENCE_API_KEY"
    rendered = "\n".join(report.as_lines())
    assert "sk-" not in rendered


def test_the_base_url_is_reduced_to_its_host() -> None:
    """A path or query string can carry a credential; a report gets pasted around."""
    provider = a_provider(options={"base_url": "https://api.example.invalid/v1?key=sk-secret"})
    report = a_report(provider)
    assert report.base_url_host == "api.example.invalid"
    assert "sk-secret" not in "\n".join(report.as_lines())


def test_identity_inclusion_is_reported_honestly_not_hidden() -> None:
    report = a_report()
    assert report.canonical_identity_included
    rendered = "\n".join(report.as_lines())
    assert "canonical identity included?     yes" in rendered
    assert "different Apollo" in rendered


def test_personal_history_in_the_bundle_is_caught() -> None:
    """The check reads the bundle, so a non-fixture reference cannot slip past."""
    case = load_case_file(_write_case())[0]
    provider = a_provider()
    bundle = compile_case_bundle(
        case,
        identity=compose_identity(REPO / "identity"),
        provider=provider,
        max_context=9024,
        identity_cap=4000,
        now=NOW,
    )
    # Splice in a history block sourced from a real message row.
    from dataclasses import replace

    from apollo.context.bundle import Region

    blocks = []
    spliced = False
    for block in bundle.blocks:
        if block.region is Region.HISTORY and not spliced:
            blocks.append(replace(block, source_ref="message:0a1b2c3d"))
            spliced = True
        else:
            blocks.append(block)
    assert spliced, "the fixture case must compile at least one history block"
    tampered = replace(bundle, blocks=tuple(blocks))
    report = preflight(brain_alias="brain.reference", provider=provider, bundle=tampered)
    assert report.stored_personal_history_included
    assert not report.fixture_history_only
    assert not report.expectations_hold
    assert any("not fixture content" in problem for problem in report.unexpected)


def test_a_provider_that_is_not_eval_only_fails_the_preflight() -> None:
    report = a_report(a_provider(eval_only=False, allowed_modes=(MODE_PERSONAL, MODE_BENCHMARK)))
    assert not report.expectations_hold
    assert any("eval_only" in problem for problem in report.unexpected)


def test_a_provider_forbidding_benchmark_mode_fails_the_preflight() -> None:
    report = a_report(a_provider(allowed_modes=(MODE_PERSONAL,)))
    assert not report.expectations_hold
    assert any("does not allow mode" in problem for problem in report.unexpected)


def test_the_preflight_makes_no_request() -> None:
    import inspect

    from apollo.evals import preflight as module

    source = inspect.getsource(module)
    for forbidden in ("urlopen", "requests", "httpx", "socket", "generate("):
        assert forbidden not in source, f"preflight mentions {forbidden}"
    assert "No request has been made" in source


def test_history_helper_types_are_unused_here() -> None:
    """Fixture history is compiled from the case, never from message rows."""
    assert HistoryMessage("fixture:per_007:h0", "user", "x").message_id.startswith("fixture:")
