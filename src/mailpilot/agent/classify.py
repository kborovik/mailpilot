"""Email classification via Pydantic AI structured output.

This is NOT an agent -- it's a single-turn LLM call with no tools.
Uses a fast/cheap model for routing decisions.
Architecturally separate from the agent to keep concerns distinct.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import logfire
from pydantic import BaseModel
from pydantic_ai import Agent

from mailpilot.agent.model import (
    active_model_name,
    build_model,
)

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
        settings: Application settings; supplies ``llm_provider`` and the
            active provider's API key + model id (§V.47).

    Returns:
        Workflow ID if classified, None if unrouted.

    Raises:
        ValueError: If the active provider's API key is empty.
    """
    with logfire.span(
        "agent.classify_email",
        sender=sender,
        candidate_count=len(active_workflows),
    ) as span:
        if not active_workflows:
            span.set_attribute("result", "no_candidates")
            return None

        model = build_model(settings, role="classifier")
        prompt = _format_prompt(subject, body, sender, active_workflows)
        result = _AGENT.run_sync(prompt, model=model)
        usage = result.usage
        model_name = active_model_name(settings)
        span.set_attribute("model", model_name)
        span.set_attribute("llm_provider", settings.llm_provider)
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
