"""Workflow execution loop.

Composes account sync, inbound email-to-task bridging, and task
execution in a single loop. Tasks are the universal execution
primitive -- all agent invocations flow through the task queue.
"""

from __future__ import annotations

from typing import Any

import logfire
import psycopg

from mailpilot.agent import invoke_workflow_agent
from mailpilot.database import (
    complete_task,
    get_contact,
    get_email,
    get_workflow,
)
from mailpilot.models import Task
from mailpilot.settings import Settings


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
    with logfire.span(
        "run.execute_task",
        task_id=task.id,
        workflow_id=task.workflow_id,
        contact_id=task.contact_id,
    ):
        workflow = get_workflow(connection, task.workflow_id)
        if workflow is None or workflow.status != "active":
            logfire.info(
                "run.task.skip_inactive_workflow",
                task_id=task.id,
                workflow_id=task.workflow_id,
            )
            complete_task(connection, task.id, status="cancelled")
            return

        contact = get_contact(connection, task.contact_id)
        if contact is None or contact.status in ("bounced", "unsubscribed"):
            logfire.info(
                "run.task.skip_disabled_contact",
                task_id=task.id,
                contact_id=task.contact_id,
            )
            complete_task(connection, task.id, status="cancelled")
            return

        email = get_email(connection, task.email_id) if task.email_id else None

        try:
            result = invoke_workflow_agent(
                connection,
                settings,
                workflow,
                contact,
                email=email,
                task_description=task.description,
                task_context=task.context,
            )
        except Exception:
            logfire.exception(
                "run.task.agent_failed",
                task_id=task.id,
            )
            complete_task(connection, task.id, status="failed")
            return

        if result is None:
            logfire.info(
                "run.task.lock_held",
                task_id=task.id,
            )
            return

        complete_task(connection, task.id, status="completed")
