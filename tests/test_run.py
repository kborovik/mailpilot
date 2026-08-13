"""Tests for the run loop module."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import psycopg

from mailpilot.models import (
    Account,
    Contact,
    Email,
    Enrollment,
    Task,
    Workflow,
)

_NOW = datetime(2024, 1, 1, tzinfo=UTC)
_ACCOUNT_ID = "01234567-0000-7000-0000-000000000001"
_WORKFLOW_ID = "01234567-0000-7000-0000-000000000002"
_CONTACT_ID = "01234567-0000-7000-0000-000000000003"
_TASK_ID = "01234567-0000-7000-0000-000000000004"
_EMAIL_ID = "01234567-0000-7000-0000-000000000005"
_ENROLLMENT_ID = "01234567-0000-7000-0000-000000000006"


def _make_workflow(**overrides: Any) -> Workflow:
    defaults: dict[str, Any] = {
        "id": _WORKFLOW_ID,
        "name": "Test workflow",
        "template": "outbound-general",
        "type": "outbound",
        "account_id": _ACCOUNT_ID,
        "account_email": "owner@example.com",
        "status": "active",
        "goal": "Test",
        "instructions": "Do the thing.",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Workflow(**{**defaults, **overrides})


def _make_account(**overrides: Any) -> Account:
    defaults: dict[str, Any] = {
        "id": _ACCOUNT_ID,
        "email": "owner@example.com",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Account(**{**defaults, **overrides})


def _make_contact(**overrides: Any) -> Contact:
    defaults: dict[str, Any] = {
        "id": _CONTACT_ID,
        "email": "test@example.com",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Contact(**{**defaults, **overrides})


def _make_task(**overrides: Any) -> Task:
    defaults: dict[str, Any] = {
        "id": _TASK_ID,
        "enrollment_id": _ENROLLMENT_ID,
        "workflow_id": _WORKFLOW_ID,
        "contact_id": _CONTACT_ID,
        "email_id": None,
        "description": "follow up",
        "context": {},
        "scheduled_at": _NOW,
        "status": "pending",
        "result": {},
        "completed_at": None,
        "created_at": _NOW,
    }
    return Task(**{**defaults, **overrides})


def _make_enrollment(**overrides: Any) -> Enrollment:
    defaults: dict[str, Any] = {
        "id": _ENROLLMENT_ID,
        "workflow_id": _WORKFLOW_ID,
        "workflow_name": "Outbound Campaign",
        "contact_id": _CONTACT_ID,
        "contact_email": "alice@example.com",
        "contact_name": "Alice Smith",
        "status": "active",
        "reason": "",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Enrollment(**{**defaults, **overrides})


def _make_email(**overrides: Any) -> Email:
    defaults: dict[str, Any] = {
        "id": _EMAIL_ID,
        "gmail_message_id": "msg-001",
        "gmail_thread_id": "thread-001",
        "account_id": _ACCOUNT_ID,
        "contact_id": _CONTACT_ID,
        "workflow_id": _WORKFLOW_ID,
        "direction": "inbound",
        "subject": "Re: hello",
        "body_text": "Got it",
        "labels": ["INBOX"],
        "status": "received",
        "is_routed": True,
        "received_at": _NOW,
        "created_at": _NOW,
    }
    return Email(**{**defaults, **overrides})


def test_execute_task_success(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task()
    workflow = _make_workflow()
    contact = _make_contact()
    enrollment = _make_enrollment()

    agent_result = {"tool_calls": 2, "reasoning": "Sent follow-up."}
    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch(
            "mailpilot.run.invoke_workflow_agent",
            return_value=agent_result,
        ) as mock_invoke,
        patch("mailpilot.run.complete_task") as mock_complete,
    ):
        execute_task(database_connection, settings, task)

    mock_invoke.assert_called_once_with(
        database_connection,
        settings,
        workflow,
        contact,
        email=None,
        task_description="follow up",
        task_context={},
        trigger="task",
        task_id=_TASK_ID,
    )
    mock_complete.assert_called_once_with(
        database_connection,
        _TASK_ID,
        status="completed",
        result=agent_result,
    )


def test_execute_task_threads_trigger_from_context(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.32 + §V.30: scheduled first-touch task carries
    ``trigger=enrollment_schedule`` in its context; ``execute_task`` must
    surface that to ``invoke_workflow_agent`` so the agent sees first-touch
    framing, not deferred-task framing."""
    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task(context={"trigger": "enrollment_schedule"})
    workflow = _make_workflow()
    contact = _make_contact()
    enrollment = _make_enrollment()

    agent_result = {"tool_calls": 1, "reasoning": "Sent initial."}
    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch(
            "mailpilot.run.invoke_workflow_agent",
            return_value=agent_result,
        ) as mock_invoke,
        patch("mailpilot.run.complete_task"),
    ):
        execute_task(database_connection, settings, task)

    mock_invoke.assert_called_once_with(
        database_connection,
        settings,
        workflow,
        contact,
        email=None,
        task_description="follow up",
        task_context={"trigger": "enrollment_schedule"},
        trigger="enrollment_schedule",
        task_id=_TASK_ID,
    )


def test_execute_task_null_cadence_touch_reschedules_and_skips_invoke(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.136 NULL-cadence belt: a scheduled touch N>=2 whose workflow has no
    cadence pair (touches is None) is pushed one hour out (§V.25 reschedule
    shape) instead of invoking the agent -- it is neither invoked nor completed."""
    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task(context={"touch": 2, "prior_email_id": "e1"})
    workflow = _make_workflow()  # touches defaults to None
    contact = _make_contact()
    enrollment = _make_enrollment()

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch("mailpilot.run.invoke_workflow_agent") as mock_invoke,
        patch("mailpilot.run.reschedule_task_for_lock_contention") as mock_reschedule,
        patch("mailpilot.run.complete_task") as mock_complete,
    ):
        execute_task(database_connection, settings, task)

    mock_invoke.assert_not_called()
    mock_complete.assert_not_called()
    mock_reschedule.assert_called_once_with(database_connection, _TASK_ID, 3600)


def test_execute_task_touch_with_cadence_pair_proceeds_to_invoke(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.136: the NULL-cadence belt fires only when ``touches`` is None -- a
    touch N>=2 with a live cadence pair proceeds to invoke, not rescheduled."""
    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task(context={"touch": 2, "prior_email_id": "e1"})
    workflow = _make_workflow(touches=3, touch_interval_days=7)
    contact = _make_contact()
    enrollment = _make_enrollment()

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch(
            "mailpilot.run.invoke_workflow_agent",
            return_value={"tool_calls": 0, "reasoning": "sent touch 2"},
        ) as mock_invoke,
        patch("mailpilot.run.reschedule_task_for_lock_contention") as mock_reschedule,
        patch("mailpilot.run.complete_task"),
    ):
        execute_task(database_connection, settings, task)

    mock_invoke.assert_called_once()
    mock_reschedule.assert_not_called()


def test_execute_task_touch_cancelled_when_enrollment_concluded(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.83: a queued touch is cancelled with no LLM call once the enrollment
    has a terminal outcome -- the sequence concluded, no cold send is due."""
    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task(context={"touch": 2, "prior_email_id": "e1"})
    workflow = _make_workflow(touches=3, touch_interval_days=7)
    contact = _make_contact()
    enrollment = _make_enrollment()

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch(
            "mailpilot.run.get_latest_enrollment_outcome",
            return_value="failed",
        ),
        patch("mailpilot.run.invoke_workflow_agent") as mock_invoke,
        patch("mailpilot.run.complete_task") as mock_complete,
    ):
        execute_task(database_connection, settings, task)

    mock_invoke.assert_not_called()
    mock_complete.assert_called_once_with(
        database_connection,
        _TASK_ID,
        status="cancelled",
        result={"reason": "enrollment already concluded"},
    )


def test_execute_task_touch_cancelled_when_contact_replied_after_prior_touch(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.83: a queued touch is cancelled with no LLM call when the contact
    replied after the prior touch -- an engaged contact skips the cold touch."""
    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task(context={"touch": 2, "prior_email_id": "e1"})
    workflow = _make_workflow(touches=3, touch_interval_days=7)
    contact = _make_contact()
    enrollment = _make_enrollment()
    prior_touch = _make_email(direction="outbound", sent_at=_NOW)

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch("mailpilot.run.get_latest_enrollment_outcome", return_value=None),
        patch("mailpilot.run.get_email", return_value=prior_touch),
        patch(
            "mailpilot.run.has_inbound_email_from_contact_after",
            return_value=True,
        ) as mock_inbound,
        patch("mailpilot.run.invoke_workflow_agent") as mock_invoke,
        patch("mailpilot.run.complete_task") as mock_complete,
    ):
        execute_task(database_connection, settings, task)

    mock_invoke.assert_not_called()
    mock_inbound.assert_called_once_with(database_connection, _CONTACT_ID, _NOW)
    mock_complete.assert_called_once_with(
        database_connection,
        _TASK_ID,
        status="cancelled",
        result={"reason": "contact replied after prior touch"},
    )


def test_execute_task_touch_proceeds_when_not_superseded(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.83: with no terminal outcome and no fresh reply, the touch proceeds to
    invoke -- the guard does not false-fire on a live sequence."""
    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task(context={"touch": 2, "prior_email_id": "e1"})
    workflow = _make_workflow(touches=3, touch_interval_days=7)
    contact = _make_contact()
    enrollment = _make_enrollment()
    prior_touch = _make_email(direction="outbound", sent_at=_NOW)

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch("mailpilot.run.get_latest_enrollment_outcome", return_value=None),
        patch("mailpilot.run.get_email", return_value=prior_touch),
        patch(
            "mailpilot.run.has_inbound_email_from_contact_after",
            return_value=False,
        ),
        patch(
            "mailpilot.run.invoke_workflow_agent",
            return_value={"tool_calls": 0, "reasoning": "sent touch 2"},
        ) as mock_invoke,
        patch("mailpilot.run.complete_task") as mock_complete,
    ):
        execute_task(database_connection, settings, task)

    mock_invoke.assert_called_once()
    mock_complete.assert_called_once_with(
        database_connection,
        _TASK_ID,
        status="completed",
        result={"tool_calls": 0, "reasoning": "sent touch 2"},
    )


def test_execute_task_default_trigger_is_task(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.32 regression: a task row with no ``trigger`` in context defaults to
    ``trigger='task'``, preserving the legacy drain semantics."""
    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task(context={})
    workflow = _make_workflow()
    contact = _make_contact()
    enrollment = _make_enrollment()

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch(
            "mailpilot.run.invoke_workflow_agent",
            return_value={"tool_calls": 1, "reasoning": "ok"},
        ) as mock_invoke,
        patch("mailpilot.run.complete_task"),
    ):
        execute_task(database_connection, settings, task)

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args.kwargs["trigger"] == "task"


def test_execute_task_inactive_workflow(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task()
    workflow = _make_workflow(status="paused")

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.complete_task") as mock_complete,
    ):
        execute_task(database_connection, settings, task)

    mock_complete.assert_called_once_with(
        database_connection,
        _TASK_ID,
        status="cancelled",
        result={"reason": "workflow inactive or not found"},
    )


def test_execute_task_disabled_contact(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task()
    workflow = _make_workflow()
    contact = _make_contact(disabled_reason="bounced: hard bounce")

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.complete_task") as mock_complete,
    ):
        execute_task(database_connection, settings, task)

    mock_complete.assert_called_once_with(
        database_connection,
        _TASK_ID,
        status="cancelled",
        result={"reason": "contact disabled or not found"},
    )


def test_execute_task_disabled_enrollment(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.83: pre-flight cancels the task when the enrollment is not active."""
    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task()
    workflow = _make_workflow()
    contact = _make_contact()
    enrollment = _make_enrollment(status="disabled", disabled_reason="operator hold")

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch("mailpilot.run.invoke_workflow_agent") as mock_invoke,
        patch("mailpilot.run.complete_task") as mock_complete,
    ):
        execute_task(database_connection, settings, task)

    mock_invoke.assert_not_called()
    mock_complete.assert_called_once_with(
        database_connection,
        _TASK_ID,
        status="cancelled",
        result={"reason": "enrollment disabled"},
    )


def test_execute_task_missing_enrollment(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task()
    workflow = _make_workflow()
    contact = _make_contact()

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=None),
        patch("mailpilot.run.invoke_workflow_agent") as mock_invoke,
        patch("mailpilot.run.complete_task") as mock_complete,
    ):
        execute_task(database_connection, settings, task)

    mock_invoke.assert_not_called()
    mock_complete.assert_called_once_with(
        database_connection,
        _TASK_ID,
        status="cancelled",
        result={"reason": "enrollment not found"},
    )


def test_execute_task_lock_held(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.25 / §B.42: lock contention reschedules without bumping attempt_count."""
    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task()
    workflow = _make_workflow()
    contact = _make_contact()
    enrollment = _make_enrollment()

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch("mailpilot.run.invoke_workflow_agent", return_value=None),
        patch("mailpilot.run.complete_task") as mock_complete,
        patch(
            "mailpilot.run.reschedule_task_for_lock_contention"
        ) as mock_reschedule_lock,
        patch("mailpilot.run.reschedule_task_for_retry") as mock_reschedule_retry,
    ):
        execute_task(database_connection, settings, task)

    mock_complete.assert_not_called()
    mock_reschedule_retry.assert_not_called()
    mock_reschedule_lock.assert_called_once()
    args = mock_reschedule_lock.call_args.args
    assert args[0] is database_connection
    assert args[1] == _TASK_ID
    # backoff = 5s base + 0..5s jitter -> always within [5, 10].
    assert 5 <= args[2] <= 10


def test_execute_task_passes_task_id_to_invoke(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.25: drain path threads task.id through to invoke_workflow_agent so
    the advisory lock is task-scoped, not (workflow_id, contact_id)-scoped."""
    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task()
    workflow = _make_workflow()
    contact = _make_contact()
    enrollment = _make_enrollment()

    agent_result = {"tool_calls": 1, "reasoning": "Done."}
    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch(
            "mailpilot.run.invoke_workflow_agent", return_value=agent_result
        ) as mock_invoke,
        patch("mailpilot.run.complete_task"),
    ):
        execute_task(database_connection, settings, task)

    mock_invoke.assert_called_once()
    assert mock_invoke.call_args.kwargs.get("task_id") == _TASK_ID


def test_execute_task_agent_error_non_transient_terminal(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Non-transient exception goes terminal immediately, no retry."""
    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task()
    workflow = _make_workflow()
    contact = _make_contact()
    enrollment = _make_enrollment()

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch(
            "mailpilot.run.invoke_workflow_agent",
            side_effect=RuntimeError("LLM error"),
        ),
        patch("mailpilot.run.complete_task") as mock_complete,
        patch("mailpilot.run.reschedule_task_for_retry") as mock_reschedule,
    ):
        execute_task(database_connection, settings, task)

    mock_reschedule.assert_not_called()
    mock_complete.assert_called_once_with(
        database_connection,
        _TASK_ID,
        status="failed",
        result={
            "reason": "LLM error",
            "attempt_count": 1,
            "terminal": "non_transient",
        },
    )


def test_execute_task_completed_without_reply_terminal(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.120 (per §B.106): an outbound first reach-out that drops the send
    fails the run with ``AgentCompletedWithoutReplyError``; the class is
    non-transient (§V.49), so ``_handle_agent_failure`` takes the task
    terminal ``failed`` with no retry rather than a silent ``completed``."""
    from conftest import make_test_settings
    from mailpilot.exceptions import AgentCompletedWithoutReplyError
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task(context={"trigger": "enrollment_schedule"})
    workflow = _make_workflow()
    contact = _make_contact()
    enrollment = _make_enrollment()

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch(
            "mailpilot.run.invoke_workflow_agent",
            side_effect=AgentCompletedWithoutReplyError("no send on reach-out"),
        ),
        patch("mailpilot.run.complete_task") as mock_complete,
        patch("mailpilot.run.reschedule_task_for_retry") as mock_reschedule,
    ):
        execute_task(database_connection, settings, task)

    mock_reschedule.assert_not_called()
    mock_complete.assert_called_once_with(
        database_connection,
        _TASK_ID,
        status="failed",
        result={
            "reason": "no send on reach-out",
            "attempt_count": 1,
            "terminal": "non_transient",
        },
    )


def test_execute_task_transient_error_reschedules(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Transient error w/ budget left -> reschedule, not terminal."""
    from unittest.mock import MagicMock

    from googleapiclient.errors import HttpError

    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task()
    workflow = _make_workflow()
    contact = _make_contact()
    enrollment = _make_enrollment()

    resp = MagicMock()
    resp.status = 503
    transient = HttpError(resp, b"{}", uri="https://example.com")

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch("mailpilot.run.invoke_workflow_agent", side_effect=transient),
        patch("mailpilot.run.complete_task") as mock_complete,
        patch("mailpilot.run.reschedule_task_for_retry") as mock_reschedule,
    ):
        execute_task(database_connection, settings, task)

    mock_complete.assert_not_called()
    mock_reschedule.assert_called_once()
    args = mock_reschedule.call_args.args
    assert args[0] is database_connection
    assert args[1] == _TASK_ID
    assert args[2] == 30  # BACKOFF_SECONDS[0]
    assert args[3] is transient


def test_execute_task_transient_error_budget_exhausted_terminal(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Transient error w/ attempt_count == MAX-1 -> terminal failed."""
    from unittest.mock import MagicMock

    from googleapiclient.errors import HttpError

    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task(attempt_count=3)  # 4th attempt = budget exhausted
    workflow = _make_workflow()
    contact = _make_contact()
    enrollment = _make_enrollment()

    resp = MagicMock()
    resp.status = 503
    transient = HttpError(resp, b"{}", uri="https://example.com")

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch("mailpilot.run.invoke_workflow_agent", side_effect=transient),
        patch("mailpilot.run.complete_task") as mock_complete,
        patch("mailpilot.run.reschedule_task_for_retry") as mock_reschedule,
    ):
        execute_task(database_connection, settings, task)

    mock_reschedule.assert_not_called()
    mock_complete.assert_called_once()
    kwargs = mock_complete.call_args.kwargs
    assert kwargs["status"] == "failed"
    assert kwargs["result"]["attempt_count"] == 4
    assert kwargs["result"]["terminal"] == "max_attempts"


def test_execute_task_apitimeout_is_terminal_v43_exclusion(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.48 exclusion: anthropic.APITimeoutError mid-turn cannot be
    re-driven safely, must go terminal regardless of attempt budget."""
    import httpx
    from anthropic import APITimeoutError

    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task()
    workflow = _make_workflow()
    contact = _make_contact()
    enrollment = _make_enrollment()

    timeout_err = APITimeoutError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch("mailpilot.run.invoke_workflow_agent", side_effect=timeout_err),
        patch("mailpilot.run.complete_task") as mock_complete,
        patch("mailpilot.run.reschedule_task_for_retry") as mock_reschedule,
    ):
        execute_task(database_connection, settings, task)

    mock_reschedule.assert_not_called()
    mock_complete.assert_called_once()
    assert mock_complete.call_args.kwargs["status"] == "failed"
    assert mock_complete.call_args.kwargs["result"]["terminal"] == "non_transient"


def test_execute_task_httpx_readtimeout_is_terminal_v43_exclusion(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.48 exclusion: httpx.ReadTimeout from the Anthropic transport
    is not transient for retry purposes."""
    import httpx

    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task()
    workflow = _make_workflow()
    contact = _make_contact()
    enrollment = _make_enrollment()

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch(
            "mailpilot.run.invoke_workflow_agent",
            side_effect=httpx.ReadTimeout("read timeout"),
        ),
        patch("mailpilot.run.complete_task") as mock_complete,
        patch("mailpilot.run.reschedule_task_for_retry") as mock_reschedule,
    ):
        execute_task(database_connection, settings, task)

    mock_reschedule.assert_not_called()
    mock_complete.assert_called_once()
    assert mock_complete.call_args.kwargs["result"]["terminal"] == "non_transient"


def test_execute_task_with_email(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    email = _make_email()
    task = _make_task(email_id=_EMAIL_ID)
    workflow = _make_workflow()
    contact = _make_contact()
    enrollment = _make_enrollment()

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch("mailpilot.run.get_email", return_value=email),
        patch(
            "mailpilot.run.invoke_workflow_agent",
            return_value={"tool_calls": 1},
        ) as mock_invoke,
        patch("mailpilot.run.complete_task"),
    ):
        execute_task(database_connection, settings, task)

    mock_invoke.assert_called_once_with(
        database_connection,
        settings,
        workflow,
        contact,
        email=email,
        task_description="follow up",
        task_context={},
        trigger="task",
        task_id=_TASK_ID,
    )


def test_execute_task_terminal_inbound_failure_sends_fallback(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.131: a terminal failure on an inbound-email task sends one fixed
    content-free acknowledgement before marking the task ``failed`` so the
    sender never gets silent NO_REPLY (§B.116)."""
    from unittest.mock import MagicMock

    from conftest import make_test_settings
    from mailpilot.agent.templates import (
        _FALLBACK_ACKNOWLEDGEMENT,  # pyright: ignore[reportPrivateUsage]
    )
    from mailpilot.run import execute_task

    settings = make_test_settings()
    email = _make_email()
    task = _make_task(email_id=_EMAIL_ID)
    workflow = _make_workflow()
    contact = _make_contact()
    enrollment = _make_enrollment()
    account = _make_account()

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch("mailpilot.run.get_email", return_value=email),
        patch("mailpilot.run.get_account", return_value=account),
        patch("mailpilot.run.GmailClient", return_value=MagicMock()),
        patch(
            "mailpilot.run.invoke_workflow_agent",
            side_effect=RuntimeError("LLM error"),
        ),
        patch("mailpilot.run.email_ops") as mock_email_ops,
        patch("mailpilot.run.complete_task") as mock_complete,
    ):
        execute_task(database_connection, settings, task)

    mock_email_ops.reply_email.assert_called_once()
    kwargs = mock_email_ops.reply_email.call_args.kwargs
    assert kwargs["email_id"] == _EMAIL_ID
    assert kwargs["body"] == _FALLBACK_ACKNOWLEDGEMENT
    mock_complete.assert_called_once_with(
        database_connection,
        _TASK_ID,
        status="failed",
        result={
            "reason": "LLM error",
            "attempt_count": 1,
            "terminal": "non_transient",
        },
    )


def test_execute_task_terminal_outbound_failure_sends_no_fallback(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.131: an outbound first reach-out failure (``email_id`` NULL) stays
    silent -- no fallback acknowledgement on a cold open."""
    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task(email_id=None)
    workflow = _make_workflow()
    contact = _make_contact()
    enrollment = _make_enrollment()

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch(
            "mailpilot.run.invoke_workflow_agent",
            side_effect=RuntimeError("LLM error"),
        ),
        patch("mailpilot.run.email_ops") as mock_email_ops,
        patch("mailpilot.run.complete_task") as mock_complete,
    ):
        execute_task(database_connection, settings, task)

    mock_email_ops.reply_email.assert_not_called()
    assert mock_complete.call_args.kwargs["status"] == "failed"


def test_execute_task_no_double_reply_when_reply_emitted(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.131: when a reply was emitted mid-turn before a non-transient class
    raised, the in-run reply-emitted flag blocks a second fallback reply."""
    from conftest import make_test_settings
    from mailpilot.agent.tools import (
        _mark_reply_emitted,  # pyright: ignore[reportPrivateUsage]
    )
    from mailpilot.run import execute_task

    settings = make_test_settings()
    email = _make_email()
    task = _make_task(email_id=_EMAIL_ID)
    workflow = _make_workflow()
    contact = _make_contact()
    enrollment = _make_enrollment()

    def _send_then_raise(*_args: Any, **_kwargs: Any) -> None:
        # Simulate a successful mid-turn send followed by a terminal class.
        _mark_reply_emitted()
        raise RuntimeError("post-send failure")

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch("mailpilot.run.get_email", return_value=email),
        patch(
            "mailpilot.run.invoke_workflow_agent",
            side_effect=_send_then_raise,
        ),
        patch("mailpilot.run.email_ops") as mock_email_ops,
        patch("mailpilot.run.complete_task") as mock_complete,
    ):
        execute_task(database_connection, settings, task)

    mock_email_ops.reply_email.assert_not_called()
    assert mock_complete.call_args.kwargs["status"] == "failed"


def test_execute_task_ooo_inbound_failure_sends_no_fallback(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.131 / §V.169: terminal failure on an OOO inbound sends no ACK."""
    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    email = _make_email(
        subject="Re: Touch 1",
        body_text="I am out of the office returning Thursday.",
    )
    task = _make_task(email_id=_EMAIL_ID)
    workflow = _make_workflow()
    contact = _make_contact()
    enrollment = _make_enrollment()

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch("mailpilot.run.get_email", return_value=email),
        patch(
            "mailpilot.run.invoke_workflow_agent",
            side_effect=RuntimeError("LLM error"),
        ),
        patch("mailpilot.run.schedule_ooo_resume") as mock_resume,
        patch("mailpilot.run.email_ops") as mock_email_ops,
        patch("mailpilot.run.complete_task") as mock_complete,
    ):
        execute_task(database_connection, settings, task)

    mock_email_ops.reply_email.assert_not_called()
    mock_resume.assert_called_once()
    assert mock_complete.call_args.kwargs["status"] == "failed"


def test_execute_task_mechanical_ooo_skips_agent(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.169: Automatic-reply OOO on outbound completes without an agent turn."""
    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    email = _make_email(
        subject="Automatic reply: out of the office until Monday",
        body_text="I am out of the office until Monday.",
    )
    task = _make_task(email_id=_EMAIL_ID)
    workflow = _make_workflow()
    contact = _make_contact()
    enrollment = _make_enrollment()

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch("mailpilot.run.get_email", return_value=email),
        patch("mailpilot.run.invoke_workflow_agent") as mock_invoke,
        patch("mailpilot.run.schedule_ooo_resume") as mock_resume,
        patch("mailpilot.run.email_ops") as mock_email_ops,
        patch("mailpilot.run.complete_task") as mock_complete,
    ):
        execute_task(database_connection, settings, task)

    mock_invoke.assert_not_called()
    mock_email_ops.reply_email.assert_not_called()
    mock_resume.assert_called_once()
    mock_complete.assert_called_once_with(
        database_connection,
        _TASK_ID,
        status="completed",
        result={"reason": "ooo_pause"},
    )


def test_execute_task_fallback_send_failure_still_marks_failed(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.131: a fallback-send failure is best-effort -- it logs an operator
    error and still falls through to mark the task ``failed``."""
    from unittest.mock import MagicMock

    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    email = _make_email()
    task = _make_task(email_id=_EMAIL_ID)
    workflow = _make_workflow()
    contact = _make_contact()
    enrollment = _make_enrollment()
    account = _make_account()

    mock_email_ops = MagicMock()
    mock_email_ops.reply_email.side_effect = RuntimeError("gmail send failed")

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_enrollment", return_value=enrollment),
        patch("mailpilot.run.get_email", return_value=email),
        patch("mailpilot.run.get_account", return_value=account),
        patch("mailpilot.run.GmailClient", return_value=MagicMock()),
        patch(
            "mailpilot.run.invoke_workflow_agent",
            side_effect=RuntimeError("LLM error"),
        ),
        patch("mailpilot.run.email_ops", mock_email_ops),
        patch("mailpilot.run.complete_task") as mock_complete,
        patch("mailpilot.run.operator_event") as mock_operator_event,
    ):
        execute_task(database_connection, settings, task)

    mock_email_ops.reply_email.assert_called_once()
    assert mock_complete.call_args.kwargs["status"] == "failed"
    sources = [c.kwargs.get("source") for c in mock_operator_event.call_args_list]
    assert "run.task.fallback_failed" in sources
    assert "run.task.agent_failed" in sources
