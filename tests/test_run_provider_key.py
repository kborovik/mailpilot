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
from mailpilot.database import (
    activate_workflow,
    create_task,
    get_task,
    list_tasks,
    update_workflow,
)
from mailpilot.settings import require_active_provider_key


def _seed_due_t1_batch(
    connection: psycopg.Connection[dict[str, Any]], *, count: int = 3
) -> list[str]:
    """Insert ``count`` due first-reach tasks and return their ids."""
    account = make_test_account(connection)
    workflow = make_test_workflow(connection, account_id=account.id)
    update_workflow(
        connection, workflow.id, goal="test goal", instructions="test instructions"
    )
    activate_workflow(connection, workflow.id)
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


def _wait_for_in_flight(
    in_flight: dict[concurrent.futures.Future[None], tuple[str, float]],
    timeout: float = 5.0,
) -> None:
    """Block until every queued drain future has completed."""
    import time

    deadline = time.monotonic() + timeout
    while any(not future.done() for future in list(in_flight)):
        if time.monotonic() > deadline:
            raise AssertionError("futures did not complete within timeout")
        time.sleep(0.01)


def _xai_incorrect_api_key() -> Exception:
    """xAI present-but-wrong key as raised by pydantic-ai (§B.152)."""
    from pydantic_ai.exceptions import ModelAPIError

    return ModelAPIError(
        "grok-4.5",
        "Incorrect API key provided. You can obtain an API key from https://console.x.ai.",
    )


def test_drain_invalid_xai_key_skips_remaining_zero_failed(
    capsys: pytest.CaptureFixture[str],
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.47 / §B.152: due T1 batch + injected invalid-key → zero failed.

    First model call detects the present-but-wrong key. In-flight stays
    pending (no attempt_count bump). Remaining due tasks that tick are
    not executed. Operator stderr is one error line naming config set.
    """
    from mailpilot.sync import (
        _drain_pending_tasks,  # pyright: ignore[reportPrivateUsage]
        _reap_completed_tasks,  # pyright: ignore[reportPrivateUsage]
    )

    task_ids = _seed_due_t1_batch(database_connection, count=3)
    settings = make_test_settings(
        llm_provider="xai", xai_api_key="xai-wrong", max_concurrent_tasks=1
    )
    in_flight: dict[concurrent.futures.Future[None], tuple[str, float]] = {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        with (
            patch(
                "mailpilot.run.invoke_workflow_agent",
                side_effect=_xai_incorrect_api_key(),
            ) as mock_invoke,
            patch("mailpilot.run.logfire.exception") as mock_exception,
        ):
            _drain_pending_tasks(database_connection, settings, pool, in_flight)
            _wait_for_in_flight(in_flight)
            _reap_completed_tasks(in_flight)
    finally:
        pool.shutdown(wait=True)

    mock_exception.assert_not_called()
    assert mock_invoke.call_count == 1
    for task_id in task_ids:
        task = get_task(database_connection, task_id)
        assert task is not None
        assert task.status == "pending"
        assert task.attempt_count == 0
    assert list_tasks(database_connection, status="failed") == []
    err = capsys.readouterr().err
    assert err.count("event=error") == 1
    assert "mailpilot config set xai_api_key" in err
    assert "Traceback" not in err


def test_drain_anthropic_401_skips_remaining_zero_failed(
    capsys: pytest.CaptureFixture[str],
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.47: Anthropic 401 is the same class; remaining due stay pending."""
    from unittest.mock import MagicMock

    from anthropic import APIStatusError

    from mailpilot.sync import (
        _drain_pending_tasks,  # pyright: ignore[reportPrivateUsage]
        _reap_completed_tasks,  # pyright: ignore[reportPrivateUsage]
    )

    task_ids = _seed_due_t1_batch(database_connection, count=3)
    settings = make_test_settings(
        llm_provider="anthropic",
        anthropic_api_key="sk-wrong",
        max_concurrent_tasks=1,
    )
    response = MagicMock()
    response.status_code = 401
    response.headers = {}
    err_401 = APIStatusError(
        "invalid x-api-key",
        response=response,
        body={"error": {"type": "authentication_error"}},
    )
    in_flight: dict[concurrent.futures.Future[None], tuple[str, float]] = {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        with patch("mailpilot.run.invoke_workflow_agent", side_effect=err_401):
            _drain_pending_tasks(database_connection, settings, pool, in_flight)
            _wait_for_in_flight(in_flight)
            _reap_completed_tasks(in_flight)
    finally:
        pool.shutdown(wait=True)

    for task_id in task_ids:
        task = get_task(database_connection, task_id)
        assert task is not None
        assert task.status == "pending"
        assert task.attempt_count == 0
    assert list_tasks(database_connection, status="failed") == []
    err = capsys.readouterr().err
    assert "mailpilot config set anthropic_api_key" in err
    assert "Traceback" not in err
