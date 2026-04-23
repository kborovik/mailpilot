"""Agent tools for workflow execution.

Each function is a Pydantic AI tool the agent can call. Tools are defined
as standalone functions (not methods) so they can be unit-tested without
spinning up a full agent.

Dependency injection: each tool receives explicit dependency parameters
(``connection``, ``account``, ``workflow_id``, etc.) that issue #12 will
wire from ``RunContext[AgentDeps]``.

Tools per ADR-03:
    - ``send_email`` -- send via Gmail API with contact status + cooldown guards
    - ``reply_email`` -- reply in-thread with auto-resolved recipient and subject
    - ``create_task`` -- schedule deferred work
    - ``cancel_task`` -- cancel a pending task
    - ``update_contact_status`` -- report per-workflow outcome
    - ``disable_contact`` -- set global contact block (bounced/unsubscribed)
    - ``search_emails`` -- query email history
    - ``list_workflow_contacts`` -- list contacts in workflow with status
    - ``read_contact`` -- CRM contact lookup
    - ``read_company`` -- CRM company lookup
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import logfire
import psycopg

from mailpilot import database
from mailpilot.models import Account
from mailpilot.settings import Settings
from mailpilot.sync import send_email as sync_send_email

_COOLDOWN_DAYS = 30


def send_email(  # noqa: PLR0913
    connection: psycopg.Connection[dict[str, Any]],
    account: Account,
    gmail_client: object,
    settings: Settings,
    workflow_id: str,
    to: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
    cc: str | None = None,
    bcc: str | None = None,
) -> dict[str, Any]:
    """Send an email via Gmail API.

    Guards:
    1. Contact must be active (not bounced/unsubscribed) -- hard block
    2. Cooldown on unsolicited outreach only:
       - Reply (thread_id provided): always allowed
       - New conversation (no thread_id): blocked if last unsolicited outbound
         to this contact from this account was within cooldown period (30 days)

    Args:
        connection: Open database connection.
        account: Sending account.
        gmail_client: Gmail client scoped to account.
        settings: Application settings.
        workflow_id: Current workflow FK.
        to: Recipient email address.
        subject: Email subject.
        body: Email body (plain text).
        thread_id: Gmail thread ID for threading replies.
        cc: CC recipient(s), comma-separated.
        bcc: BCC recipient(s), comma-separated.

    Returns:
        Dict with sent message details (id, gmail_message_id, gmail_thread_id),
        or error dict if blocked by guard.
    """
    with logfire.span("agent.tool.send_email", to=to, workflow_id=workflow_id):
        # Guard 1: contact status check.
        contact = database.get_contact_by_email(connection, to)
        contact_id: str | None = None
        if contact is not None:
            contact_id = contact.id
            if contact.status != "active":
                return {
                    "error": "contact_disabled",
                    "message": f"contact is {contact.status}: {contact.status_reason}",
                }

            # Guard 2: cooldown (new conversations only).
            if thread_id is None:
                last = database.get_last_cold_outbound(
                    connection, account.id, contact.id, workflow_id
                )
                if last is not None and last.created_at > datetime.now(UTC) - timedelta(
                    days=_COOLDOWN_DAYS
                ):
                    sent_at = last.created_at.isoformat()
                    return {
                        "error": "cooldown",
                        "message": (
                            f"last unsolicited email sent {sent_at}; "
                            f"cooldown is {_COOLDOWN_DAYS} days"
                        ),
                    }

        email = sync_send_email(
            connection=connection,
            account=account,
            gmail_client=gmail_client,  # type: ignore[arg-type]
            settings=settings,
            to=to,
            subject=subject,
            body=body,
            contact_id=contact_id,
            workflow_id=workflow_id,
            thread_id=thread_id,
            cc=cc,
            bcc=bcc,
        )
        return {
            "id": email.id,
            "gmail_message_id": email.gmail_message_id,
            "gmail_thread_id": email.gmail_thread_id,
        }


def reply_email(  # noqa: PLR0913
    connection: psycopg.Connection[dict[str, Any]],
    account: Account,
    gmail_client: object,
    settings: Settings,
    workflow_id: str,
    email_id: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
) -> dict[str, Any]:
    """Reply to an existing email in-thread.

    Resolves recipient, subject, and thread_id automatically from the
    original email. No cooldown check -- replies are always allowed.

    Guards:
    1. Original email must exist
    2. Original email must have a gmail_thread_id
    3. Original email must have a contact_id
    4. Contact must exist
    5. Contact must be active (not bounced/unsubscribed)

    Args:
        connection: Open database connection.
        account: Sending account.
        gmail_client: Gmail client scoped to account.
        settings: Application settings.
        workflow_id: Current workflow FK.
        email_id: ID of the email being replied to.
        body: Reply body (plain text).
        cc: CC recipient(s), comma-separated.
        bcc: BCC recipient(s), comma-separated.

    Returns:
        Dict with sent message details (id, gmail_message_id, gmail_thread_id),
        or error dict if blocked by guard.
    """
    with logfire.span(
        "agent.tool.reply_email", email_id=email_id, workflow_id=workflow_id
    ):
        # Guard 1: original email must exist.
        original = database.get_email(connection, email_id)
        if original is None:
            return {
                "error": "not_found",
                "message": f"email not found: {email_id}",
            }

        # Guard 2: original email must have a gmail_thread_id.
        if original.gmail_thread_id is None:
            return {
                "error": "no_thread",
                "message": f"email has no gmail_thread_id: {email_id}",
            }

        # Guard 3: original email must have a contact.
        if original.contact_id is None:
            return {
                "error": "no_contact",
                "message": f"email has no contact_id: {email_id}",
            }

        # Guard 4: contact must exist.
        contact = database.get_contact(connection, original.contact_id)
        if contact is None:
            return {
                "error": "not_found",
                "message": f"contact not found: {original.contact_id}",
            }

        # Guard 5: contact must be active.
        if contact.status != "active":
            return {
                "error": "contact_disabled",
                "message": f"contact is {contact.status}: {contact.status_reason}",
            }

        # Derive subject: prepend "Re: " unless already prefixed.
        subject = original.subject
        if not subject.lower().startswith("re: "):
            subject = f"Re: {subject}"

        email = sync_send_email(
            connection=connection,
            account=account,
            gmail_client=gmail_client,  # type: ignore[arg-type]
            settings=settings,
            to=contact.email,
            subject=subject,
            body=body,
            contact_id=contact.id,
            workflow_id=workflow_id,
            thread_id=original.gmail_thread_id,
            cc=cc,
            bcc=bcc,
        )
        return {
            "id": email.id,
            "gmail_message_id": email.gmail_message_id,
            "gmail_thread_id": email.gmail_thread_id,
        }


def create_task(  # noqa: PLR0913
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
    description: str,
    scheduled_at: str,
    context: dict[str, Any] | None = None,
    email_id: str | None = None,
) -> dict[str, str]:
    """Schedule deferred work for later execution.

    Args:
        connection: Open database connection.
        workflow_id: Current workflow FK.
        contact_id: Contact this task targets (required).
        description: What the agent should do when the task runs.
        scheduled_at: When to execute (ISO 8601 timestamp).
        context: Arbitrary JSON context for the agent on re-invocation.
        email_id: Optional triggering email for focused context.

    Returns:
        Dict with created task ID.
    """
    with logfire.span(
        "agent.tool.create_task", workflow_id=workflow_id, contact_id=contact_id
    ):
        task = database.create_task(
            connection,
            workflow_id=workflow_id,
            contact_id=contact_id,
            description=description,
            scheduled_at=scheduled_at,
            context=context,
            email_id=email_id,
        )
        return {"id": task.id}


def cancel_task(
    connection: psycopg.Connection[dict[str, Any]],
    task_id: str,
) -> dict[str, str]:
    """Cancel a pending task.

    Use when a previously scheduled follow-up is no longer needed (e.g.,
    the contact replied before the follow-up was due).

    Args:
        connection: Open database connection.
        task_id: Task ID to cancel.

    Returns:
        Dict with cancelled task ID and status, or error if not found/not pending.
    """
    with logfire.span("agent.tool.cancel_task", task_id=task_id):
        task = database.cancel_task(connection, task_id)
        if task is None:
            return {
                "error": "not_found",
                "message": f"task not found or not pending: {task_id}",
            }
        return {"id": task.id, "status": task.status}


def update_contact_status(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
    status: str,
    reason: str,
) -> dict[str, str]:
    """Report outcome for a contact in the current workflow.

    The agent -- not the system -- decides success or failure.

    Args:
        connection: Open database connection.
        workflow_id: Current workflow FK.
        contact_id: Contact ID.
        status: "active", "completed", or "failed".
        reason: Agent's explanation (e.g., "meeting booked", "no response").

    Returns:
        Dict with updated status, or error if workflow_contact not found.
    """
    valid_statuses = ("active", "completed", "failed")
    with logfire.span(
        "agent.tool.update_contact_status",
        workflow_id=workflow_id,
        contact_id=contact_id,
        status=status,
    ):
        if status not in valid_statuses:
            return {
                "error": "invalid_status",
                "message": f"status must be one of {valid_statuses}, got: {status}",
            }
        wc = database.update_workflow_contact(
            connection, workflow_id, contact_id, status=status, reason=reason
        )
        if wc is None:
            return {
                "error": "not_found",
                "message": f"workflow_contact not found: {workflow_id}/{contact_id}",
            }
        return {"status": wc.status, "reason": wc.reason}


def disable_contact(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
    status: str,
    reason: str,
) -> dict[str, str]:
    """Set a global block on a contact (bounced or unsubscribed).

    This is a hard block across all workflows. The send_email tool checks
    contact status before sending.

    Args:
        connection: Open database connection.
        contact_id: Contact ID.
        status: "bounced" or "unsubscribed".
        reason: Explanation (e.g., "hard bounce", "replied: do not contact").

    Returns:
        Dict with updated contact status, or error if not found.
    """
    with logfire.span(
        "agent.tool.disable_contact", contact_id=contact_id, status=status
    ):
        updated = database.disable_contact(
            connection, contact_id, status=status, status_reason=reason
        )
        if updated is None:
            return {"error": "not_found", "message": f"contact not found: {contact_id}"}
        return {"id": updated.id, "status": updated.status}


def list_workflow_contacts(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> list[dict[str, Any]]:
    """List contacts in a workflow with their outcome status.

    Lets the agent coordinate across contacts (e.g., skip person B if
    person A at the same company already completed the objective).

    Args:
        connection: Open database connection.
        workflow_id: Workflow ID.

    Returns:
        List of workflow-contact records with status and reason.
    """
    with logfire.span("agent.tool.list_workflow_contacts", workflow_id=workflow_id):
        contacts = database.list_workflow_contacts(connection, workflow_id)
        return [wc.model_dump() for wc in contacts]


def search_emails(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    query: str,
) -> list[dict[str, Any]]:
    """Search email history for the current account.

    Args:
        connection: Open database connection.
        account_id: Account to scope search to.
        query: Search term matched against subject and body.

    Returns:
        List of matching email summaries.
    """
    with logfire.span("agent.tool.search_emails", account_id=account_id, query=query):
        emails = database.search_emails(connection, query, account_id=account_id)
        return [e.model_dump() for e in emails]


def read_contact(
    connection: psycopg.Connection[dict[str, Any]],
    email: str,
) -> dict[str, Any] | None:
    """Look up a contact by email address.

    Args:
        connection: Open database connection.
        email: Contact email address.

    Returns:
        Contact details or None if not found.
    """
    with logfire.span("agent.tool.read_contact", email=email):
        contact = database.get_contact_by_email(connection, email)
        if contact is None:
            return None
        return contact.model_dump()


def read_company(
    connection: psycopg.Connection[dict[str, Any]],
    domain: str,
) -> dict[str, Any] | None:
    """Look up a company by domain.

    Args:
        connection: Open database connection.
        domain: Company primary domain.

    Returns:
        Company details or None if not found.
    """
    with logfire.span("agent.tool.read_company", domain=domain):
        company = database.get_company_by_domain(connection, domain)
        if company is None:
            return None
        return company.model_dump()


def noop(reason: str) -> dict[str, Any]:
    """Explicitly decline to act.

    Call this tool when, after reviewing context, no action is appropriate.
    You must still call a tool every turn -- noop is the explicit "do nothing"
    signal.

    Args:
        reason: Why no action is needed.

    Returns:
        Acknowledgement dict.
    """
    with logfire.span("agent.tool.noop", reason=reason):
        return {"acknowledged": True, "reason": reason}
