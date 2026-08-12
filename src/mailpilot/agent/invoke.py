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
from datetime import UTC, date, datetime
from typing import Any

import logfire
import psycopg
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.messages import ModelRequest, ToolReturnPart
from pydantic_ai.models import Model

from mailpilot import cadence, database, email_ops
from mailpilot.agent import tools as agent_tools
from mailpilot.agent.model import (
    _build_model,  # pyright: ignore[reportPrivateUsage]
    active_model_name,
)
from mailpilot.drive import DriveClient
from mailpilot.exceptions import (
    AgentCompletedWithoutReplyError,
    AgentDidNotUseToolsError,
)
from mailpilot.gmail import GmailClient
from mailpilot.models import (
    Account,
    CompanyView,
    Contact,
    ContactView,
    Email,
    Enrollment,
    TouchMessage,
    Workflow,
)
from mailpilot.operator_log import operator_event
from mailpilot.settings import Settings

# Compose-only output validators (§V.42 body lint + §V.136 first-touch subject)
# use a bounded ModelRetry so a bad draft is re-composed a capped number of
# times before the run fails terminally (mirrors the §V.71 tool-path rejection
# cap).
_TOUCH_VALIDATION_RETRIES = 2

# §V.136 / §B.127: ModelRetry message when a new-thread touch omits subject.
_SUBJECT_REQUIRED_RETRY = "subject required for new thread"


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


def _wrap_send_email(  # noqa: PLR0913  # pyright: ignore[reportUnusedFunction]
    ctx: RunContext[AgentDeps],
    to: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
    cc: str | None = None,
    bcc: str | None = None,
) -> dict[str, Any]:
    """Send a new outbound email. For replies, use reply_email instead.

    Pass thread_id (the gmail_thread_id returned by an earlier touch) to
    continue a multi-touch outbound thread; omit it for a first reach-out.
    """
    # thread_id forwards outbound thread-continuation per §V.78.
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
        cc=cc,
        bcc=bcc,
    )


def _wrap_reply_email(  # pyright: ignore[reportUnusedFunction]
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


def _wrap_create_task(  # pyright: ignore[reportUnusedFunction]
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


def _wrap_cancel_task(  # pyright: ignore[reportUnusedFunction]
    ctx: RunContext[AgentDeps],
    task_id: str,
) -> dict[str, str]:
    """Cancel a pending task."""
    return agent_tools.cancel_task(
        connection=ctx.deps.connection,
        task_id=task_id,
    )


def _wrap_conclude_enrollment(  # pyright: ignore[reportUnusedFunction]
    ctx: RunContext[AgentDeps],
    disposition: str,
    note: str,
    reschedule_at: str | None = None,
) -> dict[str, Any]:
    """Conclude the current enrollment with one terminal disposition.

    `disposition` is one of meeting_booked, do_not_contact, contact_later.
    The system records the outcome, cancels pending follow-ups, and -- per
    disposition -- disables the contact or schedules a re-enrollment, then
    writes a note. This is a terminal action that satisfies the send
    obligation, like noop.

    Use do_not_contact for opt-out, wrong person, retired / left-the-company
    auto-replies, and address-change / "update your records" / hard
    email-redirect auto-replies: stop touches to the enrolled address even
    if From uses a different local-part; put the redirect, referral
    addresses, and the new email (when present) in `note`; never enroll
    the From alias or any new address. Out-of-office auto-replies are not
    this path -- call noop instead.
    """
    return agent_tools.conclude_enrollment(
        connection=ctx.deps.connection,
        enrollment_id=ctx.deps.enrollment_id,
        disposition=disposition,
        note=note,
        reschedule_at=reschedule_at,
    )


def _wrap_disable_contact(  # pyright: ignore[reportUnusedFunction]
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


def _wrap_list_enrollments(  # pyright: ignore[reportUnusedFunction]
    ctx: RunContext[AgentDeps],
) -> list[dict[str, Any]]:
    """List enrollments in the current workflow with their outcome status."""
    return agent_tools.list_enrollments(
        connection=ctx.deps.connection,
        workflow_id=ctx.deps.workflow_id,
    )


def _wrap_search_emails(  # pyright: ignore[reportUnusedFunction]
    ctx: RunContext[AgentDeps],
    query: str,
) -> list[dict[str, Any]]:
    """Search email history for the current account."""
    return agent_tools.search_emails(
        connection=ctx.deps.connection,
        account_id=ctx.deps.account.id,
        query=query,
    )


def _wrap_read_email(  # pyright: ignore[reportUnusedFunction]
    ctx: RunContext[AgentDeps],
    email_id: str,
) -> dict[str, Any] | None:
    """Read full email content (including body text) by ID."""
    return agent_tools.read_email(
        connection=ctx.deps.connection,
        account_id=ctx.deps.account.id,
        email_id=email_id,
    )


def _wrap_list_drive_markdown(  # pyright: ignore[reportUnusedFunction]
    ctx: RunContext[AgentDeps],
    folder_id: str,
) -> list[dict[str, str]] | dict[str, str]:
    """List Markdown files in a Drive folder for KB grounding."""
    return agent_tools.list_drive_markdown(
        drive_client=ctx.deps.drive_client,
        folder_id=folder_id,
    )


def _wrap_read_drive_markdown(  # pyright: ignore[reportUnusedFunction]
    ctx: RunContext[AgentDeps],
    file_id: str,
) -> dict[str, str]:
    """Read a Markdown file from Drive."""
    return agent_tools.read_drive_markdown(
        drive_client=ctx.deps.drive_client,
        file_id=file_id,
    )


def _wrap_search_drive_markdown(  # pyright: ignore[reportUnusedFunction]
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


def _wrap_noop(  # pyright: ignore[reportUnusedFunction]
    ctx: RunContext[AgentDeps],
    reason: str,
) -> dict[str, Any]:
    """Explicitly decline to act.

    Call this tool when, after reviewing context, no action is appropriate.
    You must still call a tool every turn -- noop is the explicit "do nothing"
    signal. Typical case: out-of-office or temporary absence auto-reply
    (pause once; leave enrollment open; do not conclude). Address-change
    and hard email-redirect auto-replies are not noop -- use
    conclude_enrollment with do_not_contact instead.
    """
    return agent_tools.noop(reason=reason)


# -- Agent construction --------------------------------------------------------


def _build_agent(workflow: Workflow, trigger: str = "manual") -> Agent[AgentDeps, str]:
    """Build a Pydantic AI agent for a workflow.

    The workflow's template (§V.44, §V.45) owns both the bound tool set and
    the system-prompt protocol. Workflow-specific instructions are appended
    to the template protocol. The deferred-task fragment branches on direction
    and ``trigger`` per §V.31: an inbound template uses the inbound-reply
    instruction for every trigger (reply once, then stop; the system records
    the outcome); an outbound template uses the terminal-outcome instruction on
    ``trigger='task'`` and the initial-send-only instruction otherwise (which
    prevents a premature ``conclude_enrollment`` terminal on first reach-out).
    """
    from mailpilot.agent.templates import TEMPLATES

    template = TEMPLATES[workflow.template]
    agent = Agent(
        name="mailpilot.workflow",
        deps_type=AgentDeps,
        instructions=template.build_protocol(trigger) + workflow.instructions,
        tools=list(template.tools),
    )

    # §V.129 PREVENT: a dynamic instruction re-evaluated at run-start injects
    # the current date so the model grounds a relative schedule ("about one
    # month out") on a real ``now`` and never guesses the year. ``date.today()``
    # rolls once per day -- far slower than the Anthropic prompt-cache TTL
    # (§V.47) -- so the static protocol prefix still caches across same-day
    # runs. The model-visible string carries no SPEC citation (§V.45).
    @agent.instructions
    def _ground_current_date() -> str:  # pyright: ignore[reportUnusedFunction]
        return (
            f"The current date is {date.today().isoformat()}. "
            "Ground every relative schedule on this date and never guess the year."
        )

    return agent


def _validate_touch_subject(
    subject: str | None,
    *,
    require_subject: bool,
) -> None:
    """Raise ``ModelRetry`` when a new-thread touch lacks a non-empty subject.

    §V.136 / §B.127: first-touch (new-thread) compose-only output must carry a
    subject after strip; ``None`` / ``""`` / whitespace-only triggers a bounded
    ModelRetry. Follow-ups that continue an existing thread may leave subject
    empty -- the harness reply keeps the thread subject.
    """
    if not require_subject:
        return
    if subject is None or not subject.strip():
        raise ModelRetry(_SUBJECT_REQUIRED_RETRY)


def _is_new_thread_touch(prior_email: Email | None) -> bool:
    """True when the harness will open a new thread (subject required, §V.136).

    Mirrors ``_send_touch_message``: a prior outbound with thread + contact is
    replied on; anything else opens a new cold thread.
    """
    return not (
        prior_email is not None
        and prior_email.gmail_thread_id is not None
        and prior_email.contact_id is not None
    )


def _build_touch_agent(
    workflow: Workflow,
    *,
    require_subject: bool,
) -> Agent[None, TouchMessage]:
    """Build the compose-only touch agent for an outbound touch run (§V.136).

    A touch run (first reach-out or a system-scheduled follow-up in a workflow's
    cadence) produces a structured ``TouchMessage`` instead of driving a tool
    loop: the agent binds zero tools and its validated output *is* the action, so
    the §V.81 tool-count check and the §V.120 reply walker do not apply and the
    harness sends the message itself. The workflow's ``_TOUCH_COMPOSE`` protocol
    plus its ``instructions`` frame the compose task. Output validator with a
    bounded ``ModelRetry`` (capped by the agent retry budget): §V.136 first-touch
    subject require when ``require_subject`` is true (new-thread only). Body
    format lint retired (§V.42 / §B.128). The date-grounding instruction
    (§V.129) and the model-visible protocol carry no SPEC cite (§V.45).
    """
    from mailpilot.agent.templates import (
        _TOUCH_COMPOSE,  # pyright: ignore[reportPrivateUsage]
    )

    agent: Agent[None, TouchMessage] = Agent(
        name="mailpilot.workflow",
        output_type=TouchMessage,
        instructions=_TOUCH_COMPOSE + workflow.instructions,
        retries=_TOUCH_VALIDATION_RETRIES,
    )

    @agent.instructions
    def _ground_current_date() -> str:  # pyright: ignore[reportUnusedFunction]
        return (
            f"The current date is {date.today().isoformat()}. "
            "Ground every relative reference on this date and never guess the year."
        )

    @agent.output_validator
    def _lint_touch_output(  # pyright: ignore[reportUnusedFunction]
        output: TouchMessage,
    ) -> TouchMessage:
        # §V.136 / §B.127: new-thread touch must have a non-empty subject.
        _validate_touch_subject(output.subject, require_subject=require_subject)
        return output

    return agent


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
            "Compose the initial outbound message per the workflow goal "
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
    contact_view: ContactView | None = None,
    company_view: CompanyView | None = None,
) -> str:
    """Assemble the user prompt for the agent.

    ``contact_view`` and ``company_view`` are the mechanically pre-fed CRM
    records (§V.135). ``invoke_workflow_agent`` loads them via
    ``load_contact_view`` / ``load_company_view`` -- the same loaders that back
    CLI ``contact view`` / ``company view`` -- so the agent and the operator see
    byte-identical context (§V.8). Each is rendered as a JSON ``Contact
    record:`` / ``Company record:`` section; the company section is omitted when
    the contact has no parent company (``company_view`` is None).
    """
    sections: list[str] = [
        f"Workflow: {workflow.name}",
        f"Goal: {workflow.goal}",
        f"Type: {workflow.type}",
        f"\nContact: {contact.email}",
    ]

    if contact.first_name or contact.last_name:
        name = f"{contact.first_name or ''} {contact.last_name or ''}".strip()
        sections.append(f"Name: {name}")

    # §V.135: pre-feed the contact + company records (with inlined notes) so the
    # agent grounds on them directly instead of spending a read-tool round-trip.
    # The JSON is the loader's own serialization, byte-identical to the CLI view
    # (§V.8).
    if contact_view is not None:
        sections.append("\nContact record:\n" + contact_view.model_dump_json(indent=2))
    if company_view is not None:
        sections.append("\nCompany record:\n" + company_view.model_dump_json(indent=2))

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
    """Return True if the run sent a message or reached a terminal (§V.120).

    Walks the message ledger (mirrors :func:`_extract_tool_errors`) for a
    ``reply_email`` or ``send_email`` ToolReturnPart that carries no
    ``error`` key -- the message reached Gmail -- or a ``noop`` /
    ``conclude_enrollment`` return, the explicit decline and the agent
    terminal (§V.127). Any of these satisfies the send obligation on the
    inbound tool-loop path; none means the run left the message unsent while
    reporting success. The outbound first reach-out is a compose-only touch
    run (§V.136) whose send is structural, so it is not walker-checked.
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
            if part.tool_name in (
                "reply_email",
                "send_email",
                "noop",
                "conclude_enrollment",
            ):
                return True
    return False


# -- Compose-only touch send (§V.136) ------------------------------------------


def _resolve_prior_touch_email(
    connection: psycopg.Connection[dict[str, Any]],
    task_context: dict[str, Any] | None,
    email_history: list[Email],
) -> Email | None:
    """Resolve the prior touch's email for threading a follow-up (§V.136).

    Prefers ``task_context['prior_email_id']`` -- the cadence engine threads the
    id of touch N-1's email into a scheduled touch-N task. Falls back to the
    enrollment's latest outbound email from ``email_history`` (newest-first,
    already scoped to account + contact + workflow) so a touch that lost its
    context still continues the last send. Returns ``None`` on the first touch
    (no prior outbound), so the harness opens a new thread.
    """
    prior_id = task_context.get("prior_email_id") if task_context else None
    if isinstance(prior_id, str):
        prior = database.get_email(connection, prior_id)
        if prior is not None:
            return prior
    return next((msg for msg in email_history if msg.direction == "outbound"), None)


def _send_touch_message(  # noqa: PLR0913
    connection: psycopg.Connection[dict[str, Any]],
    account: Account,
    gmail_client: GmailClient,
    settings: Settings,
    workflow: Workflow,
    contact: Contact,
    touch_message: TouchMessage,
    prior_email: Email | None,
) -> Email:
    """Send one composed touch through the ``email_ops`` policy layer (§V.136).

    A follow-up (a prior touch exists in the thread) is sent as a reply so it
    threads natively and skips the cold-outbound cooldown -- ``reply_email``
    auto-derives the recipient, ``Re:`` subject, thread, and In-Reply-To. The
    first touch opens a new thread via ``send_email`` with the composed subject.
    The §V.42 format lint already ran as the compose-only output validator, so
    ``email_ops`` is called directly (the tool-layer lint is not re-applied).
    """
    if (
        prior_email is not None
        and prior_email.gmail_thread_id is not None
        and prior_email.contact_id is not None
    ):
        return email_ops.reply_email(
            connection,
            account,
            gmail_client,
            settings,
            email_id=prior_email.id,
            body=touch_message.body,
            workflow_id=workflow.id,
        )
    return email_ops.send_email(
        connection,
        account,
        gmail_client,
        settings,
        to=contact.email,
        subject=touch_message.subject or "",
        body=touch_message.body,
        workflow_id=workflow.id,
    )


def _run_compose_only_touch(  # noqa: PLR0913
    *,
    span: Any,
    connection: psycopg.Connection[dict[str, Any]],
    account: Account,
    gmail_client: GmailClient,
    settings: Settings,
    workflow: Workflow,
    contact: Contact,
    enrollment: Enrollment,
    email_history: list[Email],
    task_context: dict[str, Any] | None,
    prompt: str,
    model: Model | str,
    touch_number: int,
) -> dict[str, Any]:
    """Run one compose-only outbound touch: compose, send, advance cadence (§V.136).

    One LLM call yields a validated ``TouchMessage`` (output validators: §V.42
    body lint + §V.136 first-touch subject require). The harness sends it via
    ``email_ops`` -- a follow-up threads on the prior touch, the first touch
    opens a new thread -- then the cadence engine schedules the next touch or,
    after the final one, concludes the enrollment ``contact_later``
    system-internally (§V.127, §V.128). No tool loop, so §V.81 / §V.120 do not
    apply; the send is structural. Returns the same completed-run result dict
    shape as the tool-loop path, plus the sent email id and the touch number.
    """
    # Resolve prior outbound before the LLM call so the subject validator knows
    # whether this touch opens a new thread (§V.136 / §B.127).
    prior_email = _resolve_prior_touch_email(connection, task_context, email_history)
    touch_agent = _build_touch_agent(
        workflow,
        require_subject=_is_new_thread_touch(prior_email),
    )
    result = touch_agent.run_sync(prompt, model=model)
    touch_message = result.output

    sent_email = _send_touch_message(
        connection,
        account,
        gmail_client,
        settings,
        workflow,
        contact,
        touch_message,
        prior_email,
    )
    cadence.advance_touch_cadence(
        connection,
        workflow,
        enrollment,
        sent_touch_number=touch_number,
        prior_email_id=sent_email.id,
        base=datetime.now(UTC),
    )

    usage = result.usage
    span.set_attribute("model", active_model_name(settings))
    span.set_attribute("input_tokens", usage.input_tokens)
    span.set_attribute("output_tokens", usage.output_tokens)
    span.set_attribute("total_tokens", usage.input_tokens + usage.output_tokens)
    span.set_attribute("llm_requests", usage.requests)
    # §V.47: bubble Anthropic prompt-cache token counts to the rollup span
    # (xAI path reports zeros; names stay for schema stability).
    span.set_attribute("cache_read_input_tokens", usage.cache_read_tokens)
    span.set_attribute("cache_creation_input_tokens", usage.cache_write_tokens)
    # Compose-only runs bind no tools (§V.81 exempt) -- report zero for both so
    # the rollup schema matches the tool-loop path.
    span.set_attribute("tool_call_count", 0)
    span.set_attribute("tool_error_count", 0)
    span.set_attribute("touch_number", touch_number)
    span.set_attribute("sent_email_id", sent_email.id)
    reasoning = f"composed and sent touch {touch_number}"
    span.set_attribute("result", "completed")
    span.set_attribute("status", "completed")
    span.set_attribute("agent_reasoning", reasoning)
    operator_event(
        "agent.run",
        workflow_id=workflow.id,
        contact_id=contact.id,
        status="completed",
        tool_calls=0,
    )
    return {
        "workflow_id": workflow.id,
        "contact_id": contact.id,
        "status": "completed",
        "tool_calls": 0,
        "tool_errors": [],
        "reasoning": reasoning,
        "sent_email_id": sent_email.id,
        "touch_number": touch_number,
    }


# -- Main entry point ----------------------------------------------------------


def invoke_workflow_agent(  # noqa: PLR0913, PLR0915, C901
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
        workflow: Workflow with instructions (system prompt) and goal.
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
            # ``conclude_enrollment`` without asking the LLM for it.
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

            # Resolve the model once; both the compose-only and the tool-loop
            # path use it. §V.47: provider-aware factory.
            if model_override is not None:
                model = model_override
            else:
                model = _build_model(settings, role="workflow")

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

            # §V.135: mechanically pre-feed the CRM records the system already
            # holds keys for. load_contact_view / load_company_view are the same
            # loaders that back CLI ``contact view`` / ``company view``, so the
            # agent prompt and the operator CLI carry byte-identical context
            # (§V.8). The company record is loaded only when the contact has a
            # parent company.
            contact_view = database.load_contact_view(connection, contact.id)
            company_view = (
                database.load_company_view(connection, contact.company_id)
                if contact.company_id is not None
                else None
            )

            # Assemble prompt (shared by both agent shapes).
            prompt = _build_user_prompt(
                workflow=workflow,
                contact=contact,
                email_history=email_history,
                email=email,
                task_description=task_description,
                task_context=task_context,
                trigger=trigger,
                contact_view=contact_view,
                company_view=company_view,
            )

            span.set_attribute("prompt_length", len(prompt))

            # §V.136 dispatch: an outbound touch run -- the first reach-out or a
            # system-scheduled touch-N task, both with no triggering email --
            # composes a structured TouchMessage. The harness sends it and
            # advances the cadence; there is no tool loop, so the §V.81 tool-count
            # check and the §V.120 reply walker do not apply (the send is
            # structural). All other runs (inbound reply, outbound reply-branch
            # task, manual) keep the tool loop below.
            touch_number = cadence.resolve_touch_number(task_context, trigger)
            if (
                workflow.type == "outbound"
                and email is None
                and touch_number is not None
            ):
                return _run_compose_only_touch(
                    span=span,
                    connection=connection,
                    account=account,
                    gmail_client=gmail_client,
                    settings=settings,
                    workflow=workflow,
                    contact=contact,
                    enrollment=enrollment,
                    email_history=email_history,
                    task_context=task_context,
                    prompt=prompt,
                    model=model,
                    touch_number=touch_number,
                )

            # Tool-loop path: build the template agent and run it.
            agent = _build_agent(workflow, trigger=trigger)
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
            span.set_attribute("model", active_model_name(settings))
            span.set_attribute("input_tokens", usage.input_tokens)
            span.set_attribute("output_tokens", usage.output_tokens)
            span.set_attribute("total_tokens", usage.input_tokens + usage.output_tokens)
            span.set_attribute("llm_requests", usage.requests)
            # §V.47: bubble Anthropic prompt-cache token counts to the rollup
            # span. Pydantic AI's RunUsage already sums these across child
            # chat turns, so no per-turn span walk is needed. xAI reports zeros.
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

            # §V.120 (per §B.106, §V.136): a send-obligated run must finish in a
            # send, a terminal, or an explicit noop. The tool-count check above
            # passes when the model called only search/read tools and then
            # narrated a message it never sent, which leaves the work undone
            # while reporting success. The walker now covers the inbound path
            # only -- a triggering email present (trigger in {email, task}) means
            # the agent must reply, conclude, or noop. The outbound first
            # reach-out is a compose-only touch run (dispatched above), so its
            # send is structural, not walker-checked. ``manual`` stays exempt.
            send_obligated = email is not None and trigger in ("email", "task")
            if send_obligated and not _sent_reply(result):
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
