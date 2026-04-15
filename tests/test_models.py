"""Tests for domain model validation."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mailpilot.models import (
    Account,
    Company,
    Contact,
    Email,
    Task,
    Workflow,
    WorkflowContact,
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


def test_workflow_contact_defaults():
    wc = WorkflowContact(
        workflow_id="w1", contact_id="c1", created_at=NOW, updated_at=NOW
    )
    assert wc.status == "pending"
    assert wc.reason == ""


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
