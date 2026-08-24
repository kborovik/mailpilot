"""Note commands."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import click

from mailpilot._filters import (
    limit_option,
    scope_option,
    time_window_options,
)
from mailpilot.cli.main import (
    _db,
    _resolve_company,
    _resolve_contact,
    main,
    output,
    output_entity,
    output_error,
)

# -- Note commands -------------------------------------------------------------


@main.group()
def note() -> None:
    """Manage notes on contacts and companies."""


@note.command("add")
@click.option("--contact-email", default=None, help="Owner contact (email or ID).")
@click.option("--company-domain", default=None, help="Owner company (domain or ID).")
@click.option("--body", required=True, help="Note text.")
def note_add(contact_email: str | None, company_domain: str | None, body: str) -> None:
    """Add a note to a contact or company."""
    from mailpilot.database import (
        add_company_note,
        add_contact_note,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    if not body.strip():
        output_error("note body cannot be empty", "validation_error")
    if (contact_email is None) == (company_domain is None):
        output_error(
            "exactly one of --contact-email or --company-domain is required",
            "validation_error",
        )
    with _db(mutate=True) as connection:
        if contact_email is not None:
            owner = ("contact", _resolve_contact(connection, contact_email).id)
        else:
            assert company_domain is not None
            owner = ("company", _resolve_company(connection, company_domain).id)
        with cli_mutation("note", "add", owner_type=owner[0], owner_id=owner[1]):
            if owner[0] == "contact":
                created = add_contact_note(connection, contact_id=owner[1], body=body)
            else:
                created = add_company_note(connection, company_id=owner[1], body=body)
            operator_event(
                "note.add",
                entity_id=created.id,
                owner_type=owner[0],
                owner_id=owner[1],
                changed=["body"],
            )
            output_entity("note", created)


@note.command("remove")
@click.argument("note_id", required=False, default=None)
@click.option("--contact-email", default=None, help="Bulk: all notes on contact.")
@click.option("--company-domain", default=None, help="Bulk: all notes on company.")
@click.option(
    "--yes",
    "confirmed",
    is_flag=True,
    default=False,
    help="Required for owner bulk remove (confirmation gate).",
)
def note_remove(
    note_id: str | None,
    contact_email: str | None,
    company_domain: str | None,
    confirmed: bool,
) -> None:
    """Delete one note by ID, or all notes on an owner with --yes.

    Single-id: ``note remove <note_id>``. Owner bulk: exactly one of
    ``--contact-email`` / ``--company-domain`` plus required ``--yes``.
    Deletes note rows only; the note_added activity trail stays intact.
    Operator-only -- the agent never deletes notes.
    """
    # Dual-mode hard-delete per §V.14: single-id or owner bulk with --yes.
    # Activity trail stays append-only. Operator-only, never an agent tool.
    from mailpilot.database import (
        delete_note,
        delete_notes,
        get_note,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    has_owner = contact_email is not None or company_domain is not None
    if note_id is not None and has_owner:
        output_error(
            "NOTE_ID is exclusive with --contact-email / --company-domain",
            "validation_error",
        )
    if note_id is None and not has_owner:
        output_error(
            "NOTE_ID or --contact-email / --company-domain is required",
            "validation_error",
        )
    if has_owner:
        if (contact_email is None) == (company_domain is None):
            output_error(
                "exactly one of --contact-email or --company-domain is required",
                "validation_error",
            )
        if not confirmed:
            output_error(
                "owner bulk remove requires --yes",
                "validation_error",
            )

    with _db(mutate=True) as connection:
        if note_id is not None:
            found = get_note(connection, note_id)
            if found is None:
                output_error(f"note {note_id} not found", "not_found")
            with cli_mutation("note", "remove", entity_id=note_id):
                delete_note(connection, note_id)
                operator_event("note.remove", entity_id=note_id)
                output_entity("note", found)
            return

        if contact_email is not None:
            owner = _resolve_contact(connection, contact_email)
            owner_key: dict[str, object] = {"contact_email": owner.email}
            owner_kwargs: dict[str, str] = {"contact_id": owner.id}
            mutation_attrs: dict[str, object] = {
                "owner_type": "contact",
                "owner_id": owner.id,
            }
        else:
            assert company_domain is not None
            company = _resolve_company(connection, company_domain)
            owner_key = {"company_domain": company.domain}
            owner_kwargs = {"company_id": company.id}
            mutation_attrs = {
                "owner_type": "company",
                "owner_id": company.id,
            }
        with cli_mutation("note", "remove", **mutation_attrs):
            note_ids = delete_notes(connection, **owner_kwargs)
            operator_event(
                "note.remove",
                removed_count=len(note_ids),
                **mutation_attrs,
            )
            output(
                {
                    "notes_removed": {
                        "owner": owner_key,
                        "removed_count": len(note_ids),
                        "note_ids": note_ids,
                    }
                },
                record_count=len(note_ids),
            )


@note.command("list")
@scope_option("--contact-email", "contact_email", "Filter by contact (email or ID).")
@scope_option("--company-domain", "company_domain", "Filter by company (domain or ID).")
@time_window_options("created_at")
@limit_option
def note_list(
    contact_email: str | None,
    company_domain: str | None,
    limit: int,
    since: str | None,
    until: str | None,
) -> None:
    """List notes on a contact or company."""
    from mailpilot.database import (
        list_notes,
    )

    if (contact_email is None) == (company_domain is None):
        output_error(
            "exactly one of --contact-email or --company-domain is required",
            "validation_error",
        )
    with _db() as connection:
        if contact_email is not None:
            notes = list_notes(
                connection,
                contact_id=_resolve_contact(connection, contact_email).id,
                limit=limit,
                since=since,
                until=until,
            )
        else:
            assert company_domain is not None
            notes = list_notes(
                connection,
                company_id=_resolve_company(connection, company_domain).id,
                limit=limit,
                since=since,
                until=until,
            )
        output({"notes": [n.model_dump(mode="json") for n in notes]})


@note.command("view")
@click.argument("note_id")
def note_view(note_id: str) -> None:
    """View a note by ID."""
    from mailpilot.database import get_note

    with _db() as connection:
        found = get_note(connection, note_id)
        if found is None:
            output_error(f"note {note_id} not found", "not_found")
        output_entity("note", found)
