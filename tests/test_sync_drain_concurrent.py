"""Concurrent task-drain behavior per §V.62 / §T.61.

Verifies the bounded ``ThreadPoolExecutor`` drain replaces the prior
sequential loop: tasks overlap up to ``settings.max_concurrent_tasks``,
each worker opens its own connection, and a poisoned worker does not
abort siblings.
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from conftest import make_test_settings


def _make_fake_task(task_id: str) -> MagicMock:
    task = MagicMock()
    task.id = task_id
    return task


def test_drain_runs_tasks_concurrently_when_pool_has_room(
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per §V.62: two pending tasks with ``max_concurrent_tasks=4`` overlap."""
    from mailpilot.sync import (
        _drain_pending_tasks,  # pyright: ignore[reportPrivateUsage]
    )

    pending = [_make_fake_task("t1"), _make_fake_task("t2")]
    monkeypatch.setattr("mailpilot.sync.list_pending_tasks", lambda *_a, **_k: pending)

    timings: list[tuple[str, str, float]] = []
    timings_lock = threading.Lock()

    def _slow_execute(_conn: object, _settings: object, task: MagicMock) -> None:
        with timings_lock:
            timings.append((task.id, "start", time.monotonic()))
        time.sleep(0.2)
        with timings_lock:
            timings.append((task.id, "end", time.monotonic()))

    import mailpilot.run as run_module

    monkeypatch.setattr(run_module, "execute_task", _slow_execute)

    fake_conn = MagicMock()
    with patch("mailpilot.sync.psycopg.connect", return_value=fake_conn):
        _drain_pending_tasks(
            database_connection, make_test_settings(max_concurrent_tasks=4)
        )

    starts = {tid: t for tid, kind, t in timings if kind == "start"}
    ends = {tid: t for tid, kind, t in timings if kind == "end"}
    assert max(starts.values()) < min(ends.values()), (
        f"expected overlap: starts={starts} ends={ends}"
    )


def test_drain_serializes_when_pool_size_is_one(
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per §V.62 regression-guard: ``max_concurrent_tasks=1`` preserves strict order."""
    from mailpilot.sync import (
        _drain_pending_tasks,  # pyright: ignore[reportPrivateUsage]
    )

    pending = [_make_fake_task("t1"), _make_fake_task("t2")]
    monkeypatch.setattr("mailpilot.sync.list_pending_tasks", lambda *_a, **_k: pending)

    timings: list[tuple[str, str, float]] = []

    def _slow_execute(_conn: object, _settings: object, task: MagicMock) -> None:
        timings.append((task.id, "start", time.monotonic()))
        time.sleep(0.05)
        timings.append((task.id, "end", time.monotonic()))

    import mailpilot.run as run_module

    monkeypatch.setattr(run_module, "execute_task", _slow_execute)

    fake_conn = MagicMock()
    with patch("mailpilot.sync.psycopg.connect", return_value=fake_conn):
        _drain_pending_tasks(
            database_connection, make_test_settings(max_concurrent_tasks=1)
        )

    # With one worker, the second task must not start until the first ended.
    starts = {tid: t for tid, kind, t in timings if kind == "start"}
    ends = {tid: t for tid, kind, t in timings if kind == "end"}
    later_start = max(starts.values())
    earlier_end = min(ends.values())
    assert later_start >= earlier_end, (
        f"expected serialization: starts={starts} ends={ends}"
    )


def test_drain_aggregates_event_across_pool(
    capsys: pytest.CaptureFixture[str],
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per §V.62: 5 tasks under max=4 → ``task.drain`` aggregates ``drained=5``."""
    from mailpilot.sync import (
        _drain_pending_tasks,  # pyright: ignore[reportPrivateUsage]
    )

    pending = [_make_fake_task(f"t{i}") for i in range(5)]
    monkeypatch.setattr("mailpilot.sync.list_pending_tasks", lambda *_a, **_k: pending)

    import mailpilot.run as run_module

    monkeypatch.setattr(run_module, "execute_task", lambda *_a, **_k: None)

    fake_conn = MagicMock()
    with patch("mailpilot.sync.psycopg.connect", return_value=fake_conn):
        _drain_pending_tasks(
            database_connection, make_test_settings(max_concurrent_tasks=4)
        )

    out = capsys.readouterr().err
    assert "event=task.drain" in out
    assert "drained=5" in out
    assert "duration_ms=" in out


def test_each_worker_opens_its_own_connection(
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per §V.62: workers MUST NOT share a ``psycopg.Connection`` (not thread-safe)."""
    from mailpilot.sync import (
        _drain_pending_tasks,  # pyright: ignore[reportPrivateUsage]
    )

    pending = [_make_fake_task(f"t{i}") for i in range(3)]
    monkeypatch.setattr("mailpilot.sync.list_pending_tasks", lambda *_a, **_k: pending)

    seen_connections: list[object] = []

    def _record_conn(conn: object, _settings: object, _task: object) -> None:
        seen_connections.append(conn)

    import mailpilot.run as run_module

    monkeypatch.setattr(run_module, "execute_task", _record_conn)

    fake_conns = [MagicMock(name=f"conn-{i}") for i in range(3)]
    with patch(
        "mailpilot.sync.psycopg.connect", side_effect=fake_conns
    ) as mock_connect:
        _drain_pending_tasks(
            database_connection, make_test_settings(max_concurrent_tasks=4)
        )

    assert mock_connect.call_count == 3
    # Distinct connection objects -- not the outer ``database_connection`` --
    # were threaded into each worker's ``execute_task`` call.
    assert len(seen_connections) == 3
    assert all(c is not database_connection for c in seen_connections)
    assert len({id(c) for c in seen_connections}) == 3
    # All worker-local connections are closed when the pool drains.
    for conn in fake_conns:
        conn.close.assert_called_once()


def test_drain_continues_when_one_worker_raises(
    capsys: pytest.CaptureFixture[str],
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per §V.62 + §V.19: a poisoned worker logs ``error`` and does not abort siblings."""
    from mailpilot.sync import (
        _drain_pending_tasks,  # pyright: ignore[reportPrivateUsage]
    )

    pending = [
        _make_fake_task("good-1"),
        _make_fake_task("boom"),
        _make_fake_task("good-2"),
    ]
    monkeypatch.setattr("mailpilot.sync.list_pending_tasks", lambda *_a, **_k: pending)

    completed: list[str] = []
    completed_lock = threading.Lock()

    def _maybe_explode(_conn: object, _settings: object, task: MagicMock) -> None:
        if task.id == "boom":
            raise RuntimeError("worker exploded")
        with completed_lock:
            completed.append(task.id)

    import mailpilot.run as run_module

    monkeypatch.setattr(run_module, "execute_task", _maybe_explode)

    fake_conn = MagicMock()
    with patch("mailpilot.sync.psycopg.connect", return_value=fake_conn):
        _drain_pending_tasks(
            database_connection, make_test_settings(max_concurrent_tasks=4)
        )

    assert sorted(completed) == ["good-1", "good-2"]
    err = capsys.readouterr().err
    assert "event=error" in err
    assert "source=sync.drain.task_failed" in err
    assert "event=task.drain" in err
    assert "drained=3" in err
