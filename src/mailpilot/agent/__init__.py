"""Pydantic AI agent for workflow execution.

This package separates agent construction from tool definitions and
classification. Files:

- ``__init__`` -- re-exports ``invoke_workflow_agent()``
- ``invoke`` -- agent construction, invocation, tool-use enforcement
- ``tools`` -- standalone tool functions (send_email, create_task, etc.)
- ``classify`` -- ``classify_email()`` structured output (not an agent)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import psycopg

    from mailpilot.models import Contact, Email, Workflow
    from mailpilot.settings import Settings


def invoke_workflow_agent(  # noqa: PLR0913
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
    workflow: Workflow,
    contact: Contact,
    email: Email | None = None,
    task_description: str = "",
    task_context: dict[str, Any] | None = None,
    model_override: object | None = None,
) -> dict[str, Any] | None:
    """Run the workflow's Pydantic AI agent for a contact.

    Thin re-export that defers the heavy import to avoid circular imports
    (agent -> tools -> sync -> routing -> agent).
    """
    from mailpilot.agent.invoke import invoke_workflow_agent as _invoke

    return _invoke(
        connection,
        settings,
        workflow,
        contact,
        email=email,
        task_description=task_description,
        task_context=task_context,
        model_override=model_override,
    )
