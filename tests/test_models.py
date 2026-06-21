"""Tests for domain model validation."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mailpilot.models import (
    Account,
    Company,
    Contact,
    Email,
    Enrollment,
    Task,
    Workflow,
)

NOW = datetime.now(tz=UTC)


def test_account_required_fields():
    account = Account(id="1", email="a@b.com", created_at=NOW, updated_at=NOW)
    assert account.email == "a@b.com"
    assert account.display_name == ""
    assert account.gmail_history_id is None


def test_account_missing_required_raises():
    with pytest.raises(ValidationError):
        Account(id="1", created_at=NOW, updated_at=NOW)  # type: ignore[call-arg]


def test_company_identity_fields():
    company = Company(
        id="1", name="Co", domain="co.com", created_at=NOW, updated_at=NOW
    )
    assert company.name == "Co"
    assert company.domain == "co.com"
    assert not hasattr(company, "domain_aliases")
    assert not hasattr(company, "industry")


def test_contact_optional_fields():
    contact = Contact(id="1", email="a@b.com", created_at=NOW, updated_at=NOW)
    assert contact.company_id is None
    assert contact.first_name is None
    assert contact.disabled_reason is None
    assert not hasattr(contact, "domain")
    assert not hasattr(contact, "position")
    assert not hasattr(contact, "status")


def test_workflow_type_literal():
    workflow = Workflow(
        id="1",
        name="W",
        template="outbound-general",
        type="outbound",
        account_id="a1",
        account_email="a1@example.com",
        created_at=NOW,
        updated_at=NOW,
    )
    assert workflow.type == "outbound"
    assert workflow.status == "draft"


def test_workflow_invalid_type_raises():
    with pytest.raises(ValidationError):
        Workflow(
            id="1",
            name="W",
            template="outbound-general",
            type="invalid",  # type: ignore[arg-type]
            account_id="a1",
            account_email="a1@example.com",
            created_at=NOW,
            updated_at=NOW,
        )


def test_enrollment_defaults():
    enrollment = Enrollment(
        id="e1",
        workflow_id="w1",
        workflow_name="Outbound Campaign",
        contact_id="c1",
        contact_email="c1@example.com",
        contact_name="C One",
        created_at=NOW,
        updated_at=NOW,
    )
    assert enrollment.status == "active"
    assert enrollment.reason == ""


def test_enrollment_status_literal_collapsed_to_active_disabled() -> None:
    """EnrollmentStatus collapsed to {active, disabled}; `paused` dropped (§V.15)."""
    from typing import get_args

    from mailpilot.models import EnrollmentStatus

    assert set(get_args(EnrollmentStatus)) == {"active", "disabled"}


def test_activity_type_literal_uses_enrollment_vocabulary() -> None:
    """enrollment_enabled added; paused/resumed retained for historical rows.

    `tag_disabled` is gone -- vocabulary-tag disable/enable write no activity
    (a tag row owns no contact/company, so an activity cannot target it), so
    the value was never emitted and carries no historical row (T170)."""
    from typing import get_args

    from mailpilot.models import ActivityType

    assert set(get_args(ActivityType)) == {
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
    }


def test_tag_is_owner_free_vocabulary_row() -> None:
    """§V.116: a `Tag` is a vocabulary entry -- name + disabled_reason only, no
    per-owner FKs (owners link via `TagAssignment`)."""
    from mailpilot.models import Tag

    tag = Tag(id="t1", name="prospect", created_at=NOW)
    assert tag.name == "prospect"
    assert tag.disabled_reason is None
    assert not hasattr(tag, "contact_id")
    assert not hasattr(tag, "company_id")


def test_tag_summary_carries_usage_count() -> None:
    """§V.116: `TagSummary` projects usage_count for list/view."""
    from mailpilot.models import TagSummary

    summary = TagSummary(id="t1", name="vip", usage_count=3, created_at=NOW)
    assert summary.usage_count == 3
    assert summary.disabled_reason is None


def test_tag_assignment_links_one_owner() -> None:
    """§V.116: `TagAssignment` binds a vocabulary tag to one owner (XOR)."""
    from mailpilot.models import TagAssignment

    assignment = TagAssignment(
        id="a1", tag_id="t1", contact_id="c1", company_id=None, created_at=NOW
    )
    assert assignment.tag_id == "t1"
    assert assignment.contact_id == "c1"
    assert assignment.company_id is None


def test_note_uses_nullable_contact_company_fks() -> None:
    """Polymorphic entity_type/entity_id replaced with typed FKs (#102 suggestion 1)."""
    from mailpilot.models import Note

    note = Note(
        id="n1",
        company_id="co1",
        contact_id=None,
        body="Met at conf",
        created_at=NOW,
    )
    assert note.company_id == "co1"
    assert note.contact_id is None


def test_activity_supports_company_only_and_structured_fks() -> None:
    """contact_id is nullable; email_id/workflow_id/task_id added (#102 suggestions 2, 5)."""
    from mailpilot.models import Activity

    company_activity = Activity(
        id="a1",
        contact_id=None,
        company_id="co1",
        type="note_added",
        summary="Company note",
        detail={},
        created_at=NOW,
    )
    assert company_activity.contact_id is None
    assert company_activity.company_id == "co1"

    email_activity = Activity(
        id="a2",
        contact_id="c1",
        company_id=None,
        email_id="e1",
        workflow_id="wf1",
        type="email_sent",
        summary="Subject",
        detail={},
        created_at=NOW,
    )
    assert email_activity.email_id == "e1"
    assert email_activity.workflow_id == "wf1"
    assert email_activity.task_id is None


def test_email_direction_literal():
    email = Email(id="1", account_id="a1", direction="inbound", created_at=NOW)
    assert email.direction == "inbound"
    assert email.is_routed is False


def test_email_invalid_direction_raises():
    with pytest.raises(ValidationError):
        Email(id="1", account_id="a1", direction="sideways", created_at=NOW)  # type: ignore[arg-type]


def test_task_defaults():
    task = Task(
        id="1",
        enrollment_id="e1",
        workflow_id="w1",
        contact_id="c1",
        description="follow up",
        scheduled_at=NOW,
        created_at=NOW,
    )
    assert task.status == "pending"
    assert task.completed_at is None
    assert task.context == {}
