"""Integration tests for database CRUD operations (real DB)."""

import threading
from datetime import UTC, datetime
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
    make_test_enrollment,
    make_test_note,
    make_test_tag,
    make_test_tag_assignment,
    make_test_workflow,
)
from mailpilot.database import (
    activate_workflow,
    cancel_enrollment_followup_tasks,
    cancel_task,
    check_workflow_wording,
    company_import_diff,
    complete_task,
    create_account,
    create_activity,
    create_company,
    create_contact,
    create_contacts_bulk,
    create_email,
    create_enrollment,
    create_meeting,
    create_or_get_contact_by_email,
    create_tag,
    create_task,
    create_tasks_for_routed_emails,
    create_workflow,
    disable_account,
    disable_company,
    disable_contact,
    disable_enrollment,
    disable_tag,
    enable_account,
    enable_company,
    enable_enrollment,
    export_companies,
    export_snapshot,
    find_pending_first_touch_task,
    get_account,
    get_account_by_email,
    get_company,
    get_company_by_domain,
    get_company_by_domain_exact,
    get_contact,
    get_contact_by_email,
    get_contacts_by_emails,
    get_email,
    get_email_by_gmail_message_id,
    get_emails_by_gmail_thread_id,
    get_enrollment,
    get_last_cold_outbound,
    get_latest_email_in_thread,
    get_latest_enrollment_outcome,
    get_meeting,
    get_meeting_by_google_event_id,
    get_note,
    get_status_payload,
    get_task,
    get_task_stats,
    get_unprocessed_inbound_email,
    get_workflow,
    get_workflow_by_name,
    get_workflow_stats,
    has_inbound_email_from_contact_after,
    import_snapshot,
    link_meeting_attendee,
    list_accounts,
    list_active_outbound_enrollments_for_contact,
    list_activities,
    list_companies,
    list_company_aliases,
    list_contacts,
    list_emails,
    list_enrollments_detailed,
    list_meeting_attendees,
    list_meetings,
    list_notes,
    list_tags,
    list_tasks,
    list_workflows,
    list_workflows_full,
    load_company_view,
    manual_retry_task,
    merge_companies,
    pause_workflow,
    record_enrollment_outcome,
    reschedule_task_for_retry,
    search_companies,
    search_contacts,
    search_emails,
    search_tags,
    search_workflows,
    update_account,
    update_company,
    update_contact,
    update_email,
    update_meeting,
    update_workflow,
    upsert_meeting,
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


def test_disable_account_sets_reason(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.118: disable_account writes disabled_reason verbatim."""
    account = make_test_account(database_connection)
    updated = disable_account(database_connection, account.id, "out of business")
    assert updated is not None
    assert updated.disabled_reason == "out of business"
    fetched = get_account(database_connection, account.id)
    assert fetched is not None
    assert fetched.disabled_reason == "out of business"


def test_disable_account_double_disable_gate_returns_none(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.118: the disabled_reason IS NULL gate blocks double-disable.

    A second disable does not match the already-disabled row, returns None,
    and leaves the first reason intact (mirror of §V.114 company disable).
    """
    account = make_test_account(database_connection)
    assert disable_account(database_connection, account.id, "first") is not None
    second = disable_account(database_connection, account.id, "second")
    assert second is None
    fetched = get_account(database_connection, account.id)
    assert fetched is not None
    assert fetched.disabled_reason == "first"


def test_disable_account_not_found_returns_none(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.118: disabling a missing account returns None."""
    assert disable_account(database_connection, "nonexistent", "reason") is None


def test_list_accounts_excludes_disabled_by_default(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.118: disabled accounts drop out of `account list` by default.

    A disabled account is gated out of the sync loop / watch renewal (which
    read this listing); include_disabled=True surfaces it with its reason.
    """
    active = make_test_account(database_connection, email="active@test.com")
    disabled = make_test_account(database_connection, email="disabled@test.com")
    disable_account(database_connection, disabled.id, "out of business")

    default = list_accounts(database_connection)
    assert {a.id for a in default} == {active.id}

    everyone = list_accounts(database_connection, include_disabled=True)
    by_id = {a.id: a for a in everyone}
    assert set(by_id) == {active.id, disabled.id}
    assert by_id[disabled.id].disabled_reason == "out of business"
    assert by_id[active.id].disabled_reason is None


def test_enable_account_clears_reason(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.118: enable_account clears disabled_reason; the account relists."""
    account = make_test_account(database_connection)
    disable_account(database_connection, account.id, "out of business")
    reenabled = enable_account(database_connection, account.id)
    assert reenabled is not None
    assert reenabled.disabled_reason is None
    fetched = get_account(database_connection, account.id)
    assert fetched is not None
    assert fetched.disabled_reason is None
    assert {a.id for a in list_accounts(database_connection)} == {account.id}


def test_enable_account_gate_blocks_active(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.118: the disabled_reason IS NOT NULL gate blocks enabling an active
    account (mirror of the double-disable gate)."""
    account = make_test_account(database_connection)
    assert enable_account(database_connection, account.id) is None


def test_enable_account_not_found_returns_none(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.118: enabling a missing account returns None."""
    assert enable_account(database_connection, "nonexistent") is None


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
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

    listen_conn = cast(
        psycopg.Connection[dict[str, Any]],
        psycopg.connect(TEST_DATABASE_URL, row_factory=dict_row, autocommit=True),  # type: ignore[arg-type]
    )
    try:
        listen_conn.execute("LISTEN task_pending")
        create_task(
            database_connection,
            enrollment_id=enrollment.id,
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
    assert companies[0].has_profile is False
    assert companies[0].tags == []
    assert companies[0].disabled_reason is None
    assert companies[0].profile is None
    assert companies[1].has_profile is False


def test_list_companies_sort_domain_desc(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.148: --sort domain --desc orders by lowercased domain descending."""
    make_test_company(database_connection, name="A Co", domain="aaa.com")
    make_test_company(database_connection, name="Z Co", domain="zzz.com")
    make_test_company(database_connection, name="M Co", domain="mmm.com")

    rows = list_companies(database_connection, sort="domain", desc=True)
    assert [c.domain for c in rows] == ["zzz.com", "mmm.com", "aaa.com"]


def test_list_companies_offset_limit_page(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.148: offset+limit page over stable name order."""
    make_test_company(database_connection, name="A", domain="a.com")
    make_test_company(database_connection, name="B", domain="b.com")
    make_test_company(database_connection, name="C", domain="c.com")

    page = list_companies(database_connection, limit=1, offset=1, sort="name")
    assert len(page) == 1
    assert page[0].name == "B"


def test_list_companies_sort_contact_count(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.148: sort by contact_count ascending."""
    from mailpilot.database import create_contact

    zero = make_test_company(database_connection, name="Zero", domain="zero.com")
    two = make_test_company(database_connection, name="Two", domain="two.com")
    create_contact(database_connection, email="a@two.com", company_id=two.id)
    create_contact(database_connection, email="b@two.com", company_id=two.id)
    _ = zero

    rows = list_companies(database_connection, sort="contact_count", desc=False)
    assert rows[0].domain == "zero.com"
    assert rows[0].contact_count == 0
    assert rows[-1].domain == "two.com"
    assert rows[-1].contact_count == 2


def test_list_companies_projects_tags(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.8/§V.116: list rows project assigned tag names (empty ok)."""
    company = make_test_company(database_connection, name="Tagged", domain="tagged.com")
    bare = make_test_company(database_connection, name="Bare", domain="bare.com")
    make_test_tag_assignment(database_connection, company_id=company.id, name="vip")
    make_test_tag_assignment(
        database_connection, company_id=company.id, name="acumatica-var"
    )

    by_id = {c.id: c for c in list_companies(database_connection)}

    assert by_id[company.id].tags == ["acumatica-var", "vip"]
    assert by_id[bare.id].tags == []


def test_list_companies_full_embeds_profile_summary_only(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.8: --full embeds profile.summary only; default leaves profile null."""
    null_id, full_id, _partial_id = _seed_profile_fixture(database_connection)

    lean = {c.id: c for c in list_companies(database_connection)}
    assert lean[full_id].profile is None
    assert lean[null_id].profile is None

    full_rows = {c.id: c for c in list_companies(database_connection, full=True)}
    assert full_rows[null_id].profile is None
    assert full_rows[full_id].profile == {"summary": "Full Co builds widgets."}
    assert "products" not in (full_rows[full_id].profile or {})


def test_list_companies_disabled_reason_on_include_disabled(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.114: disabled_reason is always on list rows when the row is returned."""
    from mailpilot.database import disable_company

    company = make_test_company(database_connection, name="Gone", domain="gone.com")
    disable_company(database_connection, company.id, reason="absorbed-brand")

    rows = list_companies(database_connection, include_disabled=True)
    by_id = {c.id: c for c in rows}
    assert by_id[company.id].disabled_reason == "absorbed-brand"


def _seed_profile_fixture(
    connection: psycopg.Connection[dict[str, Any]],
) -> tuple[str, str, str]:
    """Seed 3 companies: NULL profile, full profile, partial profile.

    Returns the (null_id, full_id, partial_id) tuple. Full and partial profiles
    are both valid per §V.72 (timezone is the only optional field).
    """
    null_co = make_test_company(connection, name="Null", domain="null.com")
    full_co = make_test_company(connection, name="Full", domain="full.com")
    partial_co = make_test_company(connection, name="Partial", domain="partial.com")
    update_company(
        connection,
        full_co.id,
        profile={
            "summary": "Full Co builds widgets.",
            "products": ["Widget A", "Widget B"],
            "target_customers": "Mid-market manufacturers.",
            "timezone": "America/Toronto",
            "sources": ["https://full.com/"],
        },
    )
    update_company(
        connection,
        partial_co.id,
        profile={
            "summary": "Partial Co builds gizmos.",
            "products": ["Gizmo"],
            "target_customers": "SMB.",
            "sources": ["https://partial.com/"],
        },
    )
    return null_co.id, full_co.id, partial_co.id


def test_list_companies_has_profile_projection(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    null_id, full_id, partial_id = _seed_profile_fixture(database_connection)
    companies = list_companies(database_connection)
    by_id = {c.id: c for c in companies}
    assert by_id[null_id].has_profile is False
    assert by_id[full_id].has_profile is True
    assert by_id[partial_id].has_profile is True


def test_list_companies_filter_has_profile_true(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    null_id, full_id, partial_id = _seed_profile_fixture(database_connection)
    companies = list_companies(database_connection, has_profile=True)
    ids = {c.id for c in companies}
    assert ids == {full_id, partial_id}
    assert null_id not in ids


def test_list_companies_filter_has_profile_false(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    null_id, full_id, partial_id = _seed_profile_fixture(database_connection)
    companies = list_companies(database_connection, has_profile=False)
    ids = {c.id for c in companies}
    assert ids == {null_id}
    assert full_id not in ids
    assert partial_id not in ids


def test_list_companies_contact_count_includes_disabled(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.96: contact_count is a child COUNT that INCLUDES disabled contacts.

    The count tracks the discovery-memoization rule (idempotent re-run skips a
    known-bad, since-disabled address), so a disabled contact must still count
    — an active-only COUNT would re-admit it as a phantom gap.
    """
    from mailpilot.database import create_contact, disable_contact

    empty = make_test_company(database_connection, name="Empty", domain="empty.com")
    populated = make_test_company(database_connection, name="Pop", domain="pop.com")
    create_contact(database_connection, email="a@pop.com", company_id=populated.id)
    bounced = create_contact(
        database_connection, email="b@pop.com", company_id=populated.id
    )
    assert bounced is not None
    disable_contact(database_connection, bounced.id, reason="bounced: hard bounce")

    by_id = {c.id: c for c in list_companies(database_connection)}
    assert by_id[empty.id].contact_count == 0
    assert by_id[populated.id].contact_count == 2


def test_list_companies_max_contacts_inclusive(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.96: --max-contacts N is an inclusive upper bound (count <= N)."""
    from mailpilot.database import create_contact

    zero = make_test_company(database_connection, name="Zero", domain="zero.com")
    one = make_test_company(database_connection, name="One", domain="one.com")
    two = make_test_company(database_connection, name="Two", domain="two.com")
    create_contact(database_connection, email="a@one.com", company_id=one.id)
    create_contact(database_connection, email="a@two.com", company_id=two.id)
    create_contact(database_connection, email="b@two.com", company_id=two.id)

    surfaced = list_companies(database_connection, max_contacts=1)
    assert {c.id for c in surfaced} == {zero.id, one.id}


def test_list_companies_min_contacts_inclusive(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.96: --min-contacts N is an inclusive lower bound (count >= N)."""
    from mailpilot.database import create_contact

    make_test_company(database_connection, name="Zero", domain="zero.com")
    one = make_test_company(database_connection, name="One", domain="one.com")
    two = make_test_company(database_connection, name="Two", domain="two.com")
    create_contact(database_connection, email="a@one.com", company_id=one.id)
    create_contact(database_connection, email="a@two.com", company_id=two.id)
    create_contact(database_connection, email="b@two.com", company_id=two.id)

    surfaced = list_companies(database_connection, min_contacts=1)
    assert {c.id for c in surfaced} == {one.id, two.id}


def test_list_companies_min_max_contacts_compose(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.96: min + max compose into a closed inclusive range on contact_count."""
    from mailpilot.database import create_contact

    make_test_company(database_connection, name="Zero", domain="zero.com")
    one = make_test_company(database_connection, name="One", domain="one.com")
    three = make_test_company(database_connection, name="Three", domain="three.com")
    create_contact(database_connection, email="a@one.com", company_id=one.id)
    for i in range(3):
        create_contact(
            database_connection, email=f"c{i}@three.com", company_id=three.id
        )

    surfaced = list_companies(database_connection, min_contacts=1, max_contacts=2)
    assert {c.id for c in surfaced} == {one.id}


def test_list_companies_discover_set_one_query(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.96: --has-profile + --max-contacts 4 == lead-contacts discover set.

    Profile-bearing companies with fewer than 5 contacts, in one query: the
    no-profile row and the at-cap (5-contact) row are both excluded.
    """
    from mailpilot.database import create_contact

    no_profile = make_test_company(
        database_connection, name="NoProfile", domain="noprofile.com"
    )
    create_contact(
        database_connection, email="x@noprofile.com", company_id=no_profile.id
    )
    under_cap = make_test_company(
        database_connection, name="UnderCap", domain="undercap.com"
    )
    at_cap = make_test_company(database_connection, name="AtCap", domain="atcap.com")
    for company in (under_cap, at_cap):
        update_company(
            database_connection,
            company.id,
            profile={
                "summary": "Co.",
                "products": ["P1"],
                "target_customers": "Enterprise.",
                "sources": ["https://x/"],
            },
        )
    create_contact(database_connection, email="a@undercap.com", company_id=under_cap.id)
    for i in range(5):
        create_contact(
            database_connection, email=f"c{i}@atcap.com", company_id=at_cap.id
        )

    discover = list_companies(database_connection, has_profile=True, max_contacts=4)
    assert {c.id for c in discover} == {under_cap.id}


def test_disable_company_sets_reason(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.114: disable_company writes disabled_reason verbatim."""
    company = make_test_company(database_connection)
    updated = disable_company(
        database_connection, company.id, "no_contacts_found:2026-06-18"
    )
    assert updated is not None
    assert updated.disabled_reason == "no_contacts_found:2026-06-18"
    fetched = get_company(database_connection, company.id)
    assert fetched is not None
    assert fetched.disabled_reason == "no_contacts_found:2026-06-18"


def test_disable_company_double_disable_gate_returns_none(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.114: the disabled_reason IS NULL gate blocks double-disable.

    A second disable does not match the already-disabled row, returns None,
    and leaves the first reason intact (mirrors §V.10 tag disable).
    """
    company = make_test_company(database_connection)
    assert disable_company(database_connection, company.id, "first reason") is not None
    second = disable_company(database_connection, company.id, "second reason")
    assert second is None
    fetched = get_company(database_connection, company.id)
    assert fetched is not None
    assert fetched.disabled_reason == "first reason"


def test_disable_company_not_found_returns_none(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.114: disabling a missing company returns None."""
    assert disable_company(database_connection, "nonexistent", "reason") is None


def test_list_companies_excludes_disabled_by_default(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.114/§V.96: disabled companies drop out of `company list` by default.

    A disabled company is out of the lead-contacts discover set; passing
    include_disabled=True surfaces it again with its reason projected.
    """
    active = make_test_company(database_connection, name="Active", domain="active.com")
    disabled = make_test_company(
        database_connection, name="Disabled", domain="disabled.com"
    )
    disable_company(database_connection, disabled.id, "no_contacts_found:2026-06-18")

    default = list_companies(database_connection)
    assert {c.id for c in default} == {active.id}

    everyone = list_companies(database_connection, include_disabled=True)
    by_id = {c.id: c for c in everyone}
    assert set(by_id) == {active.id, disabled.id}
    assert by_id[disabled.id].disabled_reason == "no_contacts_found:2026-06-18"
    assert by_id[active.id].disabled_reason is None


_PIPELINE_PROFILE: dict[str, Any] = {
    "summary": "Pipeline test company.",
    "products": ["widget"],
    "target_customers": "Enterprise.",
    "sources": ["https://example.com/"],
}


def _seed_pipeline_cohort_companies(
    connection: psycopg.Connection[dict[str, Any]],
) -> dict[str, str]:
    """Seed one company per pipeline status bucket; return domain->id map."""
    from mailpilot.database import create_contact

    ready = make_test_company(connection, name="Ready Co", domain="ready.com")
    update_company(connection, ready.id, profile=_PIPELINE_PROFILE)
    create_contact(connection, email="a@ready.com", company_id=ready.id)

    needs_contacts = make_test_company(
        connection, name="Needs Contacts", domain="needs-contacts.com"
    )
    update_company(connection, needs_contacts.id, profile=_PIPELINE_PROFILE)

    needs_profile = make_test_company(
        connection, name="Needs Profile", domain="needs-profile.com"
    )

    disabled = make_test_company(
        connection, name="Disabled Co", domain="disabled-co.com"
    )
    update_company(connection, disabled.id, profile=_PIPELINE_PROFILE)
    create_contact(connection, email="a@disabled-co.com", company_id=disabled.id)
    disable_company(connection, disabled.id, "absorbed-brand")

    return {
        "ready": ready.id,
        "needs_contacts": needs_contacts.id,
        "needs_profile": needs_profile.id,
        "disabled": disabled.id,
    }


def test_list_companies_status_ready(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.138: status=ready = profile + contact_count >= 1 + not disabled."""
    ids = _seed_pipeline_cohort_companies(database_connection)
    surfaced = list_companies(database_connection, status="ready")
    assert {c.id for c in surfaced} == {ids["ready"]}


def test_list_companies_status_needs_contacts(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.138: status=needs_contacts = profile + contact_count = 0 + not disabled."""
    ids = _seed_pipeline_cohort_companies(database_connection)
    surfaced = list_companies(database_connection, status="needs_contacts")
    assert {c.id for c in surfaced} == {ids["needs_contacts"]}


def test_list_companies_status_needs_profile(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.138: status=needs_profile = no profile + not disabled."""
    ids = _seed_pipeline_cohort_companies(database_connection)
    surfaced = list_companies(database_connection, status="needs_profile")
    assert {c.id for c in surfaced} == {ids["needs_profile"]}


def test_list_companies_status_disabled(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.138: status=disabled = disabled_reason set; overrides default hide."""
    ids = _seed_pipeline_cohort_companies(database_connection)
    # Without include_disabled — status alone surfaces the disabled row.
    surfaced = list_companies(database_connection, status="disabled")
    assert {c.id for c in surfaced} == {ids["disabled"]}
    assert surfaced[0].disabled_reason == "absorbed-brand"


def test_list_companies_status_composes_with_tag_and_min_contacts(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.138: status AND-composes with --tag and --min-contacts."""
    from mailpilot.database import create_contact

    ids = _seed_pipeline_cohort_companies(database_connection)
    # Second ready company without the tag — status=ready alone would include it.
    other_ready = make_test_company(
        database_connection, name="Other Ready", domain="other-ready.com"
    )
    update_company(database_connection, other_ready.id, profile=_PIPELINE_PROFILE)
    create_contact(
        database_connection, email="a@other-ready.com", company_id=other_ready.id
    )
    assignment = make_test_tag_assignment(
        database_connection, company_id=ids["ready"], name="vip"
    )

    surfaced = list_companies(
        database_connection,
        status="ready",
        tag=assignment.tag_id,
        min_contacts=1,
    )
    assert {c.id for c in surfaced} == {ids["ready"]}


def test_export_companies_stable_shape_and_domain_order(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.145: tracker export keys, domain ASC, tags sorted, no limit."""
    beta = make_test_company(database_connection, name="Beta Co", domain="beta.com")
    make_test_company(database_connection, name="Alpha Co", domain="alpha.com")
    make_test_tag_assignment(database_connection, company_id=beta.id, name="vip")
    make_test_tag_assignment(
        database_connection, company_id=beta.id, name="acumatica-var"
    )

    rows = export_companies(database_connection)

    assert [r["domain"] for r in rows] == ["alpha.com", "beta.com"]
    assert set(rows[0].keys()) == {
        "domain",
        "name",
        "tags",
        "has_profile",
        "contact_count",
        "disabled_reason",
    }
    assert rows[0]["name"] == "Alpha Co"
    assert rows[0]["has_profile"] is False
    assert rows[0]["contact_count"] == 0
    assert rows[0]["disabled_reason"] is None
    assert rows[0]["tags"] == []
    assert rows[1]["tags"] == ["acumatica-var", "vip"]
    assert "profile" not in rows[0]
    assert "id" not in rows[0]


def test_export_companies_full_embeds_full_profile(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.145: --full embeds full profile object or null (not summary-only)."""
    _seed_profile_fixture(database_connection)
    rows = {r["domain"]: r for r in export_companies(database_connection, full=True)}
    assert rows["null.com"]["profile"] is None
    profile = rows["full.com"]["profile"]
    assert profile is not None
    assert profile["summary"] == "Full Co builds widgets."
    assert profile["products"] == ["Widget A", "Widget B"]
    assert "sources" in profile


def test_export_companies_filters_status_and_hides_disabled(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.145/§V.138: filters compose; default hides disabled."""
    from mailpilot.database import create_contact, disable_company

    ready = make_test_company(database_connection, name="Ready", domain="ready.com")
    update_company(database_connection, ready.id, profile=_PIPELINE_PROFILE)
    create_contact(database_connection, email="a@ready.com", company_id=ready.id)
    bare = make_test_company(database_connection, name="Bare", domain="bare.com")
    disable_company(database_connection, bare.id, reason="absorbed-brand")

    default = export_companies(database_connection)
    assert {r["domain"] for r in default} == {"ready.com"}

    cohort = export_companies(database_connection, status="ready")
    assert [r["domain"] for r in cohort] == ["ready.com"]

    with_disabled = export_companies(database_connection, include_disabled=True)
    assert {r["domain"] for r in with_disabled} == {"ready.com", "bare.com"}


def test_company_import_diff_buckets(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.146: dry-run diff buckets by domain parity and CRM attributes."""
    from mailpilot.database import create_contact, disable_company

    ready = make_test_company(database_connection, name="Ready", domain="ready.com")
    update_company(database_connection, ready.id, profile=_PIPELINE_PROFILE)
    create_contact(database_connection, email="a@ready.com", company_id=ready.id)

    make_test_company(database_connection, name="No Profile", domain="noprofile.com")
    zero = make_test_company(database_connection, name="Zero", domain="zero.com")
    update_company(database_connection, zero.id, profile=_PIPELINE_PROFILE)

    disabled = make_test_company(
        database_connection, name="Disabled", domain="disabled.com"
    )
    disable_company(database_connection, disabled.id, reason="absorbed-brand")

    # File has ready + missing + disabled; CRM also has noprofile/zero (extra
    # when not in file). Include disabled so that bucket can populate.
    diff = company_import_diff(
        database_connection,
        {"ready.com", "missing.com", "disabled.com"},
        include_disabled=True,
    )

    assert diff["missing_in_crm"] == ["missing.com"]
    assert "noprofile.com" in diff["missing_profile"]
    assert "zero.com" in diff["zero_contacts"]
    assert diff["disabled"] == ["disabled.com"]
    assert "noprofile.com" in diff["extra_in_crm"]
    assert "zero.com" in diff["extra_in_crm"]
    assert "ready.com" not in diff["extra_in_crm"]
    assert "ready.com" not in diff["missing_in_crm"]
    # Union: ready, missing, disabled, noprofile, zero
    assert diff["record_count"] == 5


def test_disable_company_reenable_via_update(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.114: clearing disabled_reason re-enables the company.

    Unlike terminal tag/contact disable, a company disable is reversible -- a
    re-enabled company reappears in the default listing.
    """
    company = make_test_company(database_connection)
    disable_company(database_connection, company.id, "no_contacts_found:2026-06-18")
    assert list_companies(database_connection) == []

    reenabled = update_company(database_connection, company.id, disabled_reason=None)
    assert reenabled is not None
    assert reenabled.disabled_reason is None
    assert {c.id for c in list_companies(database_connection)} == {company.id}


def test_enable_company_clears_reason(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.114: enable_company clears disabled_reason; the company relists."""
    company = make_test_company(database_connection)
    disable_company(database_connection, company.id, "no_contacts_found:2026-06-18")
    reenabled = enable_company(database_connection, company.id)
    assert reenabled is not None
    assert reenabled.disabled_reason is None
    fetched = get_company(database_connection, company.id)
    assert fetched is not None
    assert fetched.disabled_reason is None
    assert {c.id for c in list_companies(database_connection)} == {company.id}


def test_enable_company_gate_blocks_active(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.114: the disabled_reason IS NOT NULL gate blocks enabling an active
    company (mirror of the double-disable gate)."""
    company = make_test_company(database_connection)
    assert enable_company(database_connection, company.id) is None


def test_enable_company_not_found_returns_none(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.114: enabling a missing company returns None."""
    assert enable_company(database_connection, "nonexistent") is None


def test_company_view_field_set_superset_of_base_and_summary() -> None:
    """§V.8: CompanyView field set ⊇ Company columns + CompanySummary projection.

    Recurrence guard: a view model omitting a base column is silently stripped
    from ``**company.model_dump()`` (Pydantic ``extra=ignore``), so disabled_reason
    must live on CompanyView too or `company view` would drop it. tags ride both
    list and view projections (§V.116); lean list also carries has_profile,
    contact_count, and optional profile (``--full`` summary only).
    """
    from mailpilot.models import Company, CompanySummary, CompanyView

    base = set(Company.model_fields)
    summary = set(CompanySummary.model_fields)
    view = set(CompanyView.model_fields)

    assert base <= view, f"CompanyView missing base columns: {base - view}"
    assert "disabled_reason" in base
    assert "disabled_reason" in summary
    assert "disabled_reason" in view
    assert "tags" in summary
    assert "tags" in view
    assert "aliases" in view
    assert "aliases" not in summary
    assert "has_profile" in summary
    assert "contact_count" in summary
    assert "profile" in summary


def test_search_companies(database_connection: psycopg.Connection[dict[str, Any]]):
    make_test_company(database_connection, name="Acme Inc", domain="acme.com")
    make_test_company(database_connection, name="Beta Corp", domain="beta.com")
    results = search_companies(database_connection, "acme")
    assert len(results) == 1
    assert results[0].name == "Acme Inc"
    assert results[0].has_profile is False


def test_search_companies_projects_contact_count(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.96: search_companies mirrors the contact_count projection."""
    from mailpilot.database import create_contact

    company = make_test_company(database_connection, name="Acme", domain="acme.com")
    create_contact(database_connection, email="a@acme.com", company_id=company.id)
    create_contact(database_connection, email="b@acme.com", company_id=company.id)

    results = search_companies(database_connection, "acme")
    assert len(results) == 1
    assert results[0].contact_count == 2


def test_search_companies_projects_has_profile(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    company = make_test_company(
        database_connection, name="Profiled", domain="profiled.com"
    )
    update_company(
        database_connection,
        company.id,
        profile={
            "summary": "Profiled Co.",
            "products": ["P1"],
            "target_customers": "Enterprise.",
            "sources": ["https://profiled.com/"],
        },
    )
    results = search_companies(database_connection, "profiled")
    assert len(results) == 1
    assert results[0].has_profile is True


def test_update_company(database_connection: psycopg.Connection[dict[str, Any]]):
    company = make_test_company(database_connection)
    updated = update_company(database_connection, company.id, name="New Name")
    assert updated is not None
    assert updated.name == "New Name"
    assert updated.updated_at > company.updated_at


def test_update_company_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    assert update_company(database_connection, "nonexistent", name="X") is None


def test_company_profile_column_present_and_default_null(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.72: ``company.profile`` exists as nullable JSONB and defaults to NULL."""
    row = database_connection.execute(
        "SELECT data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_name = 'company' AND column_name = 'profile'"
    ).fetchone()
    assert row is not None
    assert row["data_type"] == "jsonb"
    assert row["is_nullable"] == "YES"
    assert row["column_default"] is None

    company = make_test_company(database_connection)
    profile_row = database_connection.execute(
        "SELECT profile FROM company WHERE id = %s",
        (company.id,),
    ).fetchone()
    assert profile_row is not None
    assert profile_row["profile"] is None


def _full_profile() -> dict[str, Any]:
    return {
        "summary": "Acme makes industrial widgets for the aerospace sector.",
        "products": ["Widget X", "Widget Y"],
        "target_customers": "Aerospace OEMs and tier-1 suppliers.",
        "timezone": "America/Toronto",
        "sources": ["https://acme.com/", "https://acme.com/about"],
    }


def test_update_company_profile_full_valid(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.72: full valid profile validates and persists via JSONB column."""
    company = make_test_company(database_connection)
    profile = _full_profile()
    updated = update_company(database_connection, company.id, profile=profile)
    assert updated is not None
    assert updated.profile == profile


def test_update_company_profile_partial_no_timezone(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.72: ``timezone`` is optional; valid when omitted."""
    company = make_test_company(database_connection)
    profile = _full_profile()
    del profile["timezone"]
    updated = update_company(database_connection, company.id, profile=profile)
    assert updated is not None
    assert updated.profile is not None
    assert "timezone" not in updated.profile


def test_update_company_profile_invalid_missing_summary(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.72: missing required ``summary`` raises ValidationError."""
    from pydantic import ValidationError

    company = make_test_company(database_connection)
    profile = _full_profile()
    del profile["summary"]
    with pytest.raises(ValidationError):
        update_company(database_connection, company.id, profile=profile)


def test_update_company_profile_invalid_non_list_products(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.72: ``products`` must be a list."""
    from pydantic import ValidationError

    company = make_test_company(database_connection)
    profile = _full_profile()
    profile["products"] = "Widget X"
    with pytest.raises(ValidationError):
        update_company(database_connection, company.id, profile=profile)


def test_update_company_profile_invalid_missing_sources(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.72: ``sources`` is required."""
    from pydantic import ValidationError

    company = make_test_company(database_connection)
    profile = _full_profile()
    del profile["sources"]
    with pytest.raises(ValidationError):
        update_company(database_connection, company.id, profile=profile)


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


def test_list_contacts_by_company(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    company = make_test_company(database_connection, name="Foo", domain="foo.com")
    c1 = make_test_contact(
        database_connection, email="a@foo.com", company_id=company.id
    )
    make_test_contact(database_connection, email="b@bar.com")
    results = list_contacts(database_connection, company_id=company.id)
    assert len(results) == 1
    assert results[0].id == c1.id


def test_list_contacts_excludes_disabled_by_default(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import disable_contact

    c1 = make_test_contact(database_connection, email="a@foo.com")
    c2 = make_test_contact(database_connection, email="b@bar.com")
    disable_contact(database_connection, c2.id, reason="bounced: hard bounce")

    active = list_contacts(database_connection)
    assert len(active) == 1
    assert active[0].id == c1.id

    everyone = list_contacts(database_connection, include_disabled=True)
    assert {c.id for c in everyone} == {c1.id, c2.id}
    disabled_row = next(c for c in everyone if c.id == c2.id)
    assert disabled_row.disabled_reason == "bounced: hard bounce"


def test_enable_contact_clears_any_reason(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.80: enable_contact clears any reason, including an unsubscribe block.

    The operator owns consent -- there is no unsubscribe carve-out, so an
    ``unsubscribed:`` block re-enables the same way a bounce does.
    """
    from mailpilot.database import disable_contact, enable_contact

    contact = make_test_contact(database_connection)
    disable_contact(database_connection, contact.id, reason="unsubscribed: opt-out")
    reenabled = enable_contact(database_connection, contact.id)
    assert reenabled is not None
    assert reenabled.disabled_reason is None
    assert {c.id for c in list_contacts(database_connection)} == {contact.id}


def test_enable_contact_gate_blocks_active(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.80: the disabled_reason IS NOT NULL gate blocks enabling an active
    contact."""
    from mailpilot.database import enable_contact

    contact = make_test_contact(database_connection)
    assert enable_contact(database_connection, contact.id) is None


def test_enable_contact_not_found_returns_none(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.80: enabling a missing contact returns None."""
    from mailpilot.database import enable_contact

    assert enable_contact(database_connection, "nonexistent") is None


def test_search_contacts(database_connection: psycopg.Connection[dict[str, Any]]):
    make_test_contact(database_connection, email="alice@test.com")
    make_test_contact(database_connection, email="bob@test.com")
    results = search_contacts(database_connection, "alice")
    assert len(results) == 1


def test_update_contact(database_connection: psycopg.Connection[dict[str, Any]]):
    contact = make_test_contact(database_connection)
    updated = update_contact(database_connection, contact.id, first_name="Jane")
    assert updated is not None
    assert updated.first_name == "Jane"


def test_create_contact_with_lead_metadata(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.95: title + email_confidence are flat columns that round-trip."""
    created = create_contact(
        database_connection,
        email="lead@example.com",
        title="VP Engineering",
        email_confidence=87,
    )
    assert created is not None
    assert created.title == "VP Engineering"
    assert created.email_confidence == 87

    fetched = get_contact(database_connection, created.id)
    assert fetched is not None
    assert fetched.title == "VP Engineering"
    assert fetched.email_confidence == 87


def test_create_contact_lead_metadata_defaults_null(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.95: omitted lead-metadata is NULL (Bouncer-unknown, no signal)."""
    created = create_contact(database_connection, email="plain@example.com")
    assert created is not None
    assert created.title is None
    assert created.email_confidence is None


def test_update_contact_lead_metadata(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.95: update_contact flows title + email_confidence (model_fields gate)."""
    contact = make_test_contact(database_connection)
    updated = update_contact(
        database_connection, contact.id, title="Founder", email_confidence=42
    )
    assert updated is not None
    assert updated.title == "Founder"
    assert updated.email_confidence == 42


def test_email_confidence_check_rejects_out_of_range(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.95: schema CHECK email_confidence BETWEEN 0 AND 100."""
    with pytest.raises(psycopg.errors.CheckViolation):
        create_contact(
            database_connection,
            email="bad@example.com",
            email_confidence=101,
        )


def test_email_confidence_check_admits_boundaries(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.95: 0 and 100 are inclusive boundaries the CHECK admits."""
    low = create_contact(
        database_connection, email="low@example.com", email_confidence=0
    )
    high = create_contact(
        database_connection, email="high@example.com", email_confidence=100
    )
    assert low is not None
    assert low.email_confidence == 0
    assert high is not None
    assert high.email_confidence == 100


def test_list_contacts_max_email_confidence_filter(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.95: --max-email-confidence surfaces low-score AND NULL rows.

    NULL email_confidence = Bouncer-unknown = high risk: the operator review
    filter surfaces it, never drops it (per §B.76). Recurrence guard for the
    SQL three-valued-logic trap where ``email_confidence <= N`` alone excludes
    NULL.
    """
    risky = create_contact(
        database_connection, email="risky@example.com", email_confidence=20
    )
    safe = create_contact(
        database_connection, email="safe@example.com", email_confidence=95
    )
    unknown = create_contact(database_connection, email="unknown@example.com")
    assert risky is not None
    assert safe is not None
    assert unknown is not None

    surfaced = list_contacts(database_connection, max_email_confidence=70)
    assert {c.id for c in surfaced} == {risky.id, unknown.id}


def test_list_contacts_summary_carries_email_confidence(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """ContactSummary projects email_confidence so the filtered row is reviewable."""
    create_contact(database_connection, email="scored@example.com", email_confidence=33)
    summaries = list_contacts(database_connection)
    assert summaries[0].email_confidence == 33


def test_list_contacts_summary_carries_title_and_company_domain(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.5: ContactSummary carries title + company_domain via LEFT JOIN company."""
    company = make_test_company(database_connection, name="Acme", domain="acme.com")
    create_contact(
        database_connection,
        email="vp@acme.com",
        company_id=company.id,
        title="VP Sales",
    )
    create_contact(database_connection, email="solo@nowhere.com")

    summaries = {c.email: c for c in list_contacts(database_connection)}
    joined = summaries["vp@acme.com"]
    assert joined.title == "VP Sales"
    assert joined.company_domain == "acme.com"
    orphan = summaries["solo@nowhere.com"]
    assert orphan.title is None
    assert orphan.company_domain is None


def test_search_contacts_summary_carries_title_and_company_domain(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.5: search_contacts mirrors the title + company_domain projection."""
    company = make_test_company(database_connection, name="Acme", domain="acme.com")
    create_contact(
        database_connection,
        email="lead@acme.com",
        company_id=company.id,
        title="Head of Ops",
    )

    results = search_contacts(database_connection, "lead@acme.com")
    assert len(results) == 1
    assert results[0].title == "Head of Ops"
    assert results[0].company_domain == "acme.com"


def test_list_contacts_min_email_confidence_filter(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.95: --min-email-confidence surfaces high-score rows; NULL excluded."""
    low = create_contact(
        database_connection, email="low@example.com", email_confidence=20
    )
    high = create_contact(
        database_connection, email="high@example.com", email_confidence=95
    )
    create_contact(database_connection, email="unknown@example.com")
    assert low is not None
    assert high is not None

    surfaced = list_contacts(database_connection, min_email_confidence=50)
    assert {c.id for c in surfaced} == {high.id}


def test_list_contacts_min_max_email_confidence_compose(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.95: min + max compose into a closed range; NULL excluded by both bounds."""
    in_band = create_contact(
        database_connection, email="band@example.com", email_confidence=60
    )
    too_low = create_contact(
        database_connection, email="toolow@example.com", email_confidence=30
    )
    too_high = create_contact(
        database_connection, email="toohigh@example.com", email_confidence=90
    )
    create_contact(database_connection, email="null@example.com")
    assert in_band is not None
    assert too_low is not None
    assert too_high is not None

    surfaced = list_contacts(
        database_connection, min_email_confidence=50, max_email_confidence=70
    )
    assert {c.id for c in surfaced} == {in_band.id}


def test_list_contacts_title_filter_is_exact(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.115 family 5: list --title is case-insensitive exact, not substring."""
    vp = create_contact(
        database_connection, email="vp@example.com", title="VP Engineering"
    )
    create_contact(database_connection, email="rep@example.com", title="Sales Rep")
    create_contact(database_connection, email="blank@example.com")
    assert vp is not None

    # Exact (case-folded) match surfaces the row.
    surfaced = list_contacts(database_connection, title="vp engineering")
    assert {c.id for c in surfaced} == {vp.id}
    # A substring no longer matches on the list filter.
    assert list_contacts(database_connection, title="engineer") == []


def test_search_contacts_matches_title_substring(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.115 family 5: substring title matching lives on the search verb."""
    vp = create_contact(
        database_connection, email="vp@example.com", title="VP Engineering"
    )
    create_contact(database_connection, email="rep@example.com", title="Sales Rep")
    assert vp is not None

    results = search_contacts(database_connection, "engineer")
    assert {c.id for c in results} == {vp.id}


def test_list_contacts_until_upper_bounds_created_at(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.115 family 6: --until is an inclusive upper bound on created_at."""
    contact = create_contact(database_connection, email="early@example.com")
    assert contact is not None

    # The contact's own created_at is an inclusive upper bound (it appears).
    at_or_before = list_contacts(
        database_connection, until=contact.created_at.isoformat()
    )
    assert contact.id in {c.id for c in at_or_before}
    # An upper bound strictly before creation excludes it.
    before = list_contacts(database_connection, until="2000-01-01T00:00:00+00:00")
    assert before == []


def test_get_contact_by_email(database_connection: psycopg.Connection[dict[str, Any]]):
    contact = make_test_contact(database_connection, email="alice@test.com")
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
    alice = make_test_contact(database_connection, email="alice@example.com")
    bob = make_test_contact(database_connection, email="bob@example.com")
    result = get_contacts_by_emails(
        database_connection, ["alice@example.com", "bob@example.com"]
    )
    assert set(result.keys()) == {"alice@example.com", "bob@example.com"}
    assert result["alice@example.com"].id == alice.id
    assert result["bob@example.com"].id == bob.id


def test_get_contacts_by_emails_omits_missing(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    make_test_contact(database_connection, email="alice@example.com")
    result = get_contacts_by_emails(
        database_connection, ["alice@example.com", "ghost@example.com"]
    )
    assert set(result.keys()) == {"alice@example.com"}


def test_get_contacts_by_emails_deduplicates_input(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    make_test_contact(database_connection, email="alice@example.com")
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
    # Rows actually persisted.
    assert get_contact_by_email(database_connection, "alice@example.com") is not None
    assert get_contact_by_email(database_connection, "bob@other.com") is not None


def test_create_contacts_bulk_returns_existing_and_new(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    existing = make_test_contact(database_connection, email="alice@example.com")
    result = create_contacts_bulk(
        database_connection, ["alice@example.com", "bob@example.com"]
    )
    assert set(result.keys()) == {"alice@example.com", "bob@example.com"}
    # Existing row kept its original id.
    assert result["alice@example.com"].id == existing.id


# -- §V.90: contact.email canonicalized lowercase at write + lookup (§B.121) ---


def test_create_contact_lowercases_email_on_insert(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.90: create_contact stores a mixed-case address as lowercase."""
    contact = create_contact(database_connection, email="CThorne@Example.com")
    assert contact is not None
    assert contact.email == "cthorne@example.com"


def test_create_contact_case_variant_hits_conflict_no_duplicate(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.90/§B.121: a case-variant insert hits ON CONFLICT -> None, no duplicate.

    Outlook/Exchange recase the local-part; the write-path lowercase (not the
    case-sensitive UNIQUE) is the dedup guard.
    """
    first = create_contact(database_connection, email="cthorne@example.com")
    assert first is not None
    second = create_contact(database_connection, email="CThorne@example.com")
    assert second is None
    row = database_connection.execute(
        "SELECT COUNT(*) AS n FROM contact WHERE email = %(email)s",
        {"email": "cthorne@example.com"},
    ).fetchone()
    assert row is not None
    assert row["n"] == 1


def test_get_contact_by_email_resolves_mixed_case(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.90: a mixed-case lookup resolves the canonical lowercase row."""
    contact = make_test_contact(database_connection, email="alice@test.com")
    found = get_contact_by_email(database_connection, "Alice@Test.COM")
    assert found is not None
    assert found.id == contact.id


def test_create_or_get_contact_by_email_case_variant_returns_existing(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.90/§B.121: a recased From resolves the enrolled row, never mints a dup."""
    first = create_or_get_contact_by_email(
        database_connection, email="cthorne@example.com", first_name="Chris"
    )
    second = create_or_get_contact_by_email(
        database_connection, email="CThorne@Example.com", first_name="Chris"
    )
    assert second.id == first.id
    row = database_connection.execute(
        "SELECT COUNT(*) AS n FROM contact WHERE email = %(email)s",
        {"email": "cthorne@example.com"},
    ).fetchone()
    assert row is not None
    assert row["n"] == 1


def test_get_contacts_by_emails_resolves_mixed_case(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.90: batch lookup canonicalizes inputs to the lowercase stored key."""
    contact = make_test_contact(database_connection, email="alice@example.com")
    result = get_contacts_by_emails(database_connection, ["Alice@Example.com"])
    assert set(result.keys()) == {"alice@example.com"}
    assert result["alice@example.com"].id == contact.id


def test_create_contacts_bulk_folds_case_variants_to_one_row(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.90/§B.121: case-variant inputs collapse to a single canonical row."""
    result = create_contacts_bulk(
        database_connection, ["CThorne@Example.com", "cthorne@example.com"]
    )
    assert set(result.keys()) == {"cthorne@example.com"}
    row = database_connection.execute(
        "SELECT COUNT(*) AS n FROM contact WHERE email = %(email)s",
        {"email": "cthorne@example.com"},
    ).fetchone()
    assert row is not None
    assert row["n"] == 1


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
    assert result["weirdaddress"].email == "weirdaddress"


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
    assert fetched.name == "test-workflow"


def test_list_workflows_by_account(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    a1 = make_test_account(database_connection, email="a@test.com")
    a2 = make_test_account(database_connection, email="b@test.com")
    make_test_workflow(database_connection, account_id=a1.id, name="w1")
    make_test_workflow(database_connection, account_id=a2.id, name="w2")
    results = list_workflows(database_connection, account_id=a1.id)
    assert len(results) == 1
    assert results[0].name == "w1"


def test_list_workflows_full_orders_by_name(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.63: ``workflow export`` payload must be name-ordered for deterministic diffs."""
    account = make_test_account(database_connection)
    make_test_workflow(database_connection, account_id=account.id, name="charlie")
    make_test_workflow(database_connection, account_id=account.id, name="alpha")
    make_test_workflow(database_connection, account_id=account.id, name="bravo")
    results = list_workflows_full(database_connection, account.id)
    assert [w.name for w in results] == ["alpha", "bravo", "charlie"]
    # Returns full Workflow rows (not summaries) -- goal/instructions present.
    assert all(hasattr(w, "goal") for w in results)
    assert all(hasattr(w, "instructions") for w in results)


def test_list_workflows_full_scopes_to_account(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    a1 = make_test_account(database_connection, email="a@test.com")
    a2 = make_test_account(database_connection, email="b@test.com")
    make_test_workflow(database_connection, account_id=a1.id, name="a-only")
    make_test_workflow(database_connection, account_id=a2.id, name="b-only")
    results = list_workflows_full(database_connection, a1.id)
    assert [w.name for w in results] == ["a-only"]


def test_update_workflow(database_connection: psycopg.Connection[dict[str, Any]]):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    updated = update_workflow(database_connection, workflow.id, goal="Book demo")
    assert updated is not None
    assert updated.goal == "Book demo"


def test_update_workflow_rebinds_account(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.103: `account_id` is a non-def field, so update re-binds the account."""
    account = make_test_account(database_connection)
    other = make_test_account(database_connection, email="other@example.com")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    updated = update_workflow(database_connection, workflow.id, account_id=other.id)
    assert updated is not None
    assert updated.account_id == other.id
    assert updated.account_email == "other@example.com"


def test_update_workflow_rejects_template_change(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.44: `template` change must raise -- forces delete+recreate."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    with pytest.raises(ValueError, match="template is immutable"):
        update_workflow(database_connection, workflow.id, template="inbound-general")


def test_update_workflow_rejects_type_change(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.44: `type` is derived from template at create time and cannot be updated."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    with pytest.raises(ValueError, match="type is derived"):
        update_workflow(database_connection, workflow.id, type="inbound")


def test_activate_workflow(database_connection: psycopg.Connection[dict[str, Any]]):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    update_workflow(
        database_connection,
        workflow.id,
        goal="Book demo",
        instructions="You are a sales rep.",
    )
    activated = activate_workflow(database_connection, workflow.id)
    assert activated.status == "active"


def test_activate_workflow_requires_goal(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    update_workflow(
        database_connection, workflow.id, instructions="You are a sales rep."
    )
    with pytest.raises(ValueError, match="goal must be non-empty"):
        activate_workflow(database_connection, workflow.id)


def test_activate_workflow_requires_instructions(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    update_workflow(database_connection, workflow.id, goal="Book demo")
    with pytest.raises(ValueError, match="instructions must be non-empty"):
        activate_workflow(database_connection, workflow.id)


def test_pause_workflow(database_connection: psycopg.Connection[dict[str, Any]]):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    update_workflow(
        database_connection,
        workflow.id,
        goal="Book demo",
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
    w1 = make_test_workflow(database_connection, account_id=account.id, name="w1")
    make_test_workflow(database_connection, account_id=account.id, name="w2")
    update_workflow(
        database_connection,
        w1.id,
        goal="Book demo",
        instructions="You are a sales rep.",
    )
    activate_workflow(database_connection, w1.id)
    # w2 stays as draft
    active = list_workflows(database_connection, account_id=account.id, status="active")
    assert len(active) == 1
    assert active[0].name == "w1"
    drafts = list_workflows(database_connection, account_id=account.id, status="draft")
    assert len(drafts) == 1
    assert drafts[0].name == "w2"
    all_workflows = list_workflows(database_connection, account_id=account.id)
    assert len(all_workflows) == 2


def test_list_workflows_by_type(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    make_test_workflow(
        database_connection,
        account_id=account.id,
        name="outreach",
        template="outbound-general",
    )
    make_test_workflow(
        database_connection,
        account_id=account.id,
        name="auto-reply",
        template="inbound-general",
    )
    outbound = list_workflows(database_connection, workflow_type="outbound")
    assert len(outbound) == 1
    assert outbound[0].name == "outreach"
    inbound = list_workflows(database_connection, workflow_type="inbound")
    assert len(inbound) == 1
    assert inbound[0].name == "auto-reply"


def test_search_workflows_by_name(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    make_test_workflow(database_connection, account_id=account.id, name="demo-outreach")
    make_test_workflow(
        database_connection, account_id=account.id, name="support-auto-reply"
    )
    results = search_workflows(database_connection, "demo")
    assert len(results) == 1
    assert results[0].name == "demo-outreach"


def test_search_workflows_by_goal(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    w1 = make_test_workflow(database_connection, account_id=account.id, name="alpha")
    make_test_workflow(database_connection, account_id=account.id, name="beta")
    update_workflow(database_connection, w1.id, goal="Book discovery call")
    results = search_workflows(database_connection, "discovery")
    assert len(results) == 1
    assert results[0].id == w1.id


def test_search_workflows_respects_limit(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    for i in range(5):
        make_test_workflow(database_connection, account_id=account.id, name=f"flow-{i}")
    results = search_workflows(database_connection, "flow", limit=2)
    assert len(results) == 2


def test_workflow_account_email_populated_across_returners(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.5 parent-NI clause: every ``Workflow`` / ``WorkflowSummary`` return
    carries ``account_email`` joined from ``account.email``.
    """
    account = make_test_account(database_connection, email="owner@parent-ni.test")
    created = make_test_workflow(database_connection, account_id=account.id)
    assert created.account_id == account.id
    assert created.account_email == "owner@parent-ni.test"

    fetched = get_workflow(database_connection, created.id)
    assert fetched is not None
    assert fetched.account_email == "owner@parent-ni.test"

    listed = list_workflows(database_connection, account_id=account.id)
    assert len(listed) == 1
    assert listed[0].account_id == account.id
    assert listed[0].account_email == "owner@parent-ni.test"

    full = list_workflows_full(database_connection, account.id)
    assert len(full) == 1
    assert full[0].account_email == "owner@parent-ni.test"

    searched = search_workflows(database_connection, "Test")
    assert searched[0].account_email == "owner@parent-ni.test"

    updated = update_workflow(
        database_connection,
        created.id,
        goal="Book demo",
        instructions="Do.",
    )
    assert updated is not None
    assert updated.account_email == "owner@parent-ni.test"

    activated = activate_workflow(database_connection, created.id)
    assert activated.account_email == "owner@parent-ni.test"

    paused = pause_workflow(database_connection, created.id)
    assert paused.account_email == "owner@parent-ni.test"


def test_workflow_account_email_reflects_joined_account_per_workflow(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.5 parent-NI clause: JOIN scopes per workflow (no cross-talk)."""
    a1 = make_test_account(database_connection, email="one@parent-ni.test")
    a2 = make_test_account(database_connection, email="two@parent-ni.test")
    make_test_workflow(database_connection, account_id=a1.id, name="w1")
    make_test_workflow(database_connection, account_id=a2.id, name="w2")
    listed = list_workflows(database_connection)
    by_name = {row.name: row.account_email for row in listed}
    assert by_name["w1"] == "one@parent-ni.test"
    assert by_name["w2"] == "two@parent-ni.test"


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


def test_list_emails_by_route_method(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        gmail_message_id="msg_rm_a",
        subject="Classified A",
        is_routed=True,
        route_method="classified",
    )
    create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        gmail_message_id="msg_rm_b",
        subject="Thread B",
        is_routed=True,
        route_method="thread_match",
    )
    classified = list_emails(database_connection, route_method="classified")
    assert len(classified) == 1
    assert classified[0].subject == "Classified A"
    assert classified[0].route_method == "classified"


def test_update_email_persists_route_method(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        gmail_message_id="msg_rm_upd",
        subject="To be routed",
    )
    assert email is not None
    assert email.route_method is None
    updated = update_email(
        database_connection,
        email.id,
        is_routed=True,
        route_method="thread_match",
    )
    assert updated is not None
    assert updated.route_method == "thread_match"


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


def test_list_emails_summary_includes_gmail_thread_id(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.7(+): Summary projection MUST include gmail_thread_id so callers
    can answer thread-pivot questions (smoke-test A4 threading confirmation
    per §B.39) from `list` without round-tripping per row through `view`.

    Inbound rows synced from Gmail carry a thread id; outbound rows queued
    locally before send carry null until Gmail accepts the send. Both shapes
    must surface on the Summary projection.
    """
    account = make_test_account(database_connection)
    synced = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        gmail_message_id="msg_threaded",
        gmail_thread_id="thread_value_42",
        subject="threaded message",
    )
    queued = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="queued for send",
    )
    assert synced is not None
    assert queued is not None

    results = {row.id: row for row in list_emails(database_connection)}
    assert results[synced.id].gmail_thread_id == "thread_value_42"
    assert results[queued.id].gmail_thread_id is None


def test_list_emails_summary_includes_is_routed(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Summary projection MUST include is_routed so callers can answer
    routing state from `list` without falling back to `view`.
    """
    account = make_test_account(database_connection)
    routed = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        gmail_message_id="msg_routed",
        subject="routed message",
    )
    unrouted = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        gmail_message_id="msg_unrouted",
        subject="unrouted message",
    )
    assert routed is not None
    assert unrouted is not None
    update_email(database_connection, routed.id, is_routed=True)

    results = {row.id: row for row in list_emails(database_connection)}
    assert results[routed.id].is_routed is True
    assert results[unrouted.id].is_routed is False


def test_list_emails_summary_includes_recipients(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Summary projection MUST carry the To/Cc/Bcc map (§V.7).

    A single bulk `email list` exposes each message's recipients so the
    campaign-test delivery check keys arrivals on the recipient alias without a
    per-row `email view` (§V.122).
    """
    account = make_test_account(database_connection)
    created = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        gmail_message_id="msg_with_recipients",
        subject="shared subject",
        recipients={"to": ["inbound2@lab5.ca"], "cc": ["ops@lab5.ca"]},
    )
    assert created is not None

    results = {row.id: row for row in list_emails(database_connection)}
    assert results[created.id].recipients == {
        "to": ["inbound2@lab5.ca"],
        "cc": ["ops@lab5.ca"],
    }


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
    contact = make_test_contact(database_connection, email="r@example.com")
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
    contact = make_test_contact(database_connection, email="r@example.com")
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
    contact = make_test_contact(database_connection, email="r@example.com")
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
    contact = make_test_contact(database_connection, email="r@example.com")
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
    contact = make_test_contact(database_connection, email="r@example.com")
    wf_a = make_test_workflow(
        database_connection, account_id=account.id, name="campaign-a"
    )
    wf_b = make_test_workflow(
        database_connection, account_id=account.id, name="campaign-b"
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
    # recipients rides EmailSummary now (§V.7) -- read it straight off the row.
    assert "kb@lab5.ca" in results[0].recipients["to"]


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
    c1 = make_test_contact(database_connection, email="a@test.com")
    c2 = make_test_contact(database_connection, email="b@test.com")
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


def test_status_payload_counts_block_includes_activities(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from conftest import make_test_settings

    contact = make_test_contact(database_connection)
    make_test_activity(database_connection, contact_id=contact.id)
    payload = get_status_payload(database_connection, make_test_settings())
    counts = payload["counts"]
    assert isinstance(counts, dict)
    assert counts["activities"] == 1


# -- Tag -----------------------------------------------------------------------


def test_normalize_tag_name_accepts_valid_inputs() -> None:
    """Lowercase, hyphenated, alphanumeric tags pass through unchanged."""
    from mailpilot.database import (
        _normalize_tag_name,  # pyright: ignore[reportPrivateUsage]
    )

    assert _normalize_tag_name("prospect") == "prospect"
    assert _normalize_tag_name("hot-lead") == "hot-lead"
    assert _normalize_tag_name("q4-2025") == "q4-2025"


def test_normalize_tag_name_collapses_separators_and_case() -> None:
    """Whitespace, underscores, and uppercase are normalized; hyphens collapse."""
    from mailpilot.database import (
        _normalize_tag_name,  # pyright: ignore[reportPrivateUsage]
    )

    assert _normalize_tag_name("Hot Lead") == "hot-lead"
    assert _normalize_tag_name("hot_lead") == "hot-lead"
    assert _normalize_tag_name("HOT--LEAD") == "hot-lead"
    assert _normalize_tag_name("  spaced  ") == "spaced"
    assert _normalize_tag_name("-leading-trailing-") == "leading-trailing"


def test_normalize_tag_name_rejects_invalid() -> None:
    """Names that cannot be normalized to [a-z0-9][a-z0-9-]* raise ValueError."""
    from mailpilot.database import (
        _normalize_tag_name,  # pyright: ignore[reportPrivateUsage]
    )

    with pytest.raises(ValueError, match="invalid tag name"):
        _normalize_tag_name("")
    with pytest.raises(ValueError, match="invalid tag name"):
        _normalize_tag_name("---")
    with pytest.raises(ValueError, match="invalid tag name"):
        _normalize_tag_name("hot/lead")
    with pytest.raises(ValueError, match="invalid tag name"):
        _normalize_tag_name("hot.lead")


# -- vocabulary (§V.116) -------------------------------------------------------


def test_create_tag_defines_owner_free_vocabulary_row(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.116: create_tag defines a vocabulary entry, no owner."""
    tag = create_tag(database_connection, name="prospect")
    assert tag is not None
    assert tag.name == "prospect"
    assert tag.disabled_reason is None
    assert not hasattr(tag, "contact_id")


def test_create_tag_normalizes_name(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """create_tag applies _normalize_tag_name."""
    tag = create_tag(database_connection, name="Hot Lead")
    assert tag is not None
    assert tag.name == "hot-lead"


def test_create_tag_duplicate_name_returns_none(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.90: tag.name is globally unique; a second create returns None."""
    first = create_tag(database_connection, name="prospect")
    second = create_tag(database_connection, name="prospect")
    assert first is not None
    assert second is None


def test_get_tag_by_name_and_by_id(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.107: a tag resolves by its unique name and by id."""
    from mailpilot.database import get_tag, get_tag_by_name

    created = create_tag(database_connection, name="vip")
    assert created is not None
    by_name = get_tag_by_name(database_connection, "VIP")  # case-folded
    assert by_name is not None
    assert by_name.id == created.id
    by_id = get_tag(database_connection, created.id)
    assert by_id is not None
    assert by_id.name == "vip"
    assert get_tag_by_name(database_connection, "ghost") is None


def test_get_tag_summary_projects_usage_count(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.116: tag view carries usage_count (assignment count)."""
    from mailpilot.database import get_tag_summary_by_name

    tag = create_tag(database_connection, name="vip")
    assert tag is not None
    a = make_test_contact(database_connection, email="x1@acme.test")
    b = make_test_contact(database_connection, email="x2@acme.test")
    make_test_tag_assignment(database_connection, contact_id=a.id, name="vip")
    make_test_tag_assignment(database_connection, contact_id=b.id, name="vip")
    summary = get_tag_summary_by_name(database_connection, "vip")
    assert summary is not None
    assert summary.usage_count == 2


def test_list_tags_vocabulary_with_usage_count(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.116: owner-free list_tags returns the whole vocabulary + usage_count."""
    create_tag(database_connection, name="cold")
    create_tag(database_connection, name="vip")
    contact = make_test_contact(database_connection)
    make_test_tag_assignment(database_connection, contact_id=contact.id, name="vip")
    tags = list_tags(database_connection)
    by_name = {t.name: t.usage_count for t in tags}
    assert by_name == {"cold": 0, "vip": 1}


def test_list_tags_by_owner_lists_only_assigned(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.116: list_tags scoped to an owner lists that owner's tags only."""
    create_tag(database_connection, name="cold")
    contact = make_test_contact(database_connection)
    make_test_tag_assignment(database_connection, contact_id=contact.id, name="vip")
    tags = list_tags(database_connection, contact_id=contact.id)
    assert [t.name for t in tags] == ["vip"]
    assert tags[0].usage_count == 1


def test_list_tags_empty(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = make_test_contact(database_connection)
    assert list_tags(database_connection, contact_id=contact.id) == []


def test_search_tags_vocabulary(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.116: search_tags substring-matches vocabulary names + usage_count."""
    create_tag(database_connection, name="prospect")
    create_tag(database_connection, name="cold")
    results = search_tags(database_connection, name="pro")
    assert [t.name for t in results] == ["prospect"]
    assert results[0].usage_count == 0


def test_status_payload_counts_block_includes_tags(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from conftest import make_test_settings

    make_test_tag(database_connection, name="prospect")
    payload = get_status_payload(database_connection, make_test_settings())
    counts = payload["counts"]
    assert isinstance(counts, dict)
    assert counts["tags"] == 1


# -- vocabulary soft-disable (§V.10/§V.116) ------------------------------------


def test_disable_tag_soft_retires_and_writes_no_activity(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.116/§V.17: disable_tag retires the vocabulary row and -- being
    owner-free -- writes no activity (an activity needs a contact/company)."""
    from mailpilot.database import disable_tag

    contact = make_test_contact(database_connection)
    make_test_tag_assignment(database_connection, contact_id=contact.id, name="cold")
    disabled = disable_tag(database_connection, name="cold", reason="stale")
    assert disabled is not None
    assert disabled.name == "cold"
    assert disabled.disabled_reason == "stale"
    # No tag_disabled activity is written for a vocabulary retire.
    activities = list_activities(database_connection, contact_id=contact.id)
    assert "tag_disabled" not in [a.type for a in activities]


def test_disable_tag_double_disable_returns_none(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.10: disabled_reason IS NULL gate blocks double-disable."""
    from mailpilot.database import disable_tag

    create_tag(database_connection, name="cold")
    first = disable_tag(database_connection, name="cold", reason="stale")
    second = disable_tag(database_connection, name="cold", reason="again")
    assert first is not None
    assert second is None


def test_disable_tag_undefined_returns_none(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import disable_tag

    assert disable_tag(database_connection, name="ghost", reason="x") is None


def test_enable_tag_clears_reason_and_writes_no_activity(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.10/§V.17: enable_tag clears disabled_reason and -- being owner-free --
    writes no activity. The tag returns to the default list."""
    from mailpilot.database import disable_tag, enable_tag

    contact = make_test_contact(database_connection)
    make_test_tag_assignment(database_connection, contact_id=contact.id, name="cold")
    disable_tag(database_connection, name="cold", reason="stale")
    enabled = enable_tag(database_connection, name="cold")
    assert enabled is not None
    assert enabled.name == "cold"
    assert enabled.disabled_reason is None
    assert {t.name for t in list_tags(database_connection)} == {"cold"}
    activities = list_activities(database_connection, contact_id=contact.id)
    assert "tag_enabled" not in [a.type for a in activities]


def test_enable_tag_gate_blocks_active(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.10: the disabled_reason IS NOT NULL gate blocks enabling an active
    tag (returns None)."""
    from mailpilot.database import create_tag, enable_tag

    create_tag(database_connection, name="cold")
    assert enable_tag(database_connection, name="cold") is None


def test_enable_tag_undefined_returns_none(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.10: enabling an undefined tag returns None."""
    from mailpilot.database import enable_tag

    assert enable_tag(database_connection, name="ghost") is None


def test_disabled_tag_hidden_from_default_list(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.10: a retired vocabulary tag drops from the default list."""
    from mailpilot.database import disable_tag

    create_tag(database_connection, name="hot")
    create_tag(database_connection, name="cold")
    disable_tag(database_connection, name="cold", reason="stale")
    assert {t.name for t in list_tags(database_connection)} == {"hot"}
    all_tags = list_tags(database_connection, include_disabled=True)
    assert {t.name for t in all_tags} == {"hot", "cold"}
    assert [t.name for t in search_tags(database_connection, name="co")] == []
    assert [
        t.name
        for t in search_tags(database_connection, name="co", include_disabled=True)
    ] == ["cold"]


# -- assignment link lifecycle (§V.91/§V.116) ----------------------------------


def test_assign_tag_to_contact_emits_activity_atomically(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.91: assign_tag_to_contact links + emits tag_added in one transaction."""
    from mailpilot.database import assign_tag_to_contact

    contact = make_test_contact(database_connection)
    tag = create_tag(database_connection, name="prospect")
    assert tag is not None
    assignment = assign_tag_to_contact(
        database_connection, tag_id=tag.id, contact_id=contact.id
    )
    assert assignment is not None
    assert assignment.tag_id == tag.id
    assert assignment.contact_id == contact.id
    assert assignment.company_id is None
    assert [t.name for t in list_tags(database_connection, contact_id=contact.id)] == [
        "prospect"
    ]
    activities = list_activities(database_connection, contact_id=contact.id)
    assert len(activities) == 1
    assert activities[0].type == "tag_added"
    assert activities[0].summary == "Tagged as prospect"


def test_assign_tag_duplicate_returns_none_no_activity(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """A re-link of the same (tag, owner) returns None, writes no activity."""
    from mailpilot.database import assign_tag_to_contact

    contact = make_test_contact(database_connection)
    tag = create_tag(database_connection, name="prospect")
    assert tag is not None
    assign_tag_to_contact(database_connection, tag_id=tag.id, contact_id=contact.id)
    second = assign_tag_to_contact(
        database_connection, tag_id=tag.id, contact_id=contact.id
    )
    assert second is None
    assert len(list_activities(database_connection, contact_id=contact.id)) == 1


def test_assign_tag_to_company_emits_company_activity(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import assign_tag_to_company

    company = make_test_company(database_connection)
    tag = create_tag(database_connection, name="enterprise")
    assert tag is not None
    assign_tag_to_company(database_connection, tag_id=tag.id, company_id=company.id)
    activities = list_activities(database_connection, company_id=company.id)
    assert len(activities) == 1
    assert activities[0].type == "tag_added"
    assert activities[0].company_id == company.id
    assert activities[0].contact_id is None


def test_assign_tag_unknown_contact_raises(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import assign_tag_to_contact

    tag = create_tag(database_connection, name="prospect")
    assert tag is not None
    with pytest.raises(ValueError, match="contact not found"):
        assign_tag_to_contact(database_connection, tag_id=tag.id, contact_id="ghost")


def test_remove_tag_from_contact_deletes_and_emits_activity(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.116: tag remove unlinks and emits tag_removed; vocabulary survives."""
    from mailpilot.database import get_tag_by_name, remove_tag_from_contact

    contact = make_test_contact(database_connection)
    make_test_tag_assignment(database_connection, contact_id=contact.id, name="cold")
    tag = get_tag_by_name(database_connection, "cold")
    assert tag is not None
    removed = remove_tag_from_contact(
        database_connection, tag_id=tag.id, contact_id=contact.id
    )
    assert removed is not None
    assert list_tags(database_connection, contact_id=contact.id) == []
    # Vocabulary entry is untouched.
    assert get_tag_by_name(database_connection, "cold") is not None
    activities = list_activities(database_connection, contact_id=contact.id)
    types = [a.type for a in activities]
    assert "tag_removed" in types
    removed_event = next(a for a in activities if a.type == "tag_removed")
    assert removed_event.summary == "Untagged cold"


def test_remove_tag_absent_returns_none_no_activity(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import remove_tag_from_contact

    contact = make_test_contact(database_connection)
    tag = create_tag(database_connection, name="cold")
    assert tag is not None
    assert (
        remove_tag_from_contact(
            database_connection, tag_id=tag.id, contact_id=contact.id
        )
        is None
    )
    assert [
        a.type for a in list_activities(database_connection, contact_id=contact.id)
    ] == []


def test_remove_tag_from_company(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import get_tag_by_name, remove_tag_from_company

    company = make_test_company(database_connection)
    make_test_tag_assignment(
        database_connection, company_id=company.id, name="enterprise"
    )
    tag = get_tag_by_name(database_connection, "enterprise")
    assert tag is not None
    removed = remove_tag_from_company(
        database_connection, tag_id=tag.id, company_id=company.id
    )
    assert removed is not None
    assert list_tags(database_connection, company_id=company.id) == []
    types = [
        a.type for a in list_activities(database_connection, company_id=company.id)
    ]
    assert "tag_removed" in types


def test_set_company_tags_replaces_with_activity(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.141/§V.14: set_company_tags adds missing, removes extras, one txn."""
    from mailpilot.database import create_tag, set_company_tags

    company = make_test_company(database_connection)
    keep = create_tag(database_connection, name="acumatica-var")
    drop = create_tag(database_connection, name="stale-var")
    add = create_tag(database_connection, name="dynamics-365-var")
    assert keep is not None
    assert drop is not None
    assert add is not None
    make_test_tag_assignment(
        database_connection, company_id=company.id, name="acumatica-var"
    )
    make_test_tag_assignment(
        database_connection, company_id=company.id, name="stale-var"
    )

    final = set_company_tags(
        database_connection,
        company_id=company.id,
        tag_ids=[keep.id, add.id],
    )

    assert final == ["acumatica-var", "dynamics-365-var"]
    assert [t.name for t in list_tags(database_connection, company_id=company.id)] == [
        "acumatica-var",
        "dynamics-365-var",
    ]
    activities = list_activities(database_connection, company_id=company.id)
    types = {a.type for a in activities}
    assert "tag_added" in types
    assert "tag_removed" in types
    assert any(
        a.type == "tag_removed" and a.summary == "Untagged stale-var"
        for a in activities
    )
    assert any(
        a.type == "tag_added" and a.summary == "Tagged as dynamics-365-var"
        for a in activities
    )


def test_set_company_tags_empty_clears(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.141: empty tag_ids clears every company assignment."""
    from mailpilot.database import set_company_tags

    company = make_test_company(database_connection)
    make_test_tag_assignment(database_connection, company_id=company.id, name="vip")
    make_test_tag_assignment(database_connection, company_id=company.id, name="partner")

    final = set_company_tags(database_connection, company_id=company.id, tag_ids=[])

    assert final == []
    assert list_tags(database_connection, company_id=company.id) == []


def test_set_contact_tags_replaces(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.141: set_contact_tags mirrors company replace semantics."""
    from mailpilot.database import create_tag, set_contact_tags

    contact = make_test_contact(database_connection)
    a = create_tag(database_connection, name="hot")
    b = create_tag(database_connection, name="warm")
    assert a is not None
    assert b is not None
    make_test_tag_assignment(database_connection, contact_id=contact.id, name="hot")

    final = set_contact_tags(database_connection, contact_id=contact.id, tag_ids=[b.id])

    assert final == ["warm"]
    assert [t.name for t in list_tags(database_connection, contact_id=contact.id)] == [
        "warm"
    ]


# -- membership filters on company/contact list (§V.116) -----------------------


def test_list_companies_filter_by_tag(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.116: list_companies tag= keeps only companies carrying the tag."""
    a = make_test_company(database_connection, name="A", domain="a.test")
    b = make_test_company(database_connection, name="B", domain="b.test")
    tag = create_tag(database_connection, name="enterprise")
    assert tag is not None
    make_test_tag_assignment(database_connection, company_id=a.id, name="enterprise")
    rows = list_companies(database_connection, tag=tag.id)
    assert [c.domain for c in rows] == ["a.test"]
    assert b.domain not in [c.domain for c in rows]


def test_list_companies_exclude_by_no_tag(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.116: list_companies exclude_tags keeps only companies NOT carrying it."""
    a = make_test_company(database_connection, name="A", domain="a.test")
    make_test_company(database_connection, name="B", domain="b.test")
    tag = create_tag(database_connection, name="no-contacts-found")
    assert tag is not None
    make_test_tag_assignment(
        database_connection, company_id=a.id, name="no-contacts-found"
    )
    rows = list_companies(database_connection, exclude_tags=[tag.id])
    assert [c.domain for c in rows] == ["b.test"]


def test_list_companies_exclude_by_multiple_no_tags(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.96/§V.116: repeatable exclude_tags drops every memoization class.

    One NOT EXISTS per tag, all intersected -- a company tagged either
    ``no-contacts-found`` or ``contacts-exhausted`` leaves the discover set.
    """
    a = make_test_company(database_connection, name="A", domain="a.test")
    b = make_test_company(database_connection, name="B", domain="b.test")
    c = make_test_company(database_connection, name="C", domain="c.test")
    no_dm = create_tag(database_connection, name="no-contacts-found")
    exhausted = create_tag(database_connection, name="contacts-exhausted")
    assert no_dm is not None
    assert exhausted is not None
    make_test_tag_assignment(
        database_connection, company_id=a.id, name="no-contacts-found"
    )
    make_test_tag_assignment(
        database_connection, company_id=b.id, name="contacts-exhausted"
    )
    rows = list_companies(database_connection, exclude_tags=[no_dm.id, exhausted.id])
    assert [row.domain for row in rows] == ["c.test"]
    assert c.domain == "c.test"


def test_list_companies_tag_and_no_tag_intersection(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.116: --tag and --no-tag compose as an intersection (carries A not B)."""
    a = make_test_company(database_connection, name="A", domain="a.test")
    b = make_test_company(database_connection, name="B", domain="b.test")
    profiled = create_tag(database_connection, name="profiled")
    skip = create_tag(database_connection, name="no-contacts-found")
    assert profiled is not None
    assert skip is not None
    make_test_tag_assignment(database_connection, company_id=a.id, name="profiled")
    make_test_tag_assignment(database_connection, company_id=b.id, name="profiled")
    make_test_tag_assignment(
        database_connection, company_id=b.id, name="no-contacts-found"
    )
    rows = list_companies(database_connection, tag=profiled.id, exclude_tags=[skip.id])
    assert [c.domain for c in rows] == ["a.test"]


def test_list_contacts_filter_by_tag(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.116: list_contacts tag= / exclude_tags= over the assignment join."""
    a = make_test_contact(database_connection, email="x1@acme.test")
    b = make_test_contact(database_connection, email="x2@acme.test")
    tag = create_tag(database_connection, name="vip")
    assert tag is not None
    make_test_tag_assignment(database_connection, contact_id=a.id, name="vip")
    assert [c.email for c in list_contacts(database_connection, tag=tag.id)] == [
        "x1@acme.test"
    ]
    assert [
        c.email for c in list_contacts(database_connection, exclude_tags=[tag.id])
    ] == ["x2@acme.test"]
    assert b.email == "x2@acme.test"


def test_list_contacts_exclude_by_multiple_no_tags(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.116: repeatable exclude_tags drops contacts carrying any named tag."""
    a = make_test_contact(database_connection, email="a@acme.test")
    b = make_test_contact(database_connection, email="b@acme.test")
    c = make_test_contact(database_connection, email="c@acme.test")
    vip = create_tag(database_connection, name="vip")
    cold = create_tag(database_connection, name="cold")
    assert vip is not None
    assert cold is not None
    make_test_tag_assignment(database_connection, contact_id=a.id, name="vip")
    make_test_tag_assignment(database_connection, contact_id=b.id, name="cold")
    rows = list_contacts(database_connection, exclude_tags=[vip.id, cold.id])
    assert [row.email for row in rows] == ["c@acme.test"]
    assert c.email == "c@acme.test"


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
        create_note(database_connection, contact_id="c1", company_id="co1", body="x")


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


def test_delete_note_removes_one_note_returns_true(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import delete_note

    contact = make_test_contact(database_connection)
    note = make_test_note(database_connection, contact_id=contact.id, body="one")
    deleted = delete_note(database_connection, note.id)
    assert deleted is True
    assert list_notes(database_connection, contact_id=contact.id) == []


def test_delete_note_leaves_sibling_notes(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Deleting one note must leave the owner's other notes intact (§V.14)."""
    from mailpilot.database import delete_note

    contact = make_test_contact(database_connection)
    keep_one = make_test_note(database_connection, contact_id=contact.id, body="keep 1")
    drop = make_test_note(database_connection, contact_id=contact.id, body="drop me")
    keep_two = make_test_note(database_connection, contact_id=contact.id, body="keep 2")

    deleted = delete_note(database_connection, drop.id)

    assert deleted is True
    remaining = {n.id for n in list_notes(database_connection, contact_id=contact.id)}
    assert remaining == {keep_one.id, keep_two.id}


def test_delete_note_leaves_other_owners_untouched(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import delete_note

    target = make_test_contact(database_connection, email="target@acme.test")
    other = make_test_contact(database_connection, email="other@acme.test")
    company = make_test_company(database_connection)
    drop = make_test_note(database_connection, contact_id=target.id, body="poison")
    make_test_note(database_connection, contact_id=other.id, body="keep-contact")
    make_test_note(database_connection, company_id=company.id, body="keep-company")

    delete_note(database_connection, drop.id)

    assert list_notes(database_connection, contact_id=target.id) == []
    assert len(list_notes(database_connection, contact_id=other.id)) == 1
    assert len(list_notes(database_connection, company_id=company.id)) == 1


def test_delete_note_on_company(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import delete_note

    company = make_test_company(database_connection)
    note = make_test_note(database_connection, company_id=company.id, body="one")
    deleted = delete_note(database_connection, note.id)
    assert deleted is True
    assert list_notes(database_connection, company_id=company.id) == []


def test_delete_note_missing_returns_false(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import delete_note

    assert delete_note(database_connection, "nonexistent-id") is False


def test_delete_note_leaves_activity_trail_append_only(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import add_contact_note, delete_note

    contact = make_test_contact(database_connection)
    note = add_contact_note(database_connection, contact_id=contact.id, body="audited")

    delete_note(database_connection, note.id)

    assert list_notes(database_connection, contact_id=contact.id) == []
    activities = list_activities(database_connection, contact_id=contact.id)
    assert [a.type for a in activities] == ["note_added"]


def test_status_payload_counts_block_includes_notes(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from conftest import make_test_settings

    contact = make_test_contact(database_connection)
    make_test_note(database_connection, contact_id=contact.id)
    payload = get_status_payload(database_connection, make_test_settings())
    counts = payload["counts"]
    assert isinstance(counts, dict)
    assert counts["notes"] == 1


# -- Enrollment ---------------------------------------------------------------


def test_disable_enrollment(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.10(+) enrollment coverage: UPDATE to status='disabled' + activity row."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    updated = disable_enrollment(database_connection, enrollment.id, "left company")
    assert updated is not None
    assert updated.id == enrollment.id
    assert updated.status == "disabled"
    assert updated.disabled_reason == "left company"
    # Row retained (soft-disable, §V.10): both lookups still resolve.
    same = get_enrollment(database_connection, workflow.id, contact.id)
    assert same is not None
    assert same.status == "disabled"
    # §V.10 enrollment coverage: enrollment_disabled activity row emitted.
    activities = list_activities(database_connection, contact_id=contact.id)
    disabled_rows = [a for a in activities if a.type == "enrollment_disabled"]
    assert len(disabled_rows) == 1
    assert disabled_rows[0].summary == "left company"
    assert disabled_rows[0].enrollment_id == enrollment.id
    assert disabled_rows[0].workflow_id == workflow.id


def test_enable_enrollment(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.15: enable_enrollment flips status disabled->active + clears reason.

    Emits an ``enrollment_enabled`` activity -- the mirror of the
    ``enrollment_disabled`` row written on disable.
    """
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    disable_enrollment(database_connection, enrollment.id, "left company")
    updated = enable_enrollment(database_connection, enrollment.id)
    assert updated is not None
    assert updated.id == enrollment.id
    assert updated.status == "active"
    assert updated.disabled_reason is None
    same = get_enrollment(database_connection, workflow.id, contact.id)
    assert same is not None
    assert same.status == "active"
    activities = list_activities(database_connection, contact_id=contact.id)
    enabled_rows = [a for a in activities if a.type == "enrollment_enabled"]
    assert len(enabled_rows) == 1
    assert enabled_rows[0].enrollment_id == enrollment.id
    assert enabled_rows[0].workflow_id == workflow.id


def test_enable_enrollment_gate_blocks_active(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.15: the status='disabled' gate blocks enabling a live enrollment."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    assert enable_enrollment(database_connection, enrollment.id) is None


def test_enable_enrollment_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    assert enable_enrollment(database_connection, "nonexistent") is None


def test_disable_enrollment_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    updated = disable_enrollment(database_connection, "nonexistent", "reason")
    assert updated is None


def test_disable_enrollment_idempotent_re_disable(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Re-disabling overwrites reason; emits a second activity row.

    §V.10 enrollment coverage: ``changed`` semantics computed CLI-side via
    pre/post diff. At the DB layer, re-disable is a fresh UPDATE + activity
    INSERT (§V.14 append-only).
    """
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    first = disable_enrollment(database_connection, enrollment.id, "left company")
    assert first is not None
    second = disable_enrollment(database_connection, enrollment.id, "left company")
    assert second is not None
    assert second.status == "disabled"
    assert second.disabled_reason == "left company"
    activities = list_activities(database_connection, contact_id=contact.id)
    disabled_rows = [a for a in activities if a.type == "enrollment_disabled"]
    assert len(disabled_rows) == 2


def test_disable_enrollment_rejects_empty_reason(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Schema CHECK rejects disabled rows with empty reason (§V.15(+) coupling)."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    with pytest.raises(psycopg.errors.CheckViolation):
        disable_enrollment(database_connection, enrollment.id, "")


def test_list_enrollments_admits_disabled_status(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.15(+): disabled is a valid status enum value the filter passes through."""
    from mailpilot.database import list_enrollments

    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact_a = make_test_contact(database_connection, email="a@example.com")
    contact_b = make_test_contact(database_connection, email="b@example.com")
    e_a = make_test_enrollment(database_connection, workflow.id, contact_a.id)
    make_test_enrollment(database_connection, workflow.id, contact_b.id)
    disable_enrollment(database_connection, e_a.id, "left company")
    active = list_enrollments(database_connection, workflow.id, status="active")
    disabled = list_enrollments(database_connection, workflow.id, status="disabled")
    assert {e.contact_id for e in active} == {contact_b.id}
    assert {e.contact_id for e in disabled} == {contact_a.id}


def test_list_enrollments_detailed(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(
        database_connection, account_id=account.id, name="outbound-campaign"
    )
    contact = make_test_contact(database_connection, email="alice@example.com")
    update_contact(
        database_connection, contact.id, first_name="Alice", last_name="Smith"
    )
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    results = list_enrollments_detailed(database_connection, workflow_id=workflow.id)
    assert len(results) == 1
    detail = results[0]
    assert detail.id == enrollment.id
    assert detail.contact_email == "alice@example.com"
    assert detail.contact_name == "Alice Smith"
    assert detail.status == "active"
    assert detail.workflow_id == workflow.id
    assert detail.workflow_name == "outbound-campaign"
    assert detail.contact_id == contact.id


def test_list_enrollments_detailed_status_filter(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    c1 = make_test_contact(database_connection, email="a@example.com")
    c2 = make_test_contact(database_connection, email="b@example.com")
    e1 = make_test_enrollment(database_connection, workflow.id, c1.id)
    make_test_enrollment(database_connection, workflow.id, c2.id)
    disable_enrollment(database_connection, e1.id, "left company")
    results = list_enrollments_detailed(
        database_connection, workflow_id=workflow.id, status="disabled"
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


def test_enrollment_row_carries_parent_denorm_fields(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.5: Enrollment row gains workflow_name, contact_email, contact_name via JOIN.

    Asserts every getter (create, get_by_id, get composite, list, disable)
    returns the denormalised parent identifiers so all CLI surfaces inherit
    them symmetrically (mirrors ``Workflow.account_email``).
    """
    from mailpilot.database import (
        get_enrollment,
        get_enrollment_by_id,
        list_enrollments,
    )

    account = make_test_account(database_connection)
    workflow = make_test_workflow(
        database_connection, account_id=account.id, name="outbound-campaign"
    )
    contact = make_test_contact(database_connection, email="alice@example.com")
    update_contact(
        database_connection, contact.id, first_name="Alice", last_name="Smith"
    )

    created = create_enrollment(
        database_connection, workflow_id=workflow.id, contact_id=contact.id
    )
    assert created is not None
    assert created.workflow_name == "outbound-campaign"
    assert created.contact_email == "alice@example.com"
    assert created.contact_name == "Alice Smith"

    by_id = get_enrollment_by_id(database_connection, created.id)
    assert by_id is not None
    assert by_id.workflow_name == "outbound-campaign"
    assert by_id.contact_email == "alice@example.com"
    assert by_id.contact_name == "Alice Smith"

    by_composite = get_enrollment(database_connection, workflow.id, contact.id)
    assert by_composite is not None
    assert by_composite.workflow_name == "outbound-campaign"
    assert by_composite.contact_email == "alice@example.com"
    assert by_composite.contact_name == "Alice Smith"

    listed = list_enrollments(database_connection, workflow_id=workflow.id)
    assert len(listed) == 1
    assert listed[0].workflow_name == "outbound-campaign"
    assert listed[0].contact_email == "alice@example.com"
    assert listed[0].contact_name == "Alice Smith"

    disabled = disable_enrollment(database_connection, created.id, "wrap-up")
    assert disabled is not None
    assert disabled.workflow_name == "outbound-campaign"
    assert disabled.contact_email == "alice@example.com"
    assert disabled.contact_name == "Alice Smith"


def test_enrollment_status_check_rejects_paused(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.15/§V.88: status CHECK admits only {active, disabled}.

    `paused` is collapsed into `disabled`; it (and the never-valid lifecycle
    labels) are rejected at the schema level.
    """
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    for bad in ("paused", "pending", "completed", "failed"):
        with pytest.raises(psycopg.errors.CheckViolation):
            database_connection.execute(
                "UPDATE enrollment SET status = %s WHERE id = %s",
                (bad, enrollment.id),
            )
        database_connection.rollback()


def test_list_enrollments_detailed_limit(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    c1 = make_test_contact(database_connection, email="a@example.com")
    c2 = make_test_contact(database_connection, email="b@example.com")
    make_test_enrollment(database_connection, workflow.id, c1.id)
    make_test_enrollment(database_connection, workflow.id, c2.id)
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
    make_test_enrollment(database_connection, wf_a.id, contact.id)
    make_test_enrollment(database_connection, wf_b.id, contact.id)
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
    make_test_enrollment(database_connection, wf_a.id, contact.id)
    make_test_enrollment(database_connection, wf_b.id, contact.id)
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
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="follow up",
        scheduled_at="2026-04-22T12:00:00Z",
    )
    create_task(
        database_connection,
        enrollment_id=enrollment.id,
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
    enroll_a = make_test_enrollment(database_connection, workflow.id, contact_a.id)
    enroll_b = make_test_enrollment(database_connection, workflow.id, contact_b.id)
    create_task(
        database_connection,
        enrollment_id=enroll_a.id,
        workflow_id=workflow.id,
        contact_id=contact_a.id,
        description="task for A",
        scheduled_at="2026-04-22T12:00:00Z",
    )
    task_b = create_task(
        database_connection,
        enrollment_id=enroll_b.id,
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


def test_list_tasks_filters_by_trigger(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.133/§V.32: `--trigger` selects on context->>'trigger', shared w/ stats."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="first touch",
        scheduled_at="2026-04-22T12:00:00Z",
        context={"trigger": "enrollment_schedule"},
    )
    create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="deferred follow-up",
        scheduled_at="2026-04-23T12:00:00Z",
        context={"trigger": "task"},
    )
    # A row with no trigger key (default '{}' context) never matches a filter.
    create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="no trigger key",
        scheduled_at="2026-04-24T12:00:00Z",
    )

    first_touch = list_tasks(database_connection, trigger="enrollment_schedule")
    assert len(first_touch) == 1
    assert first_touch[0].description == "first touch"

    deferred = list_tasks(database_connection, trigger="task")
    assert len(deferred) == 1
    assert deferred[0].description == "deferred follow-up"


# -- get_task_stats (§V.133) ---------------------------------------------------


def test_get_task_stats_counts_per_status_total_and_window(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.133: per-status + total counts, distinct days, first/last scheduled_at."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    # Three pending tasks across two calendar days, plus one cancelled.
    create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="day 1 morning",
        scheduled_at="2026-04-22T09:00:00Z",
    )
    create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="day 1 evening",
        scheduled_at="2026-04-22T21:00:00Z",
    )
    create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="day 2",
        scheduled_at="2026-04-25T12:00:00Z",
    )
    to_cancel = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="to cancel",
        scheduled_at="2026-04-26T12:00:00Z",
    )
    cancel_task(database_connection, to_cancel.id)

    stats = get_task_stats(database_connection)
    assert stats.total == 4
    assert stats.pending == 3
    assert stats.completed == 0
    assert stats.failed == 0
    assert stats.cancelled == 1
    # Distinct UTC days: 2026-04-22, 2026-04-25, 2026-04-26.
    assert stats.distinct_scheduled_days == 3
    # Aware-datetime equality compares instants; the returned tzinfo reflects
    # the session TimeZone, which need not be UTC.
    assert stats.first_scheduled_at == datetime(2026, 4, 22, 9, tzinfo=UTC)
    assert stats.last_scheduled_at == datetime(2026, 4, 26, 12, tzinfo=UTC)


def test_get_task_stats_empty_set_is_all_zero(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.133: an empty task set returns zero counts and NULL first/last."""
    stats = get_task_stats(database_connection)
    assert stats.total == 0
    assert stats.pending == 0
    assert stats.distinct_scheduled_days == 0
    assert stats.first_scheduled_at is None
    assert stats.last_scheduled_at is None


def test_get_task_stats_bucket_tz_shifts_day_count(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.133: distinct_scheduled_days buckets in the supplied IANA timezone."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    # Both instants fall on 2026-04-22 in UTC, but straddle midnight in New York
    # (UTC-4 in April): 02:00Z -> 2026-04-21 22:00, 20:00Z -> 2026-04-22 16:00.
    create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="early",
        scheduled_at="2026-04-22T02:00:00Z",
    )
    create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="late",
        scheduled_at="2026-04-22T20:00:00Z",
    )

    assert get_task_stats(database_connection).distinct_scheduled_days == 1
    assert (
        get_task_stats(
            database_connection, bucket_tz="America/New_York"
        ).distinct_scheduled_days
        == 2
    )


def test_get_task_stats_filters_by_workflow_and_trigger(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.133/§V.107/§V.26: --workflow-id + --trigger narrow the aggregate set."""
    account = make_test_account(database_connection)
    workflow_a = make_test_workflow(
        database_connection, account_id=account.id, name="campaign-a"
    )
    workflow_b = make_test_workflow(
        database_connection, account_id=account.id, name="campaign-b"
    )
    contact = make_test_contact(database_connection)
    enroll_a = make_test_enrollment(database_connection, workflow_a.id, contact.id)
    enroll_b = make_test_enrollment(database_connection, workflow_b.id, contact.id)
    create_task(
        database_connection,
        enrollment_id=enroll_a.id,
        workflow_id=workflow_a.id,
        contact_id=contact.id,
        description="a first touch",
        scheduled_at="2026-04-22T12:00:00Z",
        context={"trigger": "enrollment_schedule"},
    )
    create_task(
        database_connection,
        enrollment_id=enroll_a.id,
        workflow_id=workflow_a.id,
        contact_id=contact.id,
        description="a follow-up",
        scheduled_at="2026-04-23T12:00:00Z",
        context={"trigger": "task"},
    )
    create_task(
        database_connection,
        enrollment_id=enroll_b.id,
        workflow_id=workflow_b.id,
        contact_id=contact.id,
        description="b first touch",
        scheduled_at="2026-04-22T12:00:00Z",
        context={"trigger": "enrollment_schedule"},
    )

    by_workflow = get_task_stats(database_connection, workflow_id=workflow_a.id)
    assert by_workflow.total == 2

    first_touch = get_task_stats(database_connection, trigger="enrollment_schedule")
    assert first_touch.total == 2

    by_both = get_task_stats(
        database_connection,
        workflow_id=workflow_a.id,
        trigger="enrollment_schedule",
    )
    assert by_both.total == 1


def test_find_pending_first_touch_task_none(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.32 idempotency probe returns None when no first-touch task exists."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    assert find_pending_first_touch_task(database_connection, enrollment.id) is None


def test_find_pending_first_touch_task_match(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.32: returns the pending email-less task for the given enrollment."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    created = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="scheduled first reach-out",
        scheduled_at="2026-04-22T12:00:00Z",
        context={"trigger": "enrollment_schedule"},
    )

    found = find_pending_first_touch_task(database_connection, enrollment.id)
    assert found is not None
    assert found.id == created.id
    assert found.email_id is None
    assert found.status == "pending"


def test_find_pending_first_touch_task_ignores_email_bound_tasks(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.32: inbound auto-tasks (email_id set) are not first-touch tasks."""
    from datetime import timedelta

    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    email = create_email(
        database_connection,
        gmail_message_id="msg-fp",
        gmail_thread_id="thread-fp",
        account_id=account.id,
        direction="inbound",
        subject="hi",
        body_text="ping",
        labels=["INBOX"],
        received_at=workflow.created_at + timedelta(minutes=5),
        contact_id=contact.id,
        workflow_id=workflow.id,
    )
    assert email is not None
    create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="handle inbound email",
        scheduled_at="2026-04-22T12:00:00Z",
        email_id=email.id,
    )
    assert find_pending_first_touch_task(database_connection, enrollment.id) is None


def test_find_pending_first_touch_task_ignores_terminal_status(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.32: only pending rows count; cancelled/completed do not block re-add."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    task = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="scheduled first reach-out",
        scheduled_at="2026-04-22T12:00:00Z",
        context={"trigger": "enrollment_schedule"},
    )
    cancel_task(database_connection, task.id)
    assert find_pending_first_touch_task(database_connection, enrollment.id) is None


def test_find_pending_first_touch_task_scoped_to_enrollment(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.32: lookup honours enrollment_id -- sibling enrollments invisible."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact_a = make_test_contact(database_connection, email="a@test.com")
    contact_b = make_test_contact(database_connection, email="b@test.com")
    enroll_a = make_test_enrollment(database_connection, workflow.id, contact_a.id)
    enroll_b = make_test_enrollment(database_connection, workflow.id, contact_b.id)
    create_task(
        database_connection,
        enrollment_id=enroll_a.id,
        workflow_id=workflow.id,
        contact_id=contact_a.id,
        description="scheduled first reach-out",
        scheduled_at="2026-04-22T12:00:00Z",
        context={"trigger": "enrollment_schedule"},
    )
    assert find_pending_first_touch_task(database_connection, enroll_b.id) is None
    assert find_pending_first_touch_task(database_connection, enroll_a.id) is not None


# -- cancel_enrollment_followup_tasks (§V.123) ---------------------------------

_FAR_FUTURE = "2099-12-31T00:00:00Z"
_FAR_PAST = "2000-01-01T00:00:00Z"


def test_cancel_enrollment_followup_tasks_cancels_future_pending(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.123: a future pending follow-up (email_id NULL) is cancelled."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    followup = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="breakup touch",
        scheduled_at=_FAR_FUTURE,
        context={"trigger": "followup"},
    )

    cancelled = cancel_enrollment_followup_tasks(database_connection, enrollment.id)

    assert [t.id for t in cancelled] == [followup.id]
    refetched = get_task(database_connection, followup.id)
    assert refetched is not None
    assert refetched.status == "cancelled"
    assert refetched.completed_at is not None


def test_cancel_enrollment_followup_tasks_cancels_email_bound_followup(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.123: a follow-up carrying email_id is still cancelled (keys on trigger)."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    email = create_email(
        database_connection,
        gmail_message_id="msg-fc",
        gmail_thread_id="thread-fc",
        account_id=account.id,
        direction="inbound",
        subject="hi",
        body_text="ping",
        contact_id=contact.id,
        workflow_id=workflow.id,
    )
    assert email is not None
    followup = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="follow-up bound to a prior email",
        scheduled_at=_FAR_FUTURE,
        email_id=email.id,
    )

    cancelled = cancel_enrollment_followup_tasks(database_connection, enrollment.id)

    assert [t.id for t in cancelled] == [followup.id]


def test_cancel_enrollment_followup_tasks_preserves_first_touch(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.123/§V.32: the enrollment_schedule first-touch is never cancelled."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    first_touch = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="scheduled first reach-out",
        scheduled_at=_FAR_FUTURE,
        context={"trigger": "enrollment_schedule"},
    )

    cancelled = cancel_enrollment_followup_tasks(database_connection, enrollment.id)

    assert cancelled == []
    refetched = get_task(database_connection, first_touch.id)
    assert refetched is not None
    assert refetched.status == "pending"


def test_cancel_enrollment_followup_tasks_skips_already_due(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.123: a task already due (scheduled_at <= now) is left to fire."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    due = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="already due",
        scheduled_at=_FAR_PAST,
        context={"trigger": "followup"},
    )

    cancelled = cancel_enrollment_followup_tasks(database_connection, enrollment.id)

    assert cancelled == []
    refetched = get_task(database_connection, due.id)
    assert refetched is not None
    assert refetched.status == "pending"


def test_cancel_enrollment_followup_tasks_skips_non_pending(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.123: completed/cancelled tasks are untouched."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    task = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="future but completed",
        scheduled_at=_FAR_FUTURE,
        context={"trigger": "followup"},
    )
    complete_task(database_connection, task.id, status="completed")

    cancelled = cancel_enrollment_followup_tasks(database_connection, enrollment.id)

    assert cancelled == []


def test_cancel_enrollment_followup_tasks_scoped_to_enrollment(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.123: a sibling enrollment's follow-up is invisible."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact_a = make_test_contact(database_connection, email="a@test.com")
    contact_b = make_test_contact(database_connection, email="b@test.com")
    enroll_a = make_test_enrollment(database_connection, workflow.id, contact_a.id)
    enroll_b = make_test_enrollment(database_connection, workflow.id, contact_b.id)
    followup_b = create_task(
        database_connection,
        enrollment_id=enroll_b.id,
        workflow_id=workflow.id,
        contact_id=contact_b.id,
        description="b breakup touch",
        scheduled_at=_FAR_FUTURE,
        context={"trigger": "followup"},
    )

    cancelled = cancel_enrollment_followup_tasks(database_connection, enroll_a.id)

    assert cancelled == []
    refetched = get_task(database_connection, followup_b.id)
    assert refetched is not None
    assert refetched.status == "pending"


# -- record_enrollment_outcome (§V.15) -----------------------------------------


def test_record_enrollment_outcome_writes_timeline_activity(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.15: a completed outcome writes a timeline activity, row untouched."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    company = make_test_company(database_connection)
    contact = make_test_contact(database_connection, company_id=company.id)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

    activity = record_enrollment_outcome(
        database_connection, enrollment.id, "completed", "meeting booked"
    )

    assert activity.type == "enrollment_completed"
    assert activity.enrollment_id == enrollment.id
    assert activity.workflow_id == workflow.id
    assert activity.contact_id == contact.id
    assert activity.company_id == company.id
    # §V.15: enrollment row status is unchanged by an outcome.
    refetched = get_enrollment(database_connection, workflow.id, contact.id)
    assert refetched is not None
    assert refetched.status == "active"


def test_record_enrollment_outcome_rejects_invalid_outcome(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

    with pytest.raises(ValueError, match="completed or failed"):
        record_enrollment_outcome(database_connection, enrollment.id, "booked", "nope")


def test_record_enrollment_outcome_persists_disposition_in_detail(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.132: a supplied disposition lands in the activity ``detail`` JSONB."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

    activity = record_enrollment_outcome(
        database_connection,
        enrollment.id,
        "completed",
        "meeting booked",
        disposition="meeting_booked",
    )

    assert activity.detail["disposition"] == "meeting_booked"
    assert activity.detail["reason"] == "meeting booked"


def test_record_enrollment_outcome_omits_disposition_when_absent(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.132: omitting disposition writes no key (legacy/forward gap)."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

    activity = record_enrollment_outcome(
        database_connection, enrollment.id, "failed", "hard bounce"
    )

    assert "disposition" not in activity.detail


# -- get_latest_enrollment_outcome (§V.83) -------------------------------------


def test_get_latest_enrollment_outcome_none_when_no_outcome(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.83: an enrollment with no recorded outcome reports no terminal state."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

    assert get_latest_enrollment_outcome(database_connection, enrollment.id) is None


def test_get_latest_enrollment_outcome_returns_latest_terminal(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.83/§V.15: the newest terminal outcome activity wins."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

    record_enrollment_outcome(
        database_connection,
        enrollment.id,
        "failed",
        "not now",
        disposition="contact_later",
    )
    record_enrollment_outcome(
        database_connection,
        enrollment.id,
        "completed",
        "meeting booked",
        disposition="meeting_booked",
    )

    assert (
        get_latest_enrollment_outcome(database_connection, enrollment.id) == "completed"
    )


# -- has_inbound_email_from_contact_after (§V.83) ------------------------------


def test_has_inbound_email_from_contact_after_true_for_later_inbound(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.83: an inbound email from the contact after the anchor is detected."""
    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    prior_touch_at = datetime(2024, 1, 1, tzinfo=UTC)
    create_email(
        database_connection,
        account_id=account.id,
        contact_id=contact.id,
        direction="inbound",
        gmail_message_id="msg_reply",
        received_at=datetime(2024, 1, 2, tzinfo=UTC),
    )

    assert has_inbound_email_from_contact_after(
        database_connection, contact.id, prior_touch_at
    )


def test_has_inbound_email_from_contact_after_false_when_none_later(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.83: earlier inbound and later outbound do not count as a reply."""
    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    prior_touch_at = datetime(2024, 1, 2, tzinfo=UTC)
    # Inbound before the anchor -- an earlier reply, not a fresh one.
    create_email(
        database_connection,
        account_id=account.id,
        contact_id=contact.id,
        direction="inbound",
        gmail_message_id="msg_old_reply",
        received_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    # Outbound after the anchor -- our own later touch, not the contact's reply.
    create_email(
        database_connection,
        account_id=account.id,
        contact_id=contact.id,
        direction="outbound",
        sent_at=datetime(2024, 1, 3, tzinfo=UTC),
    )

    assert not has_inbound_email_from_contact_after(
        database_connection, contact.id, prior_touch_at
    )


# -- get_workflow_stats (§V.132) -----------------------------------------------


def test_get_workflow_stats_returns_none_for_unknown_workflow(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.107: an unknown workflow ref resolves to None (CLI maps -> not_found)."""
    assert (
        get_workflow_stats(database_connection, "01234567-0000-7000-0000-0000000000ff")
        is None
    )


def test_get_workflow_stats_counts_all_funnel_stages(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.132: each of the 8 stages counts at enrollment grain."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="outbound"
    )

    # enrolled-only contact (no email, no outcome) -> counts only in `enrolled`+`active`.
    company = make_test_company(database_connection)
    enrolled_only = make_test_contact(
        database_connection, email="enrolled@testcorp.com", company_id=company.id
    )
    make_test_enrollment(database_connection, workflow.id, enrolled_only.id)

    # sent contact -> outbound status='sent' email.
    sent_contact = make_test_contact(
        database_connection, email="sent@testcorp.com", company_id=company.id
    )
    make_test_enrollment(database_connection, workflow.id, sent_contact.id)
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        status="sent",
        contact_id=sent_contact.id,
        workflow_id=workflow.id,
    )

    # bounced contact -> outbound status='bounced' email.
    bounced_contact = make_test_contact(
        database_connection, email="bounced@testcorp.com", company_id=company.id
    )
    make_test_enrollment(database_connection, workflow.id, bounced_contact.id)
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        status="bounced",
        contact_id=bounced_contact.id,
        workflow_id=workflow.id,
    )

    # replied contact -> inbound routed email.
    replied_contact = make_test_contact(
        database_connection, email="replied@testcorp.com", company_id=company.id
    )
    make_test_enrollment(database_connection, workflow.id, replied_contact.id)
    create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        status="received",
        contact_id=replied_contact.id,
        workflow_id=workflow.id,
        is_routed=True,
        route_method="classified",
    )

    # booked contact -> completed outcome (disposition meeting_booked).
    booked_contact = make_test_contact(
        database_connection, email="booked@testcorp.com", company_id=company.id
    )
    booked = make_test_enrollment(database_connection, workflow.id, booked_contact.id)
    record_enrollment_outcome(
        database_connection,
        booked.id,
        "completed",
        "meeting booked",
        disposition="meeting_booked",
    )

    # contact_later contact -> failed outcome (disposition contact_later).
    later_contact = make_test_contact(
        database_connection, email="later@testcorp.com", company_id=company.id
    )
    later = make_test_enrollment(database_connection, workflow.id, later_contact.id)
    record_enrollment_outcome(
        database_connection,
        later.id,
        "failed",
        "circle back",
        disposition="contact_later",
    )

    # do_not_contact contact -> failed outcome (disposition do_not_contact).
    dnc_contact = make_test_contact(
        database_connection, email="dnc@testcorp.com", company_id=company.id
    )
    dnc = make_test_enrollment(database_connection, workflow.id, dnc_contact.id)
    record_enrollment_outcome(
        database_connection,
        dnc.id,
        "failed",
        "opted out",
        disposition="do_not_contact",
    )

    stats = get_workflow_stats(database_connection, workflow.id)
    assert stats is not None
    assert stats.workflow_id == workflow.id
    assert stats.workflow_name == workflow.name
    assert stats.enrolled == 7
    assert stats.sent == 1
    assert stats.bounced == 1
    assert stats.replied == 1
    assert stats.meeting_booked == 1
    assert stats.contact_later == 1
    assert stats.do_not_contact == 1
    # active = status='active' enrollments with no terminal outcome: the
    # enrolled-only, sent, bounced, and replied contacts (4); the three
    # outcome-bearing rows drop out.
    assert stats.active == 4


def test_get_workflow_stats_multi_touch_not_double_counted(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.132: a multi-touch enrollment counts once at enrollment grain."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="outbound"
    )
    contact = make_test_contact(database_connection)
    make_test_enrollment(database_connection, workflow.id, contact.id)
    for touch in range(3):
        create_email(
            database_connection,
            account_id=account.id,
            direction="outbound",
            status="sent",
            subject=f"Touch {touch}",
            contact_id=contact.id,
            workflow_id=workflow.id,
        )

    stats = get_workflow_stats(database_connection, workflow.id)
    assert stats is not None
    assert stats.enrolled == 1
    assert stats.sent == 1


# -- list_active_outbound_enrollments_for_contact (§V.128) ---------------------


def test_list_active_outbound_enrollments_filters_by_direction_and_status(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.128: only active enrollments in outbound workflows are returned."""
    account = make_test_account(database_connection)
    outbound = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="outbound",
        workflow_type="outbound",
    )
    inbound = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="inbound",
        workflow_type="inbound",
    )
    disabled_outbound = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="disabled-outbound",
        workflow_type="outbound",
    )
    contact = make_test_contact(database_connection)
    live = make_test_enrollment(database_connection, outbound.id, contact.id)
    make_test_enrollment(database_connection, inbound.id, contact.id)
    halted = make_test_enrollment(database_connection, disabled_outbound.id, contact.id)
    disable_enrollment(database_connection, halted.id, "operator halt")

    result = list_active_outbound_enrollments_for_contact(
        database_connection, contact.id
    )

    assert [e.id for e in result] == [live.id]


def test_create_tasks_for_routed_emails(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from datetime import timedelta

    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

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
    assert created[0].enrollment_id == enrollment.id
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
    make_test_enrollment(database_connection, workflow.id, contact.id)

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
    make_test_enrollment(database_connection, workflow.id, contact.id)

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
    make_test_enrollment(database_connection, workflow.id, contact.id)

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
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)

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
        enrollment_id=enrollment.id,
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
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    task = create_task(
        database_connection,
        enrollment_id=enrollment.id,
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


def test_reschedule_task_for_retry_bumps_attempt_and_advances_scheduled_at(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.49: status stays pending; attempt_count bumped; scheduled_at
    advances by backoff."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    task = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="follow up",
        scheduled_at="2026-04-22T12:00:00Z",
    )

    rescheduled = reschedule_task_for_retry(
        database_connection,
        task.id,
        backoff_seconds=30,
        exc=RuntimeError("503 unavailable"),
    )

    assert rescheduled is not None
    assert rescheduled.status == "pending"
    assert rescheduled.attempt_count == 1
    assert rescheduled.completed_at is None
    last = rescheduled.result["last_error"]
    assert isinstance(last, dict)
    assert last["type"] == "RuntimeError"
    assert "503" in cast(str, last["message"])

    # Re-driving bumps attempt_count again
    rescheduled = reschedule_task_for_retry(
        database_connection,
        task.id,
        backoff_seconds=120,
        exc=RuntimeError("again"),
    )
    assert rescheduled is not None
    assert rescheduled.attempt_count == 2


def test_reschedule_task_for_retry_returns_none_for_unknown_id(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    rescheduled = reschedule_task_for_retry(
        database_connection,
        "01234567-0000-7000-0000-000000000000",
        backoff_seconds=30,
        exc=RuntimeError("nope"),
    )
    assert rescheduled is None


def test_manual_retry_task_resets_failed_row(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.49: failed -> pending, attempt_count=0, scheduled_at=now()."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    task = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="follow up",
        scheduled_at="2026-04-22T12:00:00Z",
    )
    complete_task(
        database_connection,
        task.id,
        status="failed",
        result={"reason": "boom"},
    )

    reset = manual_retry_task(database_connection, task.id)

    assert reset is not None
    assert reset.status == "pending"
    assert reset.attempt_count == 0
    assert reset.completed_at is None


def test_manual_retry_task_resets_cancelled_row(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    task = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="follow up",
        scheduled_at="2026-04-22T12:00:00Z",
    )
    cancel_task(database_connection, task.id)

    reset = manual_retry_task(database_connection, task.id)

    assert reset is not None
    assert reset.status == "pending"


def test_manual_retry_task_refuses_completed_row(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.49: completed rows refuse retry -- tools already fired, replay
    risks duplicate side-effects."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    task = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="follow up",
        scheduled_at="2026-04-22T12:00:00Z",
    )
    complete_task(database_connection, task.id, status="completed", result={})

    assert manual_retry_task(database_connection, task.id) is None


def test_manual_retry_task_refuses_pending_row(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.49: pending rows are no-op -- already queued."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    task = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="follow up",
        scheduled_at="2026-04-22T12:00:00Z",
    )
    assert manual_retry_task(database_connection, task.id) is None


# -- List vs view contract -----------------------------------------------------
#
# Per CLAUDE.md "list (summary), view ID (full record)" rule:
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
    assert not hasattr(companies[0], "updated_at")
    full = get_company(database_connection, companies[0].id)
    assert full is not None
    assert hasattr(full, "updated_at")


def test_contact_list_summary_get_full(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.models import ContactSummary

    make_test_contact(database_connection)
    contacts = list_contacts(database_connection)
    assert isinstance(contacts[0], ContactSummary)
    assert not hasattr(contacts[0], "updated_at")
    full = get_contact(database_connection, contacts[0].id)
    assert full is not None
    assert hasattr(full, "updated_at")
    assert full.disabled_reason is None


def test_workflow_list_summary_get_full(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.models import WorkflowSummary

    account = make_test_account(database_connection)
    make_test_workflow(database_connection, account_id=account.id)
    workflows = list_workflows(database_connection)
    assert isinstance(workflows[0], WorkflowSummary)
    assert not hasattr(workflows[0], "goal")
    full = get_workflow(database_connection, workflows[0].id)
    assert full is not None
    assert hasattr(full, "goal")


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
    assert not hasattr(emails[0], "labels")
    # recipients rides the summary now (§V.7) -- the campaign-test delivery key.
    assert emails[0].recipients == {"to": ["x@y.com"]}
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
    assert not hasattr(companies[0], "updated_at")
    full = get_company(database_connection, companies[0].id)
    assert full is not None
    assert hasattr(full, "updated_at")


def test_contact_search_summary_get_full(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.models import ContactSummary

    make_test_contact(database_connection, email="alice@example.com")
    contacts = search_contacts(database_connection, "alice")
    assert isinstance(contacts[0], ContactSummary)
    assert not hasattr(contacts[0], "updated_at")
    full = get_contact(database_connection, contacts[0].id)
    assert full is not None
    assert hasattr(full, "updated_at")


def test_workflow_search_summary_get_full(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.models import WorkflowSummary

    account = make_test_account(database_connection)
    make_test_workflow(database_connection, account_id=account.id, name="outreach")
    workflows = search_workflows(database_connection, "outreach")
    assert isinstance(workflows[0], WorkflowSummary)
    assert not hasattr(workflows[0], "goal")
    full = get_workflow(database_connection, workflows[0].id)
    assert full is not None
    assert hasattr(full, "goal")


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
    assert not hasattr(emails[0], "labels")
    # recipients rides the summary now (§V.7) -- both projections carry it.
    assert emails[0].recipients == {"to": ["client@example.com"]}
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
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    create_task(
        database_connection,
        enrollment_id=enrollment.id,
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


def test_activity_list_summary_includes_fk_columns(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.7: list-view projection MUST expose FK columns the schema declares."""
    account = make_test_account(database_connection)
    contact = make_test_contact(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    email = create_email(
        database_connection,
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=workflow.id,
        direction="outbound",
        subject="Hello",
        body_text="Hi",
        status="sent",
        gmail_message_id="msg_fk_summary",
    )
    assert email is not None
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    task = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="follow up",
        scheduled_at="2024-01-01T00:00:00+00:00",
    )
    create_activity(
        database_connection,
        contact_id=contact.id,
        activity_type="email_sent",
        summary="sent X",
        email_id=email.id,
        workflow_id=workflow.id,
        task_id=task.id,
    )
    activities = list_activities(database_connection, contact_id=contact.id)
    assert len(activities) == 1
    row = activities[0]
    assert row.email_id == email.id
    assert row.workflow_id == workflow.id
    assert row.task_id == task.id


def test_activity_list_summary_fk_columns_null_when_absent(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.7: FK fields are present (null) when the activity has no FK linkage."""
    contact = make_test_contact(database_connection)
    create_activity(
        database_connection,
        contact_id=contact.id,
        activity_type="tag_added",
        summary="tagged X",
    )
    activities = list_activities(database_connection, contact_id=contact.id)
    assert len(activities) == 1
    row = activities[0]
    assert row.email_id is None
    assert row.workflow_id is None
    assert row.task_id is None


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
    make_test_workflow(database_connection, account_id=account.id, name="a")
    make_test_workflow(database_connection, account_id=account.id, name="b")
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
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    create_task(
        database_connection,
        enrollment_id=enrollment.id,
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
    make_test_tag(database_connection, name="a")
    make_test_tag(database_connection, name="b")
    assert len(list_tags(database_connection, limit=1)) == 1
    assert (
        len(
            list_tags(
                database_connection,
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


# -- load_contact_view / load_company_view (§V.8) -----------------------------


def test_load_contact_view_returns_none_when_missing(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.database import load_contact_view

    assert load_contact_view(database_connection, "nonexistent-id") is None


def test_load_company_view_returns_none_when_missing(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.database import load_company_view

    assert load_company_view(database_connection, "nonexistent-id") is None


def test_load_contact_view_orphan_has_empty_arrays(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Contact w/o company_id => company_notes=[], company_notes_total=0."""
    from mailpilot.database import load_contact_view

    contact = make_test_contact(database_connection, email="solo@example.com")

    view = load_contact_view(database_connection, contact.id)

    assert view is not None
    assert view.id == contact.id
    assert view.company_id is None
    assert view.notes == []
    assert view.notes_total == 0
    assert view.company_notes == []
    assert view.company_notes_total == 0


def test_load_contact_view_inlines_own_and_company_notes(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """notes + company_notes both inlined when company_id set, DESC order."""
    from mailpilot.database import create_note, load_contact_view

    company = make_test_company(database_connection, name="Acme", domain="acme.com")
    contact = make_test_contact(
        database_connection, email="alice@acme.com", company_id=company.id
    )
    older_contact_note = create_note(
        database_connection, body="Older", contact_id=contact.id
    )
    newer_contact_note = create_note(
        database_connection, body="Newer", contact_id=contact.id
    )
    company_note = create_note(
        database_connection, body="Strategic account", company_id=company.id
    )

    view = load_contact_view(database_connection, contact.id)

    assert view is not None
    assert view.company_id == company.id
    assert [n.id for n in view.notes] == [
        newer_contact_note.id,
        older_contact_note.id,
    ]
    assert view.notes_total == 2
    assert [n.id for n in view.company_notes] == [company_note.id]
    assert view.company_notes_total == 1


def test_load_contact_view_caps_notes_at_ten(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """When >10 notes exist, list capped at 10 but total reflects actual count."""
    from mailpilot.database import (
        _INLINE_NOTES_CAP,  # pyright: ignore[reportPrivateUsage]
        create_note,
        load_contact_view,
    )

    contact = make_test_contact(database_connection, email="busy@example.com")
    for i in range(_INLINE_NOTES_CAP + 5):
        create_note(database_connection, body=f"note {i}", contact_id=contact.id)

    view = load_contact_view(database_connection, contact.id)

    assert view is not None
    assert len(view.notes) == _INLINE_NOTES_CAP
    assert view.notes_total == _INLINE_NOTES_CAP + 5


def test_load_contact_view_preserves_full_body_verbatim(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Notes body must be verbatim, not preview-style truncation."""
    from mailpilot.database import create_note, load_contact_view

    contact = make_test_contact(database_connection, email="long@example.com")
    long_body = "x" * 500
    create_note(database_connection, body=long_body, contact_id=contact.id)

    view = load_contact_view(database_connection, contact.id)

    assert view is not None
    assert view.notes[0].body == long_body


def test_load_company_view_inlines_notes_desc(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.database import create_note, load_company_view

    company = make_test_company(database_connection, name="Acme", domain="acme.com")
    older = create_note(database_connection, body="Older", company_id=company.id)
    newer = create_note(database_connection, body="Newer", company_id=company.id)

    view = load_company_view(database_connection, company.id)

    assert view is not None
    assert view.id == company.id
    assert [n.id for n in view.notes] == [newer.id, older.id]
    assert view.notes_total == 2


def test_load_company_view_caps_notes_at_ten(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.database import (
        _INLINE_NOTES_CAP,  # pyright: ignore[reportPrivateUsage]
        create_note,
        load_company_view,
    )

    company = make_test_company(database_connection, name="Acme", domain="acme.com")
    for i in range(_INLINE_NOTES_CAP + 3):
        create_note(database_connection, body=f"note {i}", company_id=company.id)

    view = load_company_view(database_connection, company.id)

    assert view is not None
    assert len(view.notes) == _INLINE_NOTES_CAP
    assert view.notes_total == _INLINE_NOTES_CAP + 3


def test_load_company_view_surfaces_profile_field(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """View ⊇ Company shape per §V.8 — profile column forwarded (§B.67 guard)."""
    from mailpilot.database import load_company_view, update_company

    company = make_test_company(database_connection, name="Acme", domain="acme.com")
    profile = {
        "summary": "Acme makes widgets for industrial customers.",
        "products": ["Widget A", "Widget B"],
        "target_customers": "Mid-market manufacturers.",
        "timezone": "America/Toronto",
        "sources": ["https://acme.com"],
    }
    updated = update_company(database_connection, company.id, profile=profile)
    assert updated is not None
    assert updated.profile == profile

    view = load_company_view(database_connection, company.id)

    assert view is not None
    assert view.profile == profile
    assert view.tags == []


def test_load_company_view_projects_tags_same_shape_as_list(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.8/§V.116: company view tags[] matches list projection (names, empty ok)."""
    from mailpilot.database import load_company_view

    company = make_test_company(database_connection, name="Acme", domain="acme.com")
    bare = make_test_company(database_connection, name="Bare", domain="bare.com")
    make_test_tag_assignment(database_connection, company_id=company.id, name="vip")
    make_test_tag_assignment(database_connection, company_id=company.id, name="partner")

    view = load_company_view(database_connection, company.id)
    bare_view = load_company_view(database_connection, bare.id)
    listed = {c.id: c for c in list_companies(database_connection)}

    assert view is not None
    assert bare_view is not None
    assert view.tags == ["partner", "vip"]
    assert bare_view.tags == []
    assert view.tags == listed[company.id].tags
    assert bare_view.tags == listed[bare.id].tags


def test_load_contact_view_carries_title_and_email_confidence(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.8/§B.94: ContactView forwards title + email_confidence (§V.95).

    These flat lead-metadata columns were silently dropped by Pydantic
    ``extra=ignore`` when ``ContactView`` omitted them from its field set —
    the same invisible-projection-drift class as the §B.67 ``profile`` guard.
    """
    from mailpilot.database import create_contact, load_contact_view

    contact = create_contact(
        database_connection,
        email="lead@example.com",
        title="VP Engineering",
        email_confidence=87,
    )
    assert contact is not None

    view = load_contact_view(database_connection, contact.id)

    assert view is not None
    assert view.title == "VP Engineering"
    assert view.email_confidence == 87


def test_create_contact_stores_verification_meta(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.144: verification_meta JSONB round-trips on Contact row."""
    from mailpilot.database import create_contact, get_contact

    meta = {"bouncer_status": "deliverable", "source": "hunter_pattern"}
    contact = create_contact(
        database_connection,
        email="meta@example.com",
        email_confidence=98,
        verification_meta=meta,
    )
    assert contact is not None
    assert contact.verification_meta == meta

    reloaded = get_contact(database_connection, contact.id)
    assert reloaded is not None
    assert reloaded.verification_meta == meta


def test_load_contact_view_omits_verification_meta(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.144/§V.8: default ContactView never projects verification_meta."""
    from mailpilot.database import create_contact, load_contact_view
    from mailpilot.models import ContactView

    meta = {"bouncer_status": "risky", "source": "manual"}
    contact = create_contact(
        database_connection,
        email="view-meta@example.com",
        verification_meta=meta,
    )
    assert contact is not None

    view = load_contact_view(database_connection, contact.id)
    assert view is not None
    assert "verification_meta" not in ContactView.model_fields
    assert "verification_meta" not in view.model_dump()
    assert "bouncer_status" not in view.model_dump_json()


def test_load_contact_view_carries_company_domain(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.8/§V.5: ContactView carries company_domain (LEFT JOIN company).

    NULL when the contact has no parent company.
    """
    from mailpilot.database import load_contact_view

    company = make_test_company(database_connection, name="Acme", domain="acme.com")
    joined = make_test_contact(
        database_connection, email="alice@acme.com", company_id=company.id
    )
    orphan = make_test_contact(database_connection, email="solo@example.com")

    joined_view = load_contact_view(database_connection, joined.id)
    orphan_view = load_contact_view(database_connection, orphan.id)

    assert joined_view is not None
    assert joined_view.company_domain == "acme.com"
    assert orphan_view is not None
    assert orphan_view.company_domain is None


def test_contact_view_field_set_superset_of_base_and_summary() -> None:
    """§V.8/§B.94: ContactView field set ⊇ agent-facing Contact + ContactSummary.

    Recurrence guard: a view model omitting a base column is silently stripped
    from ``**contact.model_dump()`` (Pydantic ``extra=ignore``), so the field
    set is tracked against both the base entity and the list/search summary.

    ``verification_meta`` is operator-only (§V.144) and intentionally omitted
    from the default view (opt-in via ``contact view --include-meta``).
    """
    from mailpilot.models import Contact, ContactSummary, ContactView

    # Operator-only columns excluded from the agent-facing view projection.
    operator_only = {"verification_meta"}
    base = set(Contact.model_fields) - operator_only
    summary = set(ContactSummary.model_fields)
    view = set(ContactView.model_fields)

    assert base <= view, f"ContactView missing base columns: {base - view}"
    assert summary <= view, f"ContactView missing summary denorm: {summary - view}"
    assert {"title", "email_confidence", "company_domain"} <= view
    assert "verification_meta" not in view


# -- _BASE template fragment carries §V.8 directive --------------------------


def test_base_fragment_mentions_notes_directive() -> None:
    """_BASE must contain the §V.8 / §V.135 personalize-from-notes directive once.

    Post-T209 the notes arrive through the pre-fed ``Contact record:`` /
    ``Company record:`` sections (§V.135) rather than a read tool, but the
    directive to treat those notes as personalization context still stands.
    """
    from mailpilot.agent.templates import (
        _BASE,  # pyright: ignore[reportPrivateUsage]
    )

    needle = "as context for personalizing your response"
    assert needle in _BASE
    assert _BASE.count(needle) == 1


# -- §V.16(+) ON CONFLICT race-safety on create_<noun> ------------------------


def test_create_account_returns_none_on_duplicate_email(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.16(+): re-create against existing email returns None, does not raise."""
    first = create_account(database_connection, email="dup@example.com")
    assert first is not None
    second = create_account(database_connection, email="dup@example.com")
    assert second is None


def test_create_company_returns_none_on_duplicate_domain(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.16(+): re-create against existing domain returns None, does not raise."""
    first = create_company(database_connection, name="Acme", domain="dup.com")
    assert first is not None
    second = create_company(database_connection, name="Acme 2", domain="dup.com")
    assert second is None


def test_company_alias_resolve_view_and_contact_link(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.142: alias resolve on get/view; contact --company-domain alias links."""
    company = create_company(
        database_connection,
        name="SVA Consulting",
        domain="sva.com",
        aliases=["consulting.sva.com", "SVA.COM.ALIAS"],
    )
    assert company is not None
    assert company.domain == "sva.com"
    assert list_company_aliases(database_connection, company.id) == [
        "consulting.sva.com",
        "sva.com.alias",
    ]

    by_alias = get_company_by_domain(database_connection, "consulting.sva.com")
    assert by_alias is not None
    assert by_alias.id == company.id
    assert by_alias.domain == "sva.com"

    # Case-insensitive resolve.
    by_case = get_company_by_domain(database_connection, "Consulting.SVA.com")
    assert by_case is not None
    assert by_case.id == company.id

    view = load_company_view(database_connection, company.id)
    assert view is not None
    assert view.aliases == ["consulting.sva.com", "sva.com.alias"]

    contact = create_contact(
        database_connection,
        email="lead@sva.com",
        company_id=by_alias.id,
    )
    assert contact is not None
    assert contact.company_id == company.id

    # Exact lookup does not follow alias.
    assert (
        get_company_by_domain_exact(database_connection, "consulting.sva.com") is None
    )


def test_create_company_rejects_domain_already_alias(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.142: no silent second firm when domain is a known alias."""
    first = create_company(
        database_connection,
        name="SVA",
        domain="sva.com",
        aliases=["consulting.sva.com"],
    )
    assert first is not None
    second = create_company(
        database_connection,
        name="Fake SVA",
        domain="consulting.sva.com",
    )
    assert second is None
    third = create_company(
        database_connection,
        name="Other",
        domain="other.com",
        aliases=["sva.com"],
    )
    assert third is None


def test_search_companies_matches_alias(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.142: search by alias domain finds the canonical company."""
    company = create_company(
        database_connection,
        name="Stellar",
        domain="stellarone.io",
        aliases=["stellarone.com"],
    )
    assert company is not None
    results = search_companies(database_connection, "stellarone.com")
    assert len(results) == 1
    assert results[0].id == company.id
    assert results[0].domain == "stellarone.io"


def test_merge_companies_disables_source_and_optional_contact_move(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.143: merge aliases source domain, disables source, moves contacts."""
    survivor = create_company(
        database_connection, name="Net@Work", domain="netatwork.com"
    )
    source = create_company(database_connection, name="Nexvue", domain="nexvue.com")
    assert survivor is not None
    assert source is not None
    contact = create_contact(
        database_connection, email="a@nexvue.com", company_id=source.id
    )
    assert contact is not None

    merged = merge_companies(
        database_connection,
        source.id,
        survivor.id,
        move_contacts=True,
        original_from_domain="nexvue.com",
    )
    assert merged is not None
    assert merged.id == survivor.id
    assert "nexvue.com" in list_company_aliases(database_connection, survivor.id)

    source_after = get_company(database_connection, source.id)
    assert source_after is not None
    assert source_after.disabled_reason == "merged:into netatwork.com"
    assert source_after.domain.startswith("__merged__.")

    contact_after = get_contact(database_connection, contact.id)
    assert contact_after is not None
    assert contact_after.company_id == survivor.id

    # Alias resolve hits survivor.
    resolved = get_company_by_domain(database_connection, "nexvue.com")
    assert resolved is not None
    assert resolved.id == survivor.id

    # Idempotent re-merge.
    again = merge_companies(
        database_connection,
        source.id,
        survivor.id,
        move_contacts=False,
        original_from_domain="nexvue.com",
    )
    assert again is not None
    assert again.id == survivor.id


def test_merge_companies_without_move_leaves_contacts(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.143: omit --move-contacts leaves contacts on disabled source."""
    survivor = create_company(database_connection, name="Into", domain="into.com")
    source = create_company(database_connection, name="From", domain="from.com")
    assert survivor is not None
    assert source is not None
    contact = create_contact(
        database_connection, email="stay@from.com", company_id=source.id
    )
    assert contact is not None

    merge_companies(
        database_connection,
        source.id,
        survivor.id,
        move_contacts=False,
        original_from_domain="from.com",
    )
    contact_after = get_contact(database_connection, contact.id)
    assert contact_after is not None
    assert contact_after.company_id == source.id


def test_create_contact_returns_none_on_duplicate_email(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.16(+): re-create against existing email returns None, does not raise."""
    first = create_contact(database_connection, email="dup@example.com")
    assert first is not None
    second = create_contact(database_connection, email="dup@example.com")
    assert second is None


def test_create_workflow_returns_none_on_duplicate_name(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.16(+): re-create against an existing global ``name`` returns None."""
    account = make_test_account(database_connection)
    first = create_workflow(
        database_connection,
        name="dup-workflow",
        template="outbound-general",
        account_id=account.id,
    )
    assert first is not None
    second = create_workflow(
        database_connection,
        name="dup-workflow",
        template="outbound-general",
        account_id=account.id,
    )
    assert second is None


def test_create_workflow_name_globally_unique_across_accounts(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.90/§V.103: ``workflow.name`` is global -- two accounts cannot share it."""
    a1 = make_test_account(database_connection, email="a@test.com")
    a2 = make_test_account(database_connection, email="b@test.com")
    first = create_workflow(
        database_connection,
        name="shared-name",
        template="outbound-general",
        account_id=a1.id,
    )
    assert first is not None
    second = create_workflow(
        database_connection,
        name="shared-name",
        template="outbound-general",
        account_id=a2.id,
    )
    assert second is None


def test_create_workflow_rejects_non_kebab_name(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.90/§V.103: the kebab CHECK rejects a non-kebab ``name``."""
    account = make_test_account(database_connection)
    with pytest.raises(psycopg.errors.CheckViolation):
        create_workflow(
            database_connection,
            name="Not Kebab",
            template="outbound-general",
            account_id=account.id,
        )


def test_get_workflow_by_name_case_insensitive(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.90/§V.107: ``get_workflow_by_name`` resolves the natural key case-folded."""
    account = make_test_account(database_connection)
    created = make_test_workflow(
        database_connection, account_id=account.id, name="ai-engineering"
    )
    found = get_workflow_by_name(database_connection, "AI-Engineering")
    assert found is not None
    assert found.id == created.id
    assert get_workflow_by_name(database_connection, "no-such-flow") is None


def test_create_or_get_contact_by_email_concurrent_is_safe(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.16(+) TOCTOU: two threads racing on a novel email both get the same row."""
    email = "racer@example.com"
    thread_count = 2
    barrier = threading.Barrier(thread_count)
    results: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        conn = cast(
            psycopg.Connection[dict[str, Any]],
            psycopg.connect(TEST_DATABASE_URL, row_factory=dict_row),  # type: ignore[arg-type]
        )
        try:
            barrier.wait(timeout=5)
            row = create_or_get_contact_by_email(conn, email=email)
            with lock:
                results.append(row.id)
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

    assert errors == [], errors
    assert len(results) == thread_count
    # Both workers must converge on the same row id.
    assert len(set(results)) == 1
    # Exactly one DB row landed.
    row = database_connection.execute(
        "SELECT COUNT(*) AS n FROM contact WHERE email = %(email)s",
        {"email": email},
    ).fetchone()
    assert row is not None
    assert row["n"] == 1


# -- Snapshot export / import (§V.121, §B.104) ---------------------------------


_FULL_PROFILE: dict[str, Any] = {
    "summary": "Industrial water treatment chemicals.",
    "products": ["coagulants", "antiscalants"],
    "target_customers": "Municipal water utilities.",
    "timezone": "America/Toronto",
    "sources": ["https://acme.example/about"],
}


def _populate_snapshot_fixture(
    connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Seed a fully-populated company + contact + tags graph for round-trips.

    Carries a company with a profile and two tags, a contact with title +
    email_confidence + one tag, plus a disabled, unassigned vocabulary tag so
    the round-trip exercises vocabulary survival (§V.121).
    """
    company = make_test_company(connection, name="Acme Water", domain="acme.com")
    update_company(connection, company.id, profile=_FULL_PROFILE)
    contact = create_contact(
        connection,
        email="lead@acme.com",
        first_name="Lee",
        last_name="Diaz",
        company_id=company.id,
        title="Plant Manager",
        email_confidence=92,
    )
    assert contact is not None
    make_test_tag_assignment(connection, company_id=company.id, name="customer")
    make_test_tag_assignment(connection, company_id=company.id, name="lead")
    make_test_tag_assignment(connection, contact_id=contact.id, name="decision-maker")
    # A disabled, unassigned vocabulary tag must survive its own `tags` section.
    make_test_tag(connection, name="retired")
    disable_tag(connection, "retired", "deprecated category")


def test_export_snapshot_bundle_shape(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.121: the bundle carries natural keys, embedded tags, no source id."""
    _populate_snapshot_fixture(database_connection)
    bundle = export_snapshot(database_connection)

    assert bundle["schema_version"] == 1
    assert "exported_at" in bundle

    # Vocabulary section carries every tag, including the disabled/unassigned one.
    tag_names = {t["name"] for t in bundle["tags"]}
    assert {"customer", "lead", "decision-maker", "retired"} <= tag_names
    retired = next(t for t in bundle["tags"] if t["name"] == "retired")
    assert retired["disabled_reason"] == "deprecated category"

    assert len(bundle["companies"]) == 1
    company_entry = bundle["companies"][0]
    assert company_entry["domain"] == "acme.com"
    assert company_entry["profile"]["summary"] == _FULL_PROFILE["summary"]
    assert set(company_entry["tags"]) == {"customer", "lead"}
    assert company_entry["aliases"] == []
    # No source-DB UUID is forwarded (§B.104).
    assert "id" not in company_entry
    assert "company_id" not in company_entry

    assert len(bundle["contacts"]) == 1
    contact_entry = bundle["contacts"][0]
    assert contact_entry["email"] == "lead@acme.com"
    assert contact_entry["title"] == "Plant Manager"
    assert contact_entry["email_confidence"] == 92
    assert contact_entry["company_domain"] == "acme.com"
    assert contact_entry["tags"] == ["decision-maker"]
    assert "id" not in contact_entry
    assert "company_id" not in contact_entry


def test_export_import_round_trip_field_identical(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.121 success criterion: export -> fresh import is field-identical."""
    _populate_snapshot_fixture(database_connection)
    bundle = export_snapshot(database_connection)

    # Fresh DB: wipe the company + contact + tag graph, then restore.
    database_connection.execute(
        "TRUNCATE TABLE note, tag_assignment, tag, activity, contact, company CASCADE"
    )
    database_connection.commit()

    result = import_snapshot(database_connection, bundle)
    assert result["errors"] == [], result["errors"]
    assert result["companies"] == 1
    assert result["contacts"] == 1
    assert result["tags"] >= 4

    # Restored company carries its profile (§B.104 fix) and tags.
    company = get_company_by_domain(database_connection, "acme.com")
    assert company is not None
    assert company.profile is not None
    assert company.profile["summary"] == _FULL_PROFILE["summary"]
    company_tags = {
        t.name for t in list_tags(database_connection, company_id=company.id)
    }
    assert company_tags == {"customer", "lead"}

    # Restored contact re-links to the company by domain, carries lead metadata.
    contact = get_contact_by_email(database_connection, "lead@acme.com")
    assert contact is not None
    assert contact.company_id == company.id
    assert contact.title == "Plant Manager"
    assert contact.email_confidence == 92
    contact_tags = {
        t.name for t in list_tags(database_connection, contact_id=contact.id)
    }
    assert contact_tags == {"decision-maker"}

    # The disabled, unassigned vocabulary tag survived the round-trip.
    vocabulary = {
        t.name: t.disabled_reason
        for t in list_tags(database_connection, include_disabled=True)
    }
    assert vocabulary["retired"] == "deprecated category"

    # A second export is byte-identical modulo the timestamp.
    again = export_snapshot(database_connection)
    assert again["companies"] == bundle["companies"]
    assert again["contacts"] == bundle["contacts"]
    assert again["tags"] == bundle["tags"]


def test_import_snapshot_disabled_rows_restore_reason(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.121: a disabled company / contact restores its disabled_reason."""
    company = make_test_company(database_connection, name="Gone Inc", domain="gone.com")
    contact = create_contact(
        database_connection, email="x@gone.com", company_id=company.id
    )
    assert contact is not None
    disable_company(database_connection, company.id, "out of business")
    disable_contact(database_connection, contact.id, "bounced: hard bounce")
    bundle = export_snapshot(database_connection)

    database_connection.execute(
        "TRUNCATE TABLE note, tag_assignment, tag, activity, contact, company CASCADE"
    )
    database_connection.commit()
    import_snapshot(database_connection, bundle)

    restored_company = get_company_by_domain(database_connection, "gone.com")
    assert restored_company is not None
    assert restored_company.disabled_reason == "out of business"
    restored_contact = get_contact_by_email(database_connection, "x@gone.com")
    assert restored_contact is not None
    assert restored_contact.disabled_reason == "bounced: hard bounce"


def test_import_snapshot_unresolvable_fk_is_per_row_error(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.121/§B.104: an FK-unresolvable contact yields a per-row error; the
    batch completes and every resolvable row persists."""
    bundle: dict[str, Any] = {
        "schema_version": 1,
        "tags": [],
        "companies": [
            {
                "name": "Acme",
                "domain": "acme.com",
                "profile": None,
                "disabled_reason": None,
                "tags": [],
            }
        ],
        "contacts": [
            {
                "email": "orphan@nowhere.com",
                "first_name": None,
                "last_name": None,
                "title": None,
                "email_confidence": None,
                "disabled_reason": None,
                "company_domain": "missing.com",
                "tags": [],
            },
            {
                "email": "good@acme.com",
                "first_name": None,
                "last_name": None,
                "title": None,
                "email_confidence": None,
                "disabled_reason": None,
                "company_domain": "acme.com",
                "tags": [],
            },
        ],
    }
    result = import_snapshot(database_connection, bundle)

    # The orphan records an error; the resolvable contact still lands.
    assert result["companies"] == 1
    assert result["contacts"] == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["error"] == "foreign_key_violation"
    assert result["errors"][0]["key"] == "orphan@nowhere.com"
    assert get_contact_by_email(database_connection, "good@acme.com") is not None
    assert get_contact_by_email(database_connection, "orphan@nowhere.com") is None


# -- Meeting -------------------------------------------------------------------


def test_create_and_get_meeting(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.125: create a meeting row and round-trip it by id."""
    start = datetime(2026, 7, 1, 15, 0, tzinfo=UTC)
    end = datetime(2026, 7, 1, 15, 30, tzinfo=UTC)
    meeting = create_meeting(
        database_connection,
        google_event_id="evt-1",
        meet_url="https://meet.google.com/abc-defg-hij",
        summary="Intro call",
        scheduled_at=start,
        ends_at=end,
    )
    assert meeting is not None
    assert meeting.google_event_id == "evt-1"
    assert meeting.summary == "Intro call"
    assert meeting.status == "scheduled"
    assert meeting.scheduled_at == start
    assert meeting.ends_at == end

    fetched = get_meeting(database_connection, meeting.id)
    assert fetched is not None
    assert fetched.id == meeting.id
    assert fetched.meet_url == "https://meet.google.com/abc-defg-hij"


def test_get_meeting_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    assert get_meeting(database_connection, "nonexistent") is None


def test_create_meeting_conflict_returns_none(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.125/§V.90: a second create on the same google_event_id returns None."""
    first = create_meeting(database_connection, google_event_id="evt-dup")
    assert first is not None
    second = create_meeting(database_connection, google_event_id="evt-dup")
    assert second is None


def test_upsert_meeting_idempotent_on_google_event_id(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.125/§V.90: re-polling the same event updates in place, no dup row."""
    first = upsert_meeting(
        database_connection,
        google_event_id="evt-2",
        summary="Original title",
        status="scheduled",
    )
    second = upsert_meeting(
        database_connection,
        google_event_id="evt-2",
        summary="Renamed title",
        status="completed",
    )
    # Same row (same id), fields updated.
    assert second.id == first.id
    assert second.summary == "Renamed title"
    assert second.status == "completed"

    # Exactly one row carries the event id.
    rows = database_connection.execute(
        "SELECT COUNT(*) AS n FROM meeting WHERE google_event_id = 'evt-2'"
    ).fetchone()
    assert rows is not None
    assert rows["n"] == 1

    by_event = get_meeting_by_google_event_id(database_connection, "evt-2")
    assert by_event is not None
    assert by_event.id == first.id


def test_list_meetings_orders_and_filters(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.125: list_meetings orders newest scheduled first; status + contact scope."""
    early = create_meeting(
        database_connection,
        google_event_id="evt-early",
        scheduled_at=datetime(2026, 7, 1, tzinfo=UTC),
        status="scheduled",
    )
    late = create_meeting(
        database_connection,
        google_event_id="evt-late",
        scheduled_at=datetime(2026, 8, 1, tzinfo=UTC),
        status="completed",
    )
    assert early is not None
    assert late is not None

    all_meetings = list_meetings(database_connection)
    assert [m.id for m in all_meetings] == [late.id, early.id]

    completed = list_meetings(database_connection, status="completed")
    assert [m.id for m in completed] == [late.id]

    contact = make_test_contact(database_connection)
    link_meeting_attendee(database_connection, late.id, contact.id)
    scoped = list_meetings(database_connection, contact_id=contact.id)
    assert [m.id for m in scoped] == [late.id]


def test_link_meeting_attendee_pair_unique(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.125: a meeting links its attendees; a repeat pair link returns None."""
    meeting = create_meeting(database_connection, google_event_id="evt-3")
    assert meeting is not None
    contact = make_test_contact(database_connection)

    link = link_meeting_attendee(database_connection, meeting.id, contact.id)
    assert link is not None
    assert link.meeting_id == meeting.id
    assert link.contact_id == contact.id

    # Re-linking the same pair is idempotent (no duplicate row).
    again = link_meeting_attendee(database_connection, meeting.id, contact.id)
    assert again is None

    rows = database_connection.execute(
        "SELECT COUNT(*) AS n FROM meeting_attendee WHERE meeting_id = %s",
        (meeting.id,),
    ).fetchone()
    assert rows is not None
    assert rows["n"] == 1


def test_link_meeting_attendee_multi_attendee(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.125: one meeting links more than one attendee contact."""
    meeting = create_meeting(database_connection, google_event_id="evt-4")
    assert meeting is not None
    contact_a = make_test_contact(database_connection, email="a@acme.com")
    contact_b = make_test_contact(database_connection, email="b@acme.com")

    assert link_meeting_attendee(database_connection, meeting.id, contact_a.id)
    assert link_meeting_attendee(database_connection, meeting.id, contact_b.id)

    rows = database_connection.execute(
        "SELECT COUNT(*) AS n FROM meeting_attendee WHERE meeting_id = %s",
        (meeting.id,),
    ).fetchone()
    assert rows is not None
    assert rows["n"] == 2


def test_link_meeting_attendee_unknown_refs_raise(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.94-style guard: linking an unknown meeting or contact raises."""
    contact = make_test_contact(database_connection)
    with pytest.raises(ValueError, match="meeting not found"):
        link_meeting_attendee(database_connection, "no-such-meeting", contact.id)

    meeting = create_meeting(database_connection, google_event_id="evt-5")
    assert meeting is not None
    with pytest.raises(ValueError, match="contact not found"):
        link_meeting_attendee(database_connection, meeting.id, "no-such-contact")


def test_update_meeting_summary_and_status(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.125: update_meeting edits summary + status (record-keeping only)."""
    meeting = create_meeting(
        database_connection, google_event_id="evt-upd", summary="Original"
    )
    assert meeting is not None
    assert meeting.status == "scheduled"

    updated = update_meeting(
        database_connection, meeting.id, summary="Renamed", status="cancelled"
    )
    assert updated is not None
    assert updated.id == meeting.id
    assert updated.summary == "Renamed"
    assert updated.status == "cancelled"


def test_update_meeting_not_found_returns_none(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    assert (
        update_meeting(database_connection, "no-such-meeting", status="completed")
        is None
    )


def test_update_meeting_ignores_non_allowed_fields(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.126: ingest-owned columns are not operator-editable via update_meeting."""
    meeting = create_meeting(
        database_connection, google_event_id="evt-guard", meet_url="https://m/orig"
    )
    assert meeting is not None

    # meet_url is ingest-owned; the kwarg is dropped, leaving the row unchanged.
    unchanged = update_meeting(
        database_connection, meeting.id, meet_url="https://m/new"
    )
    assert unchanged is not None
    assert unchanged.meet_url == "https://m/orig"


def test_list_meeting_attendees_returns_attendee_contacts(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.8/§B.112: list_meeting_attendees reads the meeting's attendee contacts."""
    meeting = create_meeting(database_connection, google_event_id="evt-att")
    assert meeting is not None
    alice = make_test_contact(database_connection, email="alice@acme.com")
    bob = make_test_contact(database_connection, email="bob@acme.com")
    link_meeting_attendee(database_connection, meeting.id, alice.id)
    link_meeting_attendee(database_connection, meeting.id, bob.id)

    attendees = list_meeting_attendees(database_connection, meeting.id)

    # Ordered by email; carries email + name (the reader for the write+filter pair).
    assert [c.email for c in attendees] == ["alice@acme.com", "bob@acme.com"]
    assert all(isinstance(c.email, str) for c in attendees)


def test_list_meeting_attendees_empty_when_no_links(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.8: a meeting with no linked attendees reads an empty list, not None."""
    meeting = create_meeting(database_connection, google_event_id="evt-noatt")
    assert meeting is not None
    assert list_meeting_attendees(database_connection, meeting.id) == []


def test_load_meeting_view_inlines_attendees_and_summary(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.8/§B.112: load_meeting_view inlines attendee contacts + summary fields."""
    from mailpilot.database import load_meeting_view

    meeting = create_meeting(
        database_connection, google_event_id="evt-view", summary="Intro"
    )
    assert meeting is not None
    alice = make_test_contact(database_connection, email="alice@acme.com")
    bob = make_test_contact(database_connection, email="bob@acme.com")
    link_meeting_attendee(database_connection, meeting.id, alice.id)
    link_meeting_attendee(database_connection, meeting.id, bob.id)

    view = load_meeting_view(database_connection, meeting.id)

    assert view is not None
    assert view.id == meeting.id
    assert view.summary == "Intro"
    assert [c.email for c in view.attendees] == ["alice@acme.com", "bob@acme.com"]
    assert view.attendee_emails == ["alice@acme.com", "bob@acme.com"]
    assert view.attendee_count == 2


def test_load_meeting_view_not_found_returns_none(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import load_meeting_view

    assert load_meeting_view(database_connection, "no-such-meeting") is None


def test_meeting_view_field_set_superset_of_base_and_summary() -> None:
    """§V.8/§B.94: MeetingView field set ⊇ Meeting columns + MeetingSummary denorm.

    Recurrence guard: a view model omitting a base column is silently stripped
    from ``**meeting.model_dump()`` (Pydantic ``extra=ignore``), so the field
    set is tracked against both the base entity and the list summary.
    """
    from mailpilot.models import Meeting, MeetingSummary, MeetingView

    base = set(Meeting.model_fields)
    summary = set(MeetingSummary.model_fields)
    view = set(MeetingView.model_fields)

    assert base <= view, f"MeetingView missing base columns: {base - view}"
    assert summary <= view, f"MeetingView missing summary denorm: {summary - view}"
    assert {"attendees", "attendee_emails", "attendee_count"} <= view


def test_list_meetings_projects_attendee_summary(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """§V.8/§V.96: list_meetings rows carry attendee emails + count (no N+1)."""
    meeting = create_meeting(database_connection, google_event_id="evt-summary")
    assert meeting is not None
    alice = make_test_contact(database_connection, email="alice@acme.com")
    bob = make_test_contact(database_connection, email="bob@acme.com")
    link_meeting_attendee(database_connection, meeting.id, alice.id)
    link_meeting_attendee(database_connection, meeting.id, bob.id)
    bare = create_meeting(database_connection, google_event_id="evt-bare")
    assert bare is not None

    rows = {m.id: m for m in list_meetings(database_connection)}

    assert rows[meeting.id].attendee_emails == ["alice@acme.com", "bob@acme.com"]
    assert rows[meeting.id].attendee_count == 2
    # A meeting with no attendees carries an empty summary, not NULL.
    assert rows[bare.id].attendee_emails == []
    assert rows[bare.id].attendee_count == 0


# -- check_workflow_wording (§V.134) -------------------------------------------


def _catalog_entry(
    name: str,
    template: str = "outbound-general",
    theme: str = "blue",
    goal: str = "",
    instructions: str = "",
) -> dict[str, Any]:
    """Build a parsed-TOML catalog entry mirroring a workflow def (§V.103)."""
    return {
        "name": name,
        "template": template,
        "theme": theme,
        "goal": goal,
        "instructions": instructions,
    }


def test_check_workflow_wording_classifies_four_states(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.134: in_sync / out_of_sync / not_imported / orphaned by name + hash."""
    account = make_test_account(database_connection)

    synced = create_workflow(
        database_connection,
        name="synced-flow",
        template="outbound-general",
        account_id=account.id,
        theme="green",
    )
    assert synced is not None
    update_workflow(
        database_connection,
        synced.id,
        goal="Book demos.",
        instructions="Be concise.\n",
    )

    drifted = create_workflow(
        database_connection,
        name="drifted-flow",
        template="outbound-general",
        account_id=account.id,
        theme="blue",
    )
    assert drifted is not None
    update_workflow(
        database_connection,
        drifted.id,
        goal="Old goal in the database.",
    )

    create_workflow(
        database_connection,
        name="orphaned-flow",
        template="inbound-general",
        account_id=account.id,
    )

    catalog = {
        "synced-flow": _catalog_entry(
            "synced-flow",
            template="outbound-general",
            theme="green",
            goal="Book demos.",
            instructions="Be concise.\n",
        ),
        "drifted-flow": _catalog_entry(
            "drifted-flow",
            template="outbound-general",
            goal="New goal in the catalog file.",
        ),
        "new-flow": _catalog_entry("new-flow", template="inbound-general"),
    }

    report = check_workflow_wording(database_connection, catalog)
    by_name = {entry.name: entry for entry in report.workflows}

    assert by_name["synced-flow"].state == "in_sync"
    assert by_name["drifted-flow"].state == "out_of_sync"
    assert by_name["new-flow"].state == "not_imported"
    assert by_name["orphaned-flow"].state == "orphaned"

    # not_imported carries no row hash; orphaned carries no catalog hash.
    assert by_name["new-flow"].row_hash is None
    assert by_name["new-flow"].catalog_hash is not None
    assert by_name["orphaned-flow"].catalog_hash is None
    assert by_name["orphaned-flow"].row_hash is not None

    assert report.in_sync == 1
    assert report.out_of_sync == 1
    assert report.not_imported == 1
    assert report.orphaned == 1


def test_check_workflow_wording_covers_cadence_pair(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.134/§V.136: touches + touch_interval_days join the wording hash, so a
    cadence change (or a def that drops cadence) flips in_sync -> out_of_sync."""
    account = make_test_account(database_connection)
    flow = create_workflow(
        database_connection,
        name="cadence-hash",
        template="outbound-general",
        account_id=account.id,
        theme="blue",
    )
    assert flow is not None
    update_workflow(
        database_connection,
        flow.id,
        goal="g",
        instructions="i",
        touches=3,
        touch_interval_days=7,
    )

    matching = {
        "cadence-hash": {
            **_catalog_entry("cadence-hash", goal="g", instructions="i"),
            "touches": 3,
            "touch_interval_days": 7,
        }
    }
    matched = check_workflow_wording(database_connection, matching)
    assert matched.workflows[0].state == "in_sync"

    # Flip the touch count only -> out_of_sync.
    changed = {
        "cadence-hash": {
            **_catalog_entry("cadence-hash", goal="g", instructions="i"),
            "touches": 5,
            "touch_interval_days": 7,
        }
    }
    assert (
        check_workflow_wording(database_connection, changed).workflows[0].state
        == "out_of_sync"
    )

    # A def that drops cadence (single-touch) drifts from a cadenced row.
    dropped = {
        "cadence-hash": _catalog_entry("cadence-hash", goal="g", instructions="i")
    }
    assert (
        check_workflow_wording(database_connection, dropped).workflows[0].state
        == "out_of_sync"
    )


def test_check_workflow_wording_keyed_by_name_not_hashed(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.134: name is the join key, never hashed; a single wording flip moves state."""
    account = make_test_account(database_connection)
    flow = create_workflow(
        database_connection,
        name="hashed-flow",
        template="outbound-general",
        account_id=account.id,
        theme="blue",
    )
    assert flow is not None
    update_workflow(
        database_connection,
        flow.id,
        goal="Same goal.",
        instructions="Same body.",
    )

    same = {
        "hashed-flow": _catalog_entry(
            "hashed-flow",
            template="outbound-general",
            theme="blue",
            goal="Same goal.",
            instructions="Same body.",
        )
    }
    same_report = check_workflow_wording(database_connection, same)
    assert same_report.workflows[0].state == "in_sync"
    assert same_report.workflows[0].catalog_hash == same_report.workflows[0].row_hash

    # Flip exactly one wording field (theme) -> out_of_sync.
    themed = {
        "hashed-flow": _catalog_entry(
            "hashed-flow",
            template="outbound-general",
            theme="red",
            goal="Same goal.",
            instructions="Same body.",
        )
    }
    themed_report = check_workflow_wording(database_connection, themed)
    assert themed_report.workflows[0].state == "out_of_sync"
    assert (
        themed_report.workflows[0].catalog_hash != themed_report.workflows[0].row_hash
    )


def test_check_workflow_wording_joins_globally_across_accounts(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.107: name is globally unique, so the check joins rows from every account."""
    account_a = make_test_account(database_connection, email="a@example.com")
    account_b = make_test_account(database_connection, email="b@example.com")
    create_workflow(
        database_connection,
        name="account-a-flow",
        template="outbound-general",
        account_id=account_a.id,
    )
    create_workflow(
        database_connection,
        name="account-b-flow",
        template="outbound-general",
        account_id=account_b.id,
    )

    report = check_workflow_wording(database_connection, {})
    names = {entry.name for entry in report.workflows}
    assert names == {"account-a-flow", "account-b-flow"}
    assert all(entry.state == "orphaned" for entry in report.workflows)
    assert report.orphaned == 2


def test_check_workflow_wording_scope_to_catalog_suppresses_orphaned(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.134: scoping to passed files reports only catalog names, no orphaned rows."""
    account = make_test_account(database_connection)
    create_workflow(
        database_connection,
        name="in-catalog-flow",
        template="outbound-general",
        account_id=account.id,
    )
    create_workflow(
        database_connection,
        name="other-db-flow",
        template="outbound-general",
        account_id=account.id,
    )

    catalog = {
        "in-catalog-flow": _catalog_entry(
            "in-catalog-flow", template="outbound-general"
        ),
        "new-flow": _catalog_entry("new-flow", template="inbound-general"),
    }

    scoped = check_workflow_wording(database_connection, catalog, scope_to_catalog=True)
    names = {entry.name for entry in scoped.workflows}
    # The unpassed DB row (other-db-flow) is absent; scoping shows only the
    # inquired names. not_imported (catalog name, no row) still surfaces.
    assert names == {"in-catalog-flow", "new-flow"}
    assert scoped.orphaned == 0
    by_name = {entry.name: entry for entry in scoped.workflows}
    assert by_name["in-catalog-flow"].state == "in_sync"
    assert by_name["new-flow"].state == "not_imported"

    # Directory mode (the default) still surfaces the unpassed row as orphaned.
    unscoped = check_workflow_wording(database_connection, catalog)
    unscoped_by_name = {entry.name: entry for entry in unscoped.workflows}
    assert unscoped_by_name["other-db-flow"].state == "orphaned"
    assert unscoped.orphaned == 1
