"""An OpenAI-compatible HTTP adapter (ADR-0002, spec G.2).

This is *OpenAI-compatible HTTP*, not OpenAI-specific architecture. The same
adapter serves llama.cpp's server, vLLM, SGLang, Ollama's compatible endpoint
and hosted providers, because they all speak `POST {base_url}/chat/completions`
with a messages array. llama.cpp is the initial local deployment choice and
nothing more: no assumption about it appears here or anywhere in Core.

The adapter is stateless with respect to Apollo. It cannot reach storage,
memory or Core — asserted by the architecture test — which is what makes
"Apollo is not the model" structural rather than aspirational (ADR-0001).

Transport is the standard library. A JSON POST does not justify a dependency,
and the project's stated bias is fewer libraries that are still legible in
three years.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

from apollo.brains.base import (
    CHAT_RENDER_VERSION,
    Generation,
    GenerationParams,
    ModelCapabilities,
    RenderedRequest,
    render_chat,
)
from apollo.config import ProviderConfig
from apollo.context.bundle import ContextBundle
from apollo.context.estimator import CONSERVATIVE
from apollo.errors import BrainTransportError, ConfigError

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 120
CHAT_COMPLETIONS_PATH = "/chat/completions"

#: Fields some OpenAI-compatible servers use for a separate reasoning channel.
#: Their *content* is dropped on arrival and never leaves this module; only a
#: token count survives (spec H.3). Extend this list, never the handling.
REASONING_CONTENT_FIELDS = ("reasoning_content", "reasoning", "thinking")


class OpenAICompatibleBrain:
    """Satisfies the `Brain` protocol against any OpenAI-compatible endpoint."""

    adapter_key = "openai_compatible"
    #: The bundle-to-request transformation is the shared chat rendering, so it
    #: carries the shared render version. A change to either bumps both.
    render_version = CHAT_RENDER_VERSION

    def __init__(
        self,
        *,
        key: str,
        base_url: str,
        model: str,
        max_context: int,
        api_key: str | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        supports_system_role: bool = True,
    ) -> None:
        self.key = key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._max_context = max_context
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._supports_system_role = supports_system_role

    @classmethod
    def from_config(cls, provider: ProviderConfig) -> OpenAICompatibleBrain:
        """Build from frozen configuration. Secrets come from the environment only."""
        options = provider.options
        base_url = str(options.get("base_url", "")).strip()
        model = str(options.get("model", "")).strip()
        if not base_url:
            raise ConfigError(f"provider {provider.key}: base_url is required")
        if not model:
            raise ConfigError(f"provider {provider.key}: model is required")

        # The TOML names the *variable*, never the value. A key in a committed
        # file is a key in git history for ever.
        api_key = None
        env_name = options.get("api_key_env")
        if env_name:
            api_key = os.environ.get(str(env_name))
            if not api_key:
                raise ConfigError(
                    f"provider {provider.key}: {env_name} is not set in the environment"
                )

        return cls(
            key=provider.key,
            base_url=base_url,
            model=model,
            max_context=int(options.get("max_context", provider.context_budget
                                        + provider.reserved_output)),
            api_key=api_key,
            timeout_seconds=int(options.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
            supports_system_role=bool(options.get("supports_system_role", True)),
        )

    # -- Brain protocol ----------------------------------------------------

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            max_context=self._max_context,
            # Core owns budget policy; counting stays model-agnostic until a
            # provider offers an exact counter worth trusting (ADR-0002).
            estimator=CONSERVATIVE,
            supports_system_role=self._supports_system_role,
            supports_seed=True,
            supports_temperature_zero=True,
            reports_token_counts=True,
        )

    def render(self, bundle: ContextBundle) -> RenderedRequest:
        """The shared four-region chat rendering, verified on every call."""
        return render_chat(bundle, supports_system_role=self._supports_system_role)

    def generate(self, req: RenderedRequest, params: GenerationParams) -> Generation:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
            "temperature": params.temperature,
            "max_tokens": params.max_tokens,
            "stream": False,
        }
        if params.seed is not None:
            payload["seed"] = params.seed

        started = time.monotonic()
        body = self._post(CHAT_COMPLETIONS_PATH, payload)
        latency_ms = max(1, int((time.monotonic() - started) * 1000))
        return self._to_generation(body, req, latency_ms)

    # -- transport ---------------------------------------------------------

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST JSON. Every failure becomes a transport error carrying scalars only."""
        request = urllib.request.Request(  # noqa: S310 - configured base_url, not user input
            url=self._base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise self._transport_error_from_http(exc) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            # No status, and deliberately nothing from the exception: a client's
            # message routinely embeds the URL with its query string (spec H.5).
            raise BrainTransportError(provider_error_type="connection_failed") from None

        try:
            decoded: dict[str, Any] = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BrainTransportError(provider_error_type="malformed_response") from None
        return decoded

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _transport_error_from_http(self, exc: urllib.error.HTTPError) -> BrainTransportError:
        """Read a status and two short scalars. Never the body, never the message."""
        code: str | None = None
        kind: str | None = None
        try:
            error = json.loads(exc.read()).get("error")
            if isinstance(error, dict):
                code = _short_scalar(error.get("code"))
                kind = _short_scalar(error.get("type"))
        except Exception:  # noqa: BLE001 - a malformed error body is not itself an error
            pass
        return BrainTransportError(
            http_status=int(exc.code), provider_error_code=code, provider_error_type=kind
        )

    # -- response mapping --------------------------------------------------

    def _to_generation(
        self, body: dict[str, Any], req: RenderedRequest, latency_ms: int
    ) -> Generation:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise BrainTransportError(provider_error_type="no_choices")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            raise BrainTransportError(provider_error_type="malformed_choice")

        # An empty or whitespace answer is returned as-is. Core classifies it
        # as `empty_generation`: a content-level failure that fails the turn and
        # is never retried. The adapter does not second-guess that.
        text = message.get("content")
        text = text if isinstance(text, str) else ""

        raw_usage = body.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        reasoning_tokens = _reasoning_token_count(usage, message)

        return Generation(
            text=text,
            finish_reason=str(choice.get("finish_reason") or "unknown")[:32],
            model_identifier=str(body.get("model") or self._model)[:200],
            latency_ms=latency_ms,
            rendered_prompt_hash=req.prompt_hash,
            prompt_tokens=_int_or_none(usage.get("prompt_tokens")),
            completion_tokens=_int_or_none(usage.get("completion_tokens")),
            reasoning_tokens=reasoning_tokens,
            # Scalars only. The reasoning channel's *content* is already gone by
            # here, and Core whitelists this again before anything is persisted.
            raw_meta={
                "model_identifier": str(body.get("model") or self._model)[:200],
                "finish_reason": str(choice.get("finish_reason") or "unknown")[:32],
                "provider_request_id": _short_scalar(body.get("id")) or "",
            },
        )


def _reasoning_token_count(usage: dict[str, Any], message: dict[str, Any]) -> int | None:
    """Count the reasoning channel, then let its content fall out of scope.

    Spec H.3: no hidden reasoning trace is persisted anywhere, in any stream.
    It is the model's scratch space, it frequently holds what the visible answer
    deliberately excluded, and storing it invites treating it as evidence of
    what Apollo "really" thinks. So the content is never returned from this
    module — not on `Generation`, not in `raw_meta`, not in a log.
    """
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict):
        counted = _int_or_none(details.get("reasoning_tokens"))
        if counted is not None:
            return counted
    # No count offered, but a content field present: estimate from its length so
    # the fact of reasoning is still visible in the audit record. The text
    # itself is read here and goes no further.
    for field in REASONING_CONTENT_FIELDS:
        value = message.get(field)
        if isinstance(value, str) and value:
            return CONSERVATIVE.count(value)
    return None


def _int_or_none(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _short_scalar(value: Any) -> str | None:
    if not isinstance(value, str | int) or isinstance(value, bool):
        return None
    cleaned = "".join(ch for ch in str(value) if ch.isalnum() or ch in "._-")
    return cleaned[:64] or None
