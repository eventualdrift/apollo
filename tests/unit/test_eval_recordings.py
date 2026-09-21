"""The `brain.fake` replay recording format (brief §30, spec G.4).

Tested with synthetic `Generation` values: populating a recording from a real
provider would need a real provider, and the format is what is under test.
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime

import pytest

from apollo.brains.base import Generation, GenerationParams
from apollo.brains.fake import MODE_REPLAY, FakeBrain, FakeBrainError
from apollo.evals.recordings import (
    GENERATION_KEYS,
    RECORDING_KEYS,
    Recording,
    RecordingError,
    index_by_prompt_hash,
    load_recording,
    load_recordings,
    parse_recording,
    write_recording,
)

SECRET = "sk-live-SENTINEL-must-never-be-recorded"
REASONING = "SENTINEL-hidden-chain-of-thought"


def a_generation(text: str = "No. Repetition isn't an argument.") -> Generation:
    return Generation(
        text=text,
        finish_reason="stop",
        model_identifier="some-model",
        latency_ms=812,
        rendered_prompt_hash="p" * 64,
        prompt_tokens=1200,
        completion_tokens=31,
        reasoning_tokens=440,
        # Everything a real adapter would hand over and Core discards.
        raw_meta={
            "reasoning_content": REASONING,
            "authorization": f"Bearer {SECRET}",
            "raw_body": {"choices": [{"message": {"reasoning": REASONING}}]},
        },
    )


def a_recording(**kwargs) -> Recording:
    return Recording.from_generation(
        a_generation(**kwargs),
        bundle_hash="b" * 64,
        render_version="chat-v1",
        adapter_key="openai_compatible",
        brain_alias="brain.reference",
        recorded_at=datetime(2026, 9, 21, 10, tzinfo=UTC),
    )


def test_a_recording_is_keyed_by_bundle_hash() -> None:
    assert a_recording().filename == f"{'b' * 64}.json"


def test_a_recording_carries_only_visible_generation_information() -> None:
    document = a_recording().as_document()
    assert set(document) == RECORDING_KEYS
    assert set(document["generation"]) == GENERATION_KEYS
    text = json.dumps(document)
    assert SECRET not in text
    assert REASONING not in text
    assert "raw_body" not in text and "authorization" not in text.lower()


def test_a_reasoning_token_count_survives_but_the_text_does_not() -> None:
    document = a_recording().as_document()
    assert document["generation"]["reasoning_tokens"] == 440
    assert "reasoning_content" not in json.dumps(document)


def test_a_recording_round_trips_to_a_generation() -> None:
    recording = a_recording()
    rebuilt = recording.to_generation()
    assert rebuilt.text == recording.text
    assert rebuilt.rendered_prompt_hash == recording.rendered_prompt_hash
    assert rebuilt.reasoning_tokens == 440
    # Provider metadata was never recorded, so replay cannot resurrect it.
    assert rebuilt.raw_meta == {}


def test_write_and_load_round_trip(tmp_path: pathlib.Path) -> None:
    path = write_recording(tmp_path, a_recording())
    assert path.name == f"{'b' * 64}.json"
    loaded = load_recording(path)
    assert loaded == a_recording()
    assert load_recordings(tmp_path) == [a_recording()]


def test_loading_a_missing_directory_is_empty_not_an_error(tmp_path: pathlib.Path) -> None:
    assert load_recordings(tmp_path / "nope") == []


def test_an_unknown_key_is_rejected_rather_than_ignored() -> None:
    document = a_recording().as_document()
    document["raw_body"] = {"anything": 1}
    with pytest.raises(RecordingError, match="unknown recording keys"):
        parse_recording(document)


def test_an_unknown_generation_key_is_rejected() -> None:
    document = a_recording().as_document()
    document["generation"]["reasoning_content"] = REASONING
    with pytest.raises(RecordingError, match="unknown generation keys"):
        parse_recording(document)


def test_a_wrong_version_is_rejected() -> None:
    document = a_recording().as_document()
    document["recording_version"] = 2
    with pytest.raises(RecordingError, match="recording_version"):
        parse_recording(document)


def test_two_recordings_disagreeing_on_the_same_prompt_are_an_error() -> None:
    first = a_recording()
    second = Recording.from_generation(
        a_generation(text="A different answer"),
        bundle_hash="c" * 64,
        render_version="chat-v1",
        adapter_key="openai_compatible",
        brain_alias="brain.reference",
    )
    with pytest.raises(RecordingError, match="different text"):
        index_by_prompt_hash([first, second])


# -- the fake brain actually replays what this module writes ---------------


def test_the_fake_brain_replays_a_written_recording(tmp_path: pathlib.Path) -> None:
    from tests.unit.test_brains import bundle as a_bundle  # the shared bundle helper

    brain = FakeBrain(mode=MODE_REPLAY, replay_dir=tmp_path)
    request = brain.render(a_bundle())
    write_recording(
        tmp_path,
        Recording.from_generation(
            Generation(
                text="Recorded answer.",
                finish_reason="stop",
                model_identifier="recorded/model",
                latency_ms=10,
                rendered_prompt_hash=request.prompt_hash,
                reasoning_tokens=7,
            ),
            bundle_hash="b" * 64,
            render_version="chat-v1",
            adapter_key="openai_compatible",
            brain_alias="brain.reference",
        ),
    )
    generation = brain.generate(request, GenerationParams())
    assert generation.text == "Recorded answer."


def test_replay_of_an_unrecorded_prompt_raises_rather_than_inventing(tmp_path) -> None:
    from tests.unit.test_brains import bundle as a_bundle

    brain = FakeBrain(mode=MODE_REPLAY, replay_dir=tmp_path)
    with pytest.raises(FakeBrainError, match="no recording"):
        brain.generate(brain.render(a_bundle()), GenerationParams())


def test_a_recording_made_under_another_renderer_does_not_match(tmp_path) -> None:
    """A render_version change alters the bytes; the old answer is not an answer."""
    from tests.unit.test_brains import bundle as a_bundle

    brain = FakeBrain(mode=MODE_REPLAY, replay_dir=tmp_path)
    write_recording(tmp_path, a_recording())  # a different rendered_prompt_hash
    with pytest.raises(FakeBrainError, match="no recording"):
        brain.generate(brain.render(a_bundle()), GenerationParams())
