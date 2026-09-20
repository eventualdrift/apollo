"""Identity composition, hashing and fragment tracking (ADR-0003)."""

from __future__ import annotations

import pathlib

import pytest

from apollo.core.identity import IdentityLoader, compose_identity
from apollo.errors import ConfigError

REPO = pathlib.Path(__file__).resolve().parents[2]


def _make(tmp_path: pathlib.Path, *, version: str = "1.0", fragments=("a.md", "b.md"),
          bodies=("alpha", "beta")) -> pathlib.Path:
    d = tmp_path / "identity"
    d.mkdir(exist_ok=True)
    (d / "manifest.yaml").write_text(
        f'schema_version: 1\nidentity_version: "{version}"\nfragments:\n'
        + "".join(f"  - {f}\n" for f in fragments)
    )
    for name, body in zip(fragments, bodies, strict=True):
        (d / name).write_text(body + "\n")
    return d


def test_composition_is_deterministic(tmp_path: pathlib.Path) -> None:
    d = _make(tmp_path)
    assert compose_identity(d).content == compose_identity(d).content
    assert compose_identity(d).content_hash == compose_identity(d).content_hash


def test_hash_changes_when_fragment_content_changes(tmp_path: pathlib.Path) -> None:
    d = _make(tmp_path)
    before = compose_identity(d).content_hash
    (d / "a.md").write_text("alpha, revised\n")
    assert compose_identity(d).content_hash != before


def test_hash_changes_on_a_deliberate_version_bump_alone(tmp_path: pathlib.Path) -> None:
    """The header carries the version, so relabelling is a real change (ADR-0003)."""
    d = _make(tmp_path, version="1.0")
    before = compose_identity(d).content_hash
    _make(tmp_path, version="1.1")
    assert compose_identity(d).content_hash != before


def test_hash_changes_when_fragment_order_changes(tmp_path: pathlib.Path) -> None:
    d = _make(tmp_path, fragments=("a.md", "b.md"))
    before = compose_identity(d).content_hash
    _make(tmp_path, fragments=("b.md", "a.md"), bodies=("beta", "alpha"))
    assert compose_identity(d).content_hash != before


def test_per_fragment_hashes_name_which_fragment_moved(tmp_path: pathlib.Path) -> None:
    d = _make(tmp_path)
    before = {f.path: f.sha256 for f in compose_identity(d).fragments}
    (d / "b.md").write_text("beta, revised\n")
    after = {f.path: f.sha256 for f in compose_identity(d).fragments}
    assert before["a.md"] == after["a.md"]
    assert before["b.md"] != after["b.md"]


def test_missing_fragment_is_an_error(tmp_path: pathlib.Path) -> None:
    d = _make(tmp_path)
    (d / "b.md").unlink()
    with pytest.raises(ConfigError, match="fragment not found"):
        compose_identity(d)


def test_loader_caches_until_reloaded(tmp_path: pathlib.Path) -> None:
    d = _make(tmp_path)
    loader = IdentityLoader(d)
    first = loader.load()
    (d / "a.md").write_text("changed\n")
    assert loader.load().content_hash == first.content_hash
    assert loader.reload().content_hash != first.content_hash


def test_real_identity_composes_and_excludes_context_rules() -> None:
    identity = compose_identity(REPO / "identity")
    assert [f.path for f in identity.fragments] == ["core.md", "behaviour.md", "relationship.md"]
    assert identity.version_label == "2026.09.20-1"
    # All 25 behavioural rules made it across.
    for n in range(1, 26):
        assert f"**B{n} `[" in identity.content, f"rule B{n} missing from composed identity"
    # CONTEXT_RULES belongs to the compiler, not to identity (spec B.4).
    assert "CONTEXT_RULES" not in identity.content
    # The persona probe list is eval planning and must not reach the model.
    assert "Initial persona suite coverage" not in identity.content
