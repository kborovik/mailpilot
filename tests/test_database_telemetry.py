"""Span-emission contract tests for database.py.

Verifies that ``database connection failed`` error log is emitted when
the database connection fails (e.g. database does not exist).
"""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from logfire.testing import CaptureLogfire

from mailpilot.database import (
    _MAILPILOT_VERSION,  # pyright: ignore[reportPrivateUsage]
    _compute_schema_hash,  # pyright: ignore[reportPrivateUsage]
    _connect_database,  # pyright: ignore[reportPrivateUsage]
    initialize_database,
)


def _db_spans(capfire: CaptureLogfire, name: str) -> list[dict[str, Any]]:
    """Return exported spans with the given name."""
    return [
        span
        for span in capfire.exporter.exported_spans_as_dict()
        if span["name"] == name
    ]


def _raise_connect_error(message: str) -> Any:
    """Return a side_effect that raises OperationalError with ``message``."""

    def _side_effect(*_args: Any, **_kwargs: Any) -> None:
        raise psycopg.OperationalError(message)

    return _side_effect


def test_initialize_database_error_emits_error_span(capfire: CaptureLogfire):
    with pytest.raises(SystemExit):
        initialize_database("postgresql://localhost/mailpilot_does_not_exist_xyz")

    error_spans = _db_spans(capfire, "database connection failed")
    assert len(error_spans) == 1
    attrs = error_spans[0]["attributes"]
    assert attrs["database"] == "mailpilot_does_not_exist_xyz"
    assert "createdb" in attrs["hint"]
    assert attrs["logfire.span_type"] == "log"


def test_initialize_database_error_emits_operator_error_event(
    capfire: CaptureLogfire,
    capsys: pytest.CaptureFixture[str],
):
    """§V.51/§B.71 + §V.137: connect-fail pairs log with
    ``operator_event("error", source="database.connect", ...)`` on the operator
    stderr console -- not silent on DB-connect failure from ``mailpilot run``."""
    with pytest.raises(SystemExit):
        initialize_database("postgresql://localhost/mailpilot_does_not_exist_xyz")

    err = capsys.readouterr().err
    assert "event=error" in err
    assert "source=database.connect" in err


@pytest.mark.parametrize(
    ("error_message", "hint_substring", "forbidden_substring"),
    [
        (
            'connection failed: FATAL:  role "ubuntu" does not exist',
            "role",
            "createdb",
        ),
        (
            'connection failed: FATAL:  database "mailpilot" does not exist',
            "createdb",
            None,
        ),
        (
            'connection failed: FATAL:  no pg_hba.conf entry for host "192.168.122.1", user "pilot", database "mailpilot", SSL encryption',
            "pg_hba",
            "database_url",
        ),
        (
            "connection failed: failed to resolve host 'mailpilot-1.vm.internal': [Errno 8] nodename nor servname provided, or not known",
            "resolve",
            "database_url",
        ),
        (
            'connection failed: could not translate host name "x" to address: Name or service not known',
            "hostname",
            "database_url",
        ),
        (
            'connection failed: FATAL:  password authentication failed for user "pilot"',
            "credential",
            None,
        ),
        (
            'connection failed: connection to server at "127.0.0.1", port 5432 failed: Connection refused',
            "PostgreSQL running",
            None,
        ),
        (
            "connection failed: some unknown libpq failure",
            "database_url",
            None,
        ),
    ],
)
def test_connect_database_maps_operational_error_to_hint(
    error_message: str,
    hint_substring: str,
    forbidden_substring: str | None,
) -> None:
    """§V.137: ordered OperationalError substring → SystemExit hint map."""
    with (
        patch(
            "mailpilot.database.psycopg.connect",
            side_effect=_raise_connect_error(error_message),
        ),
        pytest.raises(SystemExit) as exit_info,
    ):
        _connect_database("postgresql://localhost/mailpilot")

    exit_text = str(exit_info.value)
    assert "database connection failed:" in exit_text
    assert hint_substring.lower() in exit_text.lower()
    if forbidden_substring is not None:
        assert forbidden_substring.lower() not in exit_text.lower()


def test_connect_database_expected_fail_uses_logfire_error_not_exception() -> None:
    """§V.137: expected connect fail logs via logfire.error, not exception.

    logfire.exception dumps a Traceback to the operator console for controlled
    SystemExit paths; error keeps the event without the stack dump.
    """
    with (
        patch(
            "mailpilot.database.psycopg.connect",
            side_effect=_raise_connect_error(
                'FATAL:  no pg_hba.conf entry for host "192.168.122.1"'
            ),
        ),
        patch("mailpilot.database.logfire") as mock_logfire,
        pytest.raises(SystemExit) as exit_info,
    ):
        _connect_database("postgresql://localhost/mailpilot")

    mock_logfire.error.assert_called_once()
    mock_logfire.exception.assert_not_called()
    assert "pg_hba" in str(exit_info.value).lower()
    hint = mock_logfire.error.call_args.kwargs["hint"]
    assert "pg_hba" in str(hint).lower()


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
    probe_cursor = MagicMock()
    probe_cursor.fetchone.return_value = {"oid": "account"}

    # schema_metadata read returns a matching hash so no drift fires.
    from mailpilot.database import SCHEMA_PATH

    current_hash = _compute_schema_hash(SCHEMA_PATH.read_text())
    metadata_cursor = MagicMock()
    metadata_cursor.fetchone.return_value = {
        "mailpilot_version": _MAILPILOT_VERSION,
        "schema_hash": current_hash,
        "applied_at": datetime(2026, 1, 1, tzinfo=UTC),
    }

    mock_conn = MagicMock()

    def execute_side_effect(query: Any, *_params: Any) -> MagicMock:
        text = str(query)
        if "to_regclass" in text:
            return probe_cursor
        if "schema_metadata" in text:
            return metadata_cursor
        return MagicMock()

    mock_conn.execute.side_effect = execute_side_effect

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
