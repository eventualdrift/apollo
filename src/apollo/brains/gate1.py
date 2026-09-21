"""Gate 1 — protocol compatibility (spec G.5).

Mechanical checks a brain must pass before it may be bound to `brain.default`.
Passing Gate 1 means Apollo can *technically* use the model. It says nothing
about whether Apollo still behaves like Apollo through it — that is Gate 2, and
it is empirical.

This harness deliberately does not call the provider. A model that cannot be
reached is a deployment problem; a model whose adapter renders illegally is an
architecture problem, and only the second is Gate 1's business.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from apollo.brains.base import Brain, RenderContractError, verify_region_contract
from apollo.config import SURFACES, ProviderConfig
from apollo.context.bundle import ContextBundle


@dataclass
class Gate1Report:
    brain_key: str
    adapter_key: str
    render_version: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def record(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append((name, passed, detail))

    @property
    def passed(self) -> bool:
        return all(passed for _, passed, _ in self.checks)

    def as_lines(self) -> list[str]:
        lines = [
            f"brain:          {self.brain_key}",
            f"adapter_key:    {self.adapter_key}",
            f"render_version: {self.render_version}",
            "",
        ]
        for name, passed, detail in self.checks:
            mark = "PASS" if passed else "FAIL"
            lines.append(f"  {mark}  {name}" + (f" — {detail}" if detail else ""))
        lines.append("")
        lines.append(f"Gate 1: {'PASS' if self.passed else 'FAIL'}")
        return lines


def run_gate1(
    brain: Brain,
    provider: ProviderConfig,
    bundles: list[ContextBundle],
) -> Gate1Report:
    """Run the mechanical checks. `bundles` must include a delimiter-collision case."""
    report = Gate1Report(
        brain_key=getattr(brain, "key", "?"),
        adapter_key=brain.adapter_key,
        render_version=brain.render_version,
    )

    report.record("implements Brain", isinstance(brain, Brain))
    report.record("declares adapter_key", bool(brain.adapter_key))
    report.record("declares render_version", bool(brain.render_version))

    report.record(
        "declares allowed_modes",
        bool(provider.allowed_modes),
        ", ".join(provider.allowed_modes),
    )
    report.record("declares eval_only", isinstance(provider.eval_only, bool),
                  str(provider.eval_only))
    report.record(
        "surface rule is total",
        all(isinstance(provider.resolvable_from(s), bool) for s in SURFACES),
    )

    capabilities = brain.capabilities()
    required = provider.context_budget + provider.reserved_output
    report.record(
        "max_context >= budget + reserved output",
        capabilities.max_context >= required,
        f"{capabilities.max_context} >= {required}",
    )
    report.record("declares a token estimator", bool(capabilities.estimator.name))

    for index, bundle in enumerate(bundles):
        label = f"renders bundle {index} legally"
        try:
            request = brain.render(bundle)
            verify_region_contract(bundle, request)
            ok = bool(request.messages) and bool(request.prompt_hash)
            report.record(label, ok, f"{len(request.messages)} messages")
        except RenderContractError as exc:
            report.record(label, False, f"region contract: {exc}")
        except Exception as exc:  # noqa: BLE001 - a failed gate reports, never raises
            report.record(label, False, f"{type(exc).__name__}")

    return report
