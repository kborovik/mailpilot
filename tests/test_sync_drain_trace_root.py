"""Trace-root contract for the concurrent task drain per §V.23(∆) / §B.82.

§B.82: py3.14 ``ThreadPoolExecutor.submit`` copies the submitting thread's
``contextvars`` into each worker, so a task drained while the dispatching
tick's ``sync.loop.iteration`` span is active opens ``run.execute_task`` ->
``agent.invoke`` *under that tick's trace*. Every co-tick invoke then shares
one ``trace_id``, breaking the trace_id-is-1:1-with-an-agent.invoke contract
and smearing the gate-side per-invoke read attribution (§B.81).

§V.23(∆) fix: ``_execute_task_in_worker`` detaches the inherited OTel context
(attaches a fresh empty ``Context()``) before ``execute_task`` and restores it
in ``finally``, so each drained task roots its own trace.
"""

from __future__ import annotations

import concurrent.futures
import time
from typing import Any
from unittest.mock import MagicMock, patch

import logfire
import psycopg
import pytest
from logfire.testing import CaptureLogfire

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


def test_co_tick_drain_roots_each_invoke_in_its_own_trace(
    database_connection: psycopg.Connection[dict[str, Any]],
    capfire: CaptureLogfire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per §V.23(∆) / §B.82: co-tick drained tasks get distinct trace roots.

    Drains a >1 co-tick burst while the dispatching ``sync.loop.iteration``
    span is active. Each worker's ``agent.invoke`` span MUST:

    - carry a ``trace_id`` distinct from every sibling (1:1 per invoke);
    - NOT inherit the iteration's ``trace_id``;
    - be a trace root (``parent`` is None).
    """
    from mailpilot.sync import (
        _drain_pending_tasks,  # pyright: ignore[reportPrivateUsage]
    )

    pending = [_make_fake_task("t1"), _make_fake_task("t2"), _make_fake_task("t3")]
    monkeypatch.setattr("mailpilot.sync.list_pending_tasks", lambda *_a, **_k: pending)

    # Stand in for ``run.execute_task`` -> ``agent.invoke``: open the span the
    # gate-side read attribution keys on. Tagged with the task id so the
    # asserted spans map back to their worker.
    def _invoke_span(_conn: object, _settings: object, task: MagicMock) -> None:
        with logfire.span("agent.invoke", task_id=task.id):
            pass

    import mailpilot.run as run_module

    monkeypatch.setattr(run_module, "execute_task", _invoke_span)

    fake_conn = MagicMock()
    in_flight: dict[concurrent.futures.Future[None], tuple[str, float]] = {}
    # Submit the burst *inside* the dispatch span -- this is the structure that
    # made co-tick invokes inherit one trace_id before §V.23(∆).
    with (
        concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool,
        patch("mailpilot.sync.psycopg.connect", return_value=fake_conn),
        logfire.span("sync.loop.iteration"),
    ):
        _drain_pending_tasks(
            database_connection,
            make_test_settings(max_concurrent_tasks=4),
            pool,
            in_flight,
        )
        _wait_for_in_flight(in_flight)

    spans = capfire.exporter.exported_spans_as_dict()
    iteration_spans = [s for s in spans if s["name"] == "sync.loop.iteration"]
    invoke_spans = [s for s in spans if s["name"] == "agent.invoke"]
    assert len(iteration_spans) == 1, "expected exactly one dispatch span"
    assert len(invoke_spans) == 3, f"expected 3 agent.invoke spans, got {invoke_spans}"

    iteration_trace = iteration_spans[0]["context"]["trace_id"]
    invoke_traces = [s["context"]["trace_id"] for s in invoke_spans]

    # 1:1 -- one distinct trace per invoke (the smear bug collapsed these to 1).
    assert len(set(invoke_traces)) == 3, (
        f"co-tick invokes must each root their own trace; got traces={invoke_traces}"
    )
    # None inherits the dispatching tick's trace.
    assert all(t != iteration_trace for t in invoke_traces), (
        "agent.invoke must not inherit the sync.loop.iteration trace_id"
    )
    # Each invoke is a fresh trace root, not a child of the dispatch span.
    assert all(s["parent"] is None for s in invoke_spans), (
        "each drained agent.invoke must be a trace root (parent is None)"
    )
