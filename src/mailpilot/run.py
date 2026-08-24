"""Workflow execution loop.

Composes account sync, inbound email-to-task bridging, and task
execution in a single loop. Tasks are the universal execution
primitive -- all agent invocations flow through the task queue.
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Any

import logfire
import psycopg

from mailpilot import email_ops
from mailpilot.agent import invoke_workflow_agent
from mailpilot.agent.retry import BACKOFF_SECONDS, MAX_ATTEMPTS, is_transient
from mailpilot.agent.templates import (
    _FALLBACK_ACKNOWLEDGEMENT,  # pyright: ignore[reportPrivateUsage]
)
from mailpilot.agent.tools import (
    reply_emitted_scope,
    reply_was_emitted,
)
from mailpilot.cadence import resolve_touch_number
from mailpilot.database import (
    complete_task,
    get_account,
    get_contact,
    get_email,
    get_enrollment,
    get_latest_enrollment_outcome,
    get_workflow,
    list_emails,
    list_inbound_emails_from_contact_after,
    reschedule_task_for_lock_contention,
    reschedule_task_for_retry,
)
from mailpilot.gmail import GmailClient
from mailpilot.models import Contact, Email, Enrollment, Task, Workflow
from mailpilot.ooo import (
    is_mechanical_ooo,
    is_ooo_auto_reply,
    schedule_ooo_resume,
)
from mailpilot.operator_log import operator_event
from mailpilot.settings import Settings

_LOCK_CONTENTION_BACKOFF_SECONDS = 5
_LOCK_CONTENTION_JITTER_SECONDS = 5
# §V.136: a touch-N (N>=2) task whose workflow lost its cadence pair is pushed
# one hour out instead of composing a malformed touch (§V.25 reschedule shape).
_NULL_CADENCE_BACKOFF_SECONDS = 3600


def execute_task(
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
    task: Task,
) -> None:
    """Execute a single pending task by invoking the workflow agent.

    Args:
        connection: Open database connection.
        settings: Application settings.
        task: Pending task to execute.
    """
    with (
        logfire.span(
            "run.execute_task",
            task_id=task.id,
            workflow_id=task.workflow_id,
            contact_id=task.contact_id,
        ),
        # §V.131: install a per-task reply-emitted flag so a successful
        # ``reply_email`` / ``send_email`` this task is recorded in memory.
        # ``_handle_agent_failure`` reads it to suppress a duplicate fallback
        # reply when a non-transient class raised after a mid-turn send.
        reply_emitted_scope(),
    ):
        loaded = _load_execute_context(connection, task)
        if loaded is None:
            return
        workflow, contact, enrollment, email, trigger = loaded

        try:
            result = invoke_workflow_agent(
                connection,
                settings,
                workflow,
                contact,
                email=email,
                task_description=task.description,
                task_context=task.context,
                trigger=trigger,
                task_id=task.id,
            )
        except Exception as exc:
            _handle_agent_failure(connection, settings, task, exc)
            return

        if result is None:
            # §V.25: lock contention is not a retry -- the task ran nothing,
            # attempt_count stays put. Push scheduled_at forward so the
            # ``task_pending_trigger`` notify wakes the drain loop again;
            # leaving the row ``pending`` with no signal stranded tasks
            # behind their own lock under bursty inbound traffic (§B.42).
            backoff = _LOCK_CONTENTION_BACKOFF_SECONDS + random.randint(
                0, _LOCK_CONTENTION_JITTER_SECONDS
            )
            logfire.info(
                "run.task.lock_held",
                task_id=task.id,
                backoff_seconds=backoff,
            )
            reschedule_task_for_lock_contention(connection, task.id, backoff)
            return

        _maybe_ooo_resume_after_invoke(connection, workflow, enrollment, email)
        complete_task(connection, task.id, status="completed", result=result)


def _load_execute_context(  # noqa: PLR0911
    connection: psycopg.Connection[dict[str, Any]],
    task: Task,
) -> tuple[Workflow, Contact, Enrollment, Email | None, str] | None:
    """Load and gate the execute_task inputs, or handle the skip and return None."""
    workflow = get_workflow(connection, task.workflow_id)
    if workflow is None or workflow.status != "active":
        logfire.info(
            "run.task.skip_inactive_workflow",
            task_id=task.id,
            workflow_id=task.workflow_id,
        )
        complete_task(
            connection,
            task.id,
            status="cancelled",
            result={"reason": "workflow inactive or not found"},
        )
        return None

    contact = get_contact(connection, task.contact_id)
    if contact is None or contact.disabled_reason is not None:
        logfire.info(
            "run.task.skip_disabled_contact",
            task_id=task.id,
            contact_id=task.contact_id,
        )
        complete_task(
            connection,
            task.id,
            status="cancelled",
            result={"reason": "contact disabled or not found"},
        )
        return None

    enrollment = get_enrollment(connection, task.workflow_id, task.contact_id)
    if enrollment is None:
        logfire.info(
            "run.task.skip_missing_enrollment",
            task_id=task.id,
            workflow_id=task.workflow_id,
            contact_id=task.contact_id,
        )
        complete_task(
            connection,
            task.id,
            status="cancelled",
            result={"reason": "enrollment not found"},
        )
        return None
    if enrollment.status != "active":
        logfire.info(
            "run.task.skip_inactive_enrollment",
            task_id=task.id,
            workflow_id=task.workflow_id,
            contact_id=task.contact_id,
            enrollment_status=enrollment.status,
        )
        complete_task(
            connection,
            task.id,
            status="cancelled",
            result={"reason": f"enrollment {enrollment.status}"},
        )
        return None

    # §V.83: a queued follow-up touch (``context.touch`` set by the cadence
    # engine) is cancelled without an LLM call once the sequence no longer
    # warrants a cold send -- the enrollment already reached a terminal
    # outcome, or the contact replied after the prior touch. This complements
    # the reply-time cancellation (§V.123) by catching the touch already due
    # when the reply landed. First reach-outs (touch 1, no ``context.touch``)
    # are exempt: there is no prior touch to have been answered.
    touch = task.context.get("touch") if task.context else None
    if isinstance(touch, int):
        cancel_reason = _touch_cancel_reason(connection, task)
        if cancel_reason is not None:
            logfire.info(
                "run.task.skip_superseded_touch",
                task_id=task.id,
                workflow_id=task.workflow_id,
                contact_id=task.contact_id,
                reason=cancel_reason,
            )
            complete_task(
                connection,
                task.id,
                status="cancelled",
                result={"reason": cancel_reason},
            )
            return None

    email = get_email(connection, task.email_id) if task.email_id else None
    if _complete_mechanical_ooo(connection, task, workflow, email):
        return None

    # §V.32: scheduled first-touch tasks carry ``trigger=enrollment_schedule``
    # in their context; the run loop must surface that to the agent span so
    # initial-send framing replaces the deferred-task framing. Default to
    # ``task`` for legacy task rows that pre-date scheduled enrollment.
    context_trigger = task.context.get("trigger") if task.context else None
    trigger = context_trigger if isinstance(context_trigger, str) else "task"
    if _reschedule_null_cadence_touch(connection, task, workflow, trigger):
        return None
    return workflow, contact, enrollment, email, trigger


def _reschedule_null_cadence_touch(
    connection: psycopg.Connection[dict[str, Any]],
    task: Task,
    workflow: Workflow,
    trigger: str,
) -> bool:
    """Push a touch N>=2 with a lost cadence pair one hour out (§V.136)."""
    touch_number = resolve_touch_number(task.context, trigger)
    if touch_number is None or touch_number < 2 or workflow.touches is not None:
        return False
    logfire.warn(
        "run.task.null_cadence_touch",
        task_id=task.id,
        workflow_id=task.workflow_id,
        touch=touch_number,
    )
    operator_event(
        "task.null_cadence_reschedule",
        task_id=task.id,
        workflow_id=task.workflow_id,
        touch=touch_number,
    )
    reschedule_task_for_lock_contention(
        connection, task.id, _NULL_CADENCE_BACKOFF_SECONDS
    )
    return True


def _complete_mechanical_ooo(
    connection: psycopg.Connection[dict[str, Any]],
    task: Task,
    workflow: Workflow,
    email: Email | None,
) -> bool:
    """Complete leftover in-queue inbound tasks without an agent or second resume."""
    if email is None or workflow.type != "outbound" or not is_mechanical_ooo(email):
        return False
    complete_task(
        connection,
        task.id,
        status="completed",
        result={"reason": "ooo_pause"},
    )
    return True


def _maybe_ooo_resume_after_invoke(
    connection: psycopg.Connection[dict[str, Any]],
    workflow: Workflow,
    enrollment: Enrollment,
    email: Email | None,
) -> None:
    """Schedule resume after an agent OOO-class noop (§V.169)."""
    if (
        email is None
        or workflow.type != "outbound"
        or not is_ooo_auto_reply(email)
        or reply_was_emitted()
        or get_latest_enrollment_outcome(connection, enrollment.id) is not None
    ):
        return
    schedule_ooo_resume(connection, workflow, enrollment, email)


def _touch_cancel_reason(
    connection: psycopg.Connection[dict[str, Any]],
    task: Task,
) -> str | None:
    """Return why a queued touch should be cancelled, else None (§V.83).

    Two deterministic conditions supersede a follow-up touch, each answerable
    from database state with no LLM call: the enrollment already reached a
    terminal outcome, or the contact replied after the prior touch. Returns the
    first matching reason string, or ``None`` when the touch may proceed.
    """
    if get_latest_enrollment_outcome(connection, task.enrollment_id) is not None:
        return "enrollment already concluded"
    prior_touch_at = _prior_touch_sent_at(connection, task)
    if prior_touch_at is None:
        return None
    inbounds = list_inbound_emails_from_contact_after(
        connection, task.contact_id, prior_touch_at
    )
    if any(not is_ooo_auto_reply(email) for email in inbounds):
        return "contact replied after prior touch"
    return None


def _prior_touch_sent_at(
    connection: psycopg.Connection[dict[str, Any]],
    task: Task,
) -> datetime | None:
    """Resolve when the prior touch went out, else None (§V.83).

    Prefers ``context['prior_email_id']`` -- the cadence engine threads the id
    of touch N-1's email into a scheduled touch-N task (§V.136). Falls back to
    the enrollment's latest outbound email for a migrated row that carries the
    touch number but no ``prior_email_id`` (migration 010). Returns ``None``
    when no prior outbound touch is found, so the inbound-after check is skipped.
    """
    prior_id = task.context.get("prior_email_id") if task.context else None
    if isinstance(prior_id, str):
        prior = get_email(connection, prior_id)
        if prior is not None:
            return prior.sent_at or prior.received_at
    summaries = list_emails(
        connection,
        contact_id=task.contact_id,
        workflow_id=task.workflow_id,
        direction="outbound",
        limit=1,
    )
    if summaries:
        return summaries[0].sent_at or summaries[0].received_at
    return None


def _handle_agent_failure(
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
    task: Task,
    exc: Exception,
) -> None:
    """Branch transient (retry) vs terminal (`failed`) per `§V.49`.

    On a terminal failure of an inbound-email task, send one fixed fallback
    acknowledgement before marking the task ``failed`` so the sender never
    gets silent NO_REPLY (§V.131).
    """
    connection.rollback()
    next_attempt = task.attempt_count + 1
    transient = is_transient(exc)
    if transient and next_attempt < MAX_ATTEMPTS:
        backoff = BACKOFF_SECONDS[task.attempt_count]
        logfire.warn(
            "run.task.transient_retry",
            task_id=task.id,
            attempt=next_attempt,
            max_attempts=MAX_ATTEMPTS,
            backoff_seconds=backoff,
            exc_type=type(exc).__name__,
        )
        operator_event(
            "task.retry",
            task_id=task.id,
            attempt=next_attempt,
            exc=type(exc).__name__,
        )
        reschedule_task_for_retry(connection, task.id, backoff, exc)
        return
    logfire.exception(
        "run.task.agent_failed",
        task_id=task.id,
    )
    operator_event("error", source="run.task.agent_failed", message=str(exc))
    # §V.131: a terminal failure on an inbound-email task owes the sender one
    # fixed acknowledgement instead of silent NO_REPLY. An outbound first
    # reach-out (``email_id`` NULL) stays silent -- silence is correct on a cold
    # open. OOO / auto-reply inbound is also silent (§V.169): the ACK would
    # reply to a machine and burn last_touch / emails_sent. The in-run
    # reply-emitted flag is the only sound already-replied signal: the
    # ``connection.rollback()`` above erased any mid-turn-sent email row
    # while Gmail kept the message, so a thread/DB read would miss it and a
    # double-reply could follow a non-transient class that raised after a send.
    if task.email_id is not None and not reply_was_emitted():
        _ack_or_ooo_pause_on_failure(connection, settings, task)
    terminal_reason = "max_attempts" if transient else "non_transient"
    complete_task(
        connection,
        task.id,
        status="failed",
        result={
            "reason": str(exc),
            "attempt_count": next_attempt,
            "terminal": terminal_reason,
        },
    )


def _ack_or_ooo_pause_on_failure(
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
    task: Task,
) -> None:
    """Skip the fallback ACK on OOO inbound; otherwise send it (§V.131, §V.169)."""
    if task.email_id is None:
        return
    inbound = get_email(connection, task.email_id)
    if inbound is not None and is_ooo_auto_reply(inbound):
        workflow = get_workflow(connection, task.workflow_id)
        enrollment = get_enrollment(connection, task.workflow_id, task.contact_id)
        if workflow is not None and enrollment is not None:
            schedule_ooo_resume(connection, workflow, enrollment, inbound)
        return
    _send_fallback_acknowledgement(connection, settings, task)


def _send_fallback_acknowledgement(
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
    task: Task,
) -> None:
    """Send the fixed fallback acknowledgement for a terminal inbound failure.

    Best-effort per §V.131: a send failure logs an operator error and still
    returns so the caller marks the task ``failed``. Never re-raises -- the
    fallback must not mask the original terminal failure. Resolves the inbound
    email, its owning account, and a Gmail client the same way ``invoke`` does,
    then replies in-thread with the code-defined ``_FALLBACK_ACKNOWLEDGEMENT``
    body so the reply is never model-generated.
    """
    try:
        email = get_email(connection, task.email_id) if task.email_id else None
        if email is None:
            return
        account = get_account(connection, email.account_id)
        if account is None:
            return
        gmail_client = GmailClient(account.email)
        email_ops.reply_email(
            connection,
            account,
            gmail_client,
            settings,
            email_id=email.id,
            body=_FALLBACK_ACKNOWLEDGEMENT,
            workflow_id=task.workflow_id,
        )
    except Exception as exc:
        operator_event(
            "error",
            source="run.task.fallback_failed",
            message=str(exc),
        )
