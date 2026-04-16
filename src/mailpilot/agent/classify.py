"""Email classification via Pydantic AI structured output.

This is NOT an agent -- it's a single-turn LLM call with no tools.
Uses a fast/cheap model (e.g., Haiku) for routing decisions.
Architecturally separate from the agent to keep concerns distinct.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mailpilot.models import Workflow


def classify_email(
    subject: str,
    body: str,
    sender: str,
    active_workflows: list[Workflow],
) -> str | None:
    """Classify an inbound email to a workflow.

    Lightweight LLM call using Pydantic AI structured output (see ADR-04):
    - Input: email subject, body, sender + list of active workflows
      (name, objective)
    - Output: workflow_id or None (unrouted)
    - No tools, no agent -- pure routing decision

    Args:
        subject: Email subject line.
        body: Email body (plain text).
        sender: Sender email address.
        active_workflows: Active workflows for the account
            (name, objective).

    Returns:
        Workflow ID if classified, None if unrouted.
    """
    raise NotImplementedError
