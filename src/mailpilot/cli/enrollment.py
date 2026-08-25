"""Enrollment commands."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

import click

from mailpilot._filters import (
    desc_option,
    enum_option,
    limit_option,
    presence_option,
    scope_option,
    sort_option,
    time_window_options,
)
from mailpilot.cli._helpers import _parse_future_scheduled_at
from mailpilot.cli.main import (
    _db,
    _emit_formatted,
    _resolve_contact,
    _resolve_tag,
    _resolve_workflow,
    _resolve_workflow_id,
    main,
    output,
    output_entity,
    output_error,
)

if TYPE_CHECKING:
    from mailpilot.models import EnrollmentBatchAction, EnrollmentBatchRow

_ENROLLMENT_STATUSES = ["active", "disabled"]

_ENROLLMENT_DISPOSITIONS = ["meeting_booked", "do_not_contact", "contact_later"]

# -- Enrollment commands -------------------------------------------------------


@main.group()
def enrollment() -> None:
    """Manage contact enrollments in workflows."""


def _reject_enrollment_self_loop(
    account: Any,
    contact: Any,
    workflow_name: str,
) -> None:
    """Reject enrollment when contact.email matches workflow's account email.

    Per SPEC §V.33 -- semantic self-loop (agent notionally emails itself).
    Compare case-insensitively (Gmail addresses are case-insensitive). When
    ``account`` is ``None`` (defensive: FK-orphaned workflow), no rejection.
    """
    if account is not None and account.email.lower() == contact.email.lower():
        output_error(
            f"cannot enroll contact {contact.email} in workflow "
            f"{workflow_name}: contact email matches workflow's account email",
            "self_loop",
        )


def _same_scheduled_instant(existing: Any, scheduled_iso: str) -> bool:
    """True when ``existing`` and the parsed ISO string are the same instant."""
    from datetime import UTC, datetime

    wanted = datetime.fromisoformat(scheduled_iso)
    if wanted.tzinfo is None:
        wanted = wanted.replace(tzinfo=UTC)
    have = existing
    if have.tzinfo is None:
        have = have.replace(tzinfo=UTC)
    return have == wanted


def _is_first_reach_task(task: Any) -> bool:
    """True when the pending row is a first-reach (T1), not T2+ (§V.32)."""
    from mailpilot.cadence import resolve_touch_number

    context = task.context or {}
    trigger = str(context.get("trigger") or "")
    touch = resolve_touch_number(context, trigger)
    if touch is not None and touch >= 2:
        return False
    return trigger in ("enrollment_schedule", "enrollment_run")


def _maybe_schedule_first_touch(
    connection: Any,
    enrollment_id: str,
    workflow_id: str,
    contact_id: str,
    scheduled_iso: str | None,
    changed: list[str],
    *,
    enrollment_status: str,
    emails_sent: int,
    commit: bool = True,
) -> None:
    """Insert or last-write-wins a pending first-touch task per §V.32.

    New enrollment (no pending first-reach): insert once. Re-run on an
    active ``emails_sent=0`` enrollment with a pending first-reach: UPDATE
    ``scheduled_at`` in place when the parsed instant differs, persist
    ``touch`` 1 if absent, and append ``scheduled_first_send`` to
    ``changed``. Same instant, later touch, already-sent, or non-active
    enrollment: no-op. Never inserts a second first-reach.
    """
    if scheduled_iso is None:
        return
    from mailpilot.database import (
        create_task,
        find_pending_first_touch_task,
        update_pending_first_touch_schedule,
    )

    existing = find_pending_first_touch_task(connection, enrollment_id)
    if existing is not None:
        if not _is_first_reach_task(existing):
            return
        if emails_sent > 0 or enrollment_status != "active":
            return
        if _same_scheduled_instant(existing.scheduled_at, scheduled_iso):
            return
        update_pending_first_touch_schedule(
            connection,
            task=existing,
            scheduled_at=scheduled_iso,
            commit=commit,
        )
        changed.append("scheduled_first_send")
        return
    if emails_sent > 0:
        return
    create_task(
        connection,
        enrollment_id=enrollment_id,
        workflow_id=workflow_id,
        contact_id=contact_id,
        description="scheduled first reach-out",
        scheduled_at=scheduled_iso,
        context={"trigger": "enrollment_schedule", "touch": 1},
        email_id=None,
        commit=commit,
    )
    changed.append("scheduled_first_send")


def _reject_enrollment_add_source_xor(
    contact_email: str | None,
    tag_ref: str | None,
    file_path: str | None,
    dry_run: bool,
    scheduled_at: str | None,
) -> None:
    """Reject exclusive / required source flag combinations."""
    if tag_ref is not None and contact_email is not None:
        output_error(
            "--tag is exclusive with --contact-email",
            "validation_error",
        )
    if file_path is not None and tag_ref is not None:
        output_error("--file is exclusive with --tag", "validation_error")
    if file_path is not None and contact_email is not None:
        output_error(
            "--file is exclusive with --contact-email",
            "validation_error",
        )
    if dry_run and tag_ref is None and file_path is None:
        output_error(
            "--dry-run requires --tag or --file",
            "validation_error",
        )
    if tag_ref is not None and not dry_run and scheduled_at is None:
        output_error(
            "--tag apply requires --scheduled-at (or pass --dry-run)",
            "validation_error",
        )
    if file_path is not None and not dry_run and scheduled_at is None:
        output_error(
            "--file apply requires --scheduled-at (or pass --dry-run)",
            "validation_error",
        )
    if tag_ref is None and file_path is None and contact_email is None:
        output_error(
            "--contact-email is required (or --tag / --file for a batch)",
            "validation_error",
        )


def _reject_enrollment_add_pack_flags(
    tag_ref: str | None,
    file_path: str | None,
    min_contacts: int | None,
    limit: int | None,
    company_atomic: bool,
) -> None:
    """Reject packing / filter flags used on the wrong source.

    ``--exclude-peer`` is valid on ``--contact-email`` as well as
    ``--file`` / ``--tag``; ``--limit`` / ``--company-atomic`` stay
    batch-source only.
    """
    if min_contacts is not None and tag_ref is None:
        output_error(
            "--min-contacts is only valid with --tag",
            "validation_error",
        )
    if min_contacts is not None and min_contacts < 0:
        output_error("--min-contacts must be >= 0", "validation_error")
    if limit is not None and limit < 1:
        output_error("--limit must be >= 1", "validation_error")
    batch_source = tag_ref is not None or file_path is not None
    if (limit is not None or company_atomic) and not batch_source:
        output_error(
            "--limit / --company-atomic require --file or --tag",
            "validation_error",
        )


def _validate_enrollment_add_args(
    contact_email: str | None,
    tag_ref: str | None,
    file_path: str | None,
    dry_run: bool,
    min_contacts: int | None,
    scheduled_at: str | None,
    limit: int | None,
    company_atomic: bool,
) -> str | None:
    """Validate flag combinations for ``enrollment add``; return scheduled ISO."""
    from datetime import datetime

    _reject_enrollment_add_source_xor(
        contact_email, tag_ref, file_path, dry_run, scheduled_at
    )
    _reject_enrollment_add_pack_flags(
        tag_ref, file_path, min_contacts, limit, company_atomic
    )
    if dry_run and scheduled_at is not None:
        output_error(
            "--scheduled-at is exclusive with --dry-run",
            "validation_error",
        )
    if scheduled_at is None:
        return None
    if (tag_ref is not None or file_path is not None) and not dry_run:
        return _parse_future_scheduled_at(scheduled_at)
    try:
        return datetime.fromisoformat(scheduled_at).isoformat()
    except ValueError as exc:
        output_error(f"invalid --scheduled-at value: {exc}", "validation_error")


def _calendar_day(iso: str) -> Any:
    """Local calendar date of an ISO instant (naive treated as UTC)."""
    from datetime import UTC, datetime

    parsed = datetime.fromisoformat(iso)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.date()


def _read_enrollment_batch_file(path: str) -> list[tuple[str, str | None]]:
    """Parse ``--file`` JSON into ``(email, scheduled_at_override)`` rows."""
    import pathlib

    file_path = pathlib.Path(path)
    if not file_path.is_file():
        output_error(f"file not found: {path}", "not_found")
    try:
        raw: object = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        output_error(f"invalid JSON: {exc}", "validation_error")
    if not isinstance(raw, list):
        output_error("enrollment file must be a JSON array", "validation_error")
    rows: dict[str, str | None] = {}
    for index, item in enumerate(raw):
        if isinstance(item, str):
            email = item.strip()
            if not email:
                output_error(f"missing email at index {index}", "validation_error")
            rows[email.lower()] = None
            continue
        if isinstance(item, dict):
            email_val = item.get("email")
            if not isinstance(email_val, str) or not email_val.strip():
                output_error(f"missing email at index {index}", "validation_error")
            override = item.get("scheduled_at")
            if override is not None and not isinstance(override, str):
                output_error(
                    f"scheduled_at must be a string at index {index}",
                    "validation_error",
                )
            rows[email_val.strip().lower()] = override
            continue
        output_error(f"invalid entry at index {index}", "validation_error")
    return list(rows.items())


def _pack_enrollment_preview(
    preview: Any,
    *,
    limit: int | None,
    company_atomic: bool,
    exclude_peer: bool,
) -> Any:
    """Apply §V.171 packing flags onto a dry-run preview (no writes)."""
    from mailpilot.database import apply_enrollment_packing
    from mailpilot.models import EnrollmentPreview

    contacts, excluded = apply_enrollment_packing(
        preview.contacts,
        preview.excluded,
        limit=limit,
        company_atomic=company_atomic,
        exclude_peer=exclude_peer,
    )
    return EnrollmentPreview(
        workflow=preview.workflow,
        tag=preview.tag,
        count=len(contacts),
        contacts=contacts,
        excluded=excluded,
    )


def _emit_enrollment_preview(preview: Any) -> None:
    """Write the ``enrollment_preview`` envelope."""
    output(
        {"enrollment_preview": preview.model_dump(mode="json")},
        record_count=preview.count,
    )


def _enrollment_add_tag_preview(
    connection: Any,
    workflow: Any,
    tag_ref: str,
    min_contacts: int | None,
    *,
    limit: int | None = None,
    company_atomic: bool = False,
    exclude_peer: bool = False,
) -> None:
    """Dry-run company-or-contact tag cohort enrollment preview (no writes)."""
    from mailpilot.database import get_account, preview_enrollment_tag_cohort

    tag = _resolve_tag(connection, tag_ref)
    account = get_account(connection, workflow.account_id)
    account_email = account.email if account is not None else None
    preview = preview_enrollment_tag_cohort(
        connection,
        workflow,
        tag,
        min_contacts=min_contacts,
        account_email=account_email,
    )
    packed = _pack_enrollment_preview(
        preview,
        limit=limit,
        company_atomic=company_atomic,
        exclude_peer=exclude_peer,
    )
    _emit_enrollment_preview(packed)


def _enrollment_add_file_preview(
    connection: Any,
    workflow: Any,
    file_rows: list[tuple[str, str | None]],
    *,
    limit: int | None = None,
    company_atomic: bool = False,
    exclude_peer: bool = False,
) -> None:
    """Dry-run ``--file`` cohort preview (no writes)."""
    from mailpilot.database import get_account, preview_enrollment_file_cohort

    rows = file_rows
    account = get_account(connection, workflow.account_id)
    account_email = account.email if account is not None else None
    preview, _missing = preview_enrollment_file_cohort(
        connection,
        workflow,
        [email for email, _override in rows],
        account_email=account_email,
        drop_already_enrolled=False,
    )
    packed = _pack_enrollment_preview(
        preview,
        limit=limit,
        company_atomic=company_atomic,
        exclude_peer=exclude_peer,
    )
    _emit_enrollment_preview(packed)


def _apply_one_enrollment(
    connection: Any,
    workflow: Any,
    contact: Any,
    scheduled_iso: str | None,
    *,
    commit: bool = True,
) -> tuple[Any, list[str]] | None:
    """Create or reuse an enrollment and optionally schedule first touch.

    Returns ``(enrollment, changed)`` or None when the existing row cannot
    be loaded after an insert race.
    """
    from mailpilot.database import (
        count_outbound_sent,
        create_activity,
        create_enrollment,
        get_enrollment,
    )

    created = create_enrollment(connection, workflow.id, contact.id, commit=commit)
    if created is not None:
        create_activity(
            connection,
            contact_id=contact.id,
            activity_type="enrollment_added",
            summary=f"Assigned to {workflow.name}",
            detail={"workflow_name": workflow.name},
            company_id=contact.company_id,
            workflow_id=workflow.id,
            enrollment_id=created.id,
            commit=commit,
        )
        target = created
        changed = ["status"]
        emails_sent = 0
    else:
        existing = get_enrollment(connection, workflow.id, contact.id)
        if existing is None:
            return None
        target = existing
        changed = []
        emails_sent = (
            count_outbound_sent(connection, workflow.id, contact.id)
            if scheduled_iso is not None
            else 0
        )
    _maybe_schedule_first_touch(
        connection,
        target.id,
        workflow.id,
        contact.id,
        scheduled_iso,
        changed,
        enrollment_status=target.status,
        emails_sent=emails_sent,
        commit=commit,
    )
    return target, changed


def _batch_action(created: bool, changed: list[str]) -> EnrollmentBatchAction:
    """Map single-seat changed tokens to a batch ``action`` (§V.171)."""
    if created:
        return "created"
    if "scheduled_first_send" in changed:
        return "scheduled_first_send"
    return "unchanged"


def _assert_company_atomic_days(
    seats: list[tuple[Any, str]],
) -> None:
    """Reject mixed calendar days on one domain when ``--company-atomic``."""
    days: dict[str, Any] = {}
    for contact, iso in seats:
        domain = contact.company_domain
        if not domain:
            continue
        day = _calendar_day(iso)
        previous = days.get(domain)
        if previous is not None and previous != day:
            output_error(
                f"--company-atomic: {domain} has seats on more than one calendar day",
                "validation_error",
            )
        days[domain] = day


def _contact_company_domain(connection: Any, contact: Any) -> str | None:
    """Resolve a contact's company domain for a batch row, else None."""
    if contact.company_id is None:
        return None
    from mailpilot.database import get_company

    company = get_company(connection, contact.company_id)
    return None if company is None else company.domain


def _emit_contact_enrollment_batch(
    *,
    workflow_name: str,
    scheduled_iso: str | None,
    enrolled: list[EnrollmentBatchRow],
    peer_excluded: int,
) -> None:
    """Write the one-off ``--exclude-peer`` ``enrollment_batch`` envelope."""
    from mailpilot.models import EnrollmentBatch, EnrollmentPreviewExcluded

    batch = EnrollmentBatch(
        workflow=workflow_name,
        scheduled_at=scheduled_iso,
        source="contact",
        tag=None,
        limit=None,
        company_atomic=False,
        count=len(enrolled),
        enrolled=enrolled,
        excluded=EnrollmentPreviewExcluded(peer=peer_excluded),
    )
    output(
        {"enrollment_batch": batch.model_dump(mode="json")},
        record_count=batch.count,
    )


def _enrollment_add_contact(
    connection: Any,
    workflow: Any,
    contact_email: str,
    scheduled_iso: str | None,
    *,
    exclude_peer: bool = False,
) -> None:
    """Enroll a single contact, optionally scheduling first touch.

    ``--exclude-peer`` skips when the contact is active on another workflow
    and returns ``enrollment_batch`` with ``source=contact`` instead of the
    singular entity envelope.
    """
    from mailpilot.database import _preview_peer_workflows, get_account
    from mailpilot.models import EnrollmentBatchRow
    from mailpilot.operator_log import cli_mutation, operator_event

    if scheduled_iso is not None and workflow.type != "outbound":
        output_error(
            "--scheduled-at only valid for outbound workflows",
            "invalid_state",
        )
    contact = _resolve_contact(connection, contact_email)
    account = get_account(connection, workflow.account_id)
    _reject_enrollment_self_loop(account, contact, workflow.name)
    if exclude_peer:
        peers = _preview_peer_workflows(connection, [contact.id], workflow.id)
        if peers.get(contact.id):
            _emit_contact_enrollment_batch(
                workflow_name=workflow.name,
                scheduled_iso=scheduled_iso,
                enrolled=[],
                peer_excluded=1,
            )
            return
    mutation_attrs: dict[str, Any] = {
        "workflow_id": workflow.id,
        "contact_id": contact.id,
    }
    if scheduled_iso is not None:
        mutation_attrs["scheduled_at"] = scheduled_iso
    with cli_mutation("enrollment", "add", **mutation_attrs):
        applied = _apply_one_enrollment(connection, workflow, contact, scheduled_iso)
        if applied is None:
            return
        target, changed = applied
        event_fields: dict[str, Any] = {
            "enrollment_id": target.id,
            "workflow_id": workflow.id,
            "contact_id": contact.id,
        }
        if scheduled_iso is not None:
            event_fields["scheduled_at"] = scheduled_iso
        event_fields["changed"] = changed
        operator_event("enrollment.add", **event_fields)
        if exclude_peer:
            _emit_contact_enrollment_batch(
                workflow_name=workflow.name,
                scheduled_iso=scheduled_iso,
                enrolled=[
                    EnrollmentBatchRow(
                        email=contact.email,
                        company_domain=_contact_company_domain(connection, contact),
                        enrollment_id=target.id,
                        scheduled_at=scheduled_iso,
                        action=_batch_action("status" in changed, changed),
                    )
                ],
                peer_excluded=0,
            )
            return
        output_entity("enrollment", target)


def _resolve_seat_schedule(
    override: str | None,
    scheduled_iso: str,
) -> str:
    """Per-row ``scheduled_at`` override, else the batch flag instant."""
    if override is None:
        return scheduled_iso
    parsed = _parse_future_scheduled_at(override)
    assert parsed is not None
    return parsed


def _enrollment_add_batch(
    connection: Any,
    workflow: Any,
    *,
    scheduled_iso: str,
    source: Literal["file", "tag"],
    tag_name: str | None,
    file_rows: list[tuple[str, str | None]] | None,
    min_contacts: int | None,
    limit: int | None,
    company_atomic: bool,
    exclude_peer: bool,
) -> None:
    """Apply a reviewed tag or file cohort with first-touch schedules."""
    from mailpilot.database import (
        apply_enrollment_packing,
        get_account,
        get_contact_by_email,
        preview_enrollment_file_cohort,
        preview_enrollment_tag_cohort,
    )
    from mailpilot.models import EnrollmentBatch, EnrollmentBatchRow
    from mailpilot.operator_log import cli_mutation, operator_event

    if workflow.type != "outbound":
        output_error(
            "--scheduled-at only valid for outbound workflows",
            "invalid_state",
        )
    account = get_account(connection, workflow.account_id)
    account_email = account.email if account is not None else None
    overrides: dict[str, str | None] = {}
    if source == "tag":
        assert tag_name is not None
        tag = _resolve_tag(connection, tag_name)
        preview = preview_enrollment_tag_cohort(
            connection,
            workflow,
            tag,
            min_contacts=min_contacts,
            account_email=account_email,
        )
    else:
        assert file_rows is not None
        rows = file_rows
        overrides = dict(rows)
        preview, missing = preview_enrollment_file_cohort(
            connection,
            workflow,
            [email for email, _override in rows],
            account_email=account_email,
            drop_already_enrolled=False,
        )
        if missing:
            output_error(
                "contact not found: " + ", ".join(missing),
                "not_found",
            )
    contacts, excluded = apply_enrollment_packing(
        preview.contacts,
        preview.excluded,
        limit=limit,
        company_atomic=company_atomic,
        exclude_peer=exclude_peer,
    )
    seats: list[tuple[Any, str]] = [
        (
            contact,
            _resolve_seat_schedule(overrides.get(contact.email.lower()), scheduled_iso),
        )
        for contact in contacts
    ]
    if company_atomic:
        _assert_company_atomic_days(seats)
    enrolled: list[EnrollmentBatchRow] = []
    mutation_attrs: dict[str, Any] = {
        "workflow_id": workflow.id,
        "source": source,
        "scheduled_at": scheduled_iso,
        "count": len(seats),
    }
    with cli_mutation("enrollment", "add", **mutation_attrs):
        try:
            for contact_row, seat_iso in seats:
                contact = get_contact_by_email(connection, contact_row.email)
                if contact is None:
                    output_error(
                        f"contact not found: {contact_row.email}",
                        "not_found",
                    )
                applied = _apply_one_enrollment(
                    connection,
                    workflow,
                    contact,
                    seat_iso,
                    commit=False,
                )
                if applied is None:
                    continue
                target, changed = applied
                enrolled.append(
                    EnrollmentBatchRow(
                        email=contact_row.email,
                        company_domain=contact_row.company_domain,
                        enrollment_id=target.id,
                        scheduled_at=seat_iso,
                        action=_batch_action("status" in changed, changed),
                    )
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        operator_event(
            "enrollment.add",
            workflow_id=workflow.id,
            source=source,
            count=len(enrolled),
            scheduled_at=scheduled_iso,
            changed=["scheduled_first_send"] if enrolled else ["none"],
        )
    batch = EnrollmentBatch(
        workflow=workflow.name,
        scheduled_at=scheduled_iso,
        source=source,
        tag=preview.tag,
        limit=limit,
        company_atomic=company_atomic,
        count=len(enrolled),
        enrolled=enrolled,
        excluded=excluded,
    )
    output(
        {"enrollment_batch": batch.model_dump(mode="json")},
        record_count=batch.count,
    )


@enrollment.command("add")
@click.option(
    "--workflow-id",
    "workflow_ref",
    required=True,
    help="Workflow name or ID.",
)
@click.option(
    "--contact-email",
    default=None,
    help="Contact (email or ID). Required when not using --tag or --file.",
)
@click.option(
    "--tag",
    "tag_ref",
    default=None,
    help=(
        "Company-or-contact tag cohort. With --dry-run: preview. With "
        "--scheduled-at: apply the packed set."
    ),
)
@click.option(
    "--file",
    "file_path",
    default=None,
    type=click.Path(dir_okay=False),
    help=(
        "JSON array of email strings or {email, scheduled_at} objects. "
        "Exclusive with --tag and --contact-email."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview a --tag or --file cohort; no writes.",
)
@click.option(
    "--min-contacts",
    type=int,
    default=None,
    help="Tag path only: include companies with at least N contacts.",
)
@click.option(
    "--limit",
    "seat_limit",
    type=int,
    default=None,
    help=(
        "Cap included seats (first N by company_domain then email). "
        "Soft cap when combined with --company-atomic. Requires --file "
        "or --tag."
    ),
)
@click.option(
    "--company-atomic",
    is_flag=True,
    default=False,
    help=(
        "Never split a domain. Last company may exceed --limit. Included "
        "seats on a domain share the same calendar day. Requires --file "
        "or --tag."
    ),
)
@click.option(
    "--exclude-peer",
    is_flag=True,
    default=False,
    help=(
        "Skip if already active on another workflow. Works with --file, "
        "--tag, or --contact-email."
    ),
)
@click.option(
    "--scheduled-at",
    "scheduled_at",
    default=None,
    help=(
        "ISO 8601 timestamp for scheduled first reach-out (outbound workflows "
        "only). Required for --file / --tag apply. Re-run updates an existing "
        "pending first-reach in place. File rows may override per contact."
    ),
)
def enrollment_add(
    workflow_ref: str,
    contact_email: str | None,
    tag_ref: str | None,
    file_path: str | None,
    dry_run: bool,
    min_contacts: int | None,
    seat_limit: int | None,
    company_atomic: bool,
    exclude_peer: bool,
    scheduled_at: str | None,
) -> None:
    """Enroll a contact, preview a cohort, or apply a scheduled batch.

    Single-contact path: ``--workflow-id`` + ``--contact-email``. When
    ``--scheduled-at`` is given on an outbound workflow, a pending first
    reach-out is inserted, or an existing never-sent first-reach is
    updated in place. ``--exclude-peer`` with ``--contact-email`` skips
    when the contact is already active on another workflow and returns
    ``enrollment_batch`` with ``source=contact``. Without ``--exclude-peer``
    the envelope stays the singular enrollment entity. Tag / file dry-run:
    ``--tag`` or ``--file`` plus ``--dry-run`` returns ``enrollment_preview``
    with no writes. Tag / file apply: same source plus ``--scheduled-at``
    writes one ``enrollment_batch`` envelope. ``--tag`` matches company
    tags or contact tags (union, unique by contact). ``--limit`` and
    ``--company-atomic`` still require ``--file`` or ``--tag``.
    """
    scheduled_iso = _validate_enrollment_add_args(
        contact_email,
        tag_ref,
        file_path,
        dry_run,
        min_contacts,
        scheduled_at,
        seat_limit,
        company_atomic,
    )
    file_rows = (
        _read_enrollment_batch_file(file_path) if file_path is not None else None
    )
    with _db(mutate=True) as connection:
        workflow = _resolve_workflow(connection, workflow_ref)
        if dry_run and tag_ref is not None:
            _enrollment_add_tag_preview(
                connection,
                workflow,
                tag_ref,
                min_contacts,
                limit=seat_limit,
                company_atomic=company_atomic,
                exclude_peer=exclude_peer,
            )
            return
        if dry_run and file_path is not None:
            assert file_rows is not None
            _enrollment_add_file_preview(
                connection,
                workflow,
                file_rows,
                limit=seat_limit,
                company_atomic=company_atomic,
                exclude_peer=exclude_peer,
            )
            return
        if tag_ref is not None or file_path is not None:
            assert scheduled_iso is not None
            _enrollment_add_batch(
                connection,
                workflow,
                scheduled_iso=scheduled_iso,
                source="tag" if tag_ref is not None else "file",
                tag_name=tag_ref,
                file_rows=file_rows,
                min_contacts=min_contacts,
                limit=seat_limit,
                company_atomic=company_atomic,
                exclude_peer=exclude_peer,
            )
            return
        assert contact_email is not None
        _enrollment_add_contact(
            connection,
            workflow,
            contact_email,
            scheduled_iso,
            exclude_peer=exclude_peer,
        )


@enrollment.command("run")
@click.argument("enrollment_id")
def enrollment_run(enrollment_id: str) -> None:
    """Invoke the workflow agent for an enrollment synchronously.

    Manual runs invoke the agent directly. Going through ``create_task``
    would fire ``pg_notify('task_pending')``, which a parallel ``mailpilot
    run`` listener thread translates into a competing drain of the same
    row. Tasks are for deferred work; CLI runs are immediate.
    """
    from mailpilot.agent import invoke_workflow_agent
    from mailpilot.database import (
        get_contact,
        get_enrollment_by_id,
        get_unprocessed_inbound_email,
        get_workflow,
    )
    from mailpilot.settings import get_settings

    settings = get_settings()
    with _db(mutate=True) as connection:
        record = get_enrollment_by_id(connection, enrollment_id)
        if record is None:
            output_error(f"enrollment not found: {enrollment_id}", "not_found")
        wf = get_workflow(connection, record.workflow_id)
        if wf is None:
            output_error(f"workflow not found: {record.workflow_id}", "not_found")
        if wf.status != "active":
            output_error(
                f"workflow is not active (status={wf.status})", "invalid_state"
            )
        contact = get_contact(connection, record.contact_id)
        if contact is None:
            output_error(f"contact not found: {record.contact_id}", "not_found")
        if record.status != "active":
            output_error(
                f"enrollment is not active (status={record.status})",
                "invalid_state",
            )
        email = None
        if wf.type == "inbound":
            email = get_unprocessed_inbound_email(connection, wf.id, contact.id)
        envelope: dict[str, object] = {
            "enrollment_id": record.id,
            "workflow_id": wf.id,
            "contact_id": contact.id,
        }
        try:
            # §V.30: prompt framing comes from `trigger`, not a synthesised
            # task_description. enrollment_run is an initial reach-out, not
            # resumed deferred work.
            result = invoke_workflow_agent(
                connection,
                settings,
                wf,
                contact,
                email=email,
                trigger="enrollment_run",
            )
        except Exception as exc:
            envelope["status"] = "failed"
            envelope["result"] = {"reason": str(exc)}
            output(envelope)
            return
        if result is None:
            envelope["status"] = "skipped"
            envelope["result"] = {"reason": "agent lock held"}
            output(envelope)
            return
        envelope["status"] = "completed"
        envelope["result"] = {
            "reasoning": result.get("reasoning", ""),
            "tool_calls": result.get("tool_calls", 0),
        }
        output(envelope)


@enrollment.command("disable")
@click.argument("enrollment_id")
@click.option(
    "--reason",
    required=True,
    help="Explanation written to disabled_reason and the enrollment_disabled activity.",
)
def enrollment_disable(enrollment_id: str, reason: str) -> None:
    """Soft-disable an enrollment via terminal lifecycle exit.

    Flips ``status='disabled'``, writes ``disabled_reason``, and appends an
    ``enrollment_disabled`` activity carrying the reason. Disabled is
    terminal -- re-enrolling means creating a fresh enrollment via
    ``enrollment add``.
    """
    from mailpilot.database import (
        disable_enrollment,
        get_enrollment_by_id,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    if reason.strip() == "":
        output_error("reason cannot be empty", "validation_error")
    with _db(mutate=True) as connection:
        before = get_enrollment_by_id(connection, enrollment_id)
        if before is None:
            output_error(f"enrollment not found: {enrollment_id}", "not_found")
        with cli_mutation("enrollment", "disable", entity_id=enrollment_id):
            updated = disable_enrollment(connection, enrollment_id, reason)
            if updated is None:
                output_error(f"enrollment not found: {enrollment_id}", "not_found")
            changed = [
                field
                for field in ("status", "disabled_reason")
                if getattr(before, field) != getattr(updated, field)
            ]
            operator_event(
                "enrollment.disable",
                entity_id=enrollment_id,
                changed=changed,
            )
            output_entity("enrollment", updated)


@enrollment.command("enable")
@click.argument("enrollment_id")
def enrollment_enable(enrollment_id: str) -> None:
    """Re-enable a disabled enrollment by flipping status back to active.

    Clears disabled_reason and resumes the enrollment. Enabling an enrollment
    that is not disabled is rejected.
    """
    from mailpilot.database import (
        enable_enrollment,
        get_enrollment_by_id,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    with _db(mutate=True) as connection:
        before = get_enrollment_by_id(connection, enrollment_id)
        if before is None:
            output_error(f"enrollment not found: {enrollment_id}", "not_found")
        if before.status != "disabled":
            output_error(
                f"enrollment {enrollment_id} is not disabled",
                "validation_error",
            )
        with cli_mutation("enrollment", "enable", entity_id=enrollment_id):
            updated = enable_enrollment(connection, enrollment_id)
            if updated is None:
                output_error(
                    f"enrollment {enrollment_id} is not disabled",
                    "validation_error",
                )
            changed = [
                field
                for field in ("status", "disabled_reason")
                if getattr(before, field) != getattr(updated, field)
            ]
            operator_event(
                "enrollment.enable",
                entity_id=enrollment_id,
                changed=changed,
            )
            output_entity("enrollment", updated)


@enrollment.command("view")
@click.argument("enrollment_id")
def enrollment_view(enrollment_id: str) -> None:
    """View an enrollment by id."""
    from mailpilot.database import get_enrollment_by_id

    with _db() as connection:
        record = get_enrollment_by_id(connection, enrollment_id)
        if record is None:
            output_error("enrollment not found", "not_found")
        output_entity("enrollment", record)


@enrollment.command("list")
@scope_option("--workflow-id", "workflow_id", "Filter by workflow (name or ID).")
@scope_option("--contact-email", "contact_email", "Filter by contact (email or ID).")
@enum_option("--status", "status", _ENROLLMENT_STATUSES, "Filter by enrollment status.")
@click.option(
    "--disposition",
    default=None,
    help=(
        "Filter by latest terminal disposition: meeting_booked, do_not_contact, "
        "or contact_later. Unknown values return validation_error with allowed set."
    ),
)
@click.option(
    "--full",
    "full",
    is_flag=True,
    default=False,
    help="Denser projection: company, touch progress, next send, disposition.",
)
@click.option(
    "--stuck",
    is_flag=True,
    default=False,
    help="Only enrollments matching stuck heuristics (implies denser fields).",
)
@presence_option("pending-task", "Filter on presence of a pending follow-up task.")
@click.option(
    "--touch",
    type=int,
    default=None,
    help=(
        "Filter by next pending touch number (or last sent when none pending). "
        "Touch 1 also matches never-sent rows that have a scheduled first send."
    ),
)
@sort_option(["updated_at", "next_scheduled_at"], default="updated_at")
@desc_option
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "table", "csv", "ndjson"], case_sensitive=False),
    default="json",
    show_default=True,
    help="Output format (default JSON envelope).",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, writable=True, path_type=str),
    default=None,
    help="Write csv/ndjson to this path (status envelope on stdout).",
)
@time_window_options("updated_at")
@limit_option
def enrollment_list(
    workflow_id: str | None,
    contact_email: str | None,
    status: str | None,
    disposition: str | None,
    full: bool,
    stuck: bool,
    has_pending_task: bool | None,
    touch: int | None,
    sort: str,
    desc: bool,
    output_format: str,
    out_path: str | None,
    limit: int,
    since: str | None,
    until: str | None,
) -> None:
    """List enrollments as summaries. Filter by workflow, contact, or both.

    Default rows stay lean. Pass --full for company, touch progress, next send,
    and disposition fields used in campaign triage. --disposition filters by
    latest terminal disposition (meeting_booked, do_not_contact, contact_later).
    """
    from mailpilot.database import (
        list_enrollments_detailed,
    )
    from mailpilot.models import ENROLLMENT_FULL_FIELDS

    if disposition is not None and disposition not in _ENROLLMENT_DISPOSITIONS:
        allowed = ", ".join(_ENROLLMENT_DISPOSITIONS)
        output_error(
            f"invalid disposition {disposition!r}; allowed: {allowed}",
            "validation_error",
        )

    with _db() as connection:
        resolved_workflow_id: str | None = (
            _resolve_workflow_id(connection, workflow_id)
            if workflow_id is not None
            else None
        )
        contact_id = (
            _resolve_contact(connection, contact_email).id
            if contact_email is not None
            else None
        )
        use_full = full or stuck
        rows = list_enrollments_detailed(
            connection,
            workflow_id=resolved_workflow_id,
            contact_id=contact_id,
            status=status,
            limit=limit,
            since=since,
            until=until,
            full=use_full,
            has_pending_task=has_pending_task,
            touch=touch,
            sort=sort,
            desc=desc,
            stuck=stuck,
            disposition=disposition,
        )
        exclude = None if use_full else set(ENROLLMENT_FULL_FIELDS)
        dumped = [r.model_dump(mode="json", exclude=exclude) for r in rows]
        if output_format.lower() == "json":
            output({"enrollments": dumped})
        else:
            _emit_formatted(
                "enrollments",
                {"enrollments": dumped},
                rows=dumped,
                output_format=output_format,
                out_path=out_path,
            )
