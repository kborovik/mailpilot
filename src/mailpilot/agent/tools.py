"""Agent tools for workflow execution.

Each function is a Pydantic AI tool the agent can call. Tools are defined
as standalone functions (not methods) so they can be unit-tested without
spinning up a full agent.

Tools per ADR-03:
    - ``send_email`` -- send via Gmail API with contact status + cooldown guards
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

from typing import Any


def send_email(
    to: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Send an email via Gmail API.

    Guards:
    1. Contact must be active (not bounced/unsubscribed) -- hard block
    2. Cooldown on unsolicited outreach only:
       - Reply (thread_id provided): always allowed
       - New conversation (no thread_id): blocked if last unsolicited outbound
         to this contact from this account was within cooldown period (30 days)

    Args:
        to: Recipient email address.
        subject: Email subject.
        body: Email body (plain text).
        thread_id: Gmail thread ID for threading replies.

    Returns:
        Dict with sent message details (id, threadId).
    """
    raise NotImplementedError


def create_task(
    contact_id: str,
    description: str,
    scheduled_at: str,
    context: dict[str, Any] | None = None,
    email_id: str | None = None,
) -> dict[str, str]:
    """Schedule deferred work for later execution.

    Args:
        contact_id: Contact this task targets (required).
        description: What the agent should do when the task runs.
        scheduled_at: When to execute (ISO 8601 timestamp).
        context: Arbitrary JSON context for the agent on re-invocation.
        email_id: Optional triggering email for focused context.

    Returns:
        Dict with created task ID.
    """
    raise NotImplementedError


def update_contact_status(
    contact_id: str,
    status: str,
    reason: str,
) -> dict[str, str]:
    """Report outcome for a contact in the current workflow.

    The agent -- not the system -- decides success or failure.

    Args:
        contact_id: Contact ID.
        status: "active", "completed", or "failed".
        reason: Agent's explanation (e.g., "meeting booked", "no response").

    Returns:
        Dict with updated status.
    """
    raise NotImplementedError


def cancel_task(task_id: str) -> dict[str, str]:
    """Cancel a pending task.

    Use when a previously scheduled follow-up is no longer needed (e.g.,
    the contact replied before the follow-up was due).

    Args:
        task_id: Task ID to cancel.

    Returns:
        Dict with cancelled task ID and status.
    """
    raise NotImplementedError


def disable_contact(
    contact_id: str,
    status: str,
    reason: str,
) -> dict[str, str]:
    """Set a global block on a contact (bounced or unsubscribed).

    This is a hard block across all workflows. The send_email tool checks
    contact status before sending.

    Args:
        contact_id: Contact ID.
        status: "bounced" or "unsubscribed".
        reason: Explanation (e.g., "hard bounce", "replied: do not contact").

    Returns:
        Dict with updated contact status.
    """
    raise NotImplementedError


def list_workflow_contacts(workflow_id: str) -> list[dict[str, Any]]:
    """List contacts in a workflow with their outcome status.

    Lets the agent coordinate across contacts (e.g., skip person B if
    person A at the same company already completed the objective).

    Args:
        workflow_id: Workflow ID.

    Returns:
        List of workflow-contact records with status and reason.
    """
    raise NotImplementedError


def search_emails(query: str) -> list[dict[str, Any]]:
    """Search email history for the current account and contact.

    Args:
        query: Search term matched against subject and body.

    Returns:
        List of matching email summaries.
    """
    raise NotImplementedError


def read_contact(email: str) -> dict[str, Any] | None:
    """Look up a contact by email address.

    Args:
        email: Contact email address.

    Returns:
        Contact details or None if not found.
    """
    raise NotImplementedError


def read_company(domain: str) -> dict[str, Any] | None:
    """Look up a company by domain.

    Args:
        domain: Company primary domain.

    Returns:
        Company details or None if not found.
    """
    raise NotImplementedError
