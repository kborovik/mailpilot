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
    - ``read_contact`` -- CRM contact lookup
    - ``read_company`` -- CRM company lookup
    - ``read_email`` -- full email content lookup
    - ``list_drive_markdown`` -- list Markdown files in a Drive folder
    - ``read_drive_markdown`` -- read a Markdown file from Drive
"""

from __future__ import annotations

import contextvars
import re
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import logfire
import psycopg

from mailpilot import database, email_ops
from mailpilot.drive import DriveClient
from mailpilot.models import Account
from mailpilot.settings import Settings

# §V.71: per-agent-invocation reply-rejection counter (root cause §B.57).
# ``run.execute_task`` enters ``reply_rejection_scope`` to install a fresh
# counter for each task; ``reply_email`` and ``send_email`` increment the
# counter on every ``_check_spec_table`` (format) hit and bypass the check
# once the counter reaches ``_REPLY_REJECTION_CAP``. Bounds worst-case latency
# under prompt-fidelity loops (B7 17 calls / 195s, C burst 35 calls / 495s
# observed in §B.57). Outside a scope (legacy / CLI paths without a task row)
# the counter stays ``None`` and the check behaves as before.


class _ReplyRejectionCounter:
    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count: int = 0


_REPLY_REJECTIONS: contextvars.ContextVar[_ReplyRejectionCounter | None] = (
    contextvars.ContextVar("mailpilot.reply_rejections", default=None)
)
_REPLY_REJECTION_CAP = 3


@contextmanager
def reply_rejection_scope() -> Generator[None]:
    """Install a fresh per-invocation reply-rejection counter (§V.71).

    Wrap each task-drained ``agent.invoke`` so successive ``reply_email`` /
    ``send_email`` calls from the same task share a counter. The cap kicks in
    after ``_REPLY_REJECTION_CAP`` consecutive format-lint rejections; further
    calls bypass the check and emit a ``logfire.warn`` so the regression is
    observable instead of silently looping.
    """
    token = _REPLY_REJECTIONS.set(_ReplyRejectionCounter())
    try:
        yield
    finally:
        _REPLY_REJECTIONS.reset(token)


def _consume_reply_rejection(
    *,
    tool: str,
    rejection_type: Literal["format"],
    workflow_id: str,
    email_id: str | None,
) -> bool:
    """Record a rejection; return ``True`` when the caller MUST bypass the check.

    Returns ``False`` outside a ``reply_rejection_scope`` (legacy path) so the
    caller surfaces the error to the agent as before. Inside a scope,
    increments the counter and returns ``True`` once it reaches the cap,
    emitting the ``cap_reached`` warn span on the transition with the
    rejection type that triggered the bypass.
    """
    counter = _REPLY_REJECTIONS.get()
    if counter is None:
        return False
    counter.count += 1
    if counter.count < _REPLY_REJECTION_CAP:
        return False
    # Per §V.71: a stable span name keeps Logfire alerts grep-able across tools
    # and rejection types; ``tool`` and ``rejection_type`` ride as attributes
    # so the bypass site and trigger class stay identifiable.
    logfire.warn(
        "reply_email.reply_rejection.cap_reached",
        tool=tool,
        rejection_type=rejection_type,
        attempt=counter.count,
        workflow_id=workflow_id,
        email_id=email_id,
    )
    return True


# Per §V.42: detect spec-shape rows (label + any whitespace + non-whitespace
# value, length capped at 80 chars) on lines that do not use Markdown
# pipe-table syntax. Three or more *consecutive* such lines without a
# `|---|` separator anywhere in the body indicates a spec sheet rendered as
# space-aligned text rather than a proper pipe-table. Recurring drift per
# §B.5 / §B.9 / §B.14 / §B.36 that prompt-only fixes did not hold;
# numeric-only matching pre-§B.14 missed non-numeric KDF rows like
# "Mesh Size  20x50"; the 2-space floor pre-§B.36 missed the single-space
# `**label** *value*` cluster shape. Consecutive tracking preserves prose
# immunity under the widened `\s+` floor: short greetings or `Best,`/`Thanks!`
# closers separated from each other by blank or non-matching lines never
# accumulate to the trigger threshold. ASCII rule-line runs (`-{6,}`, `={6,}`,
# `_{6,}` on a standalone line) do NOT count as a pipe-table separator --
# only `|---|` does -- so an agent emitting `------` between key/value rows
# still trips the lint.
_SPEC_ROW_RE = re.compile(r"^[^|]{1,80}\s+\S.*$")
_PIPE_SEPARATOR_RE = re.compile(r"\|\s*-{3,}\s*\|")


def _check_spec_table(body: str) -> dict[str, str] | None:
    """Lint email body for spec rows missing a pipe-table separator (§V.42).

    Returns the format error dict when the body has >=3 consecutive lines
    matching the spec-row shape (1-80 non-pipe chars, one-or-more spaces,
    non-whitespace) without any pipe character on those lines, and no
    Markdown pipe-table separator (``|---|`` / ``| --- |``) anywhere in
    the body. Non-matching lines reset the consecutive counter so prose
    paragraphs (short greetings, blank-line-separated sentences) do not
    accumulate to the trigger threshold. Returns ``None`` otherwise so the
    caller can proceed to send.
    """
    if _PIPE_SEPARATOR_RE.search(body):
        return None
    consecutive = 0
    for line in body.splitlines():
        if "|" in line:
            consecutive = 0
            continue
        if _SPEC_ROW_RE.match(line):
            consecutive += 1
            if consecutive >= 3:
                return {
                    "error": "format",
                    "message": (
                        "spec body MUST use Markdown pipe-table w/ "
                        "|---| header separator"
                    ),
                }
        else:
            consecutive = 0
    return None


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
    format_error = _check_spec_table(body)
    if format_error is not None:
        bypass = _consume_reply_rejection(
            tool="send_email",
            rejection_type="format",
            workflow_id=workflow_id,
            email_id=None,
        )
        if not bypass:
            return format_error
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
    format_error = _check_spec_table(body)
    if format_error is not None:
        bypass = _consume_reply_rejection(
            tool="reply_email",
            rejection_type="format",
            workflow_id=workflow_id,
            email_id=email_id,
        )
        if not bypass:
            return format_error
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

    return {
        "id": email.id,
        "gmail_message_id": email.gmail_message_id,
        "gmail_thread_id": email.gmail_thread_id,
    }


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
        scheduled_at: When to execute (ISO 8601 timestamp).
        context: Arbitrary JSON context for the agent on re-invocation.
        email_id: Optional triggering email for focused context.

    Returns:
        Dict with created task ID.
    """
    task = database.create_task(
        connection,
        enrollment_id=enrollment_id,
        workflow_id=workflow_id,
        contact_id=contact_id,
        description=description,
        scheduled_at=scheduled_at,
        context=context,
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
          the contact, and writes a note.
        - ``contact_later`` -- records a failed outcome and schedules a
          re-enrollment first-touch task at ``reschedule_at`` (about three
          months out when omitted), then writes a note.

    Args:
        connection: Open database connection.
        enrollment_id: Current enrollment ID (from deps, not the LLM).
        disposition: One of meeting_booked, do_not_contact, contact_later.
        note: The agent's explanation, written to the outcome activity and a
            contact note.
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

    contact_id = enrollment.contact_id
    outcome = _CONCLUDE_OUTCOME[disposition]
    note_body = note or f"Enrollment concluded: {disposition}"

    database.record_enrollment_outcome(
        connection, enrollment_id, outcome=outcome, reason=note_body
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
            context={"trigger": "enrollment_schedule"},
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


def read_contact(
    connection: psycopg.Connection[dict[str, Any]],
    email: str,
) -> dict[str, Any]:
    """Look up a contact by email address with inlined notes.

    Routes through ``database.load_contact_view`` so the agent and the
    operator (CLI ``contact view``) see byte-identical context, including
    own notes and parent-company notes.

    Args:
        connection: Open database connection.
        email: Contact email address.

    Returns:
        ContactView dict on success; an error dict on lookup failure.
    """
    contact = database.get_contact_by_email(connection, email)
    if contact is None:
        return {"error": "not_found", "message": f"contact not found: {email}"}
    view = database.load_contact_view(connection, contact.id)
    if view is None:
        return {"error": "not_found", "message": f"contact not found: {email}"}
    return view.model_dump(mode="json")


def read_company(
    connection: psycopg.Connection[dict[str, Any]],
    domain: str,
) -> dict[str, Any]:
    """Look up a company by domain with inlined notes.

    Routes through ``database.load_company_view`` so the agent and the
    operator (CLI ``company view``) see byte-identical context including
    company notes.

    Args:
        connection: Open database connection.
        domain: Company primary domain.

    Returns:
        CompanyView dict on success; an error dict on lookup failure.
    """
    company = database.get_company_by_domain(connection, domain)
    if company is None:
        return {"error": "not_found", "message": f"company not found: {domain}"}
    view = database.load_company_view(connection, company.id)
    if view is None:
        return {"error": "not_found", "message": f"company not found: {domain}"}
    return view.model_dump(mode="json")


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
    signal.

    Args:
        reason: Why no action is needed.

    Returns:
        Acknowledgement dict.
    """
    return {"acknowledged": True, "reason": reason}
