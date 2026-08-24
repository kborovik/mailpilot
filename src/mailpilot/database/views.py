"""Composite view loaders (§V.8)."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.sql import SQL, Identifier

from mailpilot.database._common import (
    _INLINE_NOTES_CAP,
)
from mailpilot.database.activity import (
    list_activities,
)
from mailpilot.database.company import (
    get_company,
    list_company_aliases,
)
from mailpilot.database.contact import (
    get_contact,
    list_contacts,
)
from mailpilot.database.email import (
    list_emails,
)
from mailpilot.database.enrollment import (
    list_enrollments_detailed,
)
from mailpilot.database.meeting import (
    get_meeting,
    list_meeting_attendees,
)
from mailpilot.database.tag import (
    list_tags,
)
from mailpilot.models import (
    CompanyView,
    ContactView,
    MeetingView,
    Note,
)


def _load_notes_for_owner(
    connection: psycopg.Connection[dict[str, Any]],
    owner_column: str,
    owner_id: str,
) -> tuple[list[Note], int]:
    """Fetch latest notes for a single owner column plus the total row count.

    Two queries (no JOIN) per §V.8: ``LIMIT _INLINE_NOTES_CAP ORDER BY
    created_at DESC`` for the inline list, ``COUNT(*)`` for the total.
    """
    if owner_column not in {"contact_id", "company_id"}:
        raise ValueError(f"unsupported owner column: {owner_column}")
    list_query = SQL(
        "SELECT * FROM note WHERE {col} = %s ORDER BY created_at DESC LIMIT %s"
    ).format(col=Identifier(owner_column))
    rows = connection.execute(list_query, (owner_id, _INLINE_NOTES_CAP)).fetchall()
    notes = [Note.model_validate(row) for row in rows]
    count_query = SQL("SELECT COUNT(*) AS total FROM note WHERE {col} = %s").format(
        col=Identifier(owner_column)
    )
    count_row = connection.execute(count_query, (owner_id,)).fetchone()
    total = int(count_row["total"]) if count_row is not None else 0
    return notes, total


# §V.159: default / hard cap for contact view --timeline section sizes.
_TIMELINE_DEFAULT_LIMIT = 10
_TIMELINE_HARD_CAP = 50


def load_contact_view(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
) -> ContactView | None:
    """Load a contact with inlined notes (own + parent company) per §V.8.

    Returns ``None`` when the contact does not exist. ``notes`` and
    ``company_notes`` are capped at ``_INLINE_NOTES_CAP`` rows each, ordered
    by ``created_at`` DESC, full body verbatim. Totals reflect the actual row
    count, not the cap. ``company_notes`` is always ``[]`` when the contact
    has no parent company.

    The projection is a base-entity superset of agent-facing columns per
    §V.8: every ``Contact`` column except operator-only
    ``verification_meta`` (§V.144) is forwarded, and ``company_domain`` is
    fetched from the parent company (LEFT JOIN semantics, NULL when the
    contact has no company). ``tags`` is the assigned tag-name list
    (empty ok; same shape as ``ContactSummary.tags``). Meta is never on
    this path — operators use ``contact view --include-meta``.
    """
    contact = get_contact(connection, contact_id)
    if contact is None:
        return None
    notes, notes_total = _load_notes_for_owner(connection, "contact_id", contact_id)
    if contact.company_id is not None:
        company = get_company(connection, contact.company_id)
        company_domain = company.domain if company is not None else None
        company_notes, company_notes_total = _load_notes_for_owner(
            connection, "company_id", contact.company_id
        )
    else:
        company_domain = None
        company_notes = []
        company_notes_total = 0
    owner_tags = list_tags(
        connection,
        contact_id=contact_id,
        limit=1_000_000,
        include_disabled=True,
    )
    # Strip operator-only meta so agent prompt + default CLI stay byte-identical
    # and never carry verification trails (§V.144 / §V.8).
    return ContactView(
        **contact.model_dump(exclude={"verification_meta"}),
        company_domain=company_domain,
        tags=[t.name for t in owner_tags],
        notes=notes,
        notes_total=notes_total,
        company_notes=company_notes,
        company_notes_total=company_notes_total,
    )


def load_contact_timeline(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
    *,
    limit: int = _TIMELINE_DEFAULT_LIMIT,
) -> dict[str, Any] | None:
    """Load contact view plus bounded enrollments/emails/activities (§V.159).

    Returns ``None`` when the contact does not exist. Composes
    ``load_contact_view`` with denser enrollment rows (status, disposition,
    last/next touch), recent emails, and recent activities. Each list is
    capped at ``limit`` (clamped to ``[_TIMELINE_DEFAULT_LIMIT range,
    _TIMELINE_HARD_CAP]``). Disabled / do_not_contact contacts are loaded
    normally (forensics). Does not rewrite Gmail bodies.

    The bare ``load_contact_view`` path is unchanged — timeline keys only
    appear on this opt-in loader.
    """
    view = load_contact_view(connection, contact_id)
    if view is None:
        return None
    n = max(1, min(int(limit), _TIMELINE_HARD_CAP))
    enrollments = list_enrollments_detailed(
        connection, contact_id=contact_id, full=True, limit=n
    )
    emails = list_emails(connection, contact_id=contact_id, limit=n)
    activities = list_activities(connection, contact_id=contact_id, limit=n)
    payload = view.model_dump(mode="json")
    payload["enrollments"] = [e.model_dump(mode="json") for e in enrollments]
    payload["emails"] = [e.model_dump(mode="json") for e in emails]
    payload["activities"] = [a.model_dump(mode="json") for a in activities]
    payload["timeline_limit"] = n
    return payload


def load_company_view(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
) -> CompanyView | None:
    """Load a company with inlined own notes per §V.8.

    Returns ``None`` when the company does not exist. ``notes`` capped at
    ``_INLINE_NOTES_CAP`` rows, ordered by ``created_at`` DESC, full body
    verbatim. ``notes_total`` reflects the actual row count. ``tags`` is the
    assigned tag-name list (empty ok; same shape as ``CompanySummary.tags``
    and ``db export`` company.tags, §V.116).
    """
    company = get_company(connection, company_id)
    if company is None:
        return None
    notes, notes_total = _load_notes_for_owner(connection, "company_id", company_id)
    owner_tags = list_tags(
        connection,
        company_id=company_id,
        limit=1_000_000,
        include_disabled=True,
    )
    return CompanyView(
        **company.model_dump(),
        tags=[t.name for t in owner_tags],
        aliases=list_company_aliases(connection, company_id),
        notes=notes,
        notes_total=notes_total,
    )


def list_company_inspect_contacts(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
    *,
    include_meta: bool = False,
) -> list[dict[str, Any]]:
    """Lean child contacts for ``company view --full`` (§V.168).

    Returns ContactSummary dicts (same fields as ``contact list``). Disabled
    contacts are included so the inspect set matches ``contact_count``.
    ``verification_meta`` is omitted unless ``include_meta`` is True (§V.144).
    """
    summaries = list_contacts(
        connection,
        company_id=company_id,
        include_disabled=True,
        limit=1_000_000,
    )
    payloads = [summary.model_dump(mode="json") for summary in summaries]
    if not include_meta:
        return payloads
    rows = connection.execute(
        """\
        SELECT id, verification_meta
        FROM contact
        WHERE company_id = %(company_id)s
        """,
        {"company_id": company_id},
    ).fetchall()
    meta_by_id = {str(row["id"]): row["verification_meta"] for row in rows}
    for payload in payloads:
        payload["verification_meta"] = meta_by_id.get(str(payload["id"]))
    return payloads


def load_meeting_view(
    connection: psycopg.Connection[dict[str, Any]],
    meeting_id: str,
) -> MeetingView | None:
    """Load a meeting with its attendee contacts inlined per §V.8.

    Returns ``None`` when the meeting does not exist. ``attendees`` carries the
    full attendee `Contact` rows (email + name + every base column) joined via
    ``meeting_attendee`` (§V.125); ``attendee_emails`` + ``attendee_count``
    mirror the ``meeting list`` summary denorm (§V.96). The reader for the
    write+filter relation that previously had none (§B.112).

    The projection is a base-entity superset per §V.8: every ``Meeting`` column
    is forwarded via ``**meeting.model_dump()``.
    """
    meeting = get_meeting(connection, meeting_id)
    if meeting is None:
        return None
    attendees = list_meeting_attendees(connection, meeting_id)
    return MeetingView(
        **meeting.model_dump(),
        attendees=attendees,
        attendee_emails=[contact.email for contact in attendees],
        attendee_count=len(attendees),
    )
