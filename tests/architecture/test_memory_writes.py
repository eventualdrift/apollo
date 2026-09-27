"""Memory rows change only through the lifecycle module (spec D.1).

"Status transitions happen only in the lifecycle module, never by direct UPDATE
elsewhere." Two static checks hold that:

* every SQL statement that writes `memory` or `memory_observation` lives in
  `storage/repositories/memories.py`;
* the repository's writing methods are called from `memory/lifecycle.py` only.

The database enforces the rules themselves (migrations 0003, 0004); this test
keeps a second, unreviewed path from growing around them.
"""

from __future__ import annotations

import ast
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "apollo"
REPOSITORY = SRC / "storage" / "repositories" / "memories.py"
LIFECYCLE = SRC / "memory" / "lifecycle.py"

WRITE_SQL = re.compile(
    r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(public\.)?memory(_observation)?\b", re.I
)
WRITERS = frozenset(
    {
        "insert_memory",
        "insert_observation",
        "mark_confirmed",
        "mark_contradicted",
        "mark_superseded",
        "mark_archived",
        "mark_restored",
    }
)


def _sources() -> list[pathlib.Path]:
    return sorted(SRC.rglob("*.py"))


def test_only_the_memory_repository_writes_memory_rows() -> None:
    offenders = [
        str(path.relative_to(SRC))
        for path in _sources()
        if path != REPOSITORY and WRITE_SQL.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_the_repository_writers_exist() -> None:
    tree = ast.parse(REPOSITORY.read_text(encoding="utf-8"))
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert defined >= WRITERS


def test_only_the_lifecycle_calls_the_writers() -> None:
    offenders = []
    for path in _sources():
        if path in (REPOSITORY, LIFECYCLE):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in WRITERS:
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}:{node.attr}")
    assert offenders == []


def test_the_lifecycle_does_call_them() -> None:
    tree = ast.parse(LIFECYCLE.read_text(encoding="utf-8"))
    called = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert called >= WRITERS
