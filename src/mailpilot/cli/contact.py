"""Contact commands."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import Any, Literal

import click

from mailpilot._filters import (
    enrollment_coverage_options,
    include_disabled_option,
    limit_option,
    range_options,
    scope_option,
    tag_filter_options,
    time_window_options,
)
from mailpilot.cli._helpers import (
    _batch_error,
    _batch_ok,
    _emit_batch_results,
    _optional_str_fields,
    _parse_json_object,
    _parse_ndjson_object,
    _read_stdin_ndjson_lines,
    _required_nonempty_str,
    _resolve_disable_reason,
)
from mailpilot.cli.main import (
    _db,
    _resolve_company,
    _resolve_contact,
    _resolve_tag_ids,
    main,
    output,
    output_entity,
    output_error,
)


def _parse_contact_create_fields(
    payload: dict[str, object], line_number: int
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    """Validate a contact-create NDJSON object into create kwargs + ref.

    Returns ``(fields, None)`` where fields includes ``ref`` and create args,
    or ``(None, error_row)``.
    """
    email, ref, email_error = _required_nonempty_str(payload, "email", line_number)
    if email_error is not None:
        return None, email_error
    assert email is not None
    ref = email.lower()
    optional, opt_error = _optional_str_fields(
        payload,
        ("first_name", "last_name", "title", "note", "company_domain"),
        ref,
    )
    if opt_error is not None:
        return None, opt_error
    assert optional is not None
    confidence_raw = payload.get("email_confidence")
    confidence: int | None
    if confidence_raw is None:
        confidence = None
    elif isinstance(confidence_raw, int) and not isinstance(confidence_raw, bool):
        confidence = confidence_raw
    else:
        return None, _batch_error(
            ref, "validation_error", "email_confidence must be an integer"
        )
    meta_raw = payload.get("meta")
    meta: dict[str, object] | None
    if meta_raw is None:
        meta = None
    elif isinstance(meta_raw, dict):
        meta = meta_raw
    else:
        return None, _batch_error(ref, "validation_error", "meta must be a JSON object")
    upsert_raw = payload.get("upsert")
    if upsert_raw is None:
        upsert = False
    elif isinstance(upsert_raw, bool):
        upsert = upsert_raw
    else:
        return None, _batch_error(ref, "validation_error", "upsert must be a boolean")
    return {
        "ref": ref,
        "email": email,
        "first_name": optional["first_name"],
        "last_name": optional["last_name"],
        "title": optional["title"],
        "note": optional["note"],
        "company_domain": optional["company_domain"],
        "email_confidence": confidence,
        "meta": meta,
        "upsert": upsert,
    }, None


def _contact_upsert_fields(
    *,
    title: str | None,
    email_confidence: int | None,
    company_id: str | None,
    company_domain_set: bool,
    verification_meta: dict[str, object] | None,
    meta_set: bool,
) -> dict[str, object]:
    """Build field-selective contact update kwargs for §V.147 upsert.

    Only supplied create flags are included — omitted fields are never
    clobbered. ``first_name`` / ``last_name`` are insert-only.
    """
    fields: dict[str, object] = {}
    if title is not None:
        fields["title"] = title
    if email_confidence is not None:
        fields["email_confidence"] = email_confidence
    if company_domain_set:
        fields["company_id"] = company_id
    if meta_set:
        fields["verification_meta"] = verification_meta
    return fields


def _contact_create_stdin_row(  # noqa: C901, PLR0911, PLR0912
    connection: Any, line_number: int, line: str
) -> dict[str, object]:
    """Process one ``contact create --stdin`` NDJSON line into a result row."""
    from mailpilot.database import (
        add_contact_note,
        create_contact,
        get_contact_by_email,
        update_contact,
    )
    from mailpilot.operator_log import operator_event

    payload, parse_error = _parse_ndjson_object(line_number, line)
    if parse_error is not None:
        return parse_error
    assert payload is not None
    fields, field_error = _parse_contact_create_fields(payload, line_number)
    if field_error is not None:
        return field_error
    assert fields is not None
    ref = str(fields["ref"])
    company_domain = fields["company_domain"]
    company_id: str | None = None
    company_domain_set = isinstance(company_domain, str)
    if company_domain_set:
        company = _resolve_company(connection, str(company_domain), missing="none")
        if company is None:
            return _batch_error(
                ref, "not_found", f"company not found: {company_domain}"
            )
        company_id = company.id
    meta = fields["meta"] if isinstance(fields["meta"], dict) else None
    meta_set = fields["meta"] is not None
    do_upsert = bool(fields["upsert"])
    created = create_contact(
        connection,
        email=str(fields["email"]),
        first_name=fields["first_name"]
        if isinstance(fields["first_name"], str)
        else None,
        last_name=fields["last_name"] if isinstance(fields["last_name"], str) else None,
        company_id=company_id,
        title=fields["title"] if isinstance(fields["title"], str) else None,
        email_confidence=(
            fields["email_confidence"]
            if isinstance(fields["email_confidence"], int)
            else None
        ),
        verification_meta=meta,
    )
    if created is None:
        if not do_upsert:
            # Safe-idempotent: duplicate natural key -> ok skip (§V.139).
            return _batch_ok(ref)
        existing = get_contact_by_email(connection, str(fields["email"]))
        if existing is None:
            return _batch_error(
                ref, "duplicate_key", f"contact with email={ref!r} already exists"
            )
        update_fields = _contact_upsert_fields(
            title=fields["title"] if isinstance(fields["title"], str) else None,
            email_confidence=(
                fields["email_confidence"]
                if isinstance(fields["email_confidence"], int)
                else None
            ),
            company_id=company_id,
            company_domain_set=company_domain_set,
            verification_meta=meta,
            meta_set=meta_set,
        )
        if update_fields:
            updated = update_contact(connection, existing.id, **update_fields)
            if updated is None:
                return _batch_error(ref, "not_found", f"contact not found: {ref}")
            operator_event(
                "contact.upsert",
                entity_id=updated.id,
                email=updated.email,
                company_id=company_id,
                created=False,
                changed=sorted(update_fields),
            )
        return _batch_ok(ref)
    changed = ["email", "first_name", "last_name", "company_id"]
    title = fields["title"]
    if isinstance(title, str):
        changed.append("title")
    if isinstance(fields["email_confidence"], int):
        changed.append("email_confidence")
    if meta is not None:
        changed.append("verification_meta")
    note = fields["note"]
    if isinstance(note, str) and note:
        add_contact_note(connection, created.id, note)
        changed.append("note")
    operator_event(
        "contact.create",
        entity_id=created.id,
        email=created.email,
        company_id=company_id,
        changed=changed,
    )
    return _batch_ok(ref)


def _run_contact_create_stdin() -> None:
    """Drive ``contact create --stdin`` NDJSON batch (§V.139)."""
    from mailpilot.operator_log import cli_mutation

    lines = _read_stdin_ndjson_lines()
    with (
        _db(mutate=True) as connection,
        cli_mutation("contact", "create", mode="stdin", row_count=len(lines)),
    ):
        results = [
            _contact_create_stdin_row(connection, line_number, line)
            for line_number, line in lines
        ]
        _emit_batch_results(results)


# -- Contact commands ----------------------------------------------------------


@main.group()
def contact() -> None:
    """Manage contacts."""


@contact.command("create")
@click.option("--email", default=None, help="Email address (single-entity mode).")
@click.option("--first-name", default=None, help="First name.")
@click.option("--last-name", default=None, help="Last name.")
@click.option("--company-domain", default=None, help="Owning company (domain or ID).")
@click.option("--title", default=None, help="Role label (lead-metadata).")
@click.option(
    "--email-confidence",
    type=int,
    default=None,
    help="Deliverability score 0-100; low = high risk (lead-metadata).",
)
@click.option(
    "--meta-json",
    default=None,
    help=(
        "Operator-only verification meta as a JSON object "
        "(e.g. bouncer_status, source). Never injected into agent prompts."
    ),
)
@click.option(
    "--note",
    default=None,
    help="Optional first note body. Appended atomically as a `note` row.",
)
@click.option(
    "--stdin",
    "from_stdin",
    is_flag=True,
    default=False,
    help=(
        "Batch mode: read NDJSON from stdin, one object per line with "
        "contact create fields (email required; first_name, last_name, "
        "company_domain, title, email_confidence, meta, note, upsert "
        "optional). Exclusive with single-entity create options. "
        "Duplicate email is an ok skip unless upsert:true (field-selective "
        "update). Exit 0 when every row is ok; exit 1 if any row errors "
        "(full results JSON still on stdout)."
    ),
)
@click.option(
    "--upsert",
    is_flag=True,
    default=False,
    help=(
        "On natural-key conflict, update title / email_confidence / "
        "company_domain / meta when those flags are present (never clobber "
        "omitted fields). Without this flag, duplicate email returns "
        "duplicate_key. Preferred agent path."
    ),
)
def contact_create(  # noqa: C901
    email: str | None,
    first_name: str | None,
    last_name: str | None,
    company_domain: str | None,
    title: str | None,
    email_confidence: int | None,
    meta_json: str | None,
    note: str | None,
    from_stdin: bool,
    upsert: bool,
) -> None:
    """Create a new contact (single-entity or ``--stdin`` NDJSON batch)."""
    from mailpilot.database import (
        add_contact_note,
        create_contact,
        get_contact_by_email,
        update_contact,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    single_entity_set = any(
        value is not None
        for value in (
            email,
            first_name,
            last_name,
            company_domain,
            title,
            email_confidence,
            meta_json,
            note,
        )
    )
    if from_stdin:
        if single_entity_set or upsert:
            output_error(
                "--stdin is exclusive with single-entity create options",
                "validation_error",
            )
        _run_contact_create_stdin()
        return

    if email is None:
        output_error(
            "--email is required (or pass --stdin)",
            "validation_error",
        )
    verification_meta = (
        _parse_json_object(meta_json, what="meta") if meta_json is not None else None
    )
    with _db(mutate=True) as connection:
        company_id = (
            _resolve_company(connection, company_domain).id
            if company_domain is not None
            else None
        )
        with cli_mutation(
            "contact", "create", email=email, company_id=company_id, upsert=upsert
        ):
            created_row = create_contact(
                connection,
                email=email,
                first_name=first_name,
                last_name=last_name,
                company_id=company_id,
                title=title,
                email_confidence=email_confidence,
                verification_meta=verification_meta,
            )
            if created_row is None:
                if not upsert:
                    output_error(
                        f"contact with email={email!r} already exists",
                        "duplicate_key",
                    )
                existing = get_contact_by_email(connection, email)
                if existing is None:
                    output_error(
                        f"contact with email={email!r} already exists",
                        "duplicate_key",
                    )
                update_fields = _contact_upsert_fields(
                    title=title,
                    email_confidence=email_confidence,
                    company_id=company_id,
                    company_domain_set=company_domain is not None,
                    verification_meta=verification_meta,
                    meta_set=meta_json is not None,
                )
                contact_out = existing
                if update_fields:
                    updated = update_contact(connection, existing.id, **update_fields)
                    if updated is None:
                        output_error(
                            f"contact with email={email!r} already exists",
                            "duplicate_key",
                        )
                    contact_out = updated
                operator_event(
                    "contact.upsert",
                    entity_id=contact_out.id,
                    email=contact_out.email,
                    company_id=company_id,
                    created=False,
                    changed=sorted(update_fields) if update_fields else ["none"],
                )
                output_entity("contact", contact_out, created=False)
                return
            changed = ["email", "first_name", "last_name", "company_id"]
            if title is not None:
                changed.append("title")
            if email_confidence is not None:
                changed.append("email_confidence")
            if verification_meta is not None:
                changed.append("verification_meta")
            if note:
                add_contact_note(connection, created_row.id, note)
                changed.append("note")
            operator_event(
                "contact.create",
                entity_id=created_row.id,
                email=created_row.email,
                company_id=company_id,
                changed=changed,
            )
            output_entity("contact", created_row, created=True)


@contact.command("update")
@click.argument("contact_ref")
@click.option("--email", default=None, help="Email address.")
@click.option("--first-name", default=None, help="First name.")
@click.option("--last-name", default=None, help="Last name.")
@click.option("--company-domain", default=None, help="Owning company (domain or ID).")
@click.option("--title", default=None, help="Role label (lead-metadata).")
@click.option(
    "--email-confidence",
    type=int,
    default=None,
    help="Deliverability score 0-100; low = high risk (lead-metadata).",
)
@click.option(
    "--meta-json",
    default=None,
    help=(
        "Operator-only verification meta as a JSON object "
        "(replaces existing meta; never injected into agent prompts)."
    ),
)
def contact_update(
    contact_ref: str,
    email: str | None,
    first_name: str | None,
    last_name: str | None,
    company_domain: str | None,
    title: str | None,
    email_confidence: int | None,
    meta_json: str | None,
) -> None:
    """Update a contact (addressed by email or ID)."""
    from mailpilot.database import update_contact
    from mailpilot.operator_log import cli_mutation, operator_event

    with _db(mutate=True) as connection:
        before = _resolve_contact(connection, contact_ref)
        contact_id = before.id
        fields: dict[str, object] = {}
        if email is not None:
            fields["email"] = email
        if first_name is not None:
            fields["first_name"] = first_name
        if last_name is not None:
            fields["last_name"] = last_name
        if company_domain is not None:
            fields["company_id"] = _resolve_company(connection, company_domain).id
        if title is not None:
            fields["title"] = title
        if email_confidence is not None:
            fields["email_confidence"] = email_confidence
        if meta_json is not None:
            fields["verification_meta"] = _parse_json_object(meta_json, what="meta")
        with cli_mutation("contact", "update", entity_id=contact_id):
            updated = update_contact(connection, contact_id, **fields)
            if updated is None:
                output_error(f"contact not found: {contact_id}", "not_found")
            changed = [
                field
                for field in (
                    "email",
                    "first_name",
                    "last_name",
                    "company_id",
                    "title",
                    "email_confidence",
                    "verification_meta",
                )
                if getattr(before, field) != getattr(updated, field)
            ]
            operator_event(
                "contact.update",
                entity_id=contact_id,
                changed=changed,
            )
            output_entity("contact", updated)


@contact.command("disable")
@click.argument("contact_ref")
@click.option(
    "--reason",
    default=None,
    help="Explanation written to disabled_reason.",
)
@click.option(
    "--reason-file",
    "reason_file",
    default=None,
    type=click.Path(dir_okay=False),
    help=("Read disable reason from a UTF-8 file (exclusive with --reason)."),
)
def contact_disable(
    contact_ref: str, reason: str | None, reason_file: str | None
) -> None:
    """Soft-disable a contact by writing disabled_reason (email or ID).

    Pass ``--reason`` or ``--reason-file`` (exactly one).
    """
    from mailpilot.database import disable_contact
    from mailpilot.operator_log import cli_mutation, operator_event

    resolved_reason = _resolve_disable_reason(reason, reason_file)
    with _db(mutate=True) as connection:
        before = _resolve_contact(connection, contact_ref)
        contact_id = before.id
        with cli_mutation("contact", "disable", entity_id=contact_id):
            updated = disable_contact(connection, contact_id, resolved_reason)
            if updated is None:
                output_error(f"contact not found: {contact_id}", "not_found")
            changed = (
                ["disabled_reason"]
                if before.disabled_reason != updated.disabled_reason
                else []
            )
            operator_event(
                "contact.disable",
                entity_id=contact_id,
                changed=changed,
            )
            output_entity("contact", updated)


@contact.command("enable")
@click.argument("contact_ref")
def contact_enable(contact_ref: str) -> None:
    """Re-enable a disabled contact by clearing disabled_reason.

    Clears any reason, including a `bounced:` or `unsubscribed:` block -- the
    operator owns consent. Enabling a contact that is not disabled is rejected.
    Addressed by email or ID.
    """
    from mailpilot.database import enable_contact
    from mailpilot.operator_log import cli_mutation, operator_event

    with _db(mutate=True) as connection:
        before = _resolve_contact(connection, contact_ref)
        contact_id = before.id
        if before.disabled_reason is None:
            output_error(
                f"contact {contact_id} is not disabled",
                "validation_error",
            )
        with cli_mutation("contact", "enable", entity_id=contact_id):
            updated = enable_contact(connection, contact_id)
            if updated is None:
                output_error(
                    f"contact {contact_id} is not disabled",
                    "validation_error",
                )
            operator_event(
                "contact.enable",
                entity_id=contact_id,
                changed=["disabled_reason"],
            )
            output_entity("contact", updated)


@contact.command("search")
@click.argument("query")
@limit_option
def contact_search(query: str, limit: int) -> None:
    """Search contacts by email, name, or title.

    Rows project tags (assigned names, empty ok) with title and
    company_domain. Single-token: substring match on email, first_name,
    last_name, or title. Full name (e.g. "David Drouin"): order-preserving
    match on first+last. Multi-token: every token must match at least one
    of those fields (AND). Disabled contacts remain searchable.
    """
    from mailpilot.database import search_contacts

    with _db() as connection:
        contacts = search_contacts(connection, query, limit=limit)
        output({"contacts": [c.model_dump(mode="json") for c in contacts]})


@contact.command("list")
@scope_option(
    "--company-domain",
    "company_domain",
    "Filter by owning company (domain or ID).",
)
@range_options(
    "email-confidence",
    "Surface only rows with email_confidence >= N (composes with --max).",
    "Surface only rows with email_confidence <= N (low-score lead review).",
)
@click.option(
    "--title",
    default=None,
    help="Filter by title (case-insensitive exact match; use search for substring).",
)
@tag_filter_options
@enrollment_coverage_options
@include_disabled_option
@time_window_options("created_at")
@limit_option
def contact_list(
    limit: int,
    company_domain: str | None,
    since: str | None,
    until: str | None,
    include_disabled: bool,
    unenrolled: bool,
    enrolled: bool,
    max_email_confidence: int | None,
    min_email_confidence: int | None,
    title: str | None,
    tag: tuple[str, ...],
    no_tag: tuple[str, ...],
) -> None:
    """List contacts as summaries.

    Each row projects tags (assigned names, empty ok) with title and
    company_domain. Repeatable --tag is AND (row must carry every named
    tag). Repeatable --no-tag is AND (row must carry none of the named
    tags). --unenrolled keeps contacts with zero enrollment rows (any
    workflow, any status); --enrolled keeps contacts with at least one.
    The two flags are exclusive. Disabled enrollments still count as
    enrolled. Default list still excludes disabled contacts.
    """
    from mailpilot.database import list_contacts

    if unenrolled and enrolled:
        output_error(
            "--unenrolled is exclusive with --enrolled",
            "validation_error",
        )
    enrollment: Literal["unenrolled", "enrolled"] | None
    if unenrolled:
        enrollment = "unenrolled"
    elif enrolled:
        enrollment = "enrolled"
    else:
        enrollment = None
    with _db() as connection:
        company_id = (
            _resolve_company(connection, company_domain).id
            if company_domain is not None
            else None
        )
        tag_ids = _resolve_tag_ids(connection, tag)
        exclude_tag_ids = _resolve_tag_ids(connection, no_tag)
        contacts = list_contacts(
            connection,
            limit=limit,
            company_id=company_id,
            since=since,
            until=until,
            include_disabled=include_disabled,
            max_email_confidence=max_email_confidence,
            min_email_confidence=min_email_confidence,
            title=title,
            tag=tag_ids or None,
            exclude_tags=exclude_tag_ids,
            enrollment=enrollment,
        )
        output({"contacts": [c.model_dump(mode="json") for c in contacts]})


@contact.command("view")
@click.argument("contact_ref")
@click.option(
    "--include-meta",
    is_flag=True,
    default=False,
    help=(
        "Project operator-only verification_meta (null when unset). "
        "Default view and the agent prompt path omit meta."
    ),
)
@click.option(
    "--timeline",
    is_flag=True,
    default=False,
    help=(
        "Include bounded dossier: enrollments (status, disposition, last/next "
        "touch), recent emails, and recent activities. Default view is notes "
        "only. Default 10 rows per section; use --limit (hard cap 50)."
    ),
)
@click.option(
    "--limit",
    default=10,
    show_default=True,
    help=(
        "Max rows per --timeline section (enrollments, emails, activities). "
        "Hard cap 50."
    ),
)
def contact_view(
    contact_ref: str, include_meta: bool, timeline: bool, limit: int
) -> None:
    """Show a contact by email or ID with inlined notes (own + parent company).

    Projects tags (assigned names, empty ok). Pass --timeline for a
    bounded dossier (enrollments + emails + activities). Default path
    stays notes-only for agent prompt budget.
    """
    from mailpilot.database import load_contact_timeline, load_contact_view

    with _db() as connection:
        contact = _resolve_contact(connection, contact_ref)
        if timeline:
            payload = load_contact_timeline(connection, contact.id, limit=limit)
            if payload is None:
                output_error(f"contact not found: {contact_ref}", "not_found")
            if include_meta:
                payload["verification_meta"] = contact.verification_meta
            output({"contact": payload})
            return
        found = load_contact_view(connection, contact.id)
        if found is None:
            output_error(f"contact not found: {contact_ref}", "not_found")
        if not include_meta:
            output_entity("contact", found)
            return
        # Default ContactView is agent-safe; merge meta only when operator asks.
        payload = found.model_dump(mode="json")
        payload["verification_meta"] = contact.verification_meta
        output({"contact": payload})
