"""Meeting CRUD and attendees."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg.sql import SQL, Composable

from mailpilot.database._common import (
    _build_update,
    _new_id,
)
from mailpilot.models import (
    Contact,
    Meeting,
    MeetingAttendee,
    MeetingSummary,
)

# -- Meeting -------------------------------------------------------------------


def create_meeting(
    connection: psycopg.Connection[dict[str, Any]],
    google_event_id: str | None = None,
    meet_url: str | None = None,
    summary: str = "",
    scheduled_at: datetime | None = None,
    ends_at: datetime | None = None,
    status: str = "scheduled",
) -> Meeting | None:
    """Create a meeting row, or return None on google_event_id conflict (§V.125).

    Insert is atomic via ``ON CONFLICT (google_event_id) DO NOTHING`` so a
    repeat ingest of the same calendar event never raises ``UniqueViolation``:
    one insert wins and returns the row, a racing duplicate returns ``None``
    (mirrors ``create_email`` §V.90). Rows with ``google_event_id=NULL`` never
    trigger the conflict (NULLs are distinct under a UNIQUE constraint).

    Args:
        connection: Open database connection.
        google_event_id: Google Calendar event id (nullable-unique ingest key).
        meet_url: Google Meet join URL.
        summary: Event summary/title.
        scheduled_at: Event start time (UTC datetime).
        ends_at: Event end time (UTC datetime).
        status: Meeting status (``scheduled``/``completed``/``cancelled``/
            ``no_show``); operator record-keeping only, gates nothing (§V.125).

    Returns:
        Created meeting, or None if a row with the same ``google_event_id``
        already exists.
    """
    row = connection.execute(
        """\
        INSERT INTO meeting (id, google_event_id, meet_url, summary,
            scheduled_at, ends_at, status)
        VALUES (%(id)s, %(google_event_id)s, %(meet_url)s, %(summary)s,
            %(scheduled_at)s, %(ends_at)s, %(status)s)
        ON CONFLICT (google_event_id) DO NOTHING
        RETURNING *
        """,
        {
            "id": _new_id(),
            "google_event_id": google_event_id,
            "meet_url": meet_url,
            "summary": summary,
            "scheduled_at": scheduled_at,
            "ends_at": ends_at,
            "status": status,
        },
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Meeting.model_validate(row)


def get_meeting(
    connection: psycopg.Connection[dict[str, Any]],
    meeting_id: str,
) -> Meeting | None:
    """Get a meeting by ID.

    Args:
        connection: Open database connection.
        meeting_id: Meeting ID.

    Returns:
        Meeting if found, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM meeting WHERE id = %(id)s",
        {"id": meeting_id},
    ).fetchone()
    if row is None:
        return None
    return Meeting.model_validate(row)


def get_meeting_by_google_event_id(
    connection: psycopg.Connection[dict[str, Any]],
    google_event_id: str,
) -> Meeting | None:
    """Resolve a meeting by its Google Calendar event id (§V.125).

    The idempotent-ingest lookup key (mirrors
    ``get_email_by_gmail_message_id`` §V.90). Returns ``None`` when no row
    carries the event id yet.
    """
    row = connection.execute(
        "SELECT * FROM meeting WHERE google_event_id = %(google_event_id)s",
        {"google_event_id": google_event_id},
    ).fetchone()
    if row is None:
        return None
    return Meeting.model_validate(row)


def list_meetings(
    connection: psycopg.Connection[dict[str, Any]],
    limit: int = 100,
    contact_id: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[MeetingSummary]:
    """List meetings, newest scheduled first, with optional filters (§V.125).

    Each summary carries ``attendee_emails`` + ``attendee_count`` (child
    aggregate over ``meeting_attendee`` joined to ``contact``, mirroring
    ``contact_count`` §V.96) via a single LATERAL join, so a
    ``--contact-email``-scoped result names who attends without a per-row
    attendee probe (§V.8, §B.112).

    Args:
        connection: Open database connection.
        limit: Maximum rows to return.
        contact_id: Scope to meetings linking this attendee contact (join over
            ``meeting_attendee``).
        status: Filter by meeting status.
        since: Lower bound (inclusive) on ``scheduled_at`` (ISO 8601).
        until: Upper bound (inclusive) on ``scheduled_at`` (ISO 8601).

    Returns:
        Matching meetings ordered by ``scheduled_at`` DESC NULLS LAST, each
        carrying its attendee summary.
    """
    clauses: list[Composable] = []
    params: dict[str, Any] = {"limit": limit}
    if contact_id is not None:
        clauses.append(
            SQL(
                "EXISTS (SELECT 1 FROM meeting_attendee ma "
                "WHERE ma.meeting_id = m.id AND ma.contact_id = %(contact_id)s)"
            )
        )
        params["contact_id"] = contact_id
    if status is not None:
        clauses.append(SQL("m.status = %(status)s"))
        params["status"] = status
    if since is not None:
        clauses.append(SQL("m.scheduled_at >= %(since)s"))
        params["since"] = since
    if until is not None:
        clauses.append(SQL("m.scheduled_at <= %(until)s"))
        params["until"] = until
    where = SQL("")
    if clauses:
        where = SQL("WHERE ") + SQL(" AND ").join(clauses)
    query = (
        SQL(
            "SELECT m.*, "
            "COALESCE(att.emails, ARRAY[]::text[]) AS attendee_emails, "
            "COALESCE(att.cnt, 0) AS attendee_count "
            "FROM meeting m "
            "LEFT JOIN LATERAL ("
            "SELECT array_agg(ct.email ORDER BY ct.email) AS emails, "
            "COUNT(*) AS cnt "
            "FROM meeting_attendee ma "
            "JOIN contact ct ON ct.id = ma.contact_id "
            "WHERE ma.meeting_id = m.id"
            ") att ON TRUE "
        )
        + where
        + SQL(
            " ORDER BY m.scheduled_at DESC NULLS LAST, m.created_at DESC "
            "LIMIT %(limit)s"
        )
    )
    rows = connection.execute(query, params).fetchall()
    return [MeetingSummary.model_validate(row) for row in rows]


def update_meeting(
    connection: psycopg.Connection[dict[str, Any]],
    meeting_id: str,
    **fields: object,
) -> Meeting | None:
    """Update a meeting by ID (§V.125).

    Only ``summary`` and ``status`` are operator-editable -- the ingest-owned
    columns (``google_event_id``, ``meet_url``, ``scheduled_at``, ``ends_at``)
    are refreshed by CalendarClient re-poll (§V.126), never edited from the CLI.
    ``status`` is record-keeping only and gates nothing (§V.125). Non-allowed
    fields are silently dropped; an empty update returns the row unchanged.

    Args:
        connection: Open database connection.
        meeting_id: Meeting ID.
        **fields: Fields to update (only ``summary`` / ``status`` honoured).

    Returns:
        Updated meeting, or None if not found.
    """
    allowed = {"summary", "status"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_meeting(connection, meeting_id)
    updates["id"] = meeting_id
    query = _build_update("meeting", updates, SQL("id = %(id)s"))
    row = connection.execute(query, updates).fetchone()
    connection.commit()
    if row is None:
        return None
    return Meeting.model_validate(row)


def upsert_meeting(
    connection: psycopg.Connection[dict[str, Any]],
    google_event_id: str,
    meet_url: str | None = None,
    summary: str = "",
    scheduled_at: datetime | None = None,
    ends_at: datetime | None = None,
    status: str = "scheduled",
) -> Meeting:
    """Insert or update a meeting keyed on google_event_id (§V.125, idempotent).

    Re-polling the same calendar event updates the existing row in place rather
    than creating a duplicate (``ON CONFLICT (google_event_id) DO UPDATE``,
    mirrors ``upsert_sync_status``). The ingest key is required here -- a
    NULL-keyed meeting cannot be idempotently upserted, so callers without an
    event id use ``create_meeting``. ``updated_at`` is bumped on every update.

    Args:
        connection: Open database connection.
        google_event_id: Google Calendar event id (required ingest key).
        meet_url: Google Meet join URL.
        summary: Event summary/title.
        scheduled_at: Event start time (UTC datetime).
        ends_at: Event end time (UTC datetime).
        status: Meeting status (operator record-keeping only, §V.125).

    Returns:
        The inserted or updated meeting row.
    """
    row = connection.execute(
        """\
        INSERT INTO meeting (id, google_event_id, meet_url, summary,
            scheduled_at, ends_at, status)
        VALUES (%(id)s, %(google_event_id)s, %(meet_url)s, %(summary)s,
            %(scheduled_at)s, %(ends_at)s, %(status)s)
        ON CONFLICT (google_event_id) DO UPDATE
            SET meet_url = EXCLUDED.meet_url,
                summary = EXCLUDED.summary,
                scheduled_at = EXCLUDED.scheduled_at,
                ends_at = EXCLUDED.ends_at,
                status = EXCLUDED.status,
                updated_at = CURRENT_TIMESTAMP
        RETURNING *
        """,
        {
            "id": _new_id(),
            "google_event_id": google_event_id,
            "meet_url": meet_url,
            "summary": summary,
            "scheduled_at": scheduled_at,
            "ends_at": ends_at,
            "status": status,
        },
    ).fetchone()
    connection.commit()
    return Meeting.model_validate(row)


def link_meeting_attendee(
    connection: psycopg.Connection[dict[str, Any]],
    meeting_id: str,
    contact_id: str,
) -> MeetingAttendee | None:
    """Link a contact to a meeting as an attendee (§V.125).

    Idempotent via ``ON CONFLICT DO NOTHING`` on the ``(meeting_id,
    contact_id)`` UNIQUE pair: a repeat link returns ``None`` (no duplicate
    row), a fresh link returns the created row.

    Raises:
        ValueError: If the meeting or contact does not exist.
    """
    if (
        connection.execute(
            "SELECT 1 FROM meeting WHERE id = %s", (meeting_id,)
        ).fetchone()
        is None
    ):
        raise ValueError(f"meeting not found: {meeting_id}")
    if (
        connection.execute(
            "SELECT 1 FROM contact WHERE id = %s", (contact_id,)
        ).fetchone()
        is None
    ):
        raise ValueError(f"contact not found: {contact_id}")
    row = connection.execute(
        """\
        INSERT INTO meeting_attendee (id, meeting_id, contact_id)
        VALUES (%(id)s, %(meeting_id)s, %(contact_id)s)
        ON CONFLICT (meeting_id, contact_id) DO NOTHING
        RETURNING *
        """,
        {"id": _new_id(), "meeting_id": meeting_id, "contact_id": contact_id},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return MeetingAttendee.model_validate(row)


def list_meeting_attendees(
    connection: psycopg.Connection[dict[str, Any]],
    meeting_id: str,
) -> list[Contact]:
    """List the contacts attending a meeting (§V.8, the reader for §B.112).

    Joins ``meeting_attendee`` to ``contact`` so the operator reads who attends.
    The reader half of the link relation whose writer is
    ``link_meeting_attendee`` and whose filter is ``meeting list
    --contact-email`` -- without it the booking conclusion (§V.128) is reachable
    only by raw SQL.

    Args:
        connection: Open database connection.
        meeting_id: Meeting ID.

    Returns:
        Attendee contacts ordered by email; empty list when none are linked.
    """
    rows = connection.execute(
        """\
        SELECT ct.*
        FROM meeting_attendee ma
        JOIN contact ct ON ct.id = ma.contact_id
        WHERE ma.meeting_id = %(meeting_id)s
        ORDER BY ct.email
        """,
        {"meeting_id": meeting_id},
    ).fetchall()
    return [Contact.model_validate(row) for row in rows]
