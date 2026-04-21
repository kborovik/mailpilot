"""Tests for workflow agent invocation."""

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
from mailpilot.agent.invoke import (
    _advisory_lock_key,  # pyright: ignore[reportPrivateUsage]
    invoke_workflow_agent,
)
from mailpilot.database import (
    activate_workflow,
    create_workflow_contact,
    update_workflow,
)
from mailpilot.exceptions import AgentDidNotUseToolsError

# -- Helpers -------------------------------------------------------------------


def _activate(connection: psycopg.Connection[dict[str, Any]], workflow_id: str) -> None:
    """Fill required fields and activate a workflow."""
    update_workflow(
        connection,
        workflow_id,
        objective="Test objective",
        instructions="You are a sales outreach agent. Send an email to the contact.",
    )
    activate_workflow(connection, workflow_id)


def _setup(
    connection: psycopg.Connection[dict[str, Any]],
) -> tuple[Any, Any, Any]:
    """Create account, contact, and active outbound workflow."""
    account = make_test_account(connection, email="sender@example.com")
    contact = make_test_contact(connection, email="lead@acme.com", domain="acme.com")
    workflow = make_test_workflow(connection, account_id=account.id)
    _activate(connection, workflow.id)
    create_workflow_contact(connection, workflow.id, contact.id)
    return account, contact, workflow


def _model_that_calls_noop(
    messages: list[ModelMessage], info: AgentInfo
) -> ModelResponse:
    """FunctionModel that calls the noop tool then finishes."""
    # First call: invoke noop tool
    for msg in messages:
        for part in msg.parts if hasattr(msg, "parts") else []:
            if isinstance(part, ToolCallPart):
                # Tool result received -- finish with text
                return ModelResponse(
                    parts=[TextPart(content="Done, no action needed.")]
                )
    return ModelResponse(
        parts=[ToolCallPart(tool_name="noop", args={"reason": "no action needed"})]
    )


def _model_that_calls_tool(tool_name: str, tool_args: dict[str, Any]) -> FunctionModel:
    """Build a FunctionModel that calls a specific tool then finishes."""
    call_count = 0

    def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name=tool_name, args=tool_args)]
            )
        return ModelResponse(parts=[TextPart(content="Done.")])

    return FunctionModel(_respond)


def _model_no_tools(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """FunctionModel that returns text only, no tool calls."""
    return ModelResponse(
        parts=[TextPart(content="I thought about it but decided not to act.")]
    )


def _capturing_model(
    captured: list[ModelMessage],
) -> FunctionModel:
    """Build a FunctionModel that captures messages on first call, then finishes."""
    call_count = 0

    def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            captured.extend(messages)
            return ModelResponse(
                parts=[ToolCallPart(tool_name="noop", args={"reason": "testing"})]
            )
        return ModelResponse(parts=[TextPart(content="Done")])

    return FunctionModel(_respond)


# -- Tests: tool-use enforcement -----------------------------------------------


def test_agent_calls_noop_passes_enforcement(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Agent calls noop -> enforcement passes (noop counts as a tool call)."""
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    with patch("mailpilot.agent.invoke.GmailClient"):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            model_override=FunctionModel(_model_that_calls_noop),
        )
    # No exception means enforcement passed.


def test_agent_no_tool_calls_raises(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Agent returns text only -> enforcement raises AgentDidNotUseToolsError."""
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        pytest.raises(AgentDidNotUseToolsError, match=workflow.id),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            model_override=FunctionModel(_model_no_tools),
        )


def test_agent_calls_real_tool_passes_enforcement(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Agent calls a real tool (read_contact) -> enforcement passes."""
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    model = _model_that_calls_tool("read_contact", {"email": contact.email})
    with patch("mailpilot.agent.invoke.GmailClient"):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            model_override=model,
        )


# -- Tests: advisory lock ------------------------------------------------------


def test_advisory_lock_skip_when_held(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """When advisory lock is already held, invocation is skipped (returns None)."""
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )

    # Acquire the same advisory lock from a separate connection to simulate
    # a concurrent invocation.
    from conftest import TEST_DATABASE_URL

    blocker = psycopg.connect(TEST_DATABASE_URL)
    try:
        lock_key = _advisory_lock_key(workflow.id, contact.id)
        blocker.execute("SELECT pg_advisory_lock(%s)", (lock_key,))

        result = invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            model_override=FunctionModel(_model_that_calls_noop),
        )
        assert result is None
    finally:
        blocker.close()


# -- Tests: email history context -----------------------------------------------


def test_email_history_loaded(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Agent receives email history between account and contact in the prompt."""
    account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )

    # Create an email in the history.
    from mailpilot.database import create_email

    create_email(
        database_connection,
        gmail_message_id="msg-hist-1",
        gmail_thread_id="thread-hist-1",
        account_id=account.id,
        contact_id=contact.id,
        direction="outbound",
        subject="Previous outreach",
        body_text="Hi, interested in a demo?",
    )

    captured_messages: list[ModelMessage] = []
    with patch("mailpilot.agent.invoke.GmailClient"):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            model_override=_capturing_model(captured_messages),
        )

    all_text = str(captured_messages)
    assert "Previous outreach" in all_text


# -- Tests: trigger context ----------------------------------------------------


def test_inbound_email_trigger(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """When an inbound email is provided, it appears in the agent prompt."""
    account, contact, workflow = _setup(database_connection)
    # Make it inbound for this test.
    update_workflow(database_connection, workflow.id, type="inbound")
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )

    from mailpilot.database import create_email

    email = create_email(
        database_connection,
        gmail_message_id="msg-inbound-1",
        gmail_thread_id="thread-inbound-1",
        account_id=account.id,
        contact_id=contact.id,
        direction="inbound",
        subject="Question about pricing",
        body_text="How much does your product cost?",
    )

    captured_messages: list[ModelMessage] = []
    with patch("mailpilot.agent.invoke.GmailClient"):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            email=email,
            model_override=_capturing_model(captured_messages),
        )

    all_text = str(captured_messages)
    assert "Question about pricing" in all_text
    assert "How much does your product cost?" in all_text
