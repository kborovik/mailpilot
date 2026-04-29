"""Tests for sync status database operations and the per-account sync pipeline."""

import base64
import os
import signal
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from googleapiclient.errors import HttpError
from logfire.testing import CaptureLogfire

from conftest import (
    make_test_account,
    make_test_contact,
    make_test_settings,
    make_test_workflow,
)
from mailpilot.database import (
    create_email,
    delete_sync_status,
    get_contact_by_email,
    get_email,
    get_email_by_gmail_message_id,
    get_sync_status,
    list_emails,
    update_account,
    update_sync_heartbeat,
    upsert_sync_status,
)
from mailpilot.gmail import GmailClient
from mailpilot.sync import is_pid_alive, send_email, start_sync_loop, sync_account


def test_upsert_and_get_sync_status(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    pid = os.getpid()
    status = upsert_sync_status(database_connection, pid)
    assert status.pid == pid
    assert status.id == "singleton"

    fetched = get_sync_status(database_connection)
    assert fetched is not None
    assert fetched.pid == pid


def test_upsert_overwrites_existing(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    upsert_sync_status(database_connection, 1111)
    updated = upsert_sync_status(database_connection, 2222)
    assert updated.pid == 2222

    fetched = get_sync_status(database_connection)
    assert fetched is not None
    assert fetched.pid == 2222


def test_delete_sync_status(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    upsert_sync_status(database_connection, os.getpid())
    delete_sync_status(database_connection)
    assert get_sync_status(database_connection) is None


def test_update_heartbeat(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    status = upsert_sync_status(database_connection, os.getpid())
    original_heartbeat = status.heartbeat_at

    update_sync_heartbeat(database_connection)

    fetched = get_sync_status(database_connection)
    assert fetched is not None
    assert fetched.heartbeat_at >= original_heartbeat


def test_get_sync_status_when_empty(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    assert get_sync_status(database_connection) is None


def testis_pid_alive_current_process():
    assert is_pid_alive(os.getpid()) is True


def testis_pid_alive_dead_process():
    # PID 99999999 is almost certainly not running.
    assert is_pid_alive(99999999) is False


def test_heartbeat_staleness(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Verify heartbeat_at can be compared for staleness detection."""
    status = upsert_sync_status(database_connection, os.getpid())
    stale_threshold = datetime.now(tz=UTC) - timedelta(minutes=2)
    assert status.heartbeat_at > stale_threshold


# -- start_sync_loop -----------------------------------------------------------


def test_start_sync_loop_registers_pid_and_shuts_down(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Loop registers PID, runs one iteration, then shuts down cleanly."""
    settings = make_test_settings()

    with (
        patch("mailpilot.sync.threading.Event") as mock_event_cls,
        patch("mailpilot.sync._start_task_listener"),
        patch("mailpilot.sync._start_pubsub_logging_errors", return_value=None),
        patch("mailpilot.sync._run_periodic_iteration"),
        patch("mailpilot.sync.signal.signal"),
    ):
        mock_shutdown = MagicMock()
        mock_shutdown.is_set.side_effect = [False, True]
        mock_wakeup = MagicMock()
        mock_wakeup.wait.return_value = False  # timer fired, not event-driven
        mock_event_cls.side_effect = [mock_shutdown, mock_wakeup]

        start_sync_loop(database_connection, settings)

    # Verify PID was registered then cleaned up
    assert get_sync_status(database_connection) is None


def test_start_sync_loop_signal_handler_escalates_to_default(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """First signal sets shutdown_event; subsequent signals get SIG_DFL.

    Without escalation, a process blocked in C-level code (e.g. gRPC
    inside Pub/Sub setup) cannot be killed with Ctrl+C because the
    handler only sets a Python-level event that no one is reading.
    Restoring SIG_DFL after the first signal lets a second Ctrl+C kill
    the process immediately.
    """
    settings = make_test_settings()

    handlers: dict[int, Any] = {}

    def fake_signal(signum: int, handler: Any) -> Any:
        previous = handlers.get(signum)
        handlers[signum] = handler
        return previous

    captured_handler: list[Any] = []

    def capture_then_run(*_args: Any, **_kwargs: Any) -> None:
        # Invoke the registered SIGINT handler once, as if the user
        # pressed Ctrl+C while we were inside this iteration.
        captured_handler.append(handlers[signal.SIGINT])
        handlers[signal.SIGINT](signal.SIGINT, None)

    with (
        patch("mailpilot.sync.threading.Event") as mock_event_cls,
        patch("mailpilot.sync._start_task_listener"),
        patch("mailpilot.sync._start_pubsub_logging_errors", return_value=None),
        patch(
            "mailpilot.sync._run_periodic_iteration",
            side_effect=capture_then_run,
        ),
        patch("mailpilot.sync.signal.signal", side_effect=fake_signal),
    ):
        mock_shutdown = MagicMock()
        mock_shutdown.is_set.side_effect = [False, False, True]
        mock_wakeup = MagicMock()
        mock_wakeup.wait.return_value = False
        mock_event_cls.side_effect = [mock_shutdown, mock_wakeup]

        start_sync_loop(database_connection, settings)

    # The handler ran (proves it was registered and invoked).
    assert captured_handler, "shutdown handler was never invoked"
    # After the first signal, SIGINT and SIGTERM were restored to default.
    assert handlers[signal.SIGINT] is signal.SIG_DFL
    assert handlers[signal.SIGTERM] is signal.SIG_DFL


def test_start_sync_loop_calls_pubsub_when_configured(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """When google credentials are set, Pub/Sub setup is attempted."""
    settings = make_test_settings(
        google_application_credentials="/tmp/creds.json",
    )

    with (
        patch("mailpilot.sync.threading.Event") as mock_event_cls,
        patch("mailpilot.sync._start_task_listener"),
        patch(
            "mailpilot.sync._start_pubsub_logging_errors",
            return_value=None,
        ) as mock_pubsub,
        patch("mailpilot.sync._run_periodic_iteration"),
        patch("mailpilot.sync.signal.signal"),
    ):
        mock_shutdown = MagicMock()
        mock_shutdown.is_set.side_effect = [False, True]
        mock_wakeup = MagicMock()
        mock_wakeup.wait.return_value = True
        mock_event_cls.side_effect = [mock_shutdown, mock_wakeup]

        start_sync_loop(database_connection, settings)

    mock_pubsub.assert_called_once()


def test_start_sync_loop_skips_pubsub_when_no_credentials(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """When google credentials are empty, Pub/Sub setup is skipped."""
    settings = make_test_settings(google_application_credentials="")

    with (
        patch("mailpilot.sync.threading.Event") as mock_event_cls,
        patch("mailpilot.sync._start_task_listener"),
        patch(
            "mailpilot.sync._start_pubsub_logging_errors",
            return_value=None,
        ) as mock_pubsub,
        patch("mailpilot.sync._run_periodic_iteration"),
        patch("mailpilot.sync.signal.signal"),
    ):
        mock_shutdown = MagicMock()
        mock_shutdown.is_set.side_effect = [False, True]
        mock_wakeup = MagicMock()
        mock_wakeup.wait.return_value = True
        mock_event_cls.side_effect = [mock_shutdown, mock_wakeup]

        start_sync_loop(database_connection, settings)

    mock_pubsub.assert_not_called()


def test_start_sync_loop_wires_wakeup_event_to_pubsub_and_listener(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Pub/Sub setup and PG listener share one wakeup_event with the main loop.

    Without this wiring, notifications would never wake the main loop --
    real-time delivery via Pub/Sub and instant task execution via PG
    LISTEN/NOTIFY would degenerate to plain run_interval polling.
    """
    settings = make_test_settings(google_application_credentials="/tmp/creds.json")

    with (
        patch("mailpilot.sync.threading.Event") as mock_event_cls,
        patch("mailpilot.sync._start_task_listener") as mock_listener,
        patch(
            "mailpilot.sync._start_pubsub_logging_errors",
            return_value=None,
        ) as mock_pubsub,
        patch("mailpilot.sync._run_periodic_iteration"),
        patch("mailpilot.sync.signal.signal"),
    ):
        mock_shutdown = MagicMock()
        # while-check False, post-wait check False (run iteration), while-check True (exit)
        mock_shutdown.is_set.side_effect = [False, False, True]
        mock_wakeup = MagicMock()
        mock_wakeup.wait.return_value = False
        mock_event_cls.side_effect = [mock_shutdown, mock_wakeup]

        start_sync_loop(database_connection, settings)

    # Both the Pub/Sub setup and the PG listener received the wakeup_event.
    assert mock_wakeup in mock_pubsub.call_args.args
    assert mock_wakeup in mock_listener.call_args.args
    # The main loop waited on wakeup_event (with run_interval as fallback).
    mock_wakeup.wait.assert_called_with(timeout=settings.run_interval)
    # The wait was cleared so events during processing re-trigger.
    mock_wakeup.clear.assert_called()


def _iteration_spans(capfire: CaptureLogfire) -> list[dict[str, Any]]:
    return [
        span
        for span in capfire.exporter.exported_spans_as_dict()
        if span["name"] == "sync.loop.iteration"
    ]


def test_run_periodic_iteration_tags_event_wakeup_source(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """sync.loop.iteration span carries wakeup_source='event' when triggered by an event."""
    import queue as _queue  # local to avoid polluting module imports

    from mailpilot.sync import (
        _run_periodic_iteration,  # pyright: ignore[reportPrivateUsage]
    )

    settings = make_test_settings()
    sync_queue: _queue.Queue[str] = _queue.Queue()

    _run_periodic_iteration(
        database_connection,
        settings,
        sync_queue,
        wakeup_source="event",
        do_full_sweep=True,
    )

    spans = _iteration_spans(capfire)
    assert len(spans) == 1
    assert spans[0]["attributes"]["wakeup_source"] == "event"


def test_run_periodic_iteration_tags_timer_wakeup_source(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """sync.loop.iteration span carries wakeup_source='timer' on periodic-timer wake."""
    import queue as _queue

    from mailpilot.sync import (
        _run_periodic_iteration,  # pyright: ignore[reportPrivateUsage]
    )

    settings = make_test_settings()
    sync_queue: _queue.Queue[str] = _queue.Queue()

    _run_periodic_iteration(
        database_connection,
        settings,
        sync_queue,
        wakeup_source="timer",
        do_full_sweep=True,
    )

    spans = _iteration_spans(capfire)
    assert len(spans) == 1
    assert spans[0]["attributes"]["wakeup_source"] == "timer"


def test_run_periodic_iteration_runs_sync_all_when_do_full_sweep_true(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """do_full_sweep=True -> _sync_all_accounts runs and span did_full_sweep is True."""
    import queue as _queue

    from mailpilot.sync import (
        _run_periodic_iteration,  # pyright: ignore[reportPrivateUsage]
    )

    settings = make_test_settings()
    sync_queue: _queue.Queue[str] = _queue.Queue()

    with patch("mailpilot.sync._sync_all_accounts") as mock_sync_all:
        _run_periodic_iteration(
            database_connection,
            settings,
            sync_queue,
            wakeup_source="event",
            do_full_sweep=True,
        )

    mock_sync_all.assert_called_once()
    spans = _iteration_spans(capfire)
    assert len(spans) == 1
    assert spans[0]["attributes"]["did_full_sweep"] is True


def test_run_periodic_iteration_skips_sync_all_when_do_full_sweep_false(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """do_full_sweep=False -> _sync_all_accounts is skipped and span did_full_sweep is False."""
    import queue as _queue

    from mailpilot.sync import (
        _run_periodic_iteration,  # pyright: ignore[reportPrivateUsage]
    )

    settings = make_test_settings()
    sync_queue: _queue.Queue[str] = _queue.Queue()

    with patch("mailpilot.sync._sync_all_accounts") as mock_sync_all:
        _run_periodic_iteration(
            database_connection,
            settings,
            sync_queue,
            wakeup_source="event",
            do_full_sweep=False,
        )

    mock_sync_all.assert_not_called()
    spans = _iteration_spans(capfire)
    assert len(spans) == 1
    assert spans[0]["attributes"]["did_full_sweep"] is False


def test_run_periodic_iteration_full_sweep_skips_already_synced(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """An account synced from the Pub/Sub queue is not synced again by the
    full sweep in the same iteration.
    """
    import queue as _queue

    from mailpilot.sync import (
        _run_periodic_iteration,  # pyright: ignore[reportPrivateUsage]
    )

    notified = make_test_account(
        database_connection, email="notified@example.com", display_name="Notified"
    )
    other = make_test_account(
        database_connection, email="other@example.com", display_name="Other"
    )

    settings = make_test_settings()
    sync_queue: _queue.Queue[str] = _queue.Queue()
    sync_queue.put(notified.email)

    with (
        patch("mailpilot.sync.GmailClient"),
        patch("mailpilot.sync.sync_account") as mock_sync_account,
    ):
        _run_periodic_iteration(
            database_connection,
            settings,
            sync_queue,
            wakeup_source="event",
            do_full_sweep=True,
        )

    synced_emails = [call.args[1].email for call in mock_sync_account.call_args_list]
    assert sorted(synced_emails) == sorted([notified.email, other.email])


def test_start_sync_loop_time_gates_full_sweep(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """_sync_all_accounts runs at most once per run_interval seconds.

    Two event wakes within run_interval should trigger _sync_all_accounts
    only once -- the first wake. Subsequent event-burst wakes do only the
    queue drain. Once run_interval has elapsed, the next wake runs the
    sweep again.
    """
    settings = make_test_settings(run_interval=30)

    with (
        patch("mailpilot.sync.threading.Event") as mock_event_cls,
        patch("mailpilot.sync._start_task_listener"),
        patch("mailpilot.sync._start_pubsub_logging_errors", return_value=None),
        patch("mailpilot.sync._drain_sync_queue"),
        patch("mailpilot.sync._sync_all_accounts") as mock_sync_all,
        patch("mailpilot.sync.create_tasks_for_routed_emails"),
        patch("mailpilot.sync._drain_pending_tasks"),
        patch("mailpilot.sync._renew_watches_logging_errors"),
        patch("mailpilot.sync.signal.signal"),
        patch("mailpilot.sync.time.monotonic") as mock_monotonic,
    ):
        # Three iterations; one time.monotonic() call per iteration.
        # t=1000: first wake -> 1000 - 0.0 >= 30, sweep runs, last_full_sync=1000.
        # t=1010: second wake -> 1010 - 1000 = 10 < 30, sweep skipped.
        # t=1050: third wake -> 1050 - 1000 = 50 >= 30, sweep runs again.
        mock_monotonic.side_effect = [1000.0, 1010.0, 1050.0]

        mock_shutdown = MagicMock()
        # while-check False x3 (run iterations 1/2/3), then True (exit)
        mock_shutdown.is_set.side_effect = [
            False,  # while
            False,  # post-wait #1
            False,  # while
            False,  # post-wait #2
            False,  # while
            False,  # post-wait #3
            True,  # while -> exit
        ]
        mock_wakeup = MagicMock()
        mock_wakeup.wait.return_value = True  # event-driven wakes
        mock_event_cls.side_effect = [mock_shutdown, mock_wakeup]

        start_sync_loop(database_connection, settings)

    # Sweep ran on iteration 1 and iteration 3, but NOT iteration 2.
    assert mock_sync_all.call_count == 2


# -- sync_account --------------------------------------------------------------


def _epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _make_gmail_message(
    message_id: str,
    thread_id: str = "thread-1",
    from_header: str = "Alice Smith <alice@example.com>",
    subject: str = "Hello there",
    body: str = "Body of the email",
    received_at: datetime | None = None,
    label_ids: list[str] | None = None,
    rfc_message_id: str | None = None,
    extra_headers: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    received_at = received_at or datetime.now(UTC)
    body_b64 = base64.urlsafe_b64encode(body.encode()).decode()
    headers = [
        {"name": "From", "value": from_header},
        {"name": "Subject", "value": subject},
    ]
    if rfc_message_id is not None:
        headers.append({"name": "Message-ID", "value": rfc_message_id})
    if extra_headers is not None:
        headers.extend(extra_headers)
    return {
        "id": message_id,
        "threadId": thread_id,
        "internalDate": str(_epoch_ms(received_at)),
        "labelIds": label_ids or ["INBOX"],
        "payload": {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {"data": body_b64},
        },
    }


def _make_mock_client(
    email: str = "account@example.com",
) -> tuple[GmailClient, MagicMock]:
    service = MagicMock()
    # getProfile returns a history id for post-sync account update.
    service.users.return_value.getProfile.return_value.execute.return_value = {
        "emailAddress": email,
        "historyId": "9999",
    }
    client = GmailClient.from_service(email, service)
    return client, service


def _set_list_messages(service: MagicMock, stubs: list[dict[str, str]]) -> None:
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": stubs
    }


def _set_get_messages(service: MagicMock, messages: list[dict[str, Any]]) -> None:
    """Mock get_messages_batch by simulating new_batch_http_request.

    Builds an id-keyed lookup from the supplied messages so the batch
    callback returns the right payload for each request_id.
    """
    by_id: dict[str, dict[str, Any]] = {m["id"]: m for m in messages}

    def _make_batch() -> MagicMock:
        callbacks: list[tuple[str, object, object]] = []

        def fake_add(request: object, callback: object, request_id: str) -> None:
            callbacks.append((request_id, request, callback))

        def fake_execute() -> None:
            for request_id, _request, callback in callbacks:
                data = by_id.get(request_id)
                callback(request_id, data, None)  # type: ignore[operator]

        batch = MagicMock()
        batch.add = fake_add
        batch.execute = fake_execute
        return batch

    service.new_batch_http_request.side_effect = _make_batch


def _set_history(
    service: MagicMock,
    history_records: list[dict[str, Any]] | None = None,
    raise_exc: Exception | None = None,
) -> None:
    node = service.users.return_value.history.return_value.list.return_value.execute
    if raise_exc is not None:
        node.side_effect = raise_exc
    else:
        node.return_value = {"history": history_records or []}


def _http_error_404() -> HttpError:
    resp = MagicMock()
    resp.status = 404
    resp.reason = "Not Found"
    return HttpError(resp=resp, content=b'{"error": "history not found"}')


def test_sync_account_full_sync_when_no_history_id(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection, email="full@example.com")
    client, service = _make_mock_client(account.email)
    _set_list_messages(service, [{"id": "m1", "threadId": "t1"}])
    _set_get_messages(service, [_make_gmail_message("m1", "t1")])

    stored = sync_account(database_connection, account, client, make_test_settings())

    assert stored == 1
    email = get_email_by_gmail_message_id(database_connection, "m1")
    assert email is not None
    assert email.direction == "inbound"
    assert email.body_text == "Body of the email"
    assert email.subject == "Hello there"
    assert email.labels == ["INBOX"]
    contact = get_contact_by_email(database_connection, "alice@example.com")
    assert contact is not None
    assert contact.first_name == "Alice"
    assert contact.last_name == "Smith"


def test_sync_account_incremental_via_history_api(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection, email="inc@example.com")
    account = update_account(database_connection, account.id, gmail_history_id="500")
    assert account is not None
    client, service = _make_mock_client(account.email)
    _set_history(
        service,
        history_records=[
            {
                "id": "600",
                "messagesAdded": [
                    {"message": {"id": "mA", "threadId": "tA"}},
                ],
            }
        ],
    )
    _set_get_messages(service, [_make_gmail_message("mA", "tA", subject="Incremental")])

    stored = sync_account(database_connection, account, client, make_test_settings())

    assert stored == 1
    # list_messages must not be invoked on the incremental path.
    service.users.return_value.messages.return_value.list.assert_not_called()
    email = get_email_by_gmail_message_id(database_connection, "mA")
    assert email is not None
    assert email.subject == "Incremental"


def test_sync_account_falls_back_to_full_sync_on_history_404(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection, email="stale@example.com")
    account = update_account(
        database_connection, account.id, gmail_history_id="ancient"
    )
    assert account is not None
    client, service = _make_mock_client(account.email)
    _set_history(service, raise_exc=_http_error_404())
    _set_list_messages(service, [{"id": "mX", "threadId": "tX"}])
    _set_get_messages(service, [_make_gmail_message("mX", "tX")])

    stored = sync_account(database_connection, account, client, make_test_settings())

    assert stored == 1
    # history API was tried first...
    service.users.return_value.history.return_value.list.assert_called()
    # ...then full sync kicked in.
    service.users.return_value.messages.return_value.list.assert_called()


def test_sync_account_skips_duplicate_messages(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection, email="dup@example.com")
    # Pre-seed an email with the same gmail_message_id.
    create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="old",
        gmail_message_id="m1",
        gmail_thread_id="t1",
    )
    client, service = _make_mock_client(account.email)
    _set_list_messages(service, [{"id": "m1", "threadId": "t1"}])
    # Duplicate filtered before batch fetch; supply no batch messages.
    _set_get_messages(service, [])

    stored = sync_account(database_connection, account, client, make_test_settings())

    assert stored == 0
    # All IDs were duplicates -> batch fetch receives empty list -> no HTTP call.
    service.new_batch_http_request.assert_not_called()


def test_sync_account_recency_gate_marks_old_messages_routed(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection, email="old@example.com")
    client, service = _make_mock_client(account.email)
    _set_list_messages(service, [{"id": "old", "threadId": "t-old"}])
    old_date = datetime.now(UTC) - timedelta(days=30)
    _set_get_messages(
        service, [_make_gmail_message("old", "t-old", received_at=old_date)]
    )

    stored = sync_account(database_connection, account, client, make_test_settings())

    assert stored == 1
    email = get_email_by_gmail_message_id(database_connection, "old")
    assert email is not None
    assert email.is_routed is True
    assert email.workflow_id is None


def test_sync_account_fresh_message_routed_as_unrouted_without_workflows(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection, email="fresh@example.com")
    client, service = _make_mock_client(account.email)
    _set_list_messages(service, [{"id": "fresh", "threadId": "t-fresh"}])
    _set_get_messages(service, [_make_gmail_message("fresh", "t-fresh")])

    sync_account(database_connection, account, client, make_test_settings())

    email = get_email_by_gmail_message_id(database_connection, "fresh")
    assert email is not None
    # No prior thread emails, no active inbound workflows -> deliberately unrouted.
    assert email.is_routed is True
    assert email.workflow_id is None


def test_sync_account_skips_routing_when_no_active_workflows(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """route_email must not be called when the account has zero active workflows."""
    from unittest.mock import patch

    account = make_test_account(database_connection, email="noroute@example.com")
    client, service = _make_mock_client(account.email)
    _set_list_messages(service, [{"id": "nr1", "threadId": "t-nr1"}])
    _set_get_messages(service, [_make_gmail_message("nr1", "t-nr1")])

    with patch("mailpilot.sync.route_email") as mock_route:
        sync_account(database_connection, account, client, make_test_settings())

    mock_route.assert_not_called()
    email = get_email_by_gmail_message_id(database_connection, "nr1")
    assert email is not None
    assert email.is_routed is True
    assert email.workflow_id is None


def test_sync_account_skips_classification_for_emails_before_earliest_workflow(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Emails older than the earliest active workflow skip LLM classification."""
    from unittest.mock import patch

    from mailpilot.database import activate_workflow, update_workflow

    account = make_test_account(database_connection, email="hist@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    update_workflow(
        database_connection,
        workflow.id,
        objective="Handle inquiries",
        instructions="Reply helpfully",
    )
    activate_workflow(database_connection, workflow.id)

    client, service = _make_mock_client(account.email)
    # Email received 2 days ago (within 7-day recency window) but 1 hour
    # before the workflow was created -- should skip classification.
    email_time = workflow.created_at - timedelta(hours=1)
    _set_list_messages(service, [{"id": "pre-wf", "threadId": "t-pre-wf"}])
    _set_get_messages(
        service,
        [_make_gmail_message("pre-wf", "t-pre-wf", received_at=email_time)],
    )

    with patch("mailpilot.sync.route_email") as mock_route:
        stored = sync_account(
            database_connection, account, client, make_test_settings()
        )

    assert stored == 1
    mock_route.assert_not_called()
    email = get_email_by_gmail_message_id(database_connection, "pre-wf")
    assert email is not None
    assert email.is_routed is True
    assert email.workflow_id is None


def test_sync_account_uses_batch_fetch(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """sync_account fetches messages via get_messages_batch, not get_message."""
    account = make_test_account(database_connection, email="batch@example.com")
    client, service = _make_mock_client(account.email)
    stubs = [{"id": f"m{i}", "threadId": f"t{i}"} for i in range(3)]
    _set_list_messages(service, stubs)
    _set_get_messages(
        service, [_make_gmail_message(f"m{i}", f"t{i}") for i in range(3)]
    )

    stored = sync_account(database_connection, account, client, make_test_settings())

    assert stored == 3
    # Batch method must have been used, not per-message get.
    service.new_batch_http_request.assert_called()


def test_sync_account_auto_creates_contact_only_once(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection, email="dual@example.com")
    client, service = _make_mock_client(account.email)
    _set_list_messages(
        service,
        [{"id": "m1", "threadId": "t1"}, {"id": "m2", "threadId": "t2"}],
    )
    _set_get_messages(
        service,
        [
            _make_gmail_message("m1", "t1", from_header="Bob <bob@example.com>"),
            _make_gmail_message("m2", "t2", from_header="Bob <bob@example.com>"),
        ],
    )

    stored = sync_account(database_connection, account, client, make_test_settings())

    assert stored == 2
    # Only one contact row for Bob despite two messages.
    contact = get_contact_by_email(database_connection, "bob@example.com")
    assert contact is not None
    emails = list_emails(database_connection, account_id=account.id)
    assert len(emails) == 2
    assert all(e.contact_id == contact.id for e in emails)


def test_sync_account_bulk_prefetches_contacts_once(
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
):
    """sync_account must call the bulk contact helpers once regardless of message count.

    Guards against the per-message N+1 fixed in #34: round-trips should
    scale with distinct senders, not total messages.
    """
    import mailpilot.database as db
    import mailpilot.sync as sync_module

    account = make_test_account(database_connection, email="bulk@example.com")
    client, service = _make_mock_client(account.email)
    stubs = [{"id": f"m{i}", "threadId": f"t{i}"} for i in range(5)]
    _set_list_messages(service, stubs)
    # 5 messages but only 2 distinct senders.
    senders = [
        "Bob <bob@example.com>",
        "Carol <carol@example.com>",
        "Bob <bob@example.com>",
        "Carol <carol@example.com>",
        "Bob <bob@example.com>",
    ]
    _set_get_messages(
        service,
        [
            _make_gmail_message(f"m{i}", f"t{i}", from_header=senders[i])
            for i in range(5)
        ],
    )

    get_calls: list[list[str]] = []
    create_calls: list[list[str]] = []

    real_get = db.get_contacts_by_emails
    real_create = db.create_contacts_bulk

    def spy_get(
        connection: psycopg.Connection[dict[str, Any]],
        emails: object,
    ) -> dict[str, Any]:
        materialized = list(emails)  # type: ignore[arg-type]
        get_calls.append(materialized)
        return real_get(connection, materialized)

    def spy_create(
        connection: psycopg.Connection[dict[str, Any]],
        emails: object,
    ) -> dict[str, Any]:
        materialized = list(emails)  # type: ignore[arg-type]
        create_calls.append(materialized)
        return real_create(connection, materialized)

    monkeypatch.setattr(sync_module, "get_contacts_by_emails", spy_get)
    monkeypatch.setattr(sync_module, "create_contacts_bulk", spy_create)

    # Fail loudly if the old per-message path is still reachable.
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "create_or_get_contact_by_email must not be called from sync_account"
        )

    monkeypatch.setattr(sync_module, "create_or_get_contact_by_email", forbidden)

    stored = sync_account(database_connection, account, client, make_test_settings())

    assert stored == 5
    assert len(get_calls) == 1, (
        f"expected 1 get_contacts_by_emails call, got {get_calls}"
    )
    # bulk insert called at most once (may be skipped when all senders exist).
    assert len(create_calls) <= 1
    # Exactly 2 contact rows despite 5 messages.
    emails_stored = list_emails(database_connection, account_id=account.id)
    contact_ids = {e.contact_id for e in emails_stored}
    assert len(contact_ids) == 2


def test_sync_account_updates_account_history_and_last_synced(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection, email="state@example.com")
    client, service = _make_mock_client(account.email)
    service.users.return_value.getProfile.return_value.execute.return_value = {
        "historyId": "12345"
    }
    _set_list_messages(service, [])
    _set_get_messages(service, [])

    before = datetime.now(UTC)
    sync_account(database_connection, account, client, make_test_settings())

    from mailpilot.database import get_account

    refreshed = get_account(database_connection, account.id)
    assert refreshed is not None
    assert refreshed.gmail_history_id == "12345"
    assert refreshed.last_synced_at is not None
    assert refreshed.last_synced_at >= before


# -- send_email ---------------------------------------------------------------


def _get_sent_mime(
    service: MagicMock,
) -> tuple[Any, list[Any]]:
    """Extract the sent MIME message and its parts from a Gmail mock.

    Returns:
        Tuple of (outer_message, parts_list). The outer message is
        a multipart/alternative; parts_list has [plain_part, html_part].
    """
    from email import message_from_bytes

    send_body = service.users.return_value.messages.return_value.send.call_args.kwargs[
        "body"
    ]
    raw = base64.urlsafe_b64decode(send_body["raw"])
    msg = message_from_bytes(raw)
    payload = msg.get_payload()
    assert isinstance(payload, list)
    return msg, list(payload)


def _make_send_client(
    email: str = "sender@example.com",
    send_result: dict[str, Any] | None = None,
) -> tuple[GmailClient, MagicMock]:
    service = MagicMock()
    payload = send_result or {
        "id": "gmail-msg-1",
        "threadId": "gmail-thread-1",
        "labelIds": ["SENT"],
    }
    service.users.return_value.messages.return_value.send.return_value.execute.return_value = payload
    client = GmailClient.from_service(email, service)
    return client, service


def test_send_email_records_outbound_row(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection, email="sender@example.com")
    client, service = _make_send_client(account.email)

    before = datetime.now(UTC)
    email = send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Hello",
        body="Body text",
    )

    assert email.direction == "outbound"
    assert email.status == "sent"
    assert email.subject == "Hello"
    assert email.body_text == "Body text"
    assert email.gmail_message_id == "gmail-msg-1"
    assert email.gmail_thread_id == "gmail-thread-1"
    assert email.is_routed is True
    assert email.sent_at is not None
    assert email.sent_at >= before
    # Gmail API invoked once with the expected payload.
    send_call = service.users.return_value.messages.return_value.send
    assert send_call.call_count == 1
    call_kwargs = send_call.call_args.kwargs
    assert call_kwargs["userId"] == "me"
    assert "raw" in call_kwargs["body"]
    # Persisted row matches the returned model.
    stored = get_email(database_connection, email.id)
    assert stored is not None
    assert stored.direction == "outbound"
    assert stored.status == "sent"


def test_send_email_formats_from_header_with_display_name(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(
        database_connection,
        email="sender@example.com",
        display_name="Alice Sender",
    )
    client, service = _make_send_client(account.email)

    send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Hi",
        body="Body",
    )

    # Decode the raw MIME payload Gmail received to inspect the From header.
    import base64
    from email import message_from_bytes

    send_body = service.users.return_value.messages.return_value.send.call_args.kwargs[
        "body"
    ]
    raw = base64.urlsafe_b64decode(send_body["raw"])
    msg = message_from_bytes(raw)
    assert msg["from"] == "Alice Sender <sender@example.com>"


def test_send_email_from_header_falls_back_to_email_only(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(
        database_connection,
        email="sender@example.com",
        display_name="",
    )
    client, service = _make_send_client(account.email)

    send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Hi",
        body="Body",
    )

    import base64
    from email import message_from_bytes

    send_body = service.users.return_value.messages.return_value.send.call_args.kwargs[
        "body"
    ]
    raw = base64.urlsafe_b64decode(send_body["raw"])
    msg = message_from_bytes(raw)
    assert msg["from"] == "sender@example.com"


def test_send_email_passes_thread_and_optional_links(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection, email="sender@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="outbound"
    )
    client, service = _make_send_client(
        account.email,
        send_result={
            "id": "gmail-msg-2",
            "threadId": "existing-thread",
            "labelIds": ["SENT"],
        },
    )

    email = send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Re: Hello",
        body="Reply body",
        workflow_id=workflow.id,
        thread_id="existing-thread",
    )

    # thread_id flows through to Gmail send payload.
    send_kwargs = service.users.return_value.messages.return_value.send.call_args.kwargs
    assert send_kwargs["body"].get("threadId") == "existing-thread"
    assert email.gmail_thread_id == "existing-thread"
    assert email.workflow_id == workflow.id


def test_send_email_propagates_gmail_errors(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection, email="sender@example.com")
    client, service = _make_send_client(account.email)
    service.users.return_value.messages.return_value.send.return_value.execute.side_effect = RuntimeError(
        "gmail boom"
    )

    with pytest.raises(RuntimeError, match="gmail boom"):
        send_email(
            database_connection,
            account=account,
            gmail_client=client,
            settings=make_test_settings(),
            to="recipient@example.com",
            subject="Hello",
            body="Body",
        )
    # No DB row must have been created when Gmail fails.
    assert list_emails(database_connection, account_id=account.id) == []


def test_send_email_passes_cc_and_bcc(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection, email="sender@example.com")
    client, service = _make_send_client(account.email)

    send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Hello",
        body="Body text",
        cc="cc1@example.com,cc2@example.com",
        bcc="bcc@example.com",
    )

    # Decode MIME payload and verify Cc and Bcc headers.
    from email import message_from_bytes

    send_body = service.users.return_value.messages.return_value.send.call_args.kwargs[
        "body"
    ]
    raw = base64.urlsafe_b64decode(send_body["raw"])
    msg = message_from_bytes(raw)
    assert msg["cc"] == "cc1@example.com,cc2@example.com"
    assert msg["bcc"] == "bcc@example.com"


def test_send_email_passes_multiple_to_recipients(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection, email="sender@example.com")
    client, service = _make_send_client(account.email)

    send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="a@example.com,b@example.com",
        subject="Group email",
        body="Body",
    )

    from email import message_from_bytes

    send_body = service.users.return_value.messages.return_value.send.call_args.kwargs[
        "body"
    ]
    raw = base64.urlsafe_b64decode(send_body["raw"])
    msg = message_from_bytes(raw)
    assert msg["to"] == "a@example.com,b@example.com"


# -- Message-ID / In-Reply-To threading ----------------------------------------


def test_sync_stores_message_id_from_inbound_email(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Sync extracts the RFC 2822 Message-ID header and stores it."""
    account = make_test_account(database_connection, email="sync-mid@example.com")
    client, service = _make_mock_client(account.email)
    rfc_mid = "<CABx123@mail.gmail.com>"
    _set_list_messages(service, [{"id": "m-mid", "threadId": "t-mid"}])
    _set_get_messages(
        service,
        [_make_gmail_message("m-mid", "t-mid", rfc_message_id=rfc_mid)],
    )

    sync_account(database_connection, account, client, make_test_settings())

    email = get_email_by_gmail_message_id(database_connection, "m-mid")
    assert email is not None
    assert email.rfc2822_message_id == rfc_mid


def test_sync_stores_none_when_message_id_absent(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """When a message has no Message-ID header, message_id stays None."""
    account = make_test_account(database_connection, email="sync-nomid@example.com")
    client, service = _make_mock_client(account.email)
    _set_list_messages(service, [{"id": "m-nomid", "threadId": "t-nomid"}])
    _set_get_messages(
        service,
        [_make_gmail_message("m-nomid", "t-nomid")],
    )

    sync_account(database_connection, account, client, make_test_settings())

    email = get_email_by_gmail_message_id(database_connection, "m-nomid")
    assert email is not None
    assert email.rfc2822_message_id is None


def test_send_email_sets_in_reply_to_and_references_headers(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """When in_reply_to is provided, the MIME message includes threading headers."""
    from email import message_from_bytes

    account = make_test_account(database_connection, email="reply-hdr@example.com")
    client, service = _make_send_client(account.email)
    original_mid = "<orig-123@mail.gmail.com>"

    send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Re: Hello",
        body="Reply body",
        thread_id="existing-thread",
        in_reply_to=original_mid,
    )

    send_body = service.users.return_value.messages.return_value.send.call_args.kwargs[
        "body"
    ]
    raw = base64.urlsafe_b64decode(send_body["raw"])
    msg = message_from_bytes(raw)
    assert msg["In-Reply-To"] == original_mid
    assert msg["References"] == original_mid


def test_send_email_omits_threading_headers_without_in_reply_to(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Without in_reply_to, no In-Reply-To or References headers are set."""
    from email import message_from_bytes

    account = make_test_account(database_connection, email="no-irt@example.com")
    client, service = _make_send_client(account.email)

    send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Hello",
        body="New message",
    )

    send_body = service.users.return_value.messages.return_value.send.call_args.kwargs[
        "body"
    ]
    raw = base64.urlsafe_b64decode(send_body["raw"])
    msg = message_from_bytes(raw)
    assert msg["In-Reply-To"] is None
    assert msg["References"] is None


def test_send_email_with_thread_id_auto_resolves_in_reply_to_from_local_thread(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """When thread_id is given, send_email pulls In-Reply-To/References from
    the latest local email in that thread without an explicit in_reply_to.

    Reproduces defect 1: outgoing replies must carry RFC 2822 threading
    headers so the recipient client can re-thread the message even when
    Gmail's threadId is opaque to it.
    """
    from email import message_from_bytes

    account = make_test_account(database_connection, email="auto-thread@example.com")
    prior_mid = "<prior-thread@mail.gmail.com>"
    prior = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        gmail_message_id="prior-msg",
        gmail_thread_id="thread-auto",
        rfc2822_message_id=prior_mid,
        subject="Original",
    )
    assert prior is not None

    client, service = _make_send_client(
        account.email,
        send_result={
            "id": "gmail-msg-reply",
            "threadId": "thread-auto",
            "labelIds": ["SENT"],
        },
    )

    send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Re: Original",
        body="Reply body",
        thread_id="thread-auto",
    )

    send_body = service.users.return_value.messages.return_value.send.call_args.kwargs[
        "body"
    ]
    raw = base64.urlsafe_b64decode(send_body["raw"])
    msg = message_from_bytes(raw)
    assert msg["In-Reply-To"] == prior_mid
    assert msg["References"] == prior_mid


def test_send_email_builds_references_chain_across_thread(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """References must list the prior chain space-separated, In-Reply-To the latest."""
    from email import message_from_bytes

    account = make_test_account(database_connection, email="chain@example.com")
    first_mid = "<first-chain@mail.gmail.com>"
    second_mid = "<second-chain@mail.gmail.com>"
    first = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        gmail_message_id="chain-1",
        gmail_thread_id="chain-thread",
        rfc2822_message_id=first_mid,
        status="sent",
    )
    second = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        gmail_message_id="chain-2",
        gmail_thread_id="chain-thread",
        rfc2822_message_id=second_mid,
    )
    assert first is not None
    assert second is not None

    client, service = _make_send_client(
        account.email,
        send_result={
            "id": "chain-3",
            "threadId": "chain-thread",
            "labelIds": ["SENT"],
        },
    )

    send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Re: Re: Hello",
        body="Body",
        thread_id="chain-thread",
    )

    send_body = service.users.return_value.messages.return_value.send.call_args.kwargs[
        "body"
    ]
    raw = base64.urlsafe_b64decode(send_body["raw"])
    msg = message_from_bytes(raw)
    assert msg["In-Reply-To"] == second_mid
    assert msg["References"] == f"{first_mid} {second_mid}"


def test_send_email_falls_back_to_gmail_headers_when_local_message_id_missing(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """If the latest local row in the thread has rfc2822_message_id=None,
    fetch the Gmail message headers to recover it."""
    from email import message_from_bytes

    account = make_test_account(database_connection, email="fallback@example.com")
    legacy = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        gmail_message_id="legacy-sent",
        gmail_thread_id="legacy-thread",
        rfc2822_message_id=None,
        status="sent",
    )
    assert legacy is not None

    recovered_mid = "<legacy-recovered@mail.gmail.com>"
    client, service = _make_send_client(
        account.email,
        send_result={
            "id": "legacy-reply",
            "threadId": "legacy-thread",
            "labelIds": ["SENT"],
        },
    )
    # Make get_message return a message with the recovered Message-ID header.
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "id": "legacy-sent",
        "threadId": "legacy-thread",
        "payload": {
            "headers": [{"name": "Message-ID", "value": recovered_mid}],
        },
    }

    send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Re: legacy",
        body="Body",
        thread_id="legacy-thread",
    )

    send_body = service.users.return_value.messages.return_value.send.call_args.kwargs[
        "body"
    ]
    raw = base64.urlsafe_b64decode(send_body["raw"])
    msg = message_from_bytes(raw)
    assert msg["In-Reply-To"] == recovered_mid
    assert msg["References"] == recovered_mid


def test_send_email_persists_rfc2822_message_id_after_send(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """After Gmail accepts the send, the outbound row is backfilled with
    its RFC 2822 Message-ID so future replies in the same thread can
    build a proper In-Reply-To/References chain."""
    account = make_test_account(database_connection, email="mid-persist@example.com")
    sent_mid = "<sent-fresh@mail.gmail.com>"
    client, service = _make_send_client(
        account.email,
        send_result={
            "id": "fresh-msg",
            "threadId": "fresh-thread",
            "labelIds": ["SENT"],
        },
    )
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "id": "fresh-msg",
        "threadId": "fresh-thread",
        "payload": {
            "headers": [{"name": "Message-ID", "value": sent_mid}],
        },
    }

    email = send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Hello",
        body="Body",
    )

    assert email.rfc2822_message_id == sent_mid
    stored = get_email(database_connection, email.id)
    assert stored is not None
    assert stored.rfc2822_message_id == sent_mid


def test_send_email_parts_use_utf8_charset(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Both plain text and HTML parts use UTF-8 charset for consistent
    rendering across mail clients."""
    account = make_test_account(database_connection, email="enc@example.com")
    client, service = _make_send_client(account.email)

    send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Hello",
        body="Plain ASCII body with no special characters.",
    )

    msg, parts = _get_sent_mime(service)
    assert msg.get_content_type() == "multipart/alternative"
    assert parts[0].get_content_charset() == "utf-8"
    assert parts[1].get_content_charset() == "utf-8"


def test_send_email_produces_multipart_alternative(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """send_email builds multipart/alternative with plain text and HTML parts.

    The plaintext part is the agent's Markdown source verbatim. Markdown is
    designed to be readable as plain text; reducing tables to tab-separated
    columns and stripping bold/italic markers loses information without
    benefit, especially for clients that fall back to text/plain.
    """
    account = make_test_account(database_connection, email="mp@example.com")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    client, service = _make_send_client(account.email)

    body = "**Bold** and a [link](https://lab5.ca)"
    send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Hello",
        body=body,
        workflow_id=workflow.id,
    )

    msg, parts = _get_sent_mime(service)
    assert msg.get_content_type() == "multipart/alternative"
    assert len(parts) == 2
    plain_part = parts[0]
    html_part = parts[1]
    assert plain_part.get_content_type() == "text/plain"
    assert html_part.get_content_type() == "text/html"
    html_raw = html_part.get_payload(decode=True)
    assert isinstance(html_raw, bytes)
    html_body = html_raw.decode()
    assert "<strong>" in html_body
    assert "lab5.ca" in html_body
    plain_raw = plain_part.get_payload(decode=True)
    assert isinstance(plain_raw, bytes)
    plain_body = plain_raw.decode()
    assert plain_body == body


def test_send_email_preserves_markdown_table_in_plaintext(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Markdown tables stay as ``|``-bordered tables in the text/plain part.

    Earlier behavior reduced tables to tab-separated rows, which was
    unreadable in mail clients that surface text/plain.
    """
    account = make_test_account(database_connection, email="t@example.com")
    client, service = _make_send_client(account.email)
    body = "| col1 | col2 |\n|------|------|\n| a | b |"

    send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Hello",
        body=body,
    )

    _msg, parts = _get_sent_mime(service)
    plain_raw = parts[0].get_payload(decode=True)
    assert isinstance(plain_raw, bytes)
    assert plain_raw.decode() == body


def test_send_email_stores_markdown_in_db(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """body_text in DB stores the agent's Markdown source verbatim."""
    account = make_test_account(database_connection, email="db@example.com")
    client, _service = _make_send_client(account.email)
    body = "**Bold** text"

    email = send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Hello",
        body=body,
    )

    assert email.body_text == body


# -- sender / recipients on send and sync -------------------------------------


def test_send_email_stores_sender_and_recipients(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection, email="Outbound@Lab5.ca")
    client, _service = _make_send_client(account.email)

    email = send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="Alice@Example.com",
        subject="Hi",
        body="Body",
        cc="Bob@Example.com",
        bcc="Secret@Example.com",
    )

    assert email.sender == "outbound@lab5.ca"
    assert email.recipients == {
        "to": ["alice@example.com"],
        "cc": ["bob@example.com"],
        "bcc": ["secret@example.com"],
    }


def test_send_email_stores_multiple_to_recipients(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection, email="sender@example.com")
    client, _service = _make_send_client(account.email)

    email = send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="a@example.com,b@example.com",
        subject="Hi",
        body="Body",
    )

    assert email.recipients["to"] == ["a@example.com", "b@example.com"]
    assert "cc" not in email.recipients
    assert "bcc" not in email.recipients


def test_extract_recipients_from_headers():
    from mailpilot.sync import (
        _extract_recipients,  # pyright: ignore[reportPrivateUsage]
    )

    headers = {
        "to": "Alice <alice@example.com>, Bob <bob@example.com>",
        "cc": "Carol <carol@example.com>",
    }
    result = _extract_recipients(headers)
    assert result == {
        "to": ["alice@example.com", "bob@example.com"],
        "cc": ["carol@example.com"],
    }
    assert "bcc" not in result


def test_extract_recipients_empty_headers():
    from mailpilot.sync import (
        _extract_recipients,  # pyright: ignore[reportPrivateUsage]
    )

    result = _extract_recipients({})
    assert result == {}


def test_sync_stores_sender_and_recipients(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Inbound sync populates sender and recipients from Gmail headers."""
    account = make_test_account(database_connection, email="inbox@lab5.ca")
    message = _make_gmail_message(
        message_id="msg_sr_1",
        thread_id="thread_sr_1",
        from_header="Alice <alice@example.com>",
        subject="Test sender/recipients",
        extra_headers=[
            {"name": "To", "value": "inbox@lab5.ca"},
            {"name": "Cc", "value": "dev@lab5.ca"},
        ],
    )

    from mailpilot.sync import (
        _store_inbound_message,  # pyright: ignore[reportPrivateUsage]
    )

    contact = make_test_contact(
        database_connection, email="alice@example.com", domain="example.com"
    )
    email = _store_inbound_message(
        database_connection,
        account,
        message,
        contacts_by_email={"alice@example.com": contact},
        settings=make_test_settings(),
        has_active_workflows=False,
    )
    assert email is not None
    assert email.sender == "alice@example.com"
    assert email.recipients == {
        "to": ["inbox@lab5.ca"],
        "cc": ["dev@lab5.ca"],
    }


def test_sync_inbound_emits_email_received_activity(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """_store_inbound_message must create one email_received activity per
    stored row, with summary=subject and detail.email_id/subject set."""
    from mailpilot.database import create_company, list_activities
    from mailpilot.sync import (
        _store_inbound_message,  # pyright: ignore[reportPrivateUsage]
    )

    account = make_test_account(database_connection, email="inbox@lab5.ca")
    company = create_company(database_connection, name="Example", domain="example.com")
    contact = make_test_contact(
        database_connection,
        email="alice@example.com",
        domain="example.com",
        company_id=company.id,
    )
    message = _make_gmail_message(
        message_id="msg_act_1",
        thread_id="thread_act_1",
        from_header="Alice <alice@example.com>",
        subject="Activity wiring test",
    )

    email = _store_inbound_message(
        database_connection,
        account,
        message,
        contacts_by_email={"alice@example.com": contact},
        settings=make_test_settings(),
        has_active_workflows=False,
    )
    assert email is not None

    activities = list_activities(
        database_connection, contact_id=contact.id, activity_type="email_received"
    )
    assert len(activities) == 1
    activity = activities[0]
    assert activity.summary == "Activity wiring test"
    assert activity.company_id == company.id
    row = database_connection.execute(
        "SELECT email_id FROM activity "
        "WHERE type = 'email_received' AND contact_id = %s",
        (contact.id,),
    ).fetchone()
    assert row is not None
    assert row["email_id"] == email.id


def test_sync_inbound_skips_activity_when_create_email_returns_none(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """A duplicate Gmail message ID returns None from create_email; no
    email_received activity must be emitted in that case."""
    from mailpilot.database import create_email, list_activities
    from mailpilot.sync import (
        _store_inbound_message,  # pyright: ignore[reportPrivateUsage]
    )

    account = make_test_account(database_connection, email="inbox@lab5.ca")
    contact = make_test_contact(
        database_connection, email="alice@example.com", domain="example.com"
    )
    # Pre-insert the same gmail_message_id so the second create_email returns None.
    create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="prior",
        contact_id=contact.id,
        gmail_message_id="dup_msg_1",
        gmail_thread_id="dup_thread_1",
    )
    message = _make_gmail_message(
        message_id="dup_msg_1",
        thread_id="dup_thread_1",
        from_header="Alice <alice@example.com>",
        subject="Should not duplicate",
    )

    result = _store_inbound_message(
        database_connection,
        account,
        message,
        contacts_by_email={"alice@example.com": contact},
        settings=make_test_settings(),
        has_active_workflows=False,
    )
    assert result is None
    activities = list_activities(
        database_connection, contact_id=contact.id, activity_type="email_received"
    )
    assert activities == []


def _routing_spans(capfire: CaptureLogfire) -> list[dict[str, Any]]:
    """Return all exported spans named ``routing.route_email``."""
    return [
        span
        for span in capfire.exporter.exported_spans_as_dict()
        if span["name"] == "routing.route_email"
    ]


def test_sync_stores_in_reply_to_and_references_headers(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Inbound sync persists In-Reply-To / References for routing fallback."""
    from mailpilot.sync import (
        _store_inbound_message,  # pyright: ignore[reportPrivateUsage]
    )

    account = make_test_account(database_connection, email="hdr@example.com")
    message = _make_gmail_message(
        message_id="msg_hdr_1",
        thread_id="thread_hdr_1",
        from_header="Alice <alice@example.com>",
        subject="Re: hello",
        extra_headers=[
            {"name": "In-Reply-To", "value": "<orig@mailpilot.test>"},
            {
                "name": "References",
                "value": "<root@mailpilot.test> <orig@mailpilot.test>",
            },
        ],
    )
    contact = make_test_contact(
        database_connection, email="alice@example.com", domain="example.com"
    )
    email = _store_inbound_message(
        database_connection,
        account,
        message,
        contacts_by_email={"alice@example.com": contact},
        settings=make_test_settings(),
        has_active_workflows=False,
    )
    assert email is not None
    assert email.in_reply_to == "<orig@mailpilot.test>"
    assert email.references_header == "<root@mailpilot.test> <orig@mailpilot.test>"


def test_sync_one_message_emits_skip_span_outside_recency_window(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Messages older than the recency window must emit a skip-tagged span."""
    from mailpilot.sync import (
        _store_inbound_message,  # pyright: ignore[reportPrivateUsage]
    )

    account = make_test_account(database_connection, email="oldspan@example.com")
    old_date = datetime.now(UTC) - timedelta(days=30)
    message = _make_gmail_message(
        message_id="old-span-1",
        thread_id="t-old-span-1",
        received_at=old_date,
    )
    contact = make_test_contact(
        database_connection,
        email="alice@example.com",
        domain="example.com",
    )

    email = _store_inbound_message(
        database_connection,
        account,
        message,
        contacts_by_email={"alice@example.com": contact},
        settings=make_test_settings(),
        has_active_workflows=False,
    )

    assert email is not None
    assert email.is_routed is True
    spans = _routing_spans(capfire)
    assert len(spans) == 1
    attrs = spans[0]["attributes"]
    assert attrs["route_method"] == "skipped_outside_window"
    assert str(attrs["email_id"]) == str(email.id)


def test_sync_one_message_emits_skip_span_when_no_active_workflows(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Messages on accounts without active workflows must emit a skip span."""
    from mailpilot.sync import (
        _store_inbound_message,  # pyright: ignore[reportPrivateUsage]
    )

    account = make_test_account(database_connection, email="nowfspan@example.com")
    # Message is recent (within the 7-day window).
    message = _make_gmail_message(
        message_id="nowf-span-1",
        thread_id="t-nowf-span-1",
    )
    contact = make_test_contact(
        database_connection,
        email="bob@example.com",
        domain="example.com",
    )

    email = _store_inbound_message(
        database_connection,
        account,
        message,
        contacts_by_email={"bob@example.com": contact},
        settings=make_test_settings(),
        has_active_workflows=False,
    )

    assert email is not None
    assert email.is_routed is True
    spans = _routing_spans(capfire)
    assert len(spans) == 1
    attrs = spans[0]["attributes"]
    assert attrs["route_method"] == "skipped_no_workflows"
    assert str(attrs["email_id"]) == str(email.id)


def test_sync_one_message_emits_skip_span_when_predates_workflows(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Messages that predate the earliest active workflow must emit a skip span."""
    from mailpilot.database import activate_workflow, update_workflow
    from mailpilot.sync import (
        _store_inbound_message,  # pyright: ignore[reportPrivateUsage]
    )

    account = make_test_account(database_connection, email="predatespan@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    update_workflow(
        database_connection,
        workflow.id,
        objective="Handle inquiries",
        instructions="Reply helpfully",
    )
    activate_workflow(database_connection, workflow.id)

    # Email received within the 7-day window but 1 hour before workflow was created.
    email_time = workflow.created_at - timedelta(hours=1)
    message = _make_gmail_message(
        message_id="predate-span-1",
        thread_id="t-predate-span-1",
        received_at=email_time,
    )
    contact = make_test_contact(
        database_connection,
        email="carol@example.com",
        domain="example.com",
    )

    email = _store_inbound_message(
        database_connection,
        account,
        message,
        contacts_by_email={"carol@example.com": contact},
        settings=make_test_settings(),
        has_active_workflows=True,
        earliest_workflow_at=workflow.created_at,
    )

    assert email is not None
    assert email.is_routed is True
    spans = _routing_spans(capfire)
    assert len(spans) == 1
    attrs = spans[0]["attributes"]
    assert attrs["route_method"] == "skipped_predates_workflows"
    assert str(attrs["email_id"]) == str(email.id)
