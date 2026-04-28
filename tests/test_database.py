"""Integration tests for database CRUD operations (real DB)."""

import threading
from datetime import UTC
from typing import Any, cast

import psycopg
import pytest
from psycopg.rows import dict_row

from conftest import (
    TEST_DATABASE_URL,
    make_test_account,
    make_test_activity,
    make_test_company,
    make_test_contact,
    make_test_note,
    make_test_tag,
    make_test_workflow,
)
from mailpilot.database import (
    activate_workflow,
    cancel_task,
    complete_task,
    create_activity,
    create_contacts_bulk,
    create_email,
    create_enrollment,
    create_or_get_contact_by_email,
    create_tag,
    create_task,
    create_tasks_for_routed_emails,
    delete_enrollment,
    delete_tag,
    get_account,
    get_account_by_email,
    get_company,
    get_company_by_domain,
    get_contact,
    get_contact_by_email,
    get_contacts_by_emails,
    get_email,
    get_email_by_gmail_message_id,
    get_emails_by_gmail_thread_id,
    get_enrollment,
    get_last_cold_outbound,
    get_latest_email_in_thread,
    get_note,
    get_status_counts,
    get_unprocessed_inbound_email,
    get_workflow,
    list_accounts,
    list_activities,
    list_companies,
    list_contacts,
    list_emails,
    list_enrollments_detailed,
    list_notes,
    list_tags,
    list_tasks,
    list_workflows,
    pause_workflow,
    search_companies,
    search_contacts,
    search_emails,
    search_tags,
    search_workflows,
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


def test_get_account_by_email(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection, email="user@example.com")
    fetched = get_account_by_email(database_connection, "user@example.com")
    assert fetched is not None
    assert fetched.id == account.id


def test_get_account_by_email_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    assert get_account_by_email(database_connection, "nobody@example.com") is None


def test_get_account_by_email_case_insensitive(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection, email="User@Example.com")
    fetched = get_account_by_email(database_connection, "user@example.com")
    assert fetched is not None
    assert fetched.id == account.id


def test_task_insert_emits_notify(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """PG trigger on task INSERT fires NOTIFY task_pending."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)

    listen_conn = cast(
        psycopg.Connection[dict[str, Any]],
        psycopg.connect(TEST_DATABASE_URL, row_factory=dict_row, autocommit=True),  # type: ignore[arg-type]
    )
    try:
        listen_conn.execute("LISTEN task_pending")
        create_task(
            database_connection,
            workflow_id=workflow.id,
            contact_id=contact.id,
            description="test task",
            scheduled_at="2026-01-01T00:00:00Z",
        )
        notifications = list(listen_conn.notifies(timeout=2.0))
        assert len(notifications) >= 1
        assert notifications[0].channel == "task_pending"
    finally:
        listen_conn.close()


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
    assert results[0].email == "a@foo.com"


def test_list_contacts_by_status(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    c1 = make_test_contact(database_connection, email="a@foo.com", domain="foo.com")
    c2 = make_test_contact(database_connection, email="b@bar.com", domain="bar.com")
    from mailpilot.database import update_contact

    update_contact(database_connection, c2.id, status="bounced")
    active = list_contacts(database_connection, status="active")
    assert len(active) == 1
    assert active[0].id == c1.id
    bounced = list_contacts(database_connection, status="bounced")
    assert len(bounced) == 1
    assert bounced[0].id == c2.id


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


def test_create_or_get_contact_by_email_creates_new(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = create_or_get_contact_by_email(
        database_connection,
        email="new@example.com",
        first_name="Alice",
        last_name="Smith",
    )
    assert contact.email == "new@example.com"
    assert contact.domain == "example.com"
    assert contact.first_name == "Alice"
    assert contact.last_name == "Smith"


def test_create_or_get_contact_by_email_returns_existing(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    first = create_or_get_contact_by_email(
        database_connection, email="dup@example.com", first_name="Bob"
    )
    second = create_or_get_contact_by_email(
        database_connection, email="dup@example.com", first_name="Robert"
    )
    assert first.id == second.id
    # Non-null existing name is not overwritten.
    assert second.first_name == "Bob"


def test_create_or_get_contact_by_email_backfills_null_names(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    created = create_or_get_contact_by_email(
        database_connection, email="nameless@example.com"
    )
    assert created.first_name is None
    assert created.last_name is None

    backfilled = create_or_get_contact_by_email(
        database_connection,
        email="nameless@example.com",
        first_name="Jane",
        last_name="Doe",
    )
    assert backfilled.id == created.id
    assert backfilled.first_name == "Jane"
    assert backfilled.last_name == "Doe"


def test_get_contacts_by_emails_empty(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    assert get_contacts_by_emails(database_connection, []) == {}


def test_get_contacts_by_emails_returns_map_for_existing(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    alice = make_test_contact(
        database_connection, email="alice@example.com", domain="example.com"
    )
    bob = make_test_contact(
        database_connection, email="bob@example.com", domain="example.com"
    )
    result = get_contacts_by_emails(
        database_connection, ["alice@example.com", "bob@example.com"]
    )
    assert set(result.keys()) == {"alice@example.com", "bob@example.com"}
    assert result["alice@example.com"].id == alice.id
    assert result["bob@example.com"].id == bob.id


def test_get_contacts_by_emails_omits_missing(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    make_test_contact(
        database_connection, email="alice@example.com", domain="example.com"
    )
    result = get_contacts_by_emails(
        database_connection, ["alice@example.com", "ghost@example.com"]
    )
    assert set(result.keys()) == {"alice@example.com"}


def test_get_contacts_by_emails_deduplicates_input(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    make_test_contact(
        database_connection, email="alice@example.com", domain="example.com"
    )
    result = get_contacts_by_emails(
        database_connection, ["alice@example.com", "alice@example.com"]
    )
    assert set(result.keys()) == {"alice@example.com"}


def test_create_contacts_bulk_empty(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    assert create_contacts_bulk(database_connection, []) == {}


def test_create_contacts_bulk_all_new(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    result = create_contacts_bulk(
        database_connection, ["alice@example.com", "bob@other.com"]
    )
    assert set(result.keys()) == {"alice@example.com", "bob@other.com"}
    assert result["alice@example.com"].domain == "example.com"
    assert result["bob@other.com"].domain == "other.com"
    # Rows actually persisted.
    assert get_contact_by_email(database_connection, "alice@example.com") is not None
    assert get_contact_by_email(database_connection, "bob@other.com") is not None


def test_create_contacts_bulk_returns_existing_and_new(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    existing = make_test_contact(
        database_connection, email="alice@example.com", domain="example.com"
    )
    result = create_contacts_bulk(
        database_connection, ["alice@example.com", "bob@example.com"]
    )
    assert set(result.keys()) == {"alice@example.com", "bob@example.com"}
    # Existing row kept its original id.
    assert result["alice@example.com"].id == existing.id


def test_create_contacts_bulk_deduplicates_input(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    result = create_contacts_bulk(
        database_connection, ["alice@example.com", "alice@example.com"]
    )
    assert set(result.keys()) == {"alice@example.com"}
    row = database_connection.execute(
        "SELECT COUNT(*) AS n FROM contact WHERE email = %(email)s",
        {"email": "alice@example.com"},
    ).fetchone()
    assert row is not None
    assert row["n"] == 1


def test_create_contacts_bulk_handles_missing_at_symbol(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    result = create_contacts_bulk(database_connection, ["weirdaddress"])
    assert "weirdaddress" in result
    assert result["weirdaddress"].domain == ""


def test_create_contacts_bulk_concurrent_is_safe(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Concurrent bulk inserts with overlapping emails must converge safely."""
    emails_a = ["alice@example.com", "bob@example.com"]
    emails_b = ["bob@example.com", "carol@example.com"]
    thread_count = 2
    barrier = threading.Barrier(thread_count)
    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(emails: list[str]) -> None:
        conn = cast(
            psycopg.Connection[dict[str, Any]],
            psycopg.connect(TEST_DATABASE_URL, row_factory=dict_row),  # type: ignore[arg-type]
        )
        try:
            barrier.wait(timeout=5)
            result = create_contacts_bulk(conn, emails)
            with lock:
                results.append({e: c.id for e, c in result.items()})
        except BaseException as exc:
            with lock:
                errors.append(exc)
        finally:
            conn.close()

    threads = [
        threading.Thread(target=worker, args=(emails_a,)),
        threading.Thread(target=worker, args=(emails_b,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    assert len(results) == thread_count

    row = database_connection.execute(
        "SELECT COUNT(*) AS n FROM contact WHERE email = ANY(%(emails)s)",
        {"emails": ["alice@example.com", "bob@example.com", "carol@example.com"]},
    ).fetchone()
    assert row is not None
    assert row["n"] == 3

    # Both workers must agree on Bob's id (the shared row).
    bob_ids = {r["bob@example.com"] for r in results}
    assert len(bob_ids) == 1


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
    updated = update_workflow(database_connection, workflow.id, objective="Book demo")
    assert updated is not None
    assert updated.objective == "Book demo"


def test_update_workflow_ignores_immutable_fields(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    updated = update_workflow(
        database_connection, workflow.id, type="inbound", account_id="other"
    )
    assert updated is not None
    assert updated.type == "outbound"
    assert updated.account_id == account.id


def test_activate_workflow(database_connection: psycopg.Connection[dict[str, Any]]):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    update_workflow(
        database_connection,
        workflow.id,
        objective="Book demo",
        instructions="You are a sales rep.",
    )
    activated = activate_workflow(database_connection, workflow.id)
    assert activated.status == "active"


def test_activate_workflow_requires_objective(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    update_workflow(
        database_connection, workflow.id, instructions="You are a sales rep."
    )
    with pytest.raises(ValueError, match="objective must be non-empty"):
        activate_workflow(database_connection, workflow.id)


def test_activate_workflow_requires_instructions(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    update_workflow(database_connection, workflow.id, objective="Book demo")
    with pytest.raises(ValueError, match="instructions must be non-empty"):
        activate_workflow(database_connection, workflow.id)


def test_pause_workflow(database_connection: psycopg.Connection[dict[str, Any]]):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    update_workflow(
        database_connection,
        workflow.id,
        objective="Book demo",
        instructions="You are a sales rep.",
    )
    activate_workflow(database_connection, workflow.id)
    paused = pause_workflow(database_connection, workflow.id)
    assert paused.status == "paused"


def test_pause_workflow_requires_active_status(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    with pytest.raises(ValueError, match="cannot pause workflow"):
        pause_workflow(database_connection, workflow.id)


def test_list_workflows_by_status(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    w1 = make_test_workflow(database_connection, account_id=account.id, name="W1")
    make_test_workflow(database_connection, account_id=account.id, name="W2")
    update_workflow(
        database_connection,
        w1.id,
        objective="Book demo",
        instructions="You are a sales rep.",
    )
    activate_workflow(database_connection, w1.id)
    # w2 stays as draft
    active = list_workflows(database_connection, account_id=account.id, status="active")
    assert len(active) == 1
    assert active[0].name == "W1"
    drafts = list_workflows(database_connection, account_id=account.id, status="draft")
    assert len(drafts) == 1
    assert drafts[0].name == "W2"
    all_workflows = list_workflows(database_connection, account_id=account.id)
    assert len(all_workflows) == 2


def test_list_workflows_by_type(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    make_test_workflow(
        database_connection,
        account_id=account.id,
        name="Outreach",
        workflow_type="outbound",
    )
    make_test_workflow(
        database_connection,
        account_id=account.id,
        name="Auto-reply",
        workflow_type="inbound",
    )
    outbound = list_workflows(database_connection, workflow_type="outbound")
    assert len(outbound) == 1
    assert outbound[0].name == "Outreach"
    inbound = list_workflows(database_connection, workflow_type="inbound")
    assert len(inbound) == 1
    assert inbound[0].name == "Auto-reply"


def test_search_workflows_by_name(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    make_test_workflow(database_connection, account_id=account.id, name="Demo outreach")
    make_test_workflow(
        database_connection, account_id=account.id, name="Support auto-reply"
    )
    results = search_workflows(database_connection, "demo")
    assert len(results) == 1
    assert results[0].name == "Demo outreach"


def test_search_workflows_by_objective(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    w1 = make_test_workflow(database_connection, account_id=account.id, name="Alpha")
    make_test_workflow(database_connection, account_id=account.id, name="Beta")
    update_workflow(database_connection, w1.id, objective="Book discovery call")
    results = search_workflows(database_connection, "discovery")
    assert len(results) == 1
    assert results[0].id == w1.id


def test_search_workflows_respects_limit(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    for i in range(5):
        make_test_workflow(database_connection, account_id=account.id, name=f"Flow {i}")
    results = search_workflows(database_connection, "flow", limit=2)
    assert len(results) == 2


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
    assert email is not None
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
    assert email is not None
    assert email.status == "sent"
    assert email.is_routed is True


def test_create_email_records_sent_at(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from datetime import UTC, datetime

    account = make_test_account(database_connection)
    sent_at = datetime(2024, 6, 1, 12, 34, 56, tzinfo=UTC)
    email = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="Outgoing",
        status="sent",
        is_routed=True,
        sent_at=sent_at,
    )
    assert email is not None
    assert email.sent_at == sent_at


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
    assert email is not None
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
    assert e1 is not None
    assert e2 is not None
    results = get_emails_by_gmail_thread_id(database_connection, "thread_abc")
    assert len(results) == 2
    ids = {e.id for e in results}
    assert e1.id in ids
    assert e2.id in ids


def test_get_emails_by_gmail_thread_id_empty(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    assert get_emails_by_gmail_thread_id(database_connection, "nonexistent") == []


def test_get_latest_email_in_thread_returns_most_recent(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Returns the most recently created email row for the given thread+account."""
    account = make_test_account(database_connection)
    first = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        gmail_message_id="thread-msg-1",
        gmail_thread_id="thread-latest",
        rfc2822_message_id="<first@mail.gmail.com>",
        subject="Hello",
        status="sent",
    )
    second = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        gmail_message_id="thread-msg-2",
        gmail_thread_id="thread-latest",
        rfc2822_message_id="<second@mail.gmail.com>",
        subject="Re: Hello",
    )
    assert first is not None
    assert second is not None
    latest = get_latest_email_in_thread(
        database_connection, account.id, "thread-latest"
    )
    assert latest is not None
    assert latest.id == second.id
    assert latest.rfc2822_message_id == "<second@mail.gmail.com>"


def test_get_latest_email_in_thread_scopes_by_account(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """The same Gmail thread id on a different account is ignored."""
    account_a = make_test_account(database_connection, email="a@example.com")
    account_b = make_test_account(database_connection, email="b@example.com")
    create_email(
        database_connection,
        account_id=account_b.id,
        direction="inbound",
        gmail_message_id="other-1",
        gmail_thread_id="shared-thread",
        rfc2822_message_id="<other@mail.gmail.com>",
    )
    assert (
        get_latest_email_in_thread(database_connection, account_a.id, "shared-thread")
        is None
    )


def test_get_latest_email_in_thread_returns_none_when_empty(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    assert (
        get_latest_email_in_thread(database_connection, account.id, "nonexistent")
        is None
    )


def test_update_email_allows_rfc2822_message_id(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Outbound rows can be backfilled with their Message-ID after Gmail send."""
    account = make_test_account(database_connection)
    email = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        gmail_message_id="msg-update-mid",
        status="sent",
    )
    assert email is not None
    assert email.rfc2822_message_id is None
    updated = update_email(
        database_connection,
        email.id,
        rfc2822_message_id="<sent@mail.gmail.com>",
    )
    assert updated is not None
    assert updated.rfc2822_message_id == "<sent@mail.gmail.com>"


def test_update_email(database_connection: psycopg.Connection[dict[str, Any]]):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        gmail_message_id="msg_update",
    )
    assert email is not None
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


def test_create_email_concurrent_same_gmail_message_id_is_safe(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Concurrent inserts for the same gmail_message_id must not raise.

    Regression guard for issue #24: two workers racing on the same Gmail
    message must land exactly one row. ON CONFLICT DO NOTHING makes the
    loser return None instead of raising UniqueViolation.
    """
    account = make_test_account(database_connection)
    account_id = account.id
    gmail_message_id = "race-msg"
    thread_count = 2
    barrier = threading.Barrier(thread_count)
    results: list[object] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        conn = cast(
            psycopg.Connection[dict[str, Any]],
            psycopg.connect(TEST_DATABASE_URL, row_factory=dict_row),  # type: ignore[arg-type]
        )
        try:
            barrier.wait(timeout=5)
            result = create_email(
                conn,
                account_id=account_id,
                direction="inbound",
                gmail_message_id=gmail_message_id,
                gmail_thread_id="race-thread",
            )
            with lock:
                results.append(result)
        except BaseException as exc:
            with lock:
                errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    assert len(results) == thread_count
    winners = [r for r in results if r is not None]
    losers = [r for r in results if r is None]
    assert len(winners) == 1
    assert len(losers) == thread_count - 1

    row = database_connection.execute(
        "SELECT COUNT(*) AS n FROM email WHERE gmail_message_id = %(gmid)s",
        {"gmid": gmail_message_id},
    ).fetchone()
    assert row is not None
    assert row["n"] == 1


def test_list_emails_since(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from datetime import datetime, timedelta

    account = make_test_account(database_connection)
    old = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Old",
        gmail_message_id="msg_old",
        received_at=datetime.now(UTC) - timedelta(days=3),
    )
    assert old is not None
    recent = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Recent",
        gmail_message_id="msg_recent",
        received_at=datetime.now(UTC),
    )
    assert recent is not None
    since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    results = list_emails(database_connection, since=since)
    assert len(results) == 1
    assert results[0].subject == "Recent"


def test_list_emails_order_matches_since_filter(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """list_emails orders by COALESCE(sent_at, received_at) DESC.

    The order column must agree with the `since` filter so an operator
    can page newest-first using a timestamp visible in the summary.
    """
    from datetime import datetime, timedelta

    account = make_test_account(database_connection)
    # Insert the chronologically-newer row FIRST so that ordering by
    # `created_at DESC` would put the older content on top -- only
    # `COALESCE(sent_at, received_at) DESC` reorders correctly.
    newer_outbound = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="Newer outbound",
        sent_at=datetime.now(UTC),
    )
    older_inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Older inbound",
        gmail_message_id="msg_older_inbound",
        received_at=datetime.now(UTC) - timedelta(days=2),
    )
    assert older_inbound is not None
    assert newer_outbound is not None
    results = list_emails(database_connection, account_id=account.id)
    assert [r.subject for r in results] == ["Newer outbound", "Older inbound"]


def test_list_emails_by_thread_id(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        gmail_message_id="msg_t1",
        gmail_thread_id="thread_a",
        subject="Thread A",
    )
    create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        gmail_message_id="msg_t2",
        gmail_thread_id="thread_b",
        subject="Thread B",
    )
    results = list_emails(database_connection, thread_id="thread_a")
    assert len(results) == 1
    assert results[0].subject == "Thread A"


def test_list_emails_by_direction(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        gmail_message_id="msg_in",
        subject="Inbound",
    )
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        gmail_message_id="msg_out",
        subject="Outbound",
        status="sent",
    )
    results = list_emails(database_connection, direction="outbound")
    assert len(results) == 1
    assert results[0].subject == "Outbound"


def test_list_emails_by_workflow_id(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        gmail_message_id="msg_wf",
        workflow_id=workflow.id,
        subject="Campaign",
        status="sent",
    )
    create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        gmail_message_id="msg_no_wf",
        subject="No workflow",
    )
    results = list_emails(database_connection, workflow_id=workflow.id)
    assert len(results) == 1
    assert results[0].subject == "Campaign"


def test_list_emails_by_status(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        gmail_message_id="msg_recv",
        subject="Received",
        status="received",
    )
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        gmail_message_id="msg_sent",
        subject="Sent",
        status="sent",
    )
    results = list_emails(database_connection, status="sent")
    assert len(results) == 1
    assert results[0].subject == "Sent"


# -- get_company_by_domain ----------------------------------------------------


def test_get_company_by_domain(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    company = make_test_company(database_connection, name="Acme", domain="acme.com")
    fetched = get_company_by_domain(database_connection, "acme.com")
    assert fetched is not None
    assert fetched.id == company.id
    assert fetched.domain == "acme.com"


def test_get_company_by_domain_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    assert get_company_by_domain(database_connection, "nonexistent.com") is None


# -- get_last_cold_outbound ----------------------------------------------------


def test_get_last_cold_outbound_returns_newest(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="r@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)

    # Older cold outbound (first in its thread).
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="old pitch",
        contact_id=contact.id,
        workflow_id=workflow.id,
        gmail_message_id="old-msg",
        gmail_thread_id="thread-old",
        status="sent",
    )
    # Newer cold outbound (first in a different thread).
    newer = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="new pitch",
        contact_id=contact.id,
        workflow_id=workflow.id,
        gmail_message_id="new-msg",
        gmail_thread_id="thread-new",
        status="sent",
    )
    assert newer is not None

    result = get_last_cold_outbound(
        database_connection, account.id, contact.id, workflow.id
    )
    assert result is not None
    assert result.id == newer.id


def test_get_last_cold_outbound_excludes_follow_ups(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """A second outbound in the same thread is a follow-up, not cold outreach."""
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="r@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)

    # First outbound in thread (cold).
    cold = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="initial pitch",
        contact_id=contact.id,
        workflow_id=workflow.id,
        gmail_message_id="cold-msg",
        gmail_thread_id="thread-1",
        status="sent",
    )
    assert cold is not None

    # Second outbound in same thread (follow-up reply, not cold).
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="follow up",
        contact_id=contact.id,
        workflow_id=workflow.id,
        gmail_message_id="followup-msg",
        gmail_thread_id="thread-1",
        status="sent",
    )

    result = get_last_cold_outbound(
        database_connection, account.id, contact.id, workflow.id
    )
    assert result is not None
    # Should return the cold email, not the follow-up.
    assert result.id == cold.id


def test_get_last_cold_outbound_ignores_inbound(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="r@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)

    create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="hello",
        contact_id=contact.id,
    )

    result = get_last_cold_outbound(
        database_connection, account.id, contact.id, workflow.id
    )
    assert result is None


def test_get_last_cold_outbound_none_when_no_emails(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="r@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)

    result = get_last_cold_outbound(
        database_connection, account.id, contact.id, workflow.id
    )
    assert result is None


def test_get_last_cold_outbound_scoped_to_workflow(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Cooldown is per workflow -- a different workflow can send independently."""
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="r@example.com", domain="example.com"
    )
    wf_a = make_test_workflow(
        database_connection, account_id=account.id, name="Campaign A"
    )
    wf_b = make_test_workflow(
        database_connection, account_id=account.id, name="Campaign B"
    )

    # Cold outbound from workflow A.
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="pitch A",
        contact_id=contact.id,
        workflow_id=wf_a.id,
        gmail_message_id="msg-a",
        gmail_thread_id="thread-a",
        status="sent",
    )

    # Workflow A has a cold outbound.
    result_a = get_last_cold_outbound(
        database_connection, account.id, contact.id, wf_a.id
    )
    assert result_a is not None

    # Workflow B has no cold outbound -- cooldown does not apply.
    result_b = get_last_cold_outbound(
        database_connection, account.id, contact.id, wf_b.id
    )
    assert result_b is None


# -- search_emails with account_id filter --------------------------------------


def test_search_emails_filters_by_account_id(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    a1 = make_test_account(database_connection, email="a1@example.com")
    a2 = make_test_account(database_connection, email="a2@example.com")

    create_email(
        database_connection,
        account_id=a1.id,
        direction="inbound",
        subject="pricing question",
    )
    create_email(
        database_connection,
        account_id=a2.id,
        direction="inbound",
        subject="pricing info",
    )

    results = search_emails(database_connection, "pricing", account_id=a1.id)
    assert len(results) == 1
    assert results[0].account_id == a1.id


# -- sender / recipients columns -----------------------------------------------


def test_create_email_with_sender_and_recipients(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    recipients = {"to": ["alice@example.com"], "cc": ["bob@example.com"]}
    email = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="Hello",
        status="sent",
        sender="outbound@lab5.ca",
        recipients=recipients,
    )
    assert email is not None
    assert email.sender == "outbound@lab5.ca"
    assert email.recipients == recipients


def test_create_email_defaults_sender_and_recipients(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
    )
    assert email is not None
    assert email.sender == ""
    assert email.recipients == {}


def test_search_emails_matches_sender(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Unrelated subject",
        sender="alice@example.com",
        gmail_message_id="msg_sender_search",
    )
    create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Another email",
        sender="bob@example.com",
        gmail_message_id="msg_sender_search_2",
    )
    results = search_emails(database_connection, "alice@example.com")
    assert len(results) == 1
    assert results[0].sender == "alice@example.com"


def test_search_emails_matches_recipients(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="Outgoing",
        status="sent",
        recipients={"to": ["kb@lab5.ca"], "cc": ["dev@lab5.ca"]},
        gmail_message_id="msg_recip_search",
    )
    results = search_emails(database_connection, "kb@lab5.ca")
    assert len(results) == 1
    assert results[0].subject == "Outgoing"


def test_list_emails_filter_by_sender(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        sender="alice@example.com",
        gmail_message_id="msg_from_alice",
    )
    create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        sender="bob@example.com",
        gmail_message_id="msg_from_bob",
    )
    results = list_emails(database_connection, sender="alice@example.com")
    assert len(results) == 1
    assert results[0].sender == "alice@example.com"


def test_list_emails_filter_by_recipient(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        status="sent",
        recipients={"to": ["kb@lab5.ca"]},
        gmail_message_id="msg_to_kb",
    )
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        status="sent",
        recipients={"to": ["other@lab5.ca"]},
        gmail_message_id="msg_to_other",
    )
    results = list_emails(database_connection, recipient="kb@lab5.ca")
    assert len(results) == 1
    # `recipients` is not in EmailSummary; verify by hydrating via get_email.
    from mailpilot.database import get_email

    full = get_email(database_connection, results[0].id)
    assert full is not None
    assert "kb@lab5.ca" in full.recipients["to"]


def test_list_emails_filter_by_recipient_matches_cc(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        status="sent",
        recipients={"to": ["main@example.com"], "cc": ["kb@lab5.ca"]},
        gmail_message_id="msg_cc_kb",
    )
    results = list_emails(database_connection, recipient="kb@lab5.ca")
    assert len(results) == 1


# -- Activity ------------------------------------------------------------------


def test_create_activity(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = make_test_contact(database_connection)
    activity = make_test_activity(database_connection, contact_id=contact.id)
    assert activity.contact_id == contact.id
    assert activity.type == "email_sent"
    assert activity.summary == "Test activity"
    assert activity.detail == {}
    assert activity.company_id is None
    assert activity.id


def test_create_activity_with_company(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    company = make_test_company(database_connection)
    contact = make_test_contact(database_connection, company_id=company.id)
    activity = make_test_activity(
        database_connection,
        contact_id=contact.id,
        company_id=company.id,
        activity_type="tag_added",
        summary="Tagged as prospect",
        detail={"tag": "prospect"},
    )
    assert activity.company_id == company.id
    assert activity.type == "tag_added"
    assert activity.detail == {"tag": "prospect"}


def test_create_activity_with_detail(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = make_test_contact(database_connection)
    activity = create_activity(
        database_connection,
        contact_id=contact.id,
        activity_type="email_sent",
        summary="Sent intro email",
        detail={"email_id": "e-123", "subject": "Hello"},
    )
    assert activity.detail == {"email_id": "e-123", "subject": "Hello"}


def test_list_activities_by_contact(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    c1 = make_test_contact(database_connection, email="a@test.com", domain="test.com")
    c2 = make_test_contact(database_connection, email="b@test.com", domain="test.com")
    make_test_activity(database_connection, contact_id=c1.id, summary="first")
    make_test_activity(database_connection, contact_id=c1.id, summary="second")
    make_test_activity(database_connection, contact_id=c2.id, summary="other")

    results = list_activities(database_connection, contact_id=c1.id)
    assert len(results) == 2
    # Ordered by created_at DESC
    assert results[0].summary == "second"
    assert results[1].summary == "first"


def test_list_activities_by_company(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    company = make_test_company(database_connection)
    contact = make_test_contact(database_connection, company_id=company.id)
    make_test_activity(
        database_connection, contact_id=contact.id, company_id=company.id
    )

    results = list_activities(database_connection, company_id=company.id)
    assert len(results) == 1
    assert results[0].company_id == company.id


def test_list_activities_by_type(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = make_test_contact(database_connection)
    make_test_activity(
        database_connection, contact_id=contact.id, activity_type="email_sent"
    )
    make_test_activity(
        database_connection, contact_id=contact.id, activity_type="tag_added"
    )

    results = list_activities(
        database_connection, contact_id=contact.id, activity_type="tag_added"
    )
    assert len(results) == 1
    assert results[0].type == "tag_added"


def test_list_activities_since(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from datetime import datetime, timedelta

    contact = make_test_contact(database_connection)
    make_test_activity(database_connection, contact_id=contact.id, summary="old")
    # Set old activity's created_at to the past
    database_connection.execute(
        "UPDATE activity SET created_at = CURRENT_TIMESTAMP - interval '2 days' "
        "WHERE summary = 'old'"
    )
    database_connection.commit()
    make_test_activity(database_connection, contact_id=contact.id, summary="recent")

    since = datetime.now(UTC) - timedelta(days=1)
    results = list_activities(
        database_connection, contact_id=contact.id, since=since.isoformat()
    )
    assert len(results) == 1
    assert results[0].summary == "recent"


def test_list_activities_with_limit(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = make_test_contact(database_connection)
    for i in range(5):
        make_test_activity(
            database_connection, contact_id=contact.id, summary=f"act-{i}"
        )

    results = list_activities(database_connection, contact_id=contact.id, limit=2)
    assert len(results) == 2


def test_list_activities_requires_filter(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    with pytest.raises(ValueError, match="contact_id or company_id"):
        list_activities(database_connection)


def test_create_activity_with_structured_fks(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """email_id, workflow_id, task_id are first-class FK columns (#102 sugg 5)."""
    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    email = create_email(
        database_connection,
        account_id=account.id,
        contact_id=contact.id,
        direction="outbound",
        subject="Hi",
        body_text="hi",
    )
    assert email is not None

    activity = create_activity(
        database_connection,
        contact_id=contact.id,
        activity_type="email_sent",
        summary="Hi",
        email_id=email.id,
        workflow_id=workflow.id,
    )
    assert activity.email_id == email.id
    assert activity.workflow_id == workflow.id
    assert activity.task_id is None


def test_create_activity_company_only(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """contact_id is nullable when company_id is provided (#102 sugg 2)."""
    company = make_test_company(database_connection)
    activity = create_activity(
        database_connection,
        company_id=company.id,
        activity_type="note_added",
        summary="Company note",
    )
    assert activity.contact_id is None
    assert activity.company_id == company.id


def test_create_activity_requires_contact_or_company(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    with pytest.raises(ValueError, match="contact_id or company_id"):
        create_activity(
            database_connection,
            activity_type="note_added",
            summary="orphan",
        )


def test_status_counts_includes_activities(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = make_test_contact(database_connection)
    make_test_activity(database_connection, contact_id=contact.id)
    counts = get_status_counts(database_connection)
    assert counts["activities"] == 1


# -- Tag -----------------------------------------------------------------------


def test_normalize_tag_name_accepts_valid_inputs() -> None:
    """Lowercase, hyphenated, alphanumeric tags pass through unchanged."""
    from mailpilot.database import _normalize_tag_name

    assert _normalize_tag_name("prospect") == "prospect"
    assert _normalize_tag_name("hot-lead") == "hot-lead"
    assert _normalize_tag_name("q4-2025") == "q4-2025"


def test_normalize_tag_name_collapses_separators_and_case() -> None:
    """Whitespace, underscores, and uppercase are normalized; hyphens collapse."""
    from mailpilot.database import _normalize_tag_name

    assert _normalize_tag_name("Hot Lead") == "hot-lead"
    assert _normalize_tag_name("hot_lead") == "hot-lead"
    assert _normalize_tag_name("HOT--LEAD") == "hot-lead"
    assert _normalize_tag_name("  spaced  ") == "spaced"
    assert _normalize_tag_name("-leading-trailing-") == "leading-trailing"


def test_normalize_tag_name_rejects_invalid() -> None:
    """Names that cannot be normalized to [a-z0-9][a-z0-9-]* raise ValueError."""
    from mailpilot.database import _normalize_tag_name

    with pytest.raises(ValueError):
        _normalize_tag_name("")
    with pytest.raises(ValueError):
        _normalize_tag_name("---")
    with pytest.raises(ValueError):
        _normalize_tag_name("hot/lead")
    with pytest.raises(ValueError):
        _normalize_tag_name("hot.lead")


def test_create_contact_tag_and_company_tag(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """create_tag accepts contact_id XOR company_id."""
    company = make_test_company(database_connection)
    contact = make_test_contact(database_connection)

    contact_tag = create_tag(database_connection, contact_id=contact.id, name="prospect")
    assert contact_tag is not None
    assert contact_tag.contact_id == contact.id
    assert contact_tag.company_id is None
    assert contact_tag.name == "prospect"

    company_tag = create_tag(database_connection, company_id=company.id, name="enterprise")
    assert company_tag is not None
    assert company_tag.company_id == company.id
    assert company_tag.contact_id is None


def test_create_tag_requires_exactly_one_owner(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """contact_id XOR company_id; passing both or neither raises."""
    with pytest.raises(ValueError, match="exactly one"):
        create_tag(database_connection, name="x")
    with pytest.raises(ValueError, match="exactly one"):
        create_tag(database_connection, contact_id="c1", company_id="co1", name="x")


def test_create_tag_normalizes_name(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """create_tag applies _normalize_tag_name."""
    contact = make_test_contact(database_connection)
    tag = create_tag(database_connection, contact_id=contact.id, name="Hot Lead")
    assert tag is not None
    assert tag.name == "hot-lead"


def test_create_tag_idempotent_on_duplicate(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Duplicate insert returns None thanks to ON CONFLICT DO NOTHING."""
    contact = make_test_contact(database_connection)
    first = create_tag(database_connection, contact_id=contact.id, name="prospect")
    second = create_tag(database_connection, contact_id=contact.id, name="prospect")
    assert first is not None
    assert second is None


def test_delete_tag_by_contact(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = make_test_contact(database_connection)
    create_tag(database_connection, contact_id=contact.id, name="cold")
    assert delete_tag(database_connection, contact_id=contact.id, name="cold") is True
    assert delete_tag(database_connection, contact_id=contact.id, name="cold") is False


def test_list_tags_by_contact(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = make_test_contact(database_connection)
    create_tag(database_connection, contact_id=contact.id, name="prospect")
    create_tag(database_connection, contact_id=contact.id, name="cold")
    tags = list_tags(database_connection, contact_id=contact.id)
    assert {t.name for t in tags} == {"prospect", "cold"}


def test_list_tags_empty(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = make_test_contact(database_connection)
    tags = list_tags(database_connection, contact_id=contact.id)
    assert tags == []


def test_list_contacts_by_tag_name(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import list_contacts_by_tag

    a = make_test_contact(database_connection, email="x1@acme.test", domain="acme.test")
    b = make_test_contact(database_connection, email="x2@acme.test", domain="acme.test")
    create_tag(database_connection, contact_id=a.id, name="hot")
    create_tag(database_connection, contact_id=b.id, name="hot")
    ids = list_contacts_by_tag(database_connection, name="hot")
    assert set(ids) == {a.id, b.id}


def test_list_companies_by_tag_name(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import list_companies_by_tag

    a = make_test_company(database_connection, name="A", domain="a.test")
    b = make_test_company(database_connection, name="B", domain="b.test")
    create_tag(database_connection, company_id=a.id, name="enterprise")
    create_tag(database_connection, company_id=b.id, name="enterprise")
    ids = list_companies_by_tag(database_connection, name="enterprise")
    assert set(ids) == {a.id, b.id}


def test_search_tags(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = make_test_contact(database_connection)
    create_tag(database_connection, contact_id=contact.id, name="prospect")
    create_tag(database_connection, contact_id=contact.id, name="cold")

    results = search_tags(database_connection, name="pro")
    assert len(results) == 1
    assert results[0].name == "prospect"


def test_search_tags_with_owner(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = make_test_contact(database_connection)
    company = make_test_company(database_connection)
    create_tag(database_connection, contact_id=contact.id, name="prospect")
    create_tag(database_connection, company_id=company.id, name="prospect")

    results = search_tags(database_connection, name="prospect", owner="contact")
    assert len(results) == 1
    assert results[0].contact_id == contact.id


def test_status_counts_includes_tags(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = make_test_contact(database_connection)
    make_test_tag(database_connection, contact_id=contact.id)
    counts = get_status_counts(database_connection)
    assert counts["tags"] == 1


# -- Atomic helpers ----------------------------------------------------------


def test_add_contact_tag_emits_activity_atomically(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """add_contact_tag writes tag + tag_added activity in one transaction."""
    from mailpilot.database import add_contact_tag

    contact = make_test_contact(database_connection)
    tag = add_contact_tag(database_connection, contact_id=contact.id, name="prospect")
    assert tag is not None
    assert tag.name == "prospect"
    assert [t.name for t in list_tags(database_connection, contact_id=contact.id)] == [
        "prospect"
    ]
    activities = list_activities(database_connection, contact_id=contact.id)
    assert len(activities) == 1
    assert activities[0].type == "tag_added"
    assert activities[0].summary == "Tagged as prospect"


def test_add_contact_tag_returns_none_on_duplicate_no_activity(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Duplicate tag insert returns None and emits no activity."""
    from mailpilot.database import add_contact_tag

    contact = make_test_contact(database_connection)
    add_contact_tag(database_connection, contact_id=contact.id, name="prospect")
    second = add_contact_tag(
        database_connection, contact_id=contact.id, name="prospect"
    )
    assert second is None
    activities = list_activities(database_connection, contact_id=contact.id)
    assert len(activities) == 1


def test_remove_contact_tag_emits_activity_atomically(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import add_contact_tag, remove_contact_tag

    contact = make_test_contact(database_connection)
    add_contact_tag(database_connection, contact_id=contact.id, name="cold")
    assert (
        remove_contact_tag(database_connection, contact_id=contact.id, name="cold")
        is True
    )
    types = [
        a.type for a in list_activities(database_connection, contact_id=contact.id)
    ]
    assert "tag_removed" in types


def test_add_company_tag_emits_company_activity(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import add_company_tag

    company = make_test_company(database_connection)
    add_company_tag(database_connection, company_id=company.id, name="enterprise")
    activities = list_activities(database_connection, company_id=company.id)
    assert len(activities) == 1
    assert activities[0].type == "tag_added"
    assert activities[0].company_id == company.id
    assert activities[0].contact_id is None


def test_remove_company_tag_emits_activity_atomically(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import add_company_tag, remove_company_tag

    company = make_test_company(database_connection)
    add_company_tag(database_connection, company_id=company.id, name="enterprise")
    assert (
        remove_company_tag(database_connection, company_id=company.id, name="enterprise")
        is True
    )
    types = [
        a.type for a in list_activities(database_connection, company_id=company.id)
    ]
    assert "tag_removed" in types


def test_add_contact_note_emits_activity_atomically(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import add_contact_note

    contact = make_test_contact(database_connection)
    note = add_contact_note(
        database_connection, contact_id=contact.id, body="quick note"
    )
    notes = list_notes(database_connection, contact_id=contact.id)
    assert [n.id for n in notes] == [note.id]
    activities = list_activities(database_connection, contact_id=contact.id)
    assert len(activities) == 1
    assert activities[0].type == "note_added"


def test_add_company_note_emits_company_activity(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import add_company_note

    company = make_test_company(database_connection)
    add_company_note(database_connection, company_id=company.id, body="ent")
    activities = list_activities(database_connection, company_id=company.id)
    assert len(activities) == 1
    assert activities[0].type == "note_added"
    assert activities[0].company_id == company.id


# -- Note ---------------------------------------------------------------------


def test_create_contact_note_and_company_note(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import create_note

    contact = make_test_contact(database_connection)
    contact_note = create_note(
        database_connection, contact_id=contact.id, body="Met at conf"
    )
    assert contact_note.contact_id == contact.id
    assert contact_note.company_id is None

    company = make_test_company(database_connection)
    company_note = create_note(
        database_connection, company_id=company.id, body="Tier 1 account"
    )
    assert company_note.company_id == company.id
    assert company_note.contact_id is None


def test_create_note_requires_exactly_one_owner(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import create_note

    with pytest.raises(ValueError, match="exactly one"):
        create_note(database_connection, body="x")
    with pytest.raises(ValueError, match="exactly one"):
        create_note(
            database_connection, contact_id="c1", company_id="co1", body="x"
        )


def test_list_notes(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = make_test_contact(database_connection)
    make_test_note(database_connection, contact_id=contact.id, body="first")
    make_test_note(database_connection, contact_id=contact.id, body="second")
    notes = list_notes(database_connection, contact_id=contact.id)
    assert len(notes) == 2
    # Ordered by created_at DESC. Summary exposes body_preview, not body.
    assert notes[0].body_preview == "second"
    assert notes[1].body_preview == "first"


def test_list_notes_empty(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = make_test_contact(database_connection)
    notes = list_notes(database_connection, contact_id=contact.id)
    assert notes == []


def test_list_notes_with_limit(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = make_test_contact(database_connection)
    make_test_note(database_connection, contact_id=contact.id, body="first")
    make_test_note(database_connection, contact_id=contact.id, body="second")
    notes = list_notes(database_connection, contact_id=contact.id, limit=1)
    assert len(notes) == 1


def test_list_notes_since(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from datetime import datetime, timedelta

    contact = make_test_contact(database_connection)
    make_test_note(database_connection, contact_id=contact.id, body="old")
    database_connection.execute(
        "UPDATE note SET created_at = CURRENT_TIMESTAMP - interval '2 days' "
        "WHERE body = 'old'"
    )
    database_connection.commit()
    make_test_note(database_connection, contact_id=contact.id, body="recent")
    since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    results = list_notes(database_connection, contact_id=contact.id, since=since)
    assert len(results) == 1
    assert results[0].body_preview == "recent"


def test_get_note(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = make_test_contact(database_connection)
    created = make_test_note(database_connection, contact_id=contact.id)
    found = get_note(database_connection, created.id)
    assert found is not None
    assert found.id == created.id
    assert found.body == "Test note body"


def test_get_note_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    found = get_note(database_connection, "nonexistent-id")
    assert found is None


def test_status_counts_includes_notes(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = make_test_contact(database_connection)
    make_test_note(database_connection, contact_id=contact.id)
    counts = get_status_counts(database_connection)
    assert counts["notes"] == 1


# -- Enrollment ---------------------------------------------------------------


def test_delete_enrollment(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    create_enrollment(database_connection, workflow.id, contact.id)
    deleted = delete_enrollment(database_connection, workflow.id, contact.id)
    assert deleted is True
    assert get_enrollment(database_connection, workflow.id, contact.id) is None


def test_delete_enrollment_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    deleted = delete_enrollment(database_connection, "nonexistent", "nonexistent")
    assert deleted is False


def test_list_enrollments_detailed(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection, email="alice@example.com")
    update_contact(
        database_connection, contact.id, first_name="Alice", last_name="Smith"
    )
    create_enrollment(database_connection, workflow.id, contact.id)
    results = list_enrollments_detailed(database_connection, workflow_id=workflow.id)
    assert len(results) == 1
    detail = results[0]
    assert detail.contact_email == "alice@example.com"
    assert detail.contact_name == "Alice Smith"
    assert detail.status == "active"
    assert detail.workflow_id == workflow.id
    assert detail.contact_id == contact.id


def test_list_enrollments_detailed_status_filter(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import update_enrollment

    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    c1 = make_test_contact(database_connection, email="a@example.com")
    c2 = make_test_contact(database_connection, email="b@example.com")
    create_enrollment(database_connection, workflow.id, c1.id)
    create_enrollment(database_connection, workflow.id, c2.id)
    update_enrollment(database_connection, workflow.id, c1.id, status="paused")
    results = list_enrollments_detailed(
        database_connection, workflow_id=workflow.id, status="paused"
    )
    assert len(results) == 1
    assert results[0].contact_id == c1.id


def test_create_enrollment_defaults_to_active(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Enrollment defaults to 'active' (status collapse, comment #4334976677)."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)

    enrollment = create_enrollment(
        database_connection, workflow_id=workflow.id, contact_id=contact.id
    )
    assert enrollment is not None
    assert enrollment.status == "active"


def test_update_enrollment_rejects_legacy_statuses(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """`completed`/`failed`/`pending` are no longer valid enrollment statuses."""
    from mailpilot.database import update_enrollment

    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    create_enrollment(
        database_connection, workflow_id=workflow.id, contact_id=contact.id
    )
    for bad in ("pending", "completed", "failed"):
        with pytest.raises((psycopg.errors.CheckViolation, ValueError)):
            update_enrollment(
                database_connection,
                workflow.id,
                contact.id,
                status=bad,
            )
        database_connection.rollback()


def test_list_enrollments_detailed_limit(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    c1 = make_test_contact(database_connection, email="a@example.com")
    c2 = make_test_contact(database_connection, email="b@example.com")
    create_enrollment(database_connection, workflow.id, c1.id)
    create_enrollment(database_connection, workflow.id, c2.id)
    results = list_enrollments_detailed(
        database_connection, workflow_id=workflow.id, limit=1
    )
    assert len(results) == 1


def test_list_enrollments_detailed_empty(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    results = list_enrollments_detailed(database_connection, workflow_id=workflow.id)
    assert results == []


def test_list_enrollments_detailed_filter_by_contact(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    wf_a = make_test_workflow(database_connection, account_id=account.id, name="wf-a")
    wf_b = make_test_workflow(database_connection, account_id=account.id, name="wf-b")
    contact = make_test_contact(database_connection, email="alice@example.com")
    create_enrollment(database_connection, wf_a.id, contact.id)
    create_enrollment(database_connection, wf_b.id, contact.id)
    results = list_enrollments_detailed(database_connection, contact_id=contact.id)
    assert len(results) == 2
    assert {r.workflow_id for r in results} == {wf_a.id, wf_b.id}


def test_list_enrollments_detailed_filter_by_workflow_and_contact(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    wf_a = make_test_workflow(database_connection, account_id=account.id, name="wf-a")
    wf_b = make_test_workflow(database_connection, account_id=account.id, name="wf-b")
    contact = make_test_contact(database_connection, email="alice@example.com")
    create_enrollment(database_connection, wf_a.id, contact.id)
    create_enrollment(database_connection, wf_b.id, contact.id)
    results = list_enrollments_detailed(
        database_connection, workflow_id=wf_a.id, contact_id=contact.id
    )
    assert len(results) == 1
    assert results[0].workflow_id == wf_a.id


# -- Task ----------------------------------------------------------------------


def test_list_tasks(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    create_task(
        database_connection,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="follow up",
        scheduled_at="2026-04-22T12:00:00Z",
    )
    create_task(
        database_connection,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="check reply",
        scheduled_at="2026-04-22T13:00:00Z",
    )
    results = list_tasks(database_connection)
    assert len(results) == 2


def test_list_tasks_with_filters(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact_a = make_test_contact(database_connection, email="a@test.com")
    contact_b = make_test_contact(database_connection, email="b@test.com")
    create_task(
        database_connection,
        workflow_id=workflow.id,
        contact_id=contact_a.id,
        description="task for A",
        scheduled_at="2026-04-22T12:00:00Z",
    )
    task_b = create_task(
        database_connection,
        workflow_id=workflow.id,
        contact_id=contact_b.id,
        description="task for B",
        scheduled_at="2026-04-22T13:00:00Z",
    )
    cancel_task(database_connection, task_b.id)

    by_contact = list_tasks(database_connection, contact_id=contact_a.id)
    assert len(by_contact) == 1
    assert by_contact[0].contact_id == contact_a.id

    cancelled = list_tasks(database_connection, status="cancelled")
    assert len(cancelled) == 1
    assert cancelled[0].contact_id == contact_b.id

    pending = list_tasks(database_connection, status="pending")
    assert len(pending) == 1
    assert pending[0].contact_id == contact_a.id


def test_create_tasks_for_routed_emails(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from datetime import timedelta

    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)

    email = create_email(
        database_connection,
        gmail_message_id="msg-001",
        gmail_thread_id="thread-001",
        account_id=account.id,
        direction="inbound",
        subject="Re: hello",
        body_text="Got it",
        labels=["INBOX"],
        received_at=workflow.created_at + timedelta(minutes=5),
        contact_id=contact.id,
        workflow_id=workflow.id,
    )
    assert email is not None

    created = create_tasks_for_routed_emails(database_connection)
    assert len(created) == 1
    assert created[0].email_id == email.id
    assert created[0].workflow_id == workflow.id
    assert created[0].contact_id == contact.id
    assert created[0].description == "handle inbound email"

    # Idempotent: second call creates no duplicates.
    again = create_tasks_for_routed_emails(database_connection)
    assert len(again) == 0


def test_create_tasks_for_routed_emails_skips_outbound(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from datetime import timedelta

    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)

    create_email(
        database_connection,
        gmail_message_id="msg-002",
        gmail_thread_id="thread-002",
        account_id=account.id,
        direction="outbound",
        subject="Hello",
        body_text="Hi there",
        labels=["SENT"],
        sent_at=workflow.created_at + timedelta(minutes=5),
        contact_id=contact.id,
        workflow_id=workflow.id,
    )

    created = create_tasks_for_routed_emails(database_connection)
    assert len(created) == 0


def test_create_tasks_for_routed_emails_skips_historical(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Emails stored in DB before the workflow was created should not be bridged."""
    from datetime import datetime, timedelta

    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)

    # Create email FIRST (simulates full sync storing historical email).
    pre_existing_email = create_email(
        database_connection,
        gmail_message_id="msg-hist",
        gmail_thread_id="thread-hist",
        account_id=account.id,
        direction="inbound",
        subject="Old message",
        body_text="From last month",
        labels=["INBOX"],
        received_at=datetime.now(UTC) - timedelta(days=30),
        contact_id=contact.id,
    )
    assert pre_existing_email is not None

    # Create workflow AFTER the email was stored.
    workflow = make_test_workflow(database_connection, account_id=account.id)

    # Simulate routing: set workflow_id on the pre-existing email.
    database_connection.execute(
        "UPDATE email SET workflow_id = %s WHERE id = %s",
        (workflow.id, pre_existing_email.id),
    )
    database_connection.commit()

    # Email stored AFTER the workflow was created -- should be bridged.
    recent_email = create_email(
        database_connection,
        gmail_message_id="msg-recent",
        gmail_thread_id="thread-recent",
        account_id=account.id,
        direction="inbound",
        subject="New message",
        body_text="Just now",
        labels=["INBOX"],
        received_at=datetime.now(UTC),
        contact_id=contact.id,
        workflow_id=workflow.id,
    )
    assert recent_email is not None

    created = create_tasks_for_routed_emails(database_connection)
    assert len(created) == 1
    assert created[0].email_id == recent_email.id


def test_create_tasks_for_routed_emails_bridges_email_synced_after_workflow(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Email received by Gmail before workflow but synced after should be bridged.

    This is the race condition from the smoke test: outbound sends email,
    then inbound workflow is created, then sync stores the email. The email's
    received_at (Gmail timestamp) predates the workflow, but created_at
    (DB insert time) is after the workflow.
    """
    from datetime import timedelta

    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)

    # Email received by Gmail BEFORE workflow, but synced/stored AFTER.
    # created_at is auto-set to now() which is after workflow.created_at.
    email = create_email(
        database_connection,
        gmail_message_id="msg-race",
        gmail_thread_id="thread-race",
        account_id=account.id,
        direction="inbound",
        subject="Recent email with old Gmail timestamp",
        body_text="Arrived just before workflow was created",
        labels=["INBOX"],
        received_at=workflow.created_at - timedelta(seconds=17),
        contact_id=contact.id,
        workflow_id=workflow.id,
    )
    assert email is not None

    created = create_tasks_for_routed_emails(database_connection)
    assert len(created) == 1
    assert created[0].email_id == email.id


def test_get_unprocessed_inbound_email(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from datetime import timedelta

    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)

    # No emails yet -- returns None.
    result = get_unprocessed_inbound_email(database_connection, workflow.id, contact.id)
    assert result is None

    # Create an inbound email for this contact+workflow.
    email = create_email(
        database_connection,
        gmail_message_id="msg-unproc-1",
        gmail_thread_id="thread-unproc-1",
        account_id=account.id,
        direction="inbound",
        subject="Question",
        body_text="Can you help?",
        labels=["INBOX"],
        received_at=workflow.created_at + timedelta(minutes=5),
        contact_id=contact.id,
        workflow_id=workflow.id,
    )
    assert email is not None

    # Now returns the email.
    result = get_unprocessed_inbound_email(database_connection, workflow.id, contact.id)
    assert result is not None
    assert result.id == email.id

    # Create a task for that email -- it becomes "processed".
    create_task(
        database_connection,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="handle inbound email",
        scheduled_at="2026-04-22T12:00:00Z",
        email_id=email.id,
    )

    # Now returns None (email has a task).
    result = get_unprocessed_inbound_email(database_connection, workflow.id, contact.id)
    assert result is None


def test_get_unprocessed_inbound_email_returns_most_recent(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from datetime import timedelta

    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)

    older = create_email(
        database_connection,
        gmail_message_id="msg-older",
        gmail_thread_id="thread-older",
        account_id=account.id,
        direction="inbound",
        subject="First",
        body_text="First msg",
        labels=["INBOX"],
        received_at=workflow.created_at + timedelta(minutes=1),
        contact_id=contact.id,
        workflow_id=workflow.id,
    )
    assert older is not None
    newer = create_email(
        database_connection,
        gmail_message_id="msg-newer",
        gmail_thread_id="thread-newer",
        account_id=account.id,
        direction="inbound",
        subject="Second",
        body_text="Second msg",
        labels=["INBOX"],
        received_at=workflow.created_at + timedelta(minutes=10),
        contact_id=contact.id,
        workflow_id=workflow.id,
    )
    assert newer is not None

    result = get_unprocessed_inbound_email(database_connection, workflow.id, contact.id)
    assert result is not None
    assert result.id == newer.id


def test_complete_task_stores_result(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    task = create_task(
        database_connection,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="follow up",
        scheduled_at="2026-04-22T12:00:00Z",
    )
    agent_result: dict[str, object] = {
        "reasoning": "Contact hasn't replied in 3 days, sending follow-up.",
        "tool_calls": 1,
    }
    completed = complete_task(
        database_connection, task.id, status="completed", result=agent_result
    )
    assert completed is not None
    assert completed.status == "completed"
    assert completed.result["reasoning"] == agent_result["reasoning"]
    assert completed.result["tool_calls"] == agent_result["tool_calls"]
    assert completed.completed_at is not None


# -- List vs view contract -----------------------------------------------------
#
# Per ADR / CLAUDE.md "list (summary), view ID (full record)" rule:
# every `list_*` returns the matching `<Entity>Summary` projection (a strict
# subset of the full model), and every `get_*` returns the full domain model.
# These tests pin the contract so accidental field additions to a Summary
# (or accidental field reads after a `list_*`) are caught at test time.


def test_account_list_summary_get_full(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.models import AccountSummary

    make_test_account(database_connection)
    accounts = list_accounts(database_connection)
    assert isinstance(accounts[0], AccountSummary)
    assert not hasattr(accounts[0], "gmail_history_id")
    full = get_account(database_connection, accounts[0].id)
    assert full is not None
    assert hasattr(full, "gmail_history_id")


def test_company_list_summary_get_full(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.models import CompanySummary

    make_test_company(database_connection)
    companies = list_companies(database_connection)
    assert isinstance(companies[0], CompanySummary)
    assert not hasattr(companies[0], "profile_summary")
    full = get_company(database_connection, companies[0].id)
    assert full is not None
    assert hasattr(full, "profile_summary")


def test_contact_list_summary_get_full(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.models import ContactSummary

    make_test_contact(database_connection)
    contacts = list_contacts(database_connection)
    assert isinstance(contacts[0], ContactSummary)
    assert not hasattr(contacts[0], "position")
    full = get_contact(database_connection, contacts[0].id)
    assert full is not None
    assert hasattr(full, "position")


def test_workflow_list_summary_get_full(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.models import WorkflowSummary

    account = make_test_account(database_connection)
    make_test_workflow(database_connection, account_id=account.id)
    workflows = list_workflows(database_connection)
    assert isinstance(workflows[0], WorkflowSummary)
    assert not hasattr(workflows[0], "objective")
    full = get_workflow(database_connection, workflows[0].id)
    assert full is not None
    assert hasattr(full, "objective")


def test_enrollment_list_summary_drops_reason_and_created_at(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.models import EnrollmentSummary

    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    create_enrollment(database_connection, workflow.id, contact.id)
    rows = list_enrollments_detailed(database_connection, workflow_id=workflow.id)
    assert isinstance(rows[0], EnrollmentSummary)
    assert not hasattr(rows[0], "reason")
    assert not hasattr(rows[0], "created_at")


def test_email_list_summary_get_full(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.models import EmailSummary

    account = make_test_account(database_connection)
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="hi",
        body_text="body",
        status="sent",
        recipients={"to": ["x@y.com"]},
    )
    emails = list_emails(database_connection, account_id=account.id)
    assert isinstance(emails[0], EmailSummary)
    assert not hasattr(emails[0], "body_text")
    assert not hasattr(emails[0], "recipients")
    assert not hasattr(emails[0], "labels")
    full = get_email(database_connection, emails[0].id)
    assert full is not None
    assert full.body_text == "body"
    assert "x@y.com" in full.recipients["to"]


def test_company_search_summary_get_full(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.models import CompanySummary

    make_test_company(database_connection, name="Acme Corp", domain="acme.com")
    companies = search_companies(database_connection, "acme")
    assert isinstance(companies[0], CompanySummary)
    assert not hasattr(companies[0], "profile_summary")
    full = get_company(database_connection, companies[0].id)
    assert full is not None
    assert hasattr(full, "profile_summary")


def test_contact_search_summary_get_full(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.models import ContactSummary

    make_test_contact(database_connection, email="alice@example.com")
    contacts = search_contacts(database_connection, "alice")
    assert isinstance(contacts[0], ContactSummary)
    assert not hasattr(contacts[0], "position")
    full = get_contact(database_connection, contacts[0].id)
    assert full is not None
    assert hasattr(full, "position")


def test_workflow_search_summary_get_full(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.models import WorkflowSummary

    account = make_test_account(database_connection)
    make_test_workflow(database_connection, account_id=account.id, name="Outreach")
    workflows = search_workflows(database_connection, "outreach")
    assert isinstance(workflows[0], WorkflowSummary)
    assert not hasattr(workflows[0], "objective")
    full = get_workflow(database_connection, workflows[0].id)
    assert full is not None
    assert hasattr(full, "objective")


def test_email_search_summary_get_full(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.models import EmailSummary

    account = make_test_account(database_connection)
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="Meeting Request",
        body_text="Let's schedule a call",
        status="sent",
        recipients={"to": ["client@example.com"]},
    )
    emails = search_emails(database_connection, "meeting")
    assert isinstance(emails[0], EmailSummary)
    assert not hasattr(emails[0], "body_text")
    assert not hasattr(emails[0], "recipients")
    assert not hasattr(emails[0], "labels")
    full = get_email(database_connection, emails[0].id)
    assert full is not None
    assert full.body_text == "Let's schedule a call"
    assert "client@example.com" in full.recipients["to"]


def test_task_list_summary(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.models import TaskSummary

    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    create_task(
        database_connection,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="follow up",
        scheduled_at="2024-01-01T00:00:00+00:00",
    )
    tasks = list_tasks(database_connection, workflow_id=workflow.id)
    assert isinstance(tasks[0], TaskSummary)
    assert not hasattr(tasks[0], "context")
    assert not hasattr(tasks[0], "result")


def test_activity_list_summary(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.models import ActivitySummary

    contact = make_test_contact(database_connection)
    create_activity(
        database_connection,
        contact_id=contact.id,
        activity_type="email_sent",
        summary="sent X",
        detail={"id": "abc"},
    )
    activities = list_activities(database_connection, contact_id=contact.id)
    assert isinstance(activities[0], ActivitySummary)
    assert not hasattr(activities[0], "detail")


def test_note_list_summary_with_body_preview(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.models import NoteSummary

    contact = make_test_contact(database_connection)
    make_test_note(database_connection, contact_id=contact.id, body="short")
    make_test_note(database_connection, contact_id=contact.id, body="x" * 200)
    notes = list_notes(database_connection, contact_id=contact.id)
    assert isinstance(notes[0], NoteSummary)
    assert not hasattr(notes[0], "body")
    # Long body truncated to 80 chars + "..." (ordered DESC, long one is first).
    assert notes[0].body_preview == ("x" * 80) + "..."
    # Short body returned verbatim with no ellipsis.
    assert notes[1].body_preview == "short"
    full = get_note(database_connection, notes[0].id)
    assert full is not None
    assert full.body == "x" * 200


def test_list_accounts_limit_and_since(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    make_test_account(database_connection, email="a@test.com")
    make_test_account(database_connection, email="b@test.com")
    assert len(list_accounts(database_connection, limit=1)) == 1
    assert len(list_accounts(database_connection, since="9999-01-01T00:00:00")) == 0


def test_list_workflows_limit_and_since(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    make_test_workflow(database_connection, account_id=account.id, name="A")
    make_test_workflow(database_connection, account_id=account.id, name="B")
    assert len(list_workflows(database_connection, limit=1)) == 1
    assert len(list_workflows(database_connection, since="9999-01-01T00:00:00")) == 0


def test_list_companies_since(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    make_test_company(database_connection)
    assert len(list_companies(database_connection, since="9999-01-01T00:00:00")) == 0


def test_list_contacts_since(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    make_test_contact(database_connection)
    assert len(list_contacts(database_connection, since="9999-01-01T00:00:00")) == 0


def test_list_tasks_since(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(
        database_connection, account_id=make_test_account(database_connection).id
    )
    create_task(
        database_connection,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="x",
        scheduled_at="2020-01-01T00:00:00+00:00",
    )
    assert (
        len(
            list_tasks(
                database_connection,
                workflow_id=workflow.id,
                since="2030-01-01T00:00:00",
            )
        )
        == 0
    )


def test_list_tags_limit_and_since(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    contact = make_test_contact(database_connection)
    make_test_tag(database_connection, contact_id=contact.id, name="a")
    make_test_tag(database_connection, contact_id=contact.id, name="b")
    assert (
        len(list_tags(database_connection, contact_id=contact.id, limit=1)) == 1
    )
    assert (
        len(
            list_tags(
                database_connection,
                contact_id=contact.id,
                since="9999-01-01T00:00:00",
            )
        )
        == 0
    )


def test_list_enrollments_detailed_since(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    create_enrollment(database_connection, workflow.id, contact.id)
    assert (
        len(
            list_enrollments_detailed(
                database_connection,
                workflow_id=workflow.id,
                since="9999-01-01T00:00:00",
            )
        )
        == 0
    )
