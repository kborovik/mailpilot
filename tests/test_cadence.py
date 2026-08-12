"""Tests for the system-owned touch-cadence engine (§V.136).

Covers the schedule math (``next_touch_scheduled_at`` -- interval plus
weekend-roll-to-Monday) and the advance decision (``advance_touch_cadence`` --
single-touch no-op, mid-sequence next-touch scheduling, and final-touch
system-internal conclusion ``contact_later`` "sequence exhausted" per §V.127 /
§V.128).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg

from conftest import make_test_account, make_test_contact, make_test_enrollment
from mailpilot import database
from mailpilot.cadence import (
    advance_touch_cadence,
    next_touch_scheduled_at,
    parse_touch_number,
    resolve_touch_number,
)
from mailpilot.database import create_workflow, update_workflow
from mailpilot.models import Enrollment, Workflow

# -- resolve_touch_number: compose-only dispatch key (§V.136) ------------------


def test_resolve_touch_number_from_context_touch() -> None:
    """§V.136: a scheduled touch-N task carries context.touch -> that N."""
    assert resolve_touch_number({"touch": 2, "prior_email_id": "e"}, "task") == 2


def test_resolve_touch_number_context_wins_over_trigger() -> None:
    """§V.136: context.touch takes precedence over the trigger-derived touch 1."""
    assert resolve_touch_number({"touch": 3}, "enrollment_run") == 3


def test_resolve_touch_number_first_reach_out_triggers_are_touch_one() -> None:
    """§V.136: enrollment_run / enrollment_schedule with no context.touch = touch 1."""
    assert resolve_touch_number(None, "enrollment_run") == 1
    assert (
        resolve_touch_number({"trigger": "enrollment_schedule"}, "enrollment_schedule")
        == 1
    )


def test_resolve_touch_number_tool_loop_triggers_return_none() -> None:
    """§V.136: a deferred reply-branch task / inbound reply / manual run stays on
    the tool loop -- no touch number."""
    assert resolve_touch_number(None, "task") is None
    assert resolve_touch_number({}, "email") is None
    assert resolve_touch_number(None, "manual") is None
    # Unparseable touch is ignored (malformed context, not a touch run).
    assert resolve_touch_number({"touch": "oops"}, "task") is None


def test_resolve_touch_number_parses_int_and_t_label() -> None:
    """§V.162: JSON number 2, digit string, and T<n> all resolve to 2."""
    assert resolve_touch_number({"touch": 2}, "task") == 2
    assert resolve_touch_number({"touch": "2"}, "task") == 2
    assert resolve_touch_number({"touch": "T2"}, "task") == 2


def test_parse_touch_number_rejects_unparseable() -> None:
    """§V.162: unparseable touch values return None, never raise."""
    assert parse_touch_number(None) is None
    assert parse_touch_number(True) is None
    assert parse_touch_number("oops") is None
    assert parse_touch_number("T") is None
    assert parse_touch_number("") is None
    assert parse_touch_number("T2a") is None


# -- next_touch_scheduled_at: interval + weekend roll (§V.136) -----------------


def test_next_touch_weekday_interval_lands_on_weekday() -> None:
    # 2026-07-06 is a Monday; +7 days = Monday 2026-07-13, no roll.
    base = datetime(2026, 7, 6, 13, 30, tzinfo=UTC)
    assert next_touch_scheduled_at(base, 7) == datetime(2026, 7, 13, 13, 30, tzinfo=UTC)


def test_next_touch_saturday_rolls_to_monday() -> None:
    # 2026-07-04 Saturday; +7 = Saturday 2026-07-11 -> Monday 2026-07-13.
    base = datetime(2026, 7, 4, 9, 0, tzinfo=UTC)
    assert next_touch_scheduled_at(base, 7) == datetime(2026, 7, 13, 9, 0, tzinfo=UTC)


def test_next_touch_sunday_rolls_to_monday() -> None:
    # 2026-07-05 Sunday; +7 = Sunday 2026-07-12 -> Monday 2026-07-13.
    base = datetime(2026, 7, 5, 9, 0, tzinfo=UTC)
    assert next_touch_scheduled_at(base, 7) == datetime(2026, 7, 13, 9, 0, tzinfo=UTC)


# -- advance_touch_cadence (§V.136) -------------------------------------------


def _seed_cadence_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    touches: int | None,
    touch_interval_days: int | None,
) -> tuple[Workflow, Enrollment]:
    """Seed an active outbound workflow (optional cadence) + enrollment."""
    account = make_test_account(connection)
    contact = make_test_contact(connection)
    workflow = create_workflow(
        connection, name="cad", template="outbound-general", account_id=account.id
    )
    assert workflow is not None
    if touches is not None:
        workflow = update_workflow(
            connection,
            workflow.id,
            touches=touches,
            touch_interval_days=touch_interval_days,
        )
        assert workflow is not None
    enrollment = make_test_enrollment(connection, workflow.id, contact.id)
    return workflow, enrollment


def test_advance_single_touch_is_noop(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.136: NULL cadence sends touch 1 and schedules nothing, concludes nothing."""
    workflow, enrollment = _seed_cadence_workflow(database_connection, None, None)
    base = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
    advance_touch_cadence(
        database_connection,
        workflow,
        enrollment,
        sent_touch_number=1,
        prior_email_id="em-1",
        base=base,
    )
    tasks = database_connection.execute(
        "SELECT id FROM task WHERE enrollment_id = %s", (enrollment.id,)
    ).fetchall()
    assert tasks == []
    outcomes = database_connection.execute(
        "SELECT id FROM activity WHERE enrollment_id = %s "
        "AND type IN ('enrollment_completed', 'enrollment_failed')",
        (enrollment.id,),
    ).fetchall()
    assert outcomes == []


def test_advance_mid_sequence_schedules_next_touch(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.136: after touch N (< touches) schedule touch N+1 with context
    {touch, prior_email_id} at the weekend-rolled interval; scheduled_at is
    system-computed, email_id NULL."""
    workflow, enrollment = _seed_cadence_workflow(database_connection, 3, 7)
    base = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)  # Monday -> +7 Monday, no roll
    advance_touch_cadence(
        database_connection,
        workflow,
        enrollment,
        sent_touch_number=1,
        prior_email_id="em-1",
        base=base,
    )
    rows = database_connection.execute(
        "SELECT description, context, scheduled_at, status, email_id "
        "FROM task WHERE enrollment_id = %s",
        (enrollment.id,),
    ).fetchall()
    assert len(rows) == 1
    task = rows[0]
    assert task["description"] == "Touch 2 of 3"
    assert task["context"] == {"touch": 2, "prior_email_id": "em-1"}
    assert task["status"] == "pending"
    assert task["email_id"] is None
    assert task["scheduled_at"] == datetime(2026, 7, 13, 12, 0, tzinfo=UTC)


def test_advance_final_touch_concludes_contact_later(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.136/§V.127/§V.128: after the final touch the harness concludes the
    enrollment contact_later ("sequence exhausted") system-internally -- records
    the failed/contact_later outcome, cancels stray follow-ups, writes a note,
    schedules no new touch and no re-enrollment task."""
    workflow, enrollment = _seed_cadence_workflow(database_connection, 3, 7)
    contact_id = enrollment.contact_id
    # A stray future follow-up that the conclusion must cancel.
    database.create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact_id,
        description="Touch 3 of 3",
        scheduled_at="2026-12-01T00:00:00+00:00",
        context={"touch": 3},
        email_id=None,
    )
    base = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
    advance_touch_cadence(
        database_connection,
        workflow,
        enrollment,
        sent_touch_number=3,
        prior_email_id="em-3",
        base=base,
    )

    # No NEW touch task; the only task (the stray) is now cancelled.
    statuses = [
        r["status"]
        for r in database_connection.execute(
            "SELECT status FROM task WHERE enrollment_id = %s", (enrollment.id,)
        ).fetchall()
    ]
    assert statuses == ["cancelled"]

    # The outcome is recorded contact_later on the timeline.
    outcome = database_connection.execute(
        "SELECT type, detail FROM activity WHERE enrollment_id = %s "
        "AND type IN ('enrollment_completed', 'enrollment_failed')",
        (enrollment.id,),
    ).fetchone()
    assert outcome is not None
    assert outcome["type"] == "enrollment_failed"
    assert outcome["detail"]["disposition"] == "contact_later"

    # A note records the conclusion; contact is NOT disabled (contact_later).
    notes = database.list_notes(database_connection, contact_id=contact_id)
    assert any("sequence exhausted" in note.body_preview for note in notes)
    contact = database.get_contact(database_connection, contact_id)
    assert contact is not None
    assert contact.disabled_reason is None
