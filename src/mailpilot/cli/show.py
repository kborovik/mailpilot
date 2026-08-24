"""Human report hub."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import click

from mailpilot._filters import (
    limit_option,
    scope_option,
)
from mailpilot.cli.main import (
    _db,
    _resolve_workflow_id,
    main,
    output,
    output_error,
)

# -- Show report hub -----------------------------------------------------------


@main.group()
def show() -> None:
    """Human report hub."""


@show.command("queue")
@click.option(
    "--detail",
    is_flag=True,
    default=False,
    help="Task-grain queue: one row per pending task.",
)
@scope_option("--workflow-name", "workflow_name", "Filter by workflow (name or ID).")
@click.option(
    "--tz",
    "tz_name",
    default=None,
    show_default="host local",
    help="IANA timezone for table and JSON next_at.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "table"], case_sensitive=False),
    default="table",
    show_default=True,
    help="Output format (default table).",
)
@limit_option
@click.option(
    "--overdue",
    is_flag=True,
    default=False,
    help="Only pending tasks with scheduled_at in the past.",
)
def show_queue(
    detail: bool,
    workflow_name: str | None,
    tz_name: str | None,
    output_format: str,
    limit: int,
    overdue: bool,
) -> None:
    """Show the outbound queue as a human table (JSON opt-in).

    Default grain is one row per workflow (draft, active, paused) with
    pending counts by touch (t1/t2/t3/t4p). --detail lists pending tasks
    (workflow_name, company_domain, contact, email, touch, attempts,
    next_at). Empty prints (no rows).
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from tabulate import tabulate

    from mailpilot.database import get_queue_report
    from mailpilot.queue import (
        project_queue_json_next_at,
        queue_table_cells,
        queue_table_headers,
        resolve_host_tz,
    )

    resolved_tz = resolve_host_tz() if tz_name is None else tz_name
    try:
        zone = ZoneInfo(resolved_tz)
    except ZoneInfoNotFoundError, ValueError:
        output_error(f"unknown timezone: {resolved_tz}", "validation_error")

    with _db() as connection:
        resolved_workflow_id: str | None = None
        if workflow_name is not None:
            resolved_workflow_id = _resolve_workflow_id(connection, workflow_name)
        report = get_queue_report(
            connection,
            detail=detail,
            workflow_id=resolved_workflow_id,
            tz=resolved_tz,
            limit=limit if detail else 100,
            overdue=overdue if detail else False,
        )

    dumped = project_queue_json_next_at(report.model_dump(mode="json"), tz=zone)
    if output_format.lower() == "json":
        output({"queue": dumped}, record_count=len(report.rows))
        return
    if not report.rows:
        click.echo("(no rows)")
        return
    headers = queue_table_headers(detail=detail)
    table_rows = [
        queue_table_cells(row, detail=detail, tz=zone) for row in dumped["rows"]
    ]
    click.echo(tabulate(table_rows, headers=headers, tablefmt="simple"))
