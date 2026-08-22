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


def test_initialize_database_only_inside_db() -> None:
    """§V.177 / §V.2: `initialize_database(` lives only in `_db`; no module-level database import."""
    source = _CLI_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert node.module != "mailpilot.database"
        elif isinstance(node, ast.Import):
            assert all(
                not alias.name.startswith("mailpilot.database") for alias in node.names
            )
    db_fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_db"
    )
    db_source = ast.get_source_segment(source, db_fn)
    assert db_source is not None
    in_helper = db_source.count("initialize_database(")
    assert in_helper == 1
    assert source.count("initialize_database(") == in_helper


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


def test_db_mutate_true_commits_before_close() -> None:
    """§V.177: mutate success commits before close."""
    connection = MagicMock()
    with (
        patch("mailpilot.cli._database_url", return_value="postgresql://test"),
        patch("mailpilot.database.initialize_database", return_value=connection),
        _db(mutate=True),
    ):
        pass
    connection.commit.assert_called_once_with()
    connection.close.assert_called_once_with()
    names = [item[0] for item in connection.method_calls]
    assert names.index("commit") < names.index("close")


def test_db_mutate_true_skips_commit_on_error() -> None:
    """§V.177: exception / output_error skip commit so the txn rolls back."""
    connection = MagicMock()
    with (
        patch("mailpilot.cli._database_url", return_value="postgresql://test"),
        patch("mailpilot.database.initialize_database", return_value=connection),
        pytest.raises(SystemExit),
        _db(mutate=True),
    ):
        raise SystemExit(1)
    connection.commit.assert_not_called()
    connection.close.assert_called_once_with()


def test_db_mutate_false_does_not_commit() -> None:
    """§V.177: read-path `_db()` does not commit."""
    connection = MagicMock()
    with (
        patch("mailpilot.cli._database_url", return_value="postgresql://test"),
        patch("mailpilot.database.initialize_database", return_value=connection),
        _db(),
    ):
        pass
    connection.commit.assert_not_called()
    connection.close.assert_called_once_with()
