"""ADR-07 span-emission contract tests for database.py.

Verifies that ``database connection failed`` error log is emitted when
the database connection fails (e.g. database does not exist).
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from logfire.testing import CaptureLogfire

from mailpilot.database import initialize_database


def _db_spans(capfire: CaptureLogfire, name: str) -> list[dict[str, Any]]:
    """Return exported spans with the given name."""
    return [
        span
        for span in capfire.exporter.exported_spans_as_dict()
        if span["name"] == name
    ]


def test_initialize_database_error_emits_error_span(capfire: CaptureLogfire):
    with pytest.raises(SystemExit):
        initialize_database("postgresql://localhost/mailpilot_does_not_exist_xyz")

    error_spans = _db_spans(capfire, "database connection failed")
    assert len(error_spans) == 1
    attrs = error_spans[0]["attributes"]
    assert attrs["database"] == "mailpilot_does_not_exist_xyz"
    assert "createdb" in attrs["hint"]
    assert attrs["logfire.span_type"] == "log"


def test_initialize_database_skips_schema_when_account_table_exists():
    """Reapplying schema.sql while the sync loop is running deadlocks.

    schema.sql contains ``DROP TRIGGER IF EXISTS task_pending_trigger`` and
    ``CREATE TRIGGER`` statements that take AccessExclusiveLock on the
    ``task`` table. The sync loop (or the agent calling create_task) holds
    a RowExclusiveLock from an INSERT INTO task. The two collide as a
    PostgreSQL deadlock which kills any CLI command that opens a fresh
    connection while the loop is busy.

    Probing for ``account`` via ``to_regclass`` is the cheap idempotency
    gate that avoids the lock entirely on already-initialized databases.
    """
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    # Probe returns a non-None oid -- table exists.
    mock_cursor.fetchone.return_value = {"oid": "account"}
    mock_conn.execute.return_value = mock_cursor

    with patch("mailpilot.database.psycopg.connect", return_value=mock_conn):
        initialize_database("postgresql://localhost/test")

    executed = [str(call.args[0]) for call in mock_conn.execute.call_args_list]
    assert any("to_regclass" in q for q in executed), (
        "expected existence probe before schema apply"
    )
    assert not any("CREATE TABLE" in q for q in executed), (
        "must skip schema apply when account table exists"
    )


def test_initialize_database_applies_schema_when_account_table_missing():
    """Fresh database must still get schema applied on first connection."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    # Probe returns None -- table does not exist yet.
    mock_cursor.fetchone.return_value = {"oid": None}
    mock_conn.execute.return_value = mock_cursor

    with patch("mailpilot.database.psycopg.connect", return_value=mock_conn):
        initialize_database("postgresql://localhost/test")

    executed = [str(call.args[0]) for call in mock_conn.execute.call_args_list]
    assert any("CREATE TABLE" in q for q in executed), (
        "must apply schema when account table does not exist"
    )
