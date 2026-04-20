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
    make_test_workflow,
)
from mailpilot.database import (
    activate_workflow,
    create_activity,
    create_contacts_bulk,
    create_email,
    create_or_get_contact_by_email,
    get_account,
    get_company,
    get_company_by_domain,
    get_contact,
    get_contact_by_email,
    get_contacts_by_emails,
    get_email,
    get_email_by_gmail_message_id,
    get_emails_by_gmail_thread_id,
    get_last_cold_outbound,
    get_status_counts,
    get_workflow,
    list_accounts,
    list_activities,
    list_companies,
    list_contacts,
    list_emails,
    list_workflows,
    pause_workflow,
    search_companies,
    search_contacts,
    search_emails,
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

    # Older cold outbound (first in its thread).
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="old pitch",
        contact_id=contact.id,
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
        gmail_message_id="new-msg",
        gmail_thread_id="thread-new",
        status="sent",
    )
    assert newer is not None

    result = get_last_cold_outbound(database_connection, account.id, contact.id)
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

    # First outbound in thread (cold).
    cold = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="initial pitch",
        contact_id=contact.id,
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
        gmail_message_id="followup-msg",
        gmail_thread_id="thread-1",
        status="sent",
    )

    result = get_last_cold_outbound(database_connection, account.id, contact.id)
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

    create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="hello",
        contact_id=contact.id,
    )

    result = get_last_cold_outbound(database_connection, account.id, contact.id)
    assert result is None


def test_get_last_cold_outbound_none_when_no_emails(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="r@example.com", domain="example.com"
    )

    result = get_last_cold_outbound(database_connection, account.id, contact.id)
    assert result is None


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


def test_status_counts_includes_activities(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = make_test_contact(database_connection)
    make_test_activity(database_connection, contact_id=contact.id)
    counts = get_status_counts(database_connection)
    assert counts["activities"] == 1
