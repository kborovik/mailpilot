"""Tests for agent tool implementations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from logfire.testing import CaptureLogfire

from conftest import (
    make_test_account,
    make_test_company,
    make_test_contact,
    make_test_enrollment,
    make_test_settings,
    make_test_workflow,
)
from mailpilot.agent.tools import (
    cancel_task,
    create_task,
    disable_contact,
    list_drive_markdown,
    list_enrollments,
    noop,
    read_company,
    read_contact,
    read_drive_markdown,
    read_email,
    record_enrollment_outcome,
    reply_email,
    search_drive_markdown,
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
    make_test_contact(database_connection, email="recipient@example.com")
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
    contact = make_test_contact(database_connection, email="bounced@example.com")
    db_disable_contact(database_connection, contact.id, reason="bounced: hard bounce")
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
    contact = make_test_contact(database_connection, email="recent@example.com")
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
    contact = make_test_contact(database_connection, email="sender@example.com")
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
    contact = make_test_contact(database_connection, email="bounced@example.com")
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
    db_disable_contact(database_connection, contact.id, reason="bounced: hard bounce")

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


# -- _check_spec_table (§V.42) -------------------------------------------------


def test_send_email_pure_prose_passes(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    make_test_contact(database_connection, email="recipient@example.com")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    body = (
        "Hi there,\n\n"
        "Thanks for reaching out. Happy to help -- I will get back to you "
        "with the details shortly.\n\n"
        "Best,\nThe team"
    )

    result = send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        to="recipient@example.com",
        subject="Hello",
        body=body,
    )

    assert "error" not in result
    gmail_client.send_message.assert_called_once()


def test_send_email_pipe_table_passes(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    make_test_contact(database_connection, email="recipient@example.com")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    body = (
        "Here are the specs you asked about:\n\n"
        "| Specification | Value |\n"
        "|---|---|\n"
        "| Continuous Flow Rate | 110 GPM |\n"
        "| Peak Flow Rate | 165 GPM |\n"
        "| Resin Volume | 36 cu ft |\n\n"
        "Let me know if you need anything else."
    )

    result = send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        to="recipient@example.com",
        subject="Specs",
        body=body,
    )

    assert "error" not in result
    gmail_client.send_message.assert_called_once()


def test_send_email_spec_shape_no_table_rejects(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    make_test_contact(database_connection, email="recipient@example.com")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    body = (
        "Here are the specs:\n\n"
        "Continuous Flow Rate  110 GPM\n"
        "Peak Flow Rate  165 GPM\n"
        "Resin Volume  36 cu ft\n"
    )

    result = send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        to="recipient@example.com",
        subject="Specs",
        body=body,
    )

    assert result["error"] == "format"
    assert "|---|" in result["message"]
    gmail_client.send_message.assert_not_called()


def test_reply_email_decline_body_passes(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection, email="sender@example.com")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Veolia OPUS II?",
        contact_id=contact.id,
        workflow_id=workflow.id,
        gmail_message_id="inbound-decline",
        gmail_thread_id="thread-decline",
    )
    assert inbound is not None

    gmail_client = _make_gmail_client(account)

    body = (
        "Thanks for your question about the Veolia OPUS II. Unfortunately "
        "we do not carry that line, so I cannot share specs for it. Happy "
        "to help with our own catalogue if useful."
    )

    result = reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        email_id=inbound.id,
        body=body,
    )

    assert "error" not in result
    gmail_client.send_message.assert_called_once()


def test_send_email_non_numeric_specs_rejects(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§B.14 regression: KDF specs with non-numeric values (e.g. `Mesh Size
    20x50`, `Type  Granular`) must trip the lint even though the value field
    is not a number."""

    account = make_test_account(database_connection)
    make_test_contact(database_connection, email="recipient@example.com")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    body = (
        "Here are the KDF media specs:\n\n"
        "Type  Granular Activated Carbon\n"
        "Mesh Size  20x50\n"
        "Color  Black\n"
    )

    result = send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        to="recipient@example.com",
        subject="Specs",
        body=body,
    )

    assert result["error"] == "format"
    assert "|---|" in result["message"]
    gmail_client.send_message.assert_not_called()


def test_send_email_ascii_rule_line_separator_rejects(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§B.14 regression: ASCII rule-line (`---...`) standalone separators
    between spec rows do NOT count as a pipe-table separator -- only `|---|`
    does -- so an agent using `------` as a faux separator still trips the
    lint."""

    account = make_test_account(database_connection)
    make_test_contact(database_connection, email="recipient@example.com")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    body = (
        "Section A\n"
        "Continuous Flow Rate  110 GPM\n"
        "Peak Flow Rate  165 GPM\n"
        "------------------------------\n"
        "Section B\n"
        "Resin Volume  36 cu ft\n"
    )

    result = send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        to="recipient@example.com",
        subject="Specs",
        body=body,
    )

    assert result["error"] == "format"
    assert "|---|" in result["message"]
    gmail_client.send_message.assert_not_called()


def test_send_email_single_space_bold_label_cluster_rejects(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§B.36 regression: `**label** *value*` cluster rendered with a single
    space between the bold label and italic value -- the smoke-test
    2026-05-28 B4 / B7 shape that slipped past the pre-§T.62 `\\s{2,}`
    floor."""
    account = make_test_account(database_connection)
    make_test_contact(database_connection, email="recipient@example.com")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    body = (
        "Hi -- here are the specs:\n\n"
        "**Peak Flow Rate** *26.6 GPM*\n"
        "**Pressure Loss** *15 psi*\n"
        "**Pipe Connection** *2 inch NPT*\n\n"
        "Let me know."
    )

    result = send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        to="recipient@example.com",
        subject="Specs",
        body=body,
    )

    assert result["error"] == "format"
    assert "|---|" in result["message"]
    gmail_client.send_message.assert_not_called()


def test_send_email_prose_with_incidental_short_line_passes(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.42 prose immunity: a multi-clause prose body with one incidental
    short line broken between long sentences must NOT trip the lint.
    Consecutive tracking resets on the short line (no internal whitespace)
    so the surrounding long lines never reach the 3-consecutive threshold."""
    account = make_test_account(database_connection)
    make_test_contact(database_connection, email="recipient@example.com")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    body = (
        "We completed the migration to the new system and validated all the "
        "data integrity checks across both clusters this morning.\n"
        "OK?\n"
        "Remaining work involves user training and documentation updates "
        "over the next two weeks before the public launch.\n"
        "Thanks!"
    )

    result = send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        to="recipient@example.com",
        subject="Update",
        body=body,
    )

    assert "error" not in result
    gmail_client.send_message.assert_called_once()


def test_send_email_mixed_body_with_separator_passes(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    make_test_contact(database_connection, email="recipient@example.com")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    # Body has spec-shape lines mixed with at least one pipe-table separator
    # somewhere -- the separator presence short-circuits the heuristic.
    body = (
        "Quick numbers below.\n\n"
        "Continuous Flow Rate  110 GPM\n"
        "Peak Flow Rate  165 GPM\n"
        "Resin Volume  36 cu ft\n\n"
        "Detailed table:\n\n"
        "| Spec | Value |\n"
        "|---|---|\n"
        "| Salt Usage | 12 lb |\n"
    )

    result = send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        to="recipient@example.com",
        subject="Mixed",
        body=body,
    )

    assert "error" not in result
    gmail_client.send_message.assert_called_once()


# -- create_task ---------------------------------------------------------------


def test_create_task_success(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

    result = create_task(
        connection=database_connection,
        enrollment_id=enrollment.id,
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
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="question",
    )
    assert email is not None

    result = create_task(
        connection=database_connection,
        enrollment_id=enrollment.id,
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
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    task = db_create_task(
        database_connection,
        enrollment_id=enrollment.id,
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
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

    result = record_enrollment_outcome(
        connection=database_connection,
        enrollment_id=enrollment.id,
        outcome="completed",
        reason="meeting booked",
    )
    assert result == {"outcome": "completed", "reason": "meeting booked"}

    refreshed = get_enrollment(database_connection, workflow.id, contact.id)
    assert refreshed is not None
    assert refreshed.status == "active"  # unchanged

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
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

    record_enrollment_outcome(
        connection=database_connection,
        enrollment_id=enrollment.id,
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
        enrollment_id="nonexistent",
        outcome="cancelled",
        reason="x",
    )
    assert result.get("error") == "invalid_outcome"


def test_record_enrollment_outcome_missing_enrollment(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    result = record_enrollment_outcome(
        connection=database_connection,
        enrollment_id="nonexistent",
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
        reason="unsubscribed: replied do not contact",
    )

    assert result["disabled_reason"] == "unsubscribed: replied do not contact"
    updated = get_contact(database_connection, contact.id)
    assert updated is not None
    assert updated.disabled_reason == "unsubscribed: replied do not contact"


def test_disable_contact_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    result = disable_contact(
        connection=database_connection,
        contact_id="nonexistent",
        reason="bounced: hard bounce",
    )

    assert result["error"] == "not_found"


def test_disable_contact_active_round_trip(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """A fresh contact has disabled_reason=None; disabling sets it."""
    contact = make_test_contact(database_connection)
    fetched = get_contact(database_connection, contact.id)
    assert fetched is not None
    assert fetched.disabled_reason is None

    disable_contact(
        connection=database_connection,
        contact_id=contact.id,
        reason="bounced: hard bounce",
    )

    refetched = get_contact(database_connection, contact.id)
    assert refetched is not None
    assert refetched.disabled_reason == "bounced: hard bounce"


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
    completed_enrollment = make_test_enrollment(
        database_connection, workflow.id, completed_contact.id
    )
    failed_enrollment = make_test_enrollment(
        database_connection, workflow.id, failed_contact.id
    )
    make_test_enrollment(database_connection, workflow.id, pending_contact.id)

    record_enrollment_outcome(
        connection=database_connection,
        enrollment_id=completed_enrollment.id,
        outcome="completed",
        reason="meeting booked",
    )
    record_enrollment_outcome(
        connection=database_connection,
        enrollment_id=failed_enrollment.id,
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
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

    record_enrollment_outcome(
        connection=database_connection,
        enrollment_id=enrollment.id,
        outcome="failed",
        reason="initial soft fail",
    )
    record_enrollment_outcome(
        connection=database_connection,
        enrollment_id=enrollment.id,
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
    contact = make_test_contact(database_connection, email="alice@example.com")

    result = read_contact(connection=database_connection, email="alice@example.com")

    assert result is not None
    assert "error" not in result
    assert result["id"] == contact.id
    assert result["email"] == "alice@example.com"
    assert result["notes"] == []
    assert result["notes_total"] == 0
    assert result["company_notes"] == []
    assert result["company_notes_total"] == 0


def test_read_contact_inlines_notes(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """read_contact view ≡ load_contact_view per §V.8."""
    from mailpilot.database import create_note

    company = make_test_company(database_connection, name="Acme", domain="acme.com")
    contact = make_test_contact(
        database_connection, email="alice@acme.com", company_id=company.id
    )
    contact_note = create_note(
        database_connection, body="Met at conference", contact_id=contact.id
    )
    company_note = create_note(
        database_connection, body="Top customer", company_id=company.id
    )

    result = read_contact(connection=database_connection, email="alice@acme.com")

    assert "error" not in result
    assert len(result["notes"]) == 1
    assert result["notes"][0]["id"] == contact_note.id
    assert result["notes"][0]["body"] == "Met at conference"
    assert result["notes_total"] == 1
    assert len(result["company_notes"]) == 1
    assert result["company_notes"][0]["id"] == company_note.id
    assert result["company_notes_total"] == 1


def test_read_contact_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    result = read_contact(connection=database_connection, email="nobody@example.com")
    assert result == {
        "error": "not_found",
        "message": "contact not found: nobody@example.com",
    }


# -- read_company --------------------------------------------------------------


def test_read_company_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    company = make_test_company(database_connection, name="Acme", domain="acme.com")

    result = read_company(connection=database_connection, domain="acme.com")

    assert result is not None
    assert "error" not in result
    assert result["id"] == company.id
    assert result["domain"] == "acme.com"
    assert result["notes"] == []
    assert result["notes_total"] == 0


def test_read_company_inlines_notes(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """read_company view ≡ load_company_view per §V.8."""
    from mailpilot.database import create_note

    company = make_test_company(database_connection, name="Acme", domain="acme.com")
    note = create_note(
        database_connection, body="High priority lead", company_id=company.id
    )

    result = read_company(connection=database_connection, domain="acme.com")

    assert "error" not in result
    assert len(result["notes"]) == 1
    assert result["notes"][0]["id"] == note.id
    assert result["notes"][0]["body"] == "High priority lead"
    assert result["notes_total"] == 1


def test_read_company_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    result = read_company(connection=database_connection, domain="nonexistent.com")
    assert result == {
        "error": "not_found",
        "message": "company not found: nonexistent.com",
    }


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


# -- list_drive_markdown / read_drive_markdown -------------------------------


def _http_error(status: int, reason: str = "") -> Any:
    """Build a googleapiclient HttpError with a given status."""
    from googleapiclient.errors import HttpError

    resp = MagicMock()
    resp.status = status
    resp.reason = reason or ("Not Found" if status == 404 else "Error")
    return HttpError(resp=resp, content=b"error")


def test_list_drive_markdown_returns_files_on_success() -> None:
    drive_client = MagicMock()
    drive_client.list_markdown.return_value = [
        {"file_id": "f1", "name": "guide.md"},
    ]

    result = list_drive_markdown(drive_client=drive_client, folder_id="FOLDER")

    assert result == [{"file_id": "f1", "name": "guide.md"}]
    drive_client.list_markdown.assert_called_once_with("FOLDER")


def test_list_drive_markdown_empty_folder_returns_empty_list() -> None:
    drive_client = MagicMock()
    drive_client.list_markdown.return_value = []

    result = list_drive_markdown(drive_client=drive_client, folder_id="EMPTY")

    assert result == []


def test_list_drive_markdown_not_found_returns_error_dict() -> None:
    drive_client = MagicMock()
    drive_client.list_markdown.side_effect = _http_error(404)

    result = list_drive_markdown(drive_client=drive_client, folder_id="MISSING")

    assert isinstance(result, dict)
    assert result["error"] == "not_found"
    assert "MISSING" in result["message"]


def test_list_drive_markdown_other_http_error_returns_drive_unavailable() -> None:
    drive_client = MagicMock()
    drive_client.list_markdown.side_effect = _http_error(500, "Server Error")

    result = list_drive_markdown(drive_client=drive_client, folder_id="FOLDER")

    assert isinstance(result, dict)
    assert result["error"] == "drive_unavailable"


def test_read_drive_markdown_returns_content_on_success() -> None:
    drive_client = MagicMock()
    drive_client.read_markdown.return_value = {
        "name": "guide.md",
        "content": "# Guide\n\nbody",
        "web_view_link": "https://x/y",
    }

    result = read_drive_markdown(drive_client=drive_client, file_id="FID")

    assert result == {
        "name": "guide.md",
        "content": "# Guide\n\nbody",
        "web_view_link": "https://x/y",
    }
    drive_client.read_markdown.assert_called_once_with("FID")


def test_read_drive_markdown_not_found_returns_error_dict() -> None:
    drive_client = MagicMock()
    drive_client.read_markdown.side_effect = _http_error(404)

    result = read_drive_markdown(drive_client=drive_client, file_id="MISSING")

    assert result["error"] == "not_found"
    assert "MISSING" in result["message"]


def test_read_drive_markdown_other_http_error_returns_drive_unavailable() -> None:
    drive_client = MagicMock()
    drive_client.read_markdown.side_effect = _http_error(503)

    result = read_drive_markdown(drive_client=drive_client, file_id="FID")

    assert result["error"] == "drive_unavailable"


# -- search_drive_markdown ---------------------------------------------------


def test_search_drive_markdown_returns_files_on_success() -> None:
    drive_client = MagicMock()
    drive_client.search_markdown.return_value = [
        {"file_id": "f7", "name": "shipping.md"},
        {"file_id": "f3", "name": "returns.md"},
    ]

    result = search_drive_markdown(
        drive_client=drive_client,
        folder_id="FOLDER",
        query="shipping policy",
    )

    assert result == [
        {"file_id": "f7", "name": "shipping.md"},
        {"file_id": "f3", "name": "returns.md"},
    ]
    drive_client.search_markdown.assert_called_once_with("FOLDER", "shipping policy")


def test_search_drive_markdown_no_match_returns_empty_list() -> None:
    drive_client = MagicMock()
    drive_client.search_markdown.return_value = []

    result = search_drive_markdown(
        drive_client=drive_client,
        folder_id="FOLDER",
        query="no such topic",
    )

    assert result == []


def test_search_drive_markdown_not_found_returns_error_dict() -> None:
    drive_client = MagicMock()
    drive_client.search_markdown.side_effect = _http_error(404)

    result = search_drive_markdown(
        drive_client=drive_client,
        folder_id="MISSING",
        query="anything",
    )

    assert isinstance(result, dict)
    assert result["error"] == "not_found"
    assert "MISSING" in result["message"]


def test_search_drive_markdown_other_http_error_returns_drive_unavailable() -> None:
    drive_client = MagicMock()
    drive_client.search_markdown.side_effect = _http_error(500, "Server Error")

    result = search_drive_markdown(
        drive_client=drive_client,
        folder_id="FOLDER",
        query="anything",
    )

    assert isinstance(result, dict)
    assert result["error"] == "drive_unavailable"


# -- §V.38 + §B.34: broadened catch envelopes ---------------------------------
# A hung sibling read in a parallel Drive fan-out used to escape the tool
# wrapper as a bare TimeoutError, bubble to ``run.task.agent_failed``, and
# burn the §V.49 retry budget on a deterministic local race. The catch arm
# now folds ``socket.timeout`` / ``TimeoutError`` / ``OSError`` into the same
# ``drive_unavailable`` tool return so the surviving sibling call carries
# the agent run.


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("read timed out"),
        TimeoutError("timed out"),
        OSError(104, "Connection reset"),
    ],
    ids=["socket_timeout", "timeout_error", "oserror"],
)
def test_list_drive_markdown_transport_fault_returns_drive_unavailable(
    exc: BaseException,
) -> None:
    drive_client = MagicMock()
    drive_client.list_markdown.side_effect = exc

    result = list_drive_markdown(drive_client=drive_client, folder_id="FOLDER")

    assert isinstance(result, dict)
    assert result["error"] == "drive_unavailable"


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("read timed out"),
        TimeoutError("timed out"),
        OSError(104, "Connection reset"),
    ],
    ids=["socket_timeout", "timeout_error", "oserror"],
)
def test_search_drive_markdown_transport_fault_returns_drive_unavailable(
    exc: BaseException,
) -> None:
    drive_client = MagicMock()
    drive_client.search_markdown.side_effect = exc

    result = search_drive_markdown(
        drive_client=drive_client,
        folder_id="FOLDER",
        query="anything",
    )

    assert isinstance(result, dict)
    assert result["error"] == "drive_unavailable"


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("read timed out"),
        TimeoutError("timed out"),
        OSError(104, "Connection reset"),
    ],
    ids=["socket_timeout", "timeout_error", "oserror"],
)
def test_read_drive_markdown_transport_fault_returns_drive_unavailable(
    exc: BaseException,
) -> None:
    drive_client = MagicMock()
    drive_client.read_markdown.side_effect = exc

    result = read_drive_markdown(drive_client=drive_client, file_id="FID")

    assert isinstance(result, dict)
    assert result["error"] == "drive_unavailable"


# -- §V.68 pre-send fact-check (§B.46 WS36-600-2 fabrication regression) --------


def test_read_drive_markdown_populates_ledger_on_success() -> None:
    drive_client = MagicMock()
    drive_client.read_markdown.return_value = {
        "name": "industrial-water-softener.md",
        "content": "WS36-600-2: 110 GPM continuous, 125 GPM peak, 30 GPM backwash.",
        "web_view_link": "https://x/y",
    }
    ledger: dict[str, str] = {}

    result = read_drive_markdown(
        drive_client=drive_client, file_id="FID", read_ledger=ledger
    )

    assert "content" in result
    assert ledger["FID"] == (
        "WS36-600-2: 110 GPM continuous, 125 GPM peak, 30 GPM backwash."
    )


def test_read_drive_markdown_error_leaves_ledger_untouched() -> None:
    drive_client = MagicMock()
    drive_client.read_markdown.side_effect = _http_error(404)
    ledger: dict[str, str] = {}

    result = read_drive_markdown(
        drive_client=drive_client, file_id="MISSING", read_ledger=ledger
    )

    assert result["error"] == "not_found"
    assert ledger == {}


def test_reply_email_zero_ledger_skips_fact_check(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.68 no-op when no read_drive_markdown calls succeeded.

    Out-of-scope declines and non-KB-grounded workflows must not be subject
    to the fact-check (zero-ledger = pre-send hook returns None).
    """
    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection, email="sender@example.com")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Question",
        contact_id=contact.id,
        workflow_id=workflow.id,
        gmail_message_id="inbound-1",
        gmail_thread_id="thread-1",
    )
    assert inbound is not None
    gmail_client = _make_gmail_client(account)

    # Body cites numeric tokens, but ledger is empty -> fact-check skipped.
    result = reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        email_id=inbound.id,
        body="Our 30-day trial covers 1500 contacts.",
        read_ledger={},
    )

    assert "error" not in result
    gmail_client.send_message.assert_called_once()


def test_reply_email_supported_tokens_pass(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection, email="sender@example.com")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="WS36-600-2 specs",
        contact_id=contact.id,
        workflow_id=workflow.id,
        gmail_message_id="inbound-2",
        gmail_thread_id="thread-2",
    )
    assert inbound is not None
    gmail_client = _make_gmail_client(account)

    ledger = {
        "softener.md": (
            "WS36-600-2: 110 GPM continuous, 125 GPM peak, 30 GPM backwash."
        )
    }

    result = reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        email_id=inbound.id,
        body=(
            "The WS36-600-2 runs 110 GPM continuous, 125 GPM peak, "
            "and 30 GPM backwash per the datasheet."
        ),
        read_ledger=ledger,
    )

    assert "error" not in result
    gmail_client.send_message.assert_called_once()


def test_reply_email_fabricated_tokens_return_fact_check_mismatch(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§B.46 regression: WS36-600-2 = 65/120/35 fabrication blocked pre-send.

    Source doc carries 110/125/30; body cites 65/120/35; wrapper must refuse
    so the agent re-drafts via the §V.39 tool-error path.
    """
    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection, email="sender@example.com")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="WS36-600-2 flow rates",
        contact_id=contact.id,
        workflow_id=workflow.id,
        gmail_message_id="inbound-3",
        gmail_thread_id="thread-3",
    )
    assert inbound is not None
    gmail_client = _make_gmail_client(account)

    ledger = {
        "softener.md": (
            "WS36-600-2: 110 GPM continuous, 125 GPM peak, 30 GPM backwash."
        ),
        "sf-100s.md": (
            "SF-100S series: same WS36-600-2 family rates 110 / 125 / 30 GPM."
        ),
    }

    result = reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        email_id=inbound.id,
        body=(
            "The WS36-600-2 runs 65 GPM continuous, 120 GPM peak, and 35 GPM backwash."
        ),
        read_ledger=ledger,
    )

    assert result["error"] == "fact_check_mismatch"
    assert set(result["unsupported"]) == {"65", "120", "35"}
    gmail_client.send_message.assert_not_called()


def test_send_email_zero_ledger_skips_fact_check(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    make_test_contact(database_connection, email="recipient@example.com")
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
        body="Reaching out in 2026; ping me back.",
        read_ledger={},
    )

    assert "error" not in result
    gmail_client.send_message.assert_called_once()


def test_send_email_fabricated_tokens_return_fact_check_mismatch(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    make_test_contact(database_connection, email="recipient@example.com")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    ledger = {"pricing.md": "Enterprise plan: 99 seats, 12 month term."}

    result = send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        to="recipient@example.com",
        subject="Pricing",
        body="The plan covers 250 seats over 24 months.",
        read_ledger=ledger,
    )

    assert result["error"] == "fact_check_mismatch"
    assert set(result["unsupported"]) == {"250", "24"}
    gmail_client.send_message.assert_not_called()


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
    contact = make_test_contact(database_connection, email="activate@example.com")
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
