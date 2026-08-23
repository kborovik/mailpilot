"""Tests for calendar event ingestion + booking conclusion (§V.126, §V.128)."""

from __future__ import annotations

from typing import Any, LiteralString
from unittest.mock import patch

import psycopg

from conftest import (
    make_test_account,
    make_test_contact,
    make_test_enrollment,
    make_test_workflow,
)
from mailpilot.calendar import CalendarEvent
from mailpilot.database import create_task, get_task, record_enrollment_outcome
from mailpilot.sync import (
    _poll_account_calendar,  # pyright: ignore[reportPrivateUsage]
    ingest_calendar_event,
)

_FAR_FUTURE = "2099-12-31T00:00:00Z"


def _event(
    *attendee_emails: str,
    google_event_id: str = "evt-1",
    summary: str = "Intro call",
) -> CalendarEvent:
    return CalendarEvent(
        google_event_id=google_event_id,
        summary=summary,
        meet_url="https://meet.google.com/abc-defg-hij",
        scheduled_at=None,
        ends_at=None,
        attendee_emails=attendee_emails,
    )


def _count(
    connection: psycopg.Connection[dict[str, Any]],
    sql: LiteralString,
    params: tuple[Any, ...],
) -> int:
    row = connection.execute(sql, params).fetchone()
    assert row is not None
    return int(next(iter(row.values())))


def _completed_count(
    connection: psycopg.Connection[dict[str, Any]], contact_id: str
) -> int:
    return _count(
        connection,
        "SELECT COUNT(*) FROM activity "
        "WHERE contact_id = %s AND type = 'enrollment_completed'",
        (contact_id,),
    )


def _note_count(connection: psycopg.Connection[dict[str, Any]], contact_id: str) -> int:
    return _count(
        connection, "SELECT COUNT(*) FROM note WHERE contact_id = %s", (contact_id,)
    )


def test_ingest_links_matched_attendee_and_skips_unmatched(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.126: a matched attendee links; an unmatched email produces no link."""
    make_test_account(database_connection)
    contact = make_test_contact(database_connection, email="prospect@acme.com")

    meeting = ingest_calendar_event(
        database_connection, _event("prospect@acme.com", "stranger@nowhere.com")
    )

    # One meeting row, keyed on the event id.
    assert meeting.google_event_id == "evt-1"
    assert _count(database_connection, "SELECT COUNT(*) FROM meeting", ()) == 1
    # Exactly one attendee link -- the matched contact, not the stranger.
    links = database_connection.execute(
        "SELECT contact_id FROM meeting_attendee WHERE meeting_id = %s", (meeting.id,)
    ).fetchall()
    assert [row["contact_id"] for row in links] == [contact.id]
    # The unmatched email never created a contact row.
    assert (
        _count(
            database_connection,
            "SELECT COUNT(*) FROM contact WHERE email = %s",
            ("stranger@nowhere.com",),
        )
        == 0
    )


def test_booking_concludes_active_outbound_enrollment_and_cancels_followups(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.128: booking concludes the enrollment, cancels future follow-ups,
    preserves the first-touch (§V.32), and writes a note."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="outbound"
    )
    contact = make_test_contact(database_connection, email="prospect@acme.com")
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    first_touch = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="scheduled first reach-out",
        scheduled_at=_FAR_FUTURE,
        context={"trigger": "enrollment_schedule"},
    )
    followup = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="breakup touch",
        scheduled_at=_FAR_FUTURE,
        context={"trigger": "followup"},
    )

    ingest_calendar_event(database_connection, _event("prospect@acme.com"))

    assert _completed_count(database_connection, contact.id) == 1
    assert _note_count(database_connection, contact.id) == 1
    # The future cold follow-up is cancelled.
    cancelled = get_task(database_connection, followup.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    # The operator first-touch is preserved (§V.32 exclusion).
    preserved = get_task(database_connection, first_touch.id)
    assert preserved is not None
    assert preserved.status == "pending"


def test_booking_conclusion_persists_meeting_booked_disposition(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.132: booking conclusion stamps ``detail.disposition = meeting_booked``."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="outbound"
    )
    contact = make_test_contact(database_connection, email="prospect@acme.com")
    make_test_enrollment(database_connection, workflow.id, contact.id)

    ingest_calendar_event(database_connection, _event("prospect@acme.com"))

    rows = database_connection.execute(
        "SELECT detail->>'disposition' AS disposition FROM activity "
        "WHERE contact_id = %s AND type = 'enrollment_completed'",
        (contact.id,),
    ).fetchall()
    assert [row["disposition"] for row in rows] == ["meeting_booked"]


def test_repoll_does_not_duplicate_meeting_or_conclusion(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.125/§V.128: re-polling the same event creates no duplicate row and
    concludes the enrollment exactly once."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="outbound"
    )
    contact = make_test_contact(database_connection, email="prospect@acme.com")
    make_test_enrollment(database_connection, workflow.id, contact.id)

    ingest_calendar_event(database_connection, _event("prospect@acme.com"))
    ingest_calendar_event(database_connection, _event("prospect@acme.com"))

    assert _count(database_connection, "SELECT COUNT(*) FROM meeting", ()) == 1
    assert _count(database_connection, "SELECT COUNT(*) FROM meeting_attendee", ()) == 1
    assert _completed_count(database_connection, contact.id) == 1
    assert _note_count(database_connection, contact.id) == 1


def test_multi_attendee_concludes_every_attendee_enrollment(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.128: a meeting with two attendee contacts concludes both their
    active outbound enrollments."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="outbound"
    )
    contact_a = make_test_contact(database_connection, email="a@acme.com")
    contact_b = make_test_contact(database_connection, email="b@acme.com")
    make_test_enrollment(database_connection, workflow.id, contact_a.id)
    make_test_enrollment(database_connection, workflow.id, contact_b.id)

    ingest_calendar_event(database_connection, _event("a@acme.com", "b@acme.com"))

    assert _completed_count(database_connection, contact_a.id) == 1
    assert _completed_count(database_connection, contact_b.id) == 1


def test_booking_concludes_every_outbound_enrollment_the_attendee_holds(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.128: fan-out fires for EVERY active outbound enrollment, not just one."""
    account = make_test_account(database_connection)
    workflow_one = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="outbound-one",
        workflow_type="outbound",
    )
    workflow_two = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="outbound-two",
        workflow_type="outbound",
    )
    contact = make_test_contact(database_connection, email="prospect@acme.com")
    make_test_enrollment(database_connection, workflow_one.id, contact.id)
    make_test_enrollment(database_connection, workflow_two.id, contact.id)

    ingest_calendar_event(database_connection, _event("prospect@acme.com"))

    assert _completed_count(database_connection, contact.id) == 2


def test_booking_concludes_already_terminal_enrollment(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.128 / §V.186: skip_if_terminal default false still concludes terminal."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="outbound"
    )
    contact = make_test_contact(database_connection, email="prospect@acme.com")
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    record_enrollment_outcome(
        database_connection,
        enrollment.id,
        "failed",
        "sequence exhausted",
        disposition="contact_later",
    )

    ingest_calendar_event(database_connection, _event("prospect@acme.com"))

    assert _completed_count(database_connection, contact.id) == 1
    count_row = database_connection.execute(
        "SELECT COUNT(*) AS n FROM activity "
        "WHERE enrollment_id = %s AND type IN "
        "('enrollment_completed', 'enrollment_failed')",
        (enrollment.id,),
    ).fetchone()
    assert count_row is not None
    assert count_row["n"] == 2


def test_inbound_enrollment_not_concluded_by_booking(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.128: booking concludes only active outbound enrollments."""
    account = make_test_account(database_connection)
    inbound = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    contact = make_test_contact(database_connection, email="prospect@acme.com")
    make_test_enrollment(database_connection, inbound.id, contact.id)

    ingest_calendar_event(database_connection, _event("prospect@acme.com"))

    # The attendee still links to the meeting...
    assert _count(database_connection, "SELECT COUNT(*) FROM meeting_attendee", ()) == 1
    # ...but the inbound enrollment is left untouched.
    assert _completed_count(database_connection, contact.id) == 0
    assert _note_count(database_connection, contact.id) == 0


# -- _poll_account_calendar (the shared per-account helper, §V.126) ------------


def test_poll_account_calendar_ingests_event_and_concludes_booking(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.126/§V.128: the helper polls one account's calendar, ingests the
    upcoming event, and concludes the attendee's active outbound enrollment.

    This is the unit the ``account sync`` CLI path calls per account.
    """
    account = make_test_account(database_connection)
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="outbound"
    )
    contact = make_test_contact(database_connection, email="prospect@acme.com")
    make_test_enrollment(database_connection, workflow.id, contact.id)

    with patch("mailpilot.sync.CalendarClient") as mock_client_cls:
        mock_client_cls.return_value.list_upcoming_events.return_value = [
            _event("prospect@acme.com")
        ]
        error = _poll_account_calendar(database_connection, account)

    assert error is None
    mock_client_cls.assert_called_once_with(account.email)
    assert _count(database_connection, "SELECT COUNT(*) FROM meeting", ()) == 1
    assert _completed_count(database_connection, contact.id) == 1


def test_poll_account_calendar_repoll_no_duplicate_concludes_once(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.125/§V.128: re-polling the same google_event_id creates no duplicate
    meeting row and concludes the enrollment exactly once."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="outbound"
    )
    contact = make_test_contact(database_connection, email="prospect@acme.com")
    make_test_enrollment(database_connection, workflow.id, contact.id)

    with patch("mailpilot.sync.CalendarClient") as mock_client_cls:
        mock_client_cls.return_value.list_upcoming_events.return_value = [
            _event("prospect@acme.com")
        ]
        assert _poll_account_calendar(database_connection, account) is None
        assert _poll_account_calendar(database_connection, account) is None

    assert _count(database_connection, "SELECT COUNT(*) FROM meeting", ()) == 1
    assert _count(database_connection, "SELECT COUNT(*) FROM meeting_attendee", ()) == 1
    assert _completed_count(database_connection, contact.id) == 1
    assert _note_count(database_connection, contact.id) == 1


def test_poll_account_calendar_isolates_transport_error(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.126: a calendar transport fault is isolated -- the helper logs it,
    returns the error string, and never raises (so the Gmail sync survives)."""
    account = make_test_account(database_connection)

    with (
        patch("mailpilot.sync.CalendarClient") as mock_client_cls,
        patch("logfire.exception"),
    ):
        mock_client_cls.return_value.list_upcoming_events.side_effect = RuntimeError(
            "calendar 500"
        )
        error = _poll_account_calendar(database_connection, account)

    assert error == "calendar 500"
    # The failed poll ingested nothing.
    assert _count(database_connection, "SELECT COUNT(*) FROM meeting", ()) == 0
