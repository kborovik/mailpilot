"""Activity append-only writers."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.sql import SQL
from psycopg.types.json import Json

from mailpilot.database._common import (
    _new_id,
)
from mailpilot.models import (
    Activity,
    ActivitySummary,
)

# -- Activity ------------------------------------------------------------------


def create_activity(
    connection: psycopg.Connection[dict[str, Any]],
    activity_type: str,
    summary: str = "",
    detail: dict[str, object] | None = None,
    contact_id: str | None = None,
    company_id: str | None = None,
    email_id: str | None = None,
    workflow_id: str | None = None,
    task_id: str | None = None,
    enrollment_id: str | None = None,
    *,
    commit: bool = True,
) -> Activity:
    """Create an activity event.

    At least one of ``contact_id`` or ``company_id`` must be set.
    Structured FK columns (``email_id``, ``workflow_id``, ``task_id``,
    ``enrollment_id``) let reports join activity to source records without
    parsing ``detail`` JSON. ``enrollment_id`` is nullable -- non-enrollment
    activity types (``email_sent``, ``note_added``, etc.) leave it null;
    enrollment-lifecycle types (``enrollment_added`` / ``enrollment_completed``
    / etc.) populate it.

    Raises:
        ValueError: If neither contact_id nor company_id is provided.
    """
    if contact_id is None and company_id is None:
        raise ValueError("at least one of contact_id or company_id is required")
    row = connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, email_id, workflow_id, task_id,
            enrollment_id, type, summary, detail
        )
        VALUES (
            %(id)s, %(contact_id)s, %(company_id)s, %(email_id)s,
            %(workflow_id)s, %(task_id)s, %(enrollment_id)s,
            %(type)s, %(summary)s, %(detail)s
        )
        RETURNING *
        """,
        {
            "id": _new_id(),
            "contact_id": contact_id,
            "company_id": company_id,
            "email_id": email_id,
            "workflow_id": workflow_id,
            "task_id": task_id,
            "enrollment_id": enrollment_id,
            "type": activity_type,
            "summary": summary,
            "detail": Json(detail or {}),
        },
    ).fetchone()
    if commit:
        connection.commit()
    return Activity.model_validate(row)


def list_activities(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str | None = None,
    company_id: str | None = None,
    activity_type: str | None = None,
    limit: int = 100,
    since: str | None = None,
    until: str | None = None,
    workflow_id: str | None = None,
) -> list[ActivitySummary]:
    """List activities as summaries with required scope filter (§V.154).

    At least one of ``contact_id``, ``company_id``, or ``workflow_id`` must be
    provided.

    Args:
        connection: Open database connection.
        contact_id: Filter by contact ID.
        company_id: Filter by company ID.
        activity_type: Filter by activity type.
        limit: Maximum number of results.
        since: ISO datetime inclusive lower bound for created_at.
        until: ISO datetime inclusive upper bound for created_at.
        workflow_id: Filter by workflow ID (campaign timeline).

    Returns:
        Activity summaries ordered by created_at descending.

    Raises:
        ValueError: If no scope filter is provided.
    """
    if contact_id is None and company_id is None and workflow_id is None:
        raise ValueError(
            "at least one of contact_id, company_id, or workflow_id is required"
        )
    conditions: list[SQL] = []
    params: dict[str, object] = {"limit": limit}
    if contact_id is not None:
        conditions.append(SQL("contact_id = %(contact_id)s"))
        params["contact_id"] = contact_id
    if company_id is not None:
        conditions.append(SQL("company_id = %(company_id)s"))
        params["company_id"] = company_id
    if workflow_id is not None:
        conditions.append(SQL("workflow_id = %(workflow_id)s"))
        params["workflow_id"] = workflow_id
    if activity_type is not None:
        conditions.append(SQL("type = %(activity_type)s"))
        params["activity_type"] = activity_type
    if since is not None:
        conditions.append(SQL("created_at >= %(since)s"))
        params["since"] = since
    if until is not None:
        conditions.append(SQL("created_at <= %(until)s"))
        params["until"] = until
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT id, contact_id, company_id, email_id, workflow_id, task_id, "
        "enrollment_id, type, summary, created_at "
        "FROM activity {} ORDER BY created_at DESC LIMIT %(limit)s"
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [ActivitySummary.model_validate(row) for row in rows]
