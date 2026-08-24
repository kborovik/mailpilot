"""OOO pause + resume harness (§V.169 / §B.136)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import psycopg

from conftest import (
    make_test_account,
    make_test_contact,
    make_test_enrollment,
    make_test_settings,
    make_test_workflow,
)
from mailpilot.database import (
    activate_workflow,
    create_email,
    create_task,
    create_tasks_for_routed_emails,
    get_latest_enrollment_outcome,
    get_task,
    get_workflow,
    list_enrollments_detailed,
    retry_tasks_matching,
    update_workflow,
)
from mailpilot.models import Email
from mailpilot.ooo import (
    AUTO_SUBMITTED_LABEL,
    auto_submitted_label,
    is_mechanical_ooo,
    is_ooo_auto_reply,
    parse_ooo_return_at,
    resolve_ooo_resume_at,
    schedule_ooo_resume,
)
from mailpilot.routing import route_email
from mailpilot.run import execute_task

_NOW = datetime(2026, 8, 13, 16, 0, tzinfo=UTC)
# Issue 236: today sits inside the week-range (August 17-22).
_INSIDE_WEEK = datetime(2026, 8, 17, 11, 13, tzinfo=UTC)
_FAR_FUTURE = "2099-12-31T00:00:00Z"

# §V.169 / §B.140 / §B.142 fixtures — year-less week-range, leave-start,
# explicit year, and same-day `on <Month> <D>`.
_FIXTURE_WEEK_RANGE = (
    "Automatic reply: The team is out of the office this week "
    "(August 17-22) for Ascent."
)
_FIXTURE_LEAVE_START = (
    "Automatic reply: Effective Monday, February 16th he begins a "
    "transition away from CLA."
)
_FIXTURE_EXPLICIT_YEAR = (
    "Automatic reply: I am out of the office until Tuesday August 18, 2026."
)
_FIXTURE_SAME_DAY_ON = (
    "Automatic reply: I'll be out of the office on August 17 "
    "with very limited access to email."
)
# §V.169 / §B.145 — Bonnie: event-week past + return Monday still-ahead.
_BONNIE_NOW = datetime(2026, 8, 19, 13, 1, tzinfo=UTC)
_FIXTURE_BONNIE = (
    "Automatic reply: I will the Acumatica Ascent event the week of "
    "August 17th. I will be fully back online on Monday, August 24th."
)
_FIXTURE_WEEK_OF_ONLY = (
    "Automatic reply: I am at the Acumatica Ascent event the week of August 17th."
)
# §V.179 / §B.146 — Gina: past-tense last-day + named successors, no emails.
_FIXTURE_GINA = (
    "Automatic reply: My last day with CLA was Wednesday, July 22. "
    "For BD Advisory please contact Bibber Smith-McCurdy, Emily Gorrell, "
    "or Leann Johnson."
)
_FIXTURE_NO_LONGER_WITH = (
    "Automatic reply: Terri is no longer with Aktion. Please update your records."
)


def _email(**overrides: Any) -> Email:
    defaults: dict[str, Any] = {
        "id": "01234567-0000-7000-0000-000000000011",
        "account_id": "01234567-0000-7000-0000-000000000001",
        "direction": "inbound",
        "subject": "",
        "body_text": "",
        "labels": [],
        "created_at": _NOW,
    }
    return Email(**{**defaults, **overrides})


def _activate_outbound(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    *,
    touches: int | None = 3,
    touch_interval_days: int | None = 4,
) -> None:
    fields: dict[str, object] = {
        "goal": "Book a meeting",
        "instructions": "Send the sequence",
    }
    if touches is not None:
        fields["touches"] = touches
        fields["touch_interval_days"] = touch_interval_days
    update_workflow(connection, workflow_id, **fields)
    activate_workflow(connection, workflow_id)


def _seed_t1(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    suffix: str,
    touches: int | None = 3,
    touch_interval_days: int | None = 4,
) -> tuple[Any, Any, Any, Any, Any]:
    account = make_test_account(connection, email=f"ooo-{suffix}@lab5.example")
    contact = make_test_contact(connection, email=f"prospect-{suffix}@example.com")
    workflow = make_test_workflow(
        connection,
        account_id=account.id,
        name=f"ooo-{suffix}",
        workflow_type="outbound",
    )
    _activate_outbound(
        connection,
        workflow.id,
        touches=touches,
        touch_interval_days=touch_interval_days,
    )
    enrollment = make_test_enrollment(connection, workflow.id, contact.id)
    outbound = create_email(
        connection,
        account_id=account.id,
        direction="outbound",
        subject="Touch 1",
        body_text="Intro",
        gmail_thread_id=f"t-ooo-{suffix}",
        contact_id=contact.id,
        workflow_id=workflow.id,
        status="sent",
        is_routed=True,
        sent_at=_NOW,
    )
    assert outbound is not None
    t2 = create_task(
        connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="Touch 2 of 3",
        scheduled_at=_FAR_FUTURE,
        context={"touch": 2, "prior_email_id": outbound.id},
    )
    return account, contact, workflow, enrollment, t2


# -- detection ----------------------------------------------------------------


def test_mechanical_ooo_from_automatic_reply_subject() -> None:
    """§V.169: subject Automatic reply is the mechanical OOO signal."""
    email = _email(
        subject="Automatic reply: out of the office returning Thursday",
        body_text="I am out of the office returning Thursday.",
    )
    assert is_mechanical_ooo(email)
    assert is_ooo_auto_reply(email)


def test_mechanical_ooo_from_auto_submitted_label() -> None:
    """§V.169: Auto-Submitted stamps AUTO_SUBMITTED so detect needs no column."""
    email = _email(
        subject="Re: Touch 1",
        body_text="I will be away until Monday.",
        labels=["INBOX", AUTO_SUBMITTED_LABEL],
    )
    assert is_mechanical_ooo(email)
    assert is_ooo_auto_reply(email)


def test_absence_language_without_header_is_ooo_class_not_mechanical() -> None:
    """§V.169: agent OOO class (absence language) skips ACK, not the agent."""
    email = _email(
        subject="Re: Touch 1",
        body_text="I am out of the office returning 2026-08-17.",
    )
    assert not is_mechanical_ooo(email)
    assert is_ooo_auto_reply(email)


def test_retired_automatic_reply_is_not_ooo() -> None:
    """§V.161 / §V.164: retired / left-company auto-reply is not OOO."""
    email = _email(
        subject="Automatic reply: retired",
        body_text=(
            "Automatic reply: I have retired from the company. "
            "Please update your records and contact Janice."
        ),
    )
    assert not is_mechanical_ooo(email)
    assert not is_ooo_auto_reply(email)


def test_no_longer_with_automatic_reply_is_not_ooo() -> None:
    """§V.179 / §V.161: no-longer-with auto-reply stays DNC, not pause."""
    email = _email(
        subject="Automatic reply: no longer with the company",
        body_text=_FIXTURE_NO_LONGER_WITH,
        labels=["INBOX", AUTO_SUBMITTED_LABEL],
    )
    assert not is_mechanical_ooo(email)
    assert not is_ooo_auto_reply(email)


def test_gina_last_day_was_is_not_ooo() -> None:
    """§V.179 / §B.146: past-tense last-day auto-reply is not OOO."""
    email = _email(
        subject="Automatic reply: My last day with CLA was Wednesday, July 22",
        body_text=_FIXTURE_GINA,
        labels=["INBOX", AUTO_SUBMITTED_LABEL],
    )
    assert not is_mechanical_ooo(email)
    assert not is_ooo_auto_reply(email)


def test_address_change_is_not_ooo() -> None:
    """§V.161: address-change auto-reply stays do_not_contact, not pause."""
    email = _email(
        subject="Automatic reply: email address has changed",
        body_text=(
            "Please note my email address has changed to new@example.com. "
            "Please update your records."
        ),
    )
    assert not is_mechanical_ooo(email)
    assert not is_ooo_auto_reply(email)


def test_auto_submitted_label_helper() -> None:
    assert auto_submitted_label("auto-replied") == AUTO_SUBMITTED_LABEL
    assert auto_submitted_label("auto-generated") == AUTO_SUBMITTED_LABEL
    assert auto_submitted_label("auto-replied; owner=foo") == AUTO_SUBMITTED_LABEL
    assert auto_submitted_label("no") is None
    assert auto_submitted_label(None) is None


# -- return-date parse --------------------------------------------------------


def test_parse_iso_return_date() -> None:
    parsed = parse_ooo_return_at(
        "Automatic reply: out of office returning 2026-08-17.",
        now=_NOW,
    )
    assert parsed is not None
    assert parsed.date().isoformat() == "2026-08-17"


def test_parse_weekday_returning_thursday() -> None:
    """2026-08-13 is Thursday; 'returning Thursday' is next Thursday."""
    parsed = parse_ooo_return_at(
        "out of the office returning Thursday.",
        now=_NOW,
    )
    assert parsed is not None
    assert parsed.date().isoformat() == "2026-08-20"


def test_parse_until_monday() -> None:
    parsed = parse_ooo_return_at(
        "I am out of the office until Monday with no access to email.",
        now=_NOW,
    )
    assert parsed is not None
    assert parsed.date().isoformat() == "2026-08-17"


def test_parse_unparseable_returns_none() -> None:
    assert parse_ooo_return_at("I am away for a bit.", now=_NOW) is None


def test_parse_week_range_containing_now_same_year() -> None:
    """§V.169 / §B.140: year-less week-range containing now → day after end."""
    parsed = parse_ooo_return_at(_FIXTURE_WEEK_RANGE, now=_INSIDE_WEEK)
    assert parsed is not None
    assert parsed.date().isoformat() == "2026-08-23"
    assert parsed.year == 2026


def test_parse_leave_start_weekday_month_day_unparseable() -> None:
    """§V.169 / §B.140: leave-start months past is not a next-year return."""
    assert parse_ooo_return_at(_FIXTURE_LEAVE_START, now=_INSIDE_WEEK) is None


def test_parse_explicit_year_wins() -> None:
    """§V.169: explicit year in the body is the return date (Lynn 2026)."""
    parsed = parse_ooo_return_at(_FIXTURE_EXPLICIT_YEAR, now=_INSIDE_WEEK)
    assert parsed is not None
    assert parsed.date().isoformat() == "2026-08-18"
    assert parsed.year == 2026


def test_parse_same_day_on_month_day_same_year() -> None:
    """§V.169 / §B.142: year-less same-day `on <Month> <D>` stays this year."""
    parsed = parse_ooo_return_at(_FIXTURE_SAME_DAY_ON, now=_INSIDE_WEEK)
    assert parsed is not None
    assert parsed.date().isoformat() == "2026-08-18"
    assert parsed.year == 2026


def test_parse_bonnie_event_week_plus_return_monday() -> None:
    """§V.169 / §B.145: return Monday wins over past event-week month-day."""
    parsed = parse_ooo_return_at(_FIXTURE_BONNIE, now=_BONNIE_NOW)
    assert parsed is not None
    assert parsed.date().isoformat() == "2026-08-24"
    assert parsed.year == 2026
    assert parsed.date().isoformat() != "2027-08-17"


def test_parse_past_week_of_not_year_plus_one() -> None:
    """§V.169 / §B.145: past 'week of' is not a return date and not year+1."""
    assert parse_ooo_return_at(_FIXTURE_WEEK_OF_ONLY, now=_BONNIE_NOW) is None


def test_resolve_unparseable_uses_interval(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.169: unparseable return -> +touch_interval_days (weekend-rolled)."""
    account = make_test_account(database_connection, email="ooo-interval@lab5.example")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, name="ooo-interval"
    )
    _activate_outbound(
        database_connection, workflow.id, touches=3, touch_interval_days=4
    )
    email = _email(subject="Automatic reply: away", body_text="I am away.")
    resolved = resolve_ooo_resume_at(email, workflow, now=_NOW)
    # Thursday + 4 days = Monday 2026-08-17 (no weekend roll needed).
    assert resolved.date().isoformat() == "2026-08-17"


def test_resolve_unparseable_null_cadence_plus_three_days(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.169: NULL cadence unparseable -> +3 days."""
    account = make_test_account(database_connection, email="ooo-nullcad@lab5.example")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, name="ooo-nullcad"
    )
    _activate_outbound(database_connection, workflow.id, touches=None)
    email = _email(subject="Automatic reply: away", body_text="I am away.")
    resolved = resolve_ooo_resume_at(email, workflow, now=_NOW)
    # Thursday + 3 = Sunday -> Monday 2026-08-17.
    assert resolved.date().isoformat() == "2026-08-17"


def test_resolve_week_range_resume_same_year(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.169 / §B.140: week-range resume stays 2026 (Sunday rolls to Monday)."""
    account = make_test_account(database_connection, email="ooo-weekrng@lab5.example")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, name="ooo-weekrng"
    )
    _activate_outbound(
        database_connection, workflow.id, touches=3, touch_interval_days=4
    )
    email = _email(subject=_FIXTURE_WEEK_RANGE, body_text=_FIXTURE_WEEK_RANGE)
    resolved = resolve_ooo_resume_at(email, workflow, now=_INSIDE_WEEK)
    assert resolved.year == 2026
    # Day-after range-end is Sunday 2026-08-23 → Monday 2026-08-24.
    assert resolved.date().isoformat() == "2026-08-24"


def test_resolve_leave_start_uses_interval_not_next_year(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.169 / §B.140: leave-start months past falls back to cadence, not 2027."""
    account = make_test_account(database_connection, email="ooo-leavest@lab5.example")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, name="ooo-leavest"
    )
    _activate_outbound(
        database_connection, workflow.id, touches=3, touch_interval_days=4
    )
    email = _email(subject=_FIXTURE_LEAVE_START, body_text=_FIXTURE_LEAVE_START)
    loaded = get_workflow(database_connection, workflow.id)
    assert loaded is not None
    resolved = resolve_ooo_resume_at(email, loaded, now=_INSIDE_WEEK)
    assert resolved.year == 2026
    # Monday 2026-08-17 + 4 days = Friday 2026-08-21.
    assert resolved.date().isoformat() == "2026-08-21"


def test_resolve_same_day_on_month_day_same_year(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.169 / §B.142: same-day `on August 17` resumes 2026-08-18, not 2027."""
    account = make_test_account(database_connection, email="ooo-sameday@lab5.example")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, name="ooo-sameday"
    )
    _activate_outbound(
        database_connection, workflow.id, touches=3, touch_interval_days=4
    )
    email = _email(subject=_FIXTURE_SAME_DAY_ON, body_text=_FIXTURE_SAME_DAY_ON)
    resolved = resolve_ooo_resume_at(email, workflow, now=_INSIDE_WEEK)
    assert resolved.year == 2026
    # Next calendar day is Tuesday 2026-08-18 (no weekend roll).
    assert resolved.date().isoformat() == "2026-08-18"
    assert resolved.date().isoformat() != "2027-08-17"


def test_resolve_bonnie_resume_same_year(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.169 / §B.145: Bonnie resume is 2026-08-24, not 2027-08-17."""
    account = make_test_account(database_connection, email="ooo-bonnie@lab5.example")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, name="ooo-bonnie"
    )
    _activate_outbound(
        database_connection, workflow.id, touches=3, touch_interval_days=4
    )
    email = _email(subject=_FIXTURE_BONNIE, body_text=_FIXTURE_BONNIE)
    resolved = resolve_ooo_resume_at(email, workflow, now=_BONNIE_NOW)
    assert resolved.year == 2026
    # Monday 2026-08-24 — no weekend roll.
    assert resolved.date().isoformat() == "2026-08-24"
    assert resolved.date().isoformat() != "2027-08-17"


def test_route_ooo_cancels_t2_and_schedules_resume(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.169 / §V.123 / §V.162: OOO inbound cancels T2 and schedules resume."""
    account, contact, workflow, enrollment, t2 = _seed_t1(
        database_connection, suffix="route"
    )
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Automatic reply: out of the office until 2026-08-17",
        body_text="I am out of the office until 2026-08-17.",
        gmail_thread_id="t-ooo-route",
        contact_id=contact.id,
        sender=contact.email,
    )
    assert inbound is not None

    route_email(database_connection, inbound, contact.email, make_test_settings())

    cancelled = get_task(database_connection, t2.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"

    rows = list_enrollments_detailed(
        database_connection, workflow_id=workflow.id, full=True
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "active"
    assert row.disposition is None
    assert row.emails_sent == 1
    assert row.last_touch == 1
    assert row.next_touch == 2
    assert row.next_scheduled_at is not None
    assert get_latest_enrollment_outcome(database_connection, enrollment.id) is None

    pending = database_connection.execute(
        "SELECT context, scheduled_at FROM task "
        "WHERE enrollment_id = %s AND status = 'pending'",
        (enrollment.id,),
    ).fetchall()
    assert len(pending) == 1
    assert pending[0]["context"]["touch"] == 2
    assert pending[0]["context"]["reason"] == "ooo_pause"
    assert isinstance(pending[0]["context"]["touch"], int)


def test_route_mechanical_ooo_does_not_enqueue_inbound_agent_task(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.188: Automatic reply after route → no handle-inbound, one resume."""
    account, contact, _workflow, enrollment, t2 = _seed_t1(
        database_connection, suffix="mech-enq"
    )
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Automatic reply: out of the office until 2026-08-17",
        body_text="I am out of the office until 2026-08-17.",
        gmail_thread_id="t-ooo-mech-enq",
        contact_id=contact.id,
        sender=contact.email,
    )
    assert inbound is not None
    route_email(database_connection, inbound, contact.email, make_test_settings())

    created = create_tasks_for_routed_emails(database_connection)
    handle = [task for task in created if task.description == "handle inbound email"]
    assert handle == []
    inbound_agent = database_connection.execute(
        "SELECT id FROM task WHERE email_id = %s AND description = %s",
        (inbound.id, "handle inbound email"),
    ).fetchall()
    assert inbound_agent == []

    pending = database_connection.execute(
        "SELECT context FROM task WHERE enrollment_id = %s AND status = 'pending' "
        "AND context->>'reason' = 'ooo_pause'",
        (enrollment.id,),
    ).fetchall()
    assert len(pending) == 1
    assert pending[0]["context"]["touch"] == 2

    cancelled = get_task(database_connection, t2.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert get_latest_enrollment_outcome(database_connection, enrollment.id) is None

    again = create_tasks_for_routed_emails(database_connection)
    assert [task for task in again if task.description == "handle inbound email"] == []
    still = database_connection.execute(
        "SELECT id FROM task WHERE email_id = %s AND description = %s",
        (inbound.id, "handle inbound email"),
    ).fetchall()
    assert still == []
    resumes = database_connection.execute(
        "SELECT id FROM task WHERE enrollment_id = %s AND status = 'pending' "
        "AND context->>'reason' = 'ooo_pause'",
        (enrollment.id,),
    ).fetchall()
    assert len(resumes) == 1


def test_route_language_only_ooo_still_enqueues_inbound_agent_task(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.188: language-only absence still gets handle inbound email."""
    account, contact, _workflow, enrollment, t2 = _seed_t1(
        database_connection, suffix="lang-enq"
    )
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Re: Touch 1",
        body_text="I am out of the office returning 2026-08-17.",
        gmail_thread_id="t-ooo-lang-enq",
        contact_id=contact.id,
        sender=contact.email,
    )
    assert inbound is not None
    route_email(database_connection, inbound, contact.email, make_test_settings())

    created = create_tasks_for_routed_emails(database_connection)
    handle = [task for task in created if task.description == "handle inbound email"]
    assert len(handle) == 1
    assert handle[0].email_id == inbound.id
    assert handle[0].enrollment_id == enrollment.id

    pending_ooo = database_connection.execute(
        "SELECT id FROM task WHERE enrollment_id = %s AND status = 'pending' "
        "AND context->>'reason' = 'ooo_pause'",
        (enrollment.id,),
    ).fetchall()
    assert pending_ooo == []
    cancelled = get_task(database_connection, t2.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"


def test_route_gina_last_day_does_not_schedule_ooo(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.179 / §V.123 / §B.146: last-day-was cancels T2, no year-pause."""
    account, contact, workflow, enrollment, t2 = _seed_t1(
        database_connection, suffix="gina"
    )
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Automatic reply: My last day with CLA was Wednesday, July 22",
        body_text=_FIXTURE_GINA,
        gmail_thread_id="t-ooo-gina",
        contact_id=contact.id,
        sender=contact.email,
        labels=["INBOX", AUTO_SUBMITTED_LABEL],
    )
    assert inbound is not None
    route_email(database_connection, inbound, contact.email, make_test_settings())

    cancelled = get_task(database_connection, t2.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    pending = database_connection.execute(
        "SELECT id, scheduled_at FROM task WHERE enrollment_id = %s "
        "AND status = 'pending' AND context->>'reason' = 'ooo_pause'",
        (enrollment.id,),
    ).fetchall()
    assert pending == []
    assert get_latest_enrollment_outcome(database_connection, enrollment.id) is None
    still = list_enrollments_detailed(
        database_connection, workflow_id=workflow.id, full=True
    )
    assert still[0].status == "active"
    assert still[0].disposition is None
    assert still[0].next_scheduled_at is None


def test_execute_task_gina_does_not_skip_agent(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.179: last-day-was is not mechanical OOO; agent still runs."""
    account, contact, workflow, enrollment, t2 = _seed_t1(
        database_connection, suffix="gina-exec"
    )
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Automatic reply: My last day with CLA was Wednesday, July 22",
        body_text=_FIXTURE_GINA,
        gmail_thread_id="t-ooo-gina-exec",
        contact_id=contact.id,
        sender=contact.email,
        labels=["INBOX", AUTO_SUBMITTED_LABEL],
    )
    assert inbound is not None
    route_email(database_connection, inbound, contact.email, make_test_settings())
    inbound_task = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="handle inbound email",
        scheduled_at=datetime.now(UTC).isoformat(),
        email_id=inbound.id,
    )

    with patch("mailpilot.run.invoke_workflow_agent") as mock_invoke:
        mock_invoke.return_value = {"disposition": "do_not_contact"}
        execute_task(database_connection, make_test_settings(), inbound_task)

    mock_invoke.assert_called_once()
    done = get_task(database_connection, inbound_task.id)
    assert done is not None
    assert done.status == "completed"
    assert done.result.get("reason") != "ooo_pause"
    pending = database_connection.execute(
        "SELECT id FROM task WHERE enrollment_id = %s AND status = 'pending' "
        "AND context->>'reason' = 'ooo_pause'",
        (enrollment.id,),
    ).fetchall()
    assert pending == []
    cancelled = get_task(database_connection, t2.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"


def test_route_retired_automatic_reply_does_not_schedule_ooo(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.161 / §V.164: retired Automatic reply is not an OOO pause."""
    account, contact, workflow, enrollment, t2 = _seed_t1(
        database_connection, suffix="retired"
    )
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Automatic reply: retired",
        body_text=(
            "Automatic reply: I have retired from the company. "
            "Please update your records."
        ),
        gmail_thread_id="t-ooo-retired",
        contact_id=contact.id,
        sender=contact.email,
    )
    assert inbound is not None
    route_email(database_connection, inbound, contact.email, make_test_settings())

    cancelled = get_task(database_connection, t2.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    pending = database_connection.execute(
        "SELECT id FROM task WHERE enrollment_id = %s AND status = 'pending' "
        "AND context->>'reason' = 'ooo_pause'",
        (enrollment.id,),
    ).fetchall()
    assert pending == []
    assert get_latest_enrollment_outcome(database_connection, enrollment.id) is None
    still = list_enrollments_detailed(
        database_connection, workflow_id=workflow.id, full=True
    )
    assert still[0].status == "active"
    assert still[0].disposition is None


def test_execute_task_ooo_skips_agent_and_does_not_burn_touch(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.169 success-path: mechanical OOO skips the agent; last_touch stays."""
    account, contact, workflow, enrollment, t2 = _seed_t1(
        database_connection, suffix="exec"
    )
    return_on = (datetime.now(UTC) + timedelta(days=5)).date().isoformat()
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject=f"Automatic reply: out of the office until {return_on}",
        body_text=f"I am out of the office until {return_on}.",
        gmail_thread_id="t-ooo-exec",
        contact_id=contact.id,
        sender=contact.email,
    )
    assert inbound is not None
    route_email(database_connection, inbound, contact.email, make_test_settings())
    inbound_task = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="handle inbound email",
        scheduled_at=datetime.now(UTC).isoformat(),
        email_id=inbound.id,
    )

    with patch("mailpilot.run.invoke_workflow_agent") as mock_invoke:
        execute_task(database_connection, make_test_settings(), inbound_task)

    mock_invoke.assert_not_called()
    done = get_task(database_connection, inbound_task.id)
    assert done is not None
    assert done.status == "completed"
    assert done.result.get("reason") == "ooo_pause"

    cancelled = get_task(database_connection, t2.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"

    row = list_enrollments_detailed(
        database_connection, workflow_id=workflow.id, full=True
    )[0]
    assert row.emails_sent == 1
    assert row.last_touch == 1
    assert row.next_touch == 2
    assert row.disposition is None
    assert get_latest_enrollment_outcome(database_connection, enrollment.id) is None
    resumes = database_connection.execute(
        "SELECT id FROM task WHERE enrollment_id = %s AND status = 'pending' "
        "AND context->>'reason' = 'ooo_pause'",
        (enrollment.id,),
    ).fetchall()
    assert len(resumes) == 1
    assert row.next_scheduled_at is not None
    delta_days = (row.next_scheduled_at - datetime.now(UTC)).total_seconds() / 86400
    assert -0.5 < delta_days < 21


def test_execute_task_ooo_fail_path_no_ack_no_burned_touch(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.169 / §V.131 fail-path: failed inbound OOO sends no ACK, no touch burn."""
    account, contact, workflow, enrollment, t2 = _seed_t1(
        database_connection, suffix="fail"
    )
    return_on = (datetime.now(UTC) + timedelta(days=5)).date().isoformat()
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Re: Touch 1",
        body_text=f"I am out of the office returning {return_on}.",
        gmail_thread_id="t-ooo-fail",
        contact_id=contact.id,
        sender=contact.email,
    )
    assert inbound is not None
    route_email(database_connection, inbound, contact.email, make_test_settings())
    inbound_task = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="handle inbound email",
        scheduled_at=datetime.now(UTC).isoformat(),
        email_id=inbound.id,
    )

    with (
        patch(
            "mailpilot.run.invoke_workflow_agent",
            side_effect=RuntimeError("LLM error"),
        ),
        patch("mailpilot.run.email_ops") as mock_email_ops,
    ):
        execute_task(database_connection, make_test_settings(), inbound_task)

    mock_email_ops.reply_email.assert_not_called()
    failed = get_task(database_connection, inbound_task.id)
    assert failed is not None
    assert failed.status == "failed"

    cancelled = get_task(database_connection, t2.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"

    row = list_enrollments_detailed(
        database_connection, workflow_id=workflow.id, full=True
    )[0]
    assert row.emails_sent == 1
    assert row.last_touch == 1
    assert row.next_touch == 2
    assert row.disposition is None

    pending = database_connection.execute(
        "SELECT context FROM task WHERE enrollment_id = %s AND status = 'pending'",
        (enrollment.id,),
    ).fetchall()
    assert len(pending) == 1
    assert pending[0]["context"]["reason"] == "ooo_pause"
    assert pending[0]["context"]["touch"] == 2


def test_retried_t2_not_recancelled_by_ooo_inbound(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.83 / §V.169 / §V.170: retry of OOO-cancelled T2 does not re-cancel."""
    account, contact, _workflow, _enrollment, t2 = _seed_t1(
        database_connection, suffix="retryooo"
    )
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Automatic reply: out of the office until 2026-08-17",
        body_text="I am out of the office until 2026-08-17.",
        gmail_thread_id="t-ooo-retryooo",
        contact_id=contact.id,
        sender=contact.email,
        received_at=_NOW + timedelta(hours=1),
    )
    assert inbound is not None
    route_email(database_connection, inbound, contact.email, make_test_settings())

    cancelled = get_task(database_connection, t2.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"

    result = retry_tasks_matching(
        database_connection, task_id=t2.id, status="cancelled"
    )
    assert result.retried_count == 1
    reset = get_task(database_connection, t2.id)
    assert reset is not None
    assert reset.status == "pending"
    assert reset.scheduled_at == t2.scheduled_at

    with patch(
        "mailpilot.run.invoke_workflow_agent",
        return_value={"tool_calls": 0, "reasoning": "sent touch 2"},
    ) as mock_invoke:
        execute_task(database_connection, make_test_settings(), reset)

    mock_invoke.assert_called_once()
    done = get_task(database_connection, t2.id)
    assert done is not None
    assert done.status == "completed"
    assert done.result.get("reason") != "contact replied after prior touch"


def test_schedule_ooo_resume_is_idempotent(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account, contact, workflow, enrollment, _t2 = _seed_t1(
        database_connection, suffix="idem"
    )
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Automatic reply: out of office until 2026-08-17",
        body_text="Out of office until 2026-08-17.",
        contact_id=contact.id,
        workflow_id=workflow.id,
    )
    assert inbound is not None
    first = schedule_ooo_resume(
        database_connection, workflow, enrollment, inbound, now=_NOW
    )
    second = schedule_ooo_resume(
        database_connection, workflow, enrollment, inbound, now=_NOW
    )
    assert first is not None
    assert second is not None
    assert first.id == second.id
    count = database_connection.execute(
        "SELECT COUNT(*) AS n FROM task WHERE enrollment_id = %s "
        "AND context->>'reason' = 'ooo_pause'",
        (enrollment.id,),
    ).fetchone()
    assert count is not None
    assert count["n"] == 1
