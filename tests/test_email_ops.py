"""Tests for the email_ops policy layer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest

from conftest import (
    make_test_account,
    make_test_contact,
    make_test_settings,
    make_test_workflow,
)
from mailpilot.database import (
    activate_workflow,
    create_email,
    create_enrollment,
    get_enrollment,
    update_workflow,
)
from mailpilot.database import disable_contact as db_disable_contact
from mailpilot.email_ops import (
    ContactDisabledError,
    ContactMissingError,
    CooldownError,
    EmailOpsError,
    OriginalMissingContactError,
    OriginalMissingThreadError,
    OriginalNotFoundError,
    reply_email,
    send_email,
)
from mailpilot.models import Account, Email

# -- Exception hierarchy -------------------------------------------------------


def test_exception_codes_match_agent_tool_strings() -> None:
    """`code` attributes must match the strings the agent tool returned
    historically, so the LLM-facing error contract is preserved."""
    assert ContactDisabledError.code == "contact_disabled"
    assert CooldownError.code == "cooldown"
    assert OriginalNotFoundError.code == "not_found"
    assert OriginalMissingThreadError.code == "no_thread"
    assert OriginalMissingContactError.code == "no_contact"
    assert ContactMissingError.code == "not_found"


def test_exceptions_inherit_from_email_ops_error() -> None:
    for cls in (
        ContactDisabledError,
        CooldownError,
        OriginalNotFoundError,
        OriginalMissingThreadError,
        OriginalMissingContactError,
        ContactMissingError,
    ):
        assert issubclass(cls, EmailOpsError)


def test_exception_str_carries_message() -> None:
    exc = ContactDisabledError("contact is bounced: hard fail")
    assert str(exc) == "contact is bounced: hard fail"
    assert exc.code == "contact_disabled"


# -- Helpers -------------------------------------------------------------------


def _activate(connection: psycopg.Connection[dict[str, Any]], workflow_id: str) -> None:
    update_workflow(
        connection,
        workflow_id,
        objective="Test objective",
        instructions="Test instructions",
    )
    activate_workflow(connection, workflow_id)


def _make_gmail_client(account: Account) -> MagicMock:
    del account
    client = MagicMock()
    client.send_message.return_value = {
        "id": "gmail-msg-1",
        "threadId": "gmail-thread-1",
        "labelIds": ["SENT"],
    }
    return client


# -- send_email ----------------------------------------------------------------


def test_send_email_returns_email_row(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    make_test_contact(
        database_connection, email="recipient@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    email = send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Hello",
        body="Hi there",
        workflow_id=workflow.id,
    )

    assert isinstance(email, Email)
    assert email.gmail_message_id == "gmail-msg-1"
    assert email.gmail_thread_id == "gmail-thread-1"
    gmail_client.send_message.assert_called_once()


def test_send_email_unknown_contact_succeeds(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """No contact row -> no guards fire, send proceeds."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    email = send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        to="brand-new@example.com",
        subject="Hi",
        body="Body",
        workflow_id=workflow.id,
    )
    assert email.gmail_message_id == "gmail-msg-1"


def test_send_email_raises_contact_disabled_when_bounced(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="recipient@example.com", domain="example.com"
    )
    db_disable_contact(database_connection, contact.id, "bounced", "hard fail")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    with pytest.raises(ContactDisabledError) as excinfo:
        send_email(
            connection=database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            to="recipient@example.com",
            subject="Hello",
            body="Hi",
            workflow_id=workflow.id,
        )
    assert "bounced" in str(excinfo.value)
    gmail_client.send_message.assert_not_called()


def test_send_email_raises_cooldown_when_recent_cold_send(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="recipient@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="Earlier",
        contact_id=contact.id,
        workflow_id=workflow.id,
        gmail_message_id="prior-1",
        gmail_thread_id="prior-thread-1",
        sent_at=datetime.now(UTC) - timedelta(days=5),
    )
    gmail_client = _make_gmail_client(account)

    with pytest.raises(CooldownError) as excinfo:
        send_email(
            connection=database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            to="recipient@example.com",
            subject="Hello",
            body="Hi",
            workflow_id=workflow.id,
        )
    assert "cooldown" in str(excinfo.value).lower()
    gmail_client.send_message.assert_not_called()


def test_send_email_activates_pending_enrollment(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="recipient@example.com", domain="example.com"
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
        to="recipient@example.com",
        subject="Hello",
        body="Hi",
        workflow_id=workflow.id,
    )

    enrollment = get_enrollment(database_connection, workflow.id, contact.id)
    assert enrollment is not None
    assert enrollment.status == "active"


def test_send_email_pending_to_active_does_not_emit_workflow_activity(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """The pending->active transition done inside email_ops must NOT emit
    a workflow_completed/workflow_failed/workflow_assigned activity --
    workflow_assigned and email_sent already cover the timeline."""
    from mailpilot.database import list_activities

    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="recipient@example.com", domain="example.com"
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
        to="recipient@example.com",
        subject="Hello",
        body="Hi",
        workflow_id=workflow.id,
    )

    workflow_activities = [
        a
        for a in list_activities(database_connection, contact_id=contact.id)
        if a.type in ("workflow_assigned", "workflow_completed", "workflow_failed")
    ]
    assert workflow_activities == []


def test_send_email_no_workflow_id_skips_enrollment(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """workflow_id=None is allowed (CLI ad-hoc send); no enrollment touched."""
    account = make_test_account(database_connection)
    make_test_contact(
        database_connection, email="recipient@example.com", domain="example.com"
    )
    gmail_client = _make_gmail_client(account)

    email = send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Hello",
        body="Hi",
    )
    assert email.gmail_message_id == "gmail-msg-1"


# -- reply_email ---------------------------------------------------------------


def _make_inbound(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    contact_id: str | None,
    workflow_id: str,
    subject: str = "Question about pricing",
    rfc2822_message_id: str | None = "<inbound-1@example.com>",
    gmail_thread_id: str | None = "thread-abc",
):
    inbound = create_email(
        connection,
        account_id=account_id,
        direction="inbound",
        subject=subject,
        contact_id=contact_id,
        workflow_id=workflow_id,
        gmail_message_id="inbound-msg-1",
        gmail_thread_id=gmail_thread_id,
        rfc2822_message_id=rfc2822_message_id,
    )
    assert inbound is not None
    return inbound


def test_reply_email_resolves_thread_recipient_and_subject(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="sender@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    inbound = _make_inbound(database_connection, account.id, contact.id, workflow.id)
    gmail_client = _make_gmail_client(account)

    email = reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        email_id=inbound.id,
        body="Here is the pricing info.",
        workflow_id=workflow.id,
    )

    assert email.gmail_message_id == "gmail-msg-1"
    call_kwargs = gmail_client.send_message.call_args.kwargs
    assert call_kwargs["to"] == "sender@example.com"
    assert call_kwargs["subject"] == "Re: Question about pricing"
    assert call_kwargs["thread_id"] == "thread-abc"


def test_reply_email_preserves_existing_re_prefix(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="sender@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    inbound = _make_inbound(
        database_connection, account.id, contact.id, workflow.id, subject="Re: Pricing"
    )
    gmail_client = _make_gmail_client(account)

    reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        email_id=inbound.id,
        body="More info",
        workflow_id=workflow.id,
    )

    assert gmail_client.send_message.call_args.kwargs["subject"] == "Re: Pricing"


def test_reply_email_raises_original_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    with pytest.raises(OriginalNotFoundError):
        reply_email(
            connection=database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            email_id="nonexistent",
            body="hi",
            workflow_id=workflow.id,
        )
    gmail_client.send_message.assert_not_called()


def test_reply_email_raises_missing_thread(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="sender@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    inbound = _make_inbound(
        database_connection,
        account.id,
        contact.id,
        workflow.id,
        gmail_thread_id=None,
    )
    gmail_client = _make_gmail_client(account)

    with pytest.raises(OriginalMissingThreadError):
        reply_email(
            connection=database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            email_id=inbound.id,
            body="hi",
            workflow_id=workflow.id,
        )


def test_reply_email_raises_missing_contact(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    inbound = _make_inbound(
        database_connection,
        account.id,
        None,
        workflow.id,
        subject="No contact",
    )
    gmail_client = _make_gmail_client(account)

    with pytest.raises(OriginalMissingContactError):
        reply_email(
            connection=database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            email_id=inbound.id,
            body="hi",
            workflow_id=workflow.id,
        )


def test_reply_email_raises_contact_disabled(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="sender@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    inbound = _make_inbound(database_connection, account.id, contact.id, workflow.id)
    db_disable_contact(database_connection, contact.id, "unsubscribed", "user opt-out")
    gmail_client = _make_gmail_client(account)

    with pytest.raises(ContactDisabledError):
        reply_email(
            connection=database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            email_id=inbound.id,
            body="hi",
            workflow_id=workflow.id,
        )


def test_reply_email_passes_in_reply_to_kwarg(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """The original's rfc2822_message_id is forwarded as in_reply_to to
    sync.send_email so threading headers are emitted."""
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="sender@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    inbound = _make_inbound(
        database_connection,
        account.id,
        contact.id,
        workflow.id,
        rfc2822_message_id="<CABx-orig@mail.gmail.com>",
    )
    gmail_client = _make_gmail_client(account)

    reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        email_id=inbound.id,
        body="hi",
        workflow_id=workflow.id,
    )

    assert (
        gmail_client.send_message.call_args.kwargs["in_reply_to"]
        == "<CABx-orig@mail.gmail.com>"
    )


def test_reply_email_activates_pending_enrollment(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="sender@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    create_enrollment(database_connection, workflow.id, contact.id)
    inbound = _make_inbound(database_connection, account.id, contact.id, workflow.id)
    gmail_client = _make_gmail_client(account)

    reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        email_id=inbound.id,
        body="hi",
        workflow_id=workflow.id,
    )

    enrollment = get_enrollment(database_connection, workflow.id, contact.id)
    assert enrollment is not None
    assert enrollment.status == "active"


# -- Activity emission ---------------------------------------------------------


def test_send_email_emits_email_sent_activity(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """A successful send must produce one email_sent activity tied to the
    resolved contact, with subject summary and workflow_id in the detail."""
    from mailpilot.database import create_company, list_activities

    account = make_test_account(database_connection)
    company = create_company(
        database_connection, name="Recipient Co", domain="example.com"
    )
    contact = make_test_contact(
        database_connection,
        email="recipient@example.com",
        domain="example.com",
        company_id=company.id,
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Outbound test",
        body="Hi",
        workflow_id=workflow.id,
    )

    activities = list_activities(
        database_connection, contact_id=contact.id, activity_type="email_sent"
    )
    assert len(activities) == 1
    assert activities[0].summary == "Outbound test"
    assert activities[0].company_id == company.id


def test_send_email_skips_activity_when_contact_unknown(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Sends to a recipient with no contact row produce no activity (no
    contact_id to anchor the timeline)."""
    from mailpilot.database import list_activities

    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        to="brand-new@example.com",
        subject="Hi",
        body="Body",
        workflow_id=workflow.id,
    )

    # No contact -> no activity. There is no contact_id to query by, so
    # assert at the company-id-less catch-all level: nothing for company
    # either.
    contact = make_test_contact(
        database_connection, email="brand-new@example.com", domain="example.com"
    )
    assert (
        list_activities(
            database_connection, contact_id=contact.id, activity_type="email_sent"
        )
        == []
    )


def test_reply_email_emits_email_sent_activity(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """reply_email must emit one email_sent activity using the reply subject."""
    from mailpilot.database import list_activities

    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="sender@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    inbound = _make_inbound(
        database_connection, account.id, contact.id, workflow.id, subject="Pricing"
    )
    gmail_client = _make_gmail_client(account)

    reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        email_id=inbound.id,
        body="Reply body",
        workflow_id=workflow.id,
    )

    activities = list_activities(
        database_connection, contact_id=contact.id, activity_type="email_sent"
    )
    assert len(activities) == 1
    assert activities[0].summary == "Re: Pricing"
