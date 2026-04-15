"""Pydantic AI agent for workflow execution.

This package separates agent construction from tool definitions and
classification. Files:

- ``__init__`` -- ``invoke_workflow_agent()`` entry point
- ``tools`` -- ``@tool`` decorated functions (send_email, create_task, etc.)
- ``classify`` -- ``classify_email()`` structured output (not an agent)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mailpilot.models import Email, Workflow


async def invoke_workflow_agent(
    workflow: Workflow,
    email: Email | None = None,
    task_description: str = "",
    task_context: dict[str, Any] | None = None,
) -> None:
    """Run the workflow's Pydantic AI agent.

    The agent is stateless -- each invocation gets fresh context from the
    database. It makes all business decisions: what to send, when to follow
    up, when to give up.

    Three trigger types:
        - Email arrives: ``email`` is set, agent processes the inbound message
        - Task due: ``task_description`` + ``task_context`` are set
        - Manual send: called from CLI with contact list via workflow

    Args:
        workflow: Workflow with instructions (system prompt) and objective.
        email: Triggering inbound email, if any.
        task_description: Deferred task description, if triggered by task runner.
        task_context: Arbitrary JSON context from the task row.
    """
    raise NotImplementedError
