"""Email routing pipeline (see §V.27).

Pipeline that assigns inbound emails to the correct workflow:

1. **RFC 2822 message-id match** -- inbound In-Reply-To / References headers
   cite a stored email's ``rfc2822_message_id``. Authoritative for replies:
   Gmail may merge same-subject threads across distinct outbound sends to one
   contact (campaign-test multi-scenario; multi-workflow enrollments), so a
   shared ``threadId`` is not a safe workflow key when Message-ID is present.
2. **Thread match** -- prior email in same Gmail thread has a workflow_id
   (used when no threading headers / Message-ID miss).
3. **LLM classification** -- single-turn call against active inbound workflows
4. **Unrouted** -- classifier ran on real candidates and rejected all of them
5. **Skipped, no inbound workflows** -- account has zero active inbound
   workflows; classifier never runs (distinct from the sync-layer
   ``skipped_no_workflows`` short-circuit, which fires when the account has
   zero active workflows of any type)

Cases 4 and 5 both store with is_routed=True, workflow_id=NULL but emit
distinct ``route_method`` values so operators can tell intentional structural
gaps apart from genuine classifier misses.

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
    cancel_enrollment_followup_tasks,
    conclude_enrollment,
    create_activity,
    create_enrollment,
    disable_contact,
    find_email_by_rfc2822_message_id,
    get_contact,
    get_emails_by_gmail_thread_id,
    get_enrollment,
    get_workflow,
    list_active_outbound_enrollments_for_contact,
    list_workflows,
    update_email,
)
from mailpilot.models import Contact, Email
from mailpilot.ooo import is_mechanical_ooo, schedule_ooo_resume
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
    """Route an inbound email through the §V.27 pipeline.

    Runs bounce detection, then the three-step routing pipeline
    (RFC message-id match -> thread match -> LLM classification -> unrouted).
    Creates an ``enrollment`` entry when routing to a workflow.

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

            # Prefer Message-ID when In-Reply-To/References are present: Gmail
            # may merge distinct same-subject threads, so thread_id can point at
            # the wrong workflow for multi-enrollment / multi-scenario sends.
            workflow_id = _try_rfc_message_id_match(connection, email)
            route_method: str
            if workflow_id is not None:
                route_method = "rfc_message_id_match"
            else:
                workflow_id = _try_thread_match(connection, email)
                if workflow_id is not None:
                    route_method = "thread_match"
                else:
                    workflow_id, route_method = _try_classify(
                        connection, email, sender_email, settings
                    )
            span.set_attribute("result", route_method)
            span.set_attribute("route_method", route_method)
            if workflow_id is not None:
                span.set_attribute("workflow_id", workflow_id)

            # "unrouted" is a span-only label: classifier ran on real candidates
            # but rejected them. §I / §V.20 persisted enum admits only the 7
            # decision values + NULL (routing pipeline ran, no enum bucket
            # matched). is_routed=TRUE carries the "pipeline completed" signal.
            persisted_route_method = (
                route_method if route_method != "unrouted" else None
            )
            # §V.164: inbound on an existing outbound thread binds the
            # enrolled contact even when From: local-part differs. Rebind
            # in the same UPDATE as the routing decision so _ensure_enrollment
            # never sees the alias From.
            bound = find_thread_enrolled_contact(
                connection,
                email.account_id,
                gmail_thread_id=email.gmail_thread_id,
                in_reply_to=email.in_reply_to,
                references_header=email.references_header,
            )
            contact_update: dict[str, object] = {}
            if bound is not None and email.contact_id != bound.id:
                contact_update["contact_id"] = bound.id
            updated = update_email(
                connection,
                email.id,
                workflow_id=workflow_id,
                is_routed=True,
                route_method=persisted_route_method,
                **contact_update,
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
                    _cancel_pending_followups(
                        connection, workflow_id, result.contact_id, result.id
                    )
                    _maybe_ooo_pause(connection, result, workflow_id)
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
    bounced, and disables the original recipient contact (§V.80). Then
    concludes every active outbound enrollment for that contact with
    ``do_not_contact`` and cancels pending follow-ups (§V.163). The bounce
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
                    reason = f"bounced: detected on email {original.id}"
                    disable_contact(
                        connection,
                        original.contact_id,
                        reason=reason,
                    )
                    _conclude_outbound_enrollments_for_bounce(
                        connection, original.contact_id, reason
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


def _conclude_outbound_enrollments_for_bounce(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
    reason: str,
) -> None:
    """Conclude every active outbound enrollment for a bounced contact (§V.163).

    Calls the §V.186 helper with ``do_not_contact`` and
    ``skip_if_terminal`` true so already-terminal enrollments are skipped.
    The enrollment row stays ``active`` (§V.15). Contact disable stays in
    ``_handle_bounce`` (§V.80).
    """
    enrollments = list_active_outbound_enrollments_for_contact(connection, contact_id)
    for enrollment in enrollments:
        conclude_enrollment(
            connection,
            enrollment.id,
            disposition="do_not_contact",
            reason=reason,
            skip_if_terminal=True,
        )


# -- Three-step routing pipeline -----------------------------------------------


def _try_thread_match(
    connection: psycopg.Connection[dict[str, Any]],
    email: Email,
) -> str | None:
    """Step 2: match via Gmail thread ID (when Message-ID stage misses).

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


def find_thread_enrolled_contact(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    *,
    gmail_thread_id: str | None = None,
    in_reply_to: str | None = None,
    references_header: str | None = None,
) -> Contact | None:
    """Return the enrolled contact on an existing outbound thread (§V.164).

    RFC Message-ID first (same order as §V.27), then Gmail thread. Only an
    outbound parent with ``contact_id`` counts -- inbound-only threads keep
    From-based resolution. Account-scoped so a shared thread id on another
    mailbox cannot leak a contact bind.
    """
    referenced_ids = _unique_message_ids(in_reply_to, references_header)
    rfc_parent: Email | None = None
    if referenced_ids:
        rfc_parent = find_email_by_rfc2822_message_id(
            connection, account_id, referenced_ids
        )

    outbound: Email | None = None
    if (
        rfc_parent is not None
        and rfc_parent.direction == "outbound"
        and rfc_parent.contact_id is not None
    ):
        outbound = rfc_parent
    if outbound is None:
        thread_ids: list[str] = []
        parent_thread = rfc_parent.gmail_thread_id if rfc_parent else None
        for tid in (gmail_thread_id, parent_thread):
            if tid and tid not in thread_ids:
                thread_ids.append(tid)
        candidates: list[Email] = []
        for tid in thread_ids:
            candidates.extend(
                prior
                for prior in get_emails_by_gmail_thread_id(connection, tid)
                if prior.account_id == account_id
                and prior.direction == "outbound"
                and prior.contact_id is not None
            )
        if candidates:
            candidates.sort(key=lambda e: e.created_at, reverse=True)
            outbound = candidates[0]
    if outbound is None or outbound.contact_id is None:
        return None
    return get_contact(connection, outbound.contact_id)


def _try_rfc_message_id_match(
    connection: psycopg.Connection[dict[str, Any]],
    email: Email,
) -> str | None:
    """Step 1: match via RFC 2822 In-Reply-To / References headers.

    Walk cited message-ids and look them up against
    ``email.rfc2822_message_id`` within the same account. Prefer this over
    Gmail thread match: recipient-side re-thread and same-subject merge can
    attach a reply to the wrong conversation when one contact has multiple
    outbound threads.

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


def _unique_message_ids(*raw_headers: str | None) -> list[str]:
    """Dedupe whitespace-separated Message-ID tokens, parent first."""
    candidates: list[str] = []
    for raw in raw_headers:
        if raw:
            candidates.extend(raw.split())
    seen: set[str] = set()
    unique: list[str] = []
    for raw in candidates:
        token = raw.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        unique.append(token)
    return unique


def _collect_referenced_message_ids(email: Email) -> list[str]:
    """Return message-ids cited by an inbound email's threading headers.

    Combines the parent ``In-Reply-To`` value with every entry in the
    whitespace-separated ``References`` chain. Duplicates are dropped
    while preserving the order that the original headers used (parent
    first, then ancestors). Returns an empty list when neither header
    is populated.
    """
    return _unique_message_ids(email.in_reply_to, email.references_header)


def _try_classify(
    connection: psycopg.Connection[dict[str, Any]],
    email: Email,
    sender_email: str,
    settings: Settings,
) -> tuple[str | None, str]:
    """Step 3: LLM classification against active inbound workflows.

    Returns ``(workflow_id, route_method)`` where ``route_method`` is:

    - ``"classified"`` -- classifier ran and returned a workflow_id.
    - ``"unrouted"`` -- classifier ran on real candidates and returned None.
    - ``"skipped_no_inbound_workflows"`` -- account has no active inbound
      workflows (or none survive hydration); classifier was not called.
    """
    summaries = list_workflows(connection, account_id=email.account_id, status="active")
    inbound_summaries = [s for s in summaries if s.type == "inbound"]
    if not inbound_summaries:
        return (None, "skipped_no_inbound_workflows")
    # classify_email reads workflow.goal, which is not in WorkflowSummary;
    # hydrate via get_workflow so the LLM prompt has the full record.
    inbound_workflows = [
        full
        for full in (get_workflow(connection, s.id) for s in inbound_summaries)
        if full is not None
    ]
    if not inbound_workflows:
        return (None, "skipped_no_inbound_workflows")
    workflow_id = classify_email(
        subject=email.subject,
        body=email.body_text,
        sender=sender_email,
        active_workflows=inbound_workflows,
        settings=settings,
    )
    return (workflow_id, "classified" if workflow_id is not None else "unrouted")


def _ensure_enrollment(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> None:
    """Create an enrollment entry if not already present.

    Emits an ``enrollment_added`` activity only on the initial insert --
    ``create_enrollment`` returns ``None`` on ON CONFLICT so re-routes
    in the same thread do not duplicate the timeline entry.
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
        activity_type="enrollment_added",
        summary=f"Assigned to {workflow_name or 'workflow'}",
        detail={"workflow_name": workflow_name},
        company_id=contact.company_id if contact is not None else None,
        workflow_id=workflow_id,
        enrollment_id=enrollment.id,
    )


def _cancel_pending_followups(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
    email_id: str,
) -> None:
    """Cancel the enrollment's pending future follow-up touches (§V.123).

    An inbound reply means the prospect engaged, so any later cold
    follow-up touch is cancelled before it wakes. The operator first-touch
    (``trigger='enrollment_schedule'``) is preserved. The enrollment is
    resolved fresh rather than reusing ``_ensure_enrollment`` because the
    reply case finds a pre-existing enrollment, where ``create_enrollment``
    returns ``None`` (§V.28) -- the cancel must fire on that branch too.
    """
    enrollment = get_enrollment(connection, workflow_id, contact_id)
    if enrollment is None:
        return
    cancelled = cancel_enrollment_followup_tasks(connection, enrollment.id)
    if cancelled:
        operator_event(
            "route.followups_cancelled",
            email_id=email_id,
            enrollment_id=enrollment.id,
            cancelled_count=len(cancelled),
        )


def _maybe_ooo_pause(
    connection: psycopg.Connection[dict[str, Any]],
    email: Email,
    workflow_id: str,
) -> None:
    """Schedule an OOO resume after V123 cancel on mechanical auto-reply.

    Only outbound enrollments. Address-change / left-company auto-replies
    are not OOO (§V.161, §V.164) and stay on the agent path.
    """
    if email.contact_id is None:
        return
    if not is_mechanical_ooo(email):
        return
    workflow = get_workflow(connection, workflow_id)
    if workflow is None or workflow.type != "outbound":
        return
    enrollment = get_enrollment(connection, workflow_id, email.contact_id)
    if enrollment is None:
        return
    schedule_ooo_resume(connection, workflow, enrollment, email)
