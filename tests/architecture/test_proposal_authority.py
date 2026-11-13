"""The model may propose a memory. It may never create one (spec D.3/2, ADR-0006).

"Proposal and authorisation are separate operations. The model may produce a
proposal. It may not approve one. Creating a memory requires a user-originated
call referencing the proposal id."

The database holds its half (migration 0005: no approved-proposal memory
without a saved proposal). This test holds the code's half, statically: from
the modules that run a turn, nothing that can create, change or approve a
memory is reachable by import. A turn can therefore write a proposal row and
nothing more, however a generation is worded.

It reads imports without importing anything, as `test_imports.py` does, and
follows them transitively: a path through three modules is still a path.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "apollo"

#: Where a generation is requested and its text handled: the turn, the one
#: path that calls a brain, retrieval, the compiler, and every adapter.
GENERATION_PATH = ("core.turns", "core.invocations", "core.retrieval", "context.compiler")

#: The only modules that can make or change a memory, or approve a proposal.
AUTHORITY = frozenset({
    "memory.approval",    # a proposal becomes a memory here, and only here
    "memory.lifecycle",   # every memory row change
    "core.proposals",     # the user-facing Save / Edit and Save / Ignore
    "core.memories",      # the user-facing direct entry
})


def _modules() -> dict[str, pathlib.Path]:
    found = {}
    for path in sorted(SRC.rglob("*.py")):
        parts = list(path.relative_to(SRC).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        found[".".join(parts)] = path
    return found


MODULES = _modules()


def _imports(path: pathlib.Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            found.update(a.name.removeprefix("apollo.") for a in node.names
                         if a.name.startswith("apollo."))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "apollo":
                found.update(a.name for a in node.names)
            elif node.module.startswith("apollo."):
                base = node.module.removeprefix("apollo.")
                found.add(base)
                # `from apollo.memory import approval` imports a module too.
                found.update(f"{base}.{a.name}" for a in node.names
                             if f"{base}.{a.name}" in MODULES)
    return found


def _reachable(start: str) -> dict[str, str]:
    """Every apollo module importing `start` loads, with the module that led there."""
    seen: dict[str, str] = {}
    todo = [(start, "")]
    while todo:
        module, via = todo.pop()
        if module in seen or module not in MODULES:
            continue
        seen[module] = via
        parts = module.split(".")
        todo.extend((".".join(parts[:i]), module) for i in range(1, len(parts)))  # its packages
        todo.extend((imported, module) for imported in _imports(MODULES[module]))
    return seen


def _path_to(reached: dict[str, str], module: str) -> str:
    chain = [module]
    while reached[chain[-1]]:
        chain.append(reached[chain[-1]])
    return " <- ".join(chain)


def test_the_generation_path_cannot_reach_what_creates_a_memory() -> None:
    brains = sorted(m for m in MODULES if m.startswith("brains."))
    assert brains, "no adapters found: the test is looking in the wrong place"
    offenders = []
    for start in (*GENERATION_PATH, *brains):
        assert start in MODULES, start
        reached = _reachable(start)
        offenders += [_path_to(reached, module) for module in sorted(AUTHORITY & set(reached))]
    assert offenders == []


def test_the_authority_modules_exist_and_the_turn_does_propose() -> None:
    """Guards the guard: a renamed module would otherwise pass the test above for free."""
    assert set(MODULES) >= AUTHORITY
    assert "memory.proposals" in _reachable("core.turns")


def test_only_the_approval_module_turns_a_proposal_into_a_memory() -> None:
    """One source kind says "a model structured this"; one module may write it."""
    users = sorted(
        module for module, path in MODULES.items()
        if "MODEL_PROPOSAL_APPROVED" in path.read_text(encoding="utf-8")
    )
    # `memory.models` defines it and `core.proposals` names it in the audit event.
    assert users == ["core.proposals", "memory.approval", "memory.models"]


def test_only_the_user_facing_surface_reaches_approval() -> None:
    importers = sorted(
        module for module, path in MODULES.items() if "memory.approval" in _imports(path)
    )
    assert importers == ["core.proposals"]
    callers = sorted(
        module for module, path in MODULES.items() if "core.proposals" in _imports(path)
    )
    assert callers == ["cli.main", "cli.proposal"]
