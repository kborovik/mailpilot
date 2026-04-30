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
    - ``record_enrollment_outcome`` -- record per-workflow outcome on timeline
    - ``disable_contact`` -- set global contact block (bounced/unsubscribed)
    - ``search_emails`` -- query email history
    - ``list_enrollments`` -- list enrollments in workflow with status
    - ``read_contact`` -- CRM contact lookup
    - ``read_company`` -- CRM company lookup
    - ``read_email`` -- full email content lookup
    - ``list_drive_markdown`` -- list Markdown files in a Drive folder
    - ``read_drive_markdown`` -- read a Markdown file from Drive
"""

from __future__ import annotations

from typing import Any

import psycopg

from mailpilot import database, email_ops
from mailpilot.drive import DriveClient
from mailpilot.models import Account
from mailpilot.settings import Settings

_VALID_DISABLE_STATUSES = ("bounced", "unsubscribed")


def send_email(  # noqa: PLR0913
    connection: psycopg.Connection[dict[str, Any]],
    account: Account,
    gmail_client: object,
    settings: Settings,
    workflow_id: str,
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
) -> dict[str, Any]:
    """Agent tool: send a new outbound email via Gmail.

    Thin wrapper over :func:`mailpilot.email_ops.send_email`. Converts
    typed policy exceptions into the LLM-facing error dict shape.
    """
    try:
        email = email_ops.send_email(
            connection,
            account,
            gmail_client,  # type: ignore[arg-type]
            settings,
            to=to,
            subject=subject,
            body=body,
            workflow_id=workflow_id,
            cc=cc,
            bcc=bcc,
        )
    except email_ops.EmailOpsError as exc:
        return {"error": exc.code, "message": str(exc)}

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
    """Agent tool: reply in-thread. Wraps :func:`email_ops.reply_email`.

    Converts typed policy exceptions into the LLM-facing error dict.
    """
    try:
        email = email_ops.reply_email(
            connection,
            account,
            gmail_client,  # type: ignore[arg-type]
            settings,
            email_id=email_id,
            body=body,
            workflow_id=workflow_id,
            cc=cc,
            bcc=bcc,
        )
    except email_ops.EmailOpsError as exc:
        return {"error": exc.code, "message": str(exc)}

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
    task = database.cancel_task(connection, task_id)
    if task is None:
        return {
            "error": "not_found",
            "message": f"task not found or not pending: {task_id}",
        }
    return {"id": task.id, "status": task.status}


def record_enrollment_outcome(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
    outcome: str,
    reason: str,
) -> dict[str, str]:
    """Record an outcome (completed or failed) on the activity timeline.

    Outcome is purely a timeline event -- the enrollment row's status is
    not modified. The agent declares the engagement done; if a later
    inbound reply arrives, the agent can react without first
    "reactivating" anything.

    Args:
        connection: Open database connection.
        workflow_id: Current workflow FK.
        contact_id: Contact ID.
        outcome: "completed" or "failed".
        reason: Agent's explanation (e.g., "meeting booked", "no response").

    Returns:
        Dict with the recorded outcome, or an error dict if the
        enrollment is missing or the outcome is invalid.
    """
    valid_outcomes = ("completed", "failed")
    if outcome not in valid_outcomes:
        return {
            "error": "invalid_outcome",
            "message": f"outcome must be one of {valid_outcomes}, got: {outcome}",
        }
    enrollment = database.get_enrollment(connection, workflow_id, contact_id)
    if enrollment is None:
        return {
            "error": "not_found",
            "message": f"enrollment not found: {workflow_id}/{contact_id}",
        }
    contact = database.get_contact(connection, contact_id)
    database.create_activity(
        connection,
        contact_id=contact_id,
        activity_type=f"enrollment_{outcome}",
        summary=reason or f"Enrollment {outcome}",
        detail={"reason": reason},
        company_id=contact.company_id if contact is not None else None,
        workflow_id=workflow_id,
    )
    return {"outcome": outcome, "reason": reason}


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
    if status not in _VALID_DISABLE_STATUSES:
        return {
            "error": "invalid_status",
            "message": (
                f"status must be one of {_VALID_DISABLE_STATUSES}, got: {status!r}"
            ),
        }
    updated = database.disable_contact(
        connection, contact_id, status=status, status_reason=reason
    )
    if updated is None:
        return {"error": "not_found", "message": f"contact not found: {contact_id}"}
    return {"id": updated.id, "status": updated.status}


def list_enrollments(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> list[dict[str, Any]]:
    """List enrollments in a workflow with their latest outcome.

    Lets the agent coordinate across contacts (e.g., skip person B if
    person A at the same company already completed the objective). Each
    row includes ``latest_outcome`` (``completed`` / ``failed`` / ``None``),
    ``latest_outcome_reason``, and ``latest_outcome_at`` -- pulled from the
    activity timeline since outcomes are timeline-only per ADR-08.

    Args:
        connection: Open database connection.
        workflow_id: Workflow ID.

    Returns:
        List of enrollment records with operational status and the latest
        outcome activity, if any.
    """
    enrollments = database.list_enrollments_with_outcomes(connection, workflow_id)
    return [e.model_dump(mode="json") for e in enrollments]


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
    company = database.get_company_by_domain(connection, domain)
    if company is None:
        return None
    return company.model_dump()


def read_email(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    email_id: str,
) -> dict[str, Any] | None:
    """Read a specific email by ID to view its full content, including body text.

    Args:
        connection: Open database connection.
        account_id: Account the agent is scoped to. Emails belonging to other
            accounts are not visible (returns None) -- prevents cross-tenant
            data leaks via prompt injection in inbound message bodies.
        email_id: The ID of the email to read.

    Returns:
        Full email details including body text, or None if not found or the
        email belongs to a different account.
    """
    email = database.get_email(connection, email_id)
    if email is None or email.account_id != account_id:
        return None
    return email.model_dump()


def list_drive_markdown(
    drive_client: DriveClient,
    folder_id: str,
) -> list[dict[str, str]] | dict[str, str]:
    """List Markdown files in a Drive folder for KB grounding.

    Args:
        drive_client: Drive client scoped to the current account.
        folder_id: Drive folder ID supplied via the workflow instructions.

    Returns:
        List of ``{"file_id": ..., "name": ...}`` on success, or an error
        dict ``{"error": ..., "message": ...}`` on Drive failure.
    """
    from googleapiclient.errors import HttpError

    try:
        return drive_client.list_markdown(folder_id)
    except HttpError as exc:
        if exc.resp.status == 404:
            return {
                "error": "not_found",
                "message": f"drive folder not found: {folder_id}",
            }
        return {
            "error": "drive_unavailable",
            "message": str(exc),
        }


def read_drive_markdown(
    drive_client: DriveClient,
    file_id: str,
) -> dict[str, str]:
    """Read a Markdown file from Drive.

    Args:
        drive_client: Drive client scoped to the current account.
        file_id: Drive file ID, typically returned by ``list_drive_markdown``.

    Returns:
        ``{"name": ..., "content": ..., "web_view_link": ...}`` on success,
        or ``{"error": ..., "message": ...}`` on Drive failure.
    """
    from googleapiclient.errors import HttpError

    try:
        return drive_client.read_markdown(file_id)
    except HttpError as exc:
        if exc.resp.status == 404:
            return {
                "error": "not_found",
                "message": f"drive file not found: {file_id}",
            }
        return {
            "error": "drive_unavailable",
            "message": str(exc),
        }


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
    return {"acknowledged": True, "reason": reason}
