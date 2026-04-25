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
    """send_email builds multipart/alternative with plain text and HTML parts."""
    account = make_test_account(database_connection, email="mp@example.com")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    client, service = _make_send_client(account.email)

    send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Hello",
        body="**Bold** and a [link](https://lab5.ca)",
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
    assert "**" not in plain_body
    assert "Bold" in plain_body


def test_send_email_stores_plain_text_in_db(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """body_text in DB contains stripped plain text, not Markdown."""
    account = make_test_account(database_connection, email="db@example.com")
    client, _service = _make_send_client(account.email)

    email = send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Hello",
        body="**Bold** text",
    )

    assert "**" not in email.body_text
    assert "Bold" in email.body_text


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
