"""Workflow agent invocation.

Builds and runs a Pydantic AI agent for a given workflow + contact pair.
This is the central execution unit -- both inbound routing and outbound
campaigns culminate here.

Advisory locking: a PostgreSQL advisory lock keyed on
``(workflow_id, contact_id)`` prevents concurrent invocations for the
same pair. If the lock is already held, the invocation is skipped.

Tool-use enforcement: the agent must call at least one tool per run.
``noop(reason)`` is the explicit "do nothing" escape hatch. A run with
zero tool calls raises ``AgentDidNotUseToolsError``.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import Any

import logfire
import psycopg
from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.models import Model

from mailpilot import database
from mailpilot.agent import tools as agent_tools
from mailpilot.exceptions import AgentDidNotUseToolsError
from mailpilot.gmail import GmailClient
from mailpilot.models import Account, Contact, Email, Workflow
from mailpilot.settings import Settings


@dataclass
class AgentDeps:
    """Dependencies injected into every agent tool via RunContext."""

    connection: psycopg.Connection[dict[str, Any]]
    account: Account
    gmail_client: GmailClient
    settings: Settings
    workflow_id: str


# -- Advisory lock -------------------------------------------------------------


def _to_signed_int32(value: int) -> int:
    """Convert an unsigned 32-bit integer to a signed 32-bit integer."""
    if value >= 0x80000000:
        return value - 0x100000000
    return value


def _advisory_lock_keys(workflow_id: str, contact_id: str) -> tuple[int, int]:
    """Compute two int32 keys for PostgreSQL two-argument advisory locking.

    Uses CRC-32 of each ID independently, giving 64 bits of collision space
    (one CRC-32 per dimension) instead of 32 bits from a single combined hash.
    Values are converted to signed int32 to match PostgreSQL's integer type.
    """
    return (
        _to_signed_int32(zlib.crc32(workflow_id.encode())),
        _to_signed_int32(zlib.crc32(contact_id.encode())),
    )


def _try_acquire_advisory_lock(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> bool:
    """Try to acquire a session-level advisory lock. Non-blocking.

    Returns True if lock was acquired, False if already held elsewhere.
    """
    k1, k2 = _advisory_lock_keys(workflow_id, contact_id)
    row = connection.execute(
        "SELECT pg_try_advisory_lock(%(k1)s, %(k2)s) AS acquired",
        {"k1": k1, "k2": k2},
    ).fetchone()
    return bool(row and row["acquired"])


def _release_advisory_lock(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> None:
    """Release a session-level advisory lock."""
    k1, k2 = _advisory_lock_keys(workflow_id, contact_id)
    connection.execute(
        "SELECT pg_advisory_unlock(%(k1)s, %(k2)s)",
        {"k1": k1, "k2": k2},
    )


# -- Tool wrappers -------------------------------------------------------------
# Thin functions that unpack AgentDeps from RunContext and delegate to the
# standalone tool functions in agent/tools.py.


def _wrap_send_email(
    ctx: RunContext[AgentDeps],
    to: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Send an email via Gmail API."""
    return agent_tools.send_email(
        connection=ctx.deps.connection,
        account=ctx.deps.account,
        gmail_client=ctx.deps.gmail_client,
        settings=ctx.deps.settings,
        workflow_id=ctx.deps.workflow_id,
        to=to,
        subject=subject,
        body=body,
        thread_id=thread_id,
    )


def _wrap_create_task(  # noqa: PLR0913
    ctx: RunContext[AgentDeps],
    contact_id: str,
    description: str,
    scheduled_at: str,
    context: dict[str, Any] | None = None,
    email_id: str | None = None,
) -> dict[str, str]:
    """Schedule deferred work for later execution."""
    return agent_tools.create_task(
        connection=ctx.deps.connection,
        workflow_id=ctx.deps.workflow_id,
        contact_id=contact_id,
        description=description,
        scheduled_at=scheduled_at,
        context=context,
        email_id=email_id,
    )


def _wrap_cancel_task(
    ctx: RunContext[AgentDeps],
    task_id: str,
) -> dict[str, str]:
    """Cancel a pending task."""
    return agent_tools.cancel_task(
        connection=ctx.deps.connection,
        task_id=task_id,
    )


def _wrap_update_contact_status(
    ctx: RunContext[AgentDeps],
    contact_id: str,
    status: str,
    reason: str,
) -> dict[str, str]:
    """Report outcome for a contact in the current workflow."""
    return agent_tools.update_contact_status(
        connection=ctx.deps.connection,
        workflow_id=ctx.deps.workflow_id,
        contact_id=contact_id,
        status=status,
        reason=reason,
    )


def _wrap_disable_contact(
    ctx: RunContext[AgentDeps],
    contact_id: str,
    status: str,
    reason: str,
) -> dict[str, str]:
    """Set a global block on a contact (bounced or unsubscribed)."""
    return agent_tools.disable_contact(
        connection=ctx.deps.connection,
        contact_id=contact_id,
        status=status,
        reason=reason,
    )


def _wrap_list_workflow_contacts(
    ctx: RunContext[AgentDeps],
) -> list[dict[str, Any]]:
    """List contacts in the current workflow with their outcome status."""
    return agent_tools.list_workflow_contacts(
        connection=ctx.deps.connection,
        workflow_id=ctx.deps.workflow_id,
    )


def _wrap_search_emails(
    ctx: RunContext[AgentDeps],
    query: str,
) -> list[dict[str, Any]]:
    """Search email history for the current account."""
    return agent_tools.search_emails(
        connection=ctx.deps.connection,
        account_id=ctx.deps.account.id,
        query=query,
    )


def _wrap_read_contact(
    ctx: RunContext[AgentDeps],
    email: str,
) -> dict[str, Any] | None:
    """Look up a contact by email address."""
    return agent_tools.read_contact(
        connection=ctx.deps.connection,
        email=email,
    )


def _wrap_read_company(
    ctx: RunContext[AgentDeps],
    domain: str,
) -> dict[str, Any] | None:
    """Look up a company by domain."""
    return agent_tools.read_company(
        connection=ctx.deps.connection,
        domain=domain,
    )


def _wrap_noop(
    ctx: RunContext[AgentDeps],
    reason: str,
) -> dict[str, Any]:
    """Explicitly decline to act.

    Call this tool when, after reviewing context, no action is appropriate.
    You must still call a tool every turn -- noop is the explicit "do nothing"
    signal.
    """
    return agent_tools.noop(reason=reason)


# -- Agent construction --------------------------------------------------------


_TOOLS: list[Tool[AgentDeps]] = [
    Tool(_wrap_send_email, name="send_email"),
    Tool(_wrap_create_task, name="create_task"),
    Tool(_wrap_cancel_task, name="cancel_task"),
    Tool(_wrap_update_contact_status, name="update_contact_status"),
    Tool(_wrap_disable_contact, name="disable_contact"),
    Tool(_wrap_list_workflow_contacts, name="list_workflow_contacts"),
    Tool(_wrap_search_emails, name="search_emails"),
    Tool(_wrap_read_contact, name="read_contact"),
    Tool(_wrap_read_company, name="read_company"),
    Tool(_wrap_noop, name="noop"),
]


_SYSTEM_PREFIX = (
    "Respond in plain text only. No markdown, no tables, no bullet "
    "points, no bold/italic, no emojis. Keep your final summary brief "
    "(2-3 sentences).\n\n"
)


def _build_agent(workflow: Workflow) -> Agent[AgentDeps, str]:
    """Build a Pydantic AI agent for a workflow."""
    return Agent(
        deps_type=AgentDeps,
        instructions=_SYSTEM_PREFIX + workflow.instructions,
        tools=_TOOLS,
    )


# -- Prompt assembly -----------------------------------------------------------


def _format_email_history(email_history: list[Email]) -> str:
    """Format email history for the agent prompt."""
    if not email_history:
        return "\nNo prior email history with this contact."
    lines = [f"\nEmail history ({len(email_history)} messages):"]
    for msg in email_history:
        direction = "SENT" if msg.direction == "outbound" else "RECEIVED"
        lines.append(f"  [{direction}] {msg.subject}")
        if msg.body_text:
            body_preview = msg.body_text[:500]
            if len(msg.body_text) > 500:
                body_preview += "..."
            lines.append(f"  {body_preview}")
    return "\n".join(lines)


def _format_trigger(
    email: Email | None,
    task_description: str,
    task_context: dict[str, Any] | None,
) -> str:
    """Format the trigger context section of the prompt."""
    if email is not None:
        return (
            f"\nNew inbound email:\nSubject: {email.subject}\nBody:\n{email.body_text}"
        )
    if task_description:
        lines = ["\nDeferred task:", f"Description: {task_description}"]
        if task_context:
            lines.append(f"Context: {task_context}")
        return "\n".join(lines)
    return (
        "\nThis is an outbound invocation. "
        "Review the contact and email history, then take appropriate action."
    )


def _build_user_prompt(  # noqa: PLR0913
    workflow: Workflow,
    contact: Contact,
    email_history: list[Email],
    email: Email | None = None,
    task_description: str = "",
    task_context: dict[str, Any] | None = None,
) -> str:
    """Assemble the user prompt for the agent."""
    sections: list[str] = [
        f"Workflow: {workflow.name}",
        f"Objective: {workflow.objective}",
        f"Type: {workflow.type}",
        f"\nContact: {contact.email}",
    ]

    if contact.first_name or contact.last_name:
        name = f"{contact.first_name or ''} {contact.last_name or ''}".strip()
        sections.append(f"Name: {name}")
    if contact.position:
        sections.append(f"Position: {contact.position}")
    if contact.domain:
        sections.append(f"Domain: {contact.domain}")

    sections.append(_format_email_history(email_history))
    sections.append(_format_trigger(email, task_description, task_context))

    return "\n".join(sections)


# -- Main entry point ----------------------------------------------------------


def invoke_workflow_agent(  # noqa: PLR0913
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
    workflow: Workflow,
    contact: Contact,
    email: Email | None = None,
    task_description: str = "",
    task_context: dict[str, Any] | None = None,
    model_override: Model | str | None = None,
) -> dict[str, Any] | None:
    """Run the workflow's Pydantic AI agent for a contact.

    The agent is stateless -- each invocation gets fresh context from the
    database.

    Args:
        connection: Open database connection.
        settings: Application settings (API keys, model config).
        workflow: Workflow with instructions (system prompt) and objective.
        contact: Target contact.
        email: Triggering inbound email, if any.
        task_description: Deferred task description, if triggered by task runner.
        task_context: Arbitrary JSON context from the task row.
        model_override: Override the LLM model (for testing with FunctionModel).

    Returns:
        Dict with invocation result, or None if skipped (lock held).

    Raises:
        AgentDidNotUseToolsError: If the agent completed without calling any tools.
    """
    with logfire.span(
        "agent.invoke",
        workflow_id=workflow.id,
        contact_id=contact.id,
        workflow_type=workflow.type,
        trigger="email" if email else ("task" if task_description else "manual"),
    ) as span:
        # Acquire advisory lock.
        if not _try_acquire_advisory_lock(connection, workflow.id, contact.id):
            logfire.debug(
                "agent.invoke.skipped_lock_held",
                workflow_id=workflow.id,
                contact_id=contact.id,
            )
            span.set_attribute("result", "skipped_lock_held")
            return None

        try:
            # Load account for this workflow.
            account = database.get_account(connection, workflow.account_id)
            if account is None:
                raise ValueError(
                    f"account not found for workflow: {workflow.account_id}"
                )

            # Load email history between account and contact.
            email_history = database.list_emails(
                connection,
                contact_id=contact.id,
                account_id=account.id,
            )

            # Build agent and deps.
            agent = _build_agent(workflow)
            if model_override is not None:
                model = model_override
            else:
                from pydantic_ai.models.anthropic import AnthropicModel
                from pydantic_ai.providers.anthropic import AnthropicProvider

                if not settings.anthropic_api_key:
                    raise ValueError(
                        "anthropic_api_key is required for agent invocation; "
                        "set it via `mailpilot config set anthropic_api_key ...`",
                    )
                model = AnthropicModel(
                    settings.anthropic_model,
                    provider=AnthropicProvider(api_key=settings.anthropic_api_key),
                )

            gmail_client = GmailClient(account.email)
            deps = AgentDeps(
                connection=connection,
                account=account,
                gmail_client=gmail_client,
                settings=settings,
                workflow_id=workflow.id,
            )

            # Assemble prompt and run.
            prompt = _build_user_prompt(
                workflow=workflow,
                contact=contact,
                email_history=email_history,
                email=email,
                task_description=task_description,
                task_context=task_context,
            )

            span.set_attribute("prompt_length", len(prompt))

            result = agent.run_sync(prompt, model=model, deps=deps)

            # Usage tracking.
            usage = result.usage()
            span.set_attribute("input_tokens", usage.input_tokens)
            span.set_attribute("output_tokens", usage.output_tokens)
            span.set_attribute("llm_requests", usage.requests)

            # Tool-use enforcement.
            tool_call_count = usage.tool_calls
            span.set_attribute("tool_call_count", tool_call_count)

            if tool_call_count == 0:
                logfire.warn(
                    "agent.no_tools_called",
                    workflow_id=workflow.id,
                    contact_id=contact.id,
                    agent_output=result.output,
                )
                raise AgentDidNotUseToolsError(
                    f"agent completed without calling any tools: "
                    f"workflow={workflow.id}, contact={contact.id}"
                )

            span.set_attribute("result", "completed")
            span.set_attribute("agent_reasoning", result.output)
            return {
                "workflow_id": workflow.id,
                "contact_id": contact.id,
                "status": "completed",
                "tool_calls": tool_call_count,
                "reasoning": result.output,
            }

        finally:
            _release_advisory_lock(connection, workflow.id, contact.id)
