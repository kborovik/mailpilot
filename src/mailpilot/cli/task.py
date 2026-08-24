"""Task commands."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import click

from mailpilot._filters import (
    TASK_TRIGGERS,
    enum_option,
    limit_option,
    scope_option,
    task_scope_options,
)
from mailpilot.cli._helpers import _parse_future_scheduled_at
from mailpilot.cli.main import (
    _db,
    _resolve_task_scope,
    _resolve_workflow_id,
    main,
    output,
    output_entity,
    output_error,
)

# -- Task commands -------------------------------------------------------------


@main.group()
def task() -> None:
    """Manage deferred agent tasks."""


@task.command("list")
@task_scope_options
@limit_option
def task_list(
    workflow_id: str | None,
    contact_email: str | None,
    status: str | None,
    trigger: str | None,
    overdue: bool,
    touches: tuple[int, ...],
    limit: int,
    since: str | None,
    until: str | None,
) -> None:
    """List tasks as summaries with optional filters."""
    from mailpilot.database import (
        list_tasks,
    )

    with _db() as connection:
        resolved_workflow_id, contact_id = _resolve_task_scope(
            connection, workflow_id, contact_email
        )
        tasks = list_tasks(
            connection,
            workflow_id=resolved_workflow_id,
            contact_id=contact_id,
            status=status,
            trigger=trigger,
            limit=limit,
            since=since,
            until=until,
            overdue=overdue,
            touches=list(touches) if touches else None,
        )
        output({"tasks": [t.model_dump(mode="json") for t in tasks]})


@task.command("stats")
@scope_option("--workflow-id", "workflow_id", "Filter by workflow (name or ID).")
@enum_option("--trigger", "trigger", TASK_TRIGGERS, "Filter by task trigger.")
@click.option(
    "--bucket-tz",
    default="UTC",
    help="IANA timezone for day-bucketing distinct_scheduled_days.",
)
def task_stats(
    workflow_id: str | None,
    trigger: str | None,
    bucket_tz: str,
) -> None:
    """Show the task-cadence aggregate over the task queue."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from mailpilot.database import (
        get_task_stats,
    )

    with _db() as connection:
        resolved_workflow_id: str | None = (
            _resolve_workflow_id(connection, workflow_id)
            if workflow_id is not None
            else None
        )
        try:
            ZoneInfo(bucket_tz)
        except ZoneInfoNotFoundError, ValueError:
            output_error(f"unknown timezone: {bucket_tz}", "validation_error")
        stats = get_task_stats(
            connection,
            workflow_id=resolved_workflow_id,
            trigger=trigger,
            bucket_tz=bucket_tz,
        )
        output({"task_stats": stats.model_dump(mode="json")})


@task.command("view")
@click.argument("task_id")
def task_view(task_id: str) -> None:
    """Show a task by ID."""
    from mailpilot.database import get_task

    with _db() as connection:
        found = get_task(connection, task_id)
        if found is None:
            output_error(f"task not found: {task_id}", "not_found")
        output_entity("task", found)


_TASK_CANCEL_REQUIRED: tuple[str, ...] = (
    "touch",
    "workflow-id",
    "contact-email",
    "trigger",
    "overdue",
)
_TASK_RETRY_REQUIRED: tuple[str, ...] = (
    "touch",
    "workflow-id",
    "contact-email",
    "trigger",
)
_TASK_CANCEL_STATUS: tuple[str, ...] = ("pending",)
_TASK_RETRY_STATUS: tuple[str, ...] = ("failed", "cancelled")


def _task_filter_mode(
    task_id: str | None,
    *,
    required: tuple[str, ...],
    allowed_status: tuple[str, ...],
    workflow_id: str | None = None,
    contact_email: str | None = None,
    status: str | None = None,
    trigger: str | None = None,
    overdue: bool = False,
    since: str | None = None,
    until: str | None = None,
    touches: tuple[int, ...] = (),
) -> None:
    """Encode TASK_ID XOR filters plus filter-mode status (§V.180)."""
    flags = {
        "touch": bool(touches),
        "workflow-id": workflow_id is not None,
        "contact-email": contact_email is not None,
        "trigger": trigger is not None,
        "overdue": overdue,
    }
    has_required = any(flags[name] for name in required)
    has_any_filter = bool(
        any(flags.values())
        or status is not None
        or since is not None
        or until is not None
    )
    if task_id is not None and has_any_filter:
        output_error(
            "TASK_ID is exclusive with filter flags",
            "validation_error",
        )
    if task_id is None and not has_required:
        listed = ", ".join(f"--{name}" for name in required)
        output_error(
            f"TASK_ID or a filter ({listed}) is required",
            "validation_error",
        )
    if task_id is None and status is not None and status not in allowed_status:
        allowed = " or ".join(allowed_status)
        output_error(
            f"filter-mode --status must be {allowed}, got {status!r}",
            "validation_error",
        )


@task.command("cancel")
@click.argument("task_id", required=False, default=None)
@task_scope_options
def task_cancel(
    task_id: str | None,
    workflow_id: str | None,
    contact_email: str | None,
    status: str | None,
    trigger: str | None,
    overdue: bool,
    touches: tuple[int, ...],
    since: str | None,
    until: str | None,
) -> None:
    """Cancel one pending task by ID, or every matching pending task.

    Filter-mode (no TASK_ID) needs at least one of --touch, --workflow-id,
    --contact-email, --trigger, or --overdue. --status defaults to pending;
    any other status is rejected. TASK_ID and filters are exclusive.
    """
    from mailpilot.database import (
        cancel_task,
        cancel_tasks_matching,
    )

    _task_filter_mode(
        task_id,
        required=_TASK_CANCEL_REQUIRED,
        allowed_status=_TASK_CANCEL_STATUS,
        workflow_id=workflow_id,
        contact_email=contact_email,
        status=status,
        trigger=trigger,
        overdue=overdue,
        since=since,
        until=until,
        touches=touches,
    )

    with _db(mutate=True) as connection:
        if task_id is not None:
            cancelled = cancel_task(connection, task_id)
            if cancelled is None:
                output_error(
                    f"task not found or not pending: {task_id}",
                    "not_found",
                )
            output_entity("task", cancelled)
            return

        resolved_workflow_id, contact_id = _resolve_task_scope(
            connection, workflow_id, contact_email
        )
        result = cancel_tasks_matching(
            connection,
            workflow_id=resolved_workflow_id,
            contact_id=contact_id,
            trigger=trigger,
            overdue=overdue,
            since=since,
            until=until,
            touches=list(touches) if touches else None,
        )
        output(
            {"task_cancel": result.model_dump(mode="json")},
            record_count=result.cancelled_count,
        )


@task.command("retry")
@click.argument("task_id", required=False, default=None)
@task_scope_options
@click.option(
    "--scheduled-at",
    "scheduled_at",
    default=None,
    help=(
        "ISO 8601 timestamp to requeue at. Applies to every selected row. "
        "Omit to keep a still-future stored time, or now when the stored "
        "time is past."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview matching ids and companies; do not write.",
)
def task_retry(
    task_id: str | None,
    workflow_id: str | None,
    contact_email: str | None,
    status: str | None,
    trigger: str | None,
    overdue: bool,
    touches: tuple[int, ...],
    since: str | None,
    until: str | None,
    scheduled_at: str | None,
    dry_run: bool,
) -> None:
    """Reset failed or cancelled tasks for a fresh attempt.

    Pass TASK_ID to retry one row, or filters to retry every matching
    failed (default) or cancelled row. Filter-mode needs at least one of
    --touch, --workflow-id, --contact-email, or --trigger. --status
    defaults to failed; only failed and cancelled are allowed. TASK_ID
    and filters are exclusive. --scheduled-at applies to every selected
    row. --dry-run previews ids and companies with no writes.
    """
    from mailpilot.database import (
        get_task,
        retry_tasks_matching,
    )

    _task_filter_mode(
        task_id,
        required=_TASK_RETRY_REQUIRED,
        allowed_status=_TASK_RETRY_STATUS,
        workflow_id=workflow_id,
        contact_email=contact_email,
        status=status,
        trigger=trigger,
        overdue=overdue,
        since=since,
        until=until,
        touches=touches,
    )
    scheduled_iso = _parse_future_scheduled_at(scheduled_at)
    with _db(mutate=True) as connection:
        if task_id is not None:
            existing = get_task(connection, task_id)
            if existing is None:
                output_error(f"task not found: {task_id}", "not_found")
            if existing.status not in _TASK_RETRY_STATUS:
                output_error(
                    f"task not retryable in status {existing.status!r}: {task_id}",
                    "invalid_state",
                )
            result = retry_tasks_matching(
                connection,
                status=existing.status,
                scheduled_at=scheduled_iso,
                dry_run=dry_run,
                task_id=task_id,
            )
            if dry_run:
                output(
                    {"task_retry": result.model_dump(mode="json")},
                    record_count=result.retried_count,
                )
                return
            if result.retried_count == 0:
                output_error(
                    f"task not retryable in status {existing.status!r}: {task_id}",
                    "invalid_state",
                )
            reset = result.reset_task
            if reset is None:
                output_error(
                    f"task retry did not return updated row: {task_id}",
                    "internal_error",
                )
            output_entity("task", reset)
            return

        resolved_workflow_id, contact_id = _resolve_task_scope(
            connection, workflow_id, contact_email
        )
        result = retry_tasks_matching(
            connection,
            workflow_id=resolved_workflow_id,
            contact_id=contact_id,
            status=status if status is not None else "failed",
            trigger=trigger,
            overdue=overdue,
            since=since,
            until=until,
            touches=list(touches) if touches else None,
            scheduled_at=scheduled_iso,
            dry_run=dry_run,
        )
        output(
            {"task_retry": result.model_dump(mode="json")},
            record_count=result.retried_count,
        )
