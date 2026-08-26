"""Agent tools for workflow execution.

Each function is a Pydantic AI tool. Tools take ``RunContext[AgentDeps]``
so templates register ``Tool(fn)`` from this module.

Tools (see §I agent tools):
    - ``send_email`` -- send via Gmail API with contact status + cooldown guards
    - ``reply_email`` -- reply in-thread with auto-resolved recipient and subject
    - ``create_task`` -- schedule deferred work
    - ``cancel_task`` -- cancel a pending task
    - ``conclude_enrollment`` -- terminal: record outcome + run disposition side effects
    - ``disable_contact`` -- set global contact block (bounced/unsubscribed)
    - ``search_emails`` -- query email history
    - ``list_enrollments`` -- list enrollments in workflow with status
    - ``read_email`` -- full email content lookup
    - ``list_drive_markdown`` -- list Markdown files in a Drive folder
    - ``read_drive_markdown`` -- read a Markdown file from Drive
    - ``search_drive_markdown`` -- full-text search Markdown files in a Drive folder
    - ``noop`` -- explicit no-op escape
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from pydantic_ai import RunContext

from mailpilot import database, email_ops
from mailpilot.cadence import parse_touch_number
from mailpilot.drive import DriveClient
from mailpilot.gmail import GmailClient
from mailpilot.models import Account, Email
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


# §V.131: per-task reply-emitted flag. ``run.execute_task`` enters
# ``reply_emitted_scope`` so a successful ``reply_email`` / ``send_email`` this
# task flips the flag. The run-loop terminal branch reads it via
# ``reply_was_emitted`` to decide whether a terminal inbound failure still owes
# the sender a fallback acknowledgement -- the ``connection.rollback()`` at the
# head of ``_handle_agent_failure`` erases a mid-turn-sent email row while Gmail
# kept the message, so this in-run flag is the only sound already-replied signal
# (blocks a double-reply when a non-transient class raised after a successful
# send). The flag is a mutable object, not a re-bound value, so a mark inside a
# worker thread's copied context -- Pydantic AI dispatches sync tools via
# asyncio.to_thread -- mutates the shared object the main thread reads back.
# Outside a scope the flag stays ``None``.
# (§V.42 retired the format-lint / reply-rejection counter path — no body
# format check remains on send/reply/compose.)


class _ReplyEmittedFlag:
    __slots__ = ("emitted",)

    def __init__(self) -> None:
        self.emitted: bool = False


_REPLY_EMITTED: contextvars.ContextVar[_ReplyEmittedFlag | None] = (
    contextvars.ContextVar("mailpilot.reply_emitted", default=None)
)


@contextmanager
def reply_emitted_scope() -> Generator[None]:
    """Install a fresh per-task reply-emitted flag (§V.131).

    Wrap each task-drained ``agent.invoke`` so a successful ``reply_email`` /
    ``send_email`` this task sets the flag. ``run._handle_agent_failure`` reads
    it via :func:`reply_was_emitted` to suppress a duplicate fallback reply.
    Outside a scope (CLI / legacy paths without a task row) the flag stays
    absent and :func:`reply_was_emitted` reads ``False``.
    """
    token = _REPLY_EMITTED.set(_ReplyEmittedFlag())
    try:
        yield
    finally:
        _REPLY_EMITTED.reset(token)


def _mark_reply_emitted() -> None:
    """Record that a send reached Gmail this task (§V.131).

    No-op outside a :func:`reply_emitted_scope` so non-task callers are
    unaffected.
    """
    flag = _REPLY_EMITTED.get()
    if flag is not None:
        flag.emitted = True


def reply_was_emitted() -> bool:
    """Return ``True`` when a send reached Gmail this task (§V.131)."""
    flag = _REPLY_EMITTED.get()
    return flag is not None and flag.emitted


def _email_tool_result(op: Callable[[], Email]) -> dict[str, Any]:
    """Run a send/reply op: error dict on policy fail, else ids + mark emitted.

    §V.39: send_email and reply_email share this shape.
    """
    try:
        email = op()
    except email_ops.EmailOpsError as exc:
        return {"error": exc.code, "message": str(exc)}
    # §V.131: the message reached Gmail -- mark the per-task reply-emitted flag
    # so the run-loop terminal branch never sends a duplicate fallback reply.
    _mark_reply_emitted()
    return {
        "id": email.id,
        "gmail_message_id": email.gmail_message_id,
        "gmail_thread_id": email.gmail_thread_id,
    }


def send_email(  # noqa: PLR0913, PLR0917
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
    deps = ctx.deps
    # thread_id forwards outbound thread-continuation per §V.78.
    return _email_tool_result(
        lambda: email_ops.send_email(
            deps.connection,
            deps.account,
            deps.gmail_client,
            deps.settings,
            to=to,
            subject=subject,
            body=body,
            workflow_id=deps.workflow_id,
            thread_id=thread_id,
            cc=cc,
            bcc=bcc,
        )
    )


def reply_email(
    ctx: RunContext[AgentDeps],
    email_id: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
) -> dict[str, Any]:
    """Reply to an existing email in-thread."""
    deps = ctx.deps
    return _email_tool_result(
        lambda: email_ops.reply_email(
            deps.connection,
            deps.account,
            deps.gmail_client,
            deps.settings,
            email_id=email_id,
            body=body,
            workflow_id=deps.workflow_id,
            cc=cc,
            bcc=bcc,
        )
    )


def _reject_past_timestamp(value: str, *, field: str) -> dict[str, str] | None:
    """Reject an agent-supplied timestamp that is not strictly in the future.

    §V.129 GUARD: the agent boundary refuses a past-dated schedule so a
    wrong-year timestamp never persists a task that fires next run-loop tick
    and then survives a booking conclusion (cancel_enrollment_followup_tasks
    cancels only future rows). Returns an error dict when ``value`` parses to
    a moment at or before now; ``None`` when it is strictly later so the
    caller proceeds. A naive timestamp is read as UTC. An unparseable value
    falls through (``None``) to the existing downstream handling.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    if parsed <= now:
        return {
            "error": "past_scheduled_at",
            "message": (
                f"{field} must be strictly after the current time "
                f"({now.isoformat()}); got {value!r}"
            ),
        }
    return None


def _normalize_touch_context(
    context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Coerce ``context.touch`` ``T<n>`` / digit-string to int (§V.162).

    Existing malformed rows stay readable via SQL parse. New agent writes
    (OOO-resume ``create_task``) persist a numeric touch.
    """
    if context is None or "touch" not in context:
        return context
    parsed = parse_touch_number(context["touch"])
    if parsed is None or parsed == context["touch"]:
        return context
    return {**context, "touch": parsed}


def create_task(
    ctx: RunContext[AgentDeps],
    description: str,
    scheduled_at: str,
    context: dict[str, Any] | None = None,
    email_id: str | None = None,
) -> dict[str, str]:
    """Schedule deferred work for the current contact."""
    # Reject a past-dated schedule at the agent boundary so no already-due task
    # row is ever persisted (§V.129). The guard sits here, not in
    # database.create_task, so the system-computed enrollment_schedule
    # first-touch (§V.32) stays exempt.
    timestamp_error = _reject_past_timestamp(scheduled_at, field="scheduled_at")
    if timestamp_error is not None:
        return timestamp_error
    deps = ctx.deps
    task = database.create_task(
        deps.connection,
        enrollment_id=deps.enrollment_id,
        workflow_id=deps.workflow_id,
        contact_id=deps.contact_id,
        description=description,
        scheduled_at=scheduled_at,
        context=_normalize_touch_context(context),
        email_id=email_id,
    )
    return {"id": task.id}


def cancel_task(
    ctx: RunContext[AgentDeps],
    task_id: str,
) -> dict[str, str]:
    """Cancel a pending task."""
    task = database.cancel_task(ctx.deps.connection, task_id)
    if task is None:
        return {
            "error": "not_found",
            "message": f"task not found or not pending: {task_id}",
        }
    return {"id": task.id, "status": task.status}


# §V.127: the agent's single terminal tool. The model picks ONE disposition.
_CONCLUDE_DISPOSITIONS = ("meeting_booked", "do_not_contact", "contact_later")
# Default deferral when ``contact_later`` omits ``reschedule_at`` -- about three
# months out (§V.127). Resolved here so the helper treats omitted
# ``reschedule_at`` as no re-enrollment task (cadence).
_RESCHEDULE_DEFAULT_DAYS = 90


def conclude_enrollment(
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
    auto-replies (including past-tense last-day auto-replies), and
    address-change / "update your records" / hard
    email-redirect auto-replies: stop touches to the enrolled address even
    if From uses a different local-part; put the redirect, referral
    addresses, named successors without emails, and the new email (when
    present) in `note`; never enroll the From alias or any new address.
    Out-of-office auto-replies are not this path -- call noop instead. A
    past last-day auto-reply is left-company, not out-of-office.
    """
    connection = ctx.deps.connection
    enrollment_id = ctx.deps.enrollment_id
    if disposition not in _CONCLUDE_DISPOSITIONS:
        return {
            "error": "invalid_disposition",
            "message": (
                f"disposition must be one of {_CONCLUDE_DISPOSITIONS}, "
                f"got: {disposition}"
            ),
        }
    enrollment = database.get_enrollment_by_id(connection, enrollment_id)
    if enrollment is None:
        return {
            "error": "not_found",
            "message": f"enrollment not found: {enrollment_id}",
        }

    # Reject an agent-supplied contact_later reschedule_at that is not strictly
    # future, before any side effect runs so the enrollment is neither
    # concluded nor scheduled (§V.129). The error key lets _sent_reply skip
    # this call (§V.120) so the agent retries with a corrected date. The
    # default-omitted ~3-month path is system-set (§V.127), so it is exempt.
    if disposition == "contact_later" and reschedule_at is not None:
        timestamp_error = _reject_past_timestamp(reschedule_at, field="reschedule_at")
        if timestamp_error is not None:
            return timestamp_error

    note_body = note or f"Enrollment concluded: {disposition}"
    scheduled_at: str | None = None
    if disposition == "contact_later":
        scheduled_at = (
            reschedule_at
            or (
                datetime.now(UTC) + timedelta(days=_RESCHEDULE_DEFAULT_DAYS)
            ).isoformat()
        )

    result = database.conclude_enrollment(
        connection,
        enrollment_id,
        disposition=disposition,
        reason=note_body,
        reschedule_at=scheduled_at,
        note=note_body,
    )
    assert result is not None
    return result


def disable_contact(
    ctx: RunContext[AgentDeps],
    reason: str,
) -> dict[str, str]:
    """Set a global block on the current contact.

    `reason` is stored verbatim in `contact.disabled_reason`. Convention:
    prefix with `"bounced: "` or `"unsubscribed: "` so the operator can
    grep the source class. Once set, `send_email` and `reply_email`
    refuse this contact across every workflow.
    """
    contact_id = ctx.deps.contact_id
    updated = database.disable_contact(ctx.deps.connection, contact_id, reason=reason)
    if updated is None:
        return {"error": "not_found", "message": f"contact not found: {contact_id}"}
    return {"id": updated.id, "disabled_reason": updated.disabled_reason or ""}


def list_enrollments(
    ctx: RunContext[AgentDeps],
) -> list[dict[str, Any]]:
    """List enrollments in the current workflow with their outcome status."""
    enrollments = database.list_enrollments_detailed(
        ctx.deps.connection,
        workflow_id=ctx.deps.workflow_id,
        full=True,
        limit=None,
        sort="created_at",
    )
    rows: list[dict[str, Any]] = []
    for enrollment in enrollments:
        dumped = enrollment.model_dump(mode="json")
        dumped["latest_outcome"] = enrollment.latest_outcome
        dumped["latest_outcome_reason"] = enrollment.latest_outcome_reason
        dumped["latest_outcome_at"] = (
            enrollment.latest_outcome_at.isoformat()
            if enrollment.latest_outcome_at is not None
            else None
        )
        rows.append(dumped)
    return rows


def search_emails(
    ctx: RunContext[AgentDeps],
    query: str,
) -> list[dict[str, Any]]:
    """Search email history for the current account."""
    emails = database.search_emails(
        ctx.deps.connection, query, account_id=ctx.deps.account.id
    )
    return [e.model_dump() for e in emails]


def read_email(
    ctx: RunContext[AgentDeps],
    email_id: str,
) -> dict[str, Any] | None:
    """Read full email content (including body text) by ID.

    Use this for search_emails hits that are not already in the prompt
    history for this enrollment.
    """
    email = database.get_email(ctx.deps.connection, email_id)
    if email is None or email.account_id != ctx.deps.account.id:
        return None
    return email.model_dump()


def _drive_call[T](
    op: Callable[[], T], *, not_found_message: str
) -> T | dict[str, str]:
    """Map Drive transport faults to a structured tool-return dict (§V.38)."""
    from googleapiclient.errors import HttpError

    try:
        return op()
    except HttpError as exc:
        if exc.resp.status == 404:
            return {"error": "not_found", "message": not_found_message}
        return {"error": "drive_unavailable", "message": str(exc)}
    except (TimeoutError, OSError) as exc:
        return {"error": "drive_unavailable", "message": str(exc)}


def list_drive_markdown(
    ctx: RunContext[AgentDeps],
    folder_id: str,
) -> list[dict[str, str]] | dict[str, str]:
    """List Markdown files in a Drive folder for KB grounding."""
    return _drive_call(
        lambda: ctx.deps.drive_client.list_markdown(folder_id),
        not_found_message=f"drive folder not found: {folder_id}",
    )


def search_drive_markdown(
    ctx: RunContext[AgentDeps],
    folder_id: str,
    query: str,
) -> list[dict[str, str]] | dict[str, str]:
    """Full-text search Markdown files in a Drive folder."""
    return _drive_call(
        lambda: ctx.deps.drive_client.search_markdown(folder_id, query),
        not_found_message=f"drive folder not found: {folder_id}",
    )


def read_drive_markdown(
    ctx: RunContext[AgentDeps],
    file_id: str,
) -> dict[str, str]:
    """Read a Markdown file from Drive."""
    return _drive_call(
        lambda: ctx.deps.drive_client.read_markdown(file_id),
        not_found_message=f"drive file not found: {file_id}",
    )


def noop(ctx: RunContext[AgentDeps], reason: str) -> dict[str, Any]:
    """Explicitly decline to act.

    Call this tool when, after reviewing context, no action is appropriate.
    You must still call a tool every turn -- noop is the explicit "do nothing"
    signal. Typical case: out-of-office or temporary absence auto-reply
    (pause once; leave enrollment open; do not conclude). Address-change,
    last-day-was, retired, and left-company auto-replies are not noop -- use
    conclude_enrollment with do_not_contact instead.
    """
    del ctx
    return {"acknowledged": True, "reason": reason}
