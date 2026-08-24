"""Email CRUD and thread lookups."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg.sql import SQL, Composable, Identifier, Placeholder
from psycopg.types.json import Json

from mailpilot.database._common import (
    _new_id,
)
from mailpilot.models import (
    Email,
    EmailSummary,
)

# -- Email ---------------------------------------------------------------------


def create_email(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    direction: str,
    subject: str = "",
    body_text: str = "",
    gmail_message_id: str | None = None,
    gmail_thread_id: str | None = None,
    contact_id: str | None = None,
    workflow_id: str | None = None,
    status: str = "received",
    is_routed: bool = False,
    route_method: str | None = None,
    received_at: datetime | None = None,
    sent_at: datetime | None = None,
    labels: list[str] | None = None,
    rfc2822_message_id: str | None = None,
    in_reply_to: str | None = None,
    references_header: str | None = None,
    sender: str = "",
    recipients: dict[str, list[str]] | None = None,
) -> Email | None:
    """Create a new email record, or return None on gmail_message_id conflict.

    Insert is atomic via ``ON CONFLICT (gmail_message_id) DO NOTHING``, so two
    concurrent workers attempting to store the same Gmail message will never
    raise ``UniqueViolation``: one wins and returns the row, the other
    returns ``None``. Outbound rows with ``gmail_message_id=NULL`` never
    trigger the conflict (NULLs are distinct under a UNIQUE constraint).

    Args:
        connection: Open database connection.
        account_id: Account FK.
        direction: "inbound" or "outbound".
        subject: Email subject.
        body_text: Plain text body.
        gmail_message_id: Gmail message ID.
        gmail_thread_id: Gmail thread ID.
        contact_id: Optional contact FK.
        workflow_id: Optional workflow FK.
        status: Email status ("sent" or "received").
        is_routed: Whether the routing pipeline has processed this email.
        route_method: Persisted routing decision (per §I email projection;
            e.g. ``thread_match``, ``classified``, ``skipped_outside_window``).
        received_at: When Gmail reports the message arrived (UTC datetime).
        sent_at: When the outbound message was handed to Gmail (UTC datetime).
        labels: Gmail label IDs attached to the message.
        rfc2822_message_id: RFC 2822 Message-ID header value.
        in_reply_to: RFC 2822 In-Reply-To header value (parent message id).
        references_header: RFC 2822 References header value (full
            whitespace-separated chain of ancestor message ids). Stored as
            ``references_header`` because ``references`` is a reserved SQL
            keyword.
        sender: Sender email address (lowercase).
        recipients: Recipient addresses grouped by type
            (``{"to": [...], "cc": [...], "bcc": [...]}``)

    Returns:
        Created email, or None if another worker already stored a row with
        the same ``gmail_message_id``.
    """
    row = connection.execute(
        """\
        INSERT INTO email (id, account_id, direction, subject,
            body_text, gmail_message_id, gmail_thread_id,
            contact_id, workflow_id, status, is_routed, route_method,
            received_at, sent_at, labels, rfc2822_message_id,
            in_reply_to, references_header,
            sender, recipients)
        VALUES (%(id)s, %(account_id)s, %(direction)s,
            %(subject)s, %(body_text)s, %(gmail_message_id)s,
            %(gmail_thread_id)s, %(contact_id)s, %(workflow_id)s,
            %(status)s, %(is_routed)s, %(route_method)s,
            %(received_at)s, %(sent_at)s,
            %(labels)s, %(rfc2822_message_id)s,
            %(in_reply_to)s, %(references_header)s,
            %(sender)s, %(recipients)s)
        ON CONFLICT (gmail_message_id) DO NOTHING
        RETURNING *
        """,
        {
            "id": _new_id(),
            "account_id": account_id,
            "direction": direction,
            "subject": subject,
            "body_text": body_text,
            "gmail_message_id": gmail_message_id,
            "gmail_thread_id": gmail_thread_id,
            "contact_id": contact_id,
            "workflow_id": workflow_id,
            "status": status,
            "is_routed": is_routed,
            "route_method": route_method,
            "received_at": received_at,
            "sent_at": sent_at,
            "labels": Json(labels or []),
            "rfc2822_message_id": rfc2822_message_id,
            "in_reply_to": in_reply_to,
            "references_header": references_header,
            "sender": sender,
            "recipients": Json(recipients or {}),
        },
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Email.model_validate(row)


def get_email(
    connection: psycopg.Connection[dict[str, Any]],
    email_id: str,
) -> Email | None:
    """Get an email by ID.

    Args:
        connection: Open database connection.
        email_id: Email ID.

    Returns:
        Email if found, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM email WHERE id = %(id)s",
        {"id": email_id},
    ).fetchone()
    if row is None:
        return None
    return Email.model_validate(row)


_EMAIL_SUMMARY_COLUMNS = SQL(
    "id, account_id, contact_id, workflow_id, direction, "
    "subject, sender, recipients, status, is_routed, route_method, "
    "gmail_thread_id, sent_at, received_at, "
    "LEFT(body_text, 500) AS snippet"
)


def list_emails(
    connection: psycopg.Connection[dict[str, Any]],
    limit: int | None = 100,
    contact_id: str | None = None,
    account_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    thread_id: str | None = None,
    direction: str | None = None,
    workflow_id: str | None = None,
    status: str | None = None,
    sender: str | None = None,
    recipient: str | None = None,
    route_method: str | None = None,
) -> list[EmailSummary]:
    """List emails as summaries with optional filters.

    Args:
        connection: Open database connection.
        limit: Maximum results. ``None`` omits LIMIT (review path §V.174).
        contact_id: Filter by contact ID.
        account_id: Filter by account ID.
        since: ISO datetime inclusive lower bound for
            ``COALESCE(sent_at, received_at)``.
        until: ISO datetime inclusive upper bound for
            ``COALESCE(sent_at, received_at)``.
        thread_id: Filter by Gmail thread ID.
        direction: Filter by direction ("inbound" or "outbound").
        workflow_id: Filter by workflow ID.
        status: Filter by email status ("sent", "received", "bounced").
        sender: Filter by sender email address (case-insensitive).
        recipient: Filter by recipient address in recipients JSONB
            (case-insensitive, matches to/cc/bcc).
        route_method: Filter by persisted routing decision (per
            §I email projection).

    Returns:
        List of email summaries ordered by ``COALESCE(sent_at, received_at)``
        descending -- the same expression used by the ``since`` filter, so
        operators can page newest-first using a timestamp visible in
        ``EmailSummary``.
    """
    conditions: list[Composable] = []
    params: dict[str, object] = {}
    if limit is not None:
        params["limit"] = limit
    # Simple equality filters keyed (param_name -> (column_name, value)).
    equality_filters: dict[str, tuple[str, str | None]] = {
        "contact_id": ("contact_id", contact_id),
        "account_id": ("account_id", account_id),
        "thread_id": ("gmail_thread_id", thread_id),
        "direction": ("direction", direction),
        "workflow_id": ("workflow_id", workflow_id),
        "status": ("status", status),
        "route_method": ("route_method", route_method),
    }
    for param_name, (column, value) in equality_filters.items():
        if value is None:
            continue
        conditions.append(
            SQL("{} = {}").format(Identifier(column), Placeholder(param_name))
        )
        params[param_name] = value
    if since is not None:
        conditions.append(SQL("COALESCE(sent_at, received_at) >= %(since)s"))
        params["since"] = since
    if until is not None:
        conditions.append(SQL("COALESCE(sent_at, received_at) <= %(until)s"))
        params["until"] = until
    if sender is not None:
        conditions.append(SQL("LOWER(sender) = LOWER(%(sender)s)"))
        params["sender"] = sender
    if recipient is not None:
        conditions.append(
            SQL("LOWER(recipients::text) LIKE LOWER(%(recipient_pattern)s)")
        )
        params["recipient_pattern"] = f"%{recipient}%"
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    limit_sql = SQL(" LIMIT %(limit)s") if limit is not None else SQL("")
    query = SQL(
        "SELECT {} FROM email {} ORDER BY COALESCE(sent_at, received_at) DESC{}"
    ).format(_EMAIL_SUMMARY_COLUMNS, where, limit_sql)
    rows = connection.execute(query, params).fetchall()
    return [EmailSummary.model_validate(row) for row in rows]


def list_enrollment_emails(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    account_id: str,
    contact_id: str,
    workflow_id: str,
) -> list[Email]:
    """Load enrollment-scoped email history as full Email rows (§V.183).

    One query returning ``body_text``. Distinct from ``list_emails``
    (EmailSummary, snippet only). Scope is (account_id, contact_id,
    workflow_id) per §V.82.

    Args:
        connection: Open database connection.
        account_id: Sending account.
        contact_id: Enrolled contact.
        workflow_id: Enrollment workflow.

    Returns:
        Full Email rows, newest first by ``COALESCE(sent_at, received_at)``.
    """
    rows = connection.execute(
        """\
        SELECT * FROM email
        WHERE account_id = %(account_id)s
          AND contact_id = %(contact_id)s
          AND workflow_id = %(workflow_id)s
        ORDER BY COALESCE(sent_at, received_at) DESC
        """,
        {
            "account_id": account_id,
            "contact_id": contact_id,
            "workflow_id": workflow_id,
        },
    ).fetchall()
    return [Email.model_validate(row) for row in rows]


def search_emails(
    connection: psycopg.Connection[dict[str, Any]],
    query: str,
    limit: int = 100,
    account_id: str | None = None,
) -> list[EmailSummary]:
    """Search emails by subject, body text, sender, or recipients.

    Args:
        connection: Open database connection.
        query: Search term.
        limit: Maximum number of results.
        account_id: Filter by account ID.

    Returns:
        Matching email summaries ordered by creation time descending.
    """
    pattern = f"%{query}%"
    params: dict[str, object] = {"pattern": pattern, "limit": limit}
    account_filter = SQL("")
    if account_id is not None:
        account_filter = SQL("AND account_id = %(account_id)s")
        params["account_id"] = account_id
    query_sql = SQL(
        "SELECT {} FROM email "
        "WHERE (LOWER(subject) LIKE LOWER(%(pattern)s) "
        "   OR LOWER(body_text) LIKE LOWER(%(pattern)s) "
        "   OR LOWER(sender) LIKE LOWER(%(pattern)s) "
        "   OR LOWER(recipients::text) LIKE LOWER(%(pattern)s)) "
        "{} "
        "ORDER BY created_at DESC "
        "LIMIT %(limit)s"
    ).format(_EMAIL_SUMMARY_COLUMNS, account_filter)
    rows = connection.execute(query_sql, params).fetchall()
    return [EmailSummary.model_validate(row) for row in rows]


def get_email_by_gmail_message_id(
    connection: psycopg.Connection[dict[str, Any]],
    gmail_message_id: str,
) -> Email | None:
    """Get an email by Gmail message ID.

    Args:
        connection: Open database connection.
        gmail_message_id: Gmail message ID (unique).

    Returns:
        Email if found, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM email WHERE gmail_message_id = %(gmail_message_id)s",
        {"gmail_message_id": gmail_message_id},
    ).fetchone()
    if row is None:
        return None
    return Email.model_validate(row)


def get_emails_by_gmail_thread_id(
    connection: psycopg.Connection[dict[str, Any]],
    gmail_thread_id: str,
) -> list[Email]:
    """Get all emails in a Gmail thread.

    Args:
        connection: Open database connection.
        gmail_thread_id: Gmail thread ID.

    Returns:
        Emails in the thread ordered by creation time.
    """
    rows = connection.execute(
        """\
        SELECT * FROM email
        WHERE gmail_thread_id = %(gmail_thread_id)s
        ORDER BY created_at
        """,
        {"gmail_thread_id": gmail_thread_id},
    ).fetchall()
    return [Email.model_validate(row) for row in rows]


def list_inbound_emails_from_contact_after(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
    after: datetime,
) -> list[Email]:
    """Return inbound emails from the contact after ``after`` (§V.83).

    The touch pre-flight classifies each row (OOO vs real reply) so an
    out-of-office auto-reply does not cancel a queued follow-up.

    Args:
        connection: Open database connection.
        contact_id: Contact FK (set on inbound rows via sender resolution).
        after: The prior touch's send moment -- only later inbound counts.

    Returns:
        Matching inbound emails, oldest first.
    """
    rows = connection.execute(
        """\
        SELECT * FROM email
        WHERE contact_id = %(contact_id)s
          AND direction = 'inbound'
          AND COALESCE(received_at, sent_at) > %(after)s
        ORDER BY COALESCE(received_at, sent_at) ASC
        """,
        {"contact_id": contact_id, "after": after},
    ).fetchall()
    return [Email.model_validate(row) for row in rows]


def get_latest_email_in_thread(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    gmail_thread_id: str,
) -> Email | None:
    """Get the most recently created email in a Gmail thread for an account.

    Used when sending a reply into an existing thread to pull the prior
    message's ``rfc2822_message_id`` for the outgoing ``In-Reply-To`` /
    ``References`` headers. Scoping by ``account_id`` keeps the lookup
    deterministic when the same Gmail thread ID is observed on multiple
    delegated mailboxes (e.g. sender + recipient on the same domain).

    Args:
        connection: Open database connection.
        account_id: Account FK.
        gmail_thread_id: Gmail thread ID.

    Returns:
        Most recently created email in the thread, or None if the thread
        has no rows for this account.
    """
    row = connection.execute(
        """\
        SELECT * FROM email
        WHERE account_id = %(account_id)s
          AND gmail_thread_id = %(gmail_thread_id)s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"account_id": account_id, "gmail_thread_id": gmail_thread_id},
    ).fetchone()
    if row is None:
        return None
    return Email.model_validate(row)


def find_email_by_rfc2822_message_id(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    message_ids: list[str],
) -> Email | None:
    """Find the most recent email matching any of the given RFC 2822 message ids.

    Used by the routing pipeline as a fallback when ``gmail_thread_id`` no
    longer joins inbound replies to their outbound parents (Gmail re-threads
    on the recipient side, producing a different ``threadId`` for the same
    conversation). Restricted to a single ``account_id`` so cross-account
    collisions on a shared Message-ID cannot leak workflow assignments.

    Args:
        connection: Open database connection.
        account_id: Account scope -- only rows belonging to this account
            are considered.
        message_ids: Candidate RFC 2822 Message-ID values, typically the
            inbound email's ``In-Reply-To`` plus every entry in its
            ``References`` chain.

    Returns:
        The most-recently created matching email, or ``None`` when no row
        in this account stores any of ``message_ids`` in its
        ``rfc2822_message_id`` column.
    """
    if not message_ids:
        return None
    row = connection.execute(
        """\
        SELECT * FROM email
        WHERE account_id = %(account_id)s
          AND rfc2822_message_id = ANY(%(message_ids)s)
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"account_id": account_id, "message_ids": message_ids},
    ).fetchone()
    if row is None:
        return None
    return Email.model_validate(row)


def get_last_cold_outbound(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    contact_id: str,
    workflow_id: str,
) -> Email | None:
    """Get the most recent cold outbound email to a contact within a workflow.

    A cold outbound email is the first outbound message in its Gmail
    thread (no prior outbound in the same thread). This distinguishes
    initial outreach from follow-up replies within an existing
    conversation. Used by the ``send_email`` agent tool for cooldown
    enforcement. Scoped to a single workflow so that independent
    campaigns can each send their first outreach independently.

    Args:
        connection: Open database connection.
        account_id: Sending account.
        contact_id: Recipient contact.
        workflow_id: Workflow scope for cooldown.

    Returns:
        Most recent cold outbound email, or None if none exists.
    """
    row = connection.execute(
        """\
        SELECT e.* FROM email e
        WHERE e.account_id = %(account_id)s
          AND e.contact_id = %(contact_id)s
          AND e.workflow_id = %(workflow_id)s
          AND e.direction = 'outbound'
          AND NOT EXISTS (
              SELECT 1 FROM email prior
              WHERE prior.gmail_thread_id = e.gmail_thread_id
                AND prior.gmail_thread_id IS NOT NULL
                AND prior.account_id = e.account_id
                AND prior.direction = 'outbound'
                AND prior.created_at < e.created_at
          )
        ORDER BY e.created_at DESC
        LIMIT 1
        """,
        {
            "account_id": account_id,
            "contact_id": contact_id,
            "workflow_id": workflow_id,
        },
    ).fetchone()
    if row is None:
        return None
    return Email.model_validate(row)


def update_email(
    connection: psycopg.Connection[dict[str, Any]],
    email_id: str,
    **fields: object,
) -> Email | None:
    """Update an email by ID.

    Args:
        connection: Open database connection.
        email_id: Email ID.
        **fields: Fields to update (must be valid Email field names).

    Returns:
        Updated email, or None if not found.
    """
    allowed = {
        "workflow_id",
        "is_routed",
        "route_method",
        "status",
        "contact_id",
        "rfc2822_message_id",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_email(connection, email_id)
    updates["id"] = email_id
    # email table has no updated_at column -- use raw SQL instead of _build_update
    set_parts = [
        SQL("{} = {}").format(Identifier(k), Placeholder(k))
        for k in updates
        if k != "id"
    ]
    set_clause = SQL(", ").join(set_parts)
    query = SQL("UPDATE email SET {} WHERE id = %(id)s RETURNING *").format(set_clause)
    row = connection.execute(query, updates).fetchone()
    connection.commit()
    if row is None:
        return None
    return Email.model_validate(row)
