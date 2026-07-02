"""Span-emission contract tests for the Logfire scrubbing callback (§V.55).

The default Logfire scrubber redacts attribute values that contain substrings
like ``"auth"`` or ``"password"``. Per §V.55, tool-return payloads on
Pydantic-AI ``execute_tool`` spans must be exempt so KB grounding regressions
remain verifiable from traces alone. Other attribute paths must continue to
flow through the default scrubber.

The exemption test drives a real instrumented agent tool call instead of a
hand-built span, so the asserted attribute path tracks the name Pydantic AI
actually emits (``gen_ai.tool.call.result`` under instrumentation format 5;
``tool_response`` before v2 — the rename silently killed the original
path-keyed exemption, see §B history for T206).
"""

from __future__ import annotations

from typing import Any

import logfire
from logfire.testing import CaptureLogfire
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from mailpilot.cli import scrub_tool_response_callback


def _reconfigure_with_scrubbing(capfire: CaptureLogfire) -> None:
    """Re-run logfire.configure with the production scrubbing callback.

    The ``capfire`` fixture configures Logfire without scrubbing, so each test
    re-configures while pointing the exporter at the same in-memory sink.
    """
    logfire.configure(
        send_to_logfire=False,
        console=False,
        additional_span_processors=[SimpleSpanProcessor(capfire.exporter)],
        scrubbing=logfire.ScrubbingOptions(callback=scrub_tool_response_callback),
        inspect_arguments=False,
    )


def _run_instrumented_kb_tool_call(capfire: CaptureLogfire) -> dict[str, Any]:
    """Run one real agent tool call under instrumentation; return the tool span attrs.

    Instrumentation is scoped to the local Agent instance (not
    ``Agent.instrument_all``) so it cannot leak into other tests.
    """

    def kb_tool(ctx: RunContext[object]) -> str:
        return "Only authorized service partners may install this unit."

    def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="kb_tool", args={})],
            )
        return ModelResponse(parts=[TextPart(content="done")])

    agent: Agent[object, str] = Agent(
        name="mailpilot.scrub-contract",
        tools=[Tool(kb_tool, name="kb_tool")],
    )
    logfire.instrument_pydantic_ai(agent)
    agent.run_sync("go", model=FunctionModel(_respond))

    tool_spans = [
        span
        for span in capfire.exporter.exported_spans_as_dict()
        if span["name"].startswith("execute_tool")
    ]
    assert len(tool_spans) == 1, (
        f"expected one 'execute_tool' span, got {len(tool_spans)}"
    )
    return tool_spans[0]["attributes"]


def test_scrub_callback_preserves_tool_result_content(capfire: CaptureLogfire):
    """gen_ai.tool.call.result containing 'authorized' must survive scrubbing."""
    _reconfigure_with_scrubbing(capfire)

    attrs = _run_instrumented_kb_tool_call(capfire)
    rendered = str(attrs["gen_ai.tool.call.result"])
    assert "authorized" in rendered, rendered
    assert "[Scrubbed" not in rendered, rendered
    assert "logfire.scrubbed" not in attrs


def test_scrub_callback_default_active_on_other_attrs(capfire: CaptureLogfire):
    """Non-exempt paths must still flow through the default scrubber."""
    _reconfigure_with_scrubbing(capfire)

    with logfire.span("execute_tool kb_tool") as span:
        span.set_attribute("gen_ai.system_prompt", "password=xyz")

    spans = [
        span
        for span in capfire.exporter.exported_spans_as_dict()
        if span["name"] == "execute_tool kb_tool"
    ]
    assert len(spans) == 1
    attrs = spans[0]["attributes"]
    assert "[Scrubbed" in str(attrs["gen_ai.system_prompt"])
    assert "logfire.scrubbed" in attrs
