"""Google Calendar API client for reading upcoming events (§V.126).

Authentication: same service account + domain-wide delegation as
:mod:`mailpilot.google_auth`. Per-account impersonation via ``with_subject``.

Scope: ``https://www.googleapis.com/auth/calendar.events.readonly``
(read-only). This is the first scope added beyond Gmail ``gmail.modify``
and Drive ``drive.readonly`` (§C).

The sync loop polls this client on the run-interval tick (§V.21 fallback)
to ingest upcoming events as ``meeting`` rows. The app never creates or
updates calendar events -- reads only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar

from mailpilot.google_auth import GoogleClient

CalendarService = Any
"""Type alias for the Calendar API service resource (untyped by Google)."""

_CALENDAR_SCOPE = ["https://www.googleapis.com/auth/calendar.events.readonly"]

_CALENDAR_MAX_RESULTS = 250
"""Upper bound on events returned by one ``list_upcoming_events`` poll.

The run-interval poll is a fallback (§V.21); a single page of upcoming
events is sufficient. Pagination of the full upcoming window is out of
scope (event-wake push channels deferred to #154)."""


@dataclass(frozen=True)
class CalendarEvent:
    """A normalized upcoming calendar event (§V.126).

    The fields mirror the ``meeting`` row columns the sync ingestion upserts
    (§V.125): ``google_event_id`` is the idempotent ingest key, ``meet_url``
    the Google Meet join link when present, and ``attendee_emails`` the raw
    attendee addresses matched to contacts by email at ingest time.
    """

    google_event_id: str
    summary: str
    meet_url: str | None
    scheduled_at: datetime | None
    ends_at: datetime | None
    attendee_emails: tuple[str, ...]


def _extract_meet_url(raw: dict[str, Any]) -> str | None:
    """Pull the Google Meet join URL from a raw event, or None.

    Prefers the top-level ``hangoutLink``; falls back to the first
    ``conferenceData`` entry point of type ``video``.
    """
    hangout_link = raw.get("hangoutLink")
    if hangout_link:
        return str(hangout_link)
    conference_data: dict[str, Any] = raw.get("conferenceData") or {}
    for entry_point in conference_data.get("entryPoints", []):
        if entry_point.get("entryPointType") == "video" and entry_point.get("uri"):
            return str(entry_point["uri"])
    return None


def _parse_event_time(node: dict[str, Any] | None) -> datetime | None:
    """Parse a Calendar ``start``/``end`` node to a datetime, or None.

    Prefers the timed ``dateTime`` (RFC 3339, possibly carrying an offset
    or ``Z``); falls back to the all-day ``date``. Returns None when the
    node is absent or carries neither.
    """
    if not node:
        return None
    raw_value = node.get("dateTime") or node.get("date")
    if not raw_value:
        return None
    return datetime.fromisoformat(str(raw_value))


def _normalize_event(raw: dict[str, Any]) -> CalendarEvent | None:
    """Normalize a raw Calendar API event into a :class:`CalendarEvent`.

    Returns None when the event carries no id (unkeyed, cannot be ingested
    idempotently per §V.125) or has been cancelled (``status='cancelled'``);
    a cancelled event must not conclude an enrollment.
    """
    event_id = raw.get("id")
    if not event_id:
        return None
    if raw.get("status") == "cancelled":
        return None
    attendee_emails = tuple(
        str(attendee["email"])
        for attendee in raw.get("attendees", [])
        if attendee.get("email")
    )
    return CalendarEvent(
        google_event_id=str(event_id),
        summary=str(raw.get("summary") or ""),
        meet_url=_extract_meet_url(raw),
        scheduled_at=_parse_event_time(raw.get("start")),
        ends_at=_parse_event_time(raw.get("end")),
        attendee_emails=attendee_emails,
    )


class CalendarClient(GoogleClient):
    """Thin wrapper around the Calendar v3 service for event reads (§V.126).

    Holds the impersonated service for one account, mirroring
    :class:`mailpilot.gmail.GmailClient` and
    :class:`mailpilot.drive.DriveClient`. Construction triggers the
    credentials build; tests should use :meth:`from_service`.

    Usage::

        client = CalendarClient("user@example.com")
        events = client.list_upcoming_events()
    """

    _api = "calendar"
    _version = "v3"
    _scopes: ClassVar[list[str]] = _CALENDAR_SCOPE

    def list_upcoming_events(
        self, max_results: int = _CALENDAR_MAX_RESULTS
    ) -> list[CalendarEvent]:
        """List upcoming events on the account's primary calendar (§V.126).

        Queries events from now forward, expanding recurring events to single
        instances and ordering by start time. Cancelled and unkeyed events are
        dropped (see :func:`_normalize_event`).

        Args:
            max_results: Maximum events to request in one poll.

        Returns:
            Normalized upcoming events, soonest first.
        """
        time_min = datetime.now(UTC).isoformat()
        response: dict[str, Any] = (
            self._service.events()
            .list(
                calendarId="primary",
                timeMin=time_min,
                singleEvents=True,
                orderBy="startTime",
                maxResults=max_results,
            )
            .execute()
        )
        events: list[CalendarEvent] = []
        for raw in response.get("items", []):
            event = _normalize_event(raw)
            if event is not None:
                events.append(event)
        return events
