"""Agent tools for workflow execution.

Each function is a Pydantic AI tool the agent can call. Tools are defined
as standalone functions (not methods) so they can be unit-tested without
spinning up a full agent.

Tools per ADR-03:
    - ``send_email`` -- send via Gmail API with cooldown guard
    - ``create_task`` -- schedule deferred work
    - ``update_contact_status`` -- report outcome (active, completed, failed)
    - ``search_emails`` -- query email history
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

    Cooldown guard on unsolicited outreach only:
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
    description: str,
    scheduled_at: str,
    context: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Schedule deferred work for later execution.

    Args:
        description: What the agent should do when the task runs.
        scheduled_at: When to execute (ISO 8601 timestamp).
        context: Arbitrary JSON context for the agent on re-invocation.

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
