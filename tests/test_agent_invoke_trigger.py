"""Span-contract tests for the ``agent.invoke`` ``trigger`` attribute.

SPEC §V12 requires the ``trigger`` attribute on the ``agent.invoke`` span
to reflect the caller path explicitly: ``enrollment_run`` for CLI manual
runs, ``task`` for background drains, ``email`` for email-driven calls,
``manual`` for direct programmatic calls. Tests assert the value flows
through from the explicit ``trigger`` parameter rather than being
heuristically inferred.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import psycopg
from logfire.testing import CaptureLogfire
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from conftest import (
    make_test_account,
    make_test_contact,
    make_test_settings,
    make_test_workflow,
)
from mailpilot.agent.invoke import invoke_workflow_agent
from mailpilot.database import (
    activate_workflow,
    create_enrollment,
    update_workflow,
)


def _activate(connection: psycopg.Connection[dict[str, Any]], workflow_id: str) -> None:
    update_workflow(
        connection,
        workflow_id,
        objective="Test objective",
        instructions="Send a follow-up to the contact.",
    )
    activate_workflow(connection, workflow_id)


def _model_that_calls_noop(
    messages: list[ModelMessage], info: AgentInfo
) -> ModelResponse:
    del info
    for msg in messages:
        for part in msg.parts if hasattr(msg, "parts") else []:
            if isinstance(part, ToolCallPart):
                return ModelResponse(parts=[TextPart(content="Done.")])
    return ModelResponse(
        parts=[ToolCallPart(tool_name="noop", args={"reason": "no action"})]
    )


def _agent_invoke_trigger(capfire: CaptureLogfire) -> str:
    spans = [
        span
        for span in capfire.exporter.exported_spans_as_dict()
        if span["name"] == "agent.invoke"
    ]
    assert len(spans) == 1, f"expected exactly one agent.invoke span, got {len(spans)}"
    trigger = spans[0]["attributes"].get("trigger")
    assert isinstance(trigger, str)
    return trigger


def _run(
    database_connection: psycopg.Connection[dict[str, Any]],
    *,
    trigger: str | None,
) -> None:
    account = make_test_account(database_connection, email="agent@example.com")
    contact = make_test_contact(
        database_connection, email="lead@acme.com", domain="acme.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    create_enrollment(database_connection, workflow.id, contact.id)

    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )

    kwargs: dict[str, Any] = {
        "model_override": FunctionModel(_model_that_calls_noop),
    }
    if trigger is not None:
        kwargs["trigger"] = trigger

    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            **kwargs,
        )


def test_trigger_enrollment_run(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    _run(database_connection, trigger="enrollment_run")
    assert _agent_invoke_trigger(capfire) == "enrollment_run"


def test_trigger_task(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    _run(database_connection, trigger="task")
    assert _agent_invoke_trigger(capfire) == "task"


def test_trigger_default_is_manual(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Direct programmatic callers that omit ``trigger`` get ``manual``."""
    _run(database_connection, trigger=None)
    assert _agent_invoke_trigger(capfire) == "manual"
