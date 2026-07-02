"""System-owned touch-cadence engine (§V.136).

The workflow definition carries a nullable cadence pair -- ``touches`` (total
sends in the sequence) and ``touch_interval_days`` (spacing between them). After
a successful touch-N send the harness advances the cadence: it schedules the
touch-(N+1) task, or, once the final touch has gone out, concludes the
enrollment ``contact_later`` ("sequence exhausted") through the same
system-internal path calendar booking uses (§V.127, §V.128) -- no agent turn and
no re-enrollment task. Every ``scheduled_at`` is system-computed, so it is exempt
from the agent-supplied-timestamp guard (§V.129).

This module owns the schedule math and the advance decision. The send itself and
the call into ``advance_touch_cadence`` are wired by the compose-only touch path
(§V.136).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import psycopg

from mailpilot import database
from mailpilot.models import Enrollment, Workflow

_SATURDAY = 5
_SUNDAY = 6

_FIRST_TOUCH_TRIGGERS = ("enrollment_run", "enrollment_schedule")


def resolve_touch_number(
    task_context: dict[str, Any] | None,
    trigger: str,
) -> int | None:
    """Return the touch number for a compose-only touch run, else ``None`` (§V.136).

    A run composes a touch (rather than driving a tool loop) when it is a
    system-scheduled touch-N task -- ``task_context['touch']`` set by the cadence
    engine -- or a first reach-out (``trigger`` in {enrollment_run,
    enrollment_schedule}, which are touch 1). Any other run (a deferred
    reply-branch task, an inbound reply, a manual call) returns ``None`` and
    stays on the tool loop.

    ``task_context['touch']`` wins over the trigger so a touch-N task drained as
    the generic ``task`` trigger still resolves to N.

    Args:
        task_context: The task row's JSON context, if any.
        trigger: The ``agent.invoke`` trigger label (§V.26).

    Returns:
        The 1-based touch number, or ``None`` for a tool-loop run.
    """
    if task_context is not None:
        touch = task_context.get("touch")
        if isinstance(touch, int):
            return touch
    if trigger in _FIRST_TOUCH_TRIGGERS:
        return 1
    return None


def next_touch_scheduled_at(base: datetime, interval_days: int) -> datetime:
    """Return the next touch time: ``base`` plus the interval, weekend-rolled.

    Adds ``interval_days`` to ``base`` (preserving the time of day), then rolls a
    Saturday landing forward to the following Monday and a Sunday landing forward
    to Monday, so a cold touch never sends on a weekend (§V.136).

    Args:
        base: Anchor timestamp -- the moment the prior touch was sent.
        interval_days: Days to wait before the next touch.

    Returns:
        The system-computed ``scheduled_at`` for the next touch.
    """
    target = base + timedelta(days=interval_days)
    weekday = target.weekday()
    if weekday == _SATURDAY:
        target += timedelta(days=2)
    elif weekday == _SUNDAY:
        target += timedelta(days=1)
    return target


def advance_touch_cadence(  # noqa: PLR0913
    connection: psycopg.Connection[dict[str, Any]],
    workflow: Workflow,
    enrollment: Enrollment,
    sent_touch_number: int,
    prior_email_id: str | None,
    base: datetime,
) -> None:
    """Advance the touch cadence after touch ``sent_touch_number`` sent (§V.136).

    - Single-touch workflow (``touches`` unset): schedule nothing, conclude
      nothing -- touch 1 stands alone.
    - Touches remain: schedule the next touch as a pending task carrying context
      ``{touch: N+1, prior_email_id}`` at the weekend-rolled interval.
    - Final touch just sent: conclude the enrollment ``contact_later`` ("sequence
      exhausted") system-internally -- record the failed outcome, cancel any
      stray follow-ups, write a note. No re-enrollment task, no agent turn
      (§V.127, §V.128).

    ``scheduled_at`` is system-computed and goes straight to
    ``database.create_task``, exempt from the §V.129 past-timestamp guard.

    Args:
        connection: Open database connection.
        workflow: The enrollment's workflow, carrying the cadence def fields.
        enrollment: The active enrollment whose cadence advances.
        sent_touch_number: The touch number just sent (1-based).
        prior_email_id: The id of the email just sent, threaded into the next
            touch's context so it continues the same conversation.
        base: Anchor timestamp for the interval math (the send moment).
    """
    if workflow.touches is None or workflow.touch_interval_days is None:
        # Single-touch: touch 1 sends and schedules nothing (§V.136).
        return

    if sent_touch_number < workflow.touches:
        next_touch_number = sent_touch_number + 1
        scheduled_at = next_touch_scheduled_at(base, workflow.touch_interval_days)
        database.create_task(
            connection,
            enrollment_id=enrollment.id,
            workflow_id=workflow.id,
            contact_id=enrollment.contact_id,
            description=f"Touch {next_touch_number} of {workflow.touches}",
            scheduled_at=scheduled_at.isoformat(),
            context={"touch": next_touch_number, "prior_email_id": prior_email_id},
            email_id=None,
        )
        return

    # Final touch sent: system-internal conclusion mirroring the calendar-booking
    # shape (§V.128) -- record + cancel + note -- but with a failed/contact_later
    # outcome. No re-enrollment task (§V.127).
    reason = f"sequence exhausted: no reply after {workflow.touches} touches"
    database.record_enrollment_outcome(
        connection,
        enrollment.id,
        outcome="failed",
        reason=reason,
        disposition="contact_later",
    )
    database.cancel_enrollment_followup_tasks(connection, enrollment.id)
    database.create_note(connection, body=reason, contact_id=enrollment.contact_id)
