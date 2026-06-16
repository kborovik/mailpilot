"""Tests for schema_metadata write, drift detection, and status envelope.

Covers §V.18 (drift warn + status envelope) and §V.19 (normalized hash).
"""

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from logfire.testing import CaptureLogfire

from conftest import make_test_settings
from mailpilot import database as db_mod
from mailpilot.database import (
    _MAILPILOT_VERSION,  # pyright: ignore[reportPrivateUsage]
    SCHEMA_PATH,
    _compute_schema_hash,  # pyright: ignore[reportPrivateUsage]
    determine_schema_verdict,
    get_status_payload,
    initialize_database,
    provision_database,
)
from mailpilot.models import SchemaMetadata, SchemaStatus

# -- _compute_schema_hash (§V.19) ---------------------------------------------


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


# -- initialize_database drift branches (§V.18) -------------------------------


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


# -- determine_schema_verdict three-state logic (§V.109) ----------------------


def _patch_verdict_inputs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    recorded_hash: str | None,
    applied: set[int],
    discovered: list[int],
    current_hash: str = "H",
) -> None:
    """Stub every determine_schema_verdict dependency for DB-free logic tests."""
    monkeypatch.setattr(db_mod, "_compute_schema_hash", lambda _sql: current_hash)
    recorded = (
        None
        if recorded_hash is None
        else SchemaMetadata(
            mailpilot_version="x",
            schema_hash=recorded_hash,
            applied_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
    )
    monkeypatch.setattr(db_mod, "_read_schema_metadata", lambda _conn: recorded)
    monkeypatch.setattr(
        db_mod, "_read_applied_migration_versions", lambda _conn: set(applied)
    )
    monkeypatch.setattr(
        db_mod,
        "_discover_migrations",
        lambda: [(v, f"m{v}", Path(f"/{v}.sql")) for v in discovered],
    )


def test_verdict_current_when_hash_matches_and_ledger_complete(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_verdict_inputs(monkeypatch, recorded_hash="H", applied={1}, discovered=[1])
    status = determine_schema_verdict(MagicMock())
    assert status.verdict == "current"
    assert status.applied == 1
    assert status.pending == 0
    assert status.recorded_hash == "H"
    assert status.current_hash == "H"


def test_verdict_current_when_hash_matches_even_if_ledger_behind(
    monkeypatch: pytest.MonkeyPatch,
):
    """Hash matches canonical schema.sql → current; a ledger gap is an
    un-recorded baseline, not a real pending change (§V.109)."""
    _patch_verdict_inputs(monkeypatch, recorded_hash="H", applied=set(), discovered=[1])
    status = determine_schema_verdict(MagicMock())
    assert status.verdict == "current"
    assert status.pending == 1  # reported, but does not gate


def test_verdict_pending_when_hash_diverges_and_migration_unapplied(
    monkeypatch: pytest.MonkeyPatch,
):
    """Ledger behind the shipped migrations + hash diverged → pending."""
    _patch_verdict_inputs(
        monkeypatch, recorded_hash="OLD", applied={1}, discovered=[1, 2]
    )
    status = determine_schema_verdict(MagicMock())
    assert status.verdict == "pending"
    assert status.pending == 1


def test_verdict_drift_when_hash_diverges_with_no_migration_path(
    monkeypatch: pytest.MonkeyPatch,
):
    """Hash mismatch | manual edit, every migration applied → drift."""
    _patch_verdict_inputs(monkeypatch, recorded_hash="OLD", applied={1}, discovered=[1])
    status = determine_schema_verdict(MagicMock())
    assert status.verdict == "drift"
    assert status.pending == 0


def test_verdict_drift_when_metadata_missing(monkeypatch: pytest.MonkeyPatch):
    """Breaks the row/table-missing → None collapse: absent metadata = drift."""
    _patch_verdict_inputs(monkeypatch, recorded_hash=None, applied={1}, discovered=[1])
    status = determine_schema_verdict(MagicMock())
    assert status.verdict == "drift"
    assert status.recorded_hash is None


# -- get_status_payload schema block (§V.11 three-state verdict) --------------


def _patch_status(monkeypatch: pytest.MonkeyPatch, status: SchemaStatus) -> None:
    monkeypatch.setattr(db_mod, "determine_schema_verdict", lambda _conn: status)


def test_status_schema_block_shape_and_values(
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
):
    """`schema` block exposes verdict + recorded/current hash + counts only."""
    _patch_status(
        monkeypatch,
        SchemaStatus(
            verdict="current",
            recorded_hash="a" * 64,
            current_hash="a" * 64,
            applied=1,
            pending=0,
        ),
    )
    payload = get_status_payload(database_connection, make_test_settings())
    block = payload["schema"]
    assert isinstance(block, dict)
    assert set(block.keys()) == {
        "verdict",
        "recorded_hash",
        "current_hash",
        "applied",
        "pending",
    }
    assert block["verdict"] == "current"
    assert block["recorded_hash"] == "a" * 64
    assert block["current_hash"] == "a" * 64
    assert block["applied"] == 1
    assert block["pending"] == 0
    assert payload["version"] == _MAILPILOT_VERSION


def test_status_schema_block_reports_pending(
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_status(
        monkeypatch,
        SchemaStatus(
            verdict="pending",
            recorded_hash="a" * 64,
            current_hash="b" * 64,
            applied=1,
            pending=2,
        ),
    )
    block = get_status_payload(database_connection, make_test_settings())["schema"]
    assert isinstance(block, dict)
    assert block["verdict"] == "pending"
    assert block["pending"] == 2


def test_status_schema_block_reports_drift(
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_status(
        monkeypatch,
        SchemaStatus(
            verdict="drift",
            recorded_hash="deadbeef" * 8,
            current_hash="b" * 64,
            applied=1,
            pending=0,
        ),
    )
    block = get_status_payload(database_connection, make_test_settings())["schema"]
    assert isinstance(block, dict)
    assert block["verdict"] == "drift"
    assert block["recorded_hash"] == "deadbeef" * 8


def test_status_schema_block_real_db_is_current(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """A provisioned test DB reports verdict=current with the canonical hash.

    Read-only diagnosis tolerates whatever the verdict is; on a freshly
    provisioned DB the recorded hash matches ``schema.sql`` so verdict=current.
    """
    block = get_status_payload(database_connection, make_test_settings())["schema"]
    assert isinstance(block, dict)
    assert block["verdict"] == "current"
    assert block["current_hash"] == _compute_schema_hash(SCHEMA_PATH.read_text())


# -- initialize_database baseline-stamp on provision (§V.108/§V.109) ----------


def test_initialize_baseline_stamps_migration_ledger_on_fresh_db():
    """Fresh provision records every shipped migration as applied (§V.108)."""
    probe_cursor = MagicMock()
    probe_cursor.fetchone.return_value = {"oid": None}
    mock_conn = MagicMock()
    mock_conn.execute.return_value = probe_cursor

    with patch("mailpilot.database.psycopg.connect", return_value=mock_conn):
        initialize_database("postgresql://localhost/test")

    ledger_inserts = [
        call
        for call in mock_conn.execute.call_args_list
        if "INSERT INTO schema_migrations" in str(call.args[0])
    ]
    assert ledger_inserts, "fresh provision must baseline-stamp the ledger"
    stamped_versions = {call.args[1][0] for call in ledger_inserts}
    assert 1 in stamped_versions  # frozen 001_initial_schema baseline


# -- write-path gate: dead-stop on pending/drift (§V.109/§V.18) ---------------


def _populated_db_mock() -> MagicMock:
    """Connection mock whose `account` probe reports an initialized DB."""
    probe_cursor = MagicMock()
    probe_cursor.fetchone.return_value = {"oid": "account"}
    mock_conn = MagicMock()

    def execute_side_effect(query: Any, *_params: Any) -> MagicMock:
        if "to_regclass" in str(query):
            return probe_cursor
        return MagicMock()

    mock_conn.execute.side_effect = execute_side_effect
    return mock_conn


def test_initialize_enforce_dead_stops_on_drift(
    capfire: CaptureLogfire, capsys: pytest.CaptureFixture[str]
):
    """require_current_schema + drift → SystemExit(1) + schema_drift envelope."""
    mock_conn = _populated_db_mock()
    drift = SchemaStatus(
        verdict="drift",
        recorded_hash="dead" * 16,
        current_hash="beef" * 16,
        applied=1,
        pending=0,
    )
    with (
        patch("mailpilot.database.psycopg.connect", return_value=mock_conn),
        patch("mailpilot.database.determine_schema_verdict", return_value=drift),
        pytest.raises(SystemExit) as exc,
    ):
        initialize_database("postgresql://localhost/test", require_current_schema=True)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert '"error": "schema_drift"' in err
    assert '"ok": false' in err
    mock_conn.close.assert_called_once()


def test_initialize_enforce_dead_stops_on_pending(
    capfire: CaptureLogfire, capsys: pytest.CaptureFixture[str]
):
    """require_current_schema + pending → SystemExit(1) + distinct code."""
    mock_conn = _populated_db_mock()
    pending = SchemaStatus(
        verdict="pending",
        recorded_hash="a" * 64,
        current_hash="b" * 64,
        applied=1,
        pending=3,
    )
    with (
        patch("mailpilot.database.psycopg.connect", return_value=mock_conn),
        patch("mailpilot.database.determine_schema_verdict", return_value=pending),
        pytest.raises(SystemExit) as exc,
    ):
        initialize_database("postgresql://localhost/test", require_current_schema=True)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert '"error": "schema_migration_pending"' in err


def test_initialize_enforce_passes_when_current():
    """require_current_schema + current → returns the connection, no exit."""
    mock_conn = _populated_db_mock()
    current = SchemaStatus(
        verdict="current",
        recorded_hash="a" * 64,
        current_hash="a" * 64,
        applied=1,
        pending=0,
    )
    with (
        patch("mailpilot.database.psycopg.connect", return_value=mock_conn),
        patch("mailpilot.database.determine_schema_verdict", return_value=current),
    ):
        connection = initialize_database(
            "postgresql://localhost/test", require_current_schema=True
        )
    assert connection is mock_conn


def test_initialize_tolerate_does_not_dead_stop_on_drift(
    capfire: CaptureLogfire,
):
    """Default read path tolerates drift — reports, never dead-stops (§V.18)."""
    mock_conn = _existing_db_mock(recorded_hash="deadbeef" * 8)
    with patch("mailpilot.database.psycopg.connect", return_value=mock_conn):
        connection = initialize_database("postgresql://localhost/test")
    assert connection is mock_conn  # no SystemExit on the read path


# -- provision_database (`db init` path, §V.110) ------------------------------


def _provision_conn_mock(*, account_exists: bool) -> MagicMock:
    """Connection mock whose `account` probe reports empty / populated DB."""
    probe_cursor = MagicMock()
    probe_cursor.fetchone.return_value = {"oid": "account" if account_exists else None}
    connection = MagicMock()

    def execute_side_effect(query: Any, *_params: Any) -> MagicMock:
        if "to_regclass" in str(query):
            return probe_cursor
        return MagicMock()

    connection.execute.side_effect = execute_side_effect
    return connection


def test_provision_database_provisions_empty_db():
    """Account absent → schema applied, report carries provisioned=True (§V.110)."""
    conn = _provision_conn_mock(account_exists=False)
    status = SchemaStatus(
        verdict="current",
        recorded_hash="a" * 64,
        current_hash="a" * 64,
        applied=1,
        pending=0,
    )
    with (
        patch("mailpilot.database.psycopg.connect", return_value=conn),
        patch("mailpilot.database.determine_schema_verdict", return_value=status),
    ):
        report = provision_database("postgresql://localhost/test")

    assert report["provisioned"] is True
    assert report["verdict"] == "current"
    executed = [str(call.args[0]) for call in conn.execute.call_args_list]
    assert any("CREATE TABLE" in q for q in executed)
    conn.close.assert_called_once()


def test_provision_database_never_mutates_populated_db():
    """Account present → never re-applies schema (no --force); reports verdict."""
    conn = _provision_conn_mock(account_exists=True)
    status = SchemaStatus(
        verdict="pending",
        recorded_hash="a" * 64,
        current_hash="b" * 64,
        applied=1,
        pending=2,
    )
    with (
        patch("mailpilot.database.psycopg.connect", return_value=conn),
        patch("mailpilot.database.determine_schema_verdict", return_value=status),
    ):
        report = provision_database("postgresql://localhost/test")

    assert report["provisioned"] is False
    assert report["verdict"] == "pending"
    assert report["pending"] == 2
    executed = [str(call.args[0]) for call in conn.execute.call_args_list]
    assert not any("CREATE TABLE" in q for q in executed)
    conn.close.assert_called_once()
