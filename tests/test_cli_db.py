"""§V.177: one `_db(*, mutate=False)` CLI connection helper."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mailpilot.cli import (
    _db,  # pyright: ignore[reportPrivateUsage]
)

_CLI_PATH = Path(__file__).resolve().parents[1] / "src" / "mailpilot" / "cli.py"


def _cli_source() -> str:
    return _CLI_PATH.read_text(encoding="utf-8")


def _cli_module_imports() -> list[ast.Import | ast.ImportFrom]:
    tree = ast.parse(_cli_source())
    return [
        node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
    ]


def _function_named(name: str) -> ast.FunctionDef:
    tree = ast.parse(_cli_source())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found in cli.py")


def test_cli_module_does_not_import_database() -> None:
    """§V.2 / §V.177: heavy database import stays inside `_db`, not module level."""
    for node in _cli_module_imports():
        if isinstance(node, ast.ImportFrom):
            assert node.module != "mailpilot.database"
            names = {alias.name for alias in node.names}
            assert "initialize_database" not in names
            continue
        assert all(
            not alias.name.startswith("mailpilot.database") for alias in node.names
        )


def test_db_helper_lazy_imports_initialize_database() -> None:
    """§V.177 / §V.2: `_db` is the only caller of `initialize_database`."""
    tree = ast.parse(_cli_source())
    db_fn = _function_named("_db")
    db_range = range(db_fn.lineno, (db_fn.end_lineno or db_fn.lineno) + 1)
    call_lines: list[int] = []
    import_lines: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "initialize_database"
        ):
            call_lines.append(node.lineno)
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == "initialize_database" for alias in node.names
        ):
            import_lines.append(node.lineno)
    assert call_lines, "_db must call initialize_database"
    assert all(line in db_range for line in call_lines), call_lines
    assert import_lines, "_db must lazy-import initialize_database"
    assert all(line in db_range for line in import_lines), import_lines


def test_db_helper_does_not_wrap_cli_mutation() -> None:
    """§V.54 / §V.177: mutation logging stays per command, not in `_db`."""
    names = {
        node.id
        for node in ast.walk(_function_named("_db"))
        if isinstance(node, ast.Name)
    }
    assert "cli_mutation" not in names


def test_db_mutate_false_skips_schema_gate() -> None:
    """§V.177: default `_db()` does not require the current schema."""
    connection = MagicMock()
    with (
        patch("mailpilot.cli._database_url", return_value="postgresql://test"),
        patch(
            "mailpilot.database.initialize_database", return_value=connection
        ) as init,
        _db() as yielded,
    ):
        assert yielded is connection
    init.assert_called_once_with("postgresql://test", require_current_schema=False)


def test_db_mutate_true_requires_current_schema() -> None:
    """§V.109 / §V.177: `_db(mutate=True)` requests the write-path schema gate."""
    connection = MagicMock()
    with (
        patch("mailpilot.cli._database_url", return_value="postgresql://test"),
        patch(
            "mailpilot.database.initialize_database", return_value=connection
        ) as init,
        _db(mutate=True) as yielded,
    ):
        assert yielded is connection
    init.assert_called_once_with("postgresql://test", require_current_schema=True)


def test_db_closes_connection_on_success() -> None:
    """§V.177: connection.close runs after a successful with-block."""
    connection = MagicMock()
    with (
        patch("mailpilot.cli._database_url", return_value="postgresql://test"),
        patch("mailpilot.database.initialize_database", return_value=connection),
        _db() as yielded,
    ):
        yielded.execute("select 1")
    connection.close.assert_called_once_with()


def test_db_closes_connection_on_error() -> None:
    """§V.177: connection.close runs when the with-block raises."""
    connection = MagicMock()
    with (
        patch("mailpilot.cli._database_url", return_value="postgresql://test"),
        patch("mailpilot.database.initialize_database", return_value=connection),
        pytest.raises(RuntimeError, match="boom"),
        _db(),
    ):
        raise RuntimeError("boom")
    connection.close.assert_called_once_with()
