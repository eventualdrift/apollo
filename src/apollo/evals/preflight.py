"""The privacy pre-flight for a hosted persona run.

Sending Apollo's compiled context to somebody else's server is the single most
consequential thing this system does. The pre-flight exists so that what is
about to leave the machine is stated in advance, in writing, from the actual
compiled bundle — not from a belief about what the compiler does.

It makes no outbound call. It inspects configuration and one real bundle and
reports. Deciding to proceed is a human act.

One line in the report is deliberately not reassuring: canonical identity *is*
included, because it is real personal content and removing it would benchmark a
different Apollo. A hosted run is a privacy decision, not a privacy-free one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from apollo.config import MODE_BENCHMARK, ProviderConfig
from apollo.context.bundle import BlockType, ContextBundle, Region

#: Source references the eval runner mints for fixture content.
FIXTURE_PREFIX = "fixture:"


@dataclass(frozen=True)
class PreflightReport:
    brain_alias: str
    provider_key: str
    base_url_host: str
    model_identifier: str
    mode: str
    eval_only: bool
    allowed_modes: tuple[str, ...]
    api_key_env: str | None
    identity_version: str | None
    identity_hash: str | None
    canonical_identity_included: bool
    fixture_history_only: bool
    stored_personal_history_included: bool
    stored_personal_memory_included: bool
    connector_data_included: bool
    unexpected: tuple[str, ...] = ()

    @property
    def expectations_hold(self) -> bool:
        """The expected shape from the brief: benchmark, eval-only, fixtures only."""
        return (
            self.mode == MODE_BENCHMARK
            and self.eval_only
            and self.fixture_history_only
            and not self.stored_personal_history_included
            and not self.stored_personal_memory_included
            and not self.connector_data_included
            and not self.unexpected
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "brain_alias": self.brain_alias,
            "provider_key": self.provider_key,
            "base_url_host": self.base_url_host,
            "model_identifier": self.model_identifier,
            "mode": self.mode,
            "eval_only": self.eval_only,
            "allowed_modes": list(self.allowed_modes),
            "api_key_env": self.api_key_env,
            "identity_version": self.identity_version,
            "identity_hash": self.identity_hash,
            "canonical_identity_included": self.canonical_identity_included,
            "fixture_history_only": self.fixture_history_only,
            "stored_personal_history_included": self.stored_personal_history_included,
            "stored_personal_memory_included": self.stored_personal_memory_included,
            "connector_data_included": self.connector_data_included,
            "unexpected": list(self.unexpected),
            "expectations_hold": self.expectations_hold,
        }

    def as_lines(self) -> list[str]:
        def yn(value: bool) -> str:
            return "yes" if value else "no"

        lines = [
            "hosted persona run — privacy pre-flight",
            "",
            f"  provider alias:                  {self.brain_alias} ({self.provider_key})",
            f"  base URL host:                   {self.base_url_host}",
            f"  model identifier:                {self.model_identifier}",
            f"  mode:                            {self.mode}",
            f"  eval_only:                       {yn(self.eval_only)}",
            f"  allowed_modes:                   {list(self.allowed_modes)}",
            f"  API key environment variable:    {self.api_key_env or '<none>'}",
            f"  identity version/hash:           {self.identity_version} / "
            f"{self.identity_hash}",
            "",
            f"  canonical identity included?     {yn(self.canonical_identity_included)}",
            f"  fixture history only?            {yn(self.fixture_history_only)}",
            f"  stored personal history included? {yn(self.stored_personal_history_included)}",
            f"  stored personal memory included? {yn(self.stored_personal_memory_included)}",
            f"  connector data included?         {yn(self.connector_data_included)}",
        ]
        for problem in self.unexpected:
            lines.append(f"  !! {problem}")
        lines += [
            "",
            "expectations hold: " + ("YES" if self.expectations_hold else "NO"),
            "",
            "Canonical identity is real personal content and is included deliberately: a "
            "benchmark of a stripped identity is a benchmark of a different Apollo.",
            "No request has been made. This command never calls the provider.",
        ]
        return lines


def preflight(
    *,
    brain_alias: str,
    provider: ProviderConfig,
    bundle: ContextBundle,
    mode: str = MODE_BENCHMARK,
) -> PreflightReport:
    """Derive the report from a bundle the runner would actually send."""
    history = [b for b in bundle.blocks if b.region is Region.HISTORY]
    non_fixture = [
        b for b in history if not (b.source_ref or "").startswith(FIXTURE_PREFIX)
    ]
    request_blocks = [b for b in bundle.blocks if b.region is Region.REQUEST]
    non_fixture_request = [
        b for b in request_blocks if not (b.source_ref or "").startswith(FIXTURE_PREFIX)
    ]
    memory_blocks = [b for b in bundle.blocks if b.block_type is BlockType.MEMORY]
    identity_blocks = [b for b in bundle.blocks if b.block_type is BlockType.IDENTITY]

    unexpected: list[str] = []
    if mode not in provider.allowed_modes:
        unexpected.append(f"provider does not allow mode {mode!r}")
    if not provider.eval_only:
        unexpected.append(
            "provider is not eval_only: a hosted reference model reachable from the "
            "interactive surface is a privacy boundary failure"
        )
    for block in non_fixture:
        unexpected.append(
            f"history block {block.source_ref!r} is not fixture content"
        )
    for block in non_fixture_request:
        unexpected.append(f"request block {block.source_ref!r} is not fixture content")
    if memory_blocks:
        unexpected.append(f"{len(memory_blocks)} MEMORY block(s) present in a benchmark bundle")

    base_url = str(provider.options.get("base_url", ""))
    host = urlsplit(base_url).hostname or "<unset>"

    return PreflightReport(
        brain_alias=brain_alias,
        provider_key=provider.key,
        # The host only: a path or query string can carry a credential, and a
        # pre-flight report is something a person pastes into a message.
        base_url_host=host,
        model_identifier=str(provider.options.get("model", "<unset>")),
        mode=mode,
        eval_only=provider.eval_only,
        allowed_modes=tuple(provider.allowed_modes),
        # The NAME of the variable. Never its value (spec K.3).
        api_key_env=(
            str(provider.options["api_key_env"]) if provider.options.get("api_key_env") else None
        ),
        identity_version=bundle.identity_version,
        identity_hash=bundle.identity_hash,
        canonical_identity_included=bool(identity_blocks),
        fixture_history_only=not non_fixture,
        stored_personal_history_included=bool(non_fixture),
        stored_personal_memory_included=bool(memory_blocks),
        # No connector exists in phase zero. Stated, not assumed away.
        connector_data_included=False,
        unexpected=tuple(unexpected),
    )
