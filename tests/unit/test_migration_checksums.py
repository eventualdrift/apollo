"""The pinned pre-checksum digests are the migrations as they are on disk.

Deployments that applied 0001-0004 before the runner recorded digests are
verified against these pins. A pin that drifted from its file would either
refuse every such deployment or, worse, be quietly re-pinned to an edit.
"""

from __future__ import annotations

from apollo.storage.db import MIGRATIONS_DIR, PRE_CHECKSUM_SHA256, migration_sha256


def test_every_pre_checksum_pin_matches_its_file() -> None:
    for name, pinned in PRE_CHECKSUM_SHA256.items():
        text = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
        assert migration_sha256(text) == pinned, name


def test_the_pins_cover_exactly_the_migrations_that_predate_checksums() -> None:
    assert sorted(PRE_CHECKSUM_SHA256) == [
        "0001_initial.sql",
        "0002_tombstone_preserves_provenance.sql",
        "0003_memory_lifecycle.sql",
        "0004_invocation_manifest_shape.sql",
    ]


def test_line_endings_do_not_change_a_digest_and_sql_does() -> None:
    assert migration_sha256("SELECT 1;\r\nSELECT 2;\r\n") == migration_sha256(
        "SELECT 1;\nSELECT 2;\n"
    )
    assert migration_sha256("SELECT 1;\n") != migration_sha256("SELECT 2;\n")
