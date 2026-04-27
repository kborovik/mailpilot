"""Policy layer for outbound email operations.

This module is the single source of truth for the rules that govern
sending and replying. Both ``cli.py`` and ``agent/tools.py`` call into
``send_email`` / ``reply_email`` here, so guards (contact-status,
cooldown, reply preconditions) and side effects (enrollment activation)
live in one place.

Failures raise typed ``EmailOpsError`` subclasses. Callers convert them
to their native error shapes -- a dict for the agent, ``output_error``
JSON for the CLI.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from mailpilot import database
from mailpilot.gmail import GmailClient
from mailpilot.models import Account, Email
from mailpilot.settings import Settings
from mailpilot.sync import send_email as sync_send_email

_COOLDOWN_DAYS = 30


class EmailOpsError(Exception):
    """Base class for email policy violations.

    Subclasses set a class-level ``code`` matching the legacy agent-tool
    error string, so the LLM-facing contract is unchanged.
    """

    code: str = "email_ops_error"


class ContactDisabledError(EmailOpsError):
    """Recipient contact is bounced or unsubscribed; send blocked."""

    code = "contact_disabled"


class CooldownError(EmailOpsError):
    """Prior unsolicited cold outbound is within the cooldown window."""

    code = "cooldown"


class OriginalNotFoundError(EmailOpsError):
    """Reply target email_id does not resolve to a row."""

    code = "not_found"


class OriginalMissingThreadError(EmailOpsError):
    """Reply target has no gmail_thread_id, so no thread to reply into."""

    code = "no_thread"


class OriginalMissingContactError(EmailOpsError):
    """Reply target has no contact_id, so no recipient can be derived."""

    code = "no_contact"


class ContactMissingError(EmailOpsError):
    """Reply target's contact_id no longer resolves to a contact row."""

    code = "not_found"


def _activate_enrollment_if_pending(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> None:
    enrollment = database.get_enrollment(connection, workflow_id, contact_id)
    if enrollment is not None and enrollment.status == "pending":
        database.update_enrollment(
            connection,
            workflow_id,
            contact_id,
            status="active",
            reason="email sent",
        )


def send_email(  # noqa: PLR0913
    connection: psycopg.Connection[dict[str, Any]],
    account: Account,
    gmail_client: GmailClient,
    settings: Settings,
    *,
    to: str,
    subject: str,
    body: str,
    workflow_id: str | None = None,
    cc: str | None = None,
    bcc: str | None = None,
) -> Email:
    """Send a new outbound email. Applies all policy guards.

    Auto-resolves contact_id from ``to``. If the contact exists, applies
    contact-status and 30-day cold-outbound cooldown guards. The cooldown
    query is workflow-scoped, so it runs only when ``workflow_id`` is set
    (ad-hoc CLI sends without a workflow have no cooldown context).
    Activates a pending enrollment when both ``workflow_id`` and a
    resolved contact are present.

    Raises:
        ContactDisabledError: contact is bounced/unsubscribed.
        CooldownError: prior unsolicited send within 30 days.
    """
    contact = database.get_contact_by_email(connection, to)
    contact_id: str | None = None
    if contact is not None:
        contact_id = contact.id
        if contact.status != "active":
            raise ContactDisabledError(
                f"contact is {contact.status}: {contact.status_reason}"
            )
        if workflow_id is not None:
            last = database.get_last_cold_outbound(
                connection, account.id, contact.id, workflow_id
            )
            if last is not None and last.created_at > datetime.now(UTC) - timedelta(
                days=_COOLDOWN_DAYS
            ):
                raise CooldownError(
                    f"last unsolicited email sent {last.created_at.isoformat()}; "
                    f"cooldown is {_COOLDOWN_DAYS} days"
                )

    email = sync_send_email(
        connection=connection,
        account=account,
        gmail_client=gmail_client,
        settings=settings,
        to=to,
        subject=subject,
        body=body,
        contact_id=contact_id,
        workflow_id=workflow_id,
        cc=cc,
        bcc=bcc,
    )

    if workflow_id is not None and contact_id is not None:
        _activate_enrollment_if_pending(connection, workflow_id, contact_id)

    return email


def reply_email(  # noqa: PLR0913
    connection: psycopg.Connection[dict[str, Any]],
    account: Account,
    gmail_client: GmailClient,
    settings: Settings,
    *,
    email_id: str,
    body: str,
    workflow_id: str | None = None,
    cc: str | None = None,
    bcc: str | None = None,
) -> Email:
    """Reply to an existing email in-thread.

    Auto-derives recipient (contact.email), subject ("Re: " prefixed
    unless already prefixed), thread_id, and In-Reply-To from the
    original. No cooldown -- replies are always allowed. Activates a
    matching pending enrollment when ``workflow_id`` is set.

    Raises:
        OriginalNotFoundError: ``email_id`` does not exist.
        OriginalMissingThreadError: original has no ``gmail_thread_id``.
        OriginalMissingContactError: original has no ``contact_id``.
        ContactMissingError: ``original.contact_id`` does not resolve.
        ContactDisabledError: contact is bounced/unsubscribed.
    """
    original = database.get_email(connection, email_id)
    if original is None:
        raise OriginalNotFoundError(f"email not found: {email_id}")
    if original.gmail_thread_id is None:
        raise OriginalMissingThreadError(f"email has no gmail_thread_id: {email_id}")
    if original.contact_id is None:
        raise OriginalMissingContactError(f"email has no contact_id: {email_id}")

    contact = database.get_contact(connection, original.contact_id)
    if contact is None:
        raise ContactMissingError(f"contact not found: {original.contact_id}")
    if contact.status != "active":
        raise ContactDisabledError(
            f"contact is {contact.status}: {contact.status_reason}"
        )

    subject = original.subject
    if not subject.lower().startswith("re: "):
        subject = f"Re: {subject}"

    email = sync_send_email(
        connection=connection,
        account=account,
        gmail_client=gmail_client,
        settings=settings,
        to=contact.email,
        subject=subject,
        body=body,
        contact_id=contact.id,
        workflow_id=workflow_id,
        thread_id=original.gmail_thread_id,
        cc=cc,
        bcc=bcc,
        in_reply_to=original.rfc2822_message_id,
    )

    if workflow_id is not None:
        _activate_enrollment_if_pending(connection, workflow_id, contact.id)

    return email
