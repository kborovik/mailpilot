"""Queue-report helpers for ``mailpilot show queue`` (§V.166).

Formatting only -- SQL lives in ``database.get_queue_report``. Kept out of
``cli.py`` so ``--help`` stays click-only at import time (§V.2).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from mailpilot.cadence import resolve_touch_number

_QUEUE_WORKFLOW_HEADERS = (
    "workflow_name",
    "status",
    "t1",
    "t2",
    "t3",
    "t4p",
    "next_at",
)
_QUEUE_TASK_TABLE_HEADERS = (
    "workflow_name",
    "company_domain",
    "contact",
    "email",
    "touch",
    "attempts",
    "next_at",
)


def format_queue_next_at(next_at: datetime | str | None, *, tz: ZoneInfo) -> str:
    """Render ``next_at`` as full ISO datetime in ``tz``.

    Empty when unset. JSON keeps the stored ISO; table converts to ``tz``.
    """
    if next_at is None or next_at == "":
        return ""
    parsed = datetime.fromisoformat(next_at) if isinstance(next_at, str) else next_at
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(tz).isoformat()


def format_queue_touch(context: dict[str, object] | None, trigger: str = "") -> str:
    """Render resolved touch as ``T<n>`` or empty (§V.162).

    Shares ``resolve_touch_number``: parse ``context.touch``, else first-reach
    triggers (``enrollment_run`` / ``enrollment_schedule``) become T1.
    """
    resolved_trigger = trigger
    if not resolved_trigger and context is not None:
        raw = context.get("trigger")
        if isinstance(raw, str):
            resolved_trigger = raw
    parsed = resolve_touch_number(context, resolved_trigger)
    if parsed is None:
        return ""
    return f"T{parsed}"


def queue_table_headers(*, detail: bool) -> tuple[str, ...]:
    """Column headers for the ASCII table (UUIDs hidden on task grain)."""
    if detail:
        return _QUEUE_TASK_TABLE_HEADERS
    return _QUEUE_WORKFLOW_HEADERS


def queue_table_cells(
    row: Mapping[str, object], *, detail: bool, tz: ZoneInfo
) -> list[str]:
    """Project one report row onto table cells (empty for nulls)."""
    headers = queue_table_headers(detail=detail)
    cells: list[str] = []
    for header in headers:
        value = row.get(header)
        if header == "next_at":
            if value is None or isinstance(value, (datetime, str)):
                cells.append(format_queue_next_at(value, tz=tz))
            else:
                cells.append(str(value))
        elif value is None:
            cells.append("")
        elif isinstance(value, datetime):
            cells.append(value.isoformat())
        else:
            cells.append(str(value))
    return cells
