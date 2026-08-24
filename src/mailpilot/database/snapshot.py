"""db export / import snapshot bundle (§V.121)."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import psycopg

from mailpilot.database.company import (
    add_company_alias,
    create_company,
    disable_company,
    get_company,
    get_company_by_domain,
    list_companies,
    list_company_aliases,
    update_company,
)
from mailpilot.database.contact import (
    create_contact,
    disable_contact,
    get_contact_by_email,
    list_contacts,
)
from mailpilot.database.tag import (
    assign_tag_to_company,
    assign_tag_to_contact,
    create_tag,
    disable_tag,
    get_tag_by_name,
    list_tags,
)

# -- Snapshot ------------------------------------------------------------------

_SNAPSHOT_SCHEMA_VERSION = 1
"""Format version of the `db export` / `db import` bundle (§V.121).

Distinct from the database schema hash (§V.19): this versions the JSON bundle
layout, not the live table structure. Bump it when the bundle sections change.
"""


def export_snapshot(
    connection: psycopg.Connection[dict[str, Any]],
) -> dict[str, Any]:
    """Build the database snapshot bundle (§V.121).

    Read-only. Scope is the tag vocabulary plus the company and contact tables
    only -- emails, activities, notes, workflows, enrollments, tasks, and
    accounts are excluded. Tags embed under their owner row; the vocabulary
    rides its own ``tags`` section so a disabled or unassigned tag survives the
    round-trip. Every link is a natural key (company domain, contact email, tag
    name); no source-DB UUID is forwarded, so a fresh import re-links a contact
    to its company by domain, never the exported id (carries the §B.104 lesson
    into the bundle).

    Args:
        connection: Open database connection.

    Returns:
        The bundle dict: ``schema_version``, ``exported_at``, ``tags``,
        ``companies`` (each with embedded ``tags``), and ``contacts`` (each with
        ``company_domain`` and embedded ``tags``).
    """
    vocabulary = list_tags(connection, limit=1_000_000, include_disabled=True)
    tags = [{"name": t.name, "disabled_reason": t.disabled_reason} for t in vocabulary]

    companies: list[dict[str, Any]] = []
    for summary in list_companies(connection, limit=1_000_000, include_disabled=True):
        company = get_company(connection, summary.id)
        if company is None:
            continue
        owner_tags = list_tags(
            connection,
            company_id=summary.id,
            limit=1_000_000,
            include_disabled=True,
        )
        companies.append(
            {
                "name": company.name,
                "domain": company.domain,
                "profile": company.profile,
                "disabled_reason": company.disabled_reason,
                "tags": [t.name for t in owner_tags],
                "aliases": list_company_aliases(connection, summary.id),
            }
        )

    contacts: list[dict[str, Any]] = []
    for summary in list_contacts(connection, limit=1_000_000, include_disabled=True):
        owner_tags = list_tags(
            connection,
            contact_id=summary.id,
            limit=1_000_000,
            include_disabled=True,
        )
        contacts.append(
            {
                "email": summary.email,
                "first_name": summary.first_name,
                "last_name": summary.last_name,
                "title": summary.title,
                "email_confidence": summary.email_confidence,
                "disabled_reason": summary.disabled_reason,
                "company_domain": summary.company_domain,
                "tags": [t.name for t in owner_tags],
            }
        )

    return {
        "schema_version": _SNAPSHOT_SCHEMA_VERSION,
        "exported_at": datetime.now(tz=UTC).isoformat(),
        "tags": tags,
        "companies": companies,
        "contacts": contacts,
    }


def _restore_tag_assignment(
    connection: psycopg.Connection[dict[str, Any]],
    tag_name: object,
    owner_key: str,
    errors: list[dict[str, Any]],
    company_id: str | None = None,
    contact_id: str | None = None,
) -> None:
    """Link a restored owner to a vocabulary tag by name (§V.121).

    Resolves the tag through the vocabulary -- a name absent from the vocabulary
    records a per-row error and the batch continues, never auto-creating the tag
    (§V.116 Enum-family rule). Vocabulary-first restore order guarantees a
    faithful bundle always resolves here.

    Args:
        connection: Open database connection.
        tag_name: Tag name carried by the owner row's ``tags`` list.
        owner_key: The owner's natural key (domain or email) for error reporting.
        errors: Accumulator the helper appends per-row failures onto.
        company_id: Owning company id, or ``None`` for a contact owner.
        contact_id: Owning contact id, or ``None`` for a company owner.
    """
    try:
        tag = (
            get_tag_by_name(connection, tag_name) if isinstance(tag_name, str) else None
        )
    except ValueError:
        tag = None
    if tag is None:
        errors.append(
            {
                "entity": "tag_assignment",
                "key": owner_key,
                "error": "not_found",
                "message": f"tag {tag_name!r} not in vocabulary",
            }
        )
        return
    if company_id is not None:
        assign_tag_to_company(connection, tag.id, company_id)
    else:
        assert contact_id is not None
        assign_tag_to_contact(connection, tag.id, contact_id)


def _restore_tags(
    connection: psycopg.Connection[dict[str, Any]],
    entries: Iterable[Any],
    errors: list[dict[str, Any]],
) -> int:
    """Restore the tag vocabulary, returning the count of rows restored (§V.121)."""
    restored = 0
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str):
            errors.append(
                {
                    "entity": "tag",
                    "key": "",
                    "error": "validation_error",
                    "message": "tag row missing 'name'",
                }
            )
            continue
        try:
            create_tag(connection, name)
        except ValueError as exc:
            errors.append(
                {
                    "entity": "tag",
                    "key": name,
                    "error": "validation_error",
                    "message": str(exc),
                }
            )
            continue
        reason = entry.get("disabled_reason")
        if reason:
            disable_tag(connection, name, reason)
        restored += 1
    return restored


def _restore_companies(  # noqa: C901
    connection: psycopg.Connection[dict[str, Any]],
    entries: Iterable[Any],
    errors: list[dict[str, Any]],
) -> int:
    """Restore companies (profile, disabled state, tags), returning the count."""
    restored = 0
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        domain = entry.get("domain") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not isinstance(domain, str):
            errors.append(
                {
                    "entity": "company",
                    "key": domain if isinstance(domain, str) else "",
                    "error": "validation_error",
                    "message": "company row missing 'name' or 'domain'",
                }
            )
            continue
        company = create_company(connection, name=name, domain=domain)
        if company is None:
            company = get_company_by_domain(connection, domain)
        if company is None:
            errors.append(
                {
                    "entity": "company",
                    "key": domain,
                    "error": "database_error",
                    "message": f"could not create or resolve company {domain!r}",
                }
            )
            continue
        profile = entry.get("profile")
        if profile is not None:
            update_company(connection, company.id, profile=profile)
        reason = entry.get("disabled_reason")
        if reason:
            disable_company(connection, company.id, reason)
        for tag_name in entry.get("tags", []):
            _restore_tag_assignment(
                connection, tag_name, domain, errors, company_id=company.id
            )
        for alias in entry.get("aliases", []):
            if not isinstance(alias, str):
                errors.append(
                    {
                        "entity": "company_alias",
                        "key": domain,
                        "error": "validation_error",
                        "message": f"alias must be a string for company {domain!r}",
                    }
                )
                continue
            try:
                add_company_alias(connection, company.id, alias, commit=False)
            except ValueError as exc:
                errors.append(
                    {
                        "entity": "company_alias",
                        "key": domain,
                        "error": "already_exists",
                        "message": str(exc),
                    }
                )
                continue
        connection.commit()
        restored += 1
    return restored


def _restore_contacts(
    connection: psycopg.Connection[dict[str, Any]],
    entries: Iterable[Any],
    errors: list[dict[str, Any]],
) -> int:
    """Restore contacts re-linked to their company by domain (§B.104 lesson)."""
    restored = 0
    for entry in entries:
        email = entry.get("email") if isinstance(entry, dict) else None
        if not isinstance(email, str):
            errors.append(
                {
                    "entity": "contact",
                    "key": "",
                    "error": "validation_error",
                    "message": "contact row missing 'email'",
                }
            )
            continue
        company_domain = entry.get("company_domain")
        company_id: str | None = None
        if company_domain is not None:
            owner = get_company_by_domain(connection, company_domain)
            if owner is None:
                errors.append(
                    {
                        "entity": "contact",
                        "key": email,
                        "error": "foreign_key_violation",
                        "message": (
                            f"company domain {company_domain!r} not found for "
                            f"contact {email!r}"
                        ),
                    }
                )
                continue
            company_id = owner.id
        contact = create_contact(
            connection,
            email=email,
            first_name=entry.get("first_name"),
            last_name=entry.get("last_name"),
            company_id=company_id,
            title=entry.get("title"),
            email_confidence=entry.get("email_confidence"),
        )
        if contact is None:
            contact = get_contact_by_email(connection, email)
        if contact is None:
            errors.append(
                {
                    "entity": "contact",
                    "key": email,
                    "error": "database_error",
                    "message": f"could not create or resolve contact {email!r}",
                }
            )
            continue
        reason = entry.get("disabled_reason")
        if reason:
            disable_contact(connection, contact.id, reason)
        for tag_name in entry.get("tags", []):
            _restore_tag_assignment(
                connection, tag_name, email, errors, contact_id=contact.id
            )
        restored += 1
    return restored


def import_snapshot(
    connection: psycopg.Connection[dict[str, Any]],
    bundle: dict[str, Any],
) -> dict[str, Any]:
    """Restore a snapshot bundle in dependency order (§V.121).

    Restore order is fixed: tag vocabulary first, then companies, then contacts.
    Vocabulary-first lets each assignment resolve by name without auto-create
    (§V.116 forbids tag auto-create). Every link resolves by natural key --
    company domain, contact email, tag name; a contact re-links to its company
    by the bundle's ``company_domain``, never a source-DB id (the §B.104
    lesson). A row that cannot resolve its foreign key records a per-row error
    entry and the batch continues -- never a batch-aborting raise.

    Args:
        connection: Open database connection.
        bundle: The snapshot bundle dict (see ``export_snapshot``).

    Returns:
        A result dict: ``tags`` / ``companies`` / ``contacts`` counts of rows
        restored, plus an ``errors`` list of per-row ``{entity, key, error,
        message}`` failures.
    """
    errors: list[dict[str, Any]] = []
    tags_restored = _restore_tags(connection, bundle.get("tags", []), errors)
    companies_restored = _restore_companies(
        connection, bundle.get("companies", []), errors
    )
    contacts_restored = _restore_contacts(
        connection, bundle.get("contacts", []), errors
    )
    return {
        "tags": tags_restored,
        "companies": companies_restored,
        "contacts": contacts_restored,
        "errors": errors,
    }
