"""Task CRUD, filter matching, and retry/cancel."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.sql import SQL, Composable
from psycopg.types.json import Json

from mailpilot.database._common import (
    _new_id,
)
from mailpilot.database._sql import (
    _sql_outbound_sent_count,
    _sql_resolve_touch,
)
from mailpilot.models import (
    Email,
    Task,
    TaskCancelResult,
    TaskRetryCompany,
    TaskRetryResult,
    TaskStats,
    TaskSummary,
)

# -- Task ----------------------------------------------------------------------


def create_task(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
    workflow_id: str,
    contact_id: str,
    description: str,
    scheduled_at: str,
    context: dict[str, object] | None = None,
    email_id: str | None = None,
    *,
    commit: bool = True,
) -> Task:
    """Create a deferred task.

    Per §V.28: every task row belongs to an enrollment. ``enrollment_id``
    is NOT NULL at the schema level; callers resolve it from the
    ``(workflow_id, contact_id)`` UNIQUE pair before invoking this fn.
    The denormalised ``workflow_id`` + ``contact_id`` columns stay for
    filter-path and dashboard compat.

    Args:
        connection: Open database connection.
        enrollment_id: Enrollment FK (NOT NULL per schema).
        workflow_id: Workflow FK (denormalised from enrollment row).
        contact_id: Contact FK (denormalised from enrollment row).
        description: What the agent should do.
        scheduled_at: When to execute (ISO timestamp).
        context: Arbitrary JSON context for the agent.
        email_id: Optional triggering email FK.
        commit: When False, leave the insert uncommitted for a caller txn.

    Returns:
        Created task.
    """
    row = connection.execute(
        """\
        INSERT INTO task (id, enrollment_id, workflow_id, contact_id, email_id,
            description, context, scheduled_at)
        VALUES (%(id)s, %(enrollment_id)s, %(workflow_id)s, %(contact_id)s,
                %(email_id)s, %(description)s, %(context)s, %(scheduled_at)s)
        RETURNING *
        """,
        {
            "id": _new_id(),
            "enrollment_id": enrollment_id,
            "workflow_id": workflow_id,
            "contact_id": contact_id,
            "email_id": email_id,
            "description": description,
            "context": Json(context or {}),
            "scheduled_at": scheduled_at,
        },
    ).fetchone()
    if commit:
        connection.commit()
    return Task.model_validate(row)


def get_task(
    connection: psycopg.Connection[dict[str, Any]],
    task_id: str,
) -> Task | None:
    """Get a task by ID.

    Args:
        connection: Open database connection.
        task_id: Task ID.

    Returns:
        Task if found, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM task WHERE id = %(id)s",
        {"id": task_id},
    ).fetchone()
    if row is None:
        return None
    return Task.model_validate(row)


def list_pending_tasks(
    connection: psycopg.Connection[dict[str, Any]],
) -> list[Task]:
    """List tasks due for execution.

    Args:
        connection: Open database connection.

    Returns:
        Pending tasks where scheduled_at <= now(), ordered by scheduled_at.
    """
    rows = connection.execute(
        """\
        SELECT * FROM task
        WHERE scheduled_at <= CURRENT_TIMESTAMP AND status = 'pending'
        ORDER BY scheduled_at
        """
    ).fetchall()
    return [Task.model_validate(row) for row in rows]


def find_pending_first_touch_task(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
) -> Task | None:
    """Return a pending first-touch task for ``enrollment_id`` if any.

    A first-touch task is the CLI-scheduled initial outbound send per §V.32:
    ``email_id IS NULL`` (not tied to a triggering inbound email) and
    ``status='pending'`` (not yet drained, not cancelled, not failed). Used
    by ``mailpilot enrollment add --scheduled-at ...`` to locate the queued
    first-reach (insert or last-write-wins UPDATE). Keyed on scalar
    ``enrollment_id`` per §V.32 post-migration.
    """
    row = connection.execute(
        """\
        SELECT * FROM task
        WHERE enrollment_id = %(enrollment_id)s
          AND email_id IS NULL
          AND status = 'pending'
        ORDER BY scheduled_at
        LIMIT 1
        """,
        {"enrollment_id": enrollment_id},
    ).fetchone()
    if row is None:
        return None
    return Task.model_validate(row)


def count_outbound_sent(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> int:
    """Count sent outbound emails for a workflow/contact pair.

    Same grain as the ``emails_sent`` projection on enrollment ``--full``
    (§V.152). Used by ``enrollment add --scheduled-at`` to refuse moving a
    first-reach after a send (§V.32).
    """
    row = connection.execute(
        SQL(
            "SELECT {count} AS n "
            "FROM (VALUES (%(workflow_id)s, %(contact_id)s)) "
            "AS e(workflow_id, contact_id)"
        ).format(count=_sql_outbound_sent_count(SQL("e"))),
        {"workflow_id": workflow_id, "contact_id": contact_id},
    ).fetchone()
    if row is None:
        return 0
    return int(row["n"])


def update_pending_first_touch_schedule(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    task: Task,
    scheduled_at: str,
    commit: bool = True,
) -> Task | None:
    """Move a pending first-reach ``scheduled_at`` in place (§V.32).

    Persists numeric ``context.touch = 1`` when absent so later SQL parsers
    do not need the enrollment_schedule fallback (§V.162). Same task id.
    """
    from mailpilot.cadence import parse_touch_number

    context = dict(task.context)
    if parse_touch_number(context.get("touch")) is None:
        context["touch"] = 1
    row = connection.execute(
        """\
        UPDATE task
        SET scheduled_at = %(scheduled_at)s,
            context = %(context)s
        WHERE id = %(id)s AND status = 'pending'
        RETURNING *
        """,
        {
            "id": task.id,
            "scheduled_at": scheduled_at,
            "context": Json(context),
        },
    ).fetchone()
    if commit:
        connection.commit()
    if row is None:
        return None
    return Task.model_validate(row)


def complete_task(
    connection: psycopg.Connection[dict[str, Any]],
    task_id: str,
    status: str = "completed",
    result: dict[str, object] | None = None,
) -> Task | None:
    """Mark a task as completed or failed, optionally storing a result.

    Args:
        connection: Open database connection.
        task_id: Task ID.
        status: "completed" or "failed".
        result: Agent reasoning and outcome to persist.

    Returns:
        Updated task, or None if not found.
    """
    result_json = result or {}
    row = connection.execute(
        """\
        UPDATE task
        SET status = %(status)s,
            result = %(result)s,
            completed_at = CURRENT_TIMESTAMP
        WHERE id = %(id)s RETURNING *
        """,
        {
            "id": task_id,
            "status": status,
            "result": Json(result_json),
        },
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Task.model_validate(row)


def cancel_task(
    connection: psycopg.Connection[dict[str, Any]],
    task_id: str,
) -> Task | None:
    """Cancel a pending task.

    Only cancels tasks with status 'pending'. Already completed or failed
    tasks are not affected.

    Args:
        connection: Open database connection.
        task_id: Task ID.

    Returns:
        Cancelled task, or None if not found or not pending.
    """
    row = connection.execute(
        """\
        UPDATE task SET status = 'cancelled', completed_at = CURRENT_TIMESTAMP
        WHERE id = %(id)s AND status = 'pending'
        RETURNING *
        """,
        {"id": task_id},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Task.model_validate(row)


def cancel_enrollment_followup_tasks(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
) -> list[Task]:
    """Cancel an enrollment's pending future follow-up tasks (§V.123).

    Bulk-cancels every ``pending`` task for ``enrollment_id`` whose
    ``scheduled_at`` is still in the future, excluding the operator
    first-touch task (the row carrying ``context->>'trigger' =
    'enrollment_schedule'`` per §V.32). Called from ``routing.route_email``
    when an inbound reply routes to the enrollment: the prospect engaged,
    so any later cold follow-up touch is cancelled before it wakes.

    Already-due tasks (``scheduled_at <= now``) and non-pending tasks are
    left untouched; status moves ``pending`` -> ``cancelled`` (mirrors
    ``cancel_task``). Agent-created follow-ups may carry a NULL
    ``email_id``, so the first-touch is identified by the trigger label,
    not by ``email_id``.

    Args:
        connection: Open database connection.
        enrollment_id: Enrollment whose follow-up tasks to cancel.

    Returns:
        The cancelled tasks, ordered by scheduled_at; empty when none matched.
    """
    rows = connection.execute(
        """\
        UPDATE task SET status = 'cancelled', completed_at = CURRENT_TIMESTAMP
        WHERE enrollment_id = %(enrollment_id)s
          AND status = 'pending'
          AND scheduled_at > CURRENT_TIMESTAMP
          AND COALESCE(context->>'trigger', '') <> 'enrollment_schedule'
        RETURNING *
        """,
        {"enrollment_id": enrollment_id},
    ).fetchall()
    connection.commit()
    return [Task.model_validate(row) for row in rows]


def _task_filter_clauses(
    params: dict[str, object],
    *,
    workflow_id: str | None = None,
    contact_id: str | None = None,
    status: str | None = None,
    trigger: str | None = None,
    overdue: bool = False,
    since: str | None = None,
    until: str | None = None,
    touches: Sequence[int] | None = None,
) -> list[Composable]:
    """Shared WHERE clauses for task list, cancel, retry, and stats.

    Touch match uses ``_sql_resolve_touch`` (parse §V.162 + first-touch
    fallback). Never filters on ``description``.
    """
    conditions: list[Composable] = []
    if workflow_id is not None:
        conditions.append(SQL("workflow_id = %(workflow_id)s"))
        params["workflow_id"] = workflow_id
    if contact_id is not None:
        conditions.append(SQL("contact_id = %(contact_id)s"))
        params["contact_id"] = contact_id
    if overdue:
        conditions.append(SQL("status = 'pending'"))
        conditions.append(SQL("scheduled_at < NOW()"))
    if status is not None:
        conditions.append(SQL("status = %(status)s"))
        params["status"] = status
    if trigger is not None:
        conditions.append(SQL("COALESCE(context->>'trigger', '') = %(trigger)s"))
        params["trigger"] = trigger
    if since is not None:
        conditions.append(SQL("scheduled_at >= %(since)s"))
        params["since"] = since
    if until is not None:
        conditions.append(SQL("scheduled_at <= %(until)s"))
        params["until"] = until
    if touches:
        conditions.append(
            SQL("{} = ANY(%(touches)s)").format(_sql_resolve_touch(SQL("context")))
        )
        params["touches"] = list(touches)
    return conditions


def list_tasks(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str | None = None,
    contact_id: str | None = None,
    status: str | None = None,
    trigger: str | None = None,
    limit: int = 100,
    since: str | None = None,
    until: str | None = None,
    *,
    overdue: bool = False,
    touches: Sequence[int] | None = None,
) -> list[TaskSummary]:
    """List tasks as summaries with optional filters.

    Args:
        connection: Open database connection.
        workflow_id: Filter by workflow ID.
        contact_id: Filter by contact ID.
        status: Filter by task status.
        trigger: Filter by caller path stored in ``context->>'trigger'``
            (§V.26 taxonomy); deterministic first-touch select on
            ``enrollment_schedule`` (§V.32), never reads ``description``.
        limit: Maximum results.
        since: ISO datetime inclusive lower bound on ``scheduled_at``.
        until: ISO datetime inclusive upper bound on ``scheduled_at``.
        overdue: When True (§V.155), only pending tasks with
            ``scheduled_at < now()``.
        touches: When set, only tasks whose resolved touch is in this set
            (parse §V.162; also first-touch trigger fallback).

    Returns:
        List of task summaries ordered by scheduled_at descending.
    """
    params: dict[str, object] = {"limit": limit}
    conditions = _task_filter_clauses(
        params,
        workflow_id=workflow_id,
        contact_id=contact_id,
        status=status,
        trigger=trigger,
        overdue=overdue,
        since=since,
        until=until,
        touches=touches,
    )
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT id, enrollment_id, workflow_id, contact_id, email_id, "
        "description, scheduled_at, status, attempt_count, "
        "jsonb_build_object('reason', result->>'reason') AS result "
        "FROM task {} ORDER BY scheduled_at DESC LIMIT %(limit)s"
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [TaskSummary.model_validate(row) for row in rows]


def cancel_tasks_matching(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    workflow_id: str | None = None,
    contact_id: str | None = None,
    trigger: str | None = None,
    overdue: bool = False,
    since: str | None = None,
    until: str | None = None,
    touches: Sequence[int] | None = None,
) -> TaskCancelResult:
    """Cancel every matching pending task in one transaction (§V.173).

    No default limit. ``leftover_pending_by_touch`` uses the same scope
    filters except ``touches``, so a ``--touch 2`` cancel still reports
    remaining T1/T3 pending. Zero matches is an ok no-op.

    Args:
        connection: Open database connection.
        workflow_id: Filter by workflow ID.
        contact_id: Filter by contact ID.
        trigger: Filter by ``context->>'trigger'``.
        overdue: When True, only pending tasks with ``scheduled_at < now()``.
        since: Inclusive lower bound on ``scheduled_at``.
        until: Inclusive upper bound on ``scheduled_at``.
        touches: Resolved touch numbers to cancel (parse §V.162).

    Returns:
        Join envelope: cancelled ids plus leftover pending-by-touch.
    """
    params: dict[str, object] = {}
    conditions = _task_filter_clauses(
        params,
        workflow_id=workflow_id,
        contact_id=contact_id,
        status="pending",
        trigger=trigger,
        overdue=overdue,
        since=since,
        until=until,
        touches=touches,
    )
    where = SQL("WHERE ") + SQL(" AND ").join(conditions)
    rows = connection.execute(
        SQL(
            "UPDATE task SET status = 'cancelled', completed_at = CURRENT_TIMESTAMP "
            "{} RETURNING id"
        ).format(where),
        params,
    ).fetchall()
    ids = sorted(str(row["id"]) for row in rows)

    leftover_params: dict[str, object] = {}
    leftover_conditions = _task_filter_clauses(
        leftover_params,
        workflow_id=workflow_id,
        contact_id=contact_id,
        status="pending",
        trigger=trigger,
        overdue=overdue,
        since=since,
        until=until,
        touches=None,
    )
    touch_expr = _sql_resolve_touch(SQL("context"))
    leftover_conditions.append(SQL("{} IS NOT NULL").format(touch_expr))
    leftover_where = SQL("WHERE ") + SQL(" AND ").join(leftover_conditions)
    leftover_rows = connection.execute(
        SQL(
            "SELECT {} AS touch, COUNT(*)::int AS n FROM task {} GROUP BY 1 ORDER BY 1"
        ).format(touch_expr, leftover_where),
        leftover_params,
    ).fetchall()
    leftover = {str(row["touch"]): int(row["n"]) for row in leftover_rows}
    connection.commit()
    return TaskCancelResult(
        cancelled_count=len(ids),
        ids=ids,
        leftover_pending_by_touch=leftover,
    )


def retry_tasks_matching(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    workflow_id: str | None = None,
    contact_id: str | None = None,
    status: str = "failed",
    trigger: str | None = None,
    overdue: bool = False,
    since: str | None = None,
    until: str | None = None,
    touches: Sequence[int] | None = None,
    scheduled_at: str | None = None,
    dry_run: bool = False,
    task_id: str | None = None,
) -> TaskRetryResult:
    """Retry every matching failed or cancelled task in one txn (§V.175).

    No default limit. ``--scheduled-at`` (when set) is the same instant
    for every selected row; omit it to apply §V.170 per row (keep a
    still-future stored time, else now). ``dry_run`` selects ids and
    companies with no writes. Zero matches is an ok no-op.

    Args:
        connection: Open database connection.
        workflow_id: Filter by workflow ID.
        contact_id: Filter by contact ID.
        status: ``failed`` (default) or ``cancelled``.
        trigger: Filter by ``context->>'trigger'``.
        overdue: When True, compose with task-list overdue (pending +
            past ``scheduled_at``); that conjunction is typically empty.
        since: Inclusive lower bound on ``scheduled_at``.
        until: Inclusive upper bound on ``scheduled_at``.
        touches: Resolved touch numbers to retry (parse §V.162).
        scheduled_at: Optional ISO override applied to every selected row.
        dry_run: When True, preview only (no UPDATE).
        task_id: Optional single-id restriction (id-mode retry).

    Returns:
        Join envelope: retried ids, override scheduled_at, companies.
        Id-mode writes set ``reset_task`` from ``RETURNING *``.
    """
    params: dict[str, object] = {}
    conditions = _task_filter_clauses(
        params,
        workflow_id=workflow_id,
        contact_id=contact_id,
        status=status,
        trigger=trigger,
        overdue=overdue,
        since=since,
        until=until,
        touches=touches,
    )
    if task_id is not None:
        conditions.append(SQL("id = %(task_id)s"))
        params["task_id"] = task_id
    conditions.append(SQL("status IN ('failed', 'cancelled')"))
    where = SQL("WHERE ") + SQL(" AND ").join(conditions)

    if dry_run:
        rows = connection.execute(
            SQL("SELECT id FROM task {}").format(where),
            params,
        ).fetchall()
    else:
        params["scheduled_at"] = scheduled_at
        rows = connection.execute(
            SQL(
                """\
                UPDATE task
                SET status = 'pending',
                    attempt_count = 0,
                    scheduled_at = CASE
                        WHEN %(scheduled_at)s::text IS NOT NULL
                            THEN (%(scheduled_at)s::text)::timestamptz
                        WHEN scheduled_at > CURRENT_TIMESTAMP THEN scheduled_at
                        ELSE CURRENT_TIMESTAMP
                    END,
                    completed_at = NULL
                {}
                RETURNING *
                """
            ).format(where),
            params,
        ).fetchall()

    ids = sorted(str(row["id"]) for row in rows)
    reset_task: Task | None = None
    if task_id is not None and not dry_run and len(rows) == 1:
        reset_task = Task.model_validate(rows[0])
    companies: list[TaskRetryCompany] = []
    if ids:
        company_rows = connection.execute(
            """\
            SELECT co.domain AS domain, COUNT(*)::int AS n
            FROM task t
            JOIN contact c ON c.id = t.contact_id
            JOIN company co ON co.id = c.company_id
            WHERE t.id = ANY(%(ids)s)
            GROUP BY co.domain
            ORDER BY co.domain
            """,
            {"ids": ids},
        ).fetchall()
        companies = [
            TaskRetryCompany(domain=str(row["domain"]), count=int(row["n"]))
            for row in company_rows
        ]
    if not dry_run:
        connection.commit()
    return TaskRetryResult(
        retried_count=len(ids),
        ids=ids,
        scheduled_at=scheduled_at,
        companies=companies,
        dry_run=dry_run,
        reset_task=reset_task,
    )


def get_task_stats(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str | None = None,
    trigger: str | None = None,
    bucket_tz: str = "UTC",
) -> TaskStats:
    """Compute the task-cadence aggregate over the task queue (§V.133).

    A single deterministic SQL aggregate at task grain -- no LLM. Returns
    per-status counts {pending, completed, failed, cancelled} plus ``total``,
    the count of distinct calendar days the tasks are scheduled across (bucketed
    in ``bucket_tz``), and the first/last ``scheduled_at``. Optional
    ``workflow_id`` and ``trigger`` narrow the task set before aggregation, the
    same filter axes ``list_tasks`` carries.

    ``distinct_scheduled_days`` buckets each ``scheduled_at`` (a ``TIMESTAMPTZ``)
    into its wall-clock date in ``bucket_tz`` -- ``AT TIME ZONE`` shifts the
    instant into that zone before truncating to a date, so a midnight-straddling
    instant lands on the operator's local day, not UTC's. The window filters
    (§V.115 lifecycle) stay on ``list_tasks``; this aggregate keeps to the
    cadence question.

    Args:
        connection: Open database connection.
        workflow_id: Filter by workflow ID (entity ref per §V.107).
        trigger: Filter by ``context->>'trigger'`` (§V.26 taxonomy);
            ``enrollment_schedule`` selects the first-touch tasks (§V.32).
        bucket_tz: IANA timezone name for day-bucketing ``distinct_scheduled_days``
            (caller validates; an unknown zone raises at query time).

    Returns:
        ``TaskStats`` over the filtered task set (all-zero counts and NULL
        first/last when no task matches).
    """
    params: dict[str, object] = {"bucket_tz": bucket_tz}
    conditions = _task_filter_clauses(
        params,
        workflow_id=workflow_id,
        trigger=trigger,
    )
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        """\
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE status = 'pending') AS pending,
            COUNT(*) FILTER (WHERE status = 'completed') AS completed,
            COUNT(*) FILTER (WHERE status = 'failed') AS failed,
            COUNT(*) FILTER (WHERE status = 'cancelled') AS cancelled,
            COUNT(DISTINCT (scheduled_at AT TIME ZONE %(bucket_tz)s)::date)
                AS distinct_scheduled_days,
            MIN(scheduled_at) AS first_scheduled_at,
            MAX(scheduled_at) AS last_scheduled_at
        FROM task {}
        """
    ).format(where)
    row = connection.execute(query, params).fetchone()
    assert row is not None  # an aggregate without GROUP BY always returns one row
    return TaskStats.model_validate(row)


def reschedule_task_for_retry(
    connection: psycopg.Connection[dict[str, Any]],
    task_id: str,
    backoff_seconds: int,
    exc: BaseException,
) -> Task | None:
    """Reschedule a transient-failure task for another attempt.

    Status remains ``pending``. ``attempt_count`` is incremented and
    ``scheduled_at`` is advanced by ``backoff_seconds``. The row's
    ``result`` JSON captures a summary of the last failure so an
    operator inspecting the row mid-retry-loop sees what's been tried.

    Args:
        connection: Open database connection.
        task_id: Task ID.
        backoff_seconds: Delay to add before the next attempt fires.
        exc: Exception from the failed attempt; used to populate the
            ``result.last_error`` summary.

    Returns:
        Updated task, or ``None`` if the row does not exist.
    """
    summary = {
        "last_error": {
            "type": type(exc).__name__,
            "message": str(exc),
        },
    }
    row = connection.execute(
        """\
        UPDATE task
        SET attempt_count = attempt_count + 1,
            scheduled_at = CURRENT_TIMESTAMP + (%(delay)s || ' seconds')::interval,
            result = %(result)s
        WHERE id = %(id)s
        RETURNING *
        """,
        {
            "id": task_id,
            "delay": str(backoff_seconds),
            "result": Json(summary),
        },
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Task.model_validate(row)


def reschedule_task_for_lock_contention(
    connection: psycopg.Connection[dict[str, Any]],
    task_id: str,
    backoff_seconds: int,
) -> Task | None:
    """Push ``scheduled_at`` forward without bumping ``attempt_count``.

    Used when the agent advisory lock was held by another worker (§V.25).
    Lock contention is not a retry: the task ran nothing, side-effect
    budget is untouched, ``attempt_count`` stays put. Bumping
    ``scheduled_at`` fires the ``task_pending_trigger`` ``UPDATE`` notify
    (§V.49 trigger extension) so the drain loop wakes again instead of
    leaving the task ``pending`` with no signal (§B.42).

    Args:
        connection: Open database connection.
        task_id: Task ID.
        backoff_seconds: Delay before the next attempt fires.

    Returns:
        Updated task, or ``None`` if the row does not exist.
    """
    row = connection.execute(
        """\
        UPDATE task
        SET scheduled_at = CURRENT_TIMESTAMP + (%(delay)s || ' seconds')::interval
        WHERE id = %(id)s
        RETURNING *
        """,
        {
            "id": task_id,
            "delay": str(backoff_seconds),
        },
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Task.model_validate(row)


def get_unprocessed_inbound_email(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> Email | None:
    """Return the most recent inbound email for a contact+workflow without a task.

    Uses the same filtering logic as ``create_tasks_for_routed_emails`` but
    scoped to a single contact and returning at most one email.

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        contact_id: Contact FK.

    Returns:
        The most recent unprocessed inbound email, or None.
    """
    row = connection.execute(
        """\
        SELECT e.* FROM email e
        JOIN workflow w ON w.id = e.workflow_id
        WHERE e.direction = 'inbound'
          AND e.workflow_id = %(workflow_id)s
          AND e.contact_id = %(contact_id)s
          AND e.created_at >= w.created_at
          AND NOT EXISTS (SELECT 1 FROM task t WHERE t.email_id = e.id)
        ORDER BY e.created_at DESC
        LIMIT 1
        """,
        {"workflow_id": workflow_id, "contact_id": contact_id},
    ).fetchone()
    if row is None:
        return None
    return Email.model_validate(row)


def create_tasks_for_routed_emails(
    connection: psycopg.Connection[dict[str, Any]],
) -> list[Task]:
    """Create immediate tasks for routed inbound emails without tasks.

    Finds inbound emails with workflow_id set but no corresponding task
    row, and creates a task with scheduled_at=now() for each. Joins
    ``enrollment`` so ``task.enrollment_id`` is populated per §V.28 --
    the enrollment row is guaranteed present because
    ``routing._ensure_enrollment`` runs earlier in the inbound pipeline.

    Mechanical OOO on an outbound enrollment is skipped (§V.188): resume
    was already scheduled in routing, and a processed marker with
    ``email_id`` set keeps later sync from re-enqueueing. Language-only
    OOO still gets ``handle inbound email``.

    Uses ``e.created_at`` (DB insert time) rather than ``e.received_at``
    (Gmail timestamp) to filter historical emails. An email can be received
    by Gmail before a workflow exists but synced into our DB after -- using
    ``received_at`` would incorrectly skip such emails.

    Args:
        connection: Open database connection.

    Returns:
        List of newly created inbound agent tasks.
    """
    from mailpilot.ooo import is_mechanical_ooo

    unmatched = connection.execute(
        """\
        SELECT e.id, e.workflow_id, e.contact_id, e.account_id, e.direction,
               e.subject, e.body_text, e.labels, e.created_at,
               en.id AS enrollment_id, w.type AS workflow_type
        FROM email e
        JOIN workflow w ON w.id = e.workflow_id
        JOIN enrollment en
          ON en.workflow_id = e.workflow_id AND en.contact_id = e.contact_id
        WHERE e.direction = 'inbound'
          AND e.contact_id IS NOT NULL
          AND e.created_at >= w.created_at
          AND NOT EXISTS (SELECT 1 FROM task t WHERE t.email_id = e.id)
        ORDER BY e.created_at
        """
    ).fetchall()
    tasks: list[Task] = []
    for email_row in unmatched:
        email = Email.model_validate(
            {
                "id": email_row["id"],
                "account_id": email_row["account_id"],
                "direction": email_row["direction"],
                "subject": email_row["subject"],
                "body_text": email_row["body_text"],
                "labels": email_row["labels"] or [],
                "created_at": email_row["created_at"],
                "contact_id": email_row["contact_id"],
                "workflow_id": email_row["workflow_id"],
            }
        )
        if email_row["workflow_type"] == "outbound" and is_mechanical_ooo(email):
            _mark_mechanical_ooo_processed(
                connection,
                enrollment_id=email_row["enrollment_id"],
                workflow_id=email_row["workflow_id"],
                contact_id=email_row["contact_id"],
                email_id=email_row["id"],
            )
            continue
        now = datetime.now(UTC).isoformat()
        t = create_task(
            connection,
            enrollment_id=email_row["enrollment_id"],
            workflow_id=email_row["workflow_id"],
            contact_id=email_row["contact_id"],
            description="handle inbound email",
            scheduled_at=now,
            email_id=email_row["id"],
        )
        tasks.append(t)
    return tasks


def _mark_mechanical_ooo_processed(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    enrollment_id: str,
    workflow_id: str,
    contact_id: str,
    email_id: str,
) -> None:
    """Insert a completed processed marker so later sync skips this inbound."""
    now = datetime.now(UTC).isoformat()
    task = create_task(
        connection,
        enrollment_id=enrollment_id,
        workflow_id=workflow_id,
        contact_id=contact_id,
        description="mechanical ooo",
        scheduled_at=now,
        email_id=email_id,
        commit=False,
    )
    complete_task(
        connection,
        task.id,
        status="completed",
        result={"reason": "ooo_pause"},
    )
