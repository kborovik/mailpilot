"""Email classification via Pydantic AI structured output.

This is NOT an agent -- it's a single-turn LLM call with no tools.
Uses a fast/cheap model (e.g., Haiku) for routing decisions.
Architecturally separate from the agent to keep concerns distinct.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import TYPE_CHECKING

import httpx
import logfire
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.providers.anthropic import AnthropicProvider

if TYPE_CHECKING:
    from mailpilot.models import Workflow
    from mailpilot.settings import Settings


_MAX_BODY_CHARS = 16384


class ClassificationResult(BaseModel):
    """Structured output returned by the classifier LLM call."""

    workflow_id: str | None = None
    reasoning: str = ""


_INSTRUCTIONS = """\
You route an inbound email to one of the candidate workflows by matching
the email's content against each workflow's goal.

Rules:
- Pick the workflow whose goal is the best semantic match for the email.
- A goal may name where NOT to route (for example "send X to Y instead");
  honor those explicit redirect hints over surface word overlap.
- Return the workflow's exact id in the `workflow_id` field.
- If no workflow is a clear match, set `workflow_id` to null -- do not guess.
- Populate `reasoning` with one short sentence explaining the decision.

Candidate workflows will be provided in the user message.
"""

_AGENT: Agent[object, ClassificationResult] = Agent(
    name="mailpilot.classifier",
    output_type=ClassificationResult,
    instructions=_INSTRUCTIONS,
)


@lru_cache(maxsize=4)
def _get_model(api_key: str, model_name: str, base_url: str) -> AnthropicModel:
    """Cache the AnthropicModel/AnthropicProvider by api_key, model_name, base_url.

    §V.47: cache_control breakpoints on the system prompt and tool
    definitions let repeated classifier calls re-bill the stable prefix as
    ``cache_read_input_tokens``.

    §V.48: 240s read-timeout on the HTTP client (4x the httpx default of
    60s) so long-context classifier calls do not surface ``TimeoutError``.
    See SPEC.md §V.48, §B.16.

    ``base_url`` is the wire endpoint and rides the cache key, so a base-URL
    change rebuilds the cached model. It defaults to ``api.anthropic.com``;
    an Anthropic-compatible endpoint (e.g. ``https://api.novita.ai/anthropic``)
    routes the same call to that vendor.
    """
    return AnthropicModel(
        model_name,
        provider=AnthropicProvider(
            api_key=api_key,
            base_url=base_url,
            http_client=httpx.AsyncClient(timeout=httpx.Timeout(240.0)),
        ),
        settings=AnthropicModelSettings(
            anthropic_cache_tool_definitions=True,
            anthropic_cache_instructions=True,
        ),
    )


def classify_email(
    subject: str,
    body: str,
    sender: str,
    active_workflows: list[Workflow],
    settings: Settings,
) -> str | None:
    """Classify an inbound email to a workflow.

    Lightweight LLM call using Pydantic AI structured output (see §V.27):
    - Input: email subject, body, sender + list of active workflows
      (name, goal)
    - Output: workflow_id or None (unrouted)
    - No tools, no agent -- pure routing decision

    When ``active_workflows`` is empty, the LLM is not invoked and None is
    returned. If the model hallucinates a ``workflow_id`` not in the
    candidate set, the result is also coerced to None.

    Args:
        subject: Email subject line.
        body: Email body (plain text).
        sender: Sender email address.
        active_workflows: Active workflows for the account (name, goal).
        settings: Application settings; supplies ``anthropic_api_key``,
            ``anthropic_model``, and ``anthropic_base_url``.

    Returns:
        Workflow ID if classified, None if unrouted.

    Raises:
        ValueError: If ``settings.anthropic_api_key`` is empty.
    """
    with logfire.span(
        "agent.classify_email",
        sender=sender,
        candidate_count=len(active_workflows),
    ) as span:
        if not active_workflows:
            span.set_attribute("result", "no_candidates")
            return None

        if not settings.anthropic_api_key:
            raise ValueError(
                "anthropic_api_key is required for classification; "
                "set it via `mailpilot config set anthropic_api_key ...`",
            )

        model = _get_model(
            settings.anthropic_api_key,
            settings.anthropic_model,
            settings.anthropic_base_url,
        )
        prompt = _format_prompt(subject, body, sender, active_workflows)
        result = _AGENT.run_sync(prompt, model=model)
        usage = result.usage
        span.set_attribute("model", settings.anthropic_model)
        span.set_attribute("input_tokens", usage.input_tokens)
        span.set_attribute("output_tokens", usage.output_tokens)
        span.set_attribute("total_tokens", usage.input_tokens + usage.output_tokens)
        output = result.output
        span.set_attribute("reasoning", output.reasoning)
        candidate_ids = {workflow.id for workflow in active_workflows}
        if output.workflow_id is None or output.workflow_id not in candidate_ids:
            span.set_attribute("result", "no_match")
            return None
        span.set_attribute("result", "match")
        span.set_attribute("workflow_id", output.workflow_id)
        return output.workflow_id


def _format_prompt(
    subject: str,
    body: str,
    sender: str,
    active_workflows: list[Workflow],
) -> str:
    """Render the user prompt for the classifier LLM call."""
    workflows_json = json.dumps(
        [
            {
                "id": workflow.id,
                "name": workflow.name,
                "goal": workflow.goal,
            }
            for workflow in active_workflows
        ],
        indent=2,
    )
    truncated_body = body[:_MAX_BODY_CHARS]
    return (
        f"Candidate workflows (JSON):\n{workflows_json}\n\n"
        f"Email:\n"
        f"From: {sender}\n"
        f"Subject: {subject}\n\n"
        f"{truncated_body}"
    )
