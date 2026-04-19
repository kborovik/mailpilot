"""Tests for the LLM-based email classifier (ADR-04 step 2)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from conftest import make_test_settings
from mailpilot.agent import classify as classify_module
from mailpilot.agent.classify import classify_email
from mailpilot.models import Workflow


def make_workflow(
    workflow_id: str,
    name: str,
    objective: str,
    workflow_type: str = "inbound",
) -> Workflow:
    now = datetime.now(UTC)
    return Workflow(
        id=workflow_id,
        name=name,
        type=workflow_type,  # pyright: ignore[reportArgumentType]
        account_id="account-1",
        status="active",
        objective=objective,
        instructions="",
        created_at=now,
        updated_at=now,
    )


def function_model_returning(
    workflow_id: str | None,
    reasoning: str = "",
) -> FunctionModel:
    """Build a FunctionModel that yields a fixed structured-output result.

    Pydantic AI routes structured output through a synthetic
    ``final_result`` tool call, so tests return a ``ToolCallPart`` the
    same way the real model would.
    """

    def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="final_result",
                    args={"workflow_id": workflow_id, "reasoning": reasoning},
                ),
            ],
        )

    return FunctionModel(_respond)


def run_classify(
    workflows: list[Workflow],
    function_model: FunctionModel,
    subject: str = "Question about pricing",
    body: str = "Hi, I'd like to know more about your plans.",
    sender: str = "alice@example.com",
) -> str | None:
    """Invoke ``classify_email`` with the agent overridden to a FunctionModel."""
    settings = make_test_settings(
        anthropic_api_key="sk-test",
        anthropic_model="claude-sonnet-4-6",
    )
    with classify_module._AGENT.override(model=function_model):  # pyright: ignore[reportPrivateUsage]
        return classify_email(
            subject=subject,
            body=body,
            sender=sender,
            active_workflows=workflows,
            settings=settings,
        )


def test_single_match_returns_workflow_id() -> None:
    workflow = make_workflow(
        "wf-sales-1",
        "Sales inbound",
        "Handle inbound pricing and demo requests",
    )
    result = run_classify(
        [workflow],
        function_model_returning(workflow_id="wf-sales-1", reasoning="pricing intent"),
    )
    assert result == "wf-sales-1"


def test_no_match_returns_none() -> None:
    workflow = make_workflow(
        "wf-support-1",
        "Support",
        "Answer customer product questions",
    )
    result = run_classify(
        [workflow],
        function_model_returning(workflow_id=None, reasoning="no topic match"),
        subject="Interested in partnership",
        body="We'd like to explore a reseller agreement.",
    )
    assert result is None


def test_multiple_workflows_clear_winner() -> None:
    sales = make_workflow(
        "wf-sales-1",
        "Sales inbound",
        "Handle inbound pricing and demo requests",
    )
    support = make_workflow(
        "wf-support-1",
        "Support",
        "Answer customer product questions",
    )
    partnerships = make_workflow(
        "wf-partner-1",
        "Partnerships",
        "Evaluate reseller and integration partner requests",
    )
    result = run_classify(
        [sales, support, partnerships],
        function_model_returning(
            workflow_id="wf-partner-1",
            reasoning="partnership inquiry",
        ),
        subject="Partner proposal",
        body="We build an analytics tool and want to integrate.",
    )
    assert result == "wf-partner-1"


def test_empty_workflows_skips_llm_call() -> None:
    def _should_not_be_called(
        messages: list[ModelMessage],
        info: AgentInfo,
    ) -> ModelResponse:
        del messages, info
        raise AssertionError("LLM must not be called when no candidates exist")

    result = run_classify(
        [],
        FunctionModel(_should_not_be_called),
    )
    assert result is None


def test_model_returning_unknown_id_treated_as_no_match() -> None:
    """A hallucinated workflow_id (not in candidate set) must return None."""
    workflow = make_workflow(
        "wf-sales-1",
        "Sales",
        "Pricing questions",
    )
    result = run_classify(
        [workflow],
        function_model_returning(workflow_id="wf-does-not-exist"),
    )
    assert result is None


def test_missing_api_key_raises(
    database_connection: Any,  # ensures schema is applied
) -> None:
    """Without an Anthropic API key, classification must fail fast."""
    workflow = make_workflow("wf-1", "Sales", "Pricing")
    settings = make_test_settings(anthropic_api_key="")
    with pytest.raises(ValueError, match="anthropic_api_key"):
        classify_email(
            subject="hi",
            body="hello",
            sender="x@example.com",
            active_workflows=[workflow],
            settings=settings,
        )
