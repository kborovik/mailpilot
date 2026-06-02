"""Span-emission contract tests for the Logfire scrubbing callback (§V.55).

The default Logfire scrubber redacts attribute values that contain substrings
like ``"auth"`` or ``"password"``. Per §V.55, ``tool_response`` payloads on
Pydantic-AI ``running tool`` spans must be exempt so KB grounding regressions
remain verifiable from traces alone. Other attribute paths must continue to
flow through the default scrubber.
"""

from __future__ import annotations

from typing import Any

import logfire
from logfire.testing import CaptureLogfire
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

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


def _running_tool_attrs(capfire: CaptureLogfire) -> dict[str, Any]:
    spans = [
        span
        for span in capfire.exporter.exported_spans_as_dict()
        if span["name"] == "running tool"
    ]
    assert len(spans) == 1, f"expected one 'running tool' span, got {len(spans)}"
    return spans[0]["attributes"]


def test_scrub_callback_preserves_tool_response_content(capfire: CaptureLogfire):
    """tool_response containing 'authorized' must survive scrubbing intact."""
    _reconfigure_with_scrubbing(capfire)

    payload = {"content": "Only authorized service partners may install this unit."}
    with logfire.span("running tool") as span:
        span.set_attribute("tool_response", payload)

    attrs = _running_tool_attrs(capfire)
    rendered = str(attrs["tool_response"])
    assert "authorized" in rendered, rendered
    assert "[Scrubbed" not in rendered, rendered
    assert "logfire.scrubbed" not in attrs


def test_scrub_callback_default_active_on_other_attrs(capfire: CaptureLogfire):
    """Non-tool_response paths must still flow through the default scrubber."""
    _reconfigure_with_scrubbing(capfire)

    with logfire.span("running tool") as span:
        span.set_attribute("gen_ai.system_prompt", "password=xyz")

    attrs = _running_tool_attrs(capfire)
    assert "[Scrubbed" in str(attrs["gen_ai.system_prompt"])
    assert "logfire.scrubbed" in attrs
