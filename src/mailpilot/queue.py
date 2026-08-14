"""Queue-report helpers for ``mailpilot show queue`` (§V.166).

Formatting only -- SQL lives in ``database.get_queue_report``. Kept out of
``cli.py`` so ``--help`` stays click-only at import time (§V.2).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mailpilot.cadence import resolve_touch_number

_LOCALTIME_PATH = Path("/etc/localtime")
_TIMEZONE_PATH = Path("/etc/timezone")

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


def _iana_name_or_none(raw: str | None) -> str | None:
    """Return a ZoneInfo-resolvable IANA name, or None."""
    if raw is None:
        return None
    name = raw.strip().lstrip(":")
    if not name:
        return None
    normalized = name.replace("\\", "/")
    if "zoneinfo/" in normalized:
        name = normalized.split("zoneinfo/", 1)[1]
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError:
        return None
    return name


def _zoneinfo_suffix(path: Path) -> str | None:
    """Take the IANA suffix after a ``zoneinfo/`` path segment."""
    parts = path.parts
    if "zoneinfo" not in parts:
        return None
    suffix = "/".join(parts[parts.index("zoneinfo") + 1 :])
    return suffix or None


def _os_zoneinfo_name() -> str | None:
    """Read host IANA from ``/etc/localtime`` or ``/etc/timezone``."""
    try:
        resolved = _LOCALTIME_PATH.resolve()
    except OSError:
        resolved = None
    if resolved is not None:
        name = _iana_name_or_none(_zoneinfo_suffix(resolved))
        if name is not None:
            return name
    try:
        text = _TIMEZONE_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    first = text.splitlines()[0] if text else None
    return _iana_name_or_none(first)


def resolve_host_tz() -> str:
    """Host IANA name for ``show queue --tz`` default (§V.166).

    Prefer ``TZ`` when set and non-empty; else OS zoneinfo. Unresolvable
    host local returns ``UTC``.
    """
    env = os.environ.get("TZ")
    if env is not None and env.strip():
        return _iana_name_or_none(env) or "UTC"
    return _os_zoneinfo_name() or "UTC"


def format_queue_next_at(next_at: datetime | str | None, *, tz: ZoneInfo) -> str:
    """Render ``next_at`` as full ISO datetime in ``tz``.

    Empty when unset. Table and JSON both convert to ``tz`` (offset required).
    """
    if next_at is None or next_at == "":
        return ""
    parsed = datetime.fromisoformat(next_at) if isinstance(next_at, str) else next_at
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(tz).isoformat()


def project_queue_json_next_at(
    payload: dict[str, Any], *, tz: ZoneInfo
) -> dict[str, Any]:
    """Rewrite each row ``next_at`` to ISO in ``tz``. Null stays null."""
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return payload
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get("next_at")
        if value is None:
            continue
        if isinstance(value, (datetime, str)):
            formatted = format_queue_next_at(value, tz=tz)
            row["next_at"] = formatted if formatted else None
    return payload


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
