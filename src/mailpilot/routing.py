"""Email routing pipeline (ADR-04).

Three-step pipeline that assigns inbound emails to the correct workflow:

1. **Thread match** -- prior email in same Gmail thread has a workflow_id
2. **LLM classification** -- single-turn call against active inbound workflows
3. **Unrouted** -- store with is_routed=True, workflow_id=NULL

Also handles bounce detection (mailer-daemon/postmaster senders and
bounce-related Gmail labels), the self-sender guard that breaks
agent-to-agent reply loops between managed accounts (#83), and creates
``workflow_contact`` entries on successful routing.
"""

from __future__ import annotations

from typing import Any

import logfire
import psycopg

from mailpilot.agent.classify import classify_email
from mailpilot.database import (
    create_workflow_contact,
    disable_contact,
    get_account_by_email,
    get_emails_by_gmail_thread_id,
    list_workflows,
    update_email,
)
from mailpilot.models import Email
from mailpilot.settings import Settings

_BOUNCE_SENDERS = frozenset({"mailer-daemon", "postmaster"})


def route_email(
    connection: psycopg.Connection[dict[str, Any]],
    email: Email,
    sender_email: str,
    settings: Settings,
) -> Email:
    """Route an inbound email through the ADR-04 pipeline.

    Runs bounce detection, then the three-step routing pipeline
    (thread match -> LLM classification -> unrouted). Creates a
    ``workflow_contact`` entry when routing to a workflow.

    Idempotent: emails with ``is_routed=True`` are returned unchanged.

    Args:
        connection: Open database connection.
        email: Newly stored inbound email to route.
        sender_email: Sender email address (parsed from From header).
        settings: Application settings (for LLM classification).

    Returns:
        Updated email with routing decision applied.
    """
    with logfire.span(
        "routing.route_email",
        email_id=email.id,
        account_id=email.account_id,
    ) as span:
        try:
            if email.is_routed:
                span.set_attribute("result", "skipped_already_routed")
                return email

            if _is_bounce(sender_email, email.labels):
                span.set_attribute("result", "bounce")
                return _handle_bounce(connection, email)

            if _is_self_sender(connection, sender_email):
                span.set_attribute("result", "self_sender")
                span.set_attribute("route_method", "self_sender")
                updated = update_email(connection, email.id, is_routed=True)
                return updated if updated is not None else email

            workflow_id = _try_thread_match(connection, email)
            if workflow_id is not None:
                span.set_attribute("result", "thread_match")
                span.set_attribute("route_method", "thread_match")
                span.set_attribute("workflow_id", workflow_id)
            else:
                workflow_id = _try_classify(connection, email, sender_email, settings)
                if workflow_id is not None:
                    span.set_attribute("result", "classified")
                    span.set_attribute("route_method", "classified")
                    span.set_attribute("workflow_id", workflow_id)
                else:
                    span.set_attribute("result", "unrouted")
                    span.set_attribute("route_method", "unrouted")

            updated = update_email(
                connection, email.id, workflow_id=workflow_id, is_routed=True
            )
            result = updated if updated is not None else email

            if workflow_id is not None and result.contact_id is not None:
                _ensure_workflow_contact(connection, workflow_id, result.contact_id)

            return result
        except Exception:
            span.set_attribute("result", "failure")
            logfire.exception("routing.route_email failed", email_id=email.id)
            raise


# -- Bounce detection ----------------------------------------------------------


def _is_bounce(sender_email: str, labels: list[str]) -> bool:
    """Check if the email is a bounce notification.

    Detects bounces via two signals:
    - Sender local part is ``mailer-daemon`` or ``postmaster`` (case-insensitive)
    - Any Gmail label contains ``BOUNCE`` (case-insensitive substring)
    """
    local_part = sender_email.split("@", maxsplit=1)[0].lower() if sender_email else ""
    if local_part in _BOUNCE_SENDERS:
        return True
    return any("BOUNCE" in label.upper() for label in labels)


def _handle_bounce(
    connection: psycopg.Connection[dict[str, Any]],
    email: Email,
) -> Email:
    """Process a bounce notification.

    Finds the original outbound email in the same thread, marks it as
    bounced, and disables the original recipient contact. The bounce
    notification itself is marked as routed.
    """
    with logfire.span(
        "routing.handle_bounce",
        email_id=email.id,
        gmail_thread_id=email.gmail_thread_id,
    ):
        if email.gmail_thread_id:
            thread_emails = get_emails_by_gmail_thread_id(
                connection, email.gmail_thread_id
            )
            outbound = [
                e
                for e in thread_emails
                if e.id != email.id
                and e.account_id == email.account_id
                and e.direction == "outbound"
            ]
            if outbound:
                outbound.sort(key=lambda e: e.created_at, reverse=True)
                original = outbound[0]
                update_email(connection, original.id, status="bounced")
                if original.contact_id is not None:
                    disable_contact(
                        connection,
                        original.contact_id,
                        status="bounced",
                        status_reason=f"Bounce detected on email {original.id}",
                    )
            else:
                logfire.warn(
                    "routing.bounce.no_outbound_in_thread",
                    email_id=email.id,
                    gmail_thread_id=email.gmail_thread_id,
                )
        else:
            logfire.warn(
                "routing.bounce.no_thread_id",
                email_id=email.id,
            )

        updated = update_email(connection, email.id, is_routed=True)
        return updated if updated is not None else email


# -- Self-sender guard ---------------------------------------------------------


def _is_self_sender(
    connection: psycopg.Connection[dict[str, Any]],
    sender_email: str,
) -> bool:
    """Check if the sender is one of our managed MailPilot accounts.

    When two managed accounts share a Gmail thread (e.g. an outbound campaign
    account replying to an inbound auto-reply account on the same domain),
    each side's reply round-trips into the other's mailbox as a fresh inbound
    email. Without this guard, ``_try_thread_match`` would link the echo to
    the local workflow and ``create_tasks_for_routed_emails`` would enqueue
    another agent task -- the runaway loop reported in #83.

    Match is case-insensitive (Gmail may normalise display-cased addresses).
    """
    if not sender_email:
        return False
    return get_account_by_email(connection, sender_email) is not None


# -- Three-step routing pipeline -----------------------------------------------


def _try_thread_match(
    connection: psycopg.Connection[dict[str, Any]],
    email: Email,
) -> str | None:
    """Step 1: match via Gmail thread ID.

    If a prior email in the same thread has a non-null ``workflow_id``,
    return the most recent such workflow. Works regardless of workflow
    status (active or paused) per the no-ghosting guarantee.
    """
    if not email.gmail_thread_id:
        return None
    thread_emails = get_emails_by_gmail_thread_id(connection, email.gmail_thread_id)
    matches = [
        prior
        for prior in thread_emails
        if prior.id != email.id
        and prior.account_id == email.account_id
        and prior.workflow_id is not None
    ]
    if not matches:
        return None
    matches.sort(key=lambda e: e.created_at, reverse=True)
    return matches[0].workflow_id


def _try_classify(
    connection: psycopg.Connection[dict[str, Any]],
    email: Email,
    sender_email: str,
    settings: Settings,
) -> str | None:
    """Step 2: LLM classification against active inbound workflows."""
    workflows = list_workflows(connection, account_id=email.account_id, status="active")
    inbound_workflows = [w for w in workflows if w.type == "inbound"]
    if not inbound_workflows:
        return None
    return classify_email(
        subject=email.subject,
        body=email.body_text,
        sender=sender_email,
        active_workflows=inbound_workflows,
        settings=settings,
    )


def _ensure_workflow_contact(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> None:
    """Create a workflow_contact entry if not already present."""
    create_workflow_contact(connection, workflow_id, contact_id)
