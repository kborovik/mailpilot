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


def test_company_jsonb_defaults():
    company = Company(
        id="1", name="Co", domain="co.com", created_at=NOW, updated_at=NOW
    )
    assert company.domain_aliases == []
    assert company.products_services == []
    assert company.locations == []


def test_contact_optional_fields():
    contact = Contact(
        id="1", email="a@b.com", domain="b.com", created_at=NOW, updated_at=NOW
    )
    assert contact.company_id is None
    assert contact.first_name is None
    assert contact.position is None


def test_workflow_type_literal():
    workflow = Workflow(
        id="1",
        name="W",
        type="outbound",
        account_id="a1",
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
            type="invalid",  # type: ignore[arg-type]
            account_id="a1",
            created_at=NOW,
            updated_at=NOW,
        )


def test_enrollment_defaults():
    enrollment = Enrollment(
        workflow_id="w1", contact_id="c1", created_at=NOW, updated_at=NOW
    )
    assert enrollment.status == "active"
    assert enrollment.reason == ""


def test_enrollment_status_literal_is_active_or_paused() -> None:
    """EnrollmentStatus collapsed to operational state only (#102)."""
    from typing import get_args

    from mailpilot.models import EnrollmentStatus

    assert set(get_args(EnrollmentStatus)) == {"active", "paused"}


def test_activity_type_literal_uses_enrollment_vocabulary() -> None:
    """workflow_* renamed to enrollment_*; pause/resume added (#102)."""
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
    }


def test_tag_uses_nullable_contact_company_fks() -> None:
    """Polymorphic entity_type/entity_id replaced with typed FKs (#102 suggestion 1)."""
    from mailpilot.models import Tag

    contact_tag = Tag(
        id="t1",
        contact_id="c1",
        company_id=None,
        name="prospect",
        created_at=NOW,
    )
    assert contact_tag.contact_id == "c1"
    assert contact_tag.company_id is None
    assert not hasattr(contact_tag, "entity_type")
    assert not hasattr(contact_tag, "entity_id")


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
        workflow_id="w1",
        contact_id="c1",
        description="follow up",
        scheduled_at=NOW,
        created_at=NOW,
    )
    assert task.status == "pending"
    assert task.completed_at is None
    assert task.context == {}
