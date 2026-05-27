"""Tests for schema_metadata write, drift detection, and status envelope.

Covers §V.58 (drift warn + status envelope) and §V.59 (normalized hash).
"""

import hashlib
import re
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from logfire.testing import CaptureLogfire

from mailpilot import database as db_mod
from mailpilot.database import (
    _MAILPILOT_VERSION,  # pyright: ignore[reportPrivateUsage]
    SCHEMA_PATH,
    _compute_schema_hash,  # pyright: ignore[reportPrivateUsage]
    get_status_counts,
    initialize_database,
)
from mailpilot.models import SchemaMetadata

# -- _compute_schema_hash (§V.59) ---------------------------------------------


def test_compute_schema_hash_ignores_added_comments():
    sql_a = "CREATE TABLE foo (id INT);"
    sql_b = "-- new comment line\nCREATE TABLE foo (id INT);"
    assert _compute_schema_hash(sql_a) == _compute_schema_hash(sql_b)


def test_compute_schema_hash_collapses_whitespace_runs():
    """Newlines and multi-space runs at the same position collapse to one space."""
    sql_a = "CREATE TABLE foo (id INT);"
    sql_b = "CREATE  TABLE  foo  (id   INT);\n"
    assert _compute_schema_hash(sql_a) == _compute_schema_hash(sql_b)


def test_compute_schema_hash_flips_on_column_added():
    sql_a = "CREATE TABLE foo (id INT);"
    sql_b = "CREATE TABLE foo (id INT, name TEXT);"
    assert _compute_schema_hash(sql_a) != _compute_schema_hash(sql_b)


def test_compute_schema_hash_matches_spec_recipe():
    """Hash MUST equal sha256(re-stripped, re-collapsed, encoded)."""
    sql = "-- header comment\nCREATE TABLE foo ( id  INT );"
    normalized = re.sub(r"--[^\n]*", "", sql)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    expected = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    assert _compute_schema_hash(sql) == expected


# -- initialize_database write-on-apply ---------------------------------------


def test_initialize_writes_metadata_row_on_fresh_db():
    """Probe-empty branch: insert schema_metadata with current version + hash."""
    probe_cursor = MagicMock()
    probe_cursor.fetchone.return_value = {"oid": None}

    mock_conn = MagicMock()
    mock_conn.execute.return_value = probe_cursor

    with patch("mailpilot.database.psycopg.connect", return_value=mock_conn):
        initialize_database("postgresql://localhost/test")

    inserts = [
        call
        for call in mock_conn.execute.call_args_list
        if "INSERT INTO schema_metadata" in str(call.args[0])
    ]
    assert len(inserts) == 1
    current_hash = _compute_schema_hash(SCHEMA_PATH.read_text())
    assert inserts[0].args[1] == (_MAILPILOT_VERSION, current_hash)


# -- initialize_database drift branches (§V.58) -------------------------------


def _existing_db_mock(
    *, recorded_hash: str | None, recorded_version: str = "0.0.0"
) -> MagicMock:
    """Build a psycopg-connection mock that simulates a probe-non-empty DB.

    ``recorded_hash=None`` simulates a legacy DB (table or row missing) via
    ``psycopg.errors.UndefinedTable``.
    """
    probe_cursor = MagicMock()
    probe_cursor.fetchone.return_value = {"oid": "account"}

    metadata_cursor = MagicMock()
    if recorded_hash is None:
        metadata_cursor.fetchone.side_effect = psycopg.errors.UndefinedTable(
            "relation schema_metadata does not exist"
        )
    else:
        metadata_cursor.fetchone.return_value = {
            "mailpilot_version": recorded_version,
            "schema_hash": recorded_hash,
            "applied_at": datetime(2020, 1, 1, tzinfo=UTC),
        }

    mock_conn = MagicMock()
    call_log: list[Any] = []

    def execute_side_effect(query: Any, *params: Any) -> MagicMock:
        text = str(query)
        call_log.append(text)
        if "to_regclass" in text:
            return probe_cursor
        if "schema_metadata" in text:
            if recorded_hash is None:
                # raise on the .execute call to simulate UndefinedTable
                raise psycopg.errors.UndefinedTable(
                    "relation schema_metadata does not exist"
                )
            return metadata_cursor
        return MagicMock()

    mock_conn.execute.side_effect = execute_side_effect
    return mock_conn


def _warn_spans(capfire: CaptureLogfire) -> list[dict[str, Any]]:
    return [
        span
        for span in capfire.exporter.exported_spans_as_dict()
        if span["name"] == "schema drift detected"
    ]


def test_initialize_drift_emits_warn_and_event(
    capfire: CaptureLogfire, capsys: pytest.CaptureFixture[str]
):
    """Stale recorded hash → logfire.warn + operator_event("schema.drift"), no SystemExit."""
    stale_hash = "deadbeef" * 8
    mock_conn = _existing_db_mock(recorded_hash=stale_hash)

    with patch("mailpilot.database.psycopg.connect", return_value=mock_conn):
        initialize_database("postgresql://localhost/test")

    warns = _warn_spans(capfire)
    assert len(warns) == 1
    current_hash = _compute_schema_hash(SCHEMA_PATH.read_text())
    attrs = warns[0]["attributes"]
    assert attrs["recorded_hash"] == stale_hash
    assert attrs["current_hash"] == current_hash
    assert attrs["recorded_version"] == "0.0.0"
    assert attrs["current_version"] == _MAILPILOT_VERSION

    err = capsys.readouterr().err
    assert "event=schema.drift" in err
    assert f"recorded_hash={stale_hash}" in err
    assert f"current_hash={current_hash}" in err


def test_initialize_version_only_change_is_silent(
    capfire: CaptureLogfire, capsys: pytest.CaptureFixture[str]
):
    """Same hash + different version → ⊥ warn, ⊥ operator event."""
    current_hash = _compute_schema_hash(SCHEMA_PATH.read_text())
    mock_conn = _existing_db_mock(recorded_hash=current_hash, recorded_version="0.0.0")

    with patch("mailpilot.database.psycopg.connect", return_value=mock_conn):
        initialize_database("postgresql://localhost/test")

    assert _warn_spans(capfire) == []
    assert "schema.drift" not in capsys.readouterr().err


def test_initialize_legacy_db_drift_via_missing_table(
    capfire: CaptureLogfire, capsys: pytest.CaptureFixture[str]
):
    """schema_metadata table missing on otherwise-initialized DB → drift."""
    mock_conn = _existing_db_mock(recorded_hash=None)

    with patch("mailpilot.database.psycopg.connect", return_value=mock_conn):
        initialize_database("postgresql://localhost/test")

    warns = _warn_spans(capfire)
    assert len(warns) == 1
    attrs = warns[0]["attributes"]
    # OTel attributes cannot carry None; logfire serializes as the string "null".
    assert attrs["recorded_hash"] == "null"
    assert attrs["recorded_version"] == "null"
    assert attrs["current_hash"] == _compute_schema_hash(SCHEMA_PATH.read_text())

    err = capsys.readouterr().err
    assert "event=schema.drift" in err
    assert "recorded_version=None" in err
    assert "recorded_hash=None" in err


# -- get_status_counts schema_metadata block (§V.58 envelope) -----------------


def test_status_envelope_no_drift_omits_current_fields(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    counts = get_status_counts(database_connection)
    block = counts["schema_metadata"]
    assert isinstance(block, dict)
    assert block["drift"] is False
    assert block["mailpilot_version"] == _MAILPILOT_VERSION
    current_hash = _compute_schema_hash(SCHEMA_PATH.read_text())
    assert block["schema_hash"] == current_hash
    assert isinstance(block["applied_at"], str)
    assert "current_version" not in block
    assert "current_hash" not in block


def test_status_envelope_drift_includes_current_fields(
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
):
    stale = SchemaMetadata(
        mailpilot_version="0.0.0",
        schema_hash="deadbeef" * 8,
        applied_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    monkeypatch.setattr(db_mod, "_read_schema_metadata", lambda _conn: stale)

    counts = get_status_counts(database_connection)
    block = counts["schema_metadata"]
    assert isinstance(block, dict)
    assert block["drift"] is True
    assert block["mailpilot_version"] == "0.0.0"
    assert block["schema_hash"] == "deadbeef" * 8
    assert block["current_version"] == _MAILPILOT_VERSION
    current_hash = _compute_schema_hash(SCHEMA_PATH.read_text())
    assert block["current_hash"] == current_hash


def test_status_envelope_drift_row_missing(
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(db_mod, "_read_schema_metadata", lambda _conn: None)

    counts = get_status_counts(database_connection)
    block = counts["schema_metadata"]
    assert isinstance(block, dict)
    assert block["drift"] is True
    assert block["mailpilot_version"] is None
    assert block["schema_hash"] is None
    assert block["applied_at"] is None
    assert block["current_version"] == _MAILPILOT_VERSION
    assert block["current_hash"] == _compute_schema_hash(SCHEMA_PATH.read_text())
