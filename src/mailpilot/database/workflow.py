"""Workflow CRUD, stats, check, review, and queue."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.sql import SQL, Composed
from psycopg.types.json import Json

from mailpilot.database._common import (
    _build_update,
    _new_id,
)
from mailpilot.database._sql import (
    _enrollment_outcome_lateral,
    _sql_outbound_sent_count,
    _sql_resolve_touch,
)
from mailpilot.database.email import (
    list_emails,
)
from mailpilot.database.enrollment import (
    list_enrollments_detailed,
)
from mailpilot.database.status import (
    _sync_loop_block,
)
from mailpilot.database.task import (
    get_task_stats,
)
from mailpilot.models import (
    QueueReport,
    QueueTaskKind,
    QueueTaskRow,
    QueueWorkflowRow,
    TouchCopy,
    TouchStageCounts,
    Workflow,
    WorkflowCheck,
    WorkflowCheckEntry,
    WorkflowReport,
    WorkflowReportMeta,
    WorkflowReview,
    WorkflowReviewActivity,
    WorkflowReviewFailedTask,
    WorkflowReviewItem,
    WorkflowReviewTaskCounts,
    WorkflowStats,
    WorkflowStatusHealth,
    WorkflowSummary,
)

# -- Workflow ------------------------------------------------------------------


def create_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    template: str,
    account_id: str,
    theme: str = "blue",
) -> Workflow | None:
    """Create a new workflow.

    The workflow's ``type`` (``inbound`` / ``outbound``) is derived from
    the template's declared direction -- callers do not pass ``type``.

    Uses ``ON CONFLICT (name) DO NOTHING`` per §V.16(+) so callers can safely
    re-invoke without catching ``UniqueViolation``. ``name`` is globally unique
    (§V.90/§V.103), so a collision against any account returns ``None``.

    Args:
        connection: Open database connection.
        name: Workflow name. Globally unique, kebab-shaped (§V.90/§V.103).
        template: Template name (e.g. ``outbound-general``). Drives both
            the agent shape and the workflow's direction.
        account_id: Account FK.
        theme: Email color theme (default "blue").

    Returns:
        Created workflow, or ``None`` if a workflow with this ``name``
        already existed.
    """
    from mailpilot.agent.templates import TEMPLATES

    if template not in TEMPLATES:
        raise ValueError(
            f"unknown workflow template {template!r}; valid: {sorted(TEMPLATES.keys())}"
        )
    direction = TEMPLATES[template].direction  # pyright: ignore[reportArgumentType]
    row = connection.execute(
        """\
        WITH inserted AS (
            INSERT INTO workflow (id, name, template, type, account_id, theme)
            VALUES (
                %(id)s, %(name)s, %(template)s, %(type)s, %(account_id)s, %(theme)s
            )
            ON CONFLICT (name) DO NOTHING
            RETURNING *
        )
        SELECT inserted.*, account.email AS account_email
        FROM inserted JOIN account ON account.id = inserted.account_id
        """,
        {
            "id": _new_id(),
            "name": name,
            "template": template,
            "type": direction,
            "account_id": account_id,
            "theme": theme,
        },
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Workflow.model_validate(row)


def get_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> Workflow | None:
    """Get a workflow by ID.

    Args:
        connection: Open database connection.
        workflow_id: Workflow ID.

    Returns:
        Workflow if found, None otherwise.
    """
    row = connection.execute(
        """\
        SELECT workflow.*, account.email AS account_email
        FROM workflow JOIN account ON account.id = workflow.account_id
        WHERE workflow.id = %(id)s
        """,
        {"id": workflow_id},
    ).fetchone()
    if row is None:
        return None
    return Workflow.model_validate(row)


def get_workflow_by_name(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
) -> Workflow | None:
    """Resolve a workflow by its globally unique ``name`` (§V.90/§V.107).

    The name is the canonical cross-environment key (§V.103). Stored names are
    kebab-shaped (lowercase), so the lookup lowercases the input to resolve the
    natural key case-insensitively, mirroring the CLI polymorphic resolver.
    Returns ``None`` when no workflow carries the name -- the caller surfaces
    ``not_found``.

    Args:
        connection: Open database connection.
        name: Workflow name (case-insensitive).

    Returns:
        Workflow if found, None otherwise.
    """
    row = connection.execute(
        """\
        SELECT workflow.*, account.email AS account_email
        FROM workflow JOIN account ON account.id = workflow.account_id
        WHERE workflow.name = %(name)s
        """,
        {"name": name.lower()},
    ).fetchone()
    if row is None:
        return None
    return Workflow.model_validate(row)


_WORKFLOW_SUMMARY_COLUMNS = SQL(
    "workflow.id, workflow.name, workflow.template, workflow.type, "
    "workflow.account_id, account.email AS account_email, "
    "workflow.status, workflow.created_at"
)


def list_workflows(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str | None = None,
    status: str | None = None,
    workflow_type: str | None = None,
    template: str | None = None,
    limit: int = 100,
    since: str | None = None,
    until: str | None = None,
) -> list[WorkflowSummary]:
    """List workflows as summaries with optional filters.

    Args:
        connection: Open database connection.
        account_id: Filter by account ID.
        status: Filter by workflow status (e.g., "active").
        workflow_type: Filter by workflow type ("inbound" or "outbound").
        template: Filter by template name.
        limit: Maximum results.
        since: ISO datetime inclusive lower bound on ``created_at``.
        until: ISO datetime inclusive upper bound on ``created_at``.

    Returns:
        List of workflow summaries ordered by creation time.
    """
    conditions: list[SQL] = []
    params: dict[str, object] = {"limit": limit}
    if account_id is not None:
        conditions.append(SQL("workflow.account_id = %(account_id)s"))
        params["account_id"] = account_id
    if status is not None:
        conditions.append(SQL("workflow.status = %(status)s"))
        params["status"] = status
    if workflow_type is not None:
        conditions.append(SQL("workflow.type = %(workflow_type)s"))
        params["workflow_type"] = workflow_type
    if template is not None:
        conditions.append(SQL("workflow.template = %(template)s"))
        params["template"] = template
    if since is not None:
        conditions.append(SQL("workflow.created_at >= %(since)s"))
        params["since"] = since
    if until is not None:
        conditions.append(SQL("workflow.created_at <= %(until)s"))
        params["until"] = until
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT {} "
        "FROM workflow JOIN account ON account.id = workflow.account_id "
        "{} ORDER BY workflow.created_at LIMIT %(limit)s"
    ).format(_WORKFLOW_SUMMARY_COLUMNS, where)
    rows = connection.execute(query, params).fetchall()
    return [WorkflowSummary.model_validate(row) for row in rows]


def list_workflows_full(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str | None = None,
) -> list[Workflow]:
    """List workflows as full rows ordered by name.

    Used by ``workflow export`` (account-scoped) to emit a declarative payload
    keyed on the globally unique ``name`` (§V.90/§V.103) and by
    ``workflow check`` (account omitted -> every row) to join the live rows
    against the catalog by ``name`` (§V.134). Ordering by ``name`` makes the
    output deterministic for diffs and round-trip testing.

    Args:
        connection: Open database connection.
        account_id: Owning account ID; ``None`` lists every account's rows.

    Returns:
        Full ``Workflow`` rows ordered by ``name``.
    """
    where = SQL("WHERE workflow.account_id = %(account_id)s") if account_id else SQL("")
    query = SQL(
        "SELECT workflow.*, account.email AS account_email "
        "FROM workflow JOIN account ON account.id = workflow.account_id "
        "{} ORDER BY workflow.name"
    ).format(where)
    rows = connection.execute(query, {"account_id": account_id}).fetchall()
    return [Workflow.model_validate(row) for row in rows]


def get_workflow_stats(
    connection: psycopg.Connection[dict[str, Any]],
    workflow: Workflow,
) -> WorkflowStats:
    """Compute the per-campaign funnel for one already-loaded workflow (§V.132).

    A single deterministic SQL aggregate over the workflow's enrollments -- no
    LLM. The enrollment row (one per contact) is the grain, so each stage is
    contact-distinct and a multi-touch outbound sequence never double-counts.
    Eight stages:

    - ``enrolled``: the workflow's enrollment rows.
    - ``sent`` / ``bounced``: enrollments with at least one outbound email of
      that status (send auto-resolves ``email.contact_id`` from the recipient,
      ``email.workflow_id`` is set at spawn).
    - ``replied``: enrollments with at least one routed inbound email (routing
      sets ``contact_id`` + ``workflow_id`` per §V.27).
    - ``meeting_booked``: enrollments whose latest terminal outcome is
      ``enrollment_completed`` (disposition-independent -- completed maps only
      to meeting_booked).
    - ``contact_later`` / ``do_not_contact``: enrollments whose latest terminal
      outcome is ``enrollment_failed``, split by ``detail->>'disposition'``.
    - ``active``: ``status='active'`` enrollments with no terminal outcome.

    Outcomes are timeline-only (§V.15): the latest ``enrollment_completed`` /
    ``enrollment_failed`` activity per enrollment wins (same LATERAL as
    ``list_enrollments_detailed(full=True)`` §V.185). Pre-§V.132 failed rows
    lack a disposition key, so they fall out of both failure splits (legacy
    gap).

    Args:
        connection: Open database connection.
        workflow: Loaded workflow row (CLI resolves the entity ref first).

    Returns:
        ``WorkflowStats`` for the workflow.
    """
    sent_count = _sql_outbound_sent_count(SQL("e"))
    row = connection.execute(
        SQL(
            """\
            WITH per_enrollment AS (
                SELECT
                    e.status,
                    {sent_count} > 0 AS has_sent,
                    EXISTS (
                        SELECT 1 FROM email
                        WHERE email.workflow_id = e.workflow_id
                          AND email.contact_id = e.contact_id
                          AND email.direction = 'outbound'
                          AND email.status = 'bounced'
                    ) AS has_bounced,
                    EXISTS (
                        SELECT 1 FROM email
                        WHERE email.workflow_id = e.workflow_id
                          AND email.contact_id = e.contact_id
                          AND email.direction = 'inbound'
                          AND email.is_routed = TRUE
                    ) AS has_replied,
                    outcome.latest_outcome,
                    outcome.disposition
                FROM enrollment e
                LEFT JOIN LATERAL (
                    SELECT
                        CASE a.type
                            WHEN 'enrollment_completed' THEN 'completed'
                            WHEN 'enrollment_failed' THEN 'failed'
                        END AS latest_outcome,
                        a.detail->>'disposition' AS disposition
                    FROM activity a
                    WHERE a.contact_id = e.contact_id
                      AND a.workflow_id = e.workflow_id
                      AND a.type IN ('enrollment_completed', 'enrollment_failed')
                    ORDER BY a.created_at DESC
                    LIMIT 1
                ) outcome ON TRUE
                WHERE e.workflow_id = %(workflow_id)s
            )
            SELECT
                COUNT(*) AS enrolled,
                COUNT(*) FILTER (WHERE has_sent) AS sent,
                COUNT(*) FILTER (WHERE has_bounced) AS bounced,
                COUNT(*) FILTER (WHERE has_replied) AS replied,
                COUNT(*) FILTER (WHERE latest_outcome = 'completed')
                    AS meeting_booked,
                COUNT(*) FILTER (
                    WHERE latest_outcome = 'failed'
                      AND disposition = 'contact_later'
                ) AS contact_later,
                COUNT(*) FILTER (
                    WHERE latest_outcome = 'failed'
                      AND disposition = 'do_not_contact'
                ) AS do_not_contact,
                COUNT(*) FILTER (
                    WHERE status = 'active' AND latest_outcome IS NULL
                ) AS active,
                COUNT(*) FILTER (
                    WHERE status = 'active'
                      AND latest_outcome IS NULL
                      AND NOT has_sent
                ) AS awaiting_first_touch,
                COUNT(*) FILTER (WHERE status = 'disabled') AS disabled
            FROM per_enrollment
            """
        ).format(sent_count=sent_count),
        {"workflow_id": workflow.id},
    ).fetchone()
    assert row is not None  # aggregate over a present workflow always returns 1 row

    touches: dict[str, TouchStageCounts] = {}
    configured_touches = workflow.touches
    if configured_touches is not None and configured_touches >= 1:
        touch_rows = connection.execute(
            SQL(
                """\
                WITH touch_nums AS (
                    SELECT generate_series(1, %(touches)s) AS touch
                )
                SELECT
                    tn.touch::text AS touch_key,
                    (
                        SELECT COUNT(*)::int FROM enrollment e
                        WHERE e.workflow_id = %(workflow_id)s
                          AND {sent_count} >= tn.touch
                    ) AS sent,
                    (
                        SELECT COUNT(*)::int FROM task t
                        WHERE t.workflow_id = %(workflow_id)s
                          AND t.status = 'pending'
                          AND {touch} = tn.touch
                    ) AS pending
                FROM touch_nums tn
                ORDER BY tn.touch
                """
            ).format(
                touch=_sql_resolve_touch(SQL("t.context")),
                sent_count=sent_count,
            ),
            {"workflow_id": workflow.id, "touches": configured_touches},
        ).fetchall()
        for tr in touch_rows:
            touches[tr["touch_key"]] = TouchStageCounts(
                sent=tr["sent"], pending=tr["pending"]
            )

    return WorkflowStats(
        workflow_id=workflow.id,
        workflow_name=workflow.name,
        enrolled=row["enrolled"],
        sent=row["sent"],
        bounced=row["bounced"],
        replied=row["replied"],
        meeting_booked=row["meeting_booked"],
        contact_later=row["contact_later"],
        do_not_contact=row["do_not_contact"],
        active=row["active"],
        touches=touches,
        awaiting_first_touch=row["awaiting_first_touch"],
        disabled=row["disabled"],
    )


def get_workflow_report(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    *,
    stuck: bool = False,
    touch: int | None = None,
    status: str | None = None,
    limit: int = 500,
) -> WorkflowReport | None:
    """Composite campaign report: funnel + tasks + enrollment matrix (§V.153).

    Pure SQL / deterministic reuses of ``get_workflow_stats``,
    ``get_task_stats``, and ``list_enrollments_detailed(full=True)``. No LLM,
    no CRM writes.
    """
    workflow = get_workflow(connection, workflow_id)
    if workflow is None:
        return None
    funnel = get_workflow_stats(connection, workflow)
    tasks = get_task_stats(connection, workflow_id=workflow_id)
    enrollments = list_enrollments_detailed(
        connection,
        workflow_id=workflow_id,
        status=status,
        limit=limit,
        full=True,
        touch=touch,
        stuck=stuck,
        sort="next_scheduled_at",
    )
    return WorkflowReport(
        workflow=WorkflowReportMeta(
            name=workflow.name,
            touches=workflow.touches,
            touch_interval_days=workflow.touch_interval_days,
            status=workflow.status,
        ),
        funnel=funnel,
        tasks=tasks,
        enrollments=enrollments,
    )


def get_workflow_status_health(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> WorkflowStatusHealth | None:
    """Ops-health composite for one workflow (§V.157).

    Wording comes from ``check_workflow_wording``. This path passes an empty
    catalog, so live rows classify as ``orphaned``. No LLM.
    """
    workflow = get_workflow(connection, workflow_id)
    if workflow is None:
        return None
    funnel = get_workflow_stats(connection, workflow)

    overdue_row = connection.execute(
        """\
        SELECT COUNT(*)::int AS n FROM task
        WHERE workflow_id = %(workflow_id)s
          AND status = 'pending'
          AND scheduled_at < NOW()
        """,
        {"workflow_id": workflow_id},
    ).fetchone()
    failed_row = connection.execute(
        """\
        SELECT COUNT(*)::int AS n FROM task
        WHERE workflow_id = %(workflow_id)s
          AND status = 'failed'
          AND completed_at >= NOW() - INTERVAL '24 hours'
        """,
        {"workflow_id": workflow_id},
    ).fetchone()
    assert overdue_row is not None
    assert failed_row is not None

    sync = _sync_loop_block(connection)
    if sync is None:
        run_loop = "stopped"
    else:
        # Heartbeat age: > 120s without tick counts as stale (2x default run_interval).
        age = sync.get("heartbeat_age_seconds")
        run_loop = "stale" if isinstance(age, int) and age > 120 else "ok"

    wording_report = check_workflow_wording(
        connection, {}, account_id=workflow.account_id
    )
    wording = next(
        (
            entry.state
            for entry in wording_report.workflows
            if entry.name == workflow.name
        ),
        "orphaned",
    )

    return WorkflowStatusHealth(
        workflow=WorkflowReportMeta(
            name=workflow.name,
            touches=workflow.touches,
            touch_interval_days=workflow.touch_interval_days,
            status=workflow.status,
        ),
        wording=wording,
        run_loop=run_loop,
        overdue_tasks=overdue_row["n"],
        failed_tasks_24h=failed_row["n"],
        enrollments_never_sent=funnel.awaiting_first_touch,
        funnel_active=funnel.active,
    )


def list_active_workflows(
    connection: psycopg.Connection[dict[str, Any]],
) -> list[Workflow]:
    """Return every active workflow, name-sorted, with no list cap (§V.174)."""
    query = SQL(
        "SELECT workflow.*, account.email AS account_email "
        "FROM workflow JOIN account ON account.id = workflow.account_id "
        "WHERE workflow.status = 'active' "
        "ORDER BY workflow.name"
    )
    rows = connection.execute(query).fetchall()
    return [Workflow.model_validate(row) for row in rows]


def _review_task_counts(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> WorkflowReviewTaskCounts:
    """Count failed, overdue, and pending tasks for one workflow."""
    row = connection.execute(
        """\
        SELECT
            COUNT(*) FILTER (WHERE status = 'failed')::int AS failed,
            COUNT(*) FILTER (
                WHERE status = 'pending' AND scheduled_at < NOW()
            )::int AS overdue,
            COUNT(*) FILTER (WHERE status = 'pending')::int AS pending
        FROM task
        WHERE workflow_id = %(workflow_id)s
        """,
        {"workflow_id": workflow_id},
    ).fetchone()
    assert row is not None
    return WorkflowReviewTaskCounts(
        failed=row["failed"], overdue=row["overdue"], pending=row["pending"]
    )


def _review_window_activities(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    since: str,
    until: str,
) -> list[WorkflowReviewActivity]:
    """Window activities including inbound email_received via email FK.

    Inbound ``email_received`` rows are stored without ``activity.workflow_id``
    (sync path). Join ``email.workflow_id`` so they still appear with snippet.
    """
    rows = connection.execute(
        """\
        SELECT a.id, a.contact_id, a.company_id, a.email_id, a.workflow_id,
            a.task_id, a.enrollment_id, a.type, a.summary, a.created_at,
            COALESCE(LEFT(e.body_text, 500), '') AS snippet
        FROM activity a
        LEFT JOIN email e ON e.id = a.email_id
        WHERE a.created_at >= %(since)s
          AND a.created_at <= %(until)s
          AND (
              a.workflow_id = %(workflow_id)s
              OR e.workflow_id = %(workflow_id)s
          )
        ORDER BY a.created_at DESC
        """,
        {"workflow_id": workflow_id, "since": since, "until": until},
    ).fetchall()
    return [WorkflowReviewActivity.model_validate(row) for row in rows]


def _review_failed_tasks(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> list[WorkflowReviewFailedTask]:
    """Failed tasks with contact_email and result.reason (§V.172)."""
    rows = connection.execute(
        """\
        SELECT t.id, t.enrollment_id, t.workflow_id, t.contact_id, t.email_id,
            t.description, t.scheduled_at, t.status, t.attempt_count,
            jsonb_build_object('reason', t.result->>'reason') AS result,
            c.email AS contact_email
        FROM task t
        JOIN contact c ON c.id = t.contact_id
        WHERE t.workflow_id = %(workflow_id)s
          AND t.status = 'failed'
        ORDER BY t.scheduled_at DESC
        """,
        {"workflow_id": workflow_id},
    ).fetchall()
    return [WorkflowReviewFailedTask.model_validate(row) for row in rows]


def _review_one_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    workflow: Workflow,
    since: str,
    until: str,
) -> WorkflowReviewItem:
    """Build one dated campaign collect for a resolved workflow."""
    funnel = get_workflow_stats(connection, workflow)
    enrollments = list_enrollments_detailed(
        connection,
        workflow_id=workflow.id,
        full=True,
        limit=None,
        sort="next_scheduled_at",
    )
    return WorkflowReviewItem(
        workflow=WorkflowReportMeta(
            name=workflow.name,
            touches=workflow.touches,
            touch_interval_days=workflow.touch_interval_days,
            status=workflow.status,
        ),
        funnel=funnel,
        task_counts=_review_task_counts(connection, workflow.id),
        emails=list_emails(
            connection,
            workflow_id=workflow.id,
            since=since,
            until=until,
            limit=None,
        ),
        activities=_review_window_activities(connection, workflow.id, since, until),
        failed_tasks=_review_failed_tasks(connection, workflow.id),
        enrollments=enrollments,
    )


def get_workflow_review(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_ids: Sequence[str],
    *,
    since: str,
    until: str,
) -> WorkflowReview:
    """Dated one-envelope campaign collect for one or more workflows (§V.174).

    Pure SQL / deterministic. No LLM, no CRM writes. Enrollments are not
    capped. Window emails and activities include inbound ``email_received``
    with snippet even when ``activity.workflow_id`` is unset.
    """
    since_dt = datetime.fromisoformat(since)
    until_dt = datetime.fromisoformat(until)
    if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=UTC)
    if until_dt.tzinfo is None:
        until_dt = until_dt.replace(tzinfo=UTC)
    reviews: list[WorkflowReviewItem] = []
    for workflow_id in workflow_ids:
        workflow = get_workflow(connection, workflow_id)
        if workflow is None:
            continue
        reviews.append(_review_one_workflow(connection, workflow, since, until))
    return WorkflowReview(since=since_dt, until=until_dt, reviews=reviews)


def get_queue_report(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    detail: bool = False,
    workflow_id: str | None = None,
    tz: str = "UTC",
    limit: int = 100,
    overdue: bool = False,
    failed: bool = False,
    stuck: bool = False,
) -> QueueReport:
    """Build the ``show queue`` report (§V.166).

    Workflow grain: one row per in-scope workflow (draft/active/paused),
    sorted by next pending ``scheduled_at`` ascending (empty last) then name.
    Task grain: default union pending + failed-unsent + stuck, sorted
    pending ``scheduled_at`` ASC then failed then stuck. ``--limit`` caps
    each kind. ``--overdue`` / ``--failed`` / ``--stuck`` select one kind.
    Does not change ``list_tasks`` DESC. No LLM, no write.
    """
    from zoneinfo import ZoneInfo

    ZoneInfo(tz)  # raise ZoneInfoNotFoundError for the CLI to map
    if detail:
        rows = _queue_task_rows(
            connection,
            workflow_id=workflow_id,
            limit=limit,
            overdue=overdue,
            failed=failed,
            stuck=stuck,
        )
        return QueueReport(grain="task", tz=tz, rows=rows)
    workflow_rows = _queue_workflow_rows(connection, workflow_id=workflow_id)
    return QueueReport(grain="workflow", tz=tz, rows=workflow_rows)


def _queue_workflow_rows(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    workflow_id: str | None,
) -> list[QueueWorkflowRow]:
    """Aggregate one row per workflow for the default queue grain."""
    conditions: list[SQL] = []
    params: dict[str, object] = {}
    if workflow_id is not None:
        conditions.append(SQL("w.id = %(workflow_id)s"))
        params["workflow_id"] = workflow_id
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    resolved = _sql_resolve_touch(SQL("t.context"))
    sent_count = _sql_outbound_sent_count(SQL("e"))
    query = SQL(
        """\
        WITH tasks AS (
            SELECT
                t.workflow_id,
                COUNT(*) FILTER (
                    WHERE t.status = 'pending' AND {touch} = 1
                )::int AS t1,
                COUNT(*) FILTER (
                    WHERE t.status = 'pending' AND {touch} = 2
                )::int AS t2,
                COUNT(*) FILTER (
                    WHERE t.status = 'pending' AND {touch} = 3
                )::int AS t3,
                COUNT(*) FILTER (
                    WHERE t.status = 'pending' AND {touch} >= 4
                )::int AS t4p,
                MIN(t.scheduled_at) FILTER (WHERE t.status = 'pending')
                    AS next_at
            FROM task t
            GROUP BY t.workflow_id
        ),
        failed AS (
            SELECT t.workflow_id, COUNT(*)::int AS failed
            FROM task t
            JOIN enrollment e ON e.id = t.enrollment_id
            {outcome}
            WHERE {failed_where}
            GROUP BY t.workflow_id
        ),
        stuck AS (
            SELECT e.workflow_id, COUNT(*)::int AS stuck
            FROM enrollment e
            {stuck_laterals}
            {outcome}
            WHERE {stuck_where}
            GROUP BY e.workflow_id
        )
        SELECT
            w.name AS workflow_name,
            w.status,
            COALESCE(tasks.t1, 0) AS t1,
            COALESCE(tasks.t2, 0) AS t2,
            COALESCE(tasks.t3, 0) AS t3,
            COALESCE(tasks.t4p, 0) AS t4p,
            COALESCE(failed.failed, 0) AS failed,
            COALESCE(stuck.stuck, 0) AS stuck,
            tasks.next_at
        FROM workflow w
        LEFT JOIN tasks ON tasks.workflow_id = w.id
        LEFT JOIN failed ON failed.workflow_id = w.id
        LEFT JOIN stuck ON stuck.workflow_id = w.id
        {where}
        ORDER BY tasks.next_at ASC NULLS LAST, w.name ASC
        """
    ).format(
        touch=resolved,
        outcome=_enrollment_outcome_lateral(),
        failed_where=_QUEUE_FAILED_UNSENT_WHERE,
        stuck_laterals=_QUEUE_STUCK_LATERALS,
        stuck_where=_queue_stuck_where(sent_count),
        where=where,
    )
    rows = connection.execute(query, params).fetchall()
    return [QueueWorkflowRow.model_validate(row) for row in rows]


_QUEUE_CONTACT_SQL = SQL(
    "COALESCE("
    "NULLIF(TRIM(BOTH FROM CONCAT_WS(' ', c.first_name, c.last_name)), ''), "
    "c.email)"
)

# Counts and --detail lists share these so a later edit cannot desync them.
_QUEUE_FAILED_UNSENT_WHERE = SQL(
    "t.status = 'failed' "
    "AND t.email_id IS NULL "
    "AND e.status = 'active' "
    "AND outcome.latest_outcome IS NULL"
)
_QUEUE_STUCK_LATERALS = SQL(
    "LEFT JOIN LATERAL ("
    "SELECT t.scheduled_at FROM task t "
    "WHERE t.enrollment_id = e.id AND t.status = 'pending' "
    "ORDER BY t.scheduled_at ASC NULLS LAST LIMIT 1"
    ") nt ON TRUE "
    "LEFT JOIN LATERAL ("
    "SELECT t.status FROM task t "
    "WHERE t.enrollment_id = e.id "
    "ORDER BY t.created_at DESC LIMIT 1"
    ") lt ON TRUE"
)


def _queue_stuck_where(sent: SQL | Composed) -> Composed:
    return SQL(
        "e.status = 'active' "
        "AND nt.scheduled_at IS NULL "
        "AND outcome.latest_outcome IS NULL "
        "AND (lt.status = 'failed' OR {sent} = 0)"
    ).format(sent=sent)


def _queue_task_from_row(row: dict[str, Any], *, kind: QueueTaskKind) -> QueueTaskRow:
    """Map a task-grain SQL row onto ``QueueTaskRow``."""
    from mailpilot.queue import format_queue_touch

    context = row["context"]
    context_dict = context if isinstance(context, dict) else None
    trigger = row["trigger"]
    trigger_text = trigger if isinstance(trigger, str) else ""
    attempts = row["attempts"]
    raw_reason = row.get("reason")
    reason = raw_reason if isinstance(raw_reason, str) else ""
    return QueueTaskRow(
        workflow_name=row["workflow_name"],
        company_domain=row["company_domain"] or "",
        contact=row["contact"],
        email=row["email"],
        touch=format_queue_touch(context_dict, trigger_text),
        attempts=int(attempts) if attempts is not None else 0,
        next_at=row["next_at"],
        kind=kind,
        reason=reason,
        task_id=row["task_id"],
        enrollment_id=row["enrollment_id"],
    )


def _queue_task_rows(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    workflow_id: str | None,
    limit: int,
    overdue: bool,
    failed: bool = False,
    stuck: bool = False,
) -> list[QueueTaskRow]:
    """Task grain: union pending+failed-unsent+stuck, or one kind."""
    if stuck:
        return _queue_stuck_rows(connection, workflow_id=workflow_id, limit=limit)
    if failed:
        return _queue_failed_rows(connection, workflow_id=workflow_id, limit=limit)
    pending = _queue_pending_rows(
        connection, workflow_id=workflow_id, limit=limit, overdue=overdue
    )
    if overdue:
        return pending
    failed_rows = _queue_failed_rows(connection, workflow_id=workflow_id, limit=limit)
    stuck_rows = _queue_stuck_rows(connection, workflow_id=workflow_id, limit=limit)
    return pending + failed_rows + stuck_rows


def _queue_pending_rows(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    workflow_id: str | None,
    limit: int,
    overdue: bool,
) -> list[QueueTaskRow]:
    """Pending task grain; ``--overdue`` keeps scheduled_at < now."""
    conditions: list[SQL] = [SQL("t.status = 'pending'")]
    params: dict[str, object] = {"limit": limit}
    if workflow_id is not None:
        conditions.append(SQL("t.workflow_id = %(workflow_id)s"))
        params["workflow_id"] = workflow_id
    if overdue:
        conditions.append(SQL("t.scheduled_at < NOW()"))
    where = SQL("WHERE ") + SQL(" AND ").join(conditions)
    query = SQL(
        """\
        SELECT
            t.id AS task_id,
            t.enrollment_id,
            t.scheduled_at AS next_at,
            {contact} AS contact,
            c.email AS email,
            COALESCE(co.domain, '') AS company_domain,
            w.name AS workflow_name,
            t.context,
            COALESCE(t.context->>'trigger', '') AS trigger,
            t.attempt_count AS attempts,
            '' AS reason
        FROM task t
        JOIN workflow w ON w.id = t.workflow_id
        JOIN contact c ON c.id = t.contact_id
        LEFT JOIN company co ON co.id = c.company_id
        {where}
        ORDER BY t.scheduled_at ASC
        LIMIT %(limit)s
        """
    ).format(contact=_QUEUE_CONTACT_SQL, where=where)
    rows = connection.execute(query, params).fetchall()
    return [_queue_task_from_row(row, kind="pending") for row in rows]


def _queue_failed_rows(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    workflow_id: str | None,
    limit: int,
) -> list[QueueTaskRow]:
    """Failed-unsent task grain (status=failed, email_id null, active, no terminal)."""
    conditions: list[SQL] = [_QUEUE_FAILED_UNSENT_WHERE]
    params: dict[str, object] = {"limit": limit}
    if workflow_id is not None:
        conditions.append(SQL("t.workflow_id = %(workflow_id)s"))
        params["workflow_id"] = workflow_id
    where = SQL("WHERE ") + SQL(" AND ").join(conditions)
    query = SQL(
        """\
        SELECT
            t.id AS task_id,
            t.enrollment_id,
            t.scheduled_at AS next_at,
            {contact} AS contact,
            c.email AS email,
            COALESCE(co.domain, '') AS company_domain,
            w.name AS workflow_name,
            t.context,
            COALESCE(t.context->>'trigger', '') AS trigger,
            t.attempt_count AS attempts,
            COALESCE(t.result->>'reason', '') AS reason
        FROM task t
        JOIN enrollment e ON e.id = t.enrollment_id
        JOIN workflow w ON w.id = t.workflow_id
        JOIN contact c ON c.id = t.contact_id
        LEFT JOIN company co ON co.id = c.company_id
        {outcome}
        {where}
        ORDER BY t.scheduled_at ASC
        LIMIT %(limit)s
        """
    ).format(
        contact=_QUEUE_CONTACT_SQL,
        outcome=_enrollment_outcome_lateral(),
        where=where,
    )
    rows = connection.execute(query, params).fetchall()
    return [_queue_task_from_row(row, kind="failed") for row in rows]


def _queue_stuck_rows(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    workflow_id: str | None,
    limit: int,
) -> list[QueueTaskRow]:
    """Stuck-enrollment grain; latest failed task fills touch/attempts/next_at."""
    sent_count = _sql_outbound_sent_count(SQL("e"))
    conditions: list[SQL | Composed] = [_queue_stuck_where(sent_count)]
    params: dict[str, object] = {"limit": limit}
    if workflow_id is not None:
        conditions.append(SQL("e.workflow_id = %(workflow_id)s"))
        params["workflow_id"] = workflow_id
    where = SQL("WHERE ") + SQL(" AND ").join(conditions)
    query = SQL(
        """\
        SELECT
            ft.id AS task_id,
            e.id AS enrollment_id,
            ft.scheduled_at AS next_at,
            {contact} AS contact,
            c.email AS email,
            COALESCE(co.domain, '') AS company_domain,
            w.name AS workflow_name,
            ft.context,
            COALESCE(ft.trigger, '') AS trigger,
            ft.attempt_count AS attempts,
            COALESCE(ft.reason, '') AS reason
        FROM enrollment e
        JOIN workflow w ON w.id = e.workflow_id
        JOIN contact c ON c.id = e.contact_id
        LEFT JOIN company co ON co.id = c.company_id
        {stuck_laterals}
        LEFT JOIN LATERAL (
            SELECT t.id, t.scheduled_at, t.attempt_count, t.context,
                   COALESCE(t.context->>'trigger', '') AS trigger,
                   COALESCE(t.result->>'reason', '') AS reason
            FROM task t
            WHERE t.enrollment_id = e.id AND t.status = 'failed'
            ORDER BY t.created_at DESC LIMIT 1
        ) ft ON TRUE
        {outcome}
        {where}
        ORDER BY ft.scheduled_at ASC NULLS LAST, c.email ASC
        LIMIT %(limit)s
        """
    ).format(
        contact=_QUEUE_CONTACT_SQL,
        stuck_laterals=_QUEUE_STUCK_LATERALS,
        outcome=_enrollment_outcome_lateral(),
        where=where,
    )
    rows = connection.execute(query, params).fetchall()
    return [_queue_task_from_row(row, kind="stuck") for row in rows]


def canonical_touch_copy(value: object) -> list[dict[str, Any]]:
    """Normalize ``touch_copy`` to hashed list-of-dicts form (§V.194).

    Sorted by ``n``. Missing/invalid entries collapse to ``[]`` so an omitted
    catalog table hashes the same as a live default empty JSONB array.
    """
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, TouchCopy):
            rows.append({"n": item.n, "subject": item.subject, "body": item.body})
            continue
        if not isinstance(item, dict):
            continue
        n = item.get("n")
        if not isinstance(n, int):
            continue
        rows.append(
            {
                "n": n,
                "subject": str(item.get("subject") or ""),
                "body": str(item.get("body") or ""),
            }
        )
    rows.sort(key=lambda row: int(row["n"]))
    return rows


def _compute_workflow_wording_hash(
    template: str,
    theme: str,
    goal: str,
    instructions: str,
    touches: int | None,
    touch_interval_days: int | None,
    touch_copy: object = (),
) -> str:
    """SHA-256 over the def fields, name excluded (§V.134).

    Hashes ``{template, theme, goal, instructions, touches,
    touch_interval_days, touch_copy}`` -- cadence pair per §V.136,
    ``touch_copy`` per §V.194. The workflow ``name`` is the join key, never a
    hashed field (§V.134). Canonical JSON (sorted keys) keeps the hash stable
    across field order and is safe for pipes/newlines in ``instructions``.

    Args:
        template: Template name.
        theme: Email color theme.
        goal: Enrollment success goal.
        instructions: Workflow instructions.
        touches: Total sends in the touch cadence, or None for single-touch.
        touch_interval_days: Days between touches, or None for single-touch.
        touch_copy: Per-touch copy catalog (list of rows or empty).

    Returns:
        Hex SHA-256 digest of the canonical def payload.
    """
    canonical = json.dumps(
        {
            "template": template,
            "theme": theme,
            "goal": goal,
            "instructions": instructions,
            "touches": touches,
            "touch_interval_days": touch_interval_days,
            "touch_copy": canonical_touch_copy(touch_copy),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def catalog_def_fields(entry: dict[str, Any]) -> dict[str, Any]:
    """Def fields as import persists them and check hashes them (§V.103/§V.134).

    Defaults match ``workflow import`` (theme -> blue, goal/instructions ->
    empty string). ``template`` defaults to empty so a malformed def without
    one simply fails to match any row rather than raising. Cadence is a pair:
    both ints persist as-is; either omitted or non-int collapses to
    ``(None, None)`` so the hash matches a legal single-touch row (schema
    CHECK forbids ``touches`` without ``touch_interval_days``). File is the
    sole source of truth -- omitted cadence is single-touch, not "keep live".
    """
    touches_raw = entry.get("touches")
    interval_raw = entry.get("touch_interval_days")
    if isinstance(touches_raw, int) and isinstance(interval_raw, int):
        touches: int | None = touches_raw
        interval: int | None = interval_raw
    else:
        touches = None
        interval = None
    return {
        "template": str(entry.get("template") or ""),
        "theme": str(entry.get("theme") or "blue"),
        "goal": str(entry.get("goal") or ""),
        "instructions": str(entry.get("instructions") or ""),
        "touches": touches,
        "touch_interval_days": interval,
        "touch_copy": canonical_touch_copy(entry.get("touch_copy")),
    }


def _stored_def_fields(persisted: dict[str, Any]) -> dict[str, Any]:
    """Live-row def fields as stored; no catalog defaulting (§V.103/§B.144).

    Empty ``theme`` stays empty (not ``blue``). Catalog defaults live only
    in ``catalog_def_fields``. Cadence non-ints collapse to ``None`` so the
    hash matches ``_compute_workflow_wording_hash``.
    """
    touches_raw = persisted.get("touches")
    interval_raw = persisted.get("touch_interval_days")
    return {
        "template": str(persisted.get("template") or ""),
        "theme": str(persisted.get("theme") or ""),
        "goal": str(persisted.get("goal") or ""),
        "instructions": str(persisted.get("instructions") or ""),
        "touches": touches_raw if isinstance(touches_raw, int) else None,
        "touch_interval_days": interval_raw if isinstance(interval_raw, int) else None,
        "touch_copy": canonical_touch_copy(persisted.get("touch_copy")),
    }


def _catalog_wording_hash(entry: dict[str, Any]) -> str:
    """Hash a parsed catalog def the way an import would persist it (§V.134)."""
    return _persisted_wording_hash(catalog_def_fields(entry))


def _persisted_wording_hash(persisted: dict[str, Any]) -> str:
    """Hash stored def fields as-is; no second catalog default (§V.103/§B.144)."""
    stored = _stored_def_fields(persisted)
    return _compute_workflow_wording_hash(
        template=stored["template"],
        theme=stored["theme"],
        goal=stored["goal"],
        instructions=stored["instructions"],
        touches=stored["touches"],
        touch_interval_days=stored["touch_interval_days"],
        touch_copy=stored["touch_copy"],
    )


def workflow_import_sync_report(
    entry: dict[str, Any], persisted: dict[str, Any]
) -> dict[str, Any]:
    """Post-apply import sync vs the live written row (§V.103/§B.143).

    ``in_sync`` is catalog SHA-256 vs persisted SHA-256 (same hash as
    ``check_workflow_wording``). ``remaining`` maps catalog def fields whose
    hashed projection still differs from stored columns -- empty when in
    sync. Equality matches the hash, not raw ``persisted.get``.
    """
    catalog = catalog_def_fields(entry)
    stored = _stored_def_fields(persisted)
    catalog_hash = _catalog_wording_hash(entry)
    row_hash = _persisted_wording_hash(persisted)
    remaining: dict[str, object] = {}
    if catalog_hash != row_hash:
        for key, catalog_value in catalog.items():
            if catalog_value != stored[key]:
                remaining[key] = catalog_value
    return {
        "in_sync": catalog_hash == row_hash,
        "catalog_hash": catalog_hash,
        "row_hash": row_hash,
        "remaining": remaining,
    }


def import_row_in_sync(entry: dict[str, Any], persisted: dict[str, Any]) -> bool:
    """True when catalog wording hash matches the persisted def fields (§V.103).

    Import uses this for the per-row ``in_sync`` flag after create/update.
    ``persisted`` is the live written-row def
    ``{template, theme, goal, instructions, touches, touch_interval_days,
    touch_copy}``.
    """
    report = workflow_import_sync_report(entry, persisted)
    return bool(report["in_sync"])


def check_workflow_wording(
    connection: psycopg.Connection[dict[str, Any]],
    catalog: dict[str, dict[str, Any]],
    scope_to_catalog: bool = False,
    account_id: str | None = None,
) -> WorkflowCheck:
    """Compare catalog defs against live rows by name and classify each (§V.134).

    A read-only 2-way live SHA-256 over the def fields
    ``{template, theme, goal, instructions, touches, touch_interval_days,
    touch_copy}`` mirroring ``db check``: no stored column, both sides hashed
    on the fly. Cadence pair per §V.136; ``touch_copy`` per §V.194. The
    globally unique ``name``
    (§V.90)
    is the join key, so the comparison spans every account's rows unless
    ``account_id`` scopes the live side. Each name lands in one of four states:

    - ``in_sync``: name on both sides, hashes equal.
    - ``out_of_sync``: name on both sides, hashes differ (re-import due).
    - ``not_imported``: name in a catalog def, no row.
    - ``orphaned``: name in a row, no catalog def.

    Def fields are import-only (§V.103), so there is no row-ahead state -- a
    mismatch always means the catalog leads. The report is informational
    (``ok:true`` regardless of state); it is never a deploy gate.

    Args:
        connection: Open database connection.
        catalog: Parsed catalog defs keyed by the def's ``name`` field (the CLI
            reader applies last-def-wins on duplicate names per §V.134).
        scope_to_catalog: When ``True``, report only the catalog names -- a DB
            row with no def is dropped, never ``orphaned`` (§V.134). The CLI
            sets this for every ``--file``-only check (file or directory).
        account_id: When set, hash only that account's live rows. The CLI
            pairs this with ``scope_to_catalog=False`` so ``--account-email``
            plus ``--file`` restores the account's full envelope (orphans
            included).

    Returns:
        ``WorkflowCheck`` carrying one entry per name plus rollup counts.
    """
    row_hashes = {
        row.name: _compute_workflow_wording_hash(
            template=row.template,
            theme=row.theme,
            goal=row.goal,
            instructions=row.instructions,
            touches=row.touches,
            touch_interval_days=row.touch_interval_days,
            touch_copy=row.touch_copy,
        )
        for row in list_workflows_full(connection, account_id)
    }
    catalog_hashes = {
        name: _catalog_wording_hash(entry) for name, entry in catalog.items()
    }

    names = set(catalog_hashes)
    if not scope_to_catalog:
        names |= set(row_hashes)
    entries: list[WorkflowCheckEntry] = []
    for name in sorted(names):
        catalog_hash = catalog_hashes.get(name)
        row_hash = row_hashes.get(name)
        if catalog_hash is not None and row_hash is not None:
            state = "in_sync" if catalog_hash == row_hash else "out_of_sync"
        elif catalog_hash is not None:
            state = "not_imported"
        else:
            state = "orphaned"
        entries.append(
            WorkflowCheckEntry(
                name=name,
                state=state,
                catalog_hash=catalog_hash,
                row_hash=row_hash,
            )
        )
    return WorkflowCheck(
        workflows=entries,
        in_sync=sum(1 for entry in entries if entry.state == "in_sync"),
        out_of_sync=sum(1 for entry in entries if entry.state == "out_of_sync"),
        not_imported=sum(1 for entry in entries if entry.state == "not_imported"),
        orphaned=sum(1 for entry in entries if entry.state == "orphaned"),
    )


def search_workflows(
    connection: psycopg.Connection[dict[str, Any]],
    query: str,
    limit: int = 100,
) -> list[WorkflowSummary]:
    """Search workflows by name or goal.

    Args:
        connection: Open database connection.
        query: Search term (matched against name and goal).
        limit: Maximum number of results.

    Returns:
        Matching workflow summaries ordered by name.
    """
    pattern = f"%{query}%"
    query_sql = SQL(
        "SELECT {} "
        "FROM workflow JOIN account ON account.id = workflow.account_id "
        "WHERE LOWER(workflow.name) LIKE LOWER(%(pattern)s) "
        "   OR LOWER(workflow.goal) LIKE LOWER(%(pattern)s) "
        "ORDER BY LOWER(workflow.name) "
        "LIMIT %(limit)s"
    ).format(_WORKFLOW_SUMMARY_COLUMNS)
    rows = connection.execute(
        query_sql,
        {"pattern": pattern, "limit": limit},
    ).fetchall()
    return [WorkflowSummary.model_validate(row) for row in rows]


def update_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    **fields: object,
) -> Workflow | None:
    """Update a workflow by ID.

    Writable fields: the def fields ``name``, ``goal``, ``instructions``,
    ``theme``, ``touches``, ``touch_interval_days``, ``touch_copy`` (import-only
    writers per §V.103, cadence pair per §V.136, copy catalog per §V.194) plus
    the non-def ``account_id`` (account re-binding, the sole field
    ``workflow update`` exposes). Status transitions
    use ``activate_workflow()`` / ``pause_workflow()``. ``type`` and ``template``
    are immutable after creation (§V.44).

    Args:
        connection: Open database connection.
        workflow_id: Workflow ID.
        **fields: Fields to update.

    Returns:
        Updated workflow, or None if not found.
    """
    allowed = {
        "name",
        "goal",
        "instructions",
        "theme",
        "touches",
        "touch_interval_days",
        "touch_copy",
        "account_id",
    }
    if "template" in fields:
        raise ValueError(
            "workflow.template is immutable; "
            "delete and recreate the workflow to change template"
        )
    if "type" in fields:
        raise ValueError(
            "workflow.type is derived from the template at create time "
            "and cannot be updated"
        )
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_workflow(connection, workflow_id)
    if "touch_copy" in updates:
        updates["touch_copy"] = Json(canonical_touch_copy(updates["touch_copy"]))
    updates["id"] = workflow_id
    query = _build_update("workflow", updates, SQL("id = %(id)s"))
    row = connection.execute(query, updates).fetchone()
    connection.commit()
    if row is None:
        return None
    return get_workflow(connection, workflow_id)


def activate_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> Workflow:
    """Transition a workflow to active status.

    Valid transitions: ``draft -> active``, ``paused -> active``.
    Guards: ``goal`` and ``instructions`` must be non-empty.

    Args:
        connection: Open database connection.
        workflow_id: Workflow ID.

    Returns:
        Updated workflow.

    Raises:
        ValueError: If workflow not found, already active, or missing
            goal/instructions.
    """
    workflow = get_workflow(connection, workflow_id)
    if workflow is None:
        raise ValueError(f"workflow {workflow_id} not found")
    if workflow.status == "active":
        raise ValueError("workflow is already active")
    if not workflow.goal.strip():
        raise ValueError("goal must be non-empty to activate")
    if not workflow.instructions.strip():
        raise ValueError("instructions must be non-empty to activate")
    row = connection.execute(
        """\
        WITH updated AS (
            UPDATE workflow
            SET status = 'active', updated_at = CURRENT_TIMESTAMP
            WHERE id = %(id)s
            RETURNING *
        )
        SELECT updated.*, account.email AS account_email
        FROM updated JOIN account ON account.id = updated.account_id
        """,
        {"id": workflow_id},
    ).fetchone()
    connection.commit()
    return Workflow.model_validate(row)


def pause_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> Workflow:
    """Transition a workflow to paused status.

    Valid transition: ``active -> paused``.

    Args:
        connection: Open database connection.
        workflow_id: Workflow ID.

    Returns:
        Updated workflow.

    Raises:
        ValueError: If workflow not found or not active.
    """
    workflow = get_workflow(connection, workflow_id)
    if workflow is None:
        raise ValueError(f"workflow {workflow_id} not found")
    if workflow.status != "active":
        raise ValueError(f"cannot pause workflow in status '{workflow.status}'")
    row = connection.execute(
        """\
        WITH updated AS (
            UPDATE workflow
            SET status = 'paused', updated_at = CURRENT_TIMESTAMP
            WHERE id = %(id)s
            RETURNING *
        )
        SELECT updated.*, account.email AS account_email
        FROM updated JOIN account ON account.id = updated.account_id
        """,
        {"id": workflow_id},
    ).fetchone()
    connection.commit()
    return Workflow.model_validate(row)
