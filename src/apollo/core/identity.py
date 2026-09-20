"""Canonical identity: file-backed, composed, hashed, snapshotted (ADR-0003).

Files are the source of truth for *authoring*; `identity_version` is an
immutable snapshot for *auditing*. A turn referencing `identity_hash` is
useless if the content behind that hash cannot be recovered, and git and the
running process diverge.

`CONTEXT_RULES` is deliberately not here. It describes the compiler's fence
syntax and is versioned with `compiler_version` (spec B.4); putting it in
identity would mean a fence-syntax change dirties the identity hash and
pollutes every persona diff with something unrelated to Apollo's character.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from apollo.errors import ConfigError

log = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.yaml"
FRAGMENT_DELIMITER = "\n\n===== {name} =====\n\n"


@dataclass(frozen=True)
class Fragment:
    path: str
    sha256: str


@dataclass(frozen=True)
class Identity:
    """A composed identity and its hash. Immutable."""

    version_label: str
    schema_version: int
    content: str
    content_hash: str
    fragments: tuple[Fragment, ...]

    def as_fragment_rows(self) -> list[dict[str, str]]:
        return [{"path": f.path, "sha256": f.sha256} for f in self.fragments]


def compose_identity(identity_dir: Path) -> Identity:
    """Deterministically compose the identity fragments named by the manifest.

    The header carries `schema_version` and `identity_version`, so a deliberate
    version bump changes the hash even when fragment content is unchanged. The
    hash covers the *composed text*, never filenames.
    """
    manifest_path = identity_dir / MANIFEST_NAME
    if not manifest_path.exists():
        raise ConfigError(f"identity manifest not found: {manifest_path}")
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}

    try:
        schema_version = int(manifest["schema_version"])
        version_label = str(manifest["identity_version"])
        fragment_names = list(manifest["fragments"])
    except KeyError as exc:
        raise ConfigError(f"identity manifest missing {exc.args[0]!r}") from None
    if not fragment_names:
        raise ConfigError("identity manifest lists no fragments")

    parts = [f"# Apollo identity\nschema_version: {schema_version}\n"
             f"identity_version: {version_label}\n"]
    fragments: list[Fragment] = []
    for name in fragment_names:
        path = identity_dir / str(name)
        if not path.exists():
            raise ConfigError(f"identity fragment not found: {path}")
        text = path.read_text(encoding="utf-8")
        parts.append(FRAGMENT_DELIMITER.format(name=name))
        parts.append(text.strip() + "\n")
        fragments.append(
            Fragment(path=str(name), sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
        )

    content = "".join(parts)
    return Identity(
        version_label=version_label,
        schema_version=schema_version,
        content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        fragments=tuple(fragments),
    )


class IdentityLoader:
    """Composes on first use and caches by hash; snapshots into the database once."""

    def __init__(self, identity_dir: Path) -> None:
        self._dir = identity_dir
        self._cached: Identity | None = None

    def load(self) -> Identity:
        if self._cached is None:
            self._cached = compose_identity(self._dir)
            log.info(
                "identity.composed",
                extra={
                    "identity_version": self._cached.version_label,
                    "identity_hash": self._cached.content_hash,
                    "count": len(self._cached.fragments),
                },
            )
        return self._cached

    def reload(self) -> Identity:
        self._cached = None
        return self.load()


def snapshot_identity(uow, identity: Identity, now: datetime) -> bool:  # type: ignore[no-untyped-def]
    """Record the composed identity if this hash has not been seen before.

    Returns True when newly recorded, so the caller can emit
    `identity.version_loaded` in the same transaction.
    """
    from apollo.storage.repositories import IdentityVersionRepository

    return IdentityVersionRepository(uow).snapshot(
        content_hash=identity.content_hash,
        version_label=identity.version_label,
        schema_version=identity.schema_version,
        content=identity.content,
        fragments=identity.as_fragment_rows(),
        now=now,
    )
