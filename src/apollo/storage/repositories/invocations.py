from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from apollo.storage.ids import uuid7
from apollo.storage.unit_of_work import UnitOfWork

PURPOSE_REPLY = "reply"
PURPOSE_MEMORY_PROPOSAL = "memory_proposal"
#: Closed enum. Adding a value requires a specification change (ADR-0013).
PURPOSES = (PURPOSE_REPLY, PURPOSE_MEMORY_PROPOSAL)


class InvocationRepository:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def start(
        self,
        *,
        turn_id: uuid.UUID,
        purpose: str,
        brain_alias: str,
        provider_key: str,
        adapter_key: str,
        render_version: str,
        compiler_version: str,
        token_estimator: str,
        context_manifest: list[dict[str, Any]],
        context_bundle_hash: str,
        context_token_estimate: int,
        max_trust_tier: str,
        taint: int,
        generation_params: dict[str, Any],
        now: datetime,
        retry_of_invocation_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """Insert a `started` invocation. Committed *before* the adapter is entered."""
        if purpose not in PURPOSES:
            raise ValueError(f"unknown invocation purpose {purpose!r}")
        invocation_id = uuid7()
        self._uow.execute(
            "INSERT INTO model_invocation (id, turn_id, seq, purpose, retry_of_invocation_id,"
            "  brain_alias, provider_key, adapter_key, render_version, compiler_version,"
            "  token_estimator, context_manifest, context_bundle_hash, context_token_estimate,"
            "  max_trust_tier, taint, generation_params, status, started_at)"
            " SELECT %s, %s, coalesce(max(seq), 0) + 1, %s, %s, %s, %s, %s, %s, %s, %s,"
            "        %s::jsonb, %s, %s, %s, %s, %s::jsonb, 'started', %s"
            "   FROM model_invocation WHERE turn_id = %s",
            (
                invocation_id,
                turn_id,
                purpose,
                retry_of_invocation_id,
                brain_alias,
                provider_key,
                adapter_key,
                render_version,
                compiler_version,
                token_estimator,
                json.dumps(context_manifest, sort_keys=True),
                context_bundle_hash,
                context_token_estimate,
                max_trust_tier,
                taint,
                json.dumps(generation_params, sort_keys=True),
                now,
                turn_id,
            ),
        )
        return invocation_id

    def complete(
        self,
        *,
        invocation_id: uuid.UUID,
        now: datetime,
        latency_ms: int,
        model_identifier: str | None,
        rendered_prompt_hash: str | None,
        finish_reason: str | None,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        reasoning_tokens: int | None,
    ) -> None:
        self._uow.execute(
            "UPDATE model_invocation SET status = 'completed', completed_at = %s, latency_ms = %s,"
            "  model_identifier = %s, rendered_prompt_hash = %s, finish_reason = %s,"
            "  prompt_tokens = %s, completion_tokens = %s, reasoning_tokens = %s"
            " WHERE id = %s AND status = 'started'",
            (
                now,
                latency_ms,
                model_identifier,
                rendered_prompt_hash,
                finish_reason,
                prompt_tokens,
                completion_tokens,
                reasoning_tokens,
                invocation_id,
            ),
        )

    def fail(
        self,
        *,
        invocation_id: uuid.UUID,
        now: datetime,
        latency_ms: int | None,
        error_kind: str,
        error_detail: str | None,
    ) -> None:
        self._uow.execute(
            "UPDATE model_invocation SET status = 'failed', completed_at = %s, latency_ms = %s,"
            "  error_kind = %s, error_detail = %s WHERE id = %s AND status = 'started'",
            (now, latency_ms, error_kind, error_detail, invocation_id),
        )

    def get(self, invocation_id: uuid.UUID) -> dict[str, Any] | None:
        cur = self._uow.execute(
            "SELECT * FROM model_invocation WHERE id = %s", (invocation_id,)
        )
        row: dict[str, Any] | None = cur.fetchone()
        return row

    def is_started(self, invocation_id: uuid.UUID) -> bool:
        """Used by the single invocation path to refuse an unrecorded model call."""
        cur = self._uow.execute(
            "SELECT 1 FROM model_invocation WHERE id = %s AND status = 'started'",
            (invocation_id,),
        )
        return cur.fetchone() is not None

    def for_turn(self, turn_id: uuid.UUID) -> list[dict[str, Any]]:
        cur = self._uow.execute(
            "SELECT * FROM model_invocation WHERE turn_id = %s ORDER BY seq", (turn_id,)
        )
        rows: list[dict[str, Any]] = cur.fetchall()
        return rows

    def expire_orphans(self, *, older_than: datetime, now: datetime) -> int:
        cur = self._uow.execute(
            "UPDATE model_invocation SET status = 'failed', completed_at = %s,"
            "  error_kind = 'interrupted'"
            " WHERE status = 'started' AND started_at < %s RETURNING id",
            (now, older_than),
        )
        return len(cur.fetchall())
