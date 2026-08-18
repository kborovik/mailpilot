"""§V.47 / §B.141: abort ``mailpilot run`` before drain when the key is missing."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import psycopg
import pytest
from click.testing import CliRunner

from conftest import (
    make_test_account,
    make_test_contact,
    make_test_enrollment,
    make_test_settings,
    make_test_workflow,
)
from mailpilot.cli import main
from mailpilot.database import create_task, get_task, list_tasks
from mailpilot.settings import require_active_provider_key


def _seed_due_t1_batch(
    connection: psycopg.Connection[dict[str, Any]], *, count: int = 3
) -> list[str]:
    """Insert ``count`` due first-reach tasks and return their ids."""
    account = make_test_account(connection)
    workflow = make_test_workflow(connection, account_id=account.id)
    task_ids: list[str] = []
    for index in range(count):
        contact = make_test_contact(connection, email=f"prospect{index}@example.com")
        enrollment = make_test_enrollment(connection, workflow.id, contact.id)
        task = create_task(
            connection,
            enrollment_id=enrollment.id,
            workflow_id=workflow.id,
            contact_id=contact.id,
            description="scheduled first reach-out",
            scheduled_at="2020-01-01T00:00:00Z",
            context={"trigger": "enrollment_schedule", "touch": 1},
        )
        task_ids.append(task.id)
    return task_ids


def test_require_active_provider_key_xai_names_env() -> None:
    """Missing xAI key names MAILPILOT_XAI_API_KEY and does not prescribe config set."""
    settings = make_test_settings(llm_provider="xai", xai_api_key="")
    with pytest.raises(ValueError, match="MAILPILOT_XAI_API_KEY") as exc_info:
        require_active_provider_key(settings)
    message = str(exc_info.value)
    assert "mailpilot config set" not in message


def test_require_active_provider_key_anthropic_names_env() -> None:
    """Missing Anthropic key names MAILPILOT_ANTHROPIC_API_KEY."""
    settings = make_test_settings(llm_provider="anthropic", anthropic_api_key="")
    with pytest.raises(ValueError, match="MAILPILOT_ANTHROPIC_API_KEY") as exc_info:
        require_active_provider_key(settings)
    assert "mailpilot config set" not in str(exc_info.value)


def test_require_active_provider_key_accepts_present_key() -> None:
    """A non-empty active-provider key passes preflight."""
    require_active_provider_key(make_test_settings(xai_api_key="xai-test"))
    require_active_provider_key(
        make_test_settings(llm_provider="anthropic", anthropic_api_key="sk-test")
    )


def test_run_aborts_before_drain_when_xai_key_empty(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.47 / §B.141: missing key + due T1 batch → abort, zero tasks failed."""
    task_ids = _seed_due_t1_batch(database_connection, count=3)
    settings = make_test_settings(llm_provider="xai", xai_api_key="")
    runner = CliRunner()

    with (
        patch("mailpilot.settings.get_settings", return_value=settings),
        patch("mailpilot.database.initialize_database") as mock_init,
        patch("mailpilot.sync.start_sync_loop") as mock_loop,
    ):
        result = runner.invoke(main, ["run"])

    assert result.exit_code == 1, result.output
    data = json.loads(result.output)
    assert data["ok"] is False
    assert "MAILPILOT_XAI_API_KEY" in data["message"]
    assert "mailpilot config set" not in data["message"]
    mock_init.assert_not_called()
    mock_loop.assert_not_called()

    for task_id in task_ids:
        task = get_task(database_connection, task_id)
        assert task is not None
        assert task.status == "pending"
    assert list_tasks(database_connection, status="failed") == []
