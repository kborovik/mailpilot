"""Operator-log emissions from the routing pipeline."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from conftest import (
    make_test_account,
    make_test_settings,
    make_test_workflow,
)
from mailpilot.agent import classify as classify_module
from mailpilot.database import (
    activate_workflow,
    create_email,
    update_workflow,
)
from mailpilot.routing import route_email


def _activate_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> None:
    update_workflow(
        connection,
        workflow_id,
        objective="Handle inbound inquiries",
        instructions="Reply helpfully",
    )
    activate_workflow(connection, workflow_id)


def _function_model_returning(workflow_id: str | None) -> FunctionModel:
    def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="final_result",
                    args={"workflow_id": workflow_id, "reasoning": ""},
                ),
            ],
        )

    return FunctionModel(_respond)


def test_route_email_emits_match_via_thread(
    capsys: pytest.CaptureFixture[str],
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection, email="rt@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    _activate_workflow(database_connection, workflow.id)

    prior = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="prior",
        gmail_thread_id="thread-z",
        workflow_id=workflow.id,
        is_routed=True,
    )
    assert prior is not None

    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="reply",
        gmail_thread_id="thread-z",
    )
    assert new_email is not None

    capsys.readouterr()
    route_email(
        database_connection, new_email, "alice@example.com", make_test_settings()
    )

    out = capsys.readouterr().err
    assert "event=route.match" in out
    assert f"email_id={new_email.id}" in out
    assert f"workflow_id={workflow.id}" in out
    assert "via=thread" in out


def test_route_email_emits_match_via_message_id(
    capsys: pytest.CaptureFixture[str],
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Reply that re-threads on the recipient side is routed via In-Reply-To."""
    account = make_test_account(database_connection, email="rfcop@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    _activate_workflow(database_connection, workflow.id)

    prior = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="initial",
        gmail_thread_id="thread-outbound",
        rfc2822_message_id="<orig@mailpilot.test>",
        workflow_id=workflow.id,
        is_routed=True,
    )
    assert prior is not None

    reply = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Re: initial",
        gmail_thread_id="thread-reply-different",
        in_reply_to="<orig@mailpilot.test>",
    )
    assert reply is not None

    capsys.readouterr()
    route_email(database_connection, reply, "alice@example.com", make_test_settings())

    out = capsys.readouterr().err
    assert "event=route.match" in out
    assert "via=message_id" in out
    assert f"workflow_id={workflow.id}" in out


def test_route_email_emits_no_match_when_classification_returns_none(
    capsys: pytest.CaptureFixture[str],
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection, email="rt2@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    _activate_workflow(database_connection, workflow.id)

    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="random",
        body_text="not relevant",
        gmail_thread_id="thread-new",
    )
    assert new_email is not None

    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="claude-sonnet-4-6"
    )
    capsys.readouterr()
    with classify_module._AGENT.override(  # pyright: ignore[reportPrivateUsage]
        model=_function_model_returning(None)
    ):
        route_email(database_connection, new_email, "bob@example.com", settings)

    out = capsys.readouterr().err
    assert "event=route.no_match" in out
    assert f"email_id={new_email.id}" in out


def test_route_email_emits_match_via_llm_when_classifier_returns_workflow(
    capsys: pytest.CaptureFixture[str],
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection, email="rt3@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    _activate_workflow(database_connection, workflow.id)

    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="cold",
        body_text="please help",
        gmail_thread_id="thread-cold",
    )
    assert new_email is not None

    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="claude-sonnet-4-6"
    )
    capsys.readouterr()
    with classify_module._AGENT.override(  # pyright: ignore[reportPrivateUsage]
        model=_function_model_returning(workflow.id)
    ):
        route_email(database_connection, new_email, "carol@example.com", settings)

    out = capsys.readouterr().err
    assert "event=route.match" in out
    assert "via=llm" in out
    assert f"workflow_id={workflow.id}" in out
