"""Email routing pipeline (ADR-04).

Pipeline that assigns inbound emails to the correct workflow:

1. **Thread match** -- prior email in same Gmail thread has a workflow_id
2. **RFC 2822 message-id match** -- inbound In-Reply-To / References headers
   cite a stored email's ``rfc2822_message_id`` (covers Gmail recipient-side
   re-threading, where the same conversation has different ``threadId`` on
   each side)
3. **LLM classification** -- single-turn call against active inbound workflows
4. **Unrouted** -- store with is_routed=True, workflow_id=NULL

Also handles bounce detection (mailer-daemon/postmaster senders and
bounce-related Gmail labels) and creates ``enrollment`` entries
on successful routing.
"""

from __future__ import annotations

from typing import Any

import logfire
import psycopg

from mailpilot.agent.classify import classify_email
from mailpilot.database import (
    create_activity,
    create_enrollment,
    disable_contact,
    find_email_by_rfc2822_message_id,
    get_contact,
    get_emails_by_gmail_thread_id,
    get_workflow,
    list_workflows,
    update_email,
)
from mailpilot.models import Email
from mailpilot.operator_log import operator_event
from mailpilot.settings import Settings

_VIA_BY_ROUTE_METHOD: dict[str, str] = {
    "thread_match": "thread",
    "rfc_message_id_match": "message_id",
    "classified": "llm",
}

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
    ``enrollment`` entry when routing to a workflow.

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

            workflow_id = _try_thread_match(connection, email)
            route_method: str
            if workflow_id is not None:
                route_method = "thread_match"
            else:
                workflow_id = _try_rfc_message_id_match(connection, email)
                if workflow_id is not None:
                    route_method = "rfc_message_id_match"
                else:
                    workflow_id = _try_classify(
                        connection, email, sender_email, settings
                    )
                    route_method = (
                        "classified" if workflow_id is not None else "unrouted"
                    )
            span.set_attribute(
                "result",
                route_method if workflow_id is not None else "unrouted",
            )
            span.set_attribute("route_method", route_method)
            if workflow_id is not None:
                span.set_attribute("workflow_id", workflow_id)

            updated = update_email(
                connection, email.id, workflow_id=workflow_id, is_routed=True
            )
            result = updated if updated is not None else email

            if workflow_id is not None:
                operator_event(
                    "route.match",
                    email_id=result.id,
                    workflow_id=workflow_id,
                    via=_VIA_BY_ROUTE_METHOD[route_method],
                )
                if result.contact_id is not None:
                    _ensure_enrollment(connection, workflow_id, result.contact_id)
            else:
                operator_event("route.no_match", email_id=result.id)

            return result
        except Exception as exc:
            span.set_attribute("result", "failure")
            logfire.exception("routing.route_email failed", email_id=email.id)
            operator_event("error", source="routing.route_email", message=str(exc))
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


def _try_rfc_message_id_match(
    connection: psycopg.Connection[dict[str, Any]],
    email: Email,
) -> str | None:
    """Step 1b: match via RFC 2822 In-Reply-To / References headers.

    Gmail re-threads on the recipient side: a reply that lands on the
    outbound mailbox can have a fresh ``threadId`` even though it cites
    our original send via ``In-Reply-To`` and ``References``. When the
    Gmail-thread lookup returns no match, walk the cited message-ids and
    look them up against ``email.rfc2822_message_id`` within the same
    account.

    Returns the matching email's ``workflow_id`` or ``None``. Scope is
    intentionally restricted to the inbound email's own ``account_id`` so
    cross-account collisions on a shared Message-ID cannot leak workflow
    assignments.
    """
    referenced_ids = _collect_referenced_message_ids(email)
    if not referenced_ids:
        return None
    parent = find_email_by_rfc2822_message_id(
        connection, email.account_id, referenced_ids
    )
    if parent is None or parent.workflow_id is None:
        return None
    return parent.workflow_id


def _collect_referenced_message_ids(email: Email) -> list[str]:
    """Return message-ids cited by an inbound email's threading headers.

    Combines the parent ``In-Reply-To`` value with every entry in the
    whitespace-separated ``References`` chain. Duplicates are dropped
    while preserving the order that the original headers used (parent
    first, then ancestors). Returns an empty list when neither header
    is populated.
    """
    candidates: list[str] = []
    if email.in_reply_to:
        candidates.extend(email.in_reply_to.split())
    if email.references_header:
        candidates.extend(email.references_header.split())
    seen: set[str] = set()
    unique: list[str] = []
    for raw in candidates:
        token = raw.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        unique.append(token)
    return unique


def _try_classify(
    connection: psycopg.Connection[dict[str, Any]],
    email: Email,
    sender_email: str,
    settings: Settings,
) -> str | None:
    """Step 2: LLM classification against active inbound workflows."""
    summaries = list_workflows(connection, account_id=email.account_id, status="active")
    inbound_summaries = [s for s in summaries if s.type == "inbound"]
    if not inbound_summaries:
        return None
    # classify_email reads workflow.objective, which is not in WorkflowSummary;
    # hydrate via get_workflow so the LLM prompt has the full record.
    inbound_workflows = [
        full
        for full in (get_workflow(connection, s.id) for s in inbound_summaries)
        if full is not None
    ]
    if not inbound_workflows:
        return None
    return classify_email(
        subject=email.subject,
        body=email.body_text,
        sender=sender_email,
        active_workflows=inbound_workflows,
        settings=settings,
    )


def _ensure_enrollment(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> None:
    """Create an enrollment entry if not already present.

    Emits a ``workflow_assigned`` activity only on the initial insert --
    ``create_enrollment`` returns ``None`` on ON CONFLICT so re-routes in
    the same thread do not duplicate the timeline entry.
    """
    enrollment = create_enrollment(connection, workflow_id, contact_id)
    if enrollment is None:
        return
    workflow = get_workflow(connection, workflow_id)
    contact = get_contact(connection, contact_id)
    workflow_name = workflow.name if workflow is not None else ""
    create_activity(
        connection,
        contact_id=contact_id,
        activity_type="workflow_assigned",
        summary=f"Assigned to {workflow_name or 'workflow'}",
        detail={"workflow_id": workflow_id, "workflow_name": workflow_name},
        company_id=contact.company_id if contact is not None else None,
    )
