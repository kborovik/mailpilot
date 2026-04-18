"""Sync loop lifecycle and per-account Gmail sync pipeline.

Runs as a foreground process managed by systemd. Provides the lifecycle
shell (``start_sync_loop``) plus the per-account sync entry point
(``sync_account``) that is invoked from the Pub/Sub callback for each
inbound notification. The Pub/Sub subscriber and watch renewal are not
yet wired up.

Usage::

    mailpilot run          # blocks until SIGTERM/SIGINT
    systemctl start mailpilot
    systemctl stop mailpilot   # sends SIGTERM -> graceful shutdown
"""

from __future__ import annotations

import os
import signal
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import click
import logfire
import psycopg

from mailpilot.database import (
    create_email,
    create_or_get_contact_by_email,
    delete_sync_status,
    get_email_by_gmail_message_id,
    get_emails_by_gmail_thread_id,
    get_sync_status,
    update_account,
    update_email,
    update_sync_heartbeat,
    upsert_sync_status,
)
from mailpilot.gmail import (
    GmailClient,
    extract_text_from_message,
    get_message_headers,
    parse_sender,
)
from mailpilot.models import Account, Email
from mailpilot.settings import Settings

_HEARTBEAT_INTERVAL = 30  # seconds
_RECENCY_WINDOW = timedelta(days=7)
_FULL_SYNC_MAX_RESULTS = 100


def is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists but we can't signal it
    return True


def start_sync_loop(connection: psycopg.Connection[dict[str, Any]]) -> None:
    """Run the sync loop (blocks until SIGTERM/SIGINT).

    Lifecycle:
    1. Check for stale sync_status row (dead PID) and overwrite
    2. Register current PID in sync_status
    3. Register SIGTERM/SIGINT handlers
    4. Loop: heartbeat every 30s, check shutdown event
    5. On shutdown: delete sync_status row

    Args:
        connection: Open database connection.
    """
    pid = os.getpid()
    shutdown_event = threading.Event()

    # Check for stale sync_status from a crashed process.
    existing = get_sync_status(connection)
    if existing is not None and is_pid_alive(existing.pid):
        logfire.warn(
            "sync.loop.already_running",
            pid=pid,
            existing_pid=existing.pid,
        )
        raise SystemExit(
            f"sync loop already running (pid {existing.pid}) -- "
            "stop it first or check with 'mailpilot status'"
        )
    if existing is not None:
        click.echo(f"Removing stale sync status (pid {existing.pid} is dead)")

    # Register this process.
    upsert_sync_status(connection, pid)
    logfire.info("sync.loop.start", pid=pid)
    click.echo(f"Sync loop started (pid {pid})")
    click.echo(f"Heartbeat interval: {_HEARTBEAT_INTERVAL}s")
    click.echo("Press Ctrl+C or send SIGTERM to stop")

    # Signal handlers set the shutdown event.
    def _handle_shutdown(signum: int, frame: object) -> None:
        signal_name = signal.Signals(signum).name
        logfire.info("sync.shutdown.signal_received", pid=pid, signal=signum)
        click.echo(f"\nReceived {signal_name}, shutting down...")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    # Main loop: heartbeat until shutdown.
    try:
        while not shutdown_event.is_set():
            shutdown_event.wait(timeout=_HEARTBEAT_INTERVAL)
            if not shutdown_event.is_set():
                update_sync_heartbeat(connection)
                logfire.debug("sync.loop.heartbeat", pid=pid)
    finally:
        delete_sync_status(connection)
        logfire.info("sync.loop.stop", pid=pid)
        click.echo("Sync loop stopped")


# -- Per-account sync ---------------------------------------------------------


def sync_account(
    connection: psycopg.Connection[dict[str, Any]],
    account: Account,
    gmail_client: GmailClient,
    settings: Settings,
) -> int:
    """Sync new inbound messages for a single Gmail account.

    Runs the per-account inbound pipeline (see ``docs/email-flow.md``):

    1. Incremental sync via ``GmailClient.get_history`` when the account
       has a stored ``gmail_history_id``. Falls back to a full INBOX
       listing on history 404.
    2. For each new message: fetch, extract text, auto-resolve the
       sender to a contact, and store an ``inbound`` email row.
    3. Apply the 7-day recency gate: messages older than the window land
       with ``is_routed=True`` / ``workflow_id=NULL``; fresher messages
       are handed to ``route_email`` for thread matching.
    4. Update ``gmail_history_id`` and ``last_synced_at`` on the account.

    Args:
        connection: Open database connection.
        account: Account to sync.
        gmail_client: Gmail client scoped to the account.
        settings: Application settings (reserved for future use).

    Returns:
        Number of newly stored email rows.
    """
    del settings  # reserved for future tuning (recency window, etc.)
    with logfire.span(
        "sync.account.run",
        account_id=account.id,
        email=account.email,
    ) as span:
        try:
            # Snapshot the mailbox's current historyId BEFORE syncing. Any
            # message that arrives during this run will still be above this
            # checkpoint and be picked up on the next incremental sync.
            checkpoint = gmail_client.get_profile().get("historyId") or ""
            message_ids, mode = _collect_new_message_ids(account, gmail_client)
            span.set_attribute("mode", mode)
            stored = 0
            for message_id in message_ids:
                if get_email_by_gmail_message_id(connection, message_id) is not None:
                    continue
                message = gmail_client.get_message(message_id)
                if message is None:
                    continue
                _store_inbound_message(connection, account, message)
                stored += 1
            update_account(
                connection,
                account.id,
                gmail_history_id=checkpoint or account.gmail_history_id,
                last_synced_at=datetime.now(UTC),
            )
            span.set_attribute("message_count", stored)
            span.set_attribute("result", "success")
            return stored
        except Exception:
            span.set_attribute("result", "failure")
            logfire.exception("sync.account.run failed", account_id=account.id)
            raise


def _collect_new_message_ids(
    account: Account,
    gmail_client: GmailClient,
) -> tuple[list[str], str]:
    """Return message IDs to fetch and the sync mode used.

    Tries incremental sync first when a history ID is known, falling
    back to a full INBOX listing on a 404. Callers still dedupe against
    the ``email`` table before fetching, so duplicates within the list
    (e.g. same message in multiple history records) are harmless.
    """
    from googleapiclient.errors import HttpError

    if account.gmail_history_id:
        try:
            history = gmail_client.get_history(
                start_history_id=account.gmail_history_id,
                history_types=["messageAdded"],
                label_id="INBOX",
            )
        except HttpError as exc:
            if exc.resp.status != 404:
                raise
            logfire.warn(
                "sync.account.history_fallback",
                account_id=account.id,
                old_history_id=account.gmail_history_id,
            )
        else:
            return (_extract_added_message_ids(history), "incremental")

    stubs = gmail_client.list_messages(
        max_results=_FULL_SYNC_MAX_RESULTS,
        label_ids=["INBOX"],
    )
    ids = [stub["id"] for stub in stubs if stub.get("id")]
    return (list(dict.fromkeys(ids)), "full")


def _extract_added_message_ids(history: list[dict[str, Any]]) -> list[str]:
    """Pull unique message IDs out of a ``messagesAdded`` history response."""
    ids: list[str] = []
    seen: set[str] = set()
    for record in history:
        for added in record.get("messagesAdded", []):
            message_id = added.get("message", {}).get("id")
            if message_id and message_id not in seen:
                seen.add(message_id)
                ids.append(message_id)
    return ids


def _store_inbound_message(
    connection: psycopg.Connection[dict[str, Any]],
    account: Account,
    message: dict[str, Any],
) -> Email:
    """Persist a Gmail message as an inbound email and route when fresh."""
    headers = get_message_headers(message)
    sender_email, first_name, last_name = parse_sender(headers.get("from", ""))
    contact = create_or_get_contact_by_email(
        connection,
        email=sender_email,
        first_name=first_name,
        last_name=last_name,
    )
    received_at = _received_at_from_message(message)
    within_window = (
        received_at is not None and datetime.now(UTC) - received_at <= _RECENCY_WINDOW
    )
    email = create_email(
        connection,
        account_id=account.id,
        direction="inbound",
        subject=headers.get("subject", ""),
        body_text=extract_text_from_message(message),
        gmail_message_id=message.get("id"),
        gmail_thread_id=message.get("threadId"),
        contact_id=contact.id,
        is_routed=not within_window,
        received_at=received_at,
        labels=list(message.get("labelIds", [])),
    )
    logfire.debug(
        "sync.account.message_stored",
        account_id=account.id,
        email_id=email.id,
        gmail_message_id=message.get("id"),
        within_recency_window=within_window,
    )
    if within_window:
        email = route_email(connection, email)
    return email


def _received_at_from_message(message: dict[str, Any]) -> datetime | None:
    """Parse Gmail's ``internalDate`` (epoch ms string) into a UTC datetime."""
    raw = message.get("internalDate")
    if not raw:
        return None
    try:
        ms = int(raw)
    except TypeError, ValueError:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


# -- Routing ------------------------------------------------------------------


def route_email(
    connection: psycopg.Connection[dict[str, Any]],
    email: Email,
) -> Email:
    """Route an inbound email to a workflow via thread matching.

    Implements step 1 of the ADR-04 routing pipeline. If a prior email
    in the same Gmail thread has a non-null ``workflow_id``, assign the
    most recent such workflow and mark the email ``is_routed``. If no
    thread match exists, the email is left unchanged -- a future
    classification pass (see ``mailpilot.agent.classify.classify_email``)
    resolves it.

    Args:
        connection: Open database connection.
        email: Newly stored inbound email to route.

    Returns:
        Possibly updated email (unchanged on no thread match).
    """
    with logfire.span(
        "sync.route_email",
        email_id=email.id,
        account_id=email.account_id,
    ) as span:
        try:
            if not email.gmail_thread_id:
                span.set_attribute("result", "skipped")
                return email
            thread_emails = get_emails_by_gmail_thread_id(
                connection, email.gmail_thread_id
            )
            matches = [
                prior
                for prior in thread_emails
                if prior.id != email.id and prior.workflow_id is not None
            ]
            if not matches:
                span.set_attribute("result", "no_match")
                return email
            matches.sort(key=lambda e: e.created_at, reverse=True)
            workflow_id = matches[0].workflow_id
            updated = update_email(
                connection, email.id, workflow_id=workflow_id, is_routed=True
            )
            span.set_attribute("result", "thread_match")
            span.set_attribute("workflow_id", workflow_id)
            return updated if updated is not None else email
        except Exception:
            span.set_attribute("result", "failure")
            logfire.exception("sync.route_email failed", email_id=email.id)
            raise
