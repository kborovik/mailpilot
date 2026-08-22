"""§V.38 + §B.34 regression tests: Drive tools serialize parallel emissions.

The underlying ``googleapiclient.discovery.Resource`` is backed by a single
``httplib2.Http`` instance whose connection-pool dict has no internal locks.
Pydantic AI dispatches sync tools via ``asyncio.to_thread``, so an
Anthropic-emitted parallel fan-out lands two worker threads against the same
shared transport. The race surfaced in production as one ``read_drive_markdown``
returning in ~1.1s while its sibling hung 60.8s at the socket timeout, which
burned the §V.49 retry budget and turned a transient blip into a terminal
``failed`` task.

The structural fix is ``sequential=True`` on every Drive ``Tool(...)``
registration in ``src/mailpilot/agent/templates.py`` -- the dispatcher then
runs parallel emissions one-at-a-time against that tool. These tests prove the
contract holds end-to-end through Pydantic AI's dispatcher with a race-detecting
fake Drive service.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic_ai import Agent, Tool
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from mailpilot.agent.tools import AgentDeps, read_drive_markdown
from mailpilot.drive import DriveClient
from mailpilot.models import Account


class _RaceDetectingService:
    """Fake Drive service that flags concurrent transport reuse.

    Mirrors the shape ``DriveClient.read_markdown`` expects:
    ``service.files().get(fileId=...).execute()`` for metadata and
    ``service.files().get_media(fileId=...).execute()`` for the body.
    Each ``execute()`` enters a critical section that raises
    ``RuntimeError("concurrent http reuse")`` if another execute() is
    already in flight on the same instance -- exactly the symptom a real
    shared ``httplib2.Http`` would expose under parallel dispatch.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_flight = 0
        self.max_concurrent = 0
        self.execute_count = 0

    @contextmanager
    def enter_execute(self) -> Generator[None]:
        with self._lock:
            if self._in_flight > 0:
                self._in_flight += 1
                self.max_concurrent = max(self.max_concurrent, self._in_flight)
                self._in_flight -= 1
                raise RuntimeError("concurrent http reuse")
            self._in_flight += 1
            self.max_concurrent = max(self.max_concurrent, self._in_flight)
            self.execute_count += 1
        try:
            # Hold the "transport in use" window open long enough for a
            # sibling thread to race the critical section if the dispatcher
            # ever lets two calls overlap.
            time.sleep(0.05)
            yield
        finally:
            with self._lock:
                self._in_flight -= 1

    def files(self) -> _RaceDetectingService:
        return self

    # Google API client passes camelCase kwargs (`fileId`, `supportsAllDrives`)
    # so the stub must match exactly; suppress N803 for that surface only.
    def get(
        self,
        *,
        fileId: str,  # noqa: N803
        fields: str,
        supportsAllDrives: bool,  # noqa: N803
    ) -> _MetadataHandle:
        del fields, supportsAllDrives
        return _MetadataHandle(self, fileId)

    def get_media(
        self,
        *,
        fileId: str,  # noqa: N803
        supportsAllDrives: bool,  # noqa: N803
    ) -> _MediaHandle:
        del supportsAllDrives
        return _MediaHandle(self, fileId)


class _MetadataHandle:
    def __init__(self, service: _RaceDetectingService, file_id: str) -> None:
        self._service = service
        self._file_id = file_id

    def execute(self) -> dict[str, str]:
        with self._service.enter_execute():
            return {
                "name": f"{self._file_id}.md",
                "webViewLink": f"https://drive.example/{self._file_id}",
            }


class _MediaHandle:
    def __init__(self, service: _RaceDetectingService, file_id: str) -> None:
        self._service = service
        self._file_id = file_id

    def execute(self) -> bytes:
        with self._service.enter_execute():
            return f"# Body for {self._file_id}\n".encode()


def _model_emitting_parallel_drive_reads(
    file_ids: tuple[str, str],
) -> FunctionModel:
    """Build a FunctionModel that emits two parallel read_drive_markdown
    calls on the first turn and a closing TextPart on the second."""

    state: dict[str, int] = {"turn": 0}

    def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        if state["turn"] == 0:
            state["turn"] = 1
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="read_drive_markdown",
                        args={"file_id": file_ids[0]},
                        tool_call_id="call-a",
                    ),
                    ToolCallPart(
                        tool_name="read_drive_markdown",
                        args={"file_id": file_ids[1]},
                        tool_call_id="call-b",
                    ),
                ]
            )
        return ModelResponse(parts=[TextPart(content="done")])

    return FunctionModel(_respond)


def _make_deps(connection: Any, service: _RaceDetectingService) -> AgentDeps:
    drive_client = DriveClient.from_service("agent@example.com", service)
    now = datetime.now(UTC)
    account = Account(
        id="acct-1",
        email="agent@example.com",
        display_name="Agent",
        created_at=now,
        updated_at=now,
    )
    return AgentDeps(
        connection=connection,
        account=account,
        gmail_client=MagicMock(),
        drive_client=drive_client,
        settings=MagicMock(),
        workflow_id="wf-1",
        contact_id="contact-1",
        enrollment_id="enroll-1",
    )


def _run_two_parallel_reads(*, sequential: bool) -> _RaceDetectingService:
    """Drive the dispatcher with two parallel read_drive_markdown calls.

    ``sequential`` toggles the Tool registration so the same fixture
    exercises both the post-fix (serialized) and pre-fix (raced) paths.
    """
    service = _RaceDetectingService()
    tool = Tool(
        read_drive_markdown,
        name="read_drive_markdown",
        sequential=sequential,
    )
    agent: Agent[AgentDeps, str] = Agent(deps_type=AgentDeps, tools=[tool])
    model = _model_emitting_parallel_drive_reads(("file-a", "file-b"))
    deps = _make_deps(connection=MagicMock(), service=service)
    agent.run_sync("dummy", model=model, deps=deps)
    return service


def test_drive_tool_sequential_true_serializes_parallel_dispatch() -> None:
    """Post-fix path: ``sequential=True`` makes the dispatcher serialize
    parallel emissions, so the race-detector never sees concurrent reuse."""
    service = _run_two_parallel_reads(sequential=True)
    assert service.execute_count == 4, (
        "expected 2 metadata + 2 media executes across both reads, "
        f"got {service.execute_count}"
    )
    assert service.max_concurrent == 1, (
        "sequential=True must keep transport reuse to one thread at a time; "
        f"saw max_concurrent={service.max_concurrent} -- §V.38 broken"
    )


def test_drive_tool_sequential_false_races_shared_transport() -> None:
    """Pre-fix race repro: without ``sequential=True``, parallel emissions
    land on distinct threads against the same transport, so the race detector
    trips ``RuntimeError("concurrent http reuse")``. This counterfactual
    exists so the positive test above is not vacuously satisfied -- removing
    ``sequential=True`` brings the §B.34 race straight back."""
    with pytest.raises(RuntimeError, match="concurrent http reuse"):
        _run_two_parallel_reads(sequential=False)
