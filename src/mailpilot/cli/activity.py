"""Activity commands."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json

import click

from mailpilot._filters import (
    enum_option,
    limit_option,
    scope_option,
    time_window_options,
)
from mailpilot.cli.main import (
    _db,
    _resolve_company,
    _resolve_contact,
    _resolve_workflow_id,
    main,
    output,
    output_entity,
    output_error,
)

_ACTIVITY_TYPES = [
    "email_sent",
    "email_received",
    "note_added",
    "tag_added",
    "tag_removed",
    "status_changed",
    "enrollment_added",
    "enrollment_completed",
    "enrollment_failed",
    "enrollment_paused",
    "enrollment_resumed",
    "enrollment_disabled",
    "enrollment_enabled",
]

# -- Activity commands ---------------------------------------------------------


@main.group()
def activity() -> None:
    """Manage activity timeline events."""


@activity.command("add")
@click.option("--contact-email", default=None, help="Owner contact (email or ID).")
@click.option("--company-domain", default=None, help="Owner company (domain or ID).")
@click.option(
    "--type",
    "activity_type",
    required=True,
    type=click.Choice(_ACTIVITY_TYPES),
    help="Activity type.",
)
@click.option("--summary", required=True, help="One-line description.")
@click.option("--detail", default=None, help="JSON detail payload.")
def activity_add(
    contact_email: str | None,
    company_domain: str | None,
    activity_type: str,
    summary: str,
    detail: str | None,
) -> None:
    """Attach an activity event to a contact or company.

    At least one of --contact-email / --company-domain is required.
    """
    from mailpilot.database import (
        create_activity,
    )

    if not summary.strip():
        output_error("summary cannot be empty", "validation_error")
    if contact_email is None and company_domain is None:
        output_error(
            "at least one of --contact-email or --company-domain is required",
            "validation_error",
        )
    detail_dict: dict[str, object] = json.loads(detail) if detail else {}
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
        created = create_activity(
            connection,
            contact_id=contact_id,
            company_id=company_id,
            activity_type=activity_type,
            summary=summary,
            detail=detail_dict,
        )
        output_entity("activity", created)


@activity.command("list")
@scope_option("--contact-email", "contact_email", "Filter by contact (email or ID).")
@scope_option("--company-domain", "company_domain", "Filter by company (domain or ID).")
@scope_option("--workflow-id", "workflow_id", "Filter by workflow (name or ID).")
@enum_option("--type", "activity_type", _ACTIVITY_TYPES, "Filter by activity type.")
@time_window_options("created_at")
@limit_option
def activity_list(
    contact_email: str | None,
    company_domain: str | None,
    workflow_id: str | None,
    activity_type: str | None,
    limit: int,
    since: str | None,
    until: str | None,
) -> None:
    """List activities (requires contact, company, or workflow scope)."""
    from mailpilot.database import (
        list_activities,
    )

    if contact_email is None and company_domain is None and workflow_id is None:
        output_error(
            "at least one of --contact-email, --company-domain, or --workflow-id "
            "is required",
            "missing_filter",
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
        resolved_workflow_id: str | None = (
            _resolve_workflow_id(connection, workflow_id)
            if workflow_id is not None
            else None
        )
        activities = list_activities(
            connection,
            contact_id=contact_id,
            company_id=company_id,
            activity_type=activity_type,
            limit=limit,
            since=since,
            until=until,
            workflow_id=resolved_workflow_id,
        )
        output({"activities": [a.model_dump(mode="json") for a in activities]})
