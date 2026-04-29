"""Operator-log emissions from the sync-loop pipeline."""

from __future__ import annotations

import itertools
import os
import queue
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest

import mailpilot.sync as sync_module
from conftest import make_test_settings


def _reset_iteration_counter() -> None:
    """Reset the module-level counter so iteration=N assertions are deterministic."""
    sync_module._iteration_counter = itertools.count(1)  # pyright: ignore[reportPrivateUsage]


@pytest.fixture(autouse=True)
def _reset_counter() -> None:  # pyright: ignore[reportUnusedFunction]
    _reset_iteration_counter()


def test_run_periodic_iteration_emits_loop_tick(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.sync import (
        _run_periodic_iteration,  # pyright: ignore[reportPrivateUsage]
    )

    monkeypatch.setattr("mailpilot.sync._drain_sync_queue", lambda *_a, **_k: None)
    monkeypatch.setattr("mailpilot.sync._sync_all_accounts", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "mailpilot.sync.create_tasks_for_routed_emails", lambda *_a, **_k: 0
    )
    monkeypatch.setattr("mailpilot.sync._drain_pending_tasks", lambda *_a, **_k: None)

    _run_periodic_iteration(
        database_connection,
        make_test_settings(),
        queue.Queue(),
        "event",
        do_full_sweep=True,
    )

    out = capsys.readouterr().out
    assert "event=loop.tick" in out
    assert "iteration=1" in out
    assert "wakeup=event" in out
    assert "full_sweep=True" in out


def test_run_periodic_iteration_increments_iteration_counter(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.sync import (
        _run_periodic_iteration,  # pyright: ignore[reportPrivateUsage]
    )

    monkeypatch.setattr("mailpilot.sync._drain_sync_queue", lambda *_a, **_k: None)
    monkeypatch.setattr("mailpilot.sync._sync_all_accounts", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "mailpilot.sync.create_tasks_for_routed_emails", lambda *_a, **_k: 0
    )
    monkeypatch.setattr("mailpilot.sync._drain_pending_tasks", lambda *_a, **_k: None)

    _run_periodic_iteration(
        database_connection,
        make_test_settings(),
        queue.Queue(),
        "timer",
        do_full_sweep=False,
    )
    _run_periodic_iteration(
        database_connection,
        make_test_settings(),
        queue.Queue(),
        "timer",
        do_full_sweep=False,
    )

    out = capsys.readouterr().out
    assert "iteration=1" in out
    assert "iteration=2" in out


def test_drain_sync_queue_emits_pubsub_notify(
    capsys: pytest.CaptureFixture[str],
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_drain_sync_queue emits one event=pubsub.notify per known account."""
    from conftest import make_test_account
    from mailpilot.sync import (
        _drain_sync_queue,  # pyright: ignore[reportPrivateUsage]
    )

    account = make_test_account(database_connection, email="notify@example.com")

    monkeypatch.setattr("mailpilot.sync.GmailClient", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr("mailpilot.sync.sync_account", lambda *_a, **_k: 0)

    sync_queue: queue.Queue[str] = queue.Queue()
    sync_queue.put(account.email)

    _drain_sync_queue(database_connection, make_test_settings(), sync_queue, set())

    out = capsys.readouterr().out
    assert "event=pubsub.notify" in out
    assert f"email={account.email}" in out


def test_sync_account_emits_sync_account_event(
    capsys: pytest.CaptureFixture[str],
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """sync_account emits one event=sync.account line on completion."""
    from conftest import make_test_account
    from mailpilot.sync import sync_account
    from test_sync import (
        _make_gmail_message,  # pyright: ignore[reportPrivateUsage]
        _make_mock_client,  # pyright: ignore[reportPrivateUsage]
        _set_get_messages,  # pyright: ignore[reportPrivateUsage]
        _set_list_messages,  # pyright: ignore[reportPrivateUsage]
    )

    account = make_test_account(database_connection, email="op@example.com")
    client, service = _make_mock_client(account.email)
    _set_list_messages(service, [{"id": "msg-1", "threadId": "thr-1"}])
    _set_get_messages(service, [_make_gmail_message("msg-1", "thr-1")])

    stored = sync_account(database_connection, account, client, make_test_settings())

    assert stored == 1
    out = capsys.readouterr().out
    assert "event=sync.account" in out
    assert "email=op@example.com" in out
    assert "new=1" in out
    assert "duplicates=0" in out
    assert "duration_ms=" in out


def test_drain_pending_tasks_emits_task_drain_when_tasks_executed(
    capsys: pytest.CaptureFixture[str],
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mailpilot.sync import (
        _drain_pending_tasks,  # pyright: ignore[reportPrivateUsage]
    )

    fake_tasks = [MagicMock(), MagicMock(), MagicMock()]
    monkeypatch.setattr(
        "mailpilot.sync.list_pending_tasks", lambda *_a, **_k: fake_tasks
    )
    import mailpilot.run as run_module

    monkeypatch.setattr(run_module, "execute_task", lambda *_a, **_k: None)

    _drain_pending_tasks(database_connection, make_test_settings())

    out = capsys.readouterr().out
    assert "event=task.drain" in out
    assert "drained=3" in out
    assert "duration_ms=" in out


def test_drain_sync_queue_emits_error_on_sync_failure(
    capsys: pytest.CaptureFixture[str],
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """logfire.exception sites mirror to operator event=error lines."""
    from conftest import make_test_account
    from mailpilot.sync import (
        _drain_sync_queue,  # pyright: ignore[reportPrivateUsage]
    )

    account = make_test_account(database_connection, email="boom@example.com")
    monkeypatch.setattr("mailpilot.sync.GmailClient", lambda *_a, **_k: MagicMock())

    def _explode(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("Gmail timeout 504")

    monkeypatch.setattr("mailpilot.sync.sync_account", _explode)

    sync_queue: queue.Queue[str] = queue.Queue()
    sync_queue.put(account.email)

    _drain_sync_queue(database_connection, make_test_settings(), sync_queue, set())

    out = capsys.readouterr().out
    assert "event=error" in out
    assert "source=sync.notification.sync_failed" in out
    assert 'message="Gmail timeout 504"' in out


def test_drain_pending_tasks_skips_event_when_no_tasks(
    capsys: pytest.CaptureFixture[str],
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mailpilot.sync import (
        _drain_pending_tasks,  # pyright: ignore[reportPrivateUsage]
    )

    monkeypatch.setattr("mailpilot.sync.list_pending_tasks", lambda *_a, **_k: [])

    _drain_pending_tasks(database_connection, make_test_settings())

    out = capsys.readouterr().out
    assert "event=task.drain" not in out


def test_start_sync_loop_emits_loop_start_and_loop_stop(
    capsys: pytest.CaptureFixture[str],
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.sync import start_sync_loop

    settings = make_test_settings()
    with (
        patch("mailpilot.sync.threading.Event") as mock_event_cls,
        patch("mailpilot.sync._start_task_listener"),
        patch("mailpilot.sync._start_pubsub_logging_errors", return_value=None),
        patch("mailpilot.sync._run_periodic_iteration"),
        patch("mailpilot.sync.signal.signal"),
    ):
        mock_shutdown = MagicMock()
        mock_shutdown.is_set.side_effect = [False, True]
        mock_wakeup = MagicMock()
        mock_wakeup.wait.return_value = False
        mock_event_cls.side_effect = [mock_shutdown, mock_wakeup]

        start_sync_loop(database_connection, settings)

    out = capsys.readouterr().out
    assert "event=loop.start" in out
    assert f"pid={os.getpid()}" in out
    assert f"interval={settings.run_interval}" in out
    assert "event=loop.stop" in out
