"""Run-record conventions (spec J.2, brief §23)."""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime

from apollo.evals.models import RunRecord
from apollo.evals.runner import to_document

REPO = pathlib.Path(__file__).resolve().parents[2]


def a_record(**kwargs) -> RunRecord:
    defaults = dict(
        run_id="r1",
        started_at=datetime(2026, 9, 21, 8, 56, 31, tzinfo=UTC),
        brain_alias="brain.reference",
        provider_key="reference",
        adapter_key="openai_compatible",
        render_version="chat-v1",
        compiler_version="compiler-v1",
        token_estimator="conservative-v1",
        identity_version="2026.09.20-1",
        identity_hash="bdcda4269cf41cb8f6f89b2f8a93120e7e51f0024dd18b1eebf2ed2927725fa8",
        generation_params={"temperature": 0.0, "seed": 7},
        determinism={},
    )
    defaults.update(kwargs)
    return RunRecord(**defaults)  # type: ignore[arg-type]


def test_the_filename_carries_timestamp_brain_and_identity_prefix() -> None:
    assert a_record().filename == "20260921T085631Z-brain_reference-bdcda426.json"


def test_two_brains_at_the_same_identity_do_not_collide() -> None:
    a = a_record().filename
    b = a_record(brain_alias="brain.local").filename
    assert a != b


def test_the_document_carries_everything_needed_months_later() -> None:
    document = to_document(a_record())
    assert set(document) >= {
        "suite_version", "run_id", "started_at", "brain_alias", "provider_key", "adapter_key",
        "render_version", "compiler_version", "token_estimator", "identity_version",
        "identity_hash", "generation_params", "determinism", "cases",
    }


def test_run_output_is_not_committed() -> None:
    """Run records contain verbatim model output; they stay out of git."""
    ignore = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert any(line.strip().rstrip("/") == "evals/runs" for line in ignore)
