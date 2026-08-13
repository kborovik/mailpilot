"""Agent tools for workflow execution.

Each function is a Pydantic AI tool the agent can call. Tools are defined
as standalone functions (not methods) so they can be unit-tested without
spinning up a full agent.

Dependency injection: each tool receives explicit dependency parameters
(``connection``, ``account``, ``workflow_id``, etc.) that issue #12 will
wire from ``RunContext[AgentDeps]``.

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
"""

from __future__ import annotations

import contextvars
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from mailpilot import database, email_ops
from mailpilot.cadence import parse_touch_number
from mailpilot.drive import DriveClient
from mailpilot.models import Account
from mailpilot.settings import Settings

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


def send_email(  # noqa: PLR0913
    connection: psycopg.Connection[dict[str, Any]],
    account: Account,
    gmail_client: object,
    settings: Settings,
    workflow_id: str,
    to: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
    cc: str | None = None,
    bcc: str | None = None,
) -> dict[str, Any]:
    """Agent tool: send a new outbound email via Gmail.

    Thin wrapper over :func:`mailpilot.email_ops.send_email`. Converts
    typed policy exceptions into the LLM-facing error dict shape.

    Pass ``thread_id`` to continue a multi-touch outbound thread: supply
    the ``gmail_thread_id`` returned by the first touch so later touches
    thread natively.
    """
    try:
        # thread_id forwards outbound thread-continuation per §V.78.
        email = email_ops.send_email(
            connection,
            account,
            gmail_client,  # type: ignore[arg-type]
            settings,
            to=to,
            subject=subject,
            body=body,
            workflow_id=workflow_id,
            thread_id=thread_id,
            cc=cc,
            bcc=bcc,
        )
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


def reply_email(  # noqa: PLR0913
    connection: psycopg.Connection[dict[str, Any]],
    account: Account,
    gmail_client: object,
    settings: Settings,
    workflow_id: str,
    email_id: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
) -> dict[str, Any]:
    """Agent tool: reply in-thread. Wraps :func:`email_ops.reply_email`.

    Converts typed policy exceptions into the LLM-facing error dict.
    """
    try:
        email = email_ops.reply_email(
            connection,
            account,
            gmail_client,  # type: ignore[arg-type]
            settings,
            email_id=email_id,
            body=body,
            workflow_id=workflow_id,
            cc=cc,
            bcc=bcc,
        )
    except email_ops.EmailOpsError as exc:
        return {"error": exc.code, "message": str(exc)}

    # §V.131: the reply reached Gmail -- mark the per-task reply-emitted flag so
    # the run-loop terminal branch never sends a duplicate fallback reply.
    _mark_reply_emitted()
    return {
        "id": email.id,
        "gmail_message_id": email.gmail_message_id,
        "gmail_thread_id": email.gmail_thread_id,
    }


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


def create_task(  # noqa: PLR0913
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
    workflow_id: str,
    contact_id: str,
    description: str,
    scheduled_at: str,
    context: dict[str, Any] | None = None,
    email_id: str | None = None,
) -> dict[str, str]:
    """Schedule deferred work for later execution.

    Args:
        connection: Open database connection.
        enrollment_id: Current enrollment FK (NOT NULL).
        workflow_id: Current workflow FK (denormalised from enrollment).
        contact_id: Contact this task targets (denormalised from enrollment).
        description: What the agent should do when the task runs.
        scheduled_at: When to execute (ISO 8601 timestamp, strictly future).
        context: Arbitrary JSON context for the agent on re-invocation.
        email_id: Optional triggering email for focused context.

    Returns:
        Dict with created task ID, or an error dict when scheduled_at is past.
    """
    # Reject a past-dated schedule at the agent boundary so no already-due task
    # row is ever persisted (§V.129). The guard sits here, not in
    # database.create_task, so the system-computed enrollment_schedule
    # first-touch (§V.32) stays exempt.
    timestamp_error = _reject_past_timestamp(scheduled_at, field="scheduled_at")
    if timestamp_error is not None:
        return timestamp_error
    task = database.create_task(
        connection,
        enrollment_id=enrollment_id,
        workflow_id=workflow_id,
        contact_id=contact_id,
        description=description,
        scheduled_at=scheduled_at,
        context=_normalize_touch_context(context),
        email_id=email_id,
    )
    return {"id": task.id}


def cancel_task(
    connection: psycopg.Connection[dict[str, Any]],
    task_id: str,
) -> dict[str, str]:
    """Cancel a pending task.

    Use when a previously scheduled follow-up is no longer needed (e.g.,
    the contact replied before the follow-up was due).

    Args:
        connection: Open database connection.
        task_id: Task ID to cancel.

    Returns:
        Dict with cancelled task ID and status, or error if not found/not pending.
    """
    task = database.cancel_task(connection, task_id)
    if task is None:
        return {
            "error": "not_found",
            "message": f"task not found or not pending: {task_id}",
        }
    return {"id": task.id, "status": task.status}


# §V.127: the agent's single terminal tool. The model picks ONE disposition and
# writes a note; the system runs the deterministic side effects. Only
# ``meeting_booked`` records the enrollment goal as reached (``completed``);
# ``do_not_contact`` and ``contact_later`` did not reach the goal this cycle
# (``failed``), the latter scheduling a fresh re-enrollment touch (§V.32).
_CONCLUDE_DISPOSITIONS = ("meeting_booked", "do_not_contact", "contact_later")
_CONCLUDE_OUTCOME: dict[str, str] = {
    "meeting_booked": "completed",
    "do_not_contact": "failed",
    "contact_later": "failed",
}
# Default deferral when ``contact_later`` omits ``reschedule_at`` -- about three
# months out (§V.127). The scheduled task self-fires when the date arrives.
_RESCHEDULE_DEFAULT_DAYS = 90


def conclude_enrollment(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
    disposition: str,
    note: str,
    reschedule_at: str | None = None,
) -> dict[str, Any]:
    """Conclude the current enrollment with one terminal disposition.

    The agent picks one ``disposition`` and writes a ``note``; the system runs
    the deterministic side effects so a cheap model faces one decision, not
    several tool calls. Every disposition records an enrollment outcome on the
    timeline and cancels the enrollment's pending future follow-up tasks (the
    operator first-touch preserved), then:

        - ``meeting_booked`` -- records a completed outcome and writes a note
          (the "I booked" reply path, distinct from calendar detection).
        - ``do_not_contact`` -- records a failed outcome, sets a global block on
          the contact, and writes a note. Use for opt-out, wrong person, retired
          / left-the-company auto-replies, and address-change / "update your
          records" / hard email-redirect auto-replies: stop touches to the
          enrolled address even if From uses a different local-part; put the
          redirect, referral addresses, and the new email (when present) in the
          note; never enroll the From alias or any new address. Out-of-office
          auto-replies are NOT this path -- use noop.
        - ``contact_later`` -- records a failed outcome and schedules a
          re-enrollment first-touch task at ``reschedule_at`` (about three
          months out when omitted), then writes a note.

    Args:
        connection: Open database connection.
        enrollment_id: Current enrollment ID (from deps, not the LLM).
        disposition: One of meeting_booked, do_not_contact, contact_later.
        note: The agent's explanation, written to the outcome activity and a
            contact note. For address-change, include the redirect and new
            email when the inbound message states one.
        reschedule_at: ISO 8601 timestamp for the contact_later re-enrollment
            touch; defaults to about three months out when omitted.

    Returns:
        Dict echoing the disposition and recorded outcome (plus the resolved
        reschedule_at for contact_later), or an error dict on a bad disposition
        or a missing enrollment.
    """
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

    contact_id = enrollment.contact_id
    outcome = _CONCLUDE_OUTCOME[disposition]
    note_body = note or f"Enrollment concluded: {disposition}"

    database.record_enrollment_outcome(
        connection,
        enrollment_id,
        outcome=outcome,
        reason=note_body,
        disposition=disposition,
    )
    database.cancel_enrollment_followup_tasks(connection, enrollment_id)

    result: dict[str, Any] = {"disposition": disposition, "outcome": outcome}

    if disposition == "do_not_contact":
        database.disable_contact(
            connection, contact_id, reason=f"do_not_contact: {note_body}"
        )
    elif disposition == "contact_later":
        scheduled_at = (
            reschedule_at
            or (
                datetime.now(UTC) + timedelta(days=_RESCHEDULE_DEFAULT_DAYS)
            ).isoformat()
        )
        database.create_task(
            connection,
            enrollment_id=enrollment_id,
            workflow_id=enrollment.workflow_id,
            contact_id=contact_id,
            description="scheduled re-enrollment first reach-out",
            scheduled_at=scheduled_at,
            context={"trigger": "enrollment_schedule", "touch": 1},
            email_id=None,
        )
        result["reschedule_at"] = scheduled_at

    database.create_note(connection, body=note_body, contact_id=contact_id)
    return result


def disable_contact(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
    reason: str,
) -> dict[str, str]:
    """Set a global block on a contact.

    Hard block across all workflows. ``send_email`` and ``reply_email``
    refuse contacts whose ``disabled_reason`` is non-null. The reason
    string is stored verbatim; convention is ``"bounced: <detail>"`` or
    ``"unsubscribed: <detail>"``.

    Args:
        connection: Open database connection.
        contact_id: Contact ID.
        reason: Explanation written to ``contact.disabled_reason``.

    Returns:
        Dict with updated contact ID and disabled_reason, or error if not found.
    """
    updated = database.disable_contact(connection, contact_id, reason=reason)
    if updated is None:
        return {"error": "not_found", "message": f"contact not found: {contact_id}"}
    return {"id": updated.id, "disabled_reason": updated.disabled_reason or ""}


def list_enrollments(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> list[dict[str, Any]]:
    """List enrollments in a workflow with their latest outcome.

    Lets the agent coordinate across contacts (e.g., skip person B if
    person A at the same company already achieved the goal). Each
    row includes ``latest_outcome`` (``completed`` / ``failed`` / ``None``),
    ``latest_outcome_reason``, and ``latest_outcome_at`` -- pulled from the
    activity timeline since outcomes are timeline-only.

    Args:
        connection: Open database connection.
        workflow_id: Workflow ID.

    Returns:
        List of enrollment records with operational status and the latest
        outcome activity, if any.
    """
    enrollments = database.list_enrollments_with_outcomes(connection, workflow_id)
    return [e.model_dump(mode="json") for e in enrollments]


def search_emails(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    query: str,
) -> list[dict[str, Any]]:
    """Search email history for the current account.

    Args:
        connection: Open database connection.
        account_id: Account to scope search to.
        query: Search term matched against subject and body.

    Returns:
        List of matching email summaries.
    """
    emails = database.search_emails(connection, query, account_id=account_id)
    return [e.model_dump() for e in emails]


def read_email(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    email_id: str,
) -> dict[str, Any] | None:
    """Read a specific email by ID to view its full content, including body text.

    Args:
        connection: Open database connection.
        account_id: Account the agent is scoped to. Emails belonging to other
            accounts are not visible (returns None) -- prevents cross-tenant
            data leaks via prompt injection in inbound message bodies.
        email_id: The ID of the email to read.

    Returns:
        Full email details including body text, or None if not found or the
        email belongs to a different account.
    """
    email = database.get_email(connection, email_id)
    if email is None or email.account_id != account_id:
        return None
    return email.model_dump()


def list_drive_markdown(
    drive_client: DriveClient,
    folder_id: str,
) -> list[dict[str, str]] | dict[str, str]:
    """List Markdown files in a Drive folder for KB grounding.

    Args:
        drive_client: Drive client scoped to the current account.
        folder_id: Drive folder ID supplied via the workflow instructions.

    Returns:
        List of ``{"file_id": ..., "name": ...}`` on success, or an error
        dict ``{"error": ..., "message": ...}`` on Drive failure.
    """
    from googleapiclient.errors import HttpError

    try:
        return drive_client.list_markdown(folder_id)
    except HttpError as exc:
        if exc.resp.status == 404:
            return {
                "error": "not_found",
                "message": f"drive folder not found: {folder_id}",
            }
        return {
            "error": "drive_unavailable",
            "message": str(exc),
        }
    except (TimeoutError, OSError) as exc:
        # Per §V.38 + §B.34: surface transport stalls / socket faults as a
        # structured tool return so a sibling parallel call carries the
        # agent run instead of bubbling to a terminal task failure.
        return {
            "error": "drive_unavailable",
            "message": str(exc),
        }


def search_drive_markdown(
    drive_client: DriveClient,
    folder_id: str,
    query: str,
) -> list[dict[str, str]] | dict[str, str]:
    """Full-text search Markdown files in the workflow's KB Drive folder.

    Prefer this over ``list_drive_markdown`` when the folder may contain
    many documents -- it lets you target the most relevant file without
    enumerating every KB entry.

    Args:
        drive_client: Drive client scoped to the current account.
        folder_id: Drive folder ID supplied via the workflow instructions.
        query: Free-text search query. Drive matches against file content
            and metadata. Results returned in Drive's native relevance order.

    Returns:
        List of ``{"file_id": ..., "name": ...}`` on success (empty list
        when nothing matches), or an error dict ``{"error": ...,
        "message": ...}`` on Drive failure.
    """
    from googleapiclient.errors import HttpError

    try:
        return drive_client.search_markdown(folder_id, query)
    except HttpError as exc:
        if exc.resp.status == 404:
            return {
                "error": "not_found",
                "message": f"drive folder not found: {folder_id}",
            }
        return {
            "error": "drive_unavailable",
            "message": str(exc),
        }
    except (TimeoutError, OSError) as exc:
        # Per §V.38 + §B.34: see list_drive_markdown rationale.
        return {
            "error": "drive_unavailable",
            "message": str(exc),
        }


def read_drive_markdown(
    drive_client: DriveClient,
    file_id: str,
) -> dict[str, str]:
    """Read a Markdown file from Drive.

    Args:
        drive_client: Drive client scoped to the current account.
        file_id: Drive file ID, typically returned by ``list_drive_markdown``.

    Returns:
        ``{"name": ..., "content": ..., "web_view_link": ...}`` on success,
        or ``{"error": ..., "message": ...}`` on Drive failure.
    """
    from googleapiclient.errors import HttpError

    try:
        result = drive_client.read_markdown(file_id)
    except HttpError as exc:
        if exc.resp.status == 404:
            return {
                "error": "not_found",
                "message": f"drive file not found: {file_id}",
            }
        return {
            "error": "drive_unavailable",
            "message": str(exc),
        }
    except (TimeoutError, OSError) as exc:
        # Per §V.38 + §B.34: a hung sibling read in a parallel fan-out used
        # to escape this catch and burn the §V.49 retry budget; the broadened
        # arm folds transport-level faults into the same drive_unavailable
        # tool-return so the surviving call carries the agent run.
        return {
            "error": "drive_unavailable",
            "message": str(exc),
        }
    return result


def noop(reason: str) -> dict[str, Any]:
    """Explicitly decline to act.

    Call this tool when, after reviewing context, no action is appropriate.
    You must still call a tool every turn -- noop is the explicit "do nothing"
    signal. Typical case: out-of-office or temporary absence auto-reply
    (pause once; leave the enrollment open; do not conclude). Address-change
    and hard email-redirect auto-replies are not noop -- use
    conclude_enrollment with do_not_contact instead.

    Args:
        reason: Why no action is needed.

    Returns:
        Acknowledgement dict.
    """
    return {"acknowledged": True, "reason": reason}
