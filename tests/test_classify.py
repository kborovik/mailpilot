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
    classify_email,
)
from mailpilot.agent.model import (
    _build_anthropic_model,  # pyright: ignore[reportPrivateUsage]
    _build_xai_model,  # pyright: ignore[reportPrivateUsage]
    build_model,
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
        llm_provider="xai",
        xai_api_key="xai-test",
        xai_model="grok-4.5",
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
    """Without the active provider API key, classification must fail fast."""
    workflow = make_workflow("wf-1", "Sales", "Pricing")
    settings = make_test_settings(llm_provider="xai", xai_api_key="")
    with pytest.raises(ValueError, match="mailpilot config set xai_api_key"):
        classify_email(
            subject="hi",
            body="hello",
            sender="x@example.com",
            active_workflows=[workflow],
            settings=settings,
        )


def test_missing_anthropic_api_key_raises_when_selected() -> None:
    """Anthropic path fails closed when anthropic_api_key is empty."""
    workflow = make_workflow("wf-1", "Sales", "Pricing")
    settings = make_test_settings(
        llm_provider="anthropic", anthropic_api_key="", xai_api_key="unused"
    )
    with pytest.raises(ValueError, match="mailpilot config set anthropic_api_key"):
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
    """Classifier and workflow agents both emit `invoke_agent` spans -- giving
    each Agent an explicit `name=` keeps `gen_ai.agent.name` legible instead
    of leaking the private `_AGENT` variable name into telemetry."""
    assert _AGENT.name == "mailpilot.classifier"


def test_classifier_anthropic_carries_cache_settings() -> None:
    """§V.47: classifier AnthropicModel sets cache_control breakpoints."""
    settings = make_test_settings(
        llm_provider="anthropic", anthropic_api_key="sk-test-cache"
    )
    model = _build_anthropic_model(settings, role="classifier")
    assert model.settings is not None
    assert model.settings.get("anthropic_cache_tool_definitions") is True
    assert model.settings.get("anthropic_cache_instructions") is True


def test_classifier_anthropic_omits_reasoning_keys() -> None:
    """§V.47: classifier carries no thinking/effort keys (workflow-only)."""
    settings = make_test_settings(llm_provider="anthropic", anthropic_api_key="sk-test")
    model = _build_anthropic_model(settings, role="classifier")
    assert model.settings is not None
    assert model.settings.get("anthropic_thinking") is None
    assert model.settings.get("anthropic_effort") is None


def test_classifier_anthropic_omits_max_tokens() -> None:
    """§V.47: classifier carries no max_tokens key (workflow-only)."""
    settings = make_test_settings(llm_provider="anthropic", anthropic_api_key="sk-test")
    model = _build_anthropic_model(settings, role="classifier")
    assert model.settings is not None
    assert model.settings.get("max_tokens") is None


def test_classifier_anthropic_uses_240s_read_timeout() -> None:
    """§V.48: classifier AnthropicProvider HTTP client carries 240s read-timeout."""
    settings = make_test_settings(
        llm_provider="anthropic", anthropic_api_key="sk-test-timeout"
    )
    model = _build_anthropic_model(settings, role="classifier")
    http_client = model._provider.client._client  # pyright: ignore[reportPrivateUsage]
    assert http_client.timeout.read == 240.0


def test_classifier_anthropic_default_base_url() -> None:
    """Default anthropic_base_url keeps the classifier on api.anthropic.com."""
    settings = make_test_settings(
        llm_provider="anthropic", anthropic_api_key="sk-test-default"
    )
    model = _build_anthropic_model(settings, role="classifier")
    base_url = str(model._provider.client.base_url)  # pyright: ignore[reportPrivateUsage]
    assert "api.anthropic.com" in base_url


def test_classifier_anthropic_threads_base_url_override() -> None:
    """A set anthropic_base_url routes the classifier to the override endpoint."""
    settings = make_test_settings(
        llm_provider="anthropic",
        anthropic_api_key="sk-test-novita",
        anthropic_model="minimax/minimax-m3",
        anthropic_base_url="https://api.novita.ai/anthropic",
    )
    model = _build_anthropic_model(settings, role="classifier")
    base_url = str(model._provider.client.base_url)  # pyright: ignore[reportPrivateUsage]
    assert "api.novita.ai/anthropic" in base_url


def test_classifier_dispatches_default_xai() -> None:
    """§V.47: default llm_provider=xai builds XaiModel for classifier."""
    from pydantic_ai.models.xai import XaiModel

    settings = make_test_settings(xai_api_key="xai-test")
    model = build_model(settings, role="classifier")
    assert isinstance(model, XaiModel)


def test_classifier_xai_omits_workflow_settings() -> None:
    """§V.47: classifier xAI path has no max_tokens / reasoning_effort."""
    settings = make_test_settings(llm_provider="xai", xai_api_key="xai-test")
    model = _build_xai_model(settings, role="classifier")
    assert model.settings is None or model.settings.get("max_tokens") is None
    assert model.settings is None or model.settings.get("xai_reasoning_effort") is None
