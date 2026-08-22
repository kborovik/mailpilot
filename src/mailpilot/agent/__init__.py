"""Pydantic AI agent for workflow execution.

This package separates agent construction from tool definitions and
classification. Files:

- ``__init__`` -- re-exports ``invoke_workflow_agent()``
- ``invoke`` -- agent construction, invocation, tool-use enforcement
- ``tools`` -- standalone tool functions (send_email, create_task, etc.)
- ``classify`` -- ``classify_email()`` structured output (not an agent)
"""

from mailpilot.agent.invoke import invoke_workflow_agent

__all__ = ["invoke_workflow_agent"]
