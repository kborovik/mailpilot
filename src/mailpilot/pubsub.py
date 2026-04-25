"""Google Cloud Pub/Sub for Gmail push notifications.

Provides idempotent infrastructure setup, a streaming pull subscriber
that enqueues account emails for sync, and periodic watch renewal.
"""

from __future__ import annotations

import base64
import json
import queue
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import logfire
from google.cloud.pubsub_v1 import PublisherClient, SubscriberClient

from mailpilot.settings import Settings

StreamingPullFuture = Any

_WATCH_RENEWAL_THRESHOLD = timedelta(hours=24)


_PUBSUB_SCOPES = ["https://www.googleapis.com/auth/pubsub"]


def _resolve_project_id(settings: Settings) -> str:
    """Read the GCP project ID from the service account JSON file.

    Args:
        settings: Application settings with google_application_credentials.

    Returns:
        Project ID string.

    Raises:
        SystemExit: If credentials path is missing or file lacks project_id.
    """
    path = settings.google_application_credentials
    if not path:
        raise SystemExit(
            "google_application_credentials not configured -- set via "
            "'mailpilot config set google_application_credentials "
            "/path/to/key.json'"
        )
    with open(path) as f:
        data: dict[str, Any] = json.load(f)
    project_id = data.get("project_id")
    if not project_id:
        raise SystemExit(f"No project_id found in {path}")
    return project_id


def _load_credentials(settings: Settings) -> Any:
    """Load service account credentials scoped for Pub/Sub.

    The Pub/Sub clients otherwise fall back to Application Default
    Credentials (gcloud user login), which on a developer machine can
    be expired and produces a 600-second retry loop on the first RPC
    before failing. Loading the configured service account file
    directly mirrors how ``GmailClient`` authenticates and avoids that
    trap entirely.
    """
    from google.oauth2.service_account import Credentials

    path = settings.google_application_credentials
    if not path:
        raise SystemExit(
            "google_application_credentials not configured -- set via "
            "'mailpilot config set google_application_credentials "
            "/path/to/key.json'"
        )
    return Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
        path, scopes=_PUBSUB_SCOPES
    )


def _topic_path(project_id: str, settings: Settings) -> str:
    return f"projects/{project_id}/topics/{settings.google_pubsub_topic}"


def _subscription_path(project_id: str, settings: Settings) -> str:
    return f"projects/{project_id}/subscriptions/{settings.google_pubsub_subscription}"


def setup_pubsub(settings: Settings) -> None:
    """Create Pub/Sub topic, subscription, and IAM policy (idempotent).

    Sets up the shared infrastructure that all Gmail accounts publish to.
    Safe to call repeatedly -- catches AlreadyExists for both topic and
    subscription.

    Args:
        settings: Application settings.
    """
    from google.api_core.exceptions import AlreadyExists
    from google.iam.v1 import policy_pb2

    project_id = _resolve_project_id(settings)
    topic = _topic_path(project_id, settings)
    subscription = _subscription_path(project_id, settings)
    credentials = _load_credentials(settings)

    publisher = PublisherClient(credentials=credentials)
    subscriber = SubscriberClient(credentials=credentials)

    # Per-RPC spans rather than a single wrapper around the whole setup.
    # Logfire only flushes a span when it ends, so wrapping multiple slow
    # RPCs together hides which one is hanging until the whole block
    # finishes. Each RPC closes on its own and gets its own attributes.
    with logfire.span("pubsub.create_topic", topic=topic):
        try:
            publisher.create_topic(name=topic)
            logfire.info("pubsub.topic.created", topic=topic)
        except AlreadyExists:
            logfire.debug("pubsub.topic.exists", topic=topic)

    with logfire.span("pubsub.set_iam_policy", topic=topic):
        policy = publisher.get_iam_policy(request={"resource": topic})
        gmail_publisher = "serviceAccount:gmail-api-push@system.gserviceaccount.com"
        already_bound = any(
            b.role == "roles/pubsub.publisher" and gmail_publisher in b.members
            for b in policy.bindings
        )
        if already_bound:
            logfire.debug("pubsub.iam.already_set", topic=topic)
        else:
            policy.bindings.append(
                policy_pb2.Binding(
                    role="roles/pubsub.publisher",
                    members=[gmail_publisher],
                )
            )
            publisher.set_iam_policy(request={"resource": topic, "policy": policy})
            logfire.info("pubsub.iam.updated", topic=topic)

    with logfire.span("pubsub.create_subscription", subscription=subscription):
        try:
            subscriber.create_subscription(
                name=subscription,
                topic=topic,
                ack_deadline_seconds=60,
            )
            logfire.info("pubsub.subscription.created", subscription=subscription)
        except AlreadyExists:
            logfire.debug("pubsub.subscription.exists", subscription=subscription)


def start_subscriber(
    settings: Settings,
    callback: Any,
) -> StreamingPullFuture:
    """Start a streaming pull subscriber.

    Args:
        settings: Application settings.
        callback: Callback invoked for each Pub/Sub message.

    Returns:
        StreamingPullFuture that blocks until cancelled.
    """
    project_id = _resolve_project_id(settings)
    subscription = _subscription_path(project_id, settings)

    subscriber = SubscriberClient(credentials=_load_credentials(settings))
    future = subscriber.subscribe(subscription, callback=callback)
    logfire.info("pubsub.subscriber.started", subscription=subscription)
    return future


def make_notification_callback(
    sync_queue: queue.Queue[str],
    wakeup_event: threading.Event | None = None,
) -> Any:
    """Create a Pub/Sub message callback that enqueues account emails.

    The callback decodes the Gmail notification, extracts the emailAddress,
    puts it on the sync queue, and (when ``wakeup_event`` is supplied)
    signals the main loop so it drains the queue immediately instead of
    waiting for the periodic timer. Always acks the message -- nacking
    malformed messages causes infinite redelivery.

    Args:
        sync_queue: Queue to put account email addresses onto.
        wakeup_event: Event the main loop blocks on between iterations.
            Setting it short-circuits the periodic timer so Pub/Sub
            notifications drive sync in real time.

    Returns:
        Callback function compatible with SubscriberClient.subscribe().
    """

    def callback(message: Any) -> None:
        with logfire.span("pubsub.notification"):
            try:
                data = json.loads(base64.urlsafe_b64decode(message.data))
                email_address = data["emailAddress"]
            except json.JSONDecodeError, KeyError, Exception:
                logfire.warn("pubsub.notification.decode_error")
                message.ack()
                return
            logfire.debug(
                "pubsub.notification.received",
                email=email_address,
            )
            sync_queue.put(email_address)
            if wakeup_event is not None:
                wakeup_event.set()
            message.ack()

    return callback


def renew_watches(
    connection: Any,
    settings: Settings,
) -> int:
    """Renew Gmail watches for accounts with expiring or missing watches.

    Checks all accounts and renews watches that expire within 24 hours
    or have no watch set. Updates watch_expiration on the account row.

    Args:
        connection: Open database connection.
        settings: Application settings.

    Returns:
        Number of watches renewed.
    """
    from mailpilot.database import list_accounts, update_account
    from mailpilot.gmail import GmailClient

    project_id = _resolve_project_id(settings)
    topic = _topic_path(project_id, settings)
    threshold = datetime.now(UTC) + _WATCH_RENEWAL_THRESHOLD

    accounts = list_accounts(connection)
    renewed = 0

    # Per-account spans rather than a single wrapper. A hung Gmail
    # watch call would otherwise block the wrapper span from flushing
    # and hide which account is the offender.
    for account in accounts:
        if (
            account.watch_expiration is not None
            and account.watch_expiration > threshold
        ):
            continue

        with logfire.span(
            "pubsub.watch_account",
            account_id=account.id,
            email=account.email,
        ):
            try:
                client = GmailClient(account.email)
                result = client.watch(topic)
                expiration_ms = int(result.get("expiration", "0"))
                new_expiration = datetime.fromtimestamp(expiration_ms / 1000, tz=UTC)
                history_id = result.get("historyId")
                update_account(
                    connection,
                    account.id,
                    watch_expiration=new_expiration,
                    **({"gmail_history_id": history_id} if history_id else {}),
                )
                renewed += 1
                logfire.info(
                    "pubsub.watch.renewed",
                    account_id=account.id,
                    email=account.email,
                    expiration=new_expiration.isoformat(),
                )
            except Exception:
                logfire.exception(
                    "pubsub.watch.renewal_failed",
                    account_id=account.id,
                    email=account.email,
                )

    return renewed
