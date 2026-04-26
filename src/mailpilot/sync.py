"""Sync loop lifecycle and per-account Gmail sync pipeline.

Runs as a foreground process managed by systemd. Provides the unified
lifecycle entry point (``start_sync_loop``) plus the per-account sync
entry point (``sync_account``).

The sync loop integrates:

- **Pub/Sub subscriber** for real-time Gmail notifications
- **PG LISTEN/NOTIFY** for instant task execution on INSERT
- **Periodic sync** as a fallback (every ``run_interval`` seconds)
- **Watch renewal** (hourly)
- **Task execution** via ``execute_task()`` from ``run.py``

Usage::

    mailpilot run          # blocks until SIGTERM/SIGINT
    systemctl start mailpilot
    systemctl stop mailpilot   # sends SIGTERM -> graceful shutdown
"""

from __future__ import annotations

import os
import queue
import signal
import threading
import time
from datetime import UTC, datetime, timedelta
from email.utils import formataddr, getaddresses
from typing import Any

import click
import logfire
import psycopg

from mailpilot.database import (
    create_contacts_bulk,
    create_email,
    create_or_get_contact_by_email,
    create_tasks_for_routed_emails,
    delete_sync_status,
    get_account_by_email,
    get_contacts_by_emails,
    get_email_by_gmail_message_id,
    get_sync_status,
    list_accounts,
    list_pending_tasks,
    list_workflows,
    update_account,
    update_contact,
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
from mailpilot.models import Account, Contact, Email
from mailpilot.routing import route_email
from mailpilot.settings import Settings

_HEARTBEAT_INTERVAL = 30  # seconds
_RECENCY_WINDOW = timedelta(days=7)
_FULL_SYNC_MAX_RESULTS = 100
_WATCH_RENEWAL_INTERVAL = 3600  # seconds (1 hour)

# -- Metrics -------------------------------------------------------------------

sync_messages_stored = logfire.metric_counter(
    "sync.messages.stored",
    description="Inbound messages persisted",
)
sync_account_duration = logfire.metric_histogram(
    "sync.account.duration_ms",
    unit="ms",
    description="Wall time of sync_account per run",
)
sync_errors = logfire.metric_counter(
    "sync.errors",
    description="Errors during per-account sync",
)


def is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists but we can't signal it
    return True


def _check_stale_sync_status(
    connection: psycopg.Connection[dict[str, Any]],
    pid: int,
) -> None:
    """Check for stale sync_status from a crashed process."""
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


def start_sync_loop(
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
) -> None:
    """Run the unified sync loop (blocks until SIGTERM/SIGINT).

    Integrates Pub/Sub subscriber, PG LISTEN/NOTIFY for tasks, periodic
    account sync, task execution, and watch renewal into a single process.

    Lifecycle:
    1. Check for stale sync_status row (dead PID) and overwrite
    2. Register current PID in sync_status
    3. Start Pub/Sub subscriber (if credentials configured)
    4. Start PG LISTEN thread for task notifications
    5. Loop: periodic sync + task drain + heartbeat
    6. On shutdown: stop subscriber, clean up

    Args:
        connection: Open database connection.
        settings: Application settings.
    """
    pid = os.getpid()
    shutdown_event = threading.Event()
    # Single wakeup event the main loop blocks on. Pub/Sub callback,
    # PG LISTEN thread, and signal handler all set it. ``run_interval``
    # is the upper-bound fallback, not the primary trigger -- otherwise
    # the real-time delivery path silently degrades to polling.
    wakeup_event = threading.Event()

    _check_stale_sync_status(connection, pid)
    upsert_sync_status(connection, pid)
    logfire.info("sync.loop.start", pid=pid)
    click.echo(f"Sync loop started (pid {pid})")
    click.echo(f"Interval: {settings.run_interval}s")
    click.echo("Press Ctrl+C or send SIGTERM to stop")

    # Signal handlers set both events. shutdown_event tells the loop
    # to stop; wakeup_event unblocks any in-flight wait so the stop is
    # observed immediately. After the first signal we restore the
    # default handlers so a second Ctrl+C / SIGTERM always exits, even
    # if the main thread is blocked in a C-level call (e.g. gRPC inside
    # Pub/Sub setup) where the events are never observed.
    def _handle_shutdown(signum: int, frame: object) -> None:
        signal_name = signal.Signals(signum).name
        logfire.info("sync.shutdown.signal_received", pid=pid, signal=signum)
        click.echo(f"\nReceived {signal_name}, shutting down...")
        shutdown_event.set()
        wakeup_event.set()
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    # Start Pub/Sub subscriber (real-time Gmail notifications).
    sync_queue: queue.Queue[str] = queue.Queue()
    subscriber_future = None
    if settings.google_application_credentials:
        subscriber_future = _start_pubsub_logging_errors(
            connection, settings, sync_queue, wakeup_event
        )

    # Start PG LISTEN thread (instant task execution on INSERT).
    database_url = str(settings.database_url)
    listener_thread = _start_task_listener(database_url, wakeup_event, shutdown_event)

    # Main loop.
    last_watch_renewal = 0.0
    try:
        while not shutdown_event.is_set():
            wakeup_event.wait(timeout=settings.run_interval)
            if shutdown_event.is_set():
                break
            # Clear before processing, not after. Events that arrive
            # mid-iteration must re-set wakeup_event so the next wait
            # returns immediately; clearing afterwards would lose them.
            wakeup_event.clear()
            update_sync_heartbeat(connection)
            logfire.debug("sync.loop.heartbeat", pid=pid)

            _run_periodic_iteration(connection, settings, sync_queue)

            # Hourly watch renewal.
            now = time.monotonic()
            if now - last_watch_renewal >= _WATCH_RENEWAL_INTERVAL:
                _renew_watches_logging_errors(connection, settings)
                last_watch_renewal = now
    finally:
        # Graceful shutdown.
        if subscriber_future is not None:
            subscriber_future.cancel()
            logfire.info("sync.pubsub.subscriber.stopped")
        shutdown_event.set()  # signal listener thread
        listener_thread.join(timeout=5)
        delete_sync_status(connection)
        logfire.info("sync.loop.stop", pid=pid)
        click.echo("Sync loop stopped")


def _start_pubsub_logging_errors(
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
    sync_queue: queue.Queue[str],
    wakeup_event: threading.Event,
) -> Any:
    """Set up Pub/Sub infrastructure and start the subscriber.

    Returns the StreamingPullFuture, or None on failure.
    """
    from mailpilot.pubsub import (
        make_notification_callback,
        renew_watches,
        setup_pubsub,
        start_subscriber,
    )

    try:
        setup_pubsub(settings)
        renew_watches(connection, settings)
        callback = make_notification_callback(sync_queue, wakeup_event)
        future = start_subscriber(settings, callback)
        click.echo("Pub/Sub subscriber started")
        return future
    except Exception:
        logfire.exception("sync.pubsub.setup_failed")
        click.echo("Warning: Pub/Sub setup failed, running in polling-only mode")
        return None


def _start_task_listener(
    database_url: str,
    wakeup_event: threading.Event,
    shutdown_event: threading.Event,
) -> threading.Thread:
    """Start a daemon thread that listens for PG NOTIFY on task_pending.

    When a notification arrives, sets ``wakeup_event`` so the main loop
    drains the task queue immediately instead of waiting for the next
    periodic timer tick.
    """

    def _listen() -> None:
        listen_conn = psycopg.connect(database_url, autocommit=True)
        try:
            listen_conn.execute("LISTEN task_pending")
            while not shutdown_event.is_set():
                for _notify in listen_conn.notifies(timeout=30.0):
                    wakeup_event.set()
                    break
        except Exception:
            logfire.exception("sync.task_listener.error")
        finally:
            listen_conn.close()

    thread = threading.Thread(target=_listen, daemon=True)
    thread.start()
    return thread


def _run_periodic_iteration(
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
    sync_queue: queue.Queue[str],
) -> None:
    """Run one iteration of the periodic sync + task drain cycle."""
    with logfire.span("sync.loop.iteration"):
        # Sync accounts from Pub/Sub notifications.
        _drain_sync_queue(connection, settings, sync_queue)

        # Periodic sync of all accounts.
        _sync_all_accounts(connection, settings)

        # Bridge routed emails to tasks and drain the queue.
        create_tasks_for_routed_emails(connection)
        _drain_pending_tasks(connection, settings)


def _drain_sync_queue(
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
    sync_queue: queue.Queue[str],
) -> None:
    """Sync accounts that received Pub/Sub notifications."""
    synced: set[str] = set()
    while not sync_queue.empty():
        try:
            email = sync_queue.get_nowait()
        except queue.Empty:
            break
        if email in synced:
            continue
        account = get_account_by_email(connection, email)
        if account is None:
            logfire.debug("sync.notification.unknown_email", email=email)
            continue
        try:
            client = GmailClient(account.email)
            sync_account(connection, account, client, settings)
            synced.add(email)
        except Exception:
            logfire.exception(
                "sync.notification.sync_failed",
                account_id=account.id,
                email=account.email,
            )


def _sync_all_accounts(
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
) -> None:
    """Sync all Gmail accounts. Errors per account are logged, not raised."""
    accounts = list_accounts(connection)
    for account in accounts:
        try:
            client = GmailClient(account.email)
            sync_account(connection, account, client, settings)
        except Exception:
            logfire.exception(
                "sync.account.sync_failed",
                account_id=account.id,
                email=account.email,
            )


def _drain_pending_tasks(
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
) -> None:
    """Execute all pending tasks that are due."""
    from mailpilot.run import execute_task

    pending = list_pending_tasks(connection)
    for task in pending:
        execute_task(connection, settings, task)


def _renew_watches_logging_errors(
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
) -> None:
    """Renew Gmail watches, catching and logging errors."""
    if not settings.google_application_credentials:
        return
    try:
        from mailpilot.pubsub import renew_watches

        renewed = renew_watches(connection, settings)
        if renewed > 0:
            logfire.info("sync.watches.renewed", count=renewed)
    except Exception:
        logfire.exception("sync.watches.renewal_failed")


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
    start = time.monotonic()
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
            span.set_attribute("fetched_count", len(message_ids))
            # Filter out message IDs already stored, then batch-fetch the
            # remaining payloads.  Scales with 1-2 HTTP round-trips instead
            # of N individual get_message calls (#68).
            new_ids: list[str] = []
            duplicate_skipped_count = 0
            for message_id in message_ids:
                if get_email_by_gmail_message_id(connection, message_id) is not None:
                    duplicate_skipped_count += 1
                else:
                    new_ids.append(message_id)
            fresh_messages = gmail_client.get_messages_batch(new_ids)
            span.set_attribute("duplicate_skipped_count", duplicate_skipped_count)
            # Resolve every distinct sender in one pair of round-trips,
            # regardless of message count. Scales with unique senders, not
            # with mailbox size.
            contacts_by_email = _resolve_contacts_for_messages(
                connection, fresh_messages
            )
            active_workflows = list_workflows(
                connection, account_id=account.id, status="active"
            )
            has_active_workflows = bool(active_workflows)
            # Compute the earliest created_at among active inbound
            # workflows. Emails received before this timestamp can never
            # produce tasks (create_tasks_for_routed_emails filters on
            # received_at >= w.created_at), so classifying them via LLM
            # is pure waste.
            inbound_created = [
                w.created_at for w in active_workflows if w.type == "inbound"
            ]
            earliest_workflow_at = min(inbound_created) if inbound_created else None
            stored = 0
            for message in fresh_messages:
                if (
                    _store_inbound_message(
                        connection,
                        account,
                        message,
                        contacts_by_email,
                        settings,
                        has_active_workflows=has_active_workflows,
                        earliest_workflow_at=earliest_workflow_at,
                    )
                    is None
                ):
                    continue
                stored += 1
            update_account(
                connection,
                account.id,
                gmail_history_id=checkpoint or account.gmail_history_id,
                last_synced_at=datetime.now(UTC),
            )
            span.set_attribute("stored_count", stored)
            duration_ms = (time.monotonic() - start) * 1000
            sync_account_duration.record(
                duration_ms,
                attributes={"account_id": account.id, "mode": mode},
            )
            return stored
        except Exception:
            sync_errors.add(
                1, attributes={"account_id": account.id, "reason": "sync_exception"}
            )
            logfire.exception("sync.account.run failed", account_id=account.id)
            raise


def _resolve_contacts_for_messages(
    connection: psycopg.Connection[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> dict[str, Contact]:
    """Resolve every distinct sender in ``messages`` to a contact row.

    Bulk pre-fetch pass that replaces the former per-message
    ``create_or_get_contact_by_email`` call. Uses one SELECT to find
    existing contacts, one INSERT for the missing ones, and an optional
    backfill pass to populate first/last names where the From header has
    them and the contact row does not. Round-trips scale with distinct
    senders, not with message count.
    """
    best_names = _aggregate_sender_names(messages)
    if not best_names:
        return {}
    senders = list(best_names)
    with logfire.span(
        "sync.account.resolve_contacts",
        distinct_sender_count=len(senders),
    ) as span:
        contacts_by_email = get_contacts_by_emails(connection, senders)
        missing = [email for email in senders if email not in contacts_by_email]
        span.set_attribute("existing_count", len(contacts_by_email))
        span.set_attribute("missing_count", len(missing))
        if missing:
            contacts_by_email.update(create_contacts_bulk(connection, missing))
        _backfill_contact_names(connection, contacts_by_email, best_names)
        return contacts_by_email


def _aggregate_sender_names(
    messages: list[dict[str, Any]],
) -> dict[str, tuple[str | None, str | None]]:
    """Collect the first non-null first/last name per sender across ``messages``."""
    best_names: dict[str, tuple[str | None, str | None]] = {}
    for message in messages:
        headers = get_message_headers(message)
        sender_email, first_name, last_name = parse_sender(headers.get("from", ""))
        if not sender_email:
            continue
        current_first, current_last = best_names.get(sender_email, (None, None))
        best_names[sender_email] = (
            current_first or first_name,
            current_last or last_name,
        )
    return best_names


def _backfill_contact_names(
    connection: psycopg.Connection[dict[str, Any]],
    contacts_by_email: dict[str, Contact],
    best_names: dict[str, tuple[str | None, str | None]],
) -> None:
    """Populate NULL first/last names from From-header values, in place."""
    for email, (first_name, last_name) in best_names.items():
        contact = contacts_by_email.get(email)
        if contact is None:
            continue
        backfill: dict[str, object] = {}
        if contact.first_name is None and first_name is not None:
            backfill["first_name"] = first_name
        if contact.last_name is None and last_name is not None:
            backfill["last_name"] = last_name
        if not backfill:
            continue
        updated = update_contact(connection, contact.id, **backfill)
        if updated is not None:
            contacts_by_email[email] = updated


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


def _extract_recipients(headers: dict[str, str]) -> dict[str, list[str]]:
    """Extract recipient addresses from email headers.

    Parses To, Cc, and Bcc headers into a dict of lowercase addresses
    grouped by type. Empty lists are omitted from the result.

    Args:
        headers: Lowercase-keyed header dict from ``get_message_headers()``.

    Returns:
        Dict like ``{"to": ["a@x.com"], "cc": ["b@x.com"]}``.
    """
    result: dict[str, list[str]] = {}
    for key in ("to", "cc", "bcc"):
        raw = headers.get(key, "")
        if not raw:
            continue
        addresses = [addr.lower() for _name, addr in getaddresses([raw]) if addr]
        if addresses:
            result[key] = addresses
    return result


def _store_inbound_message(  # noqa: PLR0913
    connection: psycopg.Connection[dict[str, Any]],
    account: Account,
    message: dict[str, Any],
    contacts_by_email: dict[str, Contact],
    settings: Settings,
    *,
    has_active_workflows: bool,
    earliest_workflow_at: datetime | None = None,
) -> Email | None:
    """Persist a Gmail message as an inbound email and route when fresh.

    Returns None when a concurrent sync_account call for the same account
    already stored the row (ON CONFLICT DO NOTHING in create_email).
    """
    headers = get_message_headers(message)
    sender_email, first_name, last_name = parse_sender(headers.get("from", ""))
    contact = contacts_by_email.get(sender_email)
    if contact is None:
        # Fallback for senders not in the pre-fetched dict (e.g. empty From
        # header): resolve one-off so a single malformed message does not
        # abort the whole sync.
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
    inbound_recipients = _extract_recipients(headers)
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
        rfc2822_message_id=headers.get("message-id"),
        in_reply_to=headers.get("in-reply-to"),
        references_header=headers.get("references"),
        sender=sender_email.lower(),
        recipients=inbound_recipients,
    )
    if email is None:
        return None
    sync_messages_stored.add(
        1,
        attributes={"within_recency_window": within_window},
    )
    # Skip LLM classification for emails older than the earliest active
    # inbound workflow -- they can never produce tasks and classifying
    # them wastes tokens (#65).
    predates_workflows = (
        earliest_workflow_at is not None
        and received_at is not None
        and received_at < earliest_workflow_at
    )
    if within_window and has_active_workflows and not predates_workflows:
        email = route_email(
            connection, email, sender_email=sender_email, settings=settings
        )
    elif within_window:
        # Within recency window but no LLM classification will run. Record
        # the skip with a reason so observability stays complete -- every
        # inbound delivery produces either a routing.route_email span or a
        # routing.route_email_skipped span (D5).
        skip_reason = (
            "predates_workflows" if predates_workflows else "no_active_workflows"
        )
        _emit_route_skipped_span(email, skip_reason)
        updated = update_email(connection, email.id, is_routed=True)
        if updated is not None:
            email = updated
    else:
        # Outside the 7-day recency window -- create_email already set
        # is_routed=True, so emit the skip span and return.
        _emit_route_skipped_span(email, "outside_recency_window")
    return email


def _emit_route_skipped_span(email: Email, reason: str) -> None:
    """Emit a ``routing.route_email_skipped`` span for an inbound email.

    Mirrors the attribute shape of ``routing.route_email`` so dashboards
    that filter on ``email_id`` / ``account_id`` keep working when an
    inbound delivery skips the routing pipeline (D5).
    """
    with logfire.span(
        "routing.route_email_skipped",
        email_id=email.id,
        account_id=email.account_id,
        reason=reason,
    ):
        pass


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


# -- Send email ----------------------------------------------------------------


def send_email(  # noqa: PLR0913
    connection: psycopg.Connection[dict[str, Any]],
    account: Account,
    gmail_client: GmailClient,
    settings: Settings,
    to: str,
    subject: str,
    body: str,
    contact_id: str | None = None,
    workflow_id: str | None = None,
    thread_id: str | None = None,
    cc: str | None = None,
    bcc: str | None = None,
    in_reply_to: str | None = None,
) -> Email:
    """Send an outbound email through Gmail and record the DB row.

    Hands the message to ``GmailClient.send_message`` first and only
    persists the row after Gmail accepts it, so a Gmail failure never
    leaves an orphan ``sent`` row behind. Outbound rows are marked
    ``is_routed=True`` because they originate from an agent/CLI and need
    no further routing.

    Args:
        connection: Open database connection.
        account: Sending account (used for service-account delegation).
        gmail_client: Gmail client scoped to ``account``.
        settings: Application settings (reserved for future tuning).
        to: Recipient email address(es), comma-separated for multiple.
        subject: Email subject.
        body: Plain text body.
        contact_id: Optional contact FK.
        workflow_id: Optional workflow FK.
        thread_id: Optional Gmail thread ID for replies.
        cc: Optional CC recipient(s), comma-separated.
        bcc: Optional BCC recipient(s), comma-separated.
        in_reply_to: RFC 2822 Message-ID of the email being replied to.
            Sets In-Reply-To and References MIME headers for cross-client
            thread grouping.

    Returns:
        The created ``Email`` row with ``direction="outbound"`` and
        ``status="sent"``.

    Raises:
        RuntimeError: If the DB insert unexpectedly returns None (would
            only happen on a duplicate Gmail message ID, which the API
            does not reuse for fresh sends).
    """
    del settings  # reserved for future tuning (per-account overrides, etc.)
    with logfire.span(
        "sync.send_email",
        account_id=account.id,
        workflow_id=workflow_id,
        contact_id=contact_id,
    ) as span:
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        from mailpilot.database import get_workflow
        from mailpilot.email_renderer import get_theme, render_email_html

        from_header = (
            formataddr((account.display_name, account.email))
            if account.display_name
            else account.email
        )

        # Look up theme from workflow
        theme_name: str | None = None
        if workflow_id is not None:
            workflow = get_workflow(connection, workflow_id)
            if workflow is not None:
                theme_name = workflow.theme
        theme = get_theme(theme_name)

        # The agent's Markdown source is the text/plain part verbatim.
        # Markdown is designed to be readable as plain text; the previous
        # ``strip_markdown`` reduction stripped table borders and bold
        # markers, leaving recipients on text/plain-only clients with
        # tab-soup tables.
        html_body = render_email_html(body, theme)
        plain_body = body

        # Build multipart/alternative MIME
        mime_message = MIMEMultipart("alternative")
        mime_message.attach(MIMEText(plain_body, "plain", "utf-8"))
        mime_message.attach(MIMEText(html_body, "html", "utf-8"))

        result = gmail_client.send_message(
            message=mime_message,
            to=to,
            subject=subject,
            from_email=from_header,
            thread_id=thread_id,
            account_id=account.id,
            cc=cc,
            bcc=bcc,
            in_reply_to=in_reply_to,
        )
        gmail_message_id = result.get("id")
        gmail_thread_id = result.get("threadId")
        labels = list(result.get("labelIds") or [])
        outbound_recipients: dict[str, list[str]] = {
            "to": [a.strip().lower() for a in to.split(",") if a.strip()],
        }
        if cc:
            outbound_recipients["cc"] = [
                a.strip().lower() for a in cc.split(",") if a.strip()
            ]
        if bcc:
            outbound_recipients["bcc"] = [
                a.strip().lower() for a in bcc.split(",") if a.strip()
            ]
        email = create_email(
            connection,
            account_id=account.id,
            direction="outbound",
            subject=subject,
            body_text=plain_body,
            gmail_message_id=gmail_message_id,
            gmail_thread_id=gmail_thread_id,
            contact_id=contact_id,
            workflow_id=workflow_id,
            status="sent",
            is_routed=True,
            sent_at=datetime.now(UTC),
            labels=labels,
            sender=account.email.lower(),
            recipients=outbound_recipients,
        )
        if email is None:
            # Gmail accepted the send but the DB insert returned None (would
            # only happen on a duplicate gmail_message_id, which Gmail should
            # never reuse). The message has been delivered; log loudly so the
            # orphan is recoverable from traces even though the span
            # attributes below will not be set.
            logfire.error(
                "sync.send_email.orphan_gmail_send",
                account_id=account.id,
                gmail_message_id=gmail_message_id,
                gmail_thread_id=gmail_thread_id,
                to=to,
                workflow_id=workflow_id,
                contact_id=contact_id,
            )
            raise RuntimeError(
                "outbound email insert returned None for "
                f"gmail_message_id={gmail_message_id}"
            )
        span.set_attribute("email_id", email.id)
        span.set_attribute("gmail_message_id", gmail_message_id)
        return email
