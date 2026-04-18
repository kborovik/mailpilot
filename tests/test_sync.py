"""Tests for sync status database operations and the per-account sync pipeline."""

import base64
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from googleapiclient.errors import HttpError

from conftest import (
    make_test_account,
    make_test_settings,
    make_test_workflow,
)
from mailpilot.database import (
    activate_workflow,
    create_email,
    delete_sync_status,
    get_contact_by_email,
    get_email,
    get_email_by_gmail_message_id,
    get_sync_status,
    list_emails,
    update_account,
    update_sync_heartbeat,
    update_workflow,
    upsert_sync_status,
)
from mailpilot.gmail import GmailClient
from mailpilot.sync import is_pid_alive, route_email, send_email, sync_account


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
) -> dict[str, Any]:
    received_at = received_at or datetime.now(UTC)
    body_b64 = base64.urlsafe_b64encode(body.encode()).decode()
    return {
        "id": message_id,
        "threadId": thread_id,
        "internalDate": str(_epoch_ms(received_at)),
        "labelIds": label_ids or ["INBOX"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": from_header},
                {"name": "Subject", "value": subject},
            ],
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


def _set_get_messages(
    service: MagicMock, messages: list[dict[str, Any] | None]
) -> None:
    service.users.return_value.messages.return_value.get.return_value.execute.side_effect = messages


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
    # get_message should NOT be called for the duplicate; supply nothing.
    _set_get_messages(service, [])

    stored = sync_account(database_connection, account, client, make_test_settings())

    assert stored == 0
    service.users.return_value.messages.return_value.get.assert_not_called()


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


def test_sync_account_fresh_message_stays_unrouted_without_thread(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection, email="fresh@example.com")
    client, service = _make_mock_client(account.email)
    _set_list_messages(service, [{"id": "fresh", "threadId": "t-fresh"}])
    _set_get_messages(service, [_make_gmail_message("fresh", "t-fresh")])

    sync_account(database_connection, account, client, make_test_settings())

    email = get_email_by_gmail_message_id(database_connection, "fresh")
    assert email is not None
    # No prior thread emails, classify not wired up -> deferred.
    assert email.is_routed is False
    assert email.workflow_id is None


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


# -- route_email ---------------------------------------------------------------


def test_route_email_thread_match_assigns_workflow(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection, email="route@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    # Make workflow active so it has a workflow_id we can reuse.
    update_workflow(
        database_connection,
        workflow.id,
        objective="o",
        instructions="i",
    )
    activate_workflow(database_connection, workflow.id)

    prior = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="prior",
        gmail_thread_id="thread-xyz",
        workflow_id=workflow.id,
        is_routed=True,
    )
    assert prior is not None
    assert prior.workflow_id == workflow.id

    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="reply",
        gmail_thread_id="thread-xyz",
    )
    assert new_email is not None

    routed = route_email(database_connection, new_email)

    assert routed.workflow_id == workflow.id
    assert routed.is_routed is True


def test_route_email_no_thread_match_leaves_unrouted(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection, email="noroute@example.com")
    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="orphan",
        gmail_thread_id="thread-orphan",
    )
    assert new_email is not None

    routed = route_email(database_connection, new_email)

    assert routed.workflow_id is None
    assert routed.is_routed is False
    stored = get_email(database_connection, new_email.id)
    assert stored is not None
    assert stored.is_routed is False


# -- send_email ---------------------------------------------------------------


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
