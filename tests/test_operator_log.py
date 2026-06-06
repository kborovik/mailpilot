"""Tests for the operator_log module."""

from __future__ import annotations

import json
import re
from typing import Any

import psycopg
import pytest

from mailpilot.operator_log import cli_mutation, operator_event


def test_emits_single_line_with_event_and_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    operator_event("loop.tick", iteration=3, wakeup="event", full_sweep=True)
    captured = capsys.readouterr()
    assert captured.out == ""
    line = captured.err.rstrip("\n")
    assert "\n" not in line
    assert re.match(r"^\d{2}:\d{2}:\d{2} event=loop\.tick ", line)
    assert "iteration=3" in line
    assert "wakeup=event" in line
    assert "full_sweep=True" in line


def test_quotes_values_with_spaces(capsys: pytest.CaptureFixture[str]) -> None:
    operator_event("error", source="sync_account", message="Gmail timeout 504")
    line = capsys.readouterr().err.rstrip("\n")
    assert 'message="Gmail timeout 504"' in line


def test_no_fields_emits_just_event(capsys: pytest.CaptureFixture[str]) -> None:
    operator_event("loop.stop")
    line = capsys.readouterr().err.rstrip("\n")
    assert re.match(r"^\d{2}:\d{2}:\d{2} event=loop\.stop$", line)


def test_flushes_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    flushed = {"called": False}

    class FakeStream:
        def write(self, text: str) -> int:
            return len(text)

        def flush(self) -> None:
            flushed["called"] = True

    monkeypatch.setattr("sys.stderr", FakeStream())
    operator_event("loop.tick", iteration=1)
    assert flushed["called"] is True


def test_does_not_write_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    operator_event("loop.tick", iteration=1)
    operator_event("error", source="x", message="boom")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "event=loop.tick" in captured.err
    assert "event=error" in captured.err


def test_escapes_double_quotes_in_quoted_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    operator_event("error", source="x", message='He said "hi"')
    line = capsys.readouterr().err.rstrip("\n")
    assert 'message="He said \\"hi\\""' in line


def test_collapses_newlines_to_spaces(
    capsys: pytest.CaptureFixture[str],
) -> None:
    operator_event("error", source="x", message="line one\nline two\rline three")
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert '"line one line two line three"' in err


def test_accepts_non_string_field_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload: dict[str, Any] = {"count": 42, "ratio": 0.5, "flag": False}
    operator_event("metric", **payload)
    line = capsys.readouterr().err.rstrip("\n")
    assert "count=42" in line
    assert "ratio=0.5" in line
    assert "flag=False" in line


# -- cli_mutation envelope translation per §V.54(+) ---------------------------


def _parse_error_envelope(stderr: str) -> dict[str, Any]:
    """Extract the trailing JSON error envelope from stderr."""
    brace_start = stderr.index("{")
    return json.loads(stderr[brace_start:])


@pytest.mark.parametrize(
    ("exception_class", "expected_code"),
    [
        (psycopg.errors.UniqueViolation, "duplicate_key"),
        (psycopg.errors.ForeignKeyViolation, "foreign_key_violation"),
        (psycopg.errors.NotNullViolation, "not_null_violation"),
        (psycopg.errors.CheckViolation, "check_violation"),
    ],
)
def test_cli_mutation_translates_psycopg_constraint_to_envelope(
    capsys: pytest.CaptureFixture[str],
    exception_class: type[psycopg.Error],
    expected_code: str,
) -> None:
    """Synthesized psycopg constraint violation → structured envelope, no traceback."""
    message = f"synthetic {exception_class.__name__} for test"
    with (
        pytest.raises(SystemExit) as excinfo,
        cli_mutation("account", "create", email="x@example.com"),
    ):
        raise exception_class(message)

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    # §V.54(+) recurrence-class assertion: stderr MUST NOT carry raw psycopg traceback.
    # (stdout cleanliness is a production-CLI runtime invariant; test-env logfire
    # console exporter writes span dumps to stdout, irrelevant to wrapper contract.)
    assert "Traceback (most recent call last):" not in captured.err
    assert "event=error source=account.create" in captured.err
    assert message in captured.err
    envelope = _parse_error_envelope(captured.err)
    assert envelope["error"] == expected_code
    assert envelope["message"] == message
    assert envelope["ok"] is False


def test_cli_mutation_translates_generic_psycopg_error_to_database_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """psycopg.Error subclass outside the constraint map → database_error fallback."""
    message = "synthetic generic OperationalError"
    with (
        pytest.raises(SystemExit) as excinfo,
        cli_mutation("company", "update", entity_id="cmp_123"),
    ):
        raise psycopg.OperationalError(message)

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "Traceback (most recent call last):" not in captured.err
    assert "event=error source=company.update" in captured.err
    envelope = _parse_error_envelope(captured.err)
    assert envelope["error"] == "database_error"
    assert envelope["message"] == message
    assert envelope["ok"] is False


def test_cli_mutation_reraises_non_psycopg_exception(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-psycopg Exception → still re-raises per existing semantics."""

    class SentinelError(RuntimeError):
        pass

    with (
        pytest.raises(SentinelError),
        cli_mutation("contact", "disable", entity_id="ct_x"),
    ):
        raise SentinelError("not a db error")

    captured = capsys.readouterr()
    assert "event=error source=contact.disable" in captured.err
    assert "not a db error" in captured.err
    # No JSON envelope translation for non-psycopg paths.
    assert '"error":' not in captured.err
    assert '"ok": false' not in captured.err


def test_cli_mutation_observability_pair_fires_before_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Operator event + logfire.exception fire before envelope translation."""
    with (
        pytest.raises(SystemExit),
        cli_mutation("workflow", "create", account_id="acc_x", name="wf"),
    ):
        raise psycopg.errors.UniqueViolation("duplicate key value")

    stderr = capsys.readouterr().err
    event_idx = stderr.index("event=error source=workflow.create")
    envelope_idx = stderr.index('"error":')
    assert event_idx < envelope_idx
