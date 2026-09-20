from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from apollo.storage.unit_of_work import UnitOfWork


class IdentityVersionRepository:
    """Insert on first sight, never update (spec C.11)."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def snapshot(
        self,
        *,
        content_hash: str,
        version_label: str,
        schema_version: int,
        content: str,
        fragments: list[dict[str, str]],
        now: datetime,
    ) -> bool:
        """Returns True when this hash was newly recorded."""
        cur = self._uow.execute(
            "INSERT INTO identity_version (content_hash, version_label, schema_version,"
            "  content, fragments, first_loaded_at)"
            " VALUES (%s, %s, %s, %s, %s::jsonb, %s)"
            " ON CONFLICT (content_hash) DO NOTHING RETURNING content_hash",
            (
                content_hash,
                version_label,
                schema_version,
                content,
                json.dumps(fragments),
                now,
            ),
        )
        return cur.fetchone() is not None

    def get(self, content_hash: str) -> dict[str, Any] | None:
        cur = self._uow.execute(
            "SELECT * FROM identity_version WHERE content_hash = %s", (content_hash,)
        )
        return cur.fetchone()
