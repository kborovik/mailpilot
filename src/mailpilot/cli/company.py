"""Target company commands."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from typing import Any

import click

from mailpilot._filters import (
    COMPANY_SORT_KEYS,
    desc_option,
    enum_option,
    include_disabled_option,
    limit_option,
    offset_option,
    presence_option,
    range_options,
    sort_option,
    tag_filter_options,
    time_window_options,
)
from mailpilot.cli._helpers import (
    _batch_error,
    _batch_ok,
    _emit_batch_results,
    _parse_json_object,
    _parse_ndjson_object,
    _read_stdin_ndjson_lines,
    _required_nonempty_str,
    _resolve_disable_reason,
)
from mailpilot.cli.main import (
    _company_cohort_kwargs,
    _db,
    _looks_like_uuid,
    _output_company_create_entity,
    _resolve_company,
    _resolve_company_id,
    _resolve_tag,
    main,
    output,
    output_entity,
    output_error,
)

_COMPANY_PIPELINE_STATUSES = [
    "ready",
    "needs_contacts",
    "needs_profile",
    "disabled",
]


def _company_disable_stdin_row(
    connection: Any, line_number: int, line: str
) -> dict[str, object]:
    """Process one ``company disable --stdin`` NDJSON line into a result row."""
    from mailpilot.database import disable_company
    from mailpilot.operator_log import operator_event

    payload, parse_error = _parse_ndjson_object(line_number, line)
    if parse_error is not None:
        return parse_error
    assert payload is not None
    domain, ref, domain_error = _required_nonempty_str(payload, "domain", line_number)
    if domain_error is not None:
        return domain_error
    assert domain is not None
    reason, _, reason_error = _required_nonempty_str(payload, "reason", line_number)
    if reason_error is not None:
        # Prefer domain as ref when reason is the only missing field.
        return _batch_error(ref, "validation_error", "reason is required")
    assert reason is not None
    company = _resolve_company(connection, domain, missing="none")
    if company is None:
        return _batch_error(domain, "not_found", f"company not found: {domain}")
    if company.disabled_reason is None:
        updated = disable_company(connection, company.id, reason)
        if updated is not None:
            operator_event(
                "company.disable",
                entity_id=company.id,
                changed=["disabled_reason"],
            )
    # Active disable, already-disabled no-op, and race no-op all report ok.
    return _batch_ok(domain)


def _run_company_disable_stdin() -> None:
    """Drive ``company disable --stdin`` NDJSON batch (§V.139)."""
    from mailpilot.operator_log import cli_mutation

    lines = _read_stdin_ndjson_lines()
    with (
        _db(mutate=True) as connection,
        cli_mutation("company", "disable", mode="stdin", row_count=len(lines)),
    ):
        results = [
            _company_disable_stdin_row(connection, line_number, line)
            for line_number, line in lines
        ]
        _emit_batch_results(results)


# -- Company commands ----------------------------------------------------------


@main.group()
def company() -> None:
    """Manage target companies."""


def _company_profile_options(fn: Any) -> Any:
    """Stack profile write flags shared by ``company create`` and ``update``."""
    fn = click.option(
        "--target-customers",
        default=None,
        help="Patch profile.target_customers (merge).",
    )(fn)
    fn = click.option(
        "--timezone",
        default=None,
        help="Patch profile.timezone (empty string clears to null).",
    )(fn)
    fn = click.option(
        "--source",
        multiple=True,
        help="Patch profile.sources (repeatable; replaces the sources list).",
    )(fn)
    fn = click.option(
        "--product",
        multiple=True,
        help="Patch profile.products (repeatable; replaces the products list).",
    )(fn)
    fn = click.option("--summary", default=None, help="Patch profile.summary (merge).")(
        fn
    )
    fn = click.option(
        "--profile",
        default=None,
        help="Full-replace profile; pass '-' to read a JSON object from stdin.",
    )(fn)
    fn = click.option(
        "--profile-file",
        default=None,
        type=click.Path(exists=True, dir_okay=False),
        help="Full-replace profile from a JSON file path.",
    )(fn)
    fn = click.option(
        "--profile-json",
        default=None,
        help=(
            "Full-replace profile as an inline JSON object "
            "(prefer --profile-file or --profile -)."
        ),
    )(fn)
    return fn


def _profile_replace_and_patch_flags(
    profile_json: str | None,
    profile_file: str | None,
    profile: str | None,
    summary: str | None,
    product: tuple[str, ...],
    source: tuple[str, ...],
    timezone: str | None,
    target_customers: str | None,
) -> tuple[list[str], bool]:
    """Return (replace flag names, has_patch); error on exclusive violations."""
    replace_flags: list[str] = []
    if profile_json is not None:
        replace_flags.append("--profile-json")
    if profile_file is not None:
        replace_flags.append("--profile-file")
    if profile is not None:
        replace_flags.append("--profile")
    has_patch = any(
        (
            summary is not None,
            bool(product),
            bool(source),
            timezone is not None,
            target_customers is not None,
        )
    )
    if len(replace_flags) > 1:
        output_error(
            "full-replace profile options are exclusive: " + ", ".join(replace_flags),
            "validation_error",
        )
    if replace_flags and has_patch:
        output_error(
            "full-replace profile options are exclusive with field-patch flags",
            "validation_error",
        )
    if profile is not None and profile != "-":
        output_error(
            "--profile only accepts '-' for stdin; use --profile-file for a path",
            "validation_error",
        )
    return replace_flags, has_patch


def _read_replace_profile(
    profile_json: str | None,
    profile_file: str | None,
    profile: str | None,
) -> dict[str, object]:
    """Load a full-replace profile dict from json / file / stdin."""
    import pathlib
    import sys

    if profile_json is not None:
        return _parse_json_object(profile_json, what="profile")
    if profile_file is not None:
        raw = pathlib.Path(profile_file).read_text(encoding="utf-8")
        return _parse_json_object(raw, what="profile")
    return _parse_json_object(sys.stdin.read(), what="profile")


def _validate_company_profile_payload(payload: dict[str, object]) -> dict[str, object]:
    """Validate a profile dict before any CRM write (§V.72 / §V.167)."""
    from pydantic import ValidationError

    from mailpilot.models import CompanyProfile

    try:
        return CompanyProfile.model_validate(payload).model_dump(exclude_unset=True)
    except ValidationError as exc:
        output_error(str(exc), "validation_error")


@company.command("create")
@click.option("--domain", required=True, help="Primary domain.")
@click.option("--name", default="", help="Company name.")
@click.option(
    "--alias",
    "aliases",
    multiple=True,
    help=(
        "Alternate domain that resolves to this company (repeatable). "
        "Shared domain space: cannot match another company domain or alias."
    ),
)
@click.option(
    "--note",
    default=None,
    help="Optional first note body. Appended atomically as a `note` row.",
)
@click.option(
    "--upsert",
    is_flag=True,
    default=False,
    help=(
        "On natural-key conflict, update name when non-empty and register "
        "missing aliases only (never wipe profile unless profile flags "
        "are also passed). Without this flag, duplicate domain returns "
        "already_exists. Preferred agent path."
    ),
)
@_company_profile_options
@click.option(
    "--tag",
    "tags",
    multiple=True,
    help=(
        "Defined vocabulary tag to link (repeatable, additive). "
        "Undefined names return not_found; never auto-created."
    ),
)
def company_create(  # noqa: C901, PLR0912, PLR0915
    domain: str,
    name: str,
    aliases: tuple[str, ...],
    note: str | None,
    upsert: bool,
    profile_json: str | None,
    profile_file: str | None,
    profile: str | None,
    summary: str | None,
    product: tuple[str, ...],
    source: tuple[str, ...],
    timezone: str | None,
    target_customers: str | None,
    tags: tuple[str, ...],
) -> None:
    """Create a company; optional profile flags and --tag are one transaction."""
    from mailpilot.database import (
        add_company_alias,
        add_company_note,
        assign_tag_to_company,
        create_company,
        get_company_by_domain_exact,
        load_company_view,
        write_company_fields,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    if not domain.strip():
        output_error("domain cannot be empty", "validation_error")
    replace_flags, has_patch = _profile_replace_and_patch_flags(
        profile_json,
        profile_file,
        profile,
        summary,
        product,
        source,
        timezone,
        target_customers,
    )
    replace_payload = (
        _read_replace_profile(profile_json, profile_file, profile)
        if replace_flags
        else None
    )
    tag_names = list(dict.fromkeys(tags))
    with _db(mutate=True) as connection:
        tag_rows = [_resolve_tag(connection, tag_name) for tag_name in tag_names]
        with cli_mutation("company", "create", domain=domain, upsert=upsert):
            existing_before = (
                get_company_by_domain_exact(connection, domain) if has_patch else None
            )
            profile_payload: dict[str, object] | None = None
            if replace_payload is not None:
                profile_payload = _validate_company_profile_payload(replace_payload)
            elif has_patch:
                existing_profile = (
                    existing_before.profile
                    if existing_before is not None
                    and isinstance(existing_before.profile, dict)
                    else None
                )
                profile_payload = _validate_company_profile_payload(
                    _merge_company_profile_patch(
                        existing_profile,
                        summary=summary,
                        products=product,
                        sources=source,
                        timezone=timezone,
                        target_customers=target_customers,
                    )
                )
            created_row = create_company(
                connection,
                name=name,
                domain=domain,
                aliases=list(aliases) if aliases else None,
                commit=False,
            )
            created = created_row is not None
            if created_row is None:
                if not upsert:
                    output_error(
                        f"company domain or alias already exists: {domain!r}",
                        "already_exists",
                    )
                # Canonical domain only — alias-of-other stays already_exists
                # (never move ownership, §V.147 / §V.142).
                existing = existing_before or get_company_by_domain_exact(
                    connection, domain
                )
                if existing is None:
                    output_error(
                        f"company domain or alias already exists: {domain!r}",
                        "already_exists",
                    )
                row = existing
            else:
                row = created_row
            changed: list[str] = []
            if created:
                changed.extend(["name", "domain"])
                if aliases:
                    changed.append("aliases")
            update_fields: dict[str, object] = {}
            if not created and name:
                update_fields["name"] = name
            if profile_payload is not None:
                update_fields["profile"] = profile_payload
            if update_fields:
                updated = write_company_fields(
                    connection, row.id, update_fields, commit=False
                )
                if updated is not None:
                    row = updated
                    if "name" in update_fields and "name" not in changed:
                        changed.append("name")
                    if "profile" in update_fields:
                        changed.append("profile")
            if not created:
                for alias in aliases:
                    try:
                        if (
                            add_company_alias(connection, row.id, alias, commit=False)
                            and "aliases" not in changed
                        ):
                            changed.append("aliases")
                    except ValueError as exc:
                        output_error(str(exc), "already_exists")
            for tag_row in tag_rows:
                assign_tag_to_company(
                    connection, tag_id=tag_row.id, company_id=row.id, commit=False
                )
            if tag_names:
                changed.append("tags")
            if note:
                add_company_note(connection, row.id, note, commit=False)
                changed.append("note")
            connection.commit()
            event_name = "company.create" if created else "company.upsert"
            operator_event(
                event_name,
                entity_id=row.id,
                domain=row.domain,
                created=created,
                changed=changed or ["none"],
            )
            viewed = load_company_view(connection, row.id)
            _output_company_create_entity(
                viewed if viewed is not None else row,
                created=created,
            )


def _merge_company_profile_patch(
    existing: dict[str, Any] | None,
    *,
    summary: str | None,
    products: tuple[str, ...],
    sources: tuple[str, ...],
    timezone: str | None,
    target_customers: str | None,
) -> dict[str, object]:
    """Field-merge patch flags into an existing profile (or empty base).

    Multi flags replace their list when at least one value is supplied.
    Empty ``--timezone`` clears optional timezone to null. Result is not
    validated here — ``update_company`` applies CompanyProfile validation.
    """
    base: dict[str, object] = dict(existing) if existing else {}
    if summary is not None:
        base["summary"] = summary
    if products:
        base["products"] = list(products)
    if sources:
        base["sources"] = list(sources)
    if timezone is not None:
        base["timezone"] = timezone if timezone else None
    if target_customers is not None:
        base["target_customers"] = target_customers
    return base


@company.command("update")
@click.argument("company_ref")
@click.option("--name", default=None, help="Company name.")
@_company_profile_options
def company_update(
    company_ref: str,
    name: str | None,
    profile_json: str | None,
    profile_file: str | None,
    profile: str | None,
    summary: str | None,
    product: tuple[str, ...],
    source: tuple[str, ...],
    timezone: str | None,
    target_customers: str | None,
) -> None:
    """Update a company (addressed by domain or ID).

    Profile writes: full-replace via exclusive --profile-json / --profile-file /
    --profile - (stdin), or field-patch merge via --summary / --product /
    --source / --timezone / --target-customers. Full-replace and field-patch
    are exclusive. Invalid profiles fail with validation_error and no write.
    """
    from mailpilot.database import update_company
    from mailpilot.operator_log import cli_mutation, operator_event

    replace_flags, has_patch = _profile_replace_and_patch_flags(
        profile_json,
        profile_file,
        profile,
        summary,
        product,
        source,
        timezone,
        target_customers,
    )

    with _db(mutate=True) as connection:
        before = _resolve_company(connection, company_ref)
        company_id = before.id
        fields: dict[str, object] = {}
        if name is not None:
            fields["name"] = name
        if replace_flags:
            fields["profile"] = _read_replace_profile(
                profile_json, profile_file, profile
            )
        elif has_patch:
            existing = before.profile if isinstance(before.profile, dict) else None
            fields["profile"] = _merge_company_profile_patch(
                existing,
                summary=summary,
                products=product,
                sources=source,
                timezone=timezone,
                target_customers=target_customers,
            )
        with cli_mutation("company", "update", entity_id=company_id):
            updated = update_company(connection, company_id, **fields)
            if updated is None:
                output_error(f"company not found: {company_id}", "not_found")
            changed = [
                field
                for field in ("name", "profile")
                if getattr(before, field) != getattr(updated, field)
            ]
            operator_event(
                "company.update",
                entity_id=company_id,
                changed=changed,
            )
            output_entity("company", updated)


@company.command("disable")
@click.argument("company_ref", required=False, default=None)
@click.option(
    "--reason",
    default=None,
    help="Explanation written to disabled_reason (single-entity mode).",
)
@click.option(
    "--reason-file",
    "reason_file",
    default=None,
    type=click.Path(dir_okay=False),
    help=(
        "Read disable reason from a UTF-8 file (single-entity; exclusive "
        "with --reason)."
    ),
)
@click.option(
    "--stdin",
    "from_stdin",
    is_flag=True,
    default=False,
    help=(
        "Batch mode: read NDJSON from stdin, one object per line with "
        "domain and reason. Exclusive with COMPANY_REF / --reason / "
        "--reason-file. Re-disable of an already-disabled company is an "
        "ok no-op. Exit 0 when every row is ok; exit 1 if any row errors "
        "(full results JSON still on stdout)."
    ),
)
def company_disable(
    company_ref: str | None,
    reason: str | None,
    reason_file: str | None,
    from_stdin: bool,
) -> None:
    """Soft-disable a company by writing disabled_reason.

    A disabled company is hidden from `company list` unless `--include-disabled`
    is passed. Disable is reversible -- re-enable with `company enable`.
    Single-entity mode takes ``--reason`` or ``--reason-file`` (XOR) and
    rejects an already-disabled company; ``--stdin`` batch mode treats
    re-disable as an ok no-op so a lead pass can re-run safely.
    """
    from mailpilot.database import disable_company
    from mailpilot.operator_log import cli_mutation, operator_event

    if from_stdin:
        if company_ref is not None:
            output_error(
                "--stdin is exclusive with a company positional target",
                "validation_error",
            )
        if reason is not None or reason_file is not None:
            output_error(
                "--stdin is exclusive with --reason / --reason-file "
                "(supply reason per NDJSON line)",
                "validation_error",
            )
        _run_company_disable_stdin()
        return

    if company_ref is None:
        output_error(
            "COMPANY_REF is required (or pass --stdin)",
            "validation_error",
        )
    resolved_reason = _resolve_disable_reason(reason, reason_file)
    with _db(mutate=True) as connection:
        before = _resolve_company(connection, company_ref)
        company_id = before.id
        if before.disabled_reason is not None:
            output_error(
                f"company {company_id} is already disabled "
                f"(reason: {before.disabled_reason})",
                "validation_error",
            )
        with cli_mutation("company", "disable", entity_id=company_id):
            updated = disable_company(connection, company_id, resolved_reason)
            if updated is None:
                output_error(
                    f"company {company_id} is already disabled",
                    "validation_error",
                )
            operator_event(
                "company.disable",
                entity_id=company_id,
                changed=["disabled_reason"],
            )
            output_entity("company", updated)


@company.command("enable")
@click.argument("company_ref")
def company_enable(company_ref: str) -> None:
    """Re-enable a soft-disabled company by clearing disabled_reason.

    The company reappears in the default `company list`. Enabling a company
    that is not disabled is rejected. Enabling a company whose domain is an
    alias of another company is rejected (`invalid_state`).
    """
    from mailpilot.database import enable_company
    from mailpilot.operator_log import cli_mutation, operator_event

    with _db(mutate=True) as connection:
        before = _resolve_company(connection, company_ref)
        company_id = before.id
        if before.disabled_reason is None:
            output_error(
                f"company {company_id} is not disabled",
                "validation_error",
            )
        with cli_mutation("company", "enable", entity_id=company_id):
            try:
                updated = enable_company(connection, company_id)
            except ValueError as exc:
                output_error(str(exc), "invalid_state")
            if updated is None:
                output_error(
                    f"company {company_id} is not disabled",
                    "validation_error",
                )
            operator_event(
                "company.enable",
                entity_id=company_id,
                changed=["disabled_reason"],
            )
            output_entity("company", updated)


@company.command("merge")
@click.option(
    "--from",
    "from_ref",
    required=True,
    help="Source company to absorb (domain or ID).",
)
@click.option(
    "--into",
    "into_ref",
    required=True,
    help="Survivor company (domain or ID).",
)
@click.option(
    "--move-contacts",
    is_flag=True,
    default=False,
    help="Reassign all contacts from the source company to the survivor.",
)
def company_merge(from_ref: str, into_ref: str, move_contacts: bool) -> None:
    """Absorb a source company into a survivor brand.

    Records the source domain as an alias on the survivor, soft-disables the
    source with reason `merged:into <survivor.domain>`, and optionally moves
    contacts. Source and survivor may already be disabled — no prior enable
    is required. A disabled survivor stays disabled with its existing
    reason. Re-running the same merge is an ok no-op.
    """
    from mailpilot.database import (
        get_company,
        get_company_by_domain,
        get_company_by_domain_exact,
        load_company_view,
        merge_companies,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    with _db(mutate=True) as connection:
        # Survivor resolves aliases (canonical firm). Disabled survivor is
        # allowed; merge keeps its disabled_reason (§V.143).
        into_company = _resolve_company(connection, into_ref)

        # Source: exact domain first so an already-merged alias is not treated
        # as a second live firm. UUID still resolves by id.
        original_from_domain: str | None = None
        if _looks_like_uuid(from_ref):
            from_company = get_company(connection, from_ref)
            if from_company is None:
                output_error(f"company not found: {from_ref}", "not_found")
            original_from_domain = from_company.domain
            if from_company.domain.startswith("__merged__."):
                # After merge the row keeps a tombstone domain; absorb key is
                # recovered from the survivor alias list when possible.
                original_from_domain = None
        else:
            from_company = get_company_by_domain_exact(connection, from_ref)
            if from_company is None:
                # Idempotent: --from is already an alias of the survivor.
                alias_hit = get_company_by_domain(connection, from_ref)
                if alias_hit is not None and alias_hit.id == into_company.id:
                    viewed = load_company_view(connection, into_company.id)
                    output_entity(
                        "company", viewed if viewed is not None else into_company
                    )
                    return
                output_error(f"company not found: {from_ref}", "not_found")
            original_from_domain = from_ref

        if from_company.id == into_company.id:
            output_error("cannot merge a company into itself", "invalid_state")

        with cli_mutation(
            "company",
            "merge",
            entity_id=into_company.id,
            from_id=from_company.id,
        ):
            try:
                merged = merge_companies(
                    connection,
                    from_company.id,
                    into_company.id,
                    move_contacts=move_contacts,
                    original_from_domain=original_from_domain,
                )
            except ValueError as exc:
                message = str(exc)
                code = (
                    "invalid_state"
                    if "disabled" in message or "itself" in message
                    else "validation_error"
                )
                output_error(message, code)
            if merged is None:
                output_error("merge failed: company not found", "not_found")
            operator_event(
                "company.merge",
                entity_id=merged.id,
                from_id=from_company.id,
                move_contacts=move_contacts,
                changed=["aliases", "disabled_reason"]
                + (["contacts"] if move_contacts else []),
            )
            viewed = load_company_view(connection, merged.id)
            output_entity("company", viewed if viewed is not None else merged)


@company.command("search")
@click.argument("query")
@sort_option(COMPANY_SORT_KEYS, default="name")
@desc_option
@offset_option
@limit_option(default=500)
def company_search(query: str, limit: int, offset: int, sort: str, desc: bool) -> None:
    """Search companies by name or domain.

    Lean rows match company list (domain, name, has_profile, contact_count,
    tags, disabled_reason). Default --limit is 500 for tag-sized cohorts.
    Sort keys: name (default), domain, created_at, contact_count; pass --desc
    for descending. Use --offset with --limit for pages.
    """
    from mailpilot.database import search_companies

    with _db() as connection:
        companies = search_companies(
            connection,
            query,
            limit=limit,
            offset=offset,
            sort=sort,
            desc=desc,
        )
        output({"companies": [c.model_dump(mode="json") for c in companies]})


@company.command("list")
@enum_option(
    "--status",
    "status",
    _COMPANY_PIPELINE_STATUSES,
    "Pipeline cohort: ready (profile + >=1 contact), needs_contacts "
    "(profile + 0 contacts), needs_profile (no profile), disabled "
    "(has disabled_reason; forces include). Composes with other filters.",
)
@presence_option("profile", "Filter on presence of a company profile.")
@range_options(
    "contacts",
    "Return only companies with contact_count >= N (composes with --max).",
    "Return only companies with contact_count <= N (inclusive).",
)
@tag_filter_options
@include_disabled_option
@time_window_options("created_at")
@click.option(
    "--full",
    is_flag=True,
    default=False,
    help="Embed profile.summary on each row (null when no profile).",
)
@sort_option(COMPANY_SORT_KEYS, default="name")
@desc_option
@offset_option
@limit_option(default=500)
def company_list(
    limit: int,
    offset: int,
    sort: str,
    desc: bool,
    since: str | None,
    until: str | None,
    has_profile: bool | None,
    max_contacts: int | None,
    min_contacts: int | None,
    include_disabled: bool,
    tag: tuple[str, ...],
    no_tag: tuple[str, ...],
    full: bool,
    status: str | None,
) -> None:
    """List companies as summaries.

    Lean rows project domain, name, has_profile, contact_count, tags,
    disabled_reason. Pass --full to embed profile.summary for triage without
    N company view calls.

    Default --limit is 500 (tag-cohort sized). Sort keys: name (default),
    domain, created_at, contact_count; pass --desc for descending. Use
    --offset with --limit for pages (record_count is the page length).

    --status filters a pipeline cohort: ready (profile + at least one contact,
    not disabled), needs_contacts (profile + zero contacts, not disabled),
    needs_profile (no profile, not disabled), disabled (disabled_reason set;
    overrides the default hide). Status AND-composes with --tag, --no-tag,
    --min/max-contacts, --has-profile, and --include-disabled.

    Repeatable --tag is AND (row must carry every named tag). Repeatable
    --no-tag is AND (row must carry none of the named tags).
    """
    from mailpilot.database import list_companies

    with _db() as connection:
        companies = list_companies(
            connection,
            limit=limit,
            offset=offset,
            sort=sort,
            desc=desc,
            since=since,
            until=until,
            has_profile=has_profile,
            max_contacts=max_contacts,
            min_contacts=min_contacts,
            full=full,
            status=status,
            **_company_cohort_kwargs(connection, tag, no_tag, include_disabled, status),
        )
        output({"companies": [c.model_dump(mode="json") for c in companies]})


@company.command("view")
@click.argument("company_ref")
@click.option(
    "--full",
    is_flag=True,
    default=False,
    help=(
        "Embed contacts (lean fields including tags) with existing "
        "company tags and notes. Distinct from company list --full "
        "(profile.summary only)."
    ),
)
@click.option(
    "--include-meta",
    is_flag=True,
    default=False,
    help=(
        "With --full, project verification_meta on each contact "
        "(null when unset). Lean view omits meta."
    ),
)
def company_view(company_ref: str, full: bool, include_meta: bool) -> None:
    """Show a company by domain or ID with inlined notes.

    Lean view is unchanged (profile, tags, aliases, notes). Pass --full to
    embed contacts[] (lean contact fields including tags) in the same
    envelope. Pass --include-meta with --full to project verification_meta
    on those contacts. Distinct from company list --full, which only
    embeds profile.summary.
    """
    from mailpilot.database import (
        list_company_inspect_contacts,
        load_company_view,
    )

    with _db() as connection:
        company_id = _resolve_company_id(connection, company_ref)
        found = load_company_view(connection, company_id)
        if found is None:
            output_error(f"company not found: {company_ref}", "not_found")
        if not full:
            output_entity("company", found)
            return
        payload = found.model_dump(mode="json")
        payload["contacts"] = list_company_inspect_contacts(
            connection, found.id, include_meta=include_meta
        )
        output({"company": payload})


@company.command("export")
@enum_option(
    "--status",
    "status",
    _COMPANY_PIPELINE_STATUSES,
    "Pipeline cohort filter (same rules as company list --status).",
)
@presence_option("profile", "Filter on presence of a company profile.")
@range_options(
    "contacts",
    "Return only companies with contact_count >= N (composes with --max).",
    "Return only companies with contact_count <= N (inclusive).",
)
@tag_filter_options
@include_disabled_option
@click.option(
    "--full",
    is_flag=True,
    default=False,
    help="Embed full profile object on each row (null when no profile).",
)
@click.option(
    "--format",
    "export_format",
    type=click.Choice(["jsonl"]),
    default="jsonl",
    show_default=True,
    help="Output format (jsonl only).",
)
@click.option(
    "--out",
    "out_path",
    default=None,
    type=click.Path(dir_okay=False),
    help=(
        "Write NDJSON to this path; stdout emits a JSON status envelope. "
        "Omit to stream NDJSON lines on stdout."
    ),
)
def company_export(
    has_profile: bool | None,
    max_contacts: int | None,
    min_contacts: int | None,
    include_disabled: bool,
    tag: tuple[str, ...],
    no_tag: tuple[str, ...],
    full: bool,
    status: str | None,
    export_format: str,
    out_path: str | None,
) -> None:
    """Export companies as tracker NDJSON (one company object per line).

    Stable keys: domain, name, tags, has_profile, contact_count,
    disabled_reason. Domains are lowercased; tags sorted; rows ordered by
    domain. Filters compose with the company list family. Pass --full to
    embed the full profile object (or null). With --out, write the file and
    print a company_export status envelope on stdout; without --out, stream
    NDJSON on stdout (no envelope). Empty set yields zero lines / empty file.
    Not the same as db export (full CRM snapshot).
    """
    import pathlib

    from mailpilot.database import export_companies

    del export_format  # only jsonl is accepted; Choice already enforced
    with _db() as connection:
        rows = export_companies(
            connection,
            has_profile=has_profile,
            max_contacts=max_contacts,
            min_contacts=min_contacts,
            full=full,
            status=status,
            **_company_cohort_kwargs(connection, tag, no_tag, include_disabled, status),
        )

    lines = [json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows]
    body = "\n".join(lines) + ("\n" if lines else "")
    if out_path is not None:
        pathlib.Path(out_path).write_text(body, encoding="utf-8")
        output(
            {
                "company_export": {
                    "path": out_path,
                    "format": "jsonl",
                    "record_count": len(rows),
                }
            },
            record_count=len(rows),
        )
        return
    # Stream exclusion: NDJSON body on stdout, no single-object envelope.
    if body:
        click.echo(body, nl=False)
    else:
        click.echo("", nl=False)


@company.command("import")
@click.option(
    "--from",
    "from_path",
    required=True,
    type=click.Path(dir_okay=False),
    help="Path to tracker NDJSON (one company object per line).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report domain parity only; no writes (required for now).",
)
@enum_option(
    "--status",
    "status",
    _COMPANY_PIPELINE_STATUSES,
    "Scope CRM side to this pipeline cohort (same as company list).",
)
@presence_option("profile", "Scope CRM side by profile presence.")
@range_options(
    "contacts",
    "Scope CRM side: contact_count >= N.",
    "Scope CRM side: contact_count <= N (inclusive).",
)
@tag_filter_options
@include_disabled_option
def company_import(
    from_path: str,
    dry_run: bool,
    has_profile: bool | None,
    max_contacts: int | None,
    min_contacts: int | None,
    include_disabled: bool,
    tag: tuple[str, ...],
    no_tag: tuple[str, ...],
    status: str | None,
) -> None:
    """Compare a tracker NDJSON file to CRM domains (dry-run only).

    Reads one JSON object per line; each line must carry a domain. Optional
    filters scope the CRM side the same way as company export. Report buckets:
    missing_in_crm, missing_profile, zero_contacts, disabled, extra_in_crm.
    Apply writes are not supported yet -- pass --dry-run.
    """
    import pathlib

    from mailpilot.database import company_import_diff

    if not dry_run:
        output_error(
            "company import apply is not supported; pass --dry-run",
            "validation_error",
        )

    path = pathlib.Path(from_path)
    if not path.is_file():
        output_error(f"tracker file not found: {from_path}", "not_found")

    file_domains: set[str] = set()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        output_error(f"cannot read tracker file: {exc}", "validation_error")
    for line_no, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            output_error(
                f"invalid NDJSON on line {line_no}: {exc}",
                "validation_error",
            )
        if not isinstance(obj, dict):
            output_error(
                f"invalid NDJSON on line {line_no}: expected object",
                "validation_error",
            )
        domain = obj.get("domain")
        if not isinstance(domain, str) or not domain.strip():
            output_error(
                f"invalid NDJSON on line {line_no}: missing domain",
                "validation_error",
            )
        file_domains.add(domain.strip().lower())

    with _db() as connection:
        diff = company_import_diff(
            connection,
            file_domains,
            has_profile=has_profile,
            max_contacts=max_contacts,
            min_contacts=min_contacts,
            status=status,
            **_company_cohort_kwargs(connection, tag, no_tag, include_disabled, status),
        )

    record_count = int(diff.pop("record_count"))
    output({"company_import_diff": diff}, record_count=record_count)
