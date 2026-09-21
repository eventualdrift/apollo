"""Recordings for `brain.fake` replay (spec G.4).

A recording is the *visible* result of one generation, stored so the full eval
suite and the integration tests can run with no GPU and no network. It is
keyed by `bundle_hash` — the hash of what Apollo compiled — because that is the
durable identity of the question that was asked. It also carries the
`rendered_prompt_hash` it was produced from, and replay matches on that: a
`render_version` change alters the bytes the model saw, so a recording made
under the old renderer must stop matching rather than quietly answer for bytes
that were never sent.

What a recording may contain is deliberately narrow: the text, the finish
reason, the model identifier, counts, and the hashes needed to place it. It
never contains hidden reasoning (spec H.3), provider secrets, response headers,
or an unsanitised provider body (spec H.5). `Generation.raw_meta` is dropped at
this boundary exactly as Core drops it at the persistence boundary.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from apollo.brains.base import Generation
from apollo.errors import ApolloError

RECORDING_VERSION = 1

#: The only keys a recording document carries. Anything else is either a
#: privacy liability (raw bodies, headers, reasoning text) or noise that would
#: drift out of sync with the adapter contract.
RECORDING_KEYS = frozenset(
    {
        "recording_version",
        "bundle_hash",
        "rendered_prompt_hash",
        "render_version",
        "adapter_key",
        "brain_alias",
        "model_identifier",
        "recorded_at",
        "generation",
    }
)

GENERATION_KEYS = frozenset(
    {"text", "finish_reason", "prompt_tokens", "completion_tokens",
     "reasoning_tokens", "latency_ms"}
)


class RecordingError(ApolloError):
    pass


@dataclass(frozen=True)
class Recording:
    bundle_hash: str
    rendered_prompt_hash: str
    render_version: str
    adapter_key: str
    brain_alias: str
    model_identifier: str
    text: str
    finish_reason: str
    recorded_at: datetime
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    #: A count, never the reasoning text itself.
    reasoning_tokens: int | None = None
    latency_ms: int = 0

    @classmethod
    def from_generation(
        cls,
        generation: Generation,
        *,
        bundle_hash: str,
        render_version: str,
        adapter_key: str,
        brain_alias: str,
        recorded_at: datetime | None = None,
    ) -> Recording:
        """Take only the visible fields. `raw_meta` is not copied — ever."""
        return cls(
            bundle_hash=bundle_hash,
            rendered_prompt_hash=generation.rendered_prompt_hash,
            render_version=render_version,
            adapter_key=adapter_key,
            brain_alias=brain_alias,
            model_identifier=generation.model_identifier,
            text=generation.text,
            finish_reason=generation.finish_reason,
            recorded_at=recorded_at or datetime.now(UTC),
            prompt_tokens=generation.prompt_tokens,
            completion_tokens=generation.completion_tokens,
            reasoning_tokens=generation.reasoning_tokens,
            latency_ms=generation.latency_ms,
        )

    def as_document(self) -> dict[str, Any]:
        return {
            "recording_version": RECORDING_VERSION,
            "bundle_hash": self.bundle_hash,
            "rendered_prompt_hash": self.rendered_prompt_hash,
            "render_version": self.render_version,
            "adapter_key": self.adapter_key,
            "brain_alias": self.brain_alias,
            "model_identifier": self.model_identifier,
            "recorded_at": self.recorded_at.isoformat(),
            "generation": {
                "text": self.text,
                "finish_reason": self.finish_reason,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "reasoning_tokens": self.reasoning_tokens,
                "latency_ms": self.latency_ms,
            },
        }

    def to_generation(self) -> Generation:
        """Rebuild the `Generation` a replaying adapter returns.

        `raw_meta` is empty by construction: the provider metadata was never
        recorded, so replay cannot resurrect it.
        """
        return Generation(
            text=self.text,
            finish_reason=self.finish_reason,
            model_identifier=self.model_identifier,
            latency_ms=self.latency_ms,
            rendered_prompt_hash=self.rendered_prompt_hash,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            reasoning_tokens=self.reasoning_tokens,
            raw_meta={},
        )

    @property
    def filename(self) -> str:
        return f"{self.bundle_hash}.json"


def write_recording(directory: pathlib.Path, recording: Recording) -> pathlib.Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / recording.filename
    path.write_text(
        json.dumps(recording.as_document(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def parse_recording(document: Any, *, where: str = "<recording>") -> Recording:
    """Strict. A recording carrying an unexpected key is a bug, not a variant."""
    if not isinstance(document, dict):
        raise RecordingError(f"{where}: a recording must be a mapping")
    unknown = set(document) - RECORDING_KEYS
    if unknown:
        raise RecordingError(f"{where}: unknown recording keys {sorted(unknown)}")
    if document.get("recording_version") != RECORDING_VERSION:
        raise RecordingError(
            f"{where}: recording_version must be {RECORDING_VERSION}, "
            f"got {document.get('recording_version')!r}"
        )
    generation = document.get("generation")
    if not isinstance(generation, dict):
        raise RecordingError(f"{where}: `generation` must be a mapping")
    unknown_gen = set(generation) - GENERATION_KEYS
    if unknown_gen:
        raise RecordingError(f"{where}: unknown generation keys {sorted(unknown_gen)}")
    for required in ("bundle_hash", "rendered_prompt_hash", "render_version", "adapter_key"):
        if not isinstance(document.get(required), str) or not document[required]:
            raise RecordingError(f"{where}: `{required}` is required")
    if not isinstance(generation.get("text"), str):
        raise RecordingError(f"{where}: generation.text is required")
    return Recording(
        bundle_hash=document["bundle_hash"],
        rendered_prompt_hash=document["rendered_prompt_hash"],
        render_version=document["render_version"],
        adapter_key=document["adapter_key"],
        brain_alias=str(document.get("brain_alias", "")),
        model_identifier=str(document.get("model_identifier", "")),
        text=generation["text"],
        finish_reason=str(generation.get("finish_reason", "stop")),
        recorded_at=_parse_time(document.get("recorded_at"), where),
        prompt_tokens=_optional_int(generation.get("prompt_tokens")),
        completion_tokens=_optional_int(generation.get("completion_tokens")),
        reasoning_tokens=_optional_int(generation.get("reasoning_tokens")),
        latency_ms=int(generation.get("latency_ms") or 0),
    )


def load_recording(path: pathlib.Path) -> Recording:
    return parse_recording(json.loads(path.read_text(encoding="utf-8")), where=path.name)


def load_recordings(directory: pathlib.Path) -> list[Recording]:
    if not directory.exists():
        return []
    return [load_recording(path) for path in sorted(directory.glob("*.json"))]


def index_by_prompt_hash(recordings: list[Recording]) -> dict[str, Recording]:
    """What a replaying adapter looks things up by: the bytes it actually sent."""
    index: dict[str, Recording] = {}
    for recording in recordings:
        existing = index.get(recording.rendered_prompt_hash)
        if existing is not None and existing.text != recording.text:
            raise RecordingError(
                f"two recordings share rendered_prompt_hash "
                f"{recording.rendered_prompt_hash} with different text: "
                f"{existing.bundle_hash} and {recording.bundle_hash}"
            )
        index[recording.rendered_prompt_hash] = recording
    return index


def _parse_time(raw: Any, where: str) -> datetime:
    if raw is None:
        return datetime.now(UTC)
    if not isinstance(raw, str):
        raise RecordingError(f"{where}: recorded_at must be an ISO-8601 string")
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        raise RecordingError(f"{where}: recorded_at is not ISO-8601: {raw!r}") from None


def _optional_int(raw: Any) -> int | None:
    return None if raw is None else int(raw)
