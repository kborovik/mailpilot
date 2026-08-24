"""Email commands."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import click

from mailpilot._filters import (
    DIRECTIONS,
    enum_option,
    limit_option,
    scope_option,
    time_window_options,
)
from mailpilot.cli.main import (
    _db,
    _resolve_account,
    _resolve_contact,
    _resolve_workflow_id,
    main,
    output,
    output_entity,
    output_error,
)

_ROUTE_METHODS = [
    "classified",
    "thread_match",
    "rfc_message_id_match",
    "skipped_outside_window",
    "skipped_no_workflows",
    "skipped_predates_workflows",
    "skipped_no_inbound_workflows",
]

_EMAIL_STATUSES = ["sent", "received", "bounced"]

# -- Email commands ------------------------------------------------------------


@main.group()
def email() -> None:
    """Manage emails."""


@email.command("search")
@click.argument("query")
@limit_option
def email_search(query: str, limit: int) -> None:
    """Search emails by subject or body."""
    from mailpilot.database import search_emails

    with _db() as connection:
        emails = search_emails(connection, query, limit=limit)
        output({"emails": [e.model_dump(mode="json") for e in emails]})


@email.command("list")
@scope_option("--contact-email", "contact_email", "Filter by contact (email or ID).")
@scope_option("--account-email", "account_email", "Filter by account (email or ID).")
@scope_option("--workflow-id", "workflow_id", "Filter by workflow (name or ID).")
@scope_option("--thread-id", "thread_id", "Filter by Gmail thread ID.")
@enum_option("--direction", "direction", DIRECTIONS, "Filter by direction.")
@enum_option("--status", "status", _EMAIL_STATUSES, "Filter by email status.")
@enum_option(
    "--route-method",
    "route_method",
    _ROUTE_METHODS,
    "Filter by persisted routing decision.",
)
@click.option("--from", "sender", default=None, help="Filter by sender email address.")
@click.option(
    "--to", "recipient", default=None, help="Filter by recipient email address."
)
@time_window_options("COALESCE(sent_at, received_at)")
@limit_option
def email_list(
    limit: int,
    contact_email: str | None,
    account_email: str | None,
    since: str | None,
    until: str | None,
    thread_id: str | None,
    direction: str | None,
    workflow_id: str | None,
    status: str | None,
    sender: str | None,
    recipient: str | None,
    route_method: str | None,
) -> None:
    """List emails with optional filters (requires at least one scope filter)."""
    from mailpilot.database import (
        list_emails,
    )

    if (
        contact_email is None
        and account_email is None
        and workflow_id is None
        and thread_id is None
        and sender is None
        and recipient is None
        and direction is None
        and status is None
        and route_method is None
        and since is None
        and until is None
    ):
        output_error(
            "at least one scope or filter is required "
            "(--contact-email, --account-email, --workflow-id, --thread-id, "
            "--from, --to, --direction, --status, --route-method, or time window)",
            "missing_filter",
        )
    with _db() as connection:
        contact_id = (
            _resolve_contact(connection, contact_email).id
            if contact_email is not None
            else None
        )
        account_id = (
            _resolve_account(connection, account_email).id
            if account_email is not None
            else None
        )
        resolved_workflow_id: str | None = (
            _resolve_workflow_id(connection, workflow_id)
            if workflow_id is not None
            else None
        )
        emails = list_emails(
            connection,
            limit=limit,
            contact_id=contact_id,
            account_id=account_id,
            since=since,
            until=until,
            thread_id=thread_id,
            direction=direction,
            workflow_id=resolved_workflow_id,
            status=status,
            sender=sender,
            recipient=recipient,
            route_method=route_method,
        )
        output({"emails": [e.model_dump(mode="json") for e in emails]})


@email.command("view")
@click.argument("email_id")
def email_view(email_id: str) -> None:
    """View a single email by ID."""
    from mailpilot.database import get_email

    with _db() as connection:
        found = get_email(connection, email_id)
        if found is None:
            output_error(f"email not found: {email_id}", "not_found")
        output_entity("email", found)


@email.command("send")
@click.option(
    "--account-email",
    default=None,
    help="Sending account (email or ID).",
)
@click.option(
    "--to",
    "to",
    required=True,
    multiple=True,
    help="Recipient email address (repeatable).",
)
@click.option("--subject", required=True, help="Email subject.")
@click.option("--body", required=True, help="Plain text body.")
@click.option("--workflow-id", default=None, help="Link to a workflow (name or ID).")
@click.option("--cc", default=None, help="CC recipient(s), comma-separated.")
@click.option("--bcc", default=None, help="BCC recipient(s), comma-separated.")
def email_send(
    account_email: str | None,
    to: tuple[str, ...],
    subject: str,
    body: str,
    workflow_id: str | None,
    cc: str | None,
    bcc: str | None,
) -> None:
    """Send a new outbound email via the given account's Gmail mailbox.

    Use ``email reply`` to continue an existing thread.
    """
    import logfire

    from mailpilot import email_ops
    from mailpilot.gmail import GmailClient
    from mailpilot.settings import get_settings

    if not subject.strip():
        output_error("subject cannot be empty", "validation_error")
    if not body.strip():
        output_error("body cannot be empty", "validation_error")

    to_joined = ",".join(to)
    settings = get_settings()
    with _db(mutate=True) as connection:
        account = _resolve_account(connection, account_email)
        resolved_workflow_id = (
            _resolve_workflow_id(connection, workflow_id)
            if workflow_id is not None
            else None
        )
        client = GmailClient(account.email)
        try:
            sent = email_ops.send_email(
                connection,
                account=account,
                gmail_client=client,
                settings=settings,
                to=to_joined,
                subject=subject,
                body=body,
                workflow_id=resolved_workflow_id,
                cc=cc,
                bcc=bcc,
            )
        except email_ops.EmailOpsError as exc:
            output_error(str(exc), exc.code)
        except Exception as exc:
            logfire.exception(
                "cli.email.send.failed",
                account_id=account.id,
                to=to,
            )
            output_error(str(exc), "send_failed")
        output_entity("email", sent)


@email.command("reply")
@click.option(
    "--account-email",
    default=None,
    help="Sending account (email or ID).",
)
@click.option(
    "--email-id",
    required=True,
    help="ID of the email being replied to.",
)
@click.option("--body", required=True, help="Reply body (plain text).")
@click.option(
    "--subject",
    default=None,
    help="Override reply subject (default: Re: original).",
)
@click.option("--workflow-id", default=None, help="Link to a workflow (name or ID).")
@click.option("--cc", default=None, help="CC recipient(s), comma-separated.")
@click.option("--bcc", default=None, help="BCC recipient(s), comma-separated.")
def email_reply(
    account_email: str | None,
    email_id: str,
    body: str,
    subject: str | None,
    workflow_id: str | None,
    cc: str | None,
    bcc: str | None,
) -> None:
    """Reply to an existing email in-thread.

    Auto-derives recipient, subject (with "Re: " prefix), thread, and
    In-Reply-To from the original. ``--subject`` overrides the subject.
    No cooldown applied.
    """
    import logfire

    from mailpilot import email_ops
    from mailpilot.gmail import GmailClient
    from mailpilot.settings import get_settings

    if not body.strip():
        output_error("body cannot be empty", "validation_error")

    settings = get_settings()
    with _db(mutate=True) as connection:
        account = _resolve_account(connection, account_email)
        resolved_workflow_id = (
            _resolve_workflow_id(connection, workflow_id)
            if workflow_id is not None
            else None
        )
        client = GmailClient(account.email)
        try:
            sent = email_ops.reply_email(
                connection,
                account=account,
                gmail_client=client,
                settings=settings,
                email_id=email_id,
                body=body,
                workflow_id=resolved_workflow_id,
                cc=cc,
                bcc=bcc,
                subject=subject,
            )
        except email_ops.EmailOpsError as exc:
            output_error(str(exc), exc.code)
        except Exception as exc:
            logfire.exception(
                "cli.email.reply.failed",
                account_id=account.id,
                email_id=email_id,
            )
            output_error(str(exc), "send_failed")
        output_entity("email", sent)
