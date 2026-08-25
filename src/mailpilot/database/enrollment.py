"""Enrollment CRUD, preview, packing, and outcomes."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import psycopg
from psycopg.sql import SQL, Identifier
from psycopg.types.json import Json

from mailpilot.database._common import (
    _new_id,
)
from mailpilot.database._sql import (
    _enrollment_full_select,
    _enrollment_lean_select,
    _enrollment_outcome_lateral,
    _enrollment_parent_select,
    _enrollment_where,
    _sql_outbound_sent_count,
    _sql_parse_touch,
)
from mailpilot.database.activity import (
    create_activity,
)
from mailpilot.database.company import (
    list_companies,
)
from mailpilot.database.contact import (
    disable_contact,
    get_contact,
    get_contacts_by_emails,
    list_contacts,
)
from mailpilot.database.note import (
    create_note,
)
from mailpilot.database.task import (
    cancel_enrollment_followup_tasks,
    create_task,
)
from mailpilot.models import (
    Activity,
    Contact,
    ContactSummary,
    Enrollment,
    EnrollmentPreview,
    EnrollmentPreviewContact,
    EnrollmentPreviewExcluded,
    EnrollmentSummary,
    Tag,
    Workflow,
)

# -- Enrollment ----------------------------------------------------------------


def create_enrollment(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
    *,
    commit: bool = True,
) -> Enrollment | None:
    """Enroll a contact in a workflow.

    Uses ON CONFLICT DO NOTHING so callers can safely re-invoke without
    catching unique-constraint errors. Returns None when the row already
    exists (same pattern as ``create_email``). ``id`` is minted client-side
    per §V.12 (UUIDv7).

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        contact_id: Contact FK.
        commit: When False, leave the insert uncommitted for a caller txn.

    Returns:
        Created enrollment, or None if it already existed.
    """
    row = connection.execute(
        SQL(
            "WITH inserted AS ("
            "INSERT INTO enrollment (id, workflow_id, contact_id) "
            "VALUES (%(id)s, %(workflow_id)s, %(contact_id)s) "
            "ON CONFLICT (workflow_id, contact_id) DO NOTHING "
            "RETURNING *"
            ") {}"
        ).format(_enrollment_parent_select(SQL("inserted"))),
        {"id": _new_id(), "workflow_id": workflow_id, "contact_id": contact_id},
    ).fetchone()
    if commit:
        connection.commit()
    if row is None:
        return None
    return Enrollment.model_validate(row)


def get_enrollment(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> Enrollment | None:
    """Get an enrollment by composite ``(workflow_id, contact_id)`` key.

    The ``(workflow_id, contact_id)`` pair remains a UNIQUE constraint
    post-migration to scalar ``id`` so composite-key lookups stay valid for
    the inbound routing and run-loop call sites that already carry the pair.

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        contact_id: Contact FK.

    Returns:
        Enrollment if found, None otherwise.
    """
    row = connection.execute(
        _enrollment_parent_select(SQL("enrollment"))
        + SQL(
            " WHERE enrollment.workflow_id = %(workflow_id)s "
            "AND enrollment.contact_id = %(contact_id)s"
        ),
        {"workflow_id": workflow_id, "contact_id": contact_id},
    ).fetchone()
    if row is None:
        return None
    return Enrollment.model_validate(row)


def get_enrollment_by_id(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
) -> Enrollment | None:
    """Get an enrollment by scalar id (§V.12).

    Args:
        connection: Open database connection.
        enrollment_id: Enrollment ID.

    Returns:
        Enrollment if found, None otherwise.
    """
    row = connection.execute(
        _enrollment_parent_select(SQL("enrollment"))
        + SQL(" WHERE enrollment.id = %(id)s"),
        {"id": enrollment_id},
    ).fetchone()
    if row is None:
        return None
    return Enrollment.model_validate(row)


def list_enrollments(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    status: str | None = None,
) -> list[Enrollment]:
    """List enrollments in a workflow with optional status filter.

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        status: Filter by enrollment status.

    Returns:
        List of enrollments.
    """
    params: dict[str, object] = {"workflow_id": workflow_id}
    status_filter = SQL("")
    if status is not None:
        status_filter = SQL("AND enrollment.status = %(status)s")
        params["status"] = status
    query = _enrollment_parent_select(SQL("enrollment")) + SQL(
        " WHERE enrollment.workflow_id = %(workflow_id)s {} "
        "ORDER BY enrollment.created_at"
    ).format(status_filter)
    rows = connection.execute(query, params).fetchall()
    return [Enrollment.model_validate(row) for row in rows]


def _preview_companies_by_id(
    connection: psycopg.Connection[dict[str, Any]],
    company_ids: set[str],
) -> dict[str, tuple[str | None, int, str | None]]:
    """Map company id -> (disabled_reason, contact_count, domain) (§V.150)."""
    if not company_ids:
        return {}
    rows = connection.execute(
        """\
        SELECT c.id, c.domain, c.disabled_reason,
               COUNT(ct.id)::int AS contact_count
        FROM company c
        LEFT JOIN contact ct ON ct.company_id = c.id
        WHERE c.id = ANY(%(ids)s)
        GROUP BY c.id
        """,
        {"ids": list(company_ids)},
    ).fetchall()
    return {
        str(row["id"]): (
            row["disabled_reason"],
            int(row["contact_count"]),
            row["domain"],
        )
        for row in rows
    }


def _enrolled_contact_ids(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> set[str]:
    """Contact ids already enrolled in a workflow (ids only, §V.185)."""
    rows = connection.execute(
        """\
        SELECT contact_id FROM enrollment
        WHERE workflow_id = %(workflow_id)s
        """,
        {"workflow_id": workflow_id},
    ).fetchall()
    return {str(row["contact_id"]) for row in rows}


def _preview_from_contacts(  # noqa: C901
    connection: psycopg.Connection[dict[str, Any]],
    contacts: Sequence[Contact | ContactSummary],
    *,
    workflow_id: str,
    account_email: str | None = None,
    drop_already_enrolled: bool = True,
    excluded: EnrollmentPreviewExcluded | None = None,
) -> tuple[list[EnrollmentPreviewContact], EnrollmentPreviewExcluded]:
    """Exclude ineligible seats and hydrate tags/peers (§V.185 / §V.150).

    Enrolled set is ``contact_id`` only.
    """
    packed = (
        excluded.model_copy() if excluded is not None else EnrollmentPreviewExcluded()
    )
    enrolled_ids = (
        _enrolled_contact_ids(connection, workflow_id)
        if drop_already_enrolled
        else set()
    )
    account_email_lower = account_email.lower() if account_email is not None else None
    company_ids = {c.company_id for c in contacts if c.company_id is not None}
    company_meta = _preview_companies_by_id(connection, company_ids)
    seen: set[str] = set()
    kept: list[Contact | ContactSummary] = []
    disabled_company_ids: set[str] = set()
    for contact in contacts:
        if contact.id in seen:
            continue
        seen.add(contact.id)
        if contact.company_id is not None:
            meta = company_meta.get(contact.company_id)
            if meta is not None and meta[0] is not None:
                disabled_company_ids.add(contact.company_id)
                continue
        if contact.disabled_reason is not None:
            packed.disabled_contacts += 1
            continue
        if (
            account_email_lower is not None
            and contact.email.lower() == account_email_lower
        ):
            packed.self_loop += 1
            continue
        if drop_already_enrolled and contact.id in enrolled_ids:
            packed.already_enrolled += 1
            continue
        kept.append(contact)
    packed.disabled_companies += len(disabled_company_ids)
    candidate_ids = [contact.id for contact in kept]
    company_owner_ids = [
        contact.company_id for contact in kept if contact.company_id is not None
    ]
    company_tags = _preview_owner_tag_names(connection, company_owner_ids, "company_id")
    contact_tags = _preview_owner_tag_names(connection, candidate_ids, "contact_id")
    peers = _preview_peer_workflows(connection, candidate_ids, workflow_id)
    preview_contacts: list[EnrollmentPreviewContact] = []
    for contact in kept:
        domain = contact.company_domain if isinstance(contact, ContactSummary) else None
        if domain is None and contact.company_id is not None:
            meta = company_meta.get(contact.company_id)
            if meta is not None:
                domain = meta[2]
        preview_contacts.append(
            EnrollmentPreviewContact(
                email=contact.email,
                title=contact.title,
                company_domain=domain,
                company_tags=(
                    company_tags.get(contact.company_id, [])
                    if contact.company_id is not None
                    else []
                ),
                contact_tags=contact_tags.get(contact.id, []),
                email_confidence=contact.email_confidence,
                peer_workflows=peers.get(contact.id, []),
            )
        )
    preview_contacts.sort(key=lambda c: (c.company_domain or "", c.email))
    return preview_contacts, packed


def _preview_owner_tag_names(
    connection: psycopg.Connection[dict[str, Any]],
    owner_ids: list[str],
    owner_column: str,
) -> dict[str, list[str]]:
    """Assigned tag names keyed by owner id (company_id or contact_id)."""
    if not owner_ids:
        return {}
    query = SQL(
        "SELECT ta.{} AS owner_id, array_agg(t.name ORDER BY t.name) AS names "
        "FROM tag_assignment ta JOIN tag t ON t.id = ta.tag_id "
        "WHERE ta.{} = ANY(%(ids)s) "
        "GROUP BY ta.{}"
    ).format(
        Identifier(owner_column),
        Identifier(owner_column),
        Identifier(owner_column),
    )
    rows = connection.execute(query, {"ids": owner_ids}).fetchall()
    return {str(row["owner_id"]): list(row["names"] or []) for row in rows}


def _preview_peer_workflows(
    connection: psycopg.Connection[dict[str, Any]],
    contact_ids: list[str],
    workflow_id: str,
) -> dict[str, list[str]]:
    """Other-workflow names with an active enrollment, keyed by contact id."""
    if not contact_ids:
        return {}
    rows = connection.execute(
        """\
        SELECT e.contact_id,
               array_agg(w.name ORDER BY w.name) AS names
        FROM enrollment e
        JOIN workflow w ON w.id = e.workflow_id
        WHERE e.contact_id = ANY(%(ids)s)
          AND e.status = 'active'
          AND e.workflow_id <> %(workflow_id)s
        GROUP BY e.contact_id
        """,
        {"ids": contact_ids, "workflow_id": workflow_id},
    ).fetchall()
    return {str(row["contact_id"]): list(row["names"] or []) for row in rows}


def preview_enrollment_tag_cohort(
    connection: psycopg.Connection[dict[str, Any]],
    workflow: Workflow,
    tag: Tag,
    *,
    min_contacts: int | None = None,
    account_email: str | None = None,
) -> EnrollmentPreview:
    """Dry-run company-or-contact tag enrollment cohort for one workflow (§V.150).

    Read-only union: company-tag expand (enabled contacts at tagged companies)
    plus contact-tag expand (enabled contacts carrying the tag). Deduped by
    contact id. Disabled companies are excluded from candidates but counted
    (§V.114). Drops already-enrolled contacts for the workflow, self-loop
    contacts (§V.33), and disabled contacts. Optional ``min_contacts`` filters
    companies before expand (company-tag) or the contact's company
    ``contact_count`` (contact-tag). Company expand uses ``company_id = ANY``.

    Args:
        connection: Open database connection.
        workflow: Resolved workflow row (name projected into the report).
        tag: Resolved vocabulary tag row.
        min_contacts: Inclusive lower bound on company contact_count.
        account_email: Workflow account email for self-loop exclusion; when
            None, the self-loop branch never fires.

    Returns:
        ``EnrollmentPreview`` with enriched candidate contacts + exclusion
        counters, sorted by ``company_domain`` then ``email``.
    """
    companies = list_companies(
        connection,
        tag=tag.id,
        min_contacts=min_contacts,
        include_disabled=True,
        limit=100_000,
        sort="domain",
    )
    tagged_contacts = list_contacts(
        connection,
        tag=tag.id,
        include_disabled=True,
        limit=100_000,
    )
    disabled_company_ids = {
        company.id for company in companies if company.disabled_reason is not None
    }
    enabled_ids = [
        company.id for company in companies if company.disabled_reason is None
    ]
    expanded: list[ContactSummary] = []
    if enabled_ids:
        expanded = list_contacts(
            connection,
            company_ids=enabled_ids,
            include_disabled=True,
            limit=None,
        )
    company_ids = {company.id for company in companies}
    extra_ids = {
        contact.company_id
        for contact in tagged_contacts
        if contact.company_id is not None and contact.company_id not in company_ids
    }
    extra_companies = _preview_companies_by_id(connection, extra_ids)
    company_meta: dict[str, tuple[str | None, int, str | None]] = {
        company.id: (company.disabled_reason, company.contact_count, company.domain)
        for company in companies
    }
    company_meta.update(extra_companies)
    raw: list[ContactSummary] = list(expanded)
    seen_ids = {contact.id for contact in expanded}
    for contact in tagged_contacts:
        if contact.id in seen_ids:
            continue
        meta = (
            company_meta.get(contact.company_id)
            if contact.company_id is not None
            else None
        )
        if contact.company_id is not None and meta is not None and meta[0] is not None:
            disabled_company_ids.add(contact.company_id)
            continue
        contact_count = 0 if meta is None else meta[1]
        if min_contacts is not None and contact_count < min_contacts:
            continue
        raw.append(contact)
        seen_ids.add(contact.id)
    contacts, excluded = _preview_from_contacts(
        connection,
        raw,
        workflow_id=workflow.id,
        account_email=account_email,
        drop_already_enrolled=True,
        excluded=EnrollmentPreviewExcluded(
            disabled_companies=len(disabled_company_ids),
        ),
    )
    return EnrollmentPreview(
        workflow=workflow.name,
        tag=tag.name,
        count=len(contacts),
        contacts=contacts,
        excluded=excluded,
    )


def preview_enrollment_file_cohort(
    connection: psycopg.Connection[dict[str, Any]],
    workflow: Workflow,
    emails: Sequence[str],
    *,
    account_email: str | None = None,
    drop_already_enrolled: bool = False,
) -> tuple[EnrollmentPreview, list[str]]:
    """Resolve a ``--file`` email list into a packed-ready preview (§V.171).

    Unknown emails are listed in the second return value and counted under
    ``excluded.not_found``. Found contacts drop disabled company/contact
    (§V.114) and self-loop (§V.33). ``drop_already_enrolled`` is True for
    tag-like preview; file apply keeps already-enrolled seats for last-write-
    wins restamp.

    Args:
        connection: Open database connection.
        workflow: Resolved workflow row.
        emails: File emails (case-insensitive; duplicates collapse).
        account_email: Workflow account email for self-loop exclusion.
        drop_already_enrolled: When True, already-enrolled this workflow
            increment ``already_enrolled`` and are omitted.

    Returns:
        ``(preview, missing_emails)``. Preview ``tag`` is None. Contacts
        are sorted by ``company_domain`` then ``email``.
    """
    unique: list[str] = list(dict.fromkeys(email.lower() for email in emails))
    found = get_contacts_by_emails(connection, unique)
    missing = [email for email in unique if email not in found]
    raw = [found[email] for email in unique if email in found]
    contacts, excluded = _preview_from_contacts(
        connection,
        raw,
        workflow_id=workflow.id,
        account_email=account_email,
        drop_already_enrolled=drop_already_enrolled,
        excluded=EnrollmentPreviewExcluded(not_found=len(missing)),
    )
    preview = EnrollmentPreview(
        workflow=workflow.name,
        tag=None,
        count=len(contacts),
        contacts=contacts,
        excluded=excluded,
    )
    return preview, missing


def apply_enrollment_packing(
    contacts: list[EnrollmentPreviewContact],
    excluded: EnrollmentPreviewExcluded,
    *,
    limit: int | None = None,
    company_atomic: bool = False,
    exclude_peer: bool = False,
) -> tuple[list[EnrollmentPreviewContact], EnrollmentPreviewExcluded]:
    """Apply ``--exclude-peer`` then ``--limit`` / ``--company-atomic`` (§V.171).

    Contacts must already be sorted by ``company_domain`` then ``email``.
    ``--exclude-peer`` drops rows with a non-empty ``peer_workflows``.
    Without ``--company-atomic``, ``--limit N`` is a hard cap (first N).
    With ``--company-atomic``, whole domain atoms are taken in that order;
    the last included atom may exceed N and a domain is never split.
    Contacts with no ``company_domain`` are each their own atom.

    Args:
        contacts: Candidate seats (group-stable sorted).
        excluded: Existing drop counters (copied; packing adds peer /
            over_limit).
        limit: Inclusive seat cap, or None for no cap.
        company_atomic: Soft-cap whole-domain atoms when limit is set.
        exclude_peer: Drop other-workflow active enrollments first.

    Returns:
        Packed contacts and an excluded copy with packing counters added.
    """
    packed_excluded = excluded.model_copy()
    remaining = list(contacts)
    if exclude_peer:
        kept: list[EnrollmentPreviewContact] = []
        for contact in remaining:
            if contact.peer_workflows:
                packed_excluded.peer += 1
            else:
                kept.append(contact)
        remaining = kept
    if limit is None:
        return remaining, packed_excluded
    if not company_atomic:
        packed_excluded.over_limit += max(0, len(remaining) - limit)
        return remaining[:limit], packed_excluded
    included: list[EnrollmentPreviewContact] = []
    taking = True
    for group in _company_atomic_groups(remaining):
        if not taking or (included and len(included) >= limit):
            taking = False
            packed_excluded.over_limit += len(group)
            continue
        included.extend(group)
        if len(included) >= limit:
            taking = False
    return included, packed_excluded


def _company_atomic_groups(
    contacts: list[EnrollmentPreviewContact],
) -> list[list[EnrollmentPreviewContact]]:
    """Group seats by domain; no-domain contacts are singleton atoms."""
    groups: list[list[EnrollmentPreviewContact]] = []
    current_key: str | None = None
    current: list[EnrollmentPreviewContact] = []
    for contact in contacts:
        key = contact.company_domain if contact.company_domain else f"\0{contact.email}"
        if current_key is None:
            current_key = key
            current = [contact]
            continue
        if key == current_key:
            current.append(contact)
            continue
        groups.append(current)
        current_key = key
        current = [contact]
    if current:
        groups.append(current)
    return groups


def list_never_sent_t1_schedules_by_domain(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> dict[str, list[datetime]]:
    """Map company_domain to pending never-sent T1 instants (§V.171 / §V.152).

    Same match as ``enrollment list --touch 1`` for never-sent first-reach:
    active enrollment, ``emails_sent=0``, pending task with parsed touch 1
    or absent ``context.touch``. Contacts with no company are omitted.

    Args:
        connection: Open database connection.
        workflow_id: Workflow whose live T1 siblings to load.

    Returns:
        Domain -> scheduled_at instants, ordered by time per domain.
    """
    sent_count = _sql_outbound_sent_count(SQL("e"))
    parsed_touch = _sql_parse_touch(SQL("t.context"))
    query = SQL(
        "SELECT co.domain AS company_domain, t.scheduled_at "
        "FROM enrollment e "
        "JOIN contact c ON c.id = e.contact_id "
        "JOIN company co ON co.id = c.company_id "
        "JOIN task t ON t.enrollment_id = e.id "
        "WHERE e.workflow_id = %(workflow_id)s "
        "AND e.status = 'active' "
        "AND t.status = 'pending' "
        "AND {sent_count} = 0 "
        "AND ({parsed_touch} = 1 OR t.context->>'touch' IS NULL) "
        "ORDER BY co.domain, t.scheduled_at"
    ).format(sent_count=sent_count, parsed_touch=parsed_touch)
    rows = connection.execute(query, {"workflow_id": workflow_id}).fetchall()
    schedules: dict[str, list[datetime]] = {}
    for row in rows:
        domain = str(row["company_domain"])
        instant = row["scheduled_at"]
        if not isinstance(instant, datetime):
            continue
        schedules.setdefault(domain, []).append(instant)
    return schedules


def get_latest_enrollment_outcome(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
) -> str | None:
    """Return the enrollment's most recent terminal outcome, else None (§V.83).

    Outcomes are timeline-only (§V.15): the newest ``enrollment_completed`` /
    ``enrollment_failed`` activity for the enrollment is its current outcome.
    Returns ``"completed"`` or ``"failed"`` when one exists, ``None`` when the
    enrollment has no recorded outcome yet.

    The touch pre-flight (§V.83) reads this to cancel a queued follow-up touch
    once the sequence has concluded -- a booked meeting, opt-out, or
    contact-later disposition -- without an LLM call.

    Args:
        connection: Open database connection.
        enrollment_id: Enrollment FK (outcome activities carry it).

    Returns:
        ``"completed"``, ``"failed"``, or ``None``.
    """
    row = connection.execute(
        """\
        SELECT CASE type
            WHEN 'enrollment_completed' THEN 'completed'
            WHEN 'enrollment_failed' THEN 'failed'
        END AS outcome
        FROM activity
        WHERE enrollment_id = %(enrollment_id)s
          AND type IN ('enrollment_completed', 'enrollment_failed')
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        {"enrollment_id": enrollment_id},
    ).fetchone()
    if row is None:
        return None
    outcome = row["outcome"]
    return outcome if isinstance(outcome, str) else None


def list_active_outbound_enrollments_for_contact(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
) -> list[Enrollment]:
    """List a contact's active enrollments in outbound workflows (§V.128).

    The booking-conclusion fan-out feeds on this: a meeting booked by an
    attendee outranks every cold outbound sequence, so the system concludes
    each active outbound enrollment the contact holds (§V.128). Inbound
    enrollments and disabled enrollments are excluded -- a booking concludes
    only live cold sequences.

    Args:
        connection: Open database connection.
        contact_id: Contact FK.

    Returns:
        Active enrollments in outbound workflows for the contact, ordered by
        ``created_at`` (denormalised parent identifiers joined per §V.5).
    """
    rows = connection.execute(
        _enrollment_parent_select(SQL("enrollment"))
        + SQL(
            " WHERE enrollment.contact_id = %(contact_id)s "
            "AND enrollment.status = 'active' "
            "AND workflow.type = 'outbound' "
            "ORDER BY enrollment.created_at"
        ),
        {"contact_id": contact_id},
    ).fetchall()
    return [Enrollment.model_validate(row) for row in rows]


def record_enrollment_outcome(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
    outcome: str,
    reason: str,
    disposition: str | None = None,
) -> Activity:
    """Record a completed/failed outcome on the enrollment timeline (§V.15).

    System-internal recorder: the outcome is purely an activity-timeline event
    (``enrollment_completed`` / ``enrollment_failed``); the ``enrollment`` row
    status is never modified (§V.15). The same transaction bumps
    ``enrollment.updated_at`` so ``enrollment list --since``/``--until``
    (filters ``e.updated_at``) plus ``--full`` plus ``--disposition`` can
    window a terminal outcome without ``contact view --timeline``. There is
    no ``disposition_updated_at`` column; ``updated_at`` is the window clock.

    When supplied, ``disposition`` is persisted into the activity ``detail``
    JSONB under the ``disposition`` key (§V.132) so the per-campaign funnel can
    split ``failed`` outcomes into ``do_not_contact`` versus ``contact_later``
    and confirm ``completed`` maps to ``meeting_booked``. The key is omitted
    when ``disposition`` is None, so pre-change rows carry no key (legacy gap).

    Args:
        connection: Open database connection.
        enrollment_id: Enrollment ID (scalar).
        outcome: ``"completed"`` or ``"failed"``.
        reason: Explanation inlined into the activity (e.g. ``"meeting booked"``).
        disposition: Terminal disposition (§V.127) in {meeting_booked,
            do_not_contact, contact_later}, or None to write no disposition key.

    Returns:
        The created outcome ``Activity``.

    Raises:
        ValueError: If ``outcome`` is not completed/failed, or the enrollment
            does not exist.
    """
    if outcome not in ("completed", "failed"):
        raise ValueError(f"outcome must be completed or failed, got: {outcome}")
    enrollment = get_enrollment_by_id(connection, enrollment_id)
    if enrollment is None:
        raise ValueError(f"enrollment not found: {enrollment_id}")
    contact = get_contact(connection, enrollment.contact_id)
    detail: dict[str, object] = {"reason": reason}
    if disposition is not None:
        detail["disposition"] = disposition
    connection.execute(
        """\
        UPDATE enrollment
        SET updated_at = CURRENT_TIMESTAMP
        WHERE id = %(id)s
        """,
        {"id": enrollment_id},
    )
    activity = create_activity(
        connection,
        contact_id=enrollment.contact_id,
        activity_type=f"enrollment_{outcome}",
        summary=reason or f"Enrollment {outcome}",
        detail=detail,
        company_id=contact.company_id if contact is not None else None,
        workflow_id=enrollment.workflow_id,
        enrollment_id=enrollment.id,
        commit=False,
    )
    connection.commit()
    return activity


_CONCLUDE_DISPOSITIONS = frozenset(
    {"meeting_booked", "do_not_contact", "contact_later"}
)
_CONCLUDE_OUTCOME: dict[str, str] = {
    "meeting_booked": "completed",
    "do_not_contact": "failed",
    "contact_later": "failed",
}


def conclude_enrollment(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
    disposition: str,
    reason: str,
    reschedule_at: str | None = None,
    note: str | None = None,
    skip_if_terminal: bool = False,
) -> dict[str, Any] | None:
    """Conclude an enrollment with one terminal disposition (§V.186).

    Shared by the agent tool, bounce, booking, and cadence. Always records
    the timeline outcome and cancels pending follow-ups (first-touch
    excluded, §V.123). Optional side effects: ``do_not_contact`` disables
    the contact when not already blocked (bounce disable stays §V.80);
    ``note`` writes a contact note; ``meeting_booked`` always writes a note;
    ``reschedule_at`` on ``contact_later`` schedules a re-enrollment
    first-touch. Omitted ``reschedule_at`` means no task (cadence
    exhaustion). ``reschedule_at`` on any other disposition raises.
    ``skip_if_terminal`` true skips when a latest outcome already exists
    (bounce); false still concludes (booking default).

    Args:
        connection: Open database connection.
        enrollment_id: Enrollment to conclude.
        disposition: One of meeting_booked, do_not_contact, contact_later.
        reason: Timeline outcome reason (system or agent note).
        reschedule_at: When set with ``contact_later``, schedule a
            re-enrollment first-touch.
        note: When set, write a contact note. ``meeting_booked`` writes
            ``reason`` when ``note`` is omitted.
        skip_if_terminal: When true, no-op if a latest outcome exists.

    Returns:
        ``{disposition, outcome}`` plus ``reschedule_at`` when a
        re-enrollment was scheduled, or ``None`` when skipped.

    Raises:
        ValueError: Unknown disposition, enrollment not found, or
            ``reschedule_at`` set on a non-``contact_later`` disposition.
    """
    if disposition not in _CONCLUDE_DISPOSITIONS:
        raise ValueError(
            f"disposition must be one of {tuple(sorted(_CONCLUDE_DISPOSITIONS))}, "
            f"got: {disposition}"
        )
    if reschedule_at is not None and disposition != "contact_later":
        raise ValueError(f"reschedule_at requires contact_later, got: {disposition}")
    if (
        skip_if_terminal
        and get_latest_enrollment_outcome(connection, enrollment_id) is not None
    ):
        return None

    enrollment = get_enrollment_by_id(connection, enrollment_id)
    if enrollment is None:
        raise ValueError(f"enrollment not found: {enrollment_id}")

    outcome = _CONCLUDE_OUTCOME[disposition]
    record_enrollment_outcome(
        connection,
        enrollment_id,
        outcome=outcome,
        reason=reason,
        disposition=disposition,
    )
    cancel_enrollment_followup_tasks(connection, enrollment_id)

    if disposition == "do_not_contact":
        contact = get_contact(connection, enrollment.contact_id)
        if contact is not None and contact.disabled_reason is None:
            disable_contact(
                connection,
                enrollment.contact_id,
                reason=f"do_not_contact: {reason}",
            )

    note_body = note
    if note_body is None and disposition == "meeting_booked":
        note_body = reason
    if note_body:
        create_note(connection, body=note_body, contact_id=enrollment.contact_id)

    result: dict[str, Any] = {"disposition": disposition, "outcome": outcome}
    if disposition == "contact_later" and reschedule_at is not None:
        create_task(
            connection,
            enrollment_id=enrollment.id,
            workflow_id=enrollment.workflow_id,
            contact_id=enrollment.contact_id,
            description="scheduled re-enrollment first reach-out",
            scheduled_at=reschedule_at,
            context={"trigger": "enrollment_schedule", "touch": 1},
            email_id=None,
        )
        result["reschedule_at"] = reschedule_at
    return result


def disable_enrollment(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
    reason: str,
) -> Enrollment | None:
    """Soft-disable an enrollment via terminal lifecycle exit (§V.10, §V.15).

    Single transaction: flips ``status='disabled'`` + writes ``disabled_reason``,
    then appends an ``enrollment_disabled`` activity carrying the reason. The
    coupling CHECK on ``enrollment`` rejects empty reasons at the schema level;
    callers MUST validate ``reason.strip() != ""`` upstream for a friendlier
    error envelope.

    Returns the updated row with denormalised parent identifiers (workflow
    name, contact email/name) so the CLI envelope can ship the full
    Enrollment model unchanged. Returns ``None`` when no row matches the id.

    Args:
        connection: Open database connection.
        enrollment_id: Enrollment ID.
        reason: Operator-supplied explanation written to ``disabled_reason``
            and inlined into the ``enrollment_disabled`` activity row.

    Returns:
        Updated ``Enrollment`` (status='disabled'), or ``None`` if not found.
    """
    row = connection.execute(
        SQL(
            "WITH updated AS ("
            "UPDATE enrollment "
            "SET status = 'disabled', "
            "disabled_reason = %(reason)s, "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE id = %(id)s "
            "RETURNING *"
            ") {}"
        ).format(
            _enrollment_parent_select(
                SQL("updated"),
                extra=SQL("contact.company_id AS contact_company_id"),
            )
        ),
        {"id": enrollment_id, "reason": reason},
    ).fetchone()
    if row is None:
        connection.commit()
        return None
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, workflow_id, enrollment_id,
            type, summary, detail
        )
        VALUES (
            %(id)s, %(contact_id)s, %(company_id)s, %(workflow_id)s,
            %(enrollment_id)s, 'enrollment_disabled', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "contact_id": row["contact_id"],
            "company_id": row["contact_company_id"],
            "workflow_id": row["workflow_id"],
            "enrollment_id": row["id"],
            "summary": reason,
            "detail": Json({"reason": reason}),
        },
    )
    connection.commit()
    row.pop("contact_company_id", None)
    return Enrollment.model_validate(row)


def enable_enrollment(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
) -> Enrollment | None:
    """Re-enable a disabled enrollment: flip ``status`` to ``active``.

    Mirror of ``disable_enrollment`` (§V.15): single transaction flips
    ``status='active'`` + clears ``disabled_reason``, then appends an
    ``enrollment_enabled`` activity. A ``status='disabled'`` gate blocks
    enabling a live enrollment -- an already-active row does not match, so the
    call returns ``None`` and writes no activity.

    Returns the updated row with denormalised parent identifiers (workflow
    name, contact email/name) so the CLI envelope can ship the full Enrollment
    model unchanged.

    Args:
        connection: Open database connection.
        enrollment_id: Enrollment ID.

    Returns:
        Updated ``Enrollment`` (status='active'), or ``None`` when no disabled
        enrollment with that id exists -- i.e. missing or already active.
    """
    row = connection.execute(
        SQL(
            "WITH updated AS ("
            "UPDATE enrollment "
            "SET status = 'active', "
            "disabled_reason = NULL, "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE id = %(id)s "
            "AND status = 'disabled' "
            "RETURNING *"
            ") {}"
        ).format(
            _enrollment_parent_select(
                SQL("updated"),
                extra=SQL("contact.company_id AS contact_company_id"),
            )
        ),
        {"id": enrollment_id},
    ).fetchone()
    if row is None:
        connection.commit()
        return None
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, workflow_id, enrollment_id,
            type, summary, detail
        )
        VALUES (
            %(id)s, %(contact_id)s, %(company_id)s, %(workflow_id)s,
            %(enrollment_id)s, 'enrollment_enabled', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "contact_id": row["contact_id"],
            "company_id": row["contact_company_id"],
            "workflow_id": row["workflow_id"],
            "enrollment_id": row["id"],
            "summary": "Enrollment re-enabled",
            "detail": Json({}),
        },
    )
    connection.commit()
    row.pop("contact_company_id", None)
    return Enrollment.model_validate(row)


def list_enrollments_detailed(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str | None = None,
    contact_id: str | None = None,
    status: str | None = None,
    limit: int | None = 100,
    since: str | None = None,
    until: str | None = None,
    *,
    full: bool = False,
    has_pending_task: bool | None = None,
    touch: int | None = None,
    sort: str = "updated_at",
    desc: bool = False,
    stuck: bool = False,
    first_send_sla_hours: int = 24,
    disposition: str | None = None,
) -> list[EnrollmentSummary]:
    """List enrollments with denormalised contact info as summaries.

    Splits ``_enrollment_where`` + lean SELECT + full SELECT (§V.185).
    Separate from ``list_enrollments`` which returns ``list[Enrollment]``.
    Both ``workflow_id`` and ``contact_id`` are optional independent
    filters; either or both can be supplied.

    When ``full=True`` (§V.152), also projects company, touch progress,
    next pending task, disposition, ``created_at``, and the latest
    completed/failed outcome for the agent envelope (§V.185).

    Args:
        connection: Open database connection.
        workflow_id: Optional workflow FK filter.
        contact_id: Optional contact FK filter.
        status: Filter by enrollment status.
        limit: Maximum results. ``None`` omits LIMIT (review path §V.174).
        since: ISO datetime inclusive lower bound on ``e.updated_at``.
        until: ISO datetime inclusive upper bound on ``e.updated_at``.
        full: When True, denser execution projection (§V.152).
        has_pending_task: When True/False, filter by presence of pending task.
        touch: Filter to enrollments whose next pending touch equals N, or
            (when no pending) whose last sent touch equals N. ``touch=1``
            also matches never-sent rows with a pending first-touch
            (``emails_sent=0`` and ``next_scheduled_at`` set) even when
            ``context.touch`` is absent (§V.152).
        sort: ``updated_at`` (default) or ``next_scheduled_at`` (full path).
        desc: Sort descending when True.
        stuck: When True (§V.155), only stuck enrollments (heuristics below).
        first_send_sla_hours: SLA for never-sent active enrollments (default 24).
        disposition: When set (§V.160), filter by latest terminal disposition
            in {meeting_booked, do_not_contact, contact_later}.

    Returns:
        List of enrollment summaries.
    """
    params: dict[str, object] = {
        "first_send_sla_hours": first_send_sla_hours,
    }
    sent_count = _sql_outbound_sent_count(SQL("e"))
    if limit is not None:
        params["limit"] = limit
    if stuck:
        # Force full joins for stuck heuristics that need next task / bounce.
        full = True
    where_clause = _enrollment_where(
        params,
        workflow_id=workflow_id,
        contact_id=contact_id,
        status=status,
        since=since,
        until=until,
        disposition=disposition,
        stuck=stuck,
        has_pending_task=has_pending_task,
        touch=touch,
        sent_count=sent_count,
    )
    if full:
        select_from = _enrollment_full_select(sent_count)
    else:
        select_from = _enrollment_lean_select()
        # §V.160 disposition filter needs outcome lateral even on lean rows.
        if disposition is not None:
            select_from = select_from + _enrollment_outcome_lateral()
    if full and sort == "next_scheduled_at":
        order_col = SQL("nt.scheduled_at")
    elif sort == "created_at":
        order_col = SQL("e.created_at")
    else:
        order_col = SQL("e.updated_at")
    order_dir = SQL("DESC") if desc else SQL("ASC")
    limit_sql = SQL(" LIMIT %(limit)s") if limit is not None else SQL("")
    query = (
        select_from
        + where_clause
        + SQL(" ORDER BY ")
        + order_col
        + SQL(" ")
        + order_dir
        + SQL(" NULLS LAST")
        + limit_sql
    )
    rows = connection.execute(query, params).fetchall()
    return [EnrollmentSummary.model_validate(row) for row in rows]
