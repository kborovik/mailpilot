"""Workflow agent invocation.

Builds and runs a Pydantic AI agent for a given workflow + contact pair.
This is the central execution unit -- both inbound routing and outbound
campaigns culminate here.

Advisory locking: a PostgreSQL advisory lock prevents concurrent
invocations from racing on the same unit of work. When a ``task_id``
is supplied (drain path, §V.25), the lock is keyed on the task so
distinct tasks for the same ``(workflow_id, contact_id)`` pair can run
concurrently via the §V.23 worker pool. CLI paths
(``enrollment_run``/``manual``) omit ``task_id`` and fall back to the
coarse ``(workflow_id, contact_id)`` key, which preserves the original
"prevent operator-initiated double-run on same enrollment" guarantee.
If the lock is already held, the invocation is skipped before the
``agent.invoke`` span opens so loser-of-race calls do not pollute the
per-trigger count metric (§B.42).

Tool-use enforcement: the agent must call at least one tool per run.
``noop(reason)`` is the explicit "do nothing" escape hatch. A run with
zero tool calls raises ``AgentDidNotUseToolsError``.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import Any

import httpx
import logfire
import psycopg
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelRequest, ToolReturnPart
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.providers.anthropic import AnthropicProvider

from mailpilot import database
from mailpilot.agent import tools as agent_tools
from mailpilot.drive import DriveClient
from mailpilot.exceptions import (
    AgentCompletedWithoutReplyError,
    AgentDidNotUseToolsError,
)
from mailpilot.gmail import GmailClient
from mailpilot.models import Account, Contact, Email, Workflow
from mailpilot.operator_log import operator_event
from mailpilot.settings import Settings


@dataclass
class AgentDeps:
    """Dependencies injected into every agent tool via RunContext."""

    connection: psycopg.Connection[dict[str, Any]]
    account: Account
    gmail_client: GmailClient
    drive_client: DriveClient
    settings: Settings
    workflow_id: str
    contact_id: str
    enrollment_id: str


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


def _advisory_lock_keys_for_task(task_id: str) -> tuple[int, int]:
    """Compute two int32 advisory-lock keys from a task ID (§V.25).

    Splits the task ID at its midpoint and CRC-32s each half so the full
    UUID participates in both keys. Matches the 64-bit collision space of
    ``_advisory_lock_keys`` and keeps the same ``(int4, int4)`` shape so
    ``pg_try_advisory_lock`` can stay on the two-argument overload.
    """
    mid = len(task_id) // 2
    return (
        _to_signed_int32(zlib.crc32(task_id[:mid].encode())),
        _to_signed_int32(zlib.crc32(task_id[mid:].encode())),
    )


def _try_acquire_advisory_lock(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
    task_id: str | None = None,
) -> bool:
    """Try to acquire a session-level advisory lock. Non-blocking.

    When ``task_id`` is supplied, the lock is keyed on the task so concurrent
    drain workers handling distinct tasks for the same
    ``(workflow_id, contact_id)`` pair do not serialize on each other
    (§V.23 + §V.25). Otherwise the lock falls back to the coarse
    ``(workflow_id, contact_id)`` key used by synchronous CLI paths.

    Returns True if lock was acquired, False if already held elsewhere.
    """
    if task_id is not None:
        k1, k2 = _advisory_lock_keys_for_task(task_id)
    else:
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
    task_id: str | None = None,
) -> None:
    """Release a session-level advisory lock (mirrors _try_acquire scope)."""
    if task_id is not None:
        k1, k2 = _advisory_lock_keys_for_task(task_id)
    else:
        k1, k2 = _advisory_lock_keys(workflow_id, contact_id)
    connection.execute(
        "SELECT pg_advisory_unlock(%(k1)s, %(k2)s)",
        {"k1": k1, "k2": k2},
    )


# -- Tool wrappers -------------------------------------------------------------
# Thin functions that unpack AgentDeps from RunContext and delegate to the
# standalone tool functions in agent/tools.py.


def _wrap_send_email(  # noqa: PLR0913
    ctx: RunContext[AgentDeps],
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
) -> dict[str, Any]:
    """Send a new outbound email. For replies, use reply_email instead."""
    return agent_tools.send_email(
        connection=ctx.deps.connection,
        account=ctx.deps.account,
        gmail_client=ctx.deps.gmail_client,
        settings=ctx.deps.settings,
        workflow_id=ctx.deps.workflow_id,
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
    )


def _wrap_reply_email(
    ctx: RunContext[AgentDeps],
    email_id: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
) -> dict[str, Any]:
    """Reply to an existing email in-thread."""
    return agent_tools.reply_email(
        connection=ctx.deps.connection,
        account=ctx.deps.account,
        gmail_client=ctx.deps.gmail_client,
        settings=ctx.deps.settings,
        workflow_id=ctx.deps.workflow_id,
        email_id=email_id,
        body=body,
        cc=cc,
        bcc=bcc,
    )


def _wrap_create_task(
    ctx: RunContext[AgentDeps],
    description: str,
    scheduled_at: str,
    context: dict[str, Any] | None = None,
    email_id: str | None = None,
) -> dict[str, str]:
    """Schedule deferred work for the current contact."""
    return agent_tools.create_task(
        connection=ctx.deps.connection,
        enrollment_id=ctx.deps.enrollment_id,
        workflow_id=ctx.deps.workflow_id,
        contact_id=ctx.deps.contact_id,
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


def _wrap_record_enrollment_outcome(
    ctx: RunContext[AgentDeps],
    outcome: str,
    reason: str,
) -> dict[str, str]:
    """Record an enrollment outcome (completed or failed) on the timeline."""
    return agent_tools.record_enrollment_outcome(
        connection=ctx.deps.connection,
        enrollment_id=ctx.deps.enrollment_id,
        outcome=outcome,
        reason=reason,
    )


def _wrap_disable_contact(
    ctx: RunContext[AgentDeps],
    reason: str,
) -> dict[str, str]:
    """Set a global block on the current contact.

    `reason` is stored verbatim in `contact.disabled_reason`. Convention:
    prefix with `"bounced: "` or `"unsubscribed: "` so the operator can
    grep the source class. Once set, `send_email` and `reply_email`
    refuse this contact across every workflow.
    """
    return agent_tools.disable_contact(
        connection=ctx.deps.connection,
        contact_id=ctx.deps.contact_id,
        reason=reason,
    )


def _wrap_list_enrollments(
    ctx: RunContext[AgentDeps],
) -> list[dict[str, Any]]:
    """List enrollments in the current workflow with their outcome status."""
    return agent_tools.list_enrollments(
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
) -> dict[str, Any]:
    """Look up a contact by email address with inlined notes."""
    return agent_tools.read_contact(
        connection=ctx.deps.connection,
        email=email,
    )


def _wrap_read_company(
    ctx: RunContext[AgentDeps],
    domain: str,
) -> dict[str, Any]:
    """Look up a company by domain with inlined notes."""
    return agent_tools.read_company(
        connection=ctx.deps.connection,
        domain=domain,
    )


def _wrap_read_email(
    ctx: RunContext[AgentDeps],
    email_id: str,
) -> dict[str, Any] | None:
    """Read full email content (including body text) by ID."""
    return agent_tools.read_email(
        connection=ctx.deps.connection,
        account_id=ctx.deps.account.id,
        email_id=email_id,
    )


def _wrap_list_drive_markdown(
    ctx: RunContext[AgentDeps],
    folder_id: str,
) -> list[dict[str, str]] | dict[str, str]:
    """List Markdown files in a Drive folder for KB grounding."""
    return agent_tools.list_drive_markdown(
        drive_client=ctx.deps.drive_client,
        folder_id=folder_id,
    )


def _wrap_read_drive_markdown(
    ctx: RunContext[AgentDeps],
    file_id: str,
) -> dict[str, str]:
    """Read a Markdown file from Drive."""
    return agent_tools.read_drive_markdown(
        drive_client=ctx.deps.drive_client,
        file_id=file_id,
    )


def _wrap_search_drive_markdown(
    ctx: RunContext[AgentDeps],
    folder_id: str,
    query: str,
) -> list[dict[str, str]] | dict[str, str]:
    """Full-text search Markdown files in a Drive folder."""
    return agent_tools.search_drive_markdown(
        drive_client=ctx.deps.drive_client,
        folder_id=folder_id,
        query=query,
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


def _build_agent(workflow: Workflow, trigger: str = "manual") -> Agent[AgentDeps, str]:
    """Build a Pydantic AI agent for a workflow.

    The workflow's template (§V.44, §V.45) owns both the bound tool set and
    the system-prompt protocol. Workflow-specific instructions are appended
    to the template protocol. The deferred-task fragment branches on
    ``trigger`` per §V.31: ``trigger='task'`` uses the terminal-outcome
    instruction; other triggers use the initial-send-only instruction
    (prevents premature ``record_enrollment_outcome`` on first reach-out).
    """
    from mailpilot.agent.templates import TEMPLATES

    template = TEMPLATES[workflow.template]
    return Agent(
        name="mailpilot.workflow",
        deps_type=AgentDeps,
        instructions=template.build_protocol(trigger) + workflow.instructions,
        tools=list(template.tools),
    )


def _build_anthropic_model(settings: Settings) -> AnthropicModel:
    """Construct the AnthropicModel with §V.47 cache_control + §V.48 read-timeout.

    Cache breakpoints on the system prompt and tool definitions let
    multi-turn invocations re-bill the stable prefix as
    ``cache_read_input_tokens`` instead of fresh input.

    The HTTP client carries a 240s read-timeout (4x the httpx default of
    60s) so long-context Anthropic calls do not surface ``TimeoutError``
    mid-conversation, which would bubble to ``run.task.agent_failed``
    with no retry (idempotency: tool-call mid-turn cannot be safely
    re-driven). See SPEC.md §V.48, §B.16.

    ``anthropic_base_url`` is the wire endpoint. It defaults to
    ``api.anthropic.com``; pointing it at an Anthropic-compatible endpoint
    (e.g. ``https://api.novita.ai/anthropic``) routes the same Messages-API
    call to that vendor with no code change.
    """
    if not settings.anthropic_api_key:
        raise ValueError(
            "anthropic_api_key is required for agent invocation; "
            "set it via `mailpilot config set anthropic_api_key ...`",
        )
    return AnthropicModel(
        settings.anthropic_model,
        provider=AnthropicProvider(
            api_key=settings.anthropic_api_key,
            base_url=settings.anthropic_base_url,
            http_client=httpx.AsyncClient(timeout=httpx.Timeout(240.0)),
        ),
        settings=AnthropicModelSettings(
            anthropic_cache_tool_definitions=True,
            anthropic_cache_instructions=True,
        ),
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
    contact_email: str = "",
    trigger: str = "manual",
) -> str:
    """Format the trigger context section of the prompt.

    §V.30: framing matches the ``agent.invoke`` span ``trigger`` attribute
    (§V.26). ``enrollment_run`` and ``enrollment_schedule`` both render the
    first-reach-out block (identical framing -- both signify "first
    outbound message; no prior context"; §V.32 distinguishes them only at
    the observability layer). ``Deferred task:`` is reserved for
    ``trigger="task"``.
    """
    if email is not None:
        header = f"\nNew inbound email:\nEmail ID: {email.id}\nFrom: {contact_email}"
        return f"{header}\nSubject: {email.subject}\nBody:\n{email.body_text}"
    if trigger in ("enrollment_run", "enrollment_schedule"):
        return (
            "\nFirst reach-out for this enrollment. "
            "Compose the initial outbound message per the workflow objective "
            "and instructions."
        )
    if trigger == "task" and task_description:
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
    trigger: str = "manual",
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

    # §V.29: trigger email body is inlined under "New inbound email:" by
    # _format_trigger; exclude it from email_history so the body never appears
    # twice in a single prompt.
    prior_history = (
        [m for m in email_history if m.id != email.id]
        if email is not None
        else email_history
    )
    sections.append(_format_email_history(prior_history))
    sections.append(
        _format_trigger(
            email,
            task_description,
            task_context,
            contact_email=contact.email,
            trigger=trigger,
        )
    )

    return "\n".join(sections)


def _extract_tool_errors(result: Any) -> list[dict[str, str]]:
    """Walk the message ledger and collect tool returns that carry error payloads."""
    errors: list[dict[str, str]] = []
    for message in result.all_messages():
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if not isinstance(part, ToolReturnPart):
                continue
            content = part.content
            if isinstance(content, dict) and "error" in content:
                errors.append(
                    {
                        "tool": part.tool_name,
                        "error": str(content.get("error")),
                        "message": str(content.get("message", "")),
                    }
                )
    return errors


def _sent_reply(result: Any) -> bool:
    """Return True if the run sent a message or explicitly declined (§V.120).

    Walks the message ledger (mirrors :func:`_extract_tool_errors`) for a
    ``reply_email`` or ``send_email`` ToolReturnPart that carries no
    ``error`` key -- the message reached Gmail -- or a ``noop`` return, the
    explicit decline. Either satisfies the send obligation for both
    directions (inbound reply and outbound first reach-out); neither means
    the run left the message unsent while reporting success.
    """
    for message in result.all_messages():
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if not isinstance(part, ToolReturnPart):
                continue
            errored = isinstance(part.content, dict) and "error" in part.content
            if errored:
                continue
            if part.tool_name in ("reply_email", "send_email", "noop"):
                return True
    return False


# -- Main entry point ----------------------------------------------------------


def invoke_workflow_agent(  # noqa: PLR0913, PLR0915
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
    workflow: Workflow,
    contact: Contact,
    email: Email | None = None,
    task_description: str = "",
    task_context: dict[str, Any] | None = None,
    model_override: Model | str | None = None,
    trigger: str = "manual",
    task_id: str | None = None,
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
        trigger: Caller path label for the ``agent.invoke`` span. One of
            ``enrollment_run`` (CLI manual via ``mailpilot enrollment run``),
            ``enrollment_schedule`` (CLI-scheduled first-touch drained from
            the task queue per §V.32; framing identical to
            ``enrollment_run``, observability distinct), ``task``
            (background drain via ``run.execute_task``), ``email``
            (email-driven), or ``manual`` (default for direct programmatic
            calls). See SPEC §V.26.
        task_id: When the invocation is a drained task (§V.23 worker pool),
            the task's ID. Used as the advisory-lock key so concurrent
            workers handling distinct tasks for the same contact do not
            serialize on each other (§V.25). CLI paths omit this.

    Returns:
        Dict with invocation result, or None if skipped (lock held).

    Raises:
        AgentDidNotUseToolsError: If the agent completed without calling any tools.
    """
    # §V.25 / §B.42: acquire the advisory lock BEFORE opening the
    # ``agent.invoke`` span so loser-of-race calls do not emit a billable
    # parent span. The per-trigger ``agent.invoke`` count then reflects
    # real invocations, not noop attempts that bounce off the lock.
    if not _try_acquire_advisory_lock(
        connection, workflow.id, contact.id, task_id=task_id
    ):
        logfire.debug(
            "agent.invoke.skipped_lock_held",
            workflow_id=workflow.id,
            contact_id=contact.id,
            task_id=task_id,
        )
        return None

    with logfire.span(
        "agent.invoke",
        workflow_id=workflow.id,
        contact_id=contact.id,
        workflow_type=workflow.type,
        trigger=trigger,
    ) as span:
        # §V.26: surface the triggering email_id on the rollup span when the
        # invocation is email-driven. ``trigger='email'`` is the direct path;
        # ``trigger='task'`` flows through here when ``task.email_id`` is set
        # (caller in ``run.py`` loads the email and passes it via ``email=``).
        # Tasks with no associated email (e.g. scheduled first reach-out) leave
        # the attr absent so per-message rollups skip them cleanly.
        if email is not None and trigger in ("email", "task"):
            span.set_attribute("email_id", email.id)

        try:
            # Load account for this workflow.
            account = database.get_account(connection, workflow.account_id)
            if account is None:
                raise ValueError(
                    f"account not found for workflow: {workflow.account_id}"
                )

            # Resolve enrollment id from the (workflow_id, contact_id) UNIQUE
            # pair so tool wrappers can pass it through to ``create_task`` /
            # ``record_enrollment_outcome`` without asking the LLM for it.
            # The enrollment row is guaranteed present at this point: outbound
            # invocations create the enrollment in ``enrollment add``; inbound
            # invocations create it in ``routing._ensure_enrollment``.
            enrollment = database.get_enrollment(connection, workflow.id, contact.id)
            if enrollment is None:
                raise ValueError(
                    f"enrollment not found for workflow={workflow.id} "
                    f"contact={contact.id}"
                )

            # Load email history scoped to this workflow + contact. The
            # agent prompt needs ``body_text`` (not in EmailSummary), so
            # hydrate each summary via ``get_email``.
            email_summaries = database.list_emails(
                connection,
                contact_id=contact.id,
                account_id=account.id,
                workflow_id=workflow.id,
            )
            email_history = [
                full
                for full in (
                    database.get_email(connection, s.id) for s in email_summaries
                )
                if full is not None
            ]

            # Build agent and deps.
            agent = _build_agent(workflow, trigger=trigger)
            if model_override is not None:
                model = model_override
            else:
                model = _build_anthropic_model(settings)

            gmail_client = GmailClient(account.email)
            drive_client = DriveClient(account.email)
            deps = AgentDeps(
                connection=connection,
                account=account,
                gmail_client=gmail_client,
                drive_client=drive_client,
                settings=settings,
                workflow_id=workflow.id,
                contact_id=contact.id,
                enrollment_id=enrollment.id,
            )

            # Assemble prompt and run.
            prompt = _build_user_prompt(
                workflow=workflow,
                contact=contact,
                email_history=email_history,
                email=email,
                task_description=task_description,
                task_context=task_context,
                trigger=trigger,
            )

            span.set_attribute("prompt_length", len(prompt))

            result = agent.run_sync(prompt, model=model, deps=deps)

            # Scan tool returns for {"error": ...} payloads -- the agent may have
            # called a tool with bad arguments and ignored the rejection in its
            # final reasoning. Surface those failures so the orchestration sees them.
            tool_errors = _extract_tool_errors(result)

            if tool_errors:
                logfire.warn(
                    "agent.tool_errors",
                    workflow_id=workflow.id,
                    contact_id=contact.id,
                    tool_error_count=len(tool_errors),
                    tool_errors=tool_errors,
                )
            span.set_attribute("tool_error_count", len(tool_errors))

            # Usage tracking.
            usage = result.usage
            span.set_attribute("model", settings.anthropic_model)
            span.set_attribute("input_tokens", usage.input_tokens)
            span.set_attribute("output_tokens", usage.output_tokens)
            span.set_attribute("total_tokens", usage.input_tokens + usage.output_tokens)
            span.set_attribute("llm_requests", usage.requests)
            # §V.47: bubble Anthropic prompt-cache token counts to the rollup
            # span. Pydantic AI's RunUsage already sums these across child
            # chat turns, so no per-turn span walk is needed.
            span.set_attribute("cache_read_input_tokens", usage.cache_read_tokens)
            span.set_attribute("cache_creation_input_tokens", usage.cache_write_tokens)

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

            # §V.120 (per §B.106): a send-obligated run must finish in a send
            # or an explicit noop. The tool-count check above passes when the
            # model called only search/read tools and then narrated a message
            # it never sent, which leaves the work undone while reporting
            # success. Two trigger classes carry the obligation: an inbound
            # trigger (email present, trigger in {email, task}) answers via
            # reply_email, and an outbound first reach-out (no email, trigger
            # in {enrollment_run, enrollment_schedule}) opens via send_email.
            # ``manual`` stays exempt (direct programmatic / test path).
            inbound_send_obligated = email is not None and trigger in ("email", "task")
            outbound_send_obligated = email is None and trigger in (
                "enrollment_run",
                "enrollment_schedule",
            )
            if (inbound_send_obligated or outbound_send_obligated) and not _sent_reply(
                result
            ):
                logfire.warn(
                    "agent.completed_without_reply",
                    workflow_id=workflow.id,
                    contact_id=contact.id,
                    agent_output=result.output,
                )
                raise AgentCompletedWithoutReplyError(
                    f"send-obligated run completed without a send or noop: "
                    f"workflow={workflow.id}, contact={contact.id}, trigger={trigger}"
                )

            span.set_attribute("result", "completed")
            span.set_attribute("status", "completed")
            span.set_attribute("agent_reasoning", result.output)
            operator_event(
                "agent.run",
                workflow_id=workflow.id,
                contact_id=contact.id,
                status="completed",
                tool_calls=tool_call_count,
            )
            return {
                "workflow_id": workflow.id,
                "contact_id": contact.id,
                "status": "completed",
                "tool_calls": tool_call_count,
                "tool_errors": tool_errors,
                "reasoning": result.output,
            }

        except Exception:
            # Mirror the operator-log error event into the span so Logfire
            # queries can identify failed runs without joining through
            # task.status. The exception itself still propagates -- callers
            # (e.g. run.py) emit the operator-log ``event=error`` line.
            span.set_attribute("status", "failed")
            raise

        finally:
            _release_advisory_lock(connection, workflow.id, contact.id, task_id=task_id)
