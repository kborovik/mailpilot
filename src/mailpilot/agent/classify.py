"""Email classification via Pydantic AI structured output.

This is NOT an agent -- it's a single-turn LLM call with no tools.
Uses a fast/cheap model (e.g., Haiku) for routing decisions.
Architecturally separate from the agent to keep concerns distinct.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import TYPE_CHECKING

import logfire
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel
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
the email's content against each workflow's objective.

Rules:
- Pick the workflow whose objective is the best semantic match for the email.
- Return the workflow's exact id in the `workflow_id` field.
- If no workflow is a clear match, set `workflow_id` to null -- do not guess.
- Populate `reasoning` with one short sentence explaining the decision.

Candidate workflows will be provided in the user message.
"""

_AGENT: Agent[None, ClassificationResult] = Agent(
    output_type=ClassificationResult,
    instructions=_INSTRUCTIONS,
)


@lru_cache(maxsize=4)
def _get_model(api_key: str, model_name: str) -> AnthropicModel:
    """Cache the AnthropicModel/AnthropicProvider pair by (api_key, model_name)."""
    return AnthropicModel(model_name, provider=AnthropicProvider(api_key=api_key))


def classify_email(
    subject: str,
    body: str,
    sender: str,
    active_workflows: list[Workflow],
    settings: Settings,
) -> str | None:
    """Classify an inbound email to a workflow.

    Lightweight LLM call using Pydantic AI structured output (see ADR-04):
    - Input: email subject, body, sender + list of active workflows
      (name, objective)
    - Output: workflow_id or None (unrouted)
    - No tools, no agent -- pure routing decision

    When ``active_workflows`` is empty, the LLM is not invoked and None is
    returned. If the model hallucinates a ``workflow_id`` not in the
    candidate set, the result is also coerced to None.

    Args:
        subject: Email subject line.
        body: Email body (plain text).
        sender: Sender email address.
        active_workflows: Active workflows for the account (name, objective).
        settings: Application settings; supplies ``anthropic_api_key`` and
            ``anthropic_model``.

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

        model = _get_model(settings.anthropic_api_key, settings.anthropic_model)
        prompt = _format_prompt(subject, body, sender, active_workflows)
        result = _AGENT.run_sync(prompt, model=model)
        usage = result.usage()
        span.set_attribute("input_tokens", usage.input_tokens)
        span.set_attribute("output_tokens", usage.output_tokens)
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
                "objective": workflow.objective,
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
