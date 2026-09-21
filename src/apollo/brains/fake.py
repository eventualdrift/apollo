"""`brain.fake` — offline, deterministic, and held to the same contract.

Three modes: `echo` (fixed transformation, plumbing tests), `scripted`
(responses from a case file), and `replay` (a recorded generation keyed by
bundle hash). Replay reads the recording format `evals/recordings.py` writes,
but does not import it: an adapter reads files, it does not depend on the
evaluation subsystem.

The fake renders through the same `render_chat` path as a real adapter, so the
region contract is exercised rather than bypassed.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import time
from typing import Any

from apollo.brains.base import (
    CHAT_RENDER_VERSION,
    Generation,
    GenerationParams,
    ModelCapabilities,
    RenderedRequest,
    render_chat,
)
from apollo.context.bundle import ContextBundle, Region
from apollo.context.estimator import CONSERVATIVE
from apollo.errors import ApolloError

MODE_ECHO = "echo"
MODE_SCRIPTED = "scripted"
MODE_REPLAY = "replay"
MODES = (MODE_ECHO, MODE_SCRIPTED, MODE_REPLAY)


class FakeBrainError(ApolloError):
    pass


class FakeBrain:
    """Satisfies `Brain`. Stateless with respect to Apollo: it holds no Apollo state."""

    adapter_key = "fake"
    render_version = CHAT_RENDER_VERSION

    def __init__(
        self,
        *,
        key: str = "fake",
        mode: str = MODE_ECHO,
        max_context: int = 8192,
        script: dict[str, str] | None = None,
        replay_dir: pathlib.Path | None = None,
        fail_times: int = 0,
        failure: Exception | None = None,
    ) -> None:
        if mode not in MODES:
            raise FakeBrainError(f"unknown fake mode {mode!r}")
        self.key = key
        self._mode = mode
        self._max_context = max_context
        self._script = dict(script or {})
        self._replay_dir = replay_dir
        #: Test affordance for the retry path: fail this many times, then succeed.
        self._fail_times = fail_times
        self._failure = failure
        self.calls: list[str] = []

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            max_context=self._max_context,
            estimator=CONSERVATIVE,
            supports_system_role=True,
            supports_temperature_zero=True,
            reports_token_counts=True,
        )

    def render(self, bundle: ContextBundle) -> RenderedRequest:
        return render_chat(bundle, supports_system_role=True)

    def generate(self, req: RenderedRequest, params: GenerationParams) -> Generation:
        started = time.monotonic()
        self.calls.append(req.prompt_hash)
        if self._fail_times > 0:
            self._fail_times -= 1
            raise self._failure or FakeBrainError("simulated failure")
        text = self._text_for(req)
        latency = max(1, int((time.monotonic() - started) * 1000))
        return Generation(
            text=text,
            finish_reason="stop",
            model_identifier=f"fake/{self._mode}",
            latency_ms=latency,
            rendered_prompt_hash=req.prompt_hash,
            prompt_tokens=sum(CONSERVATIVE.count(m.content) for m in req.messages),
            completion_tokens=CONSERVATIVE.count(text),
            reasoning_tokens=None,
            # Deliberately noisy: Core must copy out a whitelist and discard the rest.
            raw_meta={"model_identifier": f"fake/{self._mode}", "finish_reason": "stop",
                      "echo_of_prompt": req.messages[-1].content if req.messages else ""},
        )

    def _text_for(self, req: RenderedRequest) -> str:
        if self._mode == MODE_SCRIPTED:
            try:
                return self._script[req.prompt_hash]
            except KeyError:
                raise FakeBrainError("no scripted response for this prompt hash") from None
        if self._mode == MODE_REPLAY:
            return self._replay(req.prompt_hash)
        return self._echo(req)

    def _echo(self, req: RenderedRequest) -> str:
        """A deterministic function of the request only. No Apollo state involved."""
        last = req.messages[-1].content if req.messages else ""
        request_line = last.rsplit("\n\n", 1)[-1].strip()
        digest = hashlib.sha256(req.prompt_hash.encode()).hexdigest()[:8]
        return f"[fake:echo {digest}] {request_line}"

    def _replay(self, prompt_hash: str) -> str:
        """Match on the *rendered* prompt hash, not the file name.

        Recordings are filed under `bundle_hash` because that identifies the
        question Apollo compiled. Replay matches on the rendered hash because
        that identifies the bytes the model was sent: after a `render_version`
        change the same bundle renders differently, and an answer recorded for
        the old bytes is not an answer to the new ones.
        """
        if self._replay_dir is None:
            raise FakeBrainError("replay mode requires a replay directory")
        for path in sorted(self._replay_dir.glob("*.json")):
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            if data.get("rendered_prompt_hash") == prompt_hash:
                return str(data["generation"]["text"])
        raise FakeBrainError("no recording for this rendered prompt hash")


def data_and_request_text(bundle: ContextBundle) -> str:
    """Helper for tests: what the final user message should contain."""
    return "\n\n".join(
        b.content for b in (*bundle.blocks_in(Region.DATA), *bundle.blocks_in(Region.REQUEST))
    )
