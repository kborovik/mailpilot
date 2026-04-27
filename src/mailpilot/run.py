"""Workflow execution loop.

Composes account sync, inbound email-to-task bridging, and task
execution in a single loop. Tasks are the universal execution
primitive -- all agent invocations flow through the task queue.
"""

from __future__ import annotations

import time
from typing import Any

import logfire
import psycopg

from mailpilot.agent import invoke_workflow_agent
from mailpilot.database import (
    complete_task,
    create_tasks_for_routed_emails,
    get_account,
    get_contact,
    get_email,
    get_workflow,
    list_accounts,
    list_pending_tasks,
)
from mailpilot.gmail import GmailClient
from mailpilot.models import Task
from mailpilot.settings import Settings
from mailpilot.sync import sync_account


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
            complete_task(
                connection,
                task.id,
                status="cancelled",
                result={"reason": "workflow inactive or not found"},
            )
            return

        contact = get_contact(connection, task.contact_id)
        if contact is None or contact.status in ("bounced", "unsubscribed"):
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
        except Exception as exc:
            logfire.exception(
                "run.task.agent_failed",
                task_id=task.id,
            )
            connection.rollback()
            complete_task(
                connection,
                task.id,
                status="failed",
                result={"reason": str(exc)},
            )
            return

        if result is None:
            logfire.info(
                "run.task.lock_held",
                task_id=task.id,
            )
            return

        complete_task(connection, task.id, status="completed", result=result)


def run_loop(
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
) -> None:
    """Run the main execution loop.

    Each iteration:
    1. Sync all accounts (Gmail fetch + inbound routing).
    2. Bridge routed emails to tasks.
    3. Drain pending task queue.
    4. Sleep for run_interval seconds.

    Exits cleanly on KeyboardInterrupt (Ctrl+C / SIGINT).

    Args:
        connection: Open database connection.
        settings: Application settings.
    """
    logfire.info("run.loop.start", interval=settings.run_interval)
    while True:
        with logfire.span("run.loop.iteration"):
            try:
                _sync_all_accounts(connection, settings)
                create_tasks_for_routed_emails(connection)
                pending = list_pending_tasks(connection)
                for pending_task in pending:
                    execute_task(connection, settings, pending_task)
            except KeyboardInterrupt:
                logfire.info("run.loop.stop")
                return
        time.sleep(settings.run_interval)


def _sync_all_accounts(
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
) -> None:
    """Sync all Gmail accounts. Errors per account are logged, not raised."""
    summaries = list_accounts(connection, limit=1000)
    for summary in summaries:
        account = get_account(connection, summary.id)
        if account is None:
            continue
        try:
            client = GmailClient(account.email)
            sync_account(connection, account, client, settings)
        except Exception:
            logfire.exception(
                "run.sync.account_failed",
                account_id=account.id,
                email=account.email,
            )
