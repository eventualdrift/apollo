"""retrieval-notice-ablation-1 against real PostgreSQL, with a fake provider.

Proves on the real invocation path: one invocation row per case, never a
retry, a failed generation consumes its attempt, every row carries the
candidate compiler version and sealed bundle hash, reconciliation reads it
back, the provider saw the candidate notice — and a personal turn in the same
database still receives the production notice.
"""

from __future__ import annotations

import json

import pytest

from apollo.brains.base import Generation, GenerationParams, RenderedRequest
from apollo.brains.fake import FakeBrain
from apollo.brains.registry import BrainRegistry
from apollo.config import MODE_BENCHMARK, SURFACE_EVAL, Config, ProviderConfig
from apollo.context.bundle import BlockType, ContextBundle
from apollo.context.compiler import NO_RETRIEVAL_NOTICE
from apollo.core.conversations import create_conversation
from apollo.core.identity import IdentityLoader, compose_identity
from apollo.errors import BrainTransportError
from apollo.evals import notice_ablation as na
from apollo.evals.loader import load_cases
from tests.integration.conftest import REPO
from tests.unit.test_notice_ablation import CASES_DIR, _plan

pytestmark = pytest.mark.integration


class RecordingBrain(FakeBrain):
    """Answers with a fixed identifier; fails per_020 at the transport level."""

    def __init__(self) -> None:
        super().__init__(key="qwen")
        self.notices: list[str] = []
        self.generate_calls = 0

    def capabilities(self):  # type: ignore[no-untyped-def]
        import dataclasses

        return dataclasses.replace(super().capabilities(), max_context=9216)

    def render(self, bundle: ContextBundle) -> RenderedRequest:
        (notice,) = [b for b in bundle.blocks if b.block_type is BlockType.RETRIEVAL_NOTICE]
        self.notices.append(notice.content)
        return super().render(bundle)

    def generate(self, req: RenderedRequest, params: GenerationParams) -> Generation:
        self.generate_calls += 1
        if "Proxmox" in req.messages[-1].content:
            raise BrainTransportError(provider_error_type="connection_failed")
        return Generation(
            text="I have no record of that.", finish_reason="stop",
            model_identifier="qwen-served", latency_ms=5, rendered_prompt_hash=req.prompt_hash,
            prompt_tokens=10, completion_tokens=5, reasoning_tokens=0,
        )


def _config(dsn: str) -> Config:
    qwen = ProviderConfig(key="qwen", kind="fake", allowed_modes=(MODE_BENCHMARK,),
                          eval_only=True, context_budget=8000, reserved_output=1024)
    fake = ProviderConfig(key="fake", kind="fake", allowed_modes=("personal", "benchmark"),
                          eval_only=False, context_budget=8000, reserved_output=1024)
    return Config(database_dsn=dsn, providers={"qwen": qwen, "fake": fake},
                  brain_aliases={"default": "fake", "qwen": "qwen"},
                  identity_dir=REPO / "identity", identity_token_cap=4000)


def test_candidate_run_on_the_real_invocation_path(db, clock, tmp_path, service_factory):
    cases = load_cases(CASES_DIR)
    identity = compose_identity(REPO / "identity")
    plan = _plan(tmp_path, identity, cases)
    config = _config(db._dsn)
    registry = BrainRegistry(config, surface=SURFACE_EVAL)
    brain = RecordingBrain()
    registry._cache["brain.qwen"] = brain
    out = tmp_path / "exp"

    document = na.run_candidate(
        db=db, config=config, registry=registry,
        identity_loader=IdentityLoader(config.identity_dir), plan=plan, model_key="qwen",
        cases=cases, out_dir=out, clock=clock,
    )

    # Exactly five provider calls; the failed one was not retried or replaced.
    assert brain.generate_calls == 5
    # Five preflight renders (prompt-hash check, no generation) then five run renders.
    assert brain.notices == [na.CANDIDATE_RETRIEVAL_NOTICE] * 10
    statuses = {p["case_id"]: p["candidate_notice"]["status"] for p in document["pairs"]}
    assert statuses.pop("per_020_retrieval_failure_vs_emptiness") == "failed"
    assert set(statuses.values()) == {"completed"}

    rec = document["reconciliation"]
    assert rec["consistent"] is True, rec["problems"]
    assert rec["provider_generations"] == 5 and len(rec["rows"]) == 5
    assert all(row["not_a_retry"] and row["seq_is_1"] for row in rec["rows"])

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT i.compiler_version, i.context_bundle_hash, i.retry_of_invocation_id,"
            " c.mode FROM model_invocation i JOIN turn t ON t.id = i.turn_id"
            " JOIN conversation c ON c.id = t.conversation_id WHERE c.id = %s",
            (document["run"]["conversation_id"],),
        ).fetchall()
    assert len(rows) == 5
    assert {r["compiler_version"] for r in rows} == {"compiler-v1-rn1"}
    assert {r["mode"] for r in rows} == {MODE_BENCHMARK}
    assert all(r["retry_of_invocation_id"] is None for r in rows)
    assert {r["context_bundle_hash"] for r in rows} == set(
        plan["models"]["qwen"]["candidate_bundle_hashes"].values())

    written = json.loads((out / "qwen-candidate-run.json").read_text())
    assert written["pairs"][1]["historical_current_notice"][0]["response"].startswith(
        "historical qwen")

    # A second run for the same model is refused before any provider call.
    with pytest.raises(na.AblationError):
        na.run_candidate(db=db, config=config, registry=registry,
                         identity_loader=IdentityLoader(config.identity_dir), plan=plan,
                         model_key="qwen", cases=cases, out_dir=out, clock=clock)
    assert brain.generate_calls == 5

    # Personal Apollo, same database, same moment: still the production notice.
    personal_brain = RecordingBrain()
    service = service_factory(brain=personal_brain, config=config)
    conversation = create_conversation(db, now=clock(), title="personal")
    result = service.submit(conversation_id=conversation, text="Anything about the NAS?")
    assert result.status == "completed"
    assert personal_brain.notices == [NO_RETRIEVAL_NOTICE]
