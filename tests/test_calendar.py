"""Tests for the Calendar API client wrapper (§V.126)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from mailpilot.calendar import (
    _CALENDAR_SCOPE,  # pyright: ignore[reportPrivateUsage]
    CalendarClient,
)


def _make_service(items: list[dict[str, object]] | None = None) -> MagicMock:
    """Build a mock Calendar service whose events().list().execute() returns items."""
    service = MagicMock()
    list_handle = MagicMock()
    list_handle.execute.return_value = {"items": items or []}
    service.events.return_value.list.return_value = list_handle
    return service


def test_list_upcoming_events_parses_event_fields() -> None:
    service = _make_service(
        items=[
            {
                "id": "evt-1",
                "summary": "Intro call",
                "hangoutLink": "https://meet.google.com/abc-defg-hij",
                "start": {"dateTime": "2026-07-01T15:00:00+00:00"},
                "end": {"dateTime": "2026-07-01T15:30:00+00:00"},
                "attendees": [
                    {"email": "prospect@acme.com"},
                    {"email": "rep@lab5.ca"},
                ],
            }
        ]
    )
    client = CalendarClient.from_service("rep@lab5.ca", service)

    events = client.list_upcoming_events()

    assert len(events) == 1
    event = events[0]
    assert event.google_event_id == "evt-1"
    assert event.summary == "Intro call"
    assert event.meet_url == "https://meet.google.com/abc-defg-hij"
    assert event.scheduled_at == datetime.fromisoformat("2026-07-01T15:00:00+00:00")
    assert event.ends_at == datetime.fromisoformat("2026-07-01T15:30:00+00:00")
    assert event.attendee_emails == ("prospect@acme.com", "rep@lab5.ca")


def test_meet_url_falls_back_to_conference_data_video_entry_point() -> None:
    service = _make_service(
        items=[
            {
                "id": "evt-2",
                "summary": "Demo",
                "conferenceData": {
                    "entryPoints": [
                        {"entryPointType": "phone", "uri": "tel:+1-555"},
                        {
                            "entryPointType": "video",
                            "uri": "https://meet.google.com/xyz",
                        },
                    ]
                },
                "start": {"dateTime": "2026-07-02T09:00:00+00:00"},
                "end": {"dateTime": "2026-07-02T09:30:00+00:00"},
            }
        ]
    )
    client = CalendarClient.from_service("rep@lab5.ca", service)

    events = client.list_upcoming_events()

    assert events[0].meet_url == "https://meet.google.com/xyz"
    assert events[0].attendee_emails == ()


def test_event_without_meet_link_has_none_meet_url() -> None:
    service = _make_service(
        items=[
            {
                "id": "evt-3",
                "summary": "No video",
                "start": {"dateTime": "2026-07-03T09:00:00+00:00"},
                "end": {"dateTime": "2026-07-03T09:30:00+00:00"},
            }
        ]
    )
    client = CalendarClient.from_service("rep@lab5.ca", service)

    assert client.list_upcoming_events()[0].meet_url is None


def test_cancelled_event_is_skipped() -> None:
    service = _make_service(
        items=[
            {"id": "evt-live", "status": "confirmed", "summary": "Live"},
            {"id": "evt-dead", "status": "cancelled", "summary": "Cancelled"},
        ]
    )
    client = CalendarClient.from_service("rep@lab5.ca", service)

    events = client.list_upcoming_events()

    assert [e.google_event_id for e in events] == ["evt-live"]


def test_event_without_id_is_skipped() -> None:
    service = _make_service(items=[{"summary": "no id"}, {"id": "evt-ok"}])
    client = CalendarClient.from_service("rep@lab5.ca", service)

    events = client.list_upcoming_events()

    assert [e.google_event_id for e in events] == ["evt-ok"]


def test_attendees_without_email_are_dropped() -> None:
    service = _make_service(
        items=[
            {
                "id": "evt-4",
                "attendees": [
                    {"email": "has@acme.com"},
                    {"displayName": "Room A"},
                ],
            }
        ]
    )
    client = CalendarClient.from_service("rep@lab5.ca", service)

    assert client.list_upcoming_events()[0].attendee_emails == ("has@acme.com",)


def test_list_upcoming_events_query_scopes_primary_calendar_expanded() -> None:
    service = _make_service()
    client = CalendarClient.from_service("rep@lab5.ca", service)

    client.list_upcoming_events()

    call_kwargs = service.events.return_value.list.call_args.kwargs
    assert call_kwargs["calendarId"] == "primary"
    assert call_kwargs["singleEvents"] is True
    assert call_kwargs["orderBy"] == "startTime"
    # timeMin pins the lower bound to "now" so only upcoming events return.
    assert call_kwargs.get("timeMin")


def test_calendar_client_uses_readonly_scope() -> None:
    """§C: Calendar client impersonates over the read-only events scope."""
    with (
        patch("mailpilot.google_auth.build_delegated_credentials") as mock_creds,
        patch("googleapiclient.discovery.build") as mock_build,
    ):
        mock_creds.return_value = MagicMock()

        CalendarClient("rep@lab5.ca")

    assert _CALENDAR_SCOPE == [
        "https://www.googleapis.com/auth/calendar.events.readonly"
    ]
    mock_creds.assert_called_once_with(_CALENDAR_SCOPE, "rep@lab5.ca")
    build_args = mock_build.call_args.args
    build_kwargs = mock_build.call_args.kwargs
    assert build_args == ("calendar", "v3")
    assert build_kwargs == {"credentials": mock_creds.return_value}
