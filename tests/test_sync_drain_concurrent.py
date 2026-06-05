"""Concurrent task-drain behavior per §V.23 / §V.24 / §T.61 / §T.67.

Verifies the bounded ``ThreadPoolExecutor`` drain:

- Per §V.23: tasks overlap up to ``settings.max_concurrent_tasks``, each
  worker opens its own connection, and a poisoned worker does not abort
  siblings.
- Per §V.24: ``_drain_pending_tasks`` submits-and-returns -- the main
  loop never blocks on completion; ``task.drain`` events are emitted by
  ``_reap_completed_tasks`` once futures finish.
"""

from __future__ import annotations

import concurrent.futures
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


def _wait_for_in_flight(
    in_flight: dict[concurrent.futures.Future[None], tuple[str, float]],
    timeout: float = 5.0,
) -> None:
    """Block test thread until every queued future has completed."""
    deadline = time.monotonic() + timeout
    while any(not future.done() for future in list(in_flight)):
        if time.monotonic() > deadline:
            raise AssertionError("futures did not complete within timeout")
        time.sleep(0.01)


def test_drain_runs_tasks_concurrently_when_pool_has_room(
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per §V.23: two pending tasks with ``max_concurrent_tasks=4`` overlap."""
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
    in_flight: dict[concurrent.futures.Future[None], tuple[str, float]] = {}
    with (
        concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool,
        patch("mailpilot.sync.psycopg.connect", return_value=fake_conn),
    ):
        _drain_pending_tasks(
            database_connection,
            make_test_settings(max_concurrent_tasks=4),
            pool,
            in_flight,
        )
        _wait_for_in_flight(in_flight)

    starts = {tid: t for tid, kind, t in timings if kind == "start"}
    ends = {tid: t for tid, kind, t in timings if kind == "end"}
    assert max(starts.values()) < min(ends.values()), (
        f"expected overlap: starts={starts} ends={ends}"
    )


def test_drain_serializes_when_pool_size_is_one(
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per §V.23 regression-guard: ``max_concurrent_tasks=1`` preserves strict order."""
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
    in_flight: dict[concurrent.futures.Future[None], tuple[str, float]] = {}
    with (
        concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool,
        patch("mailpilot.sync.psycopg.connect", return_value=fake_conn),
    ):
        _drain_pending_tasks(
            database_connection,
            make_test_settings(max_concurrent_tasks=1),
            pool,
            in_flight,
        )
        _wait_for_in_flight(in_flight)

    # With one worker, the second task must not start until the first ended.
    starts = {tid: t for tid, kind, t in timings if kind == "start"}
    ends = {tid: t for tid, kind, t in timings if kind == "end"}
    later_start = max(starts.values())
    earlier_end = min(ends.values())
    assert later_start >= earlier_end, (
        f"expected serialization: starts={starts} ends={ends}"
    )


def test_drain_dispatcher_returns_without_blocking(
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per §V.24: ``_drain_pending_tasks`` must return immediately after submit.

    Long-running workers must not delay the main loop -- the dispatcher
    submits and returns, leaving completion to ``_reap_completed_tasks``.
    """
    from mailpilot.sync import (
        _drain_pending_tasks,  # pyright: ignore[reportPrivateUsage]
    )

    pending = [_make_fake_task(f"t{i}") for i in range(3)]
    monkeypatch.setattr("mailpilot.sync.list_pending_tasks", lambda *_a, **_k: pending)

    # Long-running worker stand-in -- if dispatch blocked on completion,
    # the total call would be at least ``hold_seconds``.
    hold_seconds = 0.5

    def _long_execute(_conn: object, _settings: object, _task: object) -> None:
        time.sleep(hold_seconds)

    import mailpilot.run as run_module

    monkeypatch.setattr(run_module, "execute_task", _long_execute)

    fake_conn = MagicMock()
    in_flight: dict[concurrent.futures.Future[None], tuple[str, float]] = {}
    with (
        concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool,
        patch("mailpilot.sync.psycopg.connect", return_value=fake_conn),
    ):
        start = time.monotonic()
        _drain_pending_tasks(
            database_connection,
            make_test_settings(max_concurrent_tasks=4),
            pool,
            in_flight,
        )
        dispatch_seconds = time.monotonic() - start

        # All three futures should still be running at this point.
        assert len(in_flight) == 3
        assert all(not future.done() for future in in_flight)

        # Dispatch must be far below ``hold_seconds`` -- the main loop is
        # free to continue with sync.account.run while workers churn.
        assert dispatch_seconds < hold_seconds / 2, (
            f"dispatcher blocked for {dispatch_seconds:.3f}s "
            f"(hold_seconds={hold_seconds})"
        )

        _wait_for_in_flight(in_flight)


def test_reap_emits_task_drain_after_futures_complete(
    capsys: pytest.CaptureFixture[str],
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per §V.24: ``task.drain`` operator event fires from the reaper.

    Five tasks dispatched, all complete, reaper emits one aggregated event.
    """
    from mailpilot.sync import (
        _drain_pending_tasks,  # pyright: ignore[reportPrivateUsage]
        _reap_completed_tasks,  # pyright: ignore[reportPrivateUsage]
    )

    pending = [_make_fake_task(f"t{i}") for i in range(5)]
    monkeypatch.setattr("mailpilot.sync.list_pending_tasks", lambda *_a, **_k: pending)

    import mailpilot.run as run_module

    monkeypatch.setattr(run_module, "execute_task", lambda *_a, **_k: None)

    fake_conn = MagicMock()
    in_flight: dict[concurrent.futures.Future[None], tuple[str, float]] = {}
    with (
        concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool,
        patch("mailpilot.sync.psycopg.connect", return_value=fake_conn),
    ):
        _drain_pending_tasks(
            database_connection,
            make_test_settings(max_concurrent_tasks=4),
            pool,
            in_flight,
        )
        # No drain event yet -- futures are still running and reaper hasn't run.
        immediately = capsys.readouterr().err
        assert "event=task.drain" not in immediately

        _wait_for_in_flight(in_flight)
        _reap_completed_tasks(in_flight)

    out = capsys.readouterr().err
    assert "event=task.drain" in out
    assert "drained=5" in out
    assert "duration_ms=" in out
    assert not in_flight, "reaper should remove completed futures"


def test_reap_is_noop_when_no_futures_in_flight(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Per §V.24: reaper called on empty ``in_flight`` emits no event."""
    from mailpilot.sync import (
        _reap_completed_tasks,  # pyright: ignore[reportPrivateUsage]
    )

    in_flight: dict[concurrent.futures.Future[None], tuple[str, float]] = {}
    _reap_completed_tasks(in_flight)

    out = capsys.readouterr().err
    assert "event=task.drain" not in out


def test_reap_skips_futures_still_running(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Per §V.24: reaper non-blocking -- in-progress futures stay in ``in_flight``."""
    from mailpilot.sync import (
        _reap_completed_tasks,  # pyright: ignore[reportPrivateUsage]
    )

    release = threading.Event()

    def _block_until_released() -> None:
        release.wait()

    in_flight: dict[concurrent.futures.Future[None], tuple[str, float]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(_block_until_released)
        in_flight[future] = ("blocker", time.monotonic())
        _reap_completed_tasks(in_flight)
        # Future still running -- reaper left it in place, no event emitted.
        assert future in in_flight
        assert (
            capsys.readouterr().err == ""
            or "event=task.drain" not in capsys.readouterr().err
        )

        release.set()
        future.result(timeout=2.0)
        _reap_completed_tasks(in_flight)
        assert not in_flight


def test_each_worker_opens_its_own_connection(
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per §V.23: workers MUST NOT share a ``psycopg.Connection`` (not thread-safe)."""
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
    in_flight: dict[concurrent.futures.Future[None], tuple[str, float]] = {}
    with (
        concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool,
        patch("mailpilot.sync.psycopg.connect", side_effect=fake_conns) as mock_connect,
    ):
        _drain_pending_tasks(
            database_connection,
            make_test_settings(max_concurrent_tasks=4),
            pool,
            in_flight,
        )
        _wait_for_in_flight(in_flight)

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
    """Per §V.23 + §V.51: a poisoned worker logs ``error`` and does not abort siblings."""
    from mailpilot.sync import (
        _drain_pending_tasks,  # pyright: ignore[reportPrivateUsage]
        _reap_completed_tasks,  # pyright: ignore[reportPrivateUsage]
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
    in_flight: dict[concurrent.futures.Future[None], tuple[str, float]] = {}
    with (
        concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool,
        patch("mailpilot.sync.psycopg.connect", return_value=fake_conn),
    ):
        _drain_pending_tasks(
            database_connection,
            make_test_settings(max_concurrent_tasks=4),
            pool,
            in_flight,
        )
        _wait_for_in_flight(in_flight)
        _reap_completed_tasks(in_flight)

    assert sorted(completed) == ["good-1", "good-2"]
    err = capsys.readouterr().err
    assert "event=error" in err
    assert "source=sync.drain.task_failed" in err
    assert "event=task.drain" in err
    assert "drained=3" in err


def test_drain_skips_task_already_in_flight_until_reaped(
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per §V.23 atomic-claim clause: re-tick on same pending row ⊥ re-dispatch.

    Regression for §B.50: ``list_pending_tasks`` keeps returning the row
    while the worker is mid-``agent.invoke``; without an in-process claim
    set the dispatcher resubmits, producing one ``run.execute_task`` span
    per tick on a single task. The fix excludes any task whose id is
    already present in ``in_flight`` from the next dispatch and only
    re-admits it after ``_reap_completed_tasks`` clears the entry.
    """
    from mailpilot.sync import (
        _drain_pending_tasks,  # pyright: ignore[reportPrivateUsage]
        _reap_completed_tasks,  # pyright: ignore[reportPrivateUsage]
    )

    # Single pending row; ``list_pending_tasks`` keeps returning it across
    # ticks until the worker writes a terminal status.
    only_pending = [_make_fake_task("solo")]
    monkeypatch.setattr(
        "mailpilot.sync.list_pending_tasks", lambda *_a, **_k: only_pending
    )

    release = threading.Event()
    invocations: list[str] = []
    invocations_lock = threading.Lock()

    def _block_until_released(
        _conn: object, _settings: object, task: MagicMock
    ) -> None:
        with invocations_lock:
            invocations.append(task.id)
        release.wait(timeout=5.0)

    import mailpilot.run as run_module

    monkeypatch.setattr(run_module, "execute_task", _block_until_released)

    fake_conn = MagicMock()
    in_flight: dict[concurrent.futures.Future[None], tuple[str, float]] = {}
    settings = make_test_settings(max_concurrent_tasks=4)
    try:
        with (
            concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool,
            patch("mailpilot.sync.psycopg.connect", return_value=fake_conn),
        ):
            # Tick 1: row is unclaimed; dispatcher submits exactly one future.
            _drain_pending_tasks(database_connection, settings, pool, in_flight)
            assert len(in_flight) == 1
            (claimed_task_id, _submit_time) = next(iter(in_flight.values()))
            assert claimed_task_id == "solo"

            # Wait until the worker is actually running so the next tick's
            # SELECT-equivalent reflects an in-progress worker.
            deadline = time.monotonic() + 2.0
            while not invocations and time.monotonic() < deadline:
                time.sleep(0.01)
            assert invocations == ["solo"], "worker must be in flight before tick 2"

            # Tick 2: same row still pending, claim set excludes it -- the
            # dispatcher MUST NOT submit a second future, and reap MUST NOT
            # surface ``task.drain`` because nothing finished.
            _reap_completed_tasks(in_flight)
            _drain_pending_tasks(database_connection, settings, pool, in_flight)
            assert len(in_flight) == 1, (
                "atomic claim must prevent re-dispatch while worker is in flight"
            )
            assert invocations == ["solo"], (
                "worker count must stay at 1 across the re-tick"
            )

            # Release the worker, wait for completion, reap clears the claim.
            release.set()
            _wait_for_in_flight(in_flight)
            _reap_completed_tasks(in_flight)
            assert not in_flight

            # Tick 3: row is unclaimed again; manual_retry-style re-runs land.
            _drain_pending_tasks(database_connection, settings, pool, in_flight)
            assert len(in_flight) == 1, (
                "post-reap dispatch must be allowed (e.g. retry reschedule)"
            )

            release.set()  # idempotent; worker already returned for the re-dispatch
            _wait_for_in_flight(in_flight)
    finally:
        # Defensive release in case any assertion failure left workers blocked.
        release.set()
