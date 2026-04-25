"""Tests for workflow agent invocation."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest
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
from mailpilot.agent.invoke import (
    _SYSTEM_PREFIX,  # pyright: ignore[reportPrivateUsage]
    _advisory_lock_keys,  # pyright: ignore[reportPrivateUsage]
    invoke_workflow_agent,
)
from mailpilot.database import (
    activate_workflow,
    create_enrollment,
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
    create_enrollment(connection, workflow.id, contact.id)
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
        k1, k2 = _advisory_lock_keys(workflow.id, contact.id)
        blocker.execute("SELECT pg_advisory_lock(%s, %s)", (k1, k2))

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
        workflow_id=workflow.id,
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


def test_email_history_scoped_to_workflow(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Agent only sees emails from its own workflow, not other workflows."""
    account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )

    from mailpilot.database import create_email

    # Email belonging to THIS workflow -- should appear.
    create_email(
        database_connection,
        gmail_message_id="msg-same-wf",
        gmail_thread_id="thread-same-wf",
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=workflow.id,
        direction="outbound",
        subject="Same workflow outreach",
        body_text="This should be visible.",
    )

    # Email belonging to a DIFFERENT workflow -- should NOT appear.
    other_workflow = make_test_workflow(
        database_connection, account_id=account.id, name="Other workflow"
    )
    _activate(database_connection, other_workflow.id)
    create_email(
        database_connection,
        gmail_message_id="msg-other-wf",
        gmail_thread_id="thread-other-wf",
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=other_workflow.id,
        direction="outbound",
        subject="Other workflow outreach",
        body_text="This should NOT be visible.",
    )

    # Email with NO workflow -- should NOT appear.
    create_email(
        database_connection,
        gmail_message_id="msg-no-wf",
        gmail_thread_id="thread-no-wf",
        account_id=account.id,
        contact_id=contact.id,
        direction="inbound",
        subject="Unrelated inbound",
        body_text="This should NOT be visible either.",
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
    assert "Same workflow outreach" in all_text
    assert "Other workflow outreach" not in all_text
    assert "Unrelated inbound" not in all_text


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


def test_inbound_email_trigger_includes_email_id_and_sender(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Inbound email trigger includes email ID and sender so agent can reply."""
    account, contact, workflow = _setup(database_connection)
    update_workflow(database_connection, workflow.id, type="inbound")
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )

    from mailpilot.database import create_email

    email = create_email(
        database_connection,
        gmail_message_id="msg-thread-test",
        gmail_thread_id="thread-abc-123",
        account_id=account.id,
        contact_id=contact.id,
        direction="inbound",
        subject="Re: proposal",
        body_text="Looks good, let's proceed.",
    )
    assert email is not None

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
    assert email.id in all_text
    assert "lead@acme.com" in all_text
    # Thread ID should NOT be exposed -- reply_email resolves it internally.
    assert "thread-abc-123" not in all_text


def test_deferred_task_trigger(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """When task_description is provided, it appears in the agent prompt."""
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )

    captured_messages: list[ModelMessage] = []
    with patch("mailpilot.agent.invoke.GmailClient"):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            task_description="Follow up on demo request",
            task_context={"days_since_last": 7},
            model_override=_capturing_model(captured_messages),
        )

    all_text = str(captured_messages)
    assert "Follow up on demo request" in all_text
    assert "days_since_last" in all_text


# -- Tests: early-exit paths ---------------------------------------------------


def test_account_not_found_raises(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """When workflow references a deleted account, raises ValueError."""
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )

    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.database.get_account", return_value=None),
        pytest.raises(ValueError, match="account not found"),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            model_override=FunctionModel(_model_that_calls_noop),
        )


def test_missing_api_key_raises(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """When no model_override and no anthropic_api_key, raises ValueError."""
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(anthropic_api_key="", anthropic_model="test-model")

    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        pytest.raises(ValueError, match="anthropic_api_key is required"),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            # No model_override -- forces the real model path.
        )


# -- Tests: usage attributes on span ------------------------------------------


def test_invoke_span_has_usage_attributes(
    database_connection: psycopg.Connection[dict[str, Any]],
    capfire: CaptureLogfire,
) -> None:
    """agent.invoke span includes input_tokens, output_tokens, llm_requests."""
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

    invoke_spans = [
        s
        for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "agent.invoke"
    ]
    assert len(invoke_spans) == 1
    attrs = invoke_spans[0]["attributes"]
    assert "model" in attrs
    assert "input_tokens" in attrs
    assert "output_tokens" in attrs
    assert "total_tokens" in attrs
    assert "llm_requests" in attrs
    assert attrs["input_tokens"] >= 0
    assert attrs["output_tokens"] >= 0
    assert attrs["total_tokens"] == attrs["input_tokens"] + attrs["output_tokens"]
    assert attrs["llm_requests"] >= 1


# -- Tests: reply_email tool ---------------------------------------------------


def test_agent_calls_reply_email(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Agent can call reply_email tool to reply in-thread."""
    account, contact, workflow = _setup(database_connection)
    update_workflow(database_connection, workflow.id, type="inbound")
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )

    from mailpilot.database import create_email

    inbound = create_email(
        database_connection,
        gmail_message_id="msg-reply-invoke",
        gmail_thread_id="thread-reply-invoke",
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=workflow.id,
        direction="inbound",
        subject="Need help",
        body_text="Can you assist?",
    )
    assert inbound is not None

    model = _model_that_calls_tool(
        "reply_email",
        {"email_id": inbound.id, "body": "Sure, happy to help!"},
    )
    with patch("mailpilot.agent.invoke.GmailClient") as mock_cls:
        mock_client = MagicMock()
        mock_client.send_message.return_value = {
            "id": "sent-1",
            "threadId": "thread-reply-invoke",
            "labelIds": ["SENT"],
        }
        mock_cls.return_value = mock_client
        result = invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            email=inbound,
            model_override=model,
        )

    assert result is not None
    mock_client.send_message.assert_called_once()
    call_kwargs = mock_client.send_message.call_args.kwargs
    assert call_kwargs["to"] == contact.email
    assert call_kwargs["subject"] == "Re: Need help"
    assert call_kwargs["thread_id"] == "thread-reply-invoke"


# -- Tests: system prefix content ----------------------------------------------


def test_system_prefix_guides_contact_status_update() -> None:
    """System prefix must instruct agents to update contact status."""
    assert "update_enrollment_status" in _SYSTEM_PREFIX


def test_system_prefix_allows_markdown_in_emails() -> None:
    """System prefix must not prohibit markdown in email content."""
    assert "No markdown" not in _SYSTEM_PREFIX
