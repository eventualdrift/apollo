"""UUIDv7 — time-ordered ids, for index locality and cheap chronological reads."""

from __future__ import annotations

import os
import time
import uuid


def uuid7(now_ms: int | None = None) -> uuid.UUID:
    """RFC 9562 UUIDv7: 48-bit millisecond timestamp, version, variant, randomness."""
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    rand = os.urandom(10)
    b = bytearray(16)
    b[0:6] = ms.to_bytes(6, "big")
    b[6:16] = rand
    b[6] = (b[6] & 0x0F) | 0x70  # version 7
    b[8] = (b[8] & 0x3F) | 0x80  # RFC 4122 variant
    return uuid.UUID(bytes=bytes(b))
