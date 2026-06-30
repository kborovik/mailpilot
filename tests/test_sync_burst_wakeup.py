"""Burst-wakeup contract per §V.69 / §T.83.

After ``_run_periodic_iteration`` classifies one or more new inbound emails
into tasks, the sync loop MUST immediately set ``wakeup_event`` and force a
full sweep on the next tick -- collapsing the inter-wave gap that Gmail
Pub/Sub batching otherwise stretches into the burst SLA budget.

Tests:

- unit: iteration returns False + leaves wakeup clear when no new tasks land.
- unit: iteration returns True + sets wakeup when at least one task lands.
- integration: 10 routed inbound emails (P=5 concurrent inserts) flow through
  the iteration; ``task.created_at - email.received_at`` stays under the
  75s burst T_delivery ceiling and the wakeup is set on the first tick.
- loop: ``start_sync_loop`` carries the force flag across iterations so two
  consecutive ticks run a full sweep even when ``run_interval`` hasn't
  elapsed between them.
"""

from __future__ import annotations

import concurrent.futures
import queue
import threading
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from psycopg.rows import dict_row

from conftest import (
    make_test_account,
    make_test_settings,
    make_test_workflow,
)
from mailpilot.database import list_pending_tasks
from mailpilot.sync import (
    _run_periodic_iteration,  # pyright: ignore[reportPrivateUsage]
    start_sync_loop,
)


def _empty_pool_and_inflight() -> tuple[
    concurrent.futures.ThreadPoolExecutor,
    dict[concurrent.futures.Future[None], tuple[str, float]],
]:
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    return pool, {}


def test_iteration_returns_false_and_leaves_wakeup_clear_on_quiet_tick(
    monkeypatch: pytest.MonkeyPatch,
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Per §V.69: no new inbound tasks -> no burst-wakeup, no forced sweep."""
    monkeypatch.setattr("mailpilot.sync._drain_sync_queue", lambda *_a, **_k: None)
    monkeypatch.setattr("mailpilot.sync._sync_all_accounts", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "mailpilot.sync.create_tasks_for_routed_emails", lambda *_a, **_k: []
    )
    monkeypatch.setattr("mailpilot.sync._drain_pending_tasks", lambda *_a, **_k: None)

    wakeup = threading.Event()
    pool, in_flight = _empty_pool_and_inflight()
    try:
        result = _run_periodic_iteration(
            database_connection,
            make_test_settings(),
            queue.Queue(),
            "event",
            do_full_sweep=True,
            pool=pool,
            in_flight=in_flight,
            wakeup_event=wakeup,
        )
    finally:
        pool.shutdown(wait=True)

    assert result is False
    assert wakeup.is_set() is False


def test_iteration_returns_true_and_sets_wakeup_when_new_tasks_land(
    monkeypatch: pytest.MonkeyPatch,
    database_connection: psycopg.Connection[dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Per §V.69: one-or-more inbound tasks -> wakeup set + force_full_sweep_next True."""
    monkeypatch.setattr("mailpilot.sync._drain_sync_queue", lambda *_a, **_k: None)
    monkeypatch.setattr("mailpilot.sync._sync_all_accounts", lambda *_a, **_k: None)

    fake_tasks = [MagicMock(), MagicMock(), MagicMock()]
    monkeypatch.setattr(
        "mailpilot.sync.create_tasks_for_routed_emails",
        lambda *_a, **_k: fake_tasks,
    )
    monkeypatch.setattr("mailpilot.sync._drain_pending_tasks", lambda *_a, **_k: None)

    wakeup = threading.Event()
    pool, in_flight = _empty_pool_and_inflight()
    try:
        result = _run_periodic_iteration(
            database_connection,
            make_test_settings(),
            queue.Queue(),
            "event",
            do_full_sweep=False,
            pool=pool,
            in_flight=in_flight,
            wakeup_event=wakeup,
        )
    finally:
        pool.shutdown(wait=True)

    assert result is True
    assert wakeup.is_set() is True

    err = capsys.readouterr().err
    assert "event=loop.burst_wakeup" in err
    assert "new_tasks=3" in err


def test_burst_10_emails_meets_t_delivery_ceiling(
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per §V.69: 10 inbound routed emails (P=5 concurrent inserts) -> burst-wakeup
    fires, every task lands within the 75s T_delivery ceiling vs its trigger email's
    received_at.
    """
    test_settings = make_test_settings()
    account = make_test_account(database_connection, email="inbound@example.com")
    workflow = make_test_workflow(
        database_connection,
        account.id,
        name="inbound-flow",
        workflow_type="inbound",
    )

    received_at = datetime.now(UTC)

    # P=5 concurrent inserts mirror the burst shape §V.69 / §V.23 model. psycopg
    # connections are not thread-safe, so each worker opens its own connection.
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def _worker(items: list[int]) -> None:
        from mailpilot.database import (
            create_contact,
            create_enrollment,
        )
        from mailpilot.database import (
            create_email as _create_email,
        )

        connection = cast(
            "psycopg.Connection[dict[str, Any]]",
            psycopg.connect(
                str(test_settings.database_url),
                row_factory=dict_row,  # type: ignore[arg-type]
            ),
        )
        try:
            for idx in items:
                contact = create_contact(connection, email=f"sender-{idx}@example.com")
                assert contact is not None
                enrollment = create_enrollment(connection, workflow.id, contact.id)
                assert enrollment is not None
                _create_email(
                    connection,
                    account_id=account.id,
                    direction="inbound",
                    subject=f"trigger {idx}",
                    body_text=f"body {idx}",
                    gmail_message_id=f"gmail-msg-{idx}",
                    gmail_thread_id=f"gmail-thr-{idx}",
                    contact_id=contact.id,
                    workflow_id=workflow.id,
                    status="received",
                    is_routed=True,
                    route_method="classified",
                    received_at=received_at,
                    sender=f"sender-{idx}@example.com",
                    recipients={"to": [account.email], "cc": [], "bcc": []},
                )
                connection.commit()
        except BaseException as exc:
            with errors_lock:
                errors.append(exc)
        finally:
            connection.close()

    work_items = list(range(10))
    threads = [
        threading.Thread(
            target=_worker,
            args=(work_items[chunk_start::5],),
            name=f"seed-{chunk_start}",
        )
        for chunk_start in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"seed threads raised: {errors}"

    # Drain side-effects mocked: this test verifies routing classification +
    # burst-wakeup, not the drain pool (covered by test_sync_drain_concurrent).
    monkeypatch.setattr("mailpilot.sync._drain_pending_tasks", lambda *_a, **_k: None)
    monkeypatch.setattr("mailpilot.sync._sync_all_accounts", lambda *_a, **_k: None)

    wakeup = threading.Event()
    pool, in_flight = _empty_pool_and_inflight()
    try:
        result = _run_periodic_iteration(
            database_connection,
            test_settings,
            queue.Queue(),
            "event",
            do_full_sweep=False,
            pool=pool,
            in_flight=in_flight,
            wakeup_event=wakeup,
        )
    finally:
        pool.shutdown(wait=True)

    # Burst-wakeup contract: iteration must signal the next tick.
    assert result is True
    assert wakeup.is_set() is True

    # T_delivery ceiling per §V.69.
    pending = list_pending_tasks(database_connection)
    assert len(pending) == 10

    row = database_connection.execute(
        """\
        SELECT MAX(EXTRACT(EPOCH FROM (t.created_at - e.received_at)))::float
            AS max_delay
        FROM task t
        JOIN email e ON e.id = t.email_id
        WHERE e.account_id = %(account_id)s
        """,
        {"account_id": account.id},
    ).fetchone()
    assert row is not None
    max_delay = row["max_delay"]
    assert max_delay is not None
    assert max_delay <= 75.0, f"T_delivery {max_delay}s > 75s ceiling"


def test_start_sync_loop_force_sweep_after_new_tasks(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Per §V.69: a tick that creates new tasks forces a full sweep on the next
    tick even when the ``run_interval`` time gate has not yet elapsed.

    Three iterations, ``run_interval=30``:

    - tick 1 @ t=1000: first wake, ``last_full_sync=0`` -> full sweep runs.
      Iteration returns force_full_sweep_next=True (10 new tasks).
    - tick 2 @ t=1005: 5s elapsed (< 30), would otherwise skip the sweep, but
      the carry-over flag forces it. Returns force_full_sweep_next=False.
    - tick 3 @ t=1010: only 5s past last sweep, no carry-over flag -> sweep
      is skipped.

    Result: ``_sync_all_accounts`` called twice (ticks 1 and 2), not three
    times -- two CONSECUTIVE full sweeps even inside the run_interval window.
    """
    settings = make_test_settings(run_interval=30)

    with (
        patch("mailpilot.sync.threading.Event") as mock_event_cls,
        patch("mailpilot.sync._start_task_listener"),
        patch("mailpilot.sync._start_pubsub_logging_errors", return_value=None),
        patch("mailpilot.sync._drain_sync_queue"),
        patch("mailpilot.sync._sync_all_accounts") as mock_sync_all,
        patch("mailpilot.sync.create_tasks_for_routed_emails") as mock_create_tasks,
        patch("mailpilot.sync._drain_pending_tasks"),
        patch("mailpilot.sync._renew_watches_logging_errors"),
        patch("mailpilot.sync.signal.signal"),
        patch("mailpilot.sync.has_google_credentials", return_value=False),
        patch("mailpilot.sync.time.monotonic") as mock_monotonic,
    ):
        mock_monotonic.side_effect = [1000.0, 1005.0, 1010.0]
        # tick 1 -> 10 new tasks -> carry-over True
        # tick 2 -> 0 tasks
        # tick 3 -> 0 tasks
        mock_create_tasks.side_effect = [[MagicMock()] * 10, [], []]

        mock_shutdown = MagicMock()
        mock_shutdown.is_set.side_effect = [
            False,  # while iter 1
            False,  # post-wait iter 1
            False,  # while iter 2
            False,  # post-wait iter 2
            False,  # while iter 3
            False,  # post-wait iter 3
            True,  # while -> exit
        ]
        mock_wakeup = MagicMock()
        mock_wakeup.wait.return_value = True
        mock_event_cls.side_effect = [mock_shutdown, mock_wakeup]

        start_sync_loop(database_connection, settings)

    assert mock_sync_all.call_count == 2
