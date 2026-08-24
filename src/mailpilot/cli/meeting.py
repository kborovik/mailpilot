"""Meeting commands."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import click

from mailpilot._filters import (
    enum_option,
    limit_option,
    scope_option,
    time_window_options,
)
from mailpilot.cli.main import (
    _db,
    _resolve_contact,
    main,
    output,
    output_entity,
    output_error,
)

_MEETING_STATUSES = ["scheduled", "completed", "cancelled", "no_show"]

# -- Meeting commands ----------------------------------------------------------


@main.group()
def meeting() -> None:
    """Inspect calendar meetings ingested from Google Calendar.

    Rows are created by calendar ingestion, never by the operator -- there is
    no `meeting create`.
    """


@meeting.command("list")
@scope_option("--contact-email", "contact_email", "Filter by attendee (email or ID).")
@enum_option("--status", "status", _MEETING_STATUSES, "Filter by meeting status.")
@time_window_options("scheduled_at")
@limit_option
def meeting_list(
    contact_email: str | None,
    status: str | None,
    limit: int,
    since: str | None,
    until: str | None,
) -> None:
    """List meetings, newest scheduled first, with optional filters."""
    from mailpilot.database import list_meetings

    with _db() as connection:
        contact_id = (
            _resolve_contact(connection, contact_email).id
            if contact_email is not None
            else None
        )
        meetings = list_meetings(
            connection,
            limit=limit,
            contact_id=contact_id,
            status=status,
            since=since,
            until=until,
        )
        output({"meetings": [m.model_dump(mode="json") for m in meetings]})


@meeting.command("view")
@click.argument("meeting_id")
def meeting_view(meeting_id: str) -> None:
    """Show a meeting by ID with its attendee contacts inlined."""
    from mailpilot.database import load_meeting_view

    with _db() as connection:
        found = load_meeting_view(connection, meeting_id)
        if found is None:
            output_error(f"meeting not found: {meeting_id}", "not_found")
        output_entity("meeting", found)


@meeting.command("add")
@click.argument("meeting_id")
@click.option(
    "--contact-email",
    required=True,
    help="Attendee contact to link (email or ID).",
)
def meeting_add(meeting_id: str, contact_email: str) -> None:
    """Link an attendee contact to a meeting.

    A repeat link on the same (meeting, contact) pair is rejected
    `already_exists` -- the pair is unique.
    """
    from mailpilot.database import (
        get_meeting,
        link_meeting_attendee,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    with _db(mutate=True) as connection:
        if get_meeting(connection, meeting_id) is None:
            output_error(f"meeting not found: {meeting_id}", "not_found")
        contact_id = _resolve_contact(connection, contact_email).id
        with cli_mutation(
            "meeting", "add", entity_id=meeting_id, contact_id=contact_id
        ):
            created = link_meeting_attendee(connection, meeting_id, contact_id)
            if created is None:
                output_error(
                    f"contact {contact_id} already attends meeting {meeting_id}",
                    "already_exists",
                )
            operator_event(
                "meeting.add",
                entity_id=meeting_id,
                contact_id=contact_id,
                changed=["contact_id"],
            )
            output_entity("meeting_attendee", created)


@meeting.command("update")
@click.argument("meeting_id")
@click.option("--summary", default=None, help="Meeting summary/title.")
@click.option(
    "--status",
    type=click.Choice(_MEETING_STATUSES),
    default=None,
    help="Meeting status (record-keeping only, gates nothing).",
)
def meeting_update(meeting_id: str, summary: str | None, status: str | None) -> None:
    """Edit a meeting's summary or status."""
    from mailpilot.database import get_meeting, update_meeting
    from mailpilot.operator_log import cli_mutation, operator_event

    with _db(mutate=True) as connection:
        before = get_meeting(connection, meeting_id)
        if before is None:
            output_error(f"meeting not found: {meeting_id}", "not_found")
        fields: dict[str, object] = {}
        if summary is not None:
            fields["summary"] = summary
        if status is not None:
            fields["status"] = status
        with cli_mutation("meeting", "update", entity_id=meeting_id):
            updated = update_meeting(connection, meeting_id, **fields)
            if updated is None:
                output_error(f"meeting not found: {meeting_id}", "not_found")
            changed = [
                field
                for field in ("summary", "status")
                if getattr(before, field) != getattr(updated, field)
            ]
            operator_event("meeting.update", entity_id=meeting_id, changed=changed)
            output_entity("meeting", updated)


@meeting.command("cancel")
@click.argument("meeting_id")
def meeting_cancel(meeting_id: str) -> None:
    """Cancel a meeting by setting its status to `cancelled`."""
    from mailpilot.database import get_meeting, update_meeting
    from mailpilot.operator_log import cli_mutation, operator_event

    with _db(mutate=True) as connection:
        if get_meeting(connection, meeting_id) is None:
            output_error(f"meeting not found: {meeting_id}", "not_found")
        with cli_mutation("meeting", "cancel", entity_id=meeting_id):
            updated = update_meeting(connection, meeting_id, status="cancelled")
            if updated is None:
                output_error(f"meeting not found: {meeting_id}", "not_found")
            operator_event("meeting.cancel", entity_id=meeting_id, changed=["status"])
            output_entity("meeting", updated)
