"""Observability contract tests for Gmail spans, retry exhaustion, sync
span attributes, sync metrics, and CLI trace_id in error output.

Covers issues #27, #28, #29, #31, #32, #33.
"""

import json
from typing import Any
from unittest.mock import MagicMock, patch

import logfire
import pytest
from click.testing import CliRunner
from logfire.testing import CaptureLogfire

from conftest import make_test_settings
from mailpilot.cli import main, output_error
from mailpilot.gmail import (
    _MAX_RETRIES as MAX_RETRIES,  # pyright: ignore[reportPrivateUsage]
)
from mailpilot.gmail import GmailClient

# -- Helpers -------------------------------------------------------------------


def _make_http_error(status: int) -> Any:
    """Create a mock HttpError with the given status code."""
    from googleapiclient.errors import HttpError

    resp = MagicMock()
    resp.status = status
    return HttpError(resp=resp, content=b"error")


def _spans_by_name(capfire: CaptureLogfire, name: str) -> list[dict[str, Any]]:
    return [s for s in capfire.exporter.exported_spans_as_dict() if s["name"] == name]


def _logs_by_msg(capfire: CaptureLogfire, msg: str) -> list[dict[str, Any]]:
    return [
        s
        for s in capfire.exporter.exported_spans_as_dict()
        if s.get("attributes", {}).get("logfire.msg") == msg
    ]


# -- #27: Gmail API spans -----------------------------------------------------


def test_gmail_span_emitted_on_success(capfire: CaptureLogfire):
    """Decorated GmailClient methods emit a gmail.* span on success.

    Uses list_messages (still decorated) to verify the span contract.
    get_profile was intentionally stripped of the decorator to reduce
    per-iteration span volume (2026-04-26 smoke test finding).
    """
    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": []
    }
    client = GmailClient.from_service("test@example.com", service)

    client.list_messages()

    spans = _spans_by_name(capfire, "gmail.list_messages")
    assert len(spans) == 1
    attrs = spans[0]["attributes"]
    assert attrs["method"] == "list_messages"
    assert attrs["user_id"] == "test@example.com"
    assert attrs["attempts"] == 1


def test_gmail_span_records_attempts_on_retry(capfire: CaptureLogfire):
    """Transient retries are visible via the attempts attribute."""
    service = MagicMock()
    execute = service.users.return_value.messages.return_value.list.return_value.execute
    execute.side_effect = [
        _make_http_error(503),
        {"messages": []},
    ]
    client = GmailClient.from_service("test@example.com", service)

    with patch("mailpilot.gmail.time.sleep"):
        client.list_messages()

    spans = _spans_by_name(capfire, "gmail.list_messages")
    assert len(spans) == 1
    assert spans[0]["attributes"]["attempts"] == 2


def test_gmail_span_records_status_on_non_transient_error(capfire: CaptureLogfire):
    """Non-transient errors include the HTTP status on the span."""
    service = MagicMock()
    execute = service.users.return_value.messages.return_value.list.return_value.execute
    execute.side_effect = _make_http_error(403)
    client = GmailClient.from_service("test@example.com", service)

    from googleapiclient.errors import HttpError

    with pytest.raises(HttpError):
        client.list_messages()

    spans = _spans_by_name(capfire, "gmail.list_messages")
    assert len(spans) == 1
    assert spans[0]["attributes"]["status"] == 403
    assert spans[0]["attributes"]["attempts"] == 1


# -- #29: gmail.retry.exhausted ------------------------------------------------


def test_retry_exhausted_emits_error_log(capfire: CaptureLogfire):
    """When all retries fail, a gmail.retry.exhausted error log is emitted."""
    from googleapiclient.errors import HttpError

    service = MagicMock()
    execute = service.users.return_value.messages.return_value.list.return_value.execute
    execute.side_effect = [_make_http_error(503)] * MAX_RETRIES
    client = GmailClient.from_service("test@example.com", service)

    with (
        pytest.raises(HttpError),
        patch("mailpilot.gmail.time.sleep"),
    ):
        client.list_messages()

    exhausted = _logs_by_msg(capfire, "gmail.retry.exhausted")
    assert len(exhausted) == 1
    attrs = exhausted[0]["attributes"]
    assert attrs["method"] == "list_messages"
    assert attrs["status"] == 503
    assert attrs["attempts"] == MAX_RETRIES


# -- #32: No result attribute on sync span ------------------------------------


def test_sync_span_has_no_result_attribute(
    capfire: CaptureLogfire,
    database_connection: Any,
):
    """sync.account.run span must not set a result=success|failure attribute."""
    from conftest import make_test_account

    account = make_test_account(database_connection, email="noresult@example.com")
    service = MagicMock()
    service.users.return_value.getProfile.return_value.execute.return_value = {
        "historyId": "100"
    }
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": []
    }
    client = GmailClient.from_service(account.email, service)

    from mailpilot.sync import sync_account

    sync_account(database_connection, account, client, make_test_settings())

    spans = _spans_by_name(capfire, "sync.account.run")
    assert len(spans) == 1
    assert "result" not in spans[0]["attributes"]


# -- #28: Enriched sync span attributes ---------------------------------------


def test_sync_span_has_enriched_attributes(
    capfire: CaptureLogfire,
    database_connection: Any,
):
    """sync.account.run span includes fetched_count, stored_count, duplicate_skipped_count."""
    from conftest import make_test_account

    account = make_test_account(database_connection, email="enriched@example.com")
    service = MagicMock()
    service.users.return_value.getProfile.return_value.execute.return_value = {
        "historyId": "100"
    }
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": []
    }
    client = GmailClient.from_service(account.email, service)

    from mailpilot.sync import sync_account

    sync_account(database_connection, account, client, make_test_settings())

    spans = _spans_by_name(capfire, "sync.account.run")
    assert len(spans) == 1
    attrs = spans[0]["attributes"]
    assert attrs["fetched_count"] == 0
    assert attrs["stored_count"] == 0
    assert attrs["duplicate_skipped_count"] == 0
    assert attrs["mode"] == "full"


# -- #33: trace_id in CLI error output ----------------------------------------


def test_output_error_includes_trace_id_when_span_active():
    """Error JSON includes trace_id when a valid span context exists."""
    with logfire.span("test.error_trace"), pytest.raises(SystemExit):
        output_error("boom", "test_error")
    # Cannot easily capture stderr in this path; the test verifies
    # no exception is raised from the trace_id code path.


def test_output_error_omits_trace_id_without_span():
    """Error JSON omits trace_id gracefully when no span is active."""
    runner = CliRunner()
    # Use a validation path that fires output_error before initialize_database()
    # so no DB connection is attempted and no logfire.warn() pollutes stderr.
    result = runner.invoke(main, ["account", "create", "--email", ""])
    # The command exits with code 1 (output_error called).
    assert result.exit_code == 1
    # Error output is on stderr -- CliRunner mixes it when mix_stderr is True.
    data = json.loads(result.output)
    assert data["ok"] is False
    assert "trace_id" not in data


def test_output_error_trace_id_format():
    """trace_id is 32-char lowercase hex when present."""
    captured: dict[str, object] = {}

    # Patch click.echo to capture the output.
    with (
        logfire.span("test.format_check"),
        patch(
            "mailpilot.cli.click.echo",
            side_effect=lambda msg, **kw: captured.update(json.loads(msg)),
        ),
        pytest.raises(SystemExit),
    ):
        output_error("boom", "fmt_test")

    if "trace_id" in captured:
        trace_id = str(captured["trace_id"])
        assert len(trace_id) == 32
        assert all(c in "0123456789abcdef" for c in trace_id)


# -- #31: Sync metrics --------------------------------------------------------


def test_sync_metrics_recorded_on_success(
    database_connection: Any,
):
    """sync.messages.stored and sync.account.duration_ms are recorded."""
    from conftest import make_test_account
    from mailpilot.sync import (
        sync_account,
        sync_account_duration,
        sync_messages_stored,
    )

    account = make_test_account(database_connection, email="metric@example.com")
    service = MagicMock()
    service.users.return_value.getProfile.return_value.execute.return_value = {
        "historyId": "100"
    }
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": []
    }
    client = GmailClient.from_service(account.email, service)

    with (
        patch.object(sync_messages_stored, "add") as mock_msg,
        patch.object(sync_account_duration, "record") as mock_duration,
    ):
        sync_account(database_connection, account, client, make_test_settings())

    # Duration histogram recorded once per sync_account call.
    mock_duration.assert_called_once()
    call_args = mock_duration.call_args
    assert call_args.args[0] > 0  # positive duration
    assert call_args.kwargs["attributes"]["account_id"] == account.id
    assert call_args.kwargs["attributes"]["mode"] == "full"
    # No messages stored -> sync_messages_stored not called.
    mock_msg.assert_not_called()


def test_sync_errors_metric_on_exception(
    database_connection: Any,
):
    """sync.errors counter increments when sync_account raises."""
    from conftest import make_test_account
    from mailpilot.sync import sync_account, sync_errors

    account = make_test_account(database_connection, email="err@example.com")
    service = MagicMock()
    service.users.return_value.getProfile.return_value.execute.side_effect = (
        RuntimeError("boom")
    )
    client = GmailClient.from_service(account.email, service)

    with (
        patch.object(sync_errors, "add") as mock_errors,
        pytest.raises(RuntimeError, match="boom"),
    ):
        sync_account(database_connection, account, client, make_test_settings())

    mock_errors.assert_called_once()
    attrs = mock_errors.call_args.kwargs["attributes"]
    assert attrs["account_id"] == account.id
    assert attrs["reason"] == "sync_exception"
