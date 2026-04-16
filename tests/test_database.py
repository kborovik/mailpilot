"""Integration tests for database CRUD operations (real DB)."""

from typing import Any

import psycopg

from conftest import (
    make_test_account,
    make_test_company,
    make_test_contact,
    make_test_workflow,
)
from mailpilot.database import (
    create_email,
    get_account,
    get_company,
    get_contact,
    get_contact_by_email,
    get_email,
    get_email_by_gmail_message_id,
    get_emails_by_gmail_thread_id,
    get_workflow,
    list_accounts,
    list_companies,
    list_contacts,
    list_emails,
    list_workflows,
    search_companies,
    search_contacts,
    update_account,
    update_company,
    update_contact,
    update_email,
    update_workflow,
)

# -- Account -------------------------------------------------------------------


def test_create_and_get_account(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    assert account.email == "test@example.com"
    assert account.display_name == "Test Account"
    assert account.id

    fetched = get_account(database_connection, account.id)
    assert fetched is not None
    assert fetched.id == account.id
    assert fetched.email == account.email


def test_get_account_not_found(database_connection: psycopg.Connection[dict[str, Any]]):
    assert get_account(database_connection, "nonexistent") is None


def test_list_accounts(database_connection: psycopg.Connection[dict[str, Any]]):
    make_test_account(database_connection, email="a@test.com")
    make_test_account(database_connection, email="b@test.com")
    accounts = list_accounts(database_connection)
    assert len(accounts) == 2


def test_update_account(database_connection: psycopg.Connection[dict[str, Any]]):
    account = make_test_account(database_connection)
    updated = update_account(database_connection, account.id, gmail_history_id="12345")
    assert updated is not None
    assert updated.gmail_history_id == "12345"
    assert updated.updated_at > account.updated_at


def test_update_account_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    assert update_account(database_connection, "nonexistent", display_name="X") is None


# -- Company -------------------------------------------------------------------


def test_create_and_get_company(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    company = make_test_company(database_connection)
    assert company.name == "Test Corp"
    assert company.domain == "testcorp.com"

    fetched = get_company(database_connection, company.id)
    assert fetched is not None
    assert fetched.domain == "testcorp.com"


def test_list_companies(database_connection: psycopg.Connection[dict[str, Any]]):
    make_test_company(database_connection, name="Alpha", domain="alpha.com")
    make_test_company(database_connection, name="Beta", domain="beta.com")
    companies = list_companies(database_connection)
    assert len(companies) == 2
    assert companies[0].name == "Alpha"


def test_search_companies(database_connection: psycopg.Connection[dict[str, Any]]):
    make_test_company(database_connection, name="Acme Inc", domain="acme.com")
    make_test_company(database_connection, name="Beta Corp", domain="beta.com")
    results = search_companies(database_connection, "acme")
    assert len(results) == 1
    assert results[0].name == "Acme Inc"


def test_update_company(database_connection: psycopg.Connection[dict[str, Any]]):
    company = make_test_company(database_connection)
    updated = update_company(
        database_connection, company.id, name="New Name", industry="Tech"
    )
    assert updated is not None
    assert updated.name == "New Name"
    assert updated.industry == "Tech"
    assert updated.updated_at > company.updated_at


def test_update_company_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    assert update_company(database_connection, "nonexistent", name="X") is None


# -- Contact -------------------------------------------------------------------


def test_create_contact_with_company(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    company = make_test_company(database_connection)
    contact = make_test_contact(database_connection, company_id=company.id)
    assert contact.company_id == company.id

    fetched = get_contact(database_connection, contact.id)
    assert fetched is not None
    assert fetched.company_id == company.id


def test_list_contacts_by_domain(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    make_test_contact(database_connection, email="a@foo.com", domain="foo.com")
    make_test_contact(database_connection, email="b@bar.com", domain="bar.com")
    results = list_contacts(database_connection, domain="foo.com")
    assert len(results) == 1
    assert results[0].domain == "foo.com"


def test_search_contacts(database_connection: psycopg.Connection[dict[str, Any]]):
    make_test_contact(database_connection, email="alice@test.com", domain="test.com")
    make_test_contact(database_connection, email="bob@test.com", domain="test.com")
    results = search_contacts(database_connection, "alice")
    assert len(results) == 1


def test_update_contact(database_connection: psycopg.Connection[dict[str, Any]]):
    contact = make_test_contact(database_connection)
    updated = update_contact(
        database_connection, contact.id, first_name="Jane", position="CEO"
    )
    assert updated is not None
    assert updated.first_name == "Jane"
    assert updated.position == "CEO"


def test_get_contact_by_email(database_connection: psycopg.Connection[dict[str, Any]]):
    contact = make_test_contact(
        database_connection, email="alice@test.com", domain="test.com"
    )
    found = get_contact_by_email(database_connection, "alice@test.com")
    assert found is not None
    assert found.id == contact.id
    assert found.email == "alice@test.com"


def test_get_contact_by_email_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    assert get_contact_by_email(database_connection, "nobody@test.com") is None


# -- Workflow ------------------------------------------------------------------


def test_create_and_get_workflow(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    assert workflow.type == "outbound"
    assert workflow.status == "draft"
    assert workflow.account_id == account.id

    fetched = get_workflow(database_connection, workflow.id)
    assert fetched is not None
    assert fetched.name == "Test Workflow"


def test_list_workflows_by_account(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    a1 = make_test_account(database_connection, email="a@test.com")
    a2 = make_test_account(database_connection, email="b@test.com")
    make_test_workflow(database_connection, account_id=a1.id, name="W1")
    make_test_workflow(database_connection, account_id=a2.id, name="W2")
    results = list_workflows(database_connection, account_id=a1.id)
    assert len(results) == 1
    assert results[0].name == "W1"


def test_update_workflow(database_connection: psycopg.Connection[dict[str, Any]]):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    updated = update_workflow(
        database_connection, workflow.id, status="active", objective="Book demo"
    )
    assert updated is not None
    assert updated.status == "active"
    assert updated.objective == "Book demo"


def test_list_workflows_by_status(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    w1 = make_test_workflow(database_connection, account_id=account.id, name="W1")
    make_test_workflow(database_connection, account_id=account.id, name="W2")
    update_workflow(database_connection, w1.id, status="active")
    # w2 stays as draft
    active = list_workflows(database_connection, account_id=account.id, status="active")
    assert len(active) == 1
    assert active[0].name == "W1"
    drafts = list_workflows(database_connection, account_id=account.id, status="draft")
    assert len(drafts) == 1
    assert drafts[0].name == "W2"
    all_workflows = list_workflows(database_connection, account_id=account.id)
    assert len(all_workflows) == 2


# -- Email ---------------------------------------------------------------------


def test_create_and_list_emails(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Hello",
        body_text="Hi there",
        gmail_message_id="msg_123",
    )
    assert email.direction == "inbound"
    assert email.subject == "Hello"
    assert email.status == "received"
    assert email.is_routed is False

    emails = list_emails(database_connection, account_id=account.id)
    assert len(emails) == 1
    assert emails[0].id == email.id


def test_create_email_with_explicit_status(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    email = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="Outgoing",
        status="sent",
        is_routed=True,
    )
    assert email.status == "sent"
    assert email.is_routed is True


def test_get_email_by_gmail_message_id(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        gmail_message_id="msg_abc",
    )
    found = get_email_by_gmail_message_id(database_connection, "msg_abc")
    assert found is not None
    assert found.id == email.id


def test_get_email_by_gmail_message_id_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    assert get_email_by_gmail_message_id(database_connection, "nonexistent") is None


def test_get_emails_by_gmail_thread_id(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    e1 = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        gmail_message_id="msg_1",
        gmail_thread_id="thread_abc",
        subject="First",
    )
    e2 = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        gmail_message_id="msg_2",
        gmail_thread_id="thread_abc",
        subject="Reply",
        status="sent",
    )
    create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        gmail_message_id="msg_3",
        gmail_thread_id="thread_other",
        subject="Unrelated",
    )
    results = get_emails_by_gmail_thread_id(database_connection, "thread_abc")
    assert len(results) == 2
    ids = {e.id for e in results}
    assert e1.id in ids
    assert e2.id in ids


def test_get_emails_by_gmail_thread_id_empty(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    assert get_emails_by_gmail_thread_id(database_connection, "nonexistent") == []


def test_update_email(database_connection: psycopg.Connection[dict[str, Any]]):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        gmail_message_id="msg_update",
    )
    assert email.is_routed is False
    assert email.workflow_id is None

    updated = update_email(
        database_connection, email.id, is_routed=True, workflow_id=workflow.id
    )
    assert updated is not None
    assert updated.is_routed is True
    assert updated.workflow_id == workflow.id

    # Verify via get
    fetched = get_email(database_connection, email.id)
    assert fetched is not None
    assert fetched.is_routed is True


def test_update_email_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    assert update_email(database_connection, "nonexistent", status="bounced") is None
