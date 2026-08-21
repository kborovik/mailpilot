"""Out-of-office auto-reply pause + resume (§V.169).

Harness-owned: an inbound OOO / temporary-absence auto-reply on an active
outbound enrollment never goes to the agent, never sends a fallback ACK
(§V.131), and never concludes (§V.161). Pending follow-ups are cancelled
(§V.123) and a resume touch is scheduled at the parsed return date.
"""

from __future__ import annotations

import re
from calendar import month_abbr, month_name
from datetime import UTC, datetime, timedelta
from typing import Any

import logfire
import psycopg

from mailpilot.cadence import next_touch_scheduled_at
from mailpilot.database import create_task, get_task
from mailpilot.models import Email, Enrollment, Task, Workflow
from mailpilot.operator_log import operator_event

# Synthetic label stamped at sync when Auto-Submitted is auto-replied /
# auto-generated, so execute_task can detect without a schema column.
AUTO_SUBMITTED_LABEL = "AUTO_SUBMITTED"
_AUTO_SUBMITTED_VALUES = frozenset({"auto-replied", "auto-generated"})

_NULL_CADENCE_RESUME_DAYS = 3

_TERMINAL_AUTO_REPLY = re.compile(
    r"retired|left the company|no longer with|no longer employed|"
    r"address has changed|email address has changed|update your records|"
    r"new email address",
    re.IGNORECASE,
)
_ABSENCE = re.compile(
    r"out of(?: the)? office|on vacation|on holiday|on leave|"
    r"away from(?: the)? office|no access to email",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_WEEKDAY = re.compile(
    r"\b(?:until|returning|return(?:ing)?(?: on)?|back(?: on)?)\s+"
    r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
_MONTH_LOOKUP: dict[str, int] = {}
for _index, _name in enumerate(month_name):
    if _name:
        _MONTH_LOOKUP[_name.lower()] = _index
for _index, _name in enumerate(month_abbr):
    if _name:
        _MONTH_LOOKUP[_name.lower()] = _index
_MONTH_PATTERN = "|".join(
    re.escape(name) for name in sorted(_MONTH_LOOKUP, key=len, reverse=True)
)
_RANGE_SEP = r"[\-\u2013\u2014]"
_MONTH_DAY = re.compile(
    rf"\b({_MONTH_PATTERN})\s+(\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?!\s*{_RANGE_SEP}\s*\d)"
    rf"(?:,?\s+(20\d{{2}}))?\b",
    re.IGNORECASE,
)
_MONTH_DAY_RANGE = re.compile(
    rf"\b({_MONTH_PATTERN})\s+(\d{{1,2}})(?:st|nd|rd|th)?"
    rf"\s*{_RANGE_SEP}\s*"
    rf"(\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:,?\s+(20\d{{2}}))?\b",
    re.IGNORECASE,
)
_LEAVE_START_CUE = re.compile(r"\b(?:effective|begins)\b", re.IGNORECASE)
_RETURN_CUE = re.compile(
    r"\b(?:until|returning|return(?:ing)?(?:\s+on)?|"
    r"fully\s+back(?:\s+online)?|back(?:\s+online)?(?:\s+on)?)\b",
    re.IGNORECASE,
)
_ON_PREFIX = re.compile(r"\bon\s+$", re.IGNORECASE)
_WEEK_OF_PREFIX = re.compile(r"\bweek of\s+$", re.IGNORECASE)
_WEEKDAY_PREFIX = re.compile(
    rf"(?:{'|'.join(_WEEKDAY_NAMES)})\s*,?\s*$",
    re.IGNORECASE,
)
_MONTHS_PAST_MIN = 2
_RETURN_CUE_WINDOW = 80


def auto_submitted_label(header_value: str | None) -> str | None:
    """Return the synthetic label when ``Auto-Submitted`` is an auto-reply."""
    if header_value is None:
        return None
    token = header_value.strip().split(";", maxsplit=1)[0].strip().lower()
    if token in _AUTO_SUBMITTED_VALUES:
        return AUTO_SUBMITTED_LABEL
    return None


def _combined_text(email: Email) -> str:
    return f"{email.subject}\n{email.body_text}"


def _is_terminal_auto_reply(text: str) -> bool:
    """Address-change / left-company auto-replies are not OOO (§V.161, §V.164)."""
    return _TERMINAL_AUTO_REPLY.search(text) is not None


def _has_mechanical_signal(email: Email) -> bool:
    subject = email.subject.lower()
    if "automatic reply" in subject:
        return True
    return AUTO_SUBMITTED_LABEL in email.labels


def is_mechanical_ooo(email: Email) -> bool:
    """True when subject/Auto-Submitted mark an OOO, not a terminal auto-reply."""
    text = _combined_text(email)
    if _is_terminal_auto_reply(text):
        return False
    return _has_mechanical_signal(email)


def is_ooo_auto_reply(email: Email) -> bool:
    """True for harness OOO: mechanical signal or absence language, not terminal.

    Mechanical = subject ``Automatic reply`` or ``AUTO_SUBMITTED`` label.
    Absence language is the agent-OOO-class stand-in when headers are missing
    (skip ACK + schedule resume; do not skip the agent — a human can say
    they are out of office and still want a reply).
    """
    text = _combined_text(email)
    if _is_terminal_auto_reply(text):
        return False
    if _has_mechanical_signal(email):
        return True
    return _ABSENCE.search(text) is not None


def parse_ooo_return_at(text: str, *, now: datetime) -> datetime | None:
    """Parse a return instant from OOO prose, or None when unparseable.

    Prefers an ISO date, then a year-less week-range containing ``now``
    (resume = day after range end, same year), then a month-day (explicit
    year wins; year-less same-day ``on <Month> <D>`` resumes the next
    calendar day same year; year-less leave-start months past is
    unparseable; multi year-less month-day prefers return / fully-back-
    online over an earlier event-week; ``week of <Month> <D>`` is never
    a return and a past event-week does not year-roll), then a weekday
    after until/returning/back. Naive dates take ``now``'s time of day
    in UTC. A parsed instant at or before ``now`` is unparseable.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    parsed = _parse_iso_date(text, now=now)
    if parsed is None:
        parsed = _parse_week_range(text, now=now)
    if parsed is None:
        parsed = _parse_month_day(text, now=now)
    if parsed is None:
        parsed = _parse_weekday(text, now=now)
    if parsed is None or parsed <= now:
        return None
    return parsed


def _at_time_of_day(day: datetime, now: datetime) -> datetime:
    return day.replace(
        hour=now.hour,
        minute=now.minute,
        second=0,
        microsecond=0,
        tzinfo=now.tzinfo,
    )


def _parse_iso_date(text: str, *, now: datetime) -> datetime | None:
    match = _ISO_DATE.search(text)
    if match is None:
        return None
    try:
        day = datetime.strptime(match.group(1), "%Y-%m-%d")
    except ValueError:
        return None
    return _at_time_of_day(day, now)


def _parse_week_range(text: str, *, now: datetime) -> datetime | None:
    """Year-less range containing now → day after end, same year (§V.169)."""
    match = _MONTH_DAY_RANGE.search(text)
    if match is None:
        return None
    month = _MONTH_LOOKUP[match.group(1).lower()]
    start_n = int(match.group(2))
    end_n = int(match.group(3))
    year = int(match.group(4)) if match.group(4) else now.year
    if end_n < start_n:
        return None
    try:
        start = datetime(year, month, start_n)
        end = datetime(year, month, end_n)
    except ValueError:
        return None
    if not (start.date() <= now.date() <= end.date()):
        return None
    return _at_time_of_day(end + timedelta(days=1), now)


def _parse_month_day(text: str, *, now: datetime) -> datetime | None:
    """Pick a month-day return; skip event-week; prefer return cues (§V.169)."""
    candidates: list[tuple[bool, datetime]] = []
    for match in _MONTH_DAY.finditer(text):
        if _is_week_of_event(text, match):
            continue
        target = _month_day_instant(text, match, now)
        if target is None:
            continue
        candidates.append((_has_return_cue_near(text, match), target))
    if not candidates:
        return None
    preferred = [instant for is_return, instant in candidates if is_return]
    pool = preferred or [instant for _, instant in candidates]
    ahead = [instant for instant in pool if instant > now]
    return ahead[-1] if ahead else pool[-1]


def _month_day_instant(
    text: str, match: re.Match[str], now: datetime
) -> datetime | None:
    month = _MONTH_LOOKUP[match.group(1).lower()]
    day_n = int(match.group(2))
    year = int(match.group(3)) if match.group(3) else now.year
    try:
        day = datetime(year, month, day_n)
    except ValueError:
        return None
    target = _at_time_of_day(day, now)
    if target <= now and match.group(3) is None:
        if _is_past_leave_start(text, match, target, now):
            return None
        if _is_same_day_on_absence(text, match, target, now):
            return _at_time_of_day(day + timedelta(days=1), now)
        try:
            target = target.replace(year=now.year + 1)
        except ValueError:
            return None
    return target


def _is_week_of_event(text: str, match: re.Match[str]) -> bool:
    """True for ``week of <Month> <D>`` — event-week, never a return (§B.145)."""
    return _WEEK_OF_PREFIX.search(text[: match.start()]) is not None


def _has_return_cue_near(text: str, match: re.Match[str]) -> bool:
    """True when until/returning/back/fully-back-online sits before this date."""
    prefix = text[: match.start()]
    window = prefix[-_RETURN_CUE_WINDOW:]
    return _RETURN_CUE.search(window) is not None


def _is_same_day_on_absence(
    text: str,
    match: re.Match[str],
    target: datetime,
    now: datetime,
) -> bool:
    """True for year-less ``on <Month> <D>`` named today (§V.169 / §B.142)."""
    if target.date() != now.date():
        return False
    return _ON_PREFIX.search(text[: match.start()]) is not None


def _is_past_leave_start(
    text: str,
    match: re.Match[str],
    target: datetime,
    now: datetime,
) -> bool:
    """True for weekday-month-day leave-start months past (§V.169 / §B.140)."""
    if not _is_months_past(target, now):
        return False
    if _RETURN_CUE.search(text) is not None:
        return False
    if _LEAVE_START_CUE.search(text) is None:
        return False
    return _WEEKDAY_PREFIX.search(text[: match.start()]) is not None


def _is_months_past(target: datetime, now: datetime) -> bool:
    if target.date() >= now.date():
        return False
    months = (now.year - target.year) * 12 + (now.month - target.month)
    return months >= _MONTHS_PAST_MIN


def _parse_weekday(text: str, *, now: datetime) -> datetime | None:
    match = _WEEKDAY.search(text)
    if match is None:
        return None
    target_wd = _WEEKDAYS_INDEX[match.group(1).lower()]
    days_ahead = (target_wd - now.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return _at_time_of_day(now + timedelta(days=days_ahead), now)


_WEEKDAYS_INDEX = {name: index for index, name in enumerate(_WEEKDAY_NAMES)}


def resolve_ooo_resume_at(
    email: Email,
    workflow: Workflow,
    *,
    now: datetime | None = None,
) -> datetime:
    """Return when the resume touch should fire (§V.169).

    Parseable return date wins (weekend-rolled so a cold send never lands
    Saturday/Sunday). Unparseable uses ``touch_interval_days``, or +3 days
    when the cadence pair is NULL.
    """
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    parsed = parse_ooo_return_at(_combined_text(email), now=now)
    if parsed is not None:
        return next_touch_scheduled_at(parsed, 0)
    interval = (
        workflow.touch_interval_days
        if workflow.touch_interval_days is not None
        else _NULL_CADENCE_RESUME_DAYS
    )
    return next_touch_scheduled_at(now, interval)


def schedule_ooo_resume(
    connection: psycopg.Connection[dict[str, Any]],
    workflow: Workflow,
    enrollment: Enrollment,
    email: Email,
    *,
    now: datetime | None = None,
) -> Task | None:
    """Schedule the next cadence touch after an OOO pause. Idempotent.

    Writes ``context.touch`` as a JSON number (§V.162) and
    ``reason=ooo_pause``. Skips when a pending OOO-resume already exists,
    when the enrollment is not active, or when the next touch would exceed
    a defined cadence. Returns the pending resume task, or None when none
    was created (and none already existed).
    """
    if enrollment.status != "active":
        return None
    existing = _pending_ooo_resume(connection, enrollment.id)
    if existing is not None:
        return existing

    emails_sent = _count_sent_outbound(connection, workflow.id, enrollment.contact_id)
    next_touch = emails_sent + 1
    if workflow.touches is not None and next_touch > workflow.touches:
        return None

    scheduled_at = resolve_ooo_resume_at(email, workflow, now=now)
    prior_id = _latest_sent_outbound_id(connection, workflow.id, enrollment.contact_id)
    context: dict[str, object] = {"touch": next_touch, "reason": "ooo_pause"}
    if prior_id is not None:
        context["prior_email_id"] = prior_id
    description = (
        f"Touch {next_touch} of {workflow.touches}"
        if workflow.touches is not None
        else f"Resume after OOO (touch {next_touch})"
    )
    task = create_task(
        connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=enrollment.contact_id,
        description=description,
        scheduled_at=scheduled_at.isoformat(),
        context=context,
        email_id=None,
    )
    logfire.info(
        "ooo.paused",
        enrollment_id=enrollment.id,
        email_id=email.id,
        touch=next_touch,
        scheduled_at=scheduled_at.isoformat(),
    )
    operator_event(
        "ooo.paused",
        enrollment_id=enrollment.id,
        email_id=email.id,
        touch=next_touch,
        scheduled_at=scheduled_at.isoformat(),
    )
    return task


def _pending_ooo_resume(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
) -> Task | None:
    row = connection.execute(
        """\
        SELECT id FROM task
        WHERE enrollment_id = %(enrollment_id)s
          AND status = 'pending'
          AND context->>'reason' = 'ooo_pause'
        ORDER BY scheduled_at ASC
        LIMIT 1
        """,
        {"enrollment_id": enrollment_id},
    ).fetchone()
    if row is None:
        return None
    return get_task(connection, row["id"])


def _count_sent_outbound(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> int:
    row = connection.execute(
        """\
        SELECT COUNT(*)::int AS n FROM email
        WHERE workflow_id = %(workflow_id)s
          AND contact_id = %(contact_id)s
          AND direction = 'outbound'
          AND status = 'sent'
        """,
        {"workflow_id": workflow_id, "contact_id": contact_id},
    ).fetchone()
    return int(row["n"]) if row is not None else 0


def _latest_sent_outbound_id(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> str | None:
    row = connection.execute(
        """\
        SELECT id FROM email
        WHERE workflow_id = %(workflow_id)s
          AND contact_id = %(contact_id)s
          AND direction = 'outbound'
          AND status = 'sent'
        ORDER BY sent_at DESC NULLS LAST, created_at DESC
        LIMIT 1
        """,
        {"workflow_id": workflow_id, "contact_id": contact_id},
    ).fetchone()
    return str(row["id"]) if row is not None else None
