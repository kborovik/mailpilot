"""Tests for the run loop module."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import psycopg

from mailpilot.models import (
    Contact,
    Email,
    Task,
    Workflow,
)

_NOW = datetime(2024, 1, 1, tzinfo=UTC)
_ACCOUNT_ID = "01234567-0000-7000-0000-000000000001"
_WORKFLOW_ID = "01234567-0000-7000-0000-000000000002"
_CONTACT_ID = "01234567-0000-7000-0000-000000000003"
_TASK_ID = "01234567-0000-7000-0000-000000000004"
_EMAIL_ID = "01234567-0000-7000-0000-000000000005"


def _make_workflow(**overrides: Any) -> Workflow:
    defaults: dict[str, Any] = {
        "id": _WORKFLOW_ID,
        "name": "Test workflow",
        "type": "outbound",
        "account_id": _ACCOUNT_ID,
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
        "domain": "example.com",
        "status": "active",
        "status_reason": "",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Contact(**{**defaults, **overrides})


def _make_task(**overrides: Any) -> Task:
    defaults: dict[str, Any] = {
        "id": _TASK_ID,
        "workflow_id": _WORKFLOW_ID,
        "contact_id": _CONTACT_ID,
        "email_id": None,
        "description": "follow up",
        "context": {},
        "scheduled_at": _NOW,
        "status": "pending",
        "completed_at": None,
        "created_at": _NOW,
    }
    return Task(**{**defaults, **overrides})


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

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch(
            "mailpilot.run.invoke_workflow_agent",
            return_value={"tool_calls": 2},
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
    )
    mock_complete.assert_called_once_with(
        database_connection, _TASK_ID, status="completed"
    )


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
        database_connection, _TASK_ID, status="cancelled"
    )


def test_execute_task_disabled_contact(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from conftest import make_test_settings
    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task()
    workflow = _make_workflow()
    contact = _make_contact(status="bounced")

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.complete_task") as mock_complete,
    ):
        execute_task(database_connection, settings, task)

    mock_complete.assert_called_once_with(
        database_connection, _TASK_ID, status="cancelled"
    )


def test_execute_task_lock_held(
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
        patch("mailpilot.run.invoke_workflow_agent", return_value=None),
        patch("mailpilot.run.complete_task") as mock_complete,
    ):
        execute_task(database_connection, settings, task)

    mock_complete.assert_not_called()


def test_execute_task_agent_error(
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
        patch(
            "mailpilot.run.invoke_workflow_agent",
            side_effect=RuntimeError("LLM error"),
        ),
        patch("mailpilot.run.complete_task") as mock_complete,
    ):
        execute_task(database_connection, settings, task)

    mock_complete.assert_called_once_with(
        database_connection, _TASK_ID, status="failed"
    )


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

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
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
    )
