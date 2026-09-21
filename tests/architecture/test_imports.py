"""Spec A.3 dependency rules, enforced rather than documented.

This test exists from step 1 so it can never be retrofitted around violations
that already exist. It parses imports statically: it does not import the
modules, so a cycle or a heavy dependency cannot hide the violation.

The load-bearing rule is `brains/` -> context types only. That is what makes
"Apollo is not the model" (ADR-0001) structurally true: an adapter that cannot
reach storage cannot hold Apollo state.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "apollo"

# package -> packages it may import from, within apollo
ALLOWED: dict[str, frozenset[str]] = {
    "brains": frozenset({"context", "errors", "sanitise", "logging_setup"}),
    "context": frozenset({"memory", "core.identity", "errors", "logging_setup"}),
    "memory": frozenset({"storage", "errors", "logging_setup"}),
    "core": frozenset(
        {"memory", "context", "brains", "audit", "storage", "config", "errors",
         "sanitise", "logging_setup"}
    ),
    "audit": frozenset({"storage", "errors", "logging_setup"}),
    "storage": frozenset({"errors", "logging_setup", "config"}),
    "api": frozenset({"core", "config", "errors", "logging_setup"}),
    "cli": frozenset(
        {"core", "config", "errors", "logging_setup", "context", "brains", "storage", "evals"}
    ),
    # The eval subsystem drives a real turn through the real invocation path,
    # so it depends on everything a turn depends on. It is still a caller:
    # nothing in Apollo imports `evals/`.
    "evals": frozenset(
        {"core", "context", "brains", "audit", "storage", "config", "errors",
         "sanitise", "logging_setup"}
    ),
}

# Modules that are pure leaf utilities and may be imported by anything.
LEAVES = frozenset({"errors", "sanitise", "logging_setup", "config", "__init__"})


def _apollo_imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("apollo."):
                    found.add(alias.name.removeprefix("apollo."))
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import
                pytest.fail(f"{path}: relative imports are not used in this codebase")
            if node.module and node.module.startswith("apollo."):
                found.add(node.module.removeprefix("apollo."))
            elif node.module == "apollo":
                for alias in node.names:
                    found.add(alias.name)
    return found


def _package_of(module: str) -> str:
    return module.split(".", 1)[0]


def _modules() -> list[tuple[str, pathlib.Path]]:
    out = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).with_suffix("")
        parts = [p for p in rel.parts if p != "__init__"]
        out.append((".".join(parts) or "__init__", path))
    return out


@pytest.mark.parametrize("module,path", _modules(), ids=lambda v: v if isinstance(v, str) else "")
def test_module_respects_dependency_rules(module: str, path: pathlib.Path) -> None:
    package = _package_of(module)
    if package in LEAVES:
        return
    allowed = ALLOWED.get(package)
    assert allowed is not None, f"{module}: package {package!r} has no declared dependency rule"
    for imported in sorted(_apollo_imports(path)):
        if imported in LEAVES or _package_of(imported) == package:
            continue
        ok = _package_of(imported) in allowed or any(
            imported == rule or imported.startswith(rule + ".") for rule in allowed
        )
        assert ok, f"{module} imports apollo.{imported}, which {package}/ may not depend on"


def test_brains_cannot_reach_apollo_state() -> None:
    """The rule ADR-0001 rests on, asserted directly and by name."""
    forbidden = {"storage", "memory", "core", "audit"}
    offenders = []
    for path in sorted((SRC / "brains").rglob("*.py")):
        for imported in _apollo_imports(path):
            if _package_of(imported) in forbidden:
                offenders.append(f"{path.name} -> apollo.{imported}")
    assert not offenders, "brains/ must hold no Apollo state: " + "; ".join(offenders)


def test_brains_do_not_touch_a_database_driver() -> None:
    """An adapter that cannot open a connection cannot read Apollo state."""
    offenders = []
    for path in sorted((SRC / "brains").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for driver in ("psycopg", "sqlite3", "sqlalchemy"):
            if f"import {driver}" in text:
                offenders.append(f"{path.name} imports {driver}")
    assert not offenders, "; ".join(offenders)


def test_nothing_depends_on_the_eval_subsystem() -> None:
    """`evals/` is a caller, never a dependency.

    The suite measures Apollo; Apollo must not be built out of it. A module
    importing `evals/` would let a fixture change behaviour in production.
    """
    offenders = []
    for module, path in _modules():
        if _package_of(module) in {"evals", "cli"}:
            continue
        for imported in _apollo_imports(path):
            if _package_of(imported) == "evals":
                offenders.append(f"{module} -> apollo.{imported}")
    assert not offenders, "; ".join(offenders)
