"""Operator-log emissions from the run loop's account-sync error path."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from conftest import make_test_settings
from mailpilot.models import Contact, Enrollment, Task, Workflow

_NOW = datetime(2024, 1, 1, tzinfo=UTC)
_ACCOUNT_ID = "01234567-0000-7000-0000-000000000001"
_WORKFLOW_ID = "01234567-0000-7000-0000-000000000002"
_CONTACT_ID = "01234567-0000-7000-0000-000000000003"
_TASK_ID = "01234567-0000-7000-0000-000000000004"


def _wf() -> Workflow:
    return Workflow(
        id=_WORKFLOW_ID,
        account_id=_ACCOUNT_ID,
        name="Test workflow",
        template="outbound-general",
        type="outbound",
        status="active",
        objective="Test",
        instructions="Do.",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _contact() -> Contact:
    return Contact(
        id=_CONTACT_ID,
        email="x@example.com",
        domain="example.com",
        status="active",
        status_reason="",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _enrollment() -> Enrollment:
    return Enrollment(
        workflow_id=_WORKFLOW_ID,
        contact_id=_CONTACT_ID,
        status="active",
        reason="",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _task(attempt_count: int = 0) -> Task:
    return Task(
        id=_TASK_ID,
        workflow_id=_WORKFLOW_ID,
        contact_id=_CONTACT_ID,
        email_id=None,
        description="follow up",
        context={},
        scheduled_at=_NOW,
        status="pending",
        result={},
        attempt_count=attempt_count,
        completed_at=None,
        created_at=_NOW,
    )


def test_execute_task_transient_retry_emits_task_retry_event(
    capsys: pytest.CaptureFixture[str],
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.44: transient-retry path emits operator_event task.retry with
    task_id, attempt, exc."""
    from googleapiclient.errors import HttpError

    from mailpilot.run import execute_task

    settings = make_test_settings()
    resp = MagicMock()
    resp.status = 503
    transient = HttpError(resp, b"{}", uri="https://example.com")

    with (
        patch("mailpilot.run.get_workflow", return_value=_wf()),
        patch("mailpilot.run.get_contact", return_value=_contact()),
        patch("mailpilot.run.get_enrollment", return_value=_enrollment()),
        patch("mailpilot.run.invoke_workflow_agent", side_effect=transient),
        patch("mailpilot.run.reschedule_task_for_retry"),
        patch("mailpilot.run.complete_task"),
    ):
        execute_task(database_connection, settings, _task())

    err = capsys.readouterr().err
    assert "event=task.retry" in err
    assert f"task_id={_TASK_ID}" in err
    assert "attempt=1" in err
    assert "exc=HttpError" in err


def test_execute_task_terminal_retry_still_emits_error(
    capsys: pytest.CaptureFixture[str],
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.19: terminal failure (transient + budget exhausted) still pairs
    logfire.exception with operator_event(error)."""
    from googleapiclient.errors import HttpError

    from mailpilot.run import execute_task

    settings = make_test_settings()
    resp = MagicMock()
    resp.status = 503
    transient = HttpError(resp, b"{}", uri="https://example.com")

    with (
        patch("mailpilot.run.get_workflow", return_value=_wf()),
        patch("mailpilot.run.get_contact", return_value=_contact()),
        patch("mailpilot.run.get_enrollment", return_value=_enrollment()),
        patch("mailpilot.run.invoke_workflow_agent", side_effect=transient),
        patch("mailpilot.run.complete_task"),
    ):
        execute_task(database_connection, settings, _task(attempt_count=3))

    err = capsys.readouterr().err
    assert "event=error" in err
    assert "source=run.task.agent_failed" in err
