"""Tag commands."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

import click

from mailpilot._filters import (
    include_disabled_option,
    limit_option,
    scope_option,
    time_window_options,
)
from mailpilot.cli._helpers import (
    _batch_error,
    _batch_ok,
    _emit_batch_results,
)
from mailpilot.cli.main import (
    _db,
    _resolve_company,
    _resolve_contact,
    _resolve_tag,
    main,
    output,
    output_entity,
    output_error,
)


def _tag_link_owners(
    verb: Literal["add", "remove"],
    tag_name: str,
    contact_emails: tuple[str, ...],
    company_domains: tuple[str, ...],
) -> None:
    """Link or unlink a tag on one or more owners of a single kind (§V.141).

    Owner-kind XOR: companies or contacts, not both, at least one.
    N=1 emits ``tag_assignment``; N>1 emits ``results``.
    """
    from mailpilot.database import (
        assign_tag_to_company,
        assign_tag_to_contact,
        remove_tag_from_company,
        remove_tag_from_contact,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    has_contacts = len(contact_emails) > 0
    has_companies = len(company_domains) > 0
    if has_contacts == has_companies:
        output_error(
            "pass --contact-email or --company-domain, not both"
            if has_contacts
            else "at least one --contact-email or --company-domain is required",
            "validation_error",
        )
    writer: Callable[..., Any]
    if has_contacts:
        owner_kind: Literal["contact", "company"] = "contact"
        owner_refs = contact_emails
        writer = assign_tag_to_contact if verb == "add" else remove_tag_from_contact
        owner_kw = "contact_id"
    else:
        owner_kind = "company"
        owner_refs = company_domains
        writer = assign_tag_to_company if verb == "add" else remove_tag_from_company
        owner_kw = "company_id"
    already_code, already_phrase = (
        ("already_exists", "already on") if verb == "add" else ("not_found", "not on")
    )

    with _db(mutate=True) as connection:
        tag_row = _resolve_tag(connection, tag_name)
        if len(owner_refs) == 1:
            ref = owner_refs[0]
            if owner_kind == "contact":
                owner_id = _resolve_contact(connection, ref).id
            else:
                owner_id = _resolve_company(connection, ref).id
            with cli_mutation(
                "tag",
                verb,
                name=tag_row.name,
                owner_type=owner_kind,
                owner_id=owner_id,
            ):
                linked = writer(connection, tag_id=tag_row.id, **{owner_kw: owner_id})
                if linked is None:
                    output_error(
                        f"tag '{tag_row.name}' {already_phrase} "
                        f"{owner_kind} {owner_id}",
                        already_code,
                    )
                operator_event(
                    f"tag.{verb}",
                    name=tag_row.name,
                    owner_type=owner_kind,
                    owner_id=owner_id,
                    changed=["tag_id"],
                )
                output_entity("tag_assignment", linked)
            return

        with cli_mutation(
            "tag",
            verb,
            name=tag_row.name,
            owner_type=owner_kind,
            owner_count=len(owner_refs),
        ):
            results: list[dict[str, object]] = []
            for ref in owner_refs:
                if owner_kind == "contact":
                    owner = _resolve_contact(connection, ref, missing="none")
                else:
                    owner = _resolve_company(connection, ref, missing="none")
                if owner is None:
                    results.append(
                        _batch_error(
                            ref,
                            "not_found",
                            f"{owner_kind} not found: {ref}",
                        )
                    )
                    continue
                linked = writer(connection, tag_id=tag_row.id, **{owner_kw: owner.id})
                if linked is not None:
                    operator_event(
                        f"tag.{verb}",
                        name=tag_row.name,
                        owner_type=owner_kind,
                        owner_id=owner.id,
                        changed=["tag_id"],
                    )
                results.append(_batch_ok(ref))
            _emit_batch_results(results)


# -- Tag commands --------------------------------------------------------------


@main.group()
def tag() -> None:
    """Manage the controlled tag vocabulary and its assignments."""


@tag.command("create")
@click.argument("name")
def tag_create(name: str) -> None:
    """Define a tag in the controlled vocabulary.

    Creates a vocabulary entry; linking owners to it is the `tag add` verb. A
    name already defined is rejected (names are globally unique).
    """
    from mailpilot.database import (
        _normalize_tag_name,  # pyright: ignore[reportPrivateUsage]
        create_tag,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    if not name.strip():
        output_error("tag name cannot be empty", "validation_error")
    with _db(mutate=True) as connection, cli_mutation("tag", "create", name=name):
        try:
            created = create_tag(connection, name=name)
        except ValueError as exc:
            output_error(str(exc), "validation_error")
        if created is None:
            normalized = _normalize_tag_name(name)
            output_error(f"tag '{normalized}' already exists", "already_exists")
        operator_event("tag.create", name=created.name, changed=["name"])
        output_entity("tag", created)


@tag.command("view")
@click.argument("name")
def tag_view(name: str) -> None:
    """Show a vocabulary tag by name with its usage_count."""
    from mailpilot.database import get_tag_summary_by_name

    with _db() as connection:
        try:
            found = get_tag_summary_by_name(connection, name)
        except ValueError:
            found = None
        if found is None:
            output_error(f"tag not found: {name}", "not_found")
        output_entity("tag", found)


@tag.command("disable")
@click.argument("name")
@click.option(
    "--reason",
    required=True,
    help="Explanation written to disabled_reason.",
)
def tag_disable(name: str, reason: str) -> None:
    """Soft-retire a tag in the controlled vocabulary.

    Flips disabled_reason on the vocabulary row, so the tag drops out of the
    default `tag list` but stays linked to its owners. Re-enable by clearing
    disabled_reason. Disabling an already-disabled tag is rejected.
    """
    from mailpilot.database import disable_tag
    from mailpilot.operator_log import cli_mutation, operator_event

    if reason.strip() == "":
        output_error("reason cannot be empty", "validation_error")
    with _db(mutate=True) as connection:
        before = _resolve_tag(connection, name)
        if before.disabled_reason is not None:
            output_error(
                f"tag '{before.name}' is already disabled "
                f"(reason: {before.disabled_reason})",
                "validation_error",
            )
        with cli_mutation("tag", "disable", entity_id=before.name):
            updated = disable_tag(connection, name=before.name, reason=reason)
            if updated is None:
                output_error(
                    f"tag '{before.name}' is already disabled",
                    "validation_error",
                )
            operator_event(
                "tag.disable",
                entity_id=updated.name,
                changed=["disabled_reason"],
            )
            output_entity("tag", updated)


@tag.command("enable")
@click.argument("name")
def tag_enable(name: str) -> None:
    """Re-enable a retired tag by clearing disabled_reason.

    The tag reappears in the default `tag list`. Enabling a tag that is not
    disabled is rejected.
    """
    from mailpilot.database import enable_tag
    from mailpilot.operator_log import cli_mutation, operator_event

    with _db(mutate=True) as connection:
        before = _resolve_tag(connection, name)
        if before.disabled_reason is None:
            output_error(
                f"tag '{before.name}' is not disabled",
                "validation_error",
            )
        with cli_mutation("tag", "enable", entity_id=before.name):
            updated = enable_tag(connection, name=before.name)
            if updated is None:
                output_error(
                    f"tag '{before.name}' is not disabled",
                    "validation_error",
                )
            operator_event(
                "tag.enable",
                entity_id=updated.name,
                changed=["disabled_reason"],
            )
            output_entity("tag", updated)


@tag.command("add")
@click.option(
    "--tag", "tag_name", required=True, help="Defined tag to link (name or ID)."
)
@click.option(
    "--contact-email",
    "contact_emails",
    multiple=True,
    help="Owner contact (email or ID); repeatable. XOR with --company-domain.",
)
@click.option(
    "--company-domain",
    "company_domains",
    multiple=True,
    help="Owner company (domain or ID); repeatable. XOR with --contact-email.",
)
def tag_add(
    tag_name: str,
    contact_emails: tuple[str, ...],
    company_domains: tuple[str, ...],
) -> None:
    """Link a defined tag to one or more contacts or companies.

    Pass repeatable ``--company-domain`` or repeatable ``--contact-email``
    (owner-kind XOR, at least one owner). One owner returns a
    ``tag_assignment`` entity envelope; multiple owners return a ``results``
    batch envelope (already-linked rows are ok skips). Errors ``not_found``
    when the tag is undefined -- never creates the tag as a side effect
    (define vocabulary with ``tag create``).
    """
    _tag_link_owners("add", tag_name, contact_emails, company_domains)


@tag.command("set")
@click.option(
    "--contact-email",
    default=None,
    help="Owner contact (email or ID). XOR with --company-domain.",
)
@click.option(
    "--company-domain",
    default=None,
    help="Owner company (domain or ID). XOR with --contact-email.",
)
@click.option(
    "--tags",
    "tags_csv",
    required=True,
    help="Comma-separated defined tag names; empty string clears all assignments.",
)
def tag_set(
    contact_email: str | None,
    company_domain: str | None,
    tags_csv: str,
) -> None:
    """Replace an owner's full tag assignment set.

    Pass exactly one of ``--company-domain`` or ``--contact-email`` plus
    ``--tags a,b,c``. Empty ``--tags`` clears every assignment. Undefined
    names error ``not_found`` with zero writes. Company success returns the
    company entity (including final ``tags``); contact success returns the
    contact entity. Vocabulary is never auto-created -- define names with
    ``tag create`` first.
    """
    from mailpilot.database import (
        get_tag_by_name,
        load_company_view,
        set_company_tags,
        set_contact_tags,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    if (contact_email is None) == (company_domain is None):
        output_error(
            "exactly one of --contact-email or --company-domain is required",
            "validation_error",
        )
    raw_names = [part.strip() for part in tags_csv.split(",")]
    tag_names = [name for name in raw_names if name]
    # Empty CSV ("") or whitespace-only is a clear; bare commas with no names too.
    with _db(mutate=True) as connection:
        tag_ids: list[str] = []
        for name in tag_names:
            try:
                tag_row = get_tag_by_name(connection, name)
            except ValueError:
                tag_row = None
            if tag_row is None:
                output_error(f"tag not found: {name}", "not_found")
            tag_ids.append(tag_row.id)
        if company_domain is not None:
            company = _resolve_company(connection, company_domain)
            with cli_mutation(
                "tag",
                "set",
                owner_type="company",
                owner_id=company.id,
                tag_count=len(tag_ids),
            ):
                final_names = set_company_tags(
                    connection, company_id=company.id, tag_ids=tag_ids
                )
                operator_event(
                    "tag.set",
                    owner_type="company",
                    owner_id=company.id,
                    changed=["tags"],
                    tags=",".join(final_names),
                )
                view = load_company_view(connection, company.id)
                if view is None:
                    output_error(f"company not found: {company.id}", "not_found")
                output_entity("company", view)
        else:
            assert contact_email is not None
            contact = _resolve_contact(connection, contact_email)
            with cli_mutation(
                "tag",
                "set",
                owner_type="contact",
                owner_id=contact.id,
                tag_count=len(tag_ids),
            ):
                final_names = set_contact_tags(
                    connection, contact_id=contact.id, tag_ids=tag_ids
                )
                operator_event(
                    "tag.set",
                    owner_type="contact",
                    owner_id=contact.id,
                    changed=["tags"],
                    tags=",".join(final_names),
                )
                output_entity("contact", contact)


@tag.command("remove")
@click.option(
    "--tag", "tag_name", required=True, help="Defined tag to unlink (name or ID)."
)
@click.option(
    "--contact-email",
    "contact_emails",
    multiple=True,
    help="Owner contact (email or ID); repeatable. XOR with --company-domain.",
)
@click.option(
    "--company-domain",
    "company_domains",
    multiple=True,
    help="Owner company (domain or ID); repeatable. XOR with --contact-email.",
)
def tag_remove(
    tag_name: str,
    contact_emails: tuple[str, ...],
    company_domains: tuple[str, ...],
) -> None:
    """Unlink a defined tag from one or more contacts or companies.

    Pass repeatable ``--company-domain`` or repeatable ``--contact-email``
    (owner-kind XOR, at least one owner). One owner returns a
    ``tag_assignment`` entity envelope; multiple owners return a ``results``
    batch envelope (already-unlinked rows are ok skips). Removes only the
    link; the tag vocabulary entry and the owners both survive.
    """
    _tag_link_owners("remove", tag_name, contact_emails, company_domains)


@tag.command("list")
@scope_option(
    "--contact-email", "contact_email", "List one contact's tags (email or ID)."
)
@scope_option(
    "--company-domain", "company_domain", "List one company's tags (domain or ID)."
)
@include_disabled_option
@time_window_options("created_at")
@limit_option
def tag_list(
    contact_email: str | None,
    company_domain: str | None,
    limit: int,
    since: str | None,
    until: str | None,
    include_disabled: bool,
) -> None:
    """List the tag vocabulary with usage_count, or one owner's tags.

    With no owner option, lists the whole vocabulary (each row carrying its
    global usage_count). With --contact-email or --company-domain, lists the
    tags assigned to that owner.
    """
    from mailpilot.database import list_tags

    if contact_email is not None and company_domain is not None:
        output_error(
            "pass at most one of --contact-email or --company-domain",
            "validation_error",
        )
    with _db() as connection:
        contact_id = (
            _resolve_contact(connection, contact_email).id
            if contact_email is not None
            else None
        )
        company_id = (
            _resolve_company(connection, company_domain).id
            if company_domain is not None
            else None
        )
        tags = list_tags(
            connection,
            contact_id=contact_id,
            company_id=company_id,
            limit=limit,
            since=since,
            until=until,
            include_disabled=include_disabled,
        )
        output({"tags": [t.model_dump(mode="json") for t in tags]})


@tag.command("search")
@click.argument("name")
@include_disabled_option
@limit_option
def tag_search(name: str, limit: int, include_disabled: bool) -> None:
    """Search the tag vocabulary by name substring."""
    from mailpilot.database import search_tags

    with _db() as connection:
        tags = search_tags(
            connection,
            name=name,
            limit=limit,
            include_disabled=include_disabled,
        )
        output({"tags": [t.model_dump(mode="json") for t in tags]})
