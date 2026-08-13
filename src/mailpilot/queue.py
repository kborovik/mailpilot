"""Queue-report helpers for ``mailpilot show queue`` (§V.166).

Formatting only -- SQL lives in ``database.get_queue_report``. Kept out of
``cli.py`` so ``--help`` stays click-only at import time (§V.2).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from mailpilot.cadence import parse_touch_number

_QUEUE_WORKFLOW_HEADERS = (
    "workflow",
    "status",
    "active",
    "pending",
    "overdue",
    "due_today",
    "next_at",
    "failed_24h",
    "never_sent",
)
_QUEUE_TASK_TABLE_HEADERS = (
    "when",
    "contact",
    "email",
    "company",
    "workflow",
    "touch",
    "trigger",
    "state",
    "attempts",
)


def format_queue_when(scheduled_at: datetime, *, now: datetime, tz: ZoneInfo) -> str:
    """Render a relative ``when`` cell for the task-grain queue.

    Shapes: ``overdue 2d``, ``today 14:00``, ``in 3d`` (hours/minutes when
    the delta is under a day). Past wins over same-calendar-day.
    """
    local_sched = scheduled_at.astimezone(tz)
    local_now = now.astimezone(tz)
    if local_sched < local_now:
        return f"overdue {_format_queue_delta(local_now - local_sched)}"
    if local_sched.date() == local_now.date():
        return f"today {local_sched.strftime('%H:%M')}"
    return f"in {_format_queue_delta(local_sched - local_now)}"


def format_queue_touch(context: dict[str, object] | None) -> str:
    """Render ``context.touch`` as ``T<n>`` or empty (§V.162)."""
    if context is None:
        return ""
    parsed = parse_touch_number(context.get("touch"))
    if parsed is None:
        return ""
    return f"T{parsed}"


def _format_queue_delta(delta: timedelta) -> str:
    """Compact duration for relative when: Nd, Nh, or Nm."""
    days = delta.days
    if days >= 1:
        return f"{days}d"
    hours = delta.seconds // 3600
    if hours >= 1:
        return f"{hours}h"
    return f"{delta.seconds // 60}m"


def queue_table_headers(*, detail: bool) -> tuple[str, ...]:
    """Column headers for the ASCII table (UUIDs hidden on task grain)."""
    if detail:
        return _QUEUE_TASK_TABLE_HEADERS
    return _QUEUE_WORKFLOW_HEADERS


def queue_table_cells(row: dict[str, object], *, detail: bool) -> list[str]:
    """Project one report row onto table cells (empty for nulls)."""
    headers = queue_table_headers(detail=detail)
    cells: list[str] = []
    for header in headers:
        value = row.get(header)
        if value is None:
            cells.append("")
        elif isinstance(value, datetime):
            cells.append(value.isoformat())
        else:
            cells.append(str(value))
    return cells
