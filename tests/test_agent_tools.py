"""Tests for agent tool implementations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from logfire.testing import CaptureLogfire
from pydantic_ai import RunContext
from pydantic_ai.usage import RunUsage

from conftest import (
    make_test_account,
    make_test_contact,
    make_test_enrollment,
    make_test_settings,
    make_test_workflow,
)
from mailpilot.agent.tools import (
    AgentDeps,
    _mark_reply_emitted,  # pyright: ignore[reportPrivateUsage]
    cancel_task,
    conclude_enrollment,
    create_task,
    disable_contact,
    list_drive_markdown,
    list_enrollments,
    noop,
    read_drive_markdown,
    read_email,
    reply_email,
    reply_emitted_scope,
    reply_was_emitted,
    search_drive_markdown,
    search_emails,
    send_email,
)
from mailpilot.database import (
    activate_workflow,
    create_email,
    create_enrollment,
    get_contact,
    get_task,
    update_workflow,
)
from mailpilot.database import (
    create_task as db_create_task,
)
from mailpilot.database import (
    record_enrollment_outcome as db_record_enrollment_outcome,
)
from mailpilot.models import Account

# -- Helpers -------------------------------------------------------------------


def _activate(connection: psycopg.Connection[dict[str, Any]], workflow_id: str) -> None:
    """Fill required fields and activate a workflow."""
    update_workflow(
        connection,
        workflow_id,
        goal="Test goal",
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


def _ctx(
    connection: Any = None,
    *,
    account: Account | None = None,
    account_id: str | None = None,
    gmail_client: Any = None,
    drive_client: Any = None,
    settings: Any = None,
    workflow_id: str = "wf-1",
    contact_id: str = "contact-1",
    enrollment_id: str = "enroll-1",
) -> RunContext[AgentDeps]:
    """Build a synthetic RunContext[AgentDeps] for direct tool calls."""
    now = datetime.now(UTC)
    return RunContext(
        deps=AgentDeps(
            connection=connection or MagicMock(),
            account=account
            or Account(
                id=account_id or "acct-1",
                email="agent@example.com",
                display_name="Agent",
                created_at=now,
                updated_at=now,
            ),
            gmail_client=gmail_client or MagicMock(),
            drive_client=drive_client or MagicMock(),
            settings=settings or MagicMock(),
            workflow_id=workflow_id,
            contact_id=contact_id,
            enrollment_id=enrollment_id,
        ),
        model=MagicMock(),
        usage=RunUsage(),
    )


# -- reply-emitted flag (§V.131) ----------------------------------------------


def test_reply_was_emitted_false_outside_scope() -> None:
    """§V.131: outside a ``reply_emitted_scope`` the flag reads False and
    ``_mark_reply_emitted`` is a no-op (legacy / CLI paths without a task)."""
    assert reply_was_emitted() is False
    _mark_reply_emitted()
    assert reply_was_emitted() is False


def test_reply_emitted_scope_marks_and_resets() -> None:
    """§V.131: inside the scope the flag starts False, flips True on
    ``_mark_reply_emitted``, and resets to False on scope exit."""
    with reply_emitted_scope():
        assert reply_was_emitted() is False
        _mark_reply_emitted()
        assert reply_was_emitted() is True
    assert reply_was_emitted() is False


def test_send_email_success_marks_reply_emitted(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.131: a successful ``send_email`` marks the per-task reply-emitted
    flag so the run-loop fallback never double-replies."""
    account = make_test_account(database_connection)
    make_test_contact(database_connection, email="recipient@example.com")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    with reply_emitted_scope():
        result = send_email(
            _ctx(
                database_connection,
                account=account,
                gmail_client=gmail_client,
                settings=make_test_settings(),
                workflow_id=workflow.id,
            ),
            to="recipient@example.com",
            subject="Hello",
            body="Hi there",
        )
        assert "error" not in result
        assert reply_was_emitted() is True


def test_reply_email_success_marks_reply_emitted(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.131: a successful ``reply_email`` marks the per-task reply-emitted
    flag."""
    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection, email="sender@example.com")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
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

    with reply_emitted_scope():
        result = reply_email(
            _ctx(
                database_connection,
                account=account,
                gmail_client=gmail_client,
                settings=make_test_settings(),
                workflow_id=workflow.id,
            ),
            email_id=inbound.id,
            body="Here is the pricing info.",
        )
        assert "error" not in result
        assert reply_was_emitted() is True


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
        _ctx(
            database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            workflow_id=workflow.id,
        ),
        to="recipient@example.com",
        subject="Hello",
        body="Hi there",
    )

    assert result["gmail_message_id"] == "gmail-msg-1"
    assert result["gmail_thread_id"] == "gmail-thread-1"
    assert "id" in result
    gmail_client.send_message.assert_called_once()


def test_send_email_forwards_thread_id_to_email_ops(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """The agent send_email tool forwards thread_id to email_ops.send_email.

    Plumbs outbound thread-continuation through the tool layer (§V.78) so a
    later touch threads natively; the dict return shape is unchanged.
    """
    from unittest.mock import patch

    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    returned = MagicMock()
    returned.id = "email-id-1"
    returned.gmail_message_id = "gmail-msg-1"
    returned.gmail_thread_id = "thread-cont"

    with patch("mailpilot.email_ops.send_email", return_value=returned) as ops_send:
        result = send_email(
            _ctx(
                database_connection,
                account=account,
                gmail_client=gmail_client,
                settings=make_test_settings(),
                workflow_id=workflow.id,
            ),
            to="prospect@example.com",
            subject="Following up",
            body="Third touch",
            thread_id="thread-cont",
        )

    assert ops_send.call_args.kwargs["thread_id"] == "thread-cont"
    assert result["gmail_thread_id"] == "thread-cont"


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
        _ctx(
            database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            workflow_id=workflow.id,
        ),
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
        _ctx(
            database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            workflow_id=workflow.id,
        ),
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
        _ctx(
            database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            workflow_id=workflow.id,
        ),
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
        _ctx(
            database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            workflow_id=workflow.id,
        ),
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
        _ctx(
            database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            workflow_id=workflow.id,
        ),
        email_id=inbound.id,
        body="Reply text",
    )

    assert result["error"] == "contact_disabled"
    assert "bounced" in result["message"]
    gmail_client.send_message.assert_not_called()


# -- send/reply body path (§V.42: no format lint) ------------------------------


def test_send_email_pure_prose_passes(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.42: multi-line ready-copy prose reaches Gmail with no format lint."""
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
        _ctx(
            database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            workflow_id=workflow.id,
        ),
        to="recipient@example.com",
        subject="Hello",
        body=body,
    )

    assert "error" not in result
    gmail_client.send_message.assert_called_once()


def test_send_email_space_aligned_spec_rows_pass(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.42 / §B.128: space-aligned label/value rows no longer format-reject."""
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
        _ctx(
            database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            workflow_id=workflow.id,
        ),
        to="recipient@example.com",
        subject="Specs",
        body=body,
    )

    assert "error" not in result
    gmail_client.send_message.assert_called_once()


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
        _ctx(
            database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            workflow_id=workflow.id,
        ),
        email_id=inbound.id,
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

    scheduled_at = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    result = create_task(
        _ctx(
            database_connection,
            workflow_id=workflow.id,
            contact_id=contact.id,
            enrollment_id=enrollment.id,
        ),
        description="Follow up in 3 days",
        scheduled_at=scheduled_at,
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
        _ctx(
            database_connection,
            workflow_id=workflow.id,
            contact_id=contact.id,
            enrollment_id=enrollment.id,
        ),
        description="Reply to question",
        scheduled_at=(datetime.now(UTC) + timedelta(days=3)).isoformat(),
        context={"topic": "pricing"},
        email_id=email.id,
    )

    task = get_task(database_connection, result["id"])
    assert task is not None
    assert task.context == {"topic": "pricing"}
    assert task.email_id == email.id


def test_create_task_normalizes_t_label_touch_to_int(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.162: new OOO-resume writes numeric touch, not a T<n> label."""
    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

    result = create_task(
        _ctx(
            database_connection,
            workflow_id=workflow.id,
            contact_id=contact.id,
            enrollment_id=enrollment.id,
        ),
        description="Resume after OOO",
        scheduled_at=(datetime.now(UTC) + timedelta(days=5)).isoformat(),
        context={"touch": "T2", "reason": "ooo_pause", "return_date": "2026-08-17"},
    )

    task = get_task(database_connection, result["id"])
    assert task is not None
    assert task.context["touch"] == 2
    assert task.context["reason"] == "ooo_pause"
    assert task.context["return_date"] == "2026-08-17"


def test_create_task_rejects_past_scheduled_at(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.129: a past-dated scheduled_at is rejected at the tool boundary.

    The guard returns an error envelope and persists NO task row so a
    wrong-year follow-up never fires next run-loop tick.
    """
    from mailpilot.database import list_tasks

    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    result = create_task(
        _ctx(
            database_connection,
            workflow_id=workflow.id,
            contact_id=contact.id,
            enrollment_id=enrollment.id,
        ),
        description="Soft follow-up next month",
        scheduled_at=past,
    )

    assert result.get("error") == "past_scheduled_at"
    assert "id" not in result
    assert list_tasks(database_connection, contact_id=contact.id) == []


def test_create_task_future_scheduled_at_persists(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.129: a strictly-future scheduled_at passes the guard and persists."""
    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

    future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    result = create_task(
        _ctx(
            database_connection,
            workflow_id=workflow.id,
            contact_id=contact.id,
            enrollment_id=enrollment.id,
        ),
        description="Soft follow-up next month",
        scheduled_at=future,
    )

    assert "id" in result
    task = get_task(database_connection, result["id"])
    assert task is not None
    assert task.status == "pending"


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

    result = cancel_task(_ctx(database_connection), task_id=task.id)

    assert result["id"] == task.id
    assert result["status"] == "cancelled"


def test_cancel_task_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    result = cancel_task(_ctx(database_connection), task_id="nonexistent")
    assert result["error"] == "not_found"


# -- conclude_enrollment (§V.127) ----------------------------------------------


def test_conclude_enrollment_meeting_booked_records_completed(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.127: meeting_booked records a completed outcome + note, no disable."""
    from mailpilot.database import list_activities, list_notes

    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

    result = conclude_enrollment(
        _ctx(database_connection, enrollment_id=enrollment.id),
        disposition="meeting_booked",
        note="booked a Google Meet",
    )
    assert result == {"disposition": "meeting_booked", "outcome": "completed"}

    types = [
        a.type for a in list_activities(database_connection, contact_id=contact.id)
    ]
    assert "enrollment_completed" in types

    # The contact stays active (no global block on a positive disposition).
    refreshed = get_contact(database_connection, contact.id)
    assert refreshed is not None
    assert refreshed.disabled_reason is None

    notes = list_notes(database_connection, contact_id=contact.id)
    assert any(n.body_preview.startswith("booked a Google Meet") for n in notes)


def test_conclude_enrollment_do_not_contact_disables_contact(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.127 + §V.79/§V.80: do_not_contact records failed + blocks the contact."""
    from mailpilot.database import list_activities

    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

    result = conclude_enrollment(
        _ctx(database_connection, enrollment_id=enrollment.id),
        disposition="do_not_contact",
        note="asked to be removed",
    )
    assert result == {"disposition": "do_not_contact", "outcome": "failed"}

    types = [
        a.type for a in list_activities(database_connection, contact_id=contact.id)
    ]
    assert "enrollment_failed" in types

    blocked = get_contact(database_connection, contact.id)
    assert blocked is not None
    assert blocked.disabled_reason is not None
    assert blocked.disabled_reason.startswith("do_not_contact:")


def test_conclude_enrollment_address_change_note_records_new_email(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.161: address-change do_not_contact note carries redirect + new email.

    Campaign-review referral depends on the note (and disabled_reason) recording
    the new address when the inbound auto-reply stated one. System side-effects
    are the same as any do_not_contact (failed + block + cancel follow-ups).
    """
    from mailpilot.database import list_activities, list_notes

    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

    new_email = "prospect.redirect@example.com"
    note = f"address-change: email redirected to {new_email}; update your records"
    result = conclude_enrollment(
        _ctx(database_connection, enrollment_id=enrollment.id),
        disposition="do_not_contact",
        note=note,
    )
    assert result == {"disposition": "do_not_contact", "outcome": "failed"}

    types = [
        a.type for a in list_activities(database_connection, contact_id=contact.id)
    ]
    assert "enrollment_failed" in types

    blocked = get_contact(database_connection, contact.id)
    assert blocked is not None
    assert blocked.disabled_reason is not None
    assert blocked.disabled_reason.startswith("do_not_contact:")
    assert new_email in blocked.disabled_reason

    notes = list_notes(database_connection, contact_id=contact.id)
    assert any(new_email in n.body_preview for n in notes)


def test_conclude_enrollment_contact_later_schedules_default_reschedule(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.127 + §V.32: contact_later schedules a re-enrollment first-touch.

    With no ``reschedule_at`` the task lands about three months out and
    carries ``trigger=enrollment_schedule`` so it self-fires as a fresh
    first reach-out.
    """
    from mailpilot.database import list_tasks

    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

    result = conclude_enrollment(
        _ctx(database_connection, enrollment_id=enrollment.id),
        disposition="contact_later",
        note="circle back in Q3",
    )
    assert result["disposition"] == "contact_later"
    assert result["outcome"] == "failed"
    scheduled = datetime.fromisoformat(result["reschedule_at"])
    # Default deferral is ~90 days out; assert it is comfortably in the future.
    assert scheduled > datetime.now(UTC) + timedelta(days=80)

    tasks = list_tasks(database_connection, contact_id=contact.id, status="pending")
    assert len(tasks) == 1
    task = get_task(database_connection, tasks[0].id)
    assert task is not None
    assert task.context.get("trigger") == "enrollment_schedule"
    assert task.context.get("touch") == 1
    assert task.email_id is None


def test_conclude_enrollment_contact_later_honors_explicit_reschedule(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.127: an agent-supplied reschedule_at is used verbatim."""
    from mailpilot.database import list_tasks

    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

    when = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    result = conclude_enrollment(
        _ctx(database_connection, enrollment_id=enrollment.id),
        disposition="contact_later",
        note="next month",
        reschedule_at=when,
    )
    assert result["reschedule_at"] == when

    tasks = list_tasks(database_connection, contact_id=contact.id, status="pending")
    assert len(tasks) == 1
    assert tasks[0].scheduled_at == datetime.fromisoformat(when)


def test_conclude_enrollment_contact_later_rejects_past_reschedule(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.129 + §V.127: a past agent-supplied reschedule_at is rejected.

    The guard fires before any side effect, so the enrollment is neither
    concluded nor scheduled; the error envelope lets ``_sent_reply`` skip
    the call (§V.120) and the agent retries with a corrected date.
    """
    from mailpilot.database import list_activities, list_tasks

    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    result = conclude_enrollment(
        _ctx(database_connection, enrollment_id=enrollment.id),
        disposition="contact_later",
        note="circle back",
        reschedule_at=past,
    )

    assert result.get("error") == "past_scheduled_at"
    # No outcome recorded and no re-enrollment task scheduled (no side effects).
    types = [
        a.type for a in list_activities(database_connection, contact_id=contact.id)
    ]
    assert "enrollment_failed" not in types
    assert list_tasks(database_connection, contact_id=contact.id) == []


def test_conclude_enrollment_cancels_future_followups_preserves_first_touch(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.127 + §V.123/§V.32: conclude cancels pending future follow-ups but
    leaves the operator first-touch (trigger=enrollment_schedule) untouched."""

    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

    future = (datetime.now(UTC) + timedelta(days=5)).isoformat()
    followup = db_create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="cold breakup touch",
        scheduled_at=future,
        context={"trigger": "task"},
    )
    first_touch = db_create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="scheduled first reach-out",
        scheduled_at=future,
        context={"trigger": "enrollment_schedule"},
    )

    conclude_enrollment(
        _ctx(database_connection, enrollment_id=enrollment.id),
        disposition="meeting_booked",
        note="booked",
    )

    cancelled = get_task(database_connection, followup.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"

    preserved = get_task(database_connection, first_touch.id)
    assert preserved is not None
    assert preserved.status == "pending"


def test_conclude_enrollment_rejects_invalid_disposition(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    result = conclude_enrollment(
        _ctx(database_connection, enrollment_id="nonexistent"),
        disposition="ghosted",
        note="x",
    )
    assert result.get("error") == "invalid_disposition"


def test_conclude_enrollment_missing_enrollment(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    result = conclude_enrollment(
        _ctx(database_connection, enrollment_id="01900000-0000-7000-8000-0000000000ff"),
        disposition="meeting_booked",
        note="x",
    )
    assert result.get("error") == "not_found"


@pytest.mark.parametrize(
    ("disposition", "outcome_type"),
    [
        ("meeting_booked", "enrollment_completed"),
        ("do_not_contact", "enrollment_failed"),
        ("contact_later", "enrollment_failed"),
    ],
)
def test_conclude_enrollment_forwards_disposition_to_outcome_detail(
    database_connection: psycopg.Connection[dict[str, Any]],
    disposition: str,
    outcome_type: str,
):
    """§V.132: conclude_enrollment forwards its disposition into the outcome
    activity ``detail`` for every disposition (§V.127)."""
    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

    conclude_enrollment(
        _ctx(database_connection, enrollment_id=enrollment.id),
        disposition=disposition,
        note="concluding",
    )

    rows = database_connection.execute(
        "SELECT detail->>'disposition' AS disposition FROM activity "
        "WHERE contact_id = %s AND type = %s",
        (contact.id, outcome_type),
    ).fetchall()
    assert [row["disposition"] for row in rows] == [disposition]


# -- disable_contact -----------------------------------------------------------


def test_disable_contact_success(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = make_test_contact(database_connection)

    result = disable_contact(
        _ctx(database_connection, contact_id=contact.id),
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
        _ctx(database_connection, contact_id="nonexistent"),
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
        _ctx(database_connection, contact_id=contact.id),
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

    result = list_enrollments(_ctx(database_connection, workflow_id=workflow.id))

    assert len(result) == 1
    assert result[0]["contact_id"] == contact.id
    assert result[0]["status"] == "active"


def test_list_enrollments_empty(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)

    result = list_enrollments(_ctx(database_connection, workflow_id=workflow.id))

    assert result == []


def test_list_enrollments_includes_latest_outcome(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Each enrollment row carries the latest enrollment_completed/failed
    outcome so the agent can coordinate across contacts (skip person B if
    person A at the same company already finished the goal)."""
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

    db_record_enrollment_outcome(
        connection=database_connection,
        enrollment_id=completed_enrollment.id,
        outcome="completed",
        reason="meeting booked",
    )
    db_record_enrollment_outcome(
        connection=database_connection,
        enrollment_id=failed_enrollment.id,
        outcome="failed",
        reason="hard bounce",
    )

    rows = list_enrollments(_ctx(database_connection, workflow_id=workflow.id))
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

    db_record_enrollment_outcome(
        connection=database_connection,
        enrollment_id=enrollment.id,
        outcome="failed",
        reason="initial soft fail",
    )
    db_record_enrollment_outcome(
        connection=database_connection,
        enrollment_id=enrollment.id,
        outcome="completed",
        reason="recovered after re-engagement",
    )

    rows = list_enrollments(_ctx(database_connection, workflow_id=workflow.id))
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
        _ctx(database_connection, account_id=a1.id),
        query="pricing",
    )

    assert len(result) == 1
    assert result[0]["account_id"] == a1.id


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
        _ctx(database_connection, account_id=account.id),
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
        _ctx(database_connection, account_id=account.id),
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
        _ctx(database_connection, account_id=account_a.id),
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

    result = list_drive_markdown(_ctx(drive_client=drive_client), folder_id="FOLDER")

    assert result == [{"file_id": "f1", "name": "guide.md"}]
    drive_client.list_markdown.assert_called_once_with("FOLDER")


def test_list_drive_markdown_empty_folder_returns_empty_list() -> None:
    drive_client = MagicMock()
    drive_client.list_markdown.return_value = []

    result = list_drive_markdown(_ctx(drive_client=drive_client), folder_id="EMPTY")

    assert result == []


def test_list_drive_markdown_not_found_returns_error_dict() -> None:
    drive_client = MagicMock()
    drive_client.list_markdown.side_effect = _http_error(404)

    result = list_drive_markdown(_ctx(drive_client=drive_client), folder_id="MISSING")

    assert isinstance(result, dict)
    assert result["error"] == "not_found"
    assert "MISSING" in result["message"]


def test_list_drive_markdown_other_http_error_returns_drive_unavailable() -> None:
    drive_client = MagicMock()
    drive_client.list_markdown.side_effect = _http_error(500, "Server Error")

    result = list_drive_markdown(_ctx(drive_client=drive_client), folder_id="FOLDER")

    assert isinstance(result, dict)
    assert result["error"] == "drive_unavailable"


def test_read_drive_markdown_returns_content_on_success() -> None:
    drive_client = MagicMock()
    drive_client.read_markdown.return_value = {
        "name": "guide.md",
        "content": "# Guide\n\nbody",
        "web_view_link": "https://x/y",
    }

    result = read_drive_markdown(_ctx(drive_client=drive_client), file_id="FID")

    assert result == {
        "name": "guide.md",
        "content": "# Guide\n\nbody",
        "web_view_link": "https://x/y",
    }
    drive_client.read_markdown.assert_called_once_with("FID")


def test_read_drive_markdown_not_found_returns_error_dict() -> None:
    drive_client = MagicMock()
    drive_client.read_markdown.side_effect = _http_error(404)

    result = read_drive_markdown(_ctx(drive_client=drive_client), file_id="MISSING")

    assert result["error"] == "not_found"
    assert "MISSING" in result["message"]


def test_read_drive_markdown_other_http_error_returns_drive_unavailable() -> None:
    drive_client = MagicMock()
    drive_client.read_markdown.side_effect = _http_error(503)

    result = read_drive_markdown(_ctx(drive_client=drive_client), file_id="FID")

    assert result["error"] == "drive_unavailable"


# -- search_drive_markdown ---------------------------------------------------


def test_search_drive_markdown_returns_files_on_success() -> None:
    drive_client = MagicMock()
    drive_client.search_markdown.return_value = [
        {"file_id": "f7", "name": "shipping.md"},
        {"file_id": "f3", "name": "returns.md"},
    ]

    result = search_drive_markdown(
        _ctx(drive_client=drive_client),
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
        _ctx(drive_client=drive_client),
        folder_id="FOLDER",
        query="no such topic",
    )

    assert result == []


def test_search_drive_markdown_not_found_returns_error_dict() -> None:
    drive_client = MagicMock()
    drive_client.search_markdown.side_effect = _http_error(404)

    result = search_drive_markdown(
        _ctx(drive_client=drive_client),
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
        _ctx(drive_client=drive_client),
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

    result = list_drive_markdown(_ctx(drive_client=drive_client), folder_id="FOLDER")

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
        _ctx(drive_client=drive_client),
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

    result = read_drive_markdown(_ctx(drive_client=drive_client), file_id="FID")

    assert isinstance(result, dict)
    assert result["error"] == "drive_unavailable"


def test_send_and_reply_share_email_tool_result() -> None:
    """§V.39: send_email and reply_email share _email_tool_result."""
    import inspect

    from mailpilot.agent import tools as tools_module

    assert hasattr(tools_module, "_email_tool_result")
    for fn in (tools_module.send_email, tools_module.reply_email):
        src = inspect.getsource(fn)
        assert "_email_tool_result" in src
        assert "except email_ops.EmailOpsError" not in src


def test_drive_faults_mapped_only_in_drive_call() -> None:
    """§V.38: one _drive_call maps HttpError 404 and timeout/OSError."""
    import inspect

    from mailpilot.agent import tools as tools_module

    assert hasattr(tools_module, "_drive_call")
    helper_src = inspect.getsource(
        tools_module._drive_call  # pyright: ignore[reportPrivateUsage]
    )
    assert "except HttpError" in helper_src
    assert "TimeoutError" in helper_src
    assert "OSError" in helper_src
    for fn in (
        tools_module.list_drive_markdown,
        tools_module.search_drive_markdown,
        tools_module.read_drive_markdown,
    ):
        src = inspect.getsource(fn)
        assert "_drive_call" in src
        assert "except HttpError" not in src


# -- §V.71 amend (§T.145): runtime fact-check abolished -----------------------


def test_runtime_fact_check_abolished() -> None:
    """§V.71 amend / §T.145: the runtime numeric-token fact-check is gone.

    ``_fact_check_body`` and its ``read_ledger`` plumbing are removed from the
    tool layer and ``AgentDeps``; numeric-spec grounding is verified at
    test-time via the reply-test grading (§V.105), not a bespoke core runtime
    guard (§V.45). Guards the recurrence class -- re-introducing a runtime
    fact-check re-opens the seed-unstable / prose-collision verdicts (§B.56,
    §B.58) and the latency loops (§B.59) the abolition closed.
    """
    import dataclasses
    import inspect

    from mailpilot.agent import tools as tools_module
    from mailpilot.agent.tools import AgentDeps

    assert not hasattr(tools_module, "_fact_check_body")
    for fn in (
        tools_module.send_email,
        tools_module.reply_email,
        tools_module.read_drive_markdown,
    ):
        params = inspect.signature(fn).parameters
        assert "read_ledger" not in params, fn.__name__
    field_names = {f.name for f in dataclasses.fields(AgentDeps)}
    assert "read_ledger" not in field_names


def test_reply_email_numeric_tokens_send_without_fact_check(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.71 amend / §T.145: a reply citing numeric tokens sends -- no runtime
    grounding check intercepts it.

    Pre-abolition the (now-removed) ``_fact_check_body`` could reject digits
    absent from a Drive ledger. With the runtime check gone, ungrounded specs
    are caught by the §V.105 test-time grader instead, so the tool layer sends
    the reply unchallenged.
    """
    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection, email="sender@example.com")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="WS36-600-2 flow rates?",
        contact_id=contact.id,
        workflow_id=workflow.id,
        gmail_message_id="inbound-no-factcheck",
        gmail_thread_id="thread-no-factcheck",
    )
    assert inbound is not None
    gmail_client = _make_gmail_client(account)

    result = reply_email(
        _ctx(
            database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            workflow_id=workflow.id,
        ),
        email_id=inbound.id,
        body="The WS36-600-2 runs 110 GPM continuous and 165 GPM peak.",
    )

    assert "error" not in result
    gmail_client.send_message.assert_called_once()


# -- noop ----------------------------------------------------------------------


def test_noop() -> None:
    result = noop(_ctx(), reason="no action needed")
    assert result["acknowledged"] is True
    assert result["reason"] == "no action needed"


# -- Span contract: no duplicate agent.tool.* spans ---------------------------


def test_no_custom_agent_tool_spans(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Agent tools must not emit custom agent.tool.* spans.

    Pydantic AI's instrument_pydantic_ai() already creates an 'execute_tool'
    span per tool call with tool arguments. Custom spans duplicate that.
    See issue #72.
    """
    # Exercise a representative tool that previously emitted a custom span.
    read_email(
        _ctx(database_connection, account_id="01900000-0000-7000-8000-000000000000"),
        email_id="01900000-0000-7000-8000-000000000001",
    )

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
        _ctx(
            database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            workflow_id=workflow.id,
        ),
        to="activate@example.com",
        subject="Hello",
        body="Hi",
    )

    span_names = [s["name"] for s in capfire.exporter.exported_spans_as_dict()]
    assert "agent.auto_activate_contact" not in span_names
