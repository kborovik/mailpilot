"""Workflow execution loop.

Composes account sync, inbound email-to-task bridging, and task
execution in a single loop. Tasks are the universal execution
primitive -- all agent invocations flow through the task queue.
"""

from __future__ import annotations

import random
from typing import Any

import logfire
import psycopg

from mailpilot.agent import invoke_workflow_agent
from mailpilot.agent.retry import BACKOFF_SECONDS, MAX_ATTEMPTS, is_transient
from mailpilot.agent.tools import reply_rejection_scope
from mailpilot.database import (
    complete_task,
    get_contact,
    get_email,
    get_enrollment,
    get_workflow,
    reschedule_task_for_lock_contention,
    reschedule_task_for_retry,
)
from mailpilot.models import Task
from mailpilot.operator_log import operator_event
from mailpilot.settings import Settings

_LOCK_CONTENTION_BACKOFF_SECONDS = 5
_LOCK_CONTENTION_JITTER_SECONDS = 5


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
        # §V.71: install a per-task reply-rejection counter so
        # ``reply_email`` / ``send_email`` calls share the cap across one
        # ``agent.invoke``. Counter covers both format-lint and fact-check
        # rejections so neither rejection class can loop unbounded. Outside
        # this scope (CLI ``enrollment run``, etc.) the counter is absent and
        # both checks behave as before.
        reply_rejection_scope(),
    ):
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
            return

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
            return

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
            return
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
            return

        email = get_email(connection, task.email_id) if task.email_id else None

        # §V.32: scheduled first-touch tasks carry ``trigger=enrollment_schedule``
        # in their context; the run loop must surface that to the agent span so
        # initial-send framing replaces the deferred-task framing. Default to
        # ``task`` for legacy task rows that pre-date scheduled enrollment.
        context_trigger = task.context.get("trigger") if task.context else None
        trigger = context_trigger if isinstance(context_trigger, str) else "task"
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
            _handle_agent_failure(connection, task, exc)
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

        complete_task(connection, task.id, status="completed", result=result)


def _handle_agent_failure(
    connection: psycopg.Connection[dict[str, Any]],
    task: Task,
    exc: Exception,
) -> None:
    """Branch transient (retry) vs terminal (`failed`) per `§V.49`."""
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
