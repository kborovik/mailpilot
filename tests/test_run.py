"""Tests for the run loop module."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import psycopg

from mailpilot.models import (
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
        "objective": "Test",
        "instructions": "Do the thing.",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Workflow(**{**defaults, **overrides})


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


def test_execute_task_paused_enrollment(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task()
    workflow = _make_workflow()
    contact = _make_contact()
    enrollment = _make_enrollment(status="paused", reason="operator hold")

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
        result={"reason": "enrollment paused"},
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
