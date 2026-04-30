"""Operator-log emissions from agent invocation."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import psycopg
import pytest
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


def test_invoke_workflow_agent_emits_agent_run(
    capsys: pytest.CaptureFixture[str],
    database_connection: psycopg.Connection[dict[str, Any]],
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

    capsys.readouterr()
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            model_override=FunctionModel(_model_that_calls_noop),
        )

    out = capsys.readouterr().err
    assert "event=agent.run" in out
    assert f"workflow_id={workflow.id}" in out
    assert f"contact_id={contact.id}" in out
    assert "status=completed" in out
    assert "tool_calls=1" in out
