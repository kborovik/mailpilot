"""Tests for agent tool implementations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import psycopg
from logfire.testing import CaptureLogfire

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
    list_enrollments,
    noop,
    read_company,
    read_contact,
    read_email,
    record_enrollment_outcome,
    reply_email,
    search_emails,
    send_email,
)
from mailpilot.database import (
    activate_workflow,
    create_email,
    create_enrollment,
    get_contact,
    get_enrollment,
    get_task,
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


# -- record_enrollment_outcome -----------------------------------------------------


def test_record_enrollment_outcome_completed_writes_activity_only(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """outcome='completed' emits enrollment_completed activity, no status change."""
    from mailpilot.database import list_activities

    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    create_enrollment(database_connection, workflow.id, contact.id)

    result = record_enrollment_outcome(
        connection=database_connection,
        workflow_id=workflow.id,
        contact_id=contact.id,
        outcome="completed",
        reason="meeting booked",
    )
    assert result == {"outcome": "completed", "reason": "meeting booked"}

    enrollment = get_enrollment(database_connection, workflow.id, contact.id)
    assert enrollment is not None
    assert enrollment.status == "active"  # unchanged

    activities = list_activities(database_connection, contact_id=contact.id)
    types = [a.type for a in activities]
    assert "enrollment_completed" in types


def test_record_enrollment_outcome_failed(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """outcome='failed' emits enrollment_failed activity."""
    from mailpilot.database import list_activities

    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    create_enrollment(database_connection, workflow.id, contact.id)

    record_enrollment_outcome(
        connection=database_connection,
        workflow_id=workflow.id,
        contact_id=contact.id,
        outcome="failed",
        reason="no response",
    )
    types = [
        a.type for a in list_activities(database_connection, contact_id=contact.id)
    ]
    assert "enrollment_failed" in types


def test_record_enrollment_outcome_rejects_invalid_outcome(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    result = record_enrollment_outcome(
        connection=database_connection,
        workflow_id="wf1",
        contact_id="c1",
        outcome="cancelled",
        reason="x",
    )
    assert result.get("error") == "invalid_outcome"


def test_record_enrollment_outcome_missing_enrollment(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    result = record_enrollment_outcome(
        connection=database_connection,
        workflow_id="wf-nonexistent",
        contact_id="c-nonexistent",
        outcome="completed",
        reason="x",
    )
    assert result.get("error") == "not_found"


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


def test_disable_contact_invalid_status_returns_error(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Passing a status outside the DB CHECK constraint returns an error dict.

    Regression: previously the CheckViolation bubbled up uncaught, leaving the
    transaction in a failed state and crashing the agent invocation loop.
    """
    contact = make_test_contact(database_connection)

    result = disable_contact(
        connection=database_connection,
        contact_id=contact.id,
        status="not-a-real-status",
        reason="test",
    )

    assert result["error"] == "invalid_status"
    assert "bounced" in result["message"] or "unsubscribed" in result["message"]

    # Connection must still be usable after the rollback.
    refetched = get_contact(database_connection, contact.id)
    assert refetched is not None
    assert refetched.status == "active"


# -- list_enrollments ----------------------------------------------------


def test_list_enrollments_success(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    create_enrollment(database_connection, workflow.id, contact.id)

    result = list_enrollments(connection=database_connection, workflow_id=workflow.id)

    assert len(result) == 1
    assert result[0]["contact_id"] == contact.id
    assert result[0]["status"] == "active"


def test_list_enrollments_empty(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)

    result = list_enrollments(connection=database_connection, workflow_id=workflow.id)

    assert result == []


def test_list_enrollments_includes_latest_outcome(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Each enrollment row carries the latest enrollment_completed/failed
    outcome so the agent can coordinate across contacts (skip person B if
    person A at the same company already finished the objective)."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)

    completed_contact = make_test_contact(database_connection, email="done@example.com")
    failed_contact = make_test_contact(database_connection, email="failed@example.com")
    pending_contact = make_test_contact(database_connection, email="open@example.com")
    create_enrollment(database_connection, workflow.id, completed_contact.id)
    create_enrollment(database_connection, workflow.id, failed_contact.id)
    create_enrollment(database_connection, workflow.id, pending_contact.id)

    record_enrollment_outcome(
        connection=database_connection,
        workflow_id=workflow.id,
        contact_id=completed_contact.id,
        outcome="completed",
        reason="meeting booked",
    )
    record_enrollment_outcome(
        connection=database_connection,
        workflow_id=workflow.id,
        contact_id=failed_contact.id,
        outcome="failed",
        reason="hard bounce",
    )

    rows = list_enrollments(connection=database_connection, workflow_id=workflow.id)
    by_contact = {row["contact_id"]: row for row in rows}

    completed_row = by_contact[completed_contact.id]
    assert completed_row["latest_outcome"] == "completed"
    assert completed_row["latest_outcome_reason"] == "meeting booked"
    assert completed_row["latest_outcome_at"] is not None

    failed_row = by_contact[failed_contact.id]
    assert failed_row["latest_outcome"] == "failed"
    assert failed_row["latest_outcome_reason"] == "hard bounce"
    assert failed_row["latest_outcome_at"] is not None

    pending_row = by_contact[pending_contact.id]
    assert pending_row["latest_outcome"] is None
    assert pending_row["latest_outcome_reason"] is None
    assert pending_row["latest_outcome_at"] is None


def test_list_enrollments_uses_most_recent_outcome(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """If multiple outcomes were recorded, only the latest is surfaced."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)

    contact = make_test_contact(database_connection, email="flip@example.com")
    create_enrollment(database_connection, workflow.id, contact.id)

    record_enrollment_outcome(
        connection=database_connection,
        workflow_id=workflow.id,
        contact_id=contact.id,
        outcome="failed",
        reason="initial soft fail",
    )
    record_enrollment_outcome(
        connection=database_connection,
        workflow_id=workflow.id,
        contact_id=contact.id,
        outcome="completed",
        reason="recovered after re-engagement",
    )

    rows = list_enrollments(connection=database_connection, workflow_id=workflow.id)
    assert len(rows) == 1
    assert rows[0]["latest_outcome"] == "completed"
    assert rows[0]["latest_outcome_reason"] == "recovered after re-engagement"


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


# -- read_email ----------------------------------------------------------------


def test_read_email_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection, email="alice@example.com")
    email = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="Hello",
        body_text="Full body content here",
        contact_id=contact.id,
        recipients={"to": ["alice@example.com"]},
        status="sent",
    )
    assert email is not None

    result = read_email(
        connection=database_connection,
        account_id=account.id,
        email_id=email.id,
    )

    assert result is not None
    assert result["id"] == email.id
    assert result["body_text"] == "Full body content here"


def test_read_email_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)

    result = read_email(
        connection=database_connection,
        account_id=account.id,
        email_id="0190a000-0000-7000-8000-000000000000",
    )
    assert result is None


def test_read_email_cross_account_returns_none(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account_a = make_test_account(database_connection, email="a@lab5.ca")
    account_b = make_test_account(database_connection, email="b@lab5.ca")
    contact = make_test_contact(database_connection, email="alice@example.com")
    email_b = create_email(
        database_connection,
        account_id=account_b.id,
        direction="outbound",
        subject="Account B private",
        body_text="Sensitive content from account B",
        contact_id=contact.id,
        recipients={"to": ["alice@example.com"]},
        status="sent",
    )
    assert email_b is not None

    result = read_email(
        connection=database_connection,
        account_id=account_a.id,
        email_id=email_b.id,
    )
    assert result is None


# -- noop ----------------------------------------------------------------------


def test_noop() -> None:
    result = noop(reason="no action needed")
    assert result["acknowledged"] is True
    assert result["reason"] == "no action needed"


# -- Span contract: no duplicate agent.tool.* spans ---------------------------


def test_no_custom_agent_tool_spans(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Agent tools must not emit custom agent.tool.* spans.

    Pydantic AI's instrument_pydantic_ai() already creates a 'running tool'
    span per tool call with tool arguments. Custom spans duplicate that.
    See issue #72.
    """
    # Exercise a representative tool that previously emitted agent.tool.read_contact.
    read_contact(connection=database_connection, email="nobody@example.com")

    span_names = [s["name"] for s in capfire.exporter.exported_spans_as_dict()]
    agent_tool_spans = [n for n in span_names if n.startswith("agent.tool.")]
    assert agent_tool_spans == [], f"unexpected custom spans: {agent_tool_spans}"


def test_no_custom_auto_activate_span(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """_activate_enrollment_if_pending must not emit agent.auto_activate_contact span.

    The helper's DB work is already captured by the parent tool span.
    See issue #72.
    """
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="activate@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    create_enrollment(database_connection, workflow.id, contact.id)

    gmail_client = _make_gmail_client(account)

    send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        to="activate@example.com",
        subject="Hello",
        body="Hi",
    )

    span_names = [s["name"] for s in capfire.exporter.exported_spans_as_dict()]
    assert "agent.auto_activate_contact" not in span_names
