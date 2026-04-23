"""Tests for agent tool implementations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import psycopg

from conftest import (
    make_test_account,
    make_test_company,
    make_test_contact,
    make_test_settings,
    make_test_workflow,
)
from mailpilot.agent.tools import (
    cancel_task,
    create_task,
    disable_contact,
    list_workflow_contacts,
    noop,
    read_company,
    read_contact,
    reply_email,
    search_emails,
    send_email,
    update_contact_status,
)
from mailpilot.database import (
    activate_workflow,
    create_email,
    create_workflow_contact,
    get_contact,
    get_task,
    get_workflow_contact,
    update_workflow,
)
from mailpilot.database import (
    create_task as db_create_task,
)
from mailpilot.models import Account

# -- Helpers -------------------------------------------------------------------


def _activate(connection: psycopg.Connection[dict[str, Any]], workflow_id: str) -> None:
    """Fill required fields and activate a workflow."""
    update_workflow(
        connection,
        workflow_id,
        objective="Test objective",
        instructions="Test instructions",
    )
    activate_workflow(connection, workflow_id)


def _make_gmail_client(
    account: Account, send_result: dict[str, Any] | None = None
) -> MagicMock:
    """Build a mock GmailClient that returns a fixed send result."""
    client = MagicMock()
    client.send_message.return_value = send_result or {
        "id": "gmail-msg-1",
        "threadId": "gmail-thread-1",
        "labelIds": ["SENT"],
    }
    return client


# -- send_email ----------------------------------------------------------------


def test_send_email_success(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    make_test_contact(
        database_connection, email="recipient@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    result = send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        to="recipient@example.com",
        subject="Hello",
        body="Hi there",
    )

    assert result["gmail_message_id"] == "gmail-msg-1"
    assert result["gmail_thread_id"] == "gmail-thread-1"
    assert "id" in result
    gmail_client.send_message.assert_called_once()


def test_send_email_blocked_by_contact_status(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import disable_contact as db_disable_contact

    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="bounced@example.com", domain="example.com"
    )
    db_disable_contact(database_connection, contact.id, "bounced", "hard bounce")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    result = send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        to="bounced@example.com",
        subject="Hello",
        body="Hi",
    )

    assert result["error"] == "contact_disabled"
    assert "bounced" in result["message"]
    gmail_client.send_message.assert_not_called()


def test_send_email_blocked_by_cooldown(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="recent@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)

    # Recent cold outbound (first in its thread, as Gmail always assigns one).
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="cold pitch",
        contact_id=contact.id,
        workflow_id=workflow.id,
        gmail_message_id="cold-msg",
        gmail_thread_id="cold-thread",
        status="sent",
        sent_at=datetime.now(UTC) - timedelta(days=5),
    )

    gmail_client = _make_gmail_client(account)

    result = send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        to="recent@example.com",
        subject="Follow up",
        body="Hi again",
    )

    assert result["error"] == "cooldown"
    gmail_client.send_message.assert_not_called()


def test_send_email_unknown_contact_succeeds(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Sending to an email not in the contacts table should succeed."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    result = send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        to="unknown@example.com",
        subject="Hello",
        body="Hi",
    )

    assert "error" not in result
    assert result["gmail_message_id"] == "gmail-msg-1"


def test_send_email_passes_cc_and_bcc(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    make_test_contact(
        database_connection, email="recipient@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    result = send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        to="recipient@example.com",
        subject="Hello",
        body="Hi there",
        cc="cc@example.com",
        bcc="bcc@example.com",
    )

    assert "error" not in result
    assert result["gmail_message_id"] == "gmail-msg-1"
    # Verify cc and bcc were passed through to sync.send_email.
    gmail_client.send_message.assert_called_once()
    call_kwargs = gmail_client.send_message.call_args.kwargs
    assert call_kwargs["cc"] == "cc@example.com"
    assert call_kwargs["bcc"] == "bcc@example.com"


# -- reply_email ---------------------------------------------------------------


def test_reply_email_resolves_thread_and_recipient(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="sender@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)

    # Simulate an inbound email that the agent wants to reply to.
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Question about pricing",
        contact_id=contact.id,
        workflow_id=workflow.id,
        gmail_message_id="inbound-msg-1",
        gmail_thread_id="thread-abc",
    )
    assert inbound is not None

    gmail_client = _make_gmail_client(account)

    result = reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        email_id=inbound.id,
        body="Here is the pricing info.",
    )

    assert "error" not in result
    assert result["gmail_message_id"] == "gmail-msg-1"
    assert result["gmail_thread_id"] == "gmail-thread-1"
    assert "id" in result

    # Verify send_message was called with resolved values.
    gmail_client.send_message.assert_called_once()
    call_kwargs = gmail_client.send_message.call_args.kwargs
    assert call_kwargs["to"] == "sender@example.com"
    assert call_kwargs["subject"] == "Re: Question about pricing"
    assert call_kwargs["thread_id"] == "thread-abc"


def test_reply_email_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    result = reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        email_id="nonexistent-email-id",
        body="Hello",
    )

    assert result["error"] == "not_found"
    gmail_client.send_message.assert_not_called()


def test_reply_email_blocked_contact(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import disable_contact as db_disable_contact

    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="bounced@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)

    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Hello",
        contact_id=contact.id,
        gmail_message_id="inbound-bounced",
        gmail_thread_id="thread-bounced",
    )
    assert inbound is not None

    # Disable the contact after the email was received.
    db_disable_contact(database_connection, contact.id, "bounced", "hard bounce")

    gmail_client = _make_gmail_client(account)

    result = reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        email_id=inbound.id,
        body="Reply text",
    )

    assert result["error"] == "contact_disabled"
    assert "bounced" in result["message"]
    gmail_client.send_message.assert_not_called()


def test_reply_email_preserves_existing_re_prefix(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="thread@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)

    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Re: Original topic",
        contact_id=contact.id,
        gmail_message_id="inbound-re",
        gmail_thread_id="thread-re",
    )
    assert inbound is not None

    gmail_client = _make_gmail_client(account)

    result = reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        email_id=inbound.id,
        body="Continuing the thread.",
    )

    assert "error" not in result
    call_kwargs = gmail_client.send_message.call_args.kwargs
    # Should NOT double-prefix to "Re: Re: Original topic".
    assert call_kwargs["subject"] == "Re: Original topic"


def test_reply_email_no_thread_id(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Reply should fail when the original email has no gmail_thread_id."""
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="nothreadid@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)

    # Create an inbound email without a gmail_thread_id.
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="No thread",
        contact_id=contact.id,
        gmail_message_id="inbound-no-thread",
        gmail_thread_id=None,
    )
    assert inbound is not None

    gmail_client = _make_gmail_client(account)

    result = reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        email_id=inbound.id,
        body="Attempting reply",
    )

    assert result["error"] == "no_thread"
    gmail_client.send_message.assert_not_called()


def test_reply_email_no_contact(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Reply should fail when the original email has no contact_id."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)

    # Create an inbound email without a contact_id.
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="No contact",
        contact_id=None,
        gmail_message_id="inbound-no-contact",
        gmail_thread_id="thread-no-contact",
    )
    assert inbound is not None

    gmail_client = _make_gmail_client(account)

    result = reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        email_id=inbound.id,
        body="Attempting reply",
    )

    assert result["error"] == "no_contact"
    gmail_client.send_message.assert_not_called()


# -- create_task ---------------------------------------------------------------


def test_create_task_success(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)

    result = create_task(
        connection=database_connection,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="Follow up in 3 days",
        scheduled_at="2026-04-22T10:00:00Z",
    )

    assert "id" in result
    task = get_task(database_connection, result["id"])
    assert task is not None
    assert task.description == "Follow up in 3 days"
    assert task.status == "pending"


def test_create_task_with_context_and_email(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="question",
    )
    assert email is not None

    result = create_task(
        connection=database_connection,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="Reply to question",
        scheduled_at="2026-04-22T10:00:00Z",
        context={"topic": "pricing"},
        email_id=email.id,
    )

    task = get_task(database_connection, result["id"])
    assert task is not None
    assert task.context == {"topic": "pricing"}
    assert task.email_id == email.id


# -- cancel_task ---------------------------------------------------------------


def test_cancel_task_success(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    task = db_create_task(
        database_connection,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="Follow up",
        scheduled_at="2026-04-22T10:00:00Z",
    )

    result = cancel_task(connection=database_connection, task_id=task.id)

    assert result["id"] == task.id
    assert result["status"] == "cancelled"


def test_cancel_task_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    result = cancel_task(connection=database_connection, task_id="nonexistent")
    assert result["error"] == "not_found"


# -- update_contact_status -----------------------------------------------------


def test_update_contact_status_success(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    create_workflow_contact(database_connection, workflow.id, contact.id)

    result = update_contact_status(
        connection=database_connection,
        workflow_id=workflow.id,
        contact_id=contact.id,
        status="completed",
        reason="meeting booked",
    )

    assert result["status"] == "completed"
    wc = get_workflow_contact(database_connection, workflow.id, contact.id)
    assert wc is not None
    assert wc.status == "completed"
    assert wc.reason == "meeting booked"


def test_update_contact_status_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    result = update_contact_status(
        connection=database_connection,
        workflow_id="nonexistent",
        contact_id="nonexistent",
        status="completed",
        reason="done",
    )

    assert result["error"] == "not_found"


# -- disable_contact -----------------------------------------------------------


def test_disable_contact_success(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = make_test_contact(database_connection)

    result = disable_contact(
        connection=database_connection,
        contact_id=contact.id,
        status="unsubscribed",
        reason="replied: do not contact",
    )

    assert result["status"] == "unsubscribed"
    updated = get_contact(database_connection, contact.id)
    assert updated is not None
    assert updated.status == "unsubscribed"
    assert updated.status_reason == "replied: do not contact"


def test_disable_contact_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    result = disable_contact(
        connection=database_connection,
        contact_id="nonexistent",
        status="bounced",
        reason="hard bounce",
    )

    assert result["error"] == "not_found"


# -- list_workflow_contacts ----------------------------------------------------


def test_list_workflow_contacts_success(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    create_workflow_contact(database_connection, workflow.id, contact.id)

    result = list_workflow_contacts(
        connection=database_connection, workflow_id=workflow.id
    )

    assert len(result) == 1
    assert result[0]["contact_id"] == contact.id
    assert result[0]["status"] == "pending"


def test_list_workflow_contacts_empty(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)

    result = list_workflow_contacts(
        connection=database_connection, workflow_id=workflow.id
    )

    assert result == []


# -- search_emails -------------------------------------------------------------


def test_search_emails_filters_by_account(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    a1 = make_test_account(database_connection, email="a1@test.com")
    a2 = make_test_account(database_connection, email="a2@test.com")

    create_email(
        database_connection,
        account_id=a1.id,
        direction="inbound",
        subject="pricing question",
    )
    create_email(
        database_connection,
        account_id=a2.id,
        direction="inbound",
        subject="pricing info",
    )

    result = search_emails(
        connection=database_connection, account_id=a1.id, query="pricing"
    )

    assert len(result) == 1
    assert result[0]["account_id"] == a1.id


# -- read_contact --------------------------------------------------------------


def test_read_contact_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = make_test_contact(
        database_connection, email="alice@example.com", domain="example.com"
    )

    result = read_contact(connection=database_connection, email="alice@example.com")

    assert result is not None
    assert result["id"] == contact.id
    assert result["email"] == "alice@example.com"


def test_read_contact_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    result = read_contact(connection=database_connection, email="nobody@example.com")
    assert result is None


# -- read_company --------------------------------------------------------------


def test_read_company_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    company = make_test_company(database_connection, name="Acme", domain="acme.com")

    result = read_company(connection=database_connection, domain="acme.com")

    assert result is not None
    assert result["id"] == company.id
    assert result["domain"] == "acme.com"


def test_read_company_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    result = read_company(connection=database_connection, domain="nonexistent.com")
    assert result is None


# -- noop ----------------------------------------------------------------------


def test_noop() -> None:
    result = noop(reason="no action needed")
    assert result["acknowledged"] is True
    assert result["reason"] == "no action needed"
