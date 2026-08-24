"""Shared SQL fragments (§V.162 / §V.178 / §V.184 / §V.185)."""
# pyright: reportPrivateUsage=false, reportUnusedFunction=false

from __future__ import annotations

from psycopg.sql import SQL, Composed


def _sql_parse_touch(context_col: SQL) -> Composed:
    """SQL that parses ``context.touch`` (2 / digit string / T<n>) to int; else NULL.

    ``context_col`` is a caller-owned fragment such as ``t.context`` or
    ``nt.context`` -- never user input. Replaces raw ``::int`` casts that
    raise ``InvalidTextRepresentation`` on ``T2`` (§V.162 / §B.132).
    """
    return SQL(
        "CASE "
        "WHEN ({col}->>'touch') ~ '^[0-9]+$' THEN ({col}->>'touch')::int "
        "WHEN ({col}->>'touch') ~ '^T[0-9]+$' "
        "THEN substring({col}->>'touch' from 2)::int "
        "ELSE NULL END"
    ).format(col=context_col)


def _sql_resolve_touch(context_col: SQL) -> Composed:
    """SQL that matches ``resolve_touch_number``: parse, else first-touch = 1.

    ``enrollment_run`` / ``enrollment_schedule`` with absent or unparseable
    ``context.touch`` resolve to 1 so stats pending and queue buckets count
    scheduled first-reach as T1 (§V.162 / §B.138).
    """
    return SQL(
        "COALESCE("
        "{parsed}, "
        "CASE WHEN {col}->>'trigger' IN ('enrollment_run', 'enrollment_schedule') "
        "THEN 1 END)"
    ).format(parsed=_sql_parse_touch(context_col), col=context_col)


def _sql_outbound_sent_count(e: SQL) -> Composed:
    """COUNT of sent outbound emails for enrollment-shaped alias ``e`` (§V.184).

    ``e`` is a caller-owned alias -- never user input.
    """
    return SQL(
        "(SELECT COUNT(*)::int FROM email "
        "WHERE email.workflow_id = {e}.workflow_id "
        "AND email.contact_id = {e}.contact_id "
        "AND email.direction = 'outbound' "
        "AND email.status = 'sent')"
    ).format(e=e)


def _enrollment_parent_select(
    src: SQL,
    extra: Composed | SQL | None = None,
) -> Composed:
    """SELECT ``src.*`` plus parent denorm JOINs (§V.185 / §V.5).

    ``src`` is a caller-owned table, CTE, or alias -- never user input.
    Optional ``extra`` injects additional selected columns after ``src.*``.
    """
    extra_sql = SQL(", {}").format(extra) if extra is not None else SQL("")
    return SQL(
        "SELECT {src}.*{extra}, "
        "workflow.name AS workflow_name, "
        "contact.email AS contact_email, "
        "TRIM(COALESCE(contact.first_name, '') || ' ' "
        "|| COALESCE(contact.last_name, '')) AS contact_name "
        "FROM {src} "
        "JOIN workflow ON workflow.id = {src}.workflow_id "
        "JOIN contact ON contact.id = {src}.contact_id"
    ).format(src=src, extra=extra_sql)


def _enrollment_outcome_lateral() -> SQL:
    """Latest completed/failed activity per enrollment alias ``e`` (§V.185)."""
    return SQL(
        "LEFT JOIN LATERAL ("
        "SELECT "
        "CASE a.type "
        "WHEN 'enrollment_completed' THEN 'completed' "
        "WHEN 'enrollment_failed' THEN 'failed' "
        "END AS latest_outcome, "
        "COALESCE(a.detail->>'reason', a.summary) AS latest_outcome_reason, "
        "a.created_at AS latest_outcome_at, "
        "a.detail->>'disposition' AS disposition "
        "FROM activity a "
        "WHERE a.contact_id = e.contact_id "
        "AND a.workflow_id = e.workflow_id "
        "AND a.type IN ('enrollment_completed', 'enrollment_failed') "
        "ORDER BY a.created_at DESC LIMIT 1"
        ") outcome ON TRUE "
    )


def _enrollment_lean_select() -> SQL:
    """Lean enrollment list SELECT + FROM/JOIN (§V.185 / §V.152)."""
    return SQL(
        "SELECT e.id, e.workflow_id, w.name AS workflow_name, "
        "e.contact_id, e.status, e.updated_at, "
        "c.email AS contact_email, "
        "TRIM(COALESCE(c.first_name, '') || ' ' || COALESCE(c.last_name, '')) "
        "AS contact_name "
        "FROM enrollment e "
        "JOIN workflow w ON w.id = e.workflow_id "
        "JOIN contact c ON c.id = e.contact_id "
    )


def _enrollment_full_select(sent_count: Composed) -> Composed:
    """Full enrollment list SELECT + FROM/JOIN + outcome LATERAL (§V.185)."""
    return (
        SQL(
            "SELECT e.id, e.workflow_id, w.name AS workflow_name, "
            "e.contact_id, e.status, e.updated_at, e.created_at, "
            "c.email AS contact_email, "
            "TRIM(COALESCE(c.first_name, '') || ' ' "
            "|| COALESCE(c.last_name, '')) AS contact_name, "
            "co.domain AS company_domain, "
            "co.name AS company_name, "
            "{sent_count} AS emails_sent, "
            "{sent_count} AS last_touch, "
            "nt.scheduled_at AS next_scheduled_at, "
            "COALESCE("
            "{next_touch}, "
            "CASE WHEN nt.scheduled_at IS NOT NULL "
            "AND nt.context->>'touch' IS NULL AND "
            "{sent_count} = 0 THEN 1 END"
            ") AS next_touch, "
            "outcome.disposition AS disposition, "
            "outcome.latest_outcome AS latest_outcome, "
            "outcome.latest_outcome_reason AS latest_outcome_reason, "
            "outcome.latest_outcome_at AS latest_outcome_at "
            "FROM enrollment e "
            "JOIN workflow w ON w.id = e.workflow_id "
            "JOIN contact c ON c.id = e.contact_id "
            "LEFT JOIN company co ON co.id = c.company_id "
            "LEFT JOIN LATERAL ("
            "SELECT t.scheduled_at, t.context FROM task t "
            "WHERE t.enrollment_id = e.id AND t.status = 'pending' "
            "ORDER BY t.scheduled_at ASC NULLS LAST LIMIT 1"
            ") nt ON TRUE "
        ).format(
            next_touch=_sql_parse_touch(SQL("nt.context")),
            sent_count=sent_count,
        )
        + _enrollment_outcome_lateral()
    )


def _enrollment_where(  # noqa: C901
    params: dict[str, object],
    *,
    workflow_id: str | None = None,
    contact_id: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
    disposition: str | None = None,
    stuck: bool = False,
    has_pending_task: bool | None = None,
    touch: int | None = None,
    sent_count: Composed,
) -> Composed | SQL:
    """Shared WHERE for ``list_enrollments_detailed`` (§V.185).

    Mutates ``params`` with filter placeholders. ``--touch`` parses
    ``context.touch``.
    """
    where_parts: list[Composed | SQL] = []
    if workflow_id is not None:
        where_parts.append(SQL("e.workflow_id = %(workflow_id)s"))
        params["workflow_id"] = workflow_id
    if contact_id is not None:
        where_parts.append(SQL("e.contact_id = %(contact_id)s"))
        params["contact_id"] = contact_id
    if status is not None:
        where_parts.append(SQL("e.status = %(status)s"))
        params["status"] = status
    if since is not None:
        where_parts.append(SQL("e.updated_at >= %(since)s"))
        params["since"] = since
    if until is not None:
        where_parts.append(SQL("e.updated_at <= %(until)s"))
        params["until"] = until
    if disposition is not None:
        params["disposition"] = disposition
        where_parts.append(SQL("outcome.disposition = %(disposition)s"))
    if stuck:
        where_parts.append(
            SQL(
                "("
                "("
                "e.status = 'active' "
                "AND outcome.disposition IS NULL "
                "AND nt.scheduled_at IS NULL "
                "AND {sent_count} = 0 "
                "AND e.created_at < NOW() "
                "- make_interval(hours => %(first_send_sla_hours)s)"
                ") "
                "OR "
                "("
                "EXISTS ("
                "SELECT 1 FROM email em "
                "WHERE em.workflow_id = e.workflow_id "
                "AND em.contact_id = e.contact_id "
                "AND em.direction = 'outbound' AND em.status = 'bounced'"
                ") "
                "AND outcome.disposition IS NULL"
                ") "
                "OR "
                "EXISTS ("
                "SELECT 1 FROM task t "
                "WHERE t.enrollment_id = e.id "
                "AND t.status = 'failed' AND t.attempt_count >= 3"
                ")"
                ")"
            ).format(sent_count=sent_count)
        )
    if has_pending_task is True:
        where_parts.append(
            SQL(
                "EXISTS ("
                "SELECT 1 FROM task t "
                "WHERE t.enrollment_id = e.id AND t.status = 'pending'"
                ")"
            )
        )
    elif has_pending_task is False:
        where_parts.append(
            SQL(
                "NOT EXISTS ("
                "SELECT 1 FROM task t "
                "WHERE t.enrollment_id = e.id AND t.status = 'pending'"
                ")"
            )
        )
    if touch is not None:
        params["touch"] = touch
        parsed_pending = _sql_parse_touch(SQL("t.context"))
        where_parts.append(
            SQL(
                "("
                "EXISTS ("
                "SELECT 1 FROM task t "
                "WHERE t.enrollment_id = e.id AND t.status = 'pending' "
                "AND {touch} = %(touch)s"
                ") "
                "OR ("
                "%(touch)s = 1 "
                "AND EXISTS ("
                "SELECT 1 FROM task t "
                "WHERE t.enrollment_id = e.id AND t.status = 'pending' "
                "AND t.context->>'touch' IS NULL"
                ") "
                "AND {sent_count} = 0"
                ") "
                "OR ("
                "NOT EXISTS ("
                "SELECT 1 FROM task t "
                "WHERE t.enrollment_id = e.id AND t.status = 'pending'"
                ") "
                "AND {sent_count} = %(touch)s"
                ")"
                ")"
            ).format(touch=parsed_pending, sent_count=sent_count)
        )
    if not where_parts:
        return SQL("")
    return SQL("WHERE ") + SQL(" AND ").join(where_parts)
