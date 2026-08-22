"""§V.47 / §B.141: skip drain when the active-provider key is missing."""

from __future__ import annotations

import concurrent.futures
import queue
import threading
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


def test_require_active_provider_key_xai_names_config_set() -> None:
    """Missing xAI key names ``mailpilot config set xai_api_key``."""
    settings = make_test_settings(llm_provider="xai", xai_api_key="")
    with pytest.raises(
        ValueError, match="mailpilot config set xai_api_key"
    ) as exc_info:
        require_active_provider_key(settings)
    assert "MAILPILOT_XAI_API_KEY" not in str(exc_info.value)


def test_require_active_provider_key_anthropic_names_config_set() -> None:
    """Missing Anthropic key names ``mailpilot config set anthropic_api_key``."""
    settings = make_test_settings(llm_provider="anthropic", anthropic_api_key="")
    with pytest.raises(
        ValueError, match="mailpilot config set anthropic_api_key"
    ) as exc_info:
        require_active_provider_key(settings)
    assert "MAILPILOT_ANTHROPIC_API_KEY" not in str(exc_info.value)


def test_require_active_provider_key_accepts_present_key() -> None:
    """A non-empty active-provider key passes preflight."""
    require_active_provider_key(make_test_settings(xai_api_key="xai-test"))
    require_active_provider_key(
        make_test_settings(llm_provider="anthropic", anthropic_api_key="sk-test")
    )


def test_run_starts_loop_when_xai_key_empty(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.47: missing key does not abort ``run``; the loop starts and stays up."""
    task_ids = _seed_due_t1_batch(database_connection, count=3)
    settings = make_test_settings(llm_provider="xai", xai_api_key="")
    runner = CliRunner()

    with (
        patch("mailpilot.settings.get_settings", return_value=settings),
        patch("mailpilot.database.initialize_database"),
        patch("mailpilot.sync.start_sync_loop") as mock_loop,
    ):
        result = runner.invoke(main, ["run"])

    assert result.exit_code == 0, result.output
    mock_loop.assert_called_once()

    for task_id in task_ids:
        task = get_task(database_connection, task_id)
        assert task is not None
        assert task.status == "pending"
    assert list_tasks(database_connection, status="failed") == []


def test_iteration_skips_drain_when_key_empty(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.47: missing key + due T1 → skip drain, zero tasks claimed or failed."""
    from mailpilot.sync import (
        _run_periodic_iteration,  # pyright: ignore[reportPrivateUsage]
    )

    task_ids = _seed_due_t1_batch(database_connection, count=3)
    settings = make_test_settings(llm_provider="xai", xai_api_key="")
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        with (
            patch("mailpilot.sync._drain_sync_queue"),
            patch("mailpilot.sync._sync_all_accounts"),
            patch("mailpilot.sync._drain_pending_tasks") as mock_drain,
        ):
            _run_periodic_iteration(
                database_connection,
                settings,
                queue.Queue(),
                "timer",
                do_full_sweep=False,
                pool=pool,
                in_flight={},
                wakeup_event=threading.Event(),
            )
    finally:
        pool.shutdown(wait=True)

    mock_drain.assert_not_called()
    for task_id in task_ids:
        task = get_task(database_connection, task_id)
        assert task is not None
        assert task.status == "pending"
    assert list_tasks(database_connection, status="failed") == []
