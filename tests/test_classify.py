"""Tests for the LLM-based email classifier (§V.27 step 3)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from logfire.testing import CaptureLogfire
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from conftest import make_test_settings
from mailpilot.agent import classify as classify_module
from mailpilot.agent.classify import (
    _AGENT,  # pyright: ignore[reportPrivateUsage]
    _get_model,  # pyright: ignore[reportPrivateUsage]
    classify_email,
)
from mailpilot.models import Workflow


def make_workflow(
    workflow_id: str,
    name: str,
    goal: str,
    workflow_type: str = "inbound",
) -> Workflow:
    now = datetime.now(UTC)
    template = "inbound-general" if workflow_type == "inbound" else "outbound-general"
    return Workflow(
        id=workflow_id,
        name=name,
        template=template,  # pyright: ignore[reportArgumentType]
        type=workflow_type,  # pyright: ignore[reportArgumentType]
        account_id="account-1",
        account_email="account-1@example.com",
        status="active",
        goal=goal,
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


def test_missing_api_key_raises() -> None:
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


def test_classify_span_has_usage_attributes(capfire: CaptureLogfire) -> None:
    """agent.classify_email span includes input_tokens, output_tokens."""
    workflow = make_workflow(
        "wf-sales-1",
        "Sales inbound",
        "Handle inbound pricing and demo requests",
    )
    run_classify(
        [workflow],
        function_model_returning(workflow_id="wf-sales-1", reasoning="pricing"),
    )

    classify_spans: list[dict[str, Any]] = [
        s
        for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "agent.classify_email"
    ]
    assert len(classify_spans) == 1
    attrs = classify_spans[0]["attributes"]
    assert "model" in attrs
    assert "input_tokens" in attrs
    assert "output_tokens" in attrs
    assert "total_tokens" in attrs
    assert attrs["input_tokens"] >= 0
    assert attrs["output_tokens"] >= 0
    assert attrs["total_tokens"] == attrs["input_tokens"] + attrs["output_tokens"]


def test_instructions_honor_explicit_redirect_hints() -> None:
    """Classifier honors a goal's explicit cross-routing hint over word overlap.

    Both live goals carry redirect lines ("send X to Y instead"). Routing on
    raw semantic overlap alone could beat the intended negative-routing, so the
    instructions tell the model to honor explicit redirect hints over surface
    word overlap.
    """
    instructions = classify_module._INSTRUCTIONS.lower()  # pyright: ignore[reportPrivateUsage]
    assert "redirect" in instructions
    assert "instead" in instructions


def test_classifier_agent_has_explicit_name_for_otel_traces() -> None:
    """Classifier and workflow agents both emit `agent run` spans -- giving
    each Agent an explicit `name=` keeps `gen_ai.agent.name` legible instead
    of leaking the private `_AGENT` variable name into telemetry."""
    assert _AGENT.name == "mailpilot.classifier"


def test_get_model_carries_cache_settings() -> None:
    """§V.47: classifier's AnthropicModel sets cache_control breakpoints.

    Pydantic AI translates ``anthropic_cache_tool_definitions`` and
    ``anthropic_cache_instructions`` into ``cache_control`` blocks on the
    last tool definition and last system block of the outbound Anthropic
    request. Inspecting the bound settings is the structural contract.
    """
    _get_model.cache_clear()
    model = _get_model(
        "sk-test-cache", "claude-sonnet-4-6", "https://api.anthropic.com"
    )
    assert model.settings is not None
    assert model.settings.get("anthropic_cache_tool_definitions") is True
    assert model.settings.get("anthropic_cache_instructions") is True


def test_get_model_omits_reasoning_keys() -> None:
    """§V.130: classifier _get_model carries no thinking/effort keys.

    The classifier is a one-shot structured-output decision; its signature
    takes no Settings, so it cannot pick up the reasoning controls. Reasoning
    config stays scoped to the workflow agent.
    """
    _get_model.cache_clear()
    model = _get_model("sk-test", "claude-sonnet-4-6", "https://api.anthropic.com")
    assert model.settings is not None
    assert model.settings.get("anthropic_thinking") is None
    assert model.settings.get("anthropic_effort") is None


def test_get_model_omits_max_tokens() -> None:
    """§V.130: classifier _get_model carries no max_tokens key.

    The output-token budget is scoped to the workflow agent; the classifier
    is a one-shot structured-output decision and its signature takes no
    Settings, so it cannot pick up the budget.
    """
    _get_model.cache_clear()
    model = _get_model("sk-test", "claude-sonnet-4-6", "https://api.anthropic.com")
    assert model.settings is not None
    assert model.settings.get("max_tokens") is None


def test_get_model_uses_240s_read_timeout() -> None:
    """§V.48: classifier's AnthropicProvider HTTP client carries a 240s read-timeout.

    Default httpx read-timeout is 60s; under model load that intersects
    long-context classifier latency and surfaces ``TimeoutError`` mid-call
    (see SPEC.md §B.16). 240s = 4x headroom. No retry on timeout.
    """
    _get_model.cache_clear()
    model = _get_model(
        "sk-test-timeout", "claude-sonnet-4-6", "https://api.anthropic.com"
    )
    http_client = model._provider.client._client  # pyright: ignore[reportPrivateUsage]
    assert http_client.timeout.read == 240.0


def test_get_model_default_base_url_targets_anthropic_endpoint() -> None:
    """The default anthropic_base_url keeps the classifier on the Anthropic endpoint.

    The settings default is the canonical Anthropic host, so threading it into
    ``AnthropicProvider`` leaves the classifier on ``api.anthropic.com``.
    """
    default_base_url = make_test_settings().anthropic_base_url
    _get_model.cache_clear()
    model = _get_model("sk-test-default", "claude-sonnet-4-6", default_base_url)
    base_url = str(model._provider.client.base_url)  # pyright: ignore[reportPrivateUsage]
    assert "api.anthropic.com" in base_url


def test_get_model_threads_base_url_override() -> None:
    """A set base_url routes the classifier to the override endpoint.

    Threading ``settings.anthropic_base_url`` into ``AnthropicProvider``
    re-targets the wire endpoint, which is the Novita switch (§I config).
    """
    _get_model.cache_clear()
    model = _get_model(
        "sk-test-novita", "minimax/minimax-m3", "https://api.novita.ai/anthropic"
    )
    base_url = str(model._provider.client.base_url)  # pyright: ignore[reportPrivateUsage]
    assert "api.novita.ai/anthropic" in base_url


def test_get_model_cache_key_includes_base_url() -> None:
    """base_url rides the lru_cache key, so changing it rebuilds the model.

    Without base_url in the cache key, a switch to Novita would return a model
    still bound to the prior endpoint until cache eviction.
    """
    _get_model.cache_clear()
    anthropic_model = _get_model(
        "sk-shared", "claude-sonnet-4-6", "https://api.anthropic.com"
    )
    novita_model = _get_model(
        "sk-shared", "claude-sonnet-4-6", "https://api.novita.ai/anthropic"
    )
    assert anthropic_model is not novita_model
