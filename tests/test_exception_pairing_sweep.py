"""§V.51 whole-surface sweep -- every ``logfire.exception`` on a run-reachable
module pairs ``operator_event("error", ...)`` in the same except block.

Per §B.71: the ``database/`` connect-fail path shipped a ``logfire.exception``
with no paired ``operator_event``, and the existing §V.51 coverage was per-site
behavioral tests on ``run.py`` paths only -- no sweep caught the gap. This
static AST sweep closes the recurrence class: any new ``logfire.exception``
added to a run-reachable module without a paired ``operator_event("error")`` in
its except block fails here.

``cli/`` is intentionally excluded: its ``logfire.exception`` sites live in
single-shot CLI commands (``account sync``, ``email send``/``reply``) that
surface errors via ``output_error`` / ``row["error"]`` envelopes, not the
run-loop operator console -- those sites are not reachable from ``mailpilot
run``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import mailpilot

# Modules on the `mailpilot run` execution path that carry logfire.exception
# sites (the sync + task loop and everything it drives at startup/steady state).
_RUN_REACHABLE_MODULES = (
    "database",
    "pubsub.py",
    "routing.py",
    "run.py",
    "sync.py",
)

_SRC_ROOT = Path(mailpilot.__file__).parent


def _module_paths(module_name: str) -> list[Path]:
    """Resolve a run-reachable module name to one or more source files."""
    path = _SRC_ROOT / module_name
    if path.is_dir():
        return sorted(p for p in path.rglob("*.py") if p.is_file())
    if module_name.endswith(".py"):
        return [_SRC_ROOT / module_name]
    return [_SRC_ROOT / f"{module_name}.py"]


def _is_logfire_exception(node: ast.AST) -> bool:
    """True when ``node`` is a ``logfire.exception(...)`` call."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "exception"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "logfire"
    )


def _is_operator_error(node: ast.AST) -> bool:
    """True when ``node`` is an ``operator_event("error", ...)`` call."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "operator_event"
        and len(node.args) >= 1
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "error"
    )


def _build_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """Map each node to its parent for upward except-handler lookup."""
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _enclosing_scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    """Return the block ``node`` must pair its ``operator_event`` within.

    The nearest enclosing ``except`` handler is the common case. A failure
    handler that receives the exception as a parameter (e.g.
    ``run._handle_agent_failure``) has no enclosing ``except`` -- it is *called*
    from one -- so the enclosing function body is the pairing scope instead.
    """
    current = parents.get(node)
    while current is not None:
        if isinstance(
            current,
            (ast.ExceptHandler, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            return current
        current = parents.get(current)
    return None


def _count_logfire_exceptions(tree: ast.AST) -> int:
    return sum(1 for node in ast.walk(tree) if _is_logfire_exception(node))


@pytest.mark.parametrize("module_name", _RUN_REACHABLE_MODULES)
def test_logfire_exception_paired_with_operator_error(module_name: str) -> None:
    """§V.51/§B.71: each ``logfire.exception`` in a run-reachable module pairs
    ``operator_event("error", ...)`` inside the same except block."""
    unpaired: list[str] = []
    for path in _module_paths(module_name):
        tree = ast.parse(path.read_text())
        parents = _build_parent_map(tree)
        for node in ast.walk(tree):
            if not _is_logfire_exception(node):
                continue
            scope = _enclosing_scope(node, parents)
            if scope is None or not any(
                _is_operator_error(inner) for inner in ast.walk(scope)
            ):
                unpaired.append(f"{path.name}:{getattr(node, 'lineno', -1)}")

    assert not unpaired, (
        f"{module_name}: logfire.exception at {unpaired} lack a paired "
        'operator_event("error", ...) in the same except block (§V.51, §B.71)'
    )


def test_sweep_actually_inspects_exception_sites() -> None:
    """Guard against a vacuous sweep -- the matcher must find the known
    ``logfire.exception`` sites, else a silently-broken matcher would let the
    pairing test pass over zero nodes."""
    total = sum(
        _count_logfire_exceptions(ast.parse(path.read_text()))
        for name in _RUN_REACHABLE_MODULES
        for path in _module_paths(name)
    )
    assert total >= 10, (
        f"expected the matcher to find the known run-reachable logfire.exception "
        f"sites; found only {total}"
    )
