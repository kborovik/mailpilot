"""Tests for Pub/Sub infrastructure and Gmail watch management."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg

from conftest import make_test_account, make_test_settings
from mailpilot.database import get_account, update_account

# -- setup_pubsub --------------------------------------------------------------


def test_setup_pubsub_creates_topic_and_subscription() -> None:
    from mailpilot.pubsub import setup_pubsub

    settings = make_test_settings(
        google_application_credentials="/tmp/creds.json",
        google_pubsub_topic="test-topic",
        google_pubsub_subscription="test-sub",
    )
    with (
        patch("mailpilot.pubsub._resolve_project_id", return_value="my-project"),
        patch("mailpilot.pubsub._load_credentials", return_value=MagicMock()),
        patch("mailpilot.pubsub.PublisherClient") as mock_pub_cls,
        patch("mailpilot.pubsub.SubscriberClient") as mock_sub_cls,
    ):
        mock_publisher = mock_pub_cls.return_value
        mock_subscriber = mock_sub_cls.return_value

        setup_pubsub(settings)

        mock_publisher.create_topic.assert_called_once_with(
            name="projects/my-project/topics/test-topic"
        )
        mock_subscriber.create_subscription.assert_called_once()
        call_kwargs = mock_subscriber.create_subscription.call_args
        assert call_kwargs[1]["name"] == "projects/my-project/subscriptions/test-sub"
        assert call_kwargs[1]["topic"] == "projects/my-project/topics/test-topic"


def test_setup_pubsub_passes_service_account_credentials() -> None:
    """Pub/Sub clients must use the configured service account, not ADC.

    Regression: instantiating PublisherClient()/SubscriberClient() with
    no credentials silently falls back to gcloud user ADC, which on a
    developer machine can be expired and traps the sync loop in a
    600-second gRPC retry before failing.
    """
    from mailpilot.pubsub import setup_pubsub

    settings = make_test_settings(
        google_application_credentials="/tmp/creds.json",
    )
    sentinel = MagicMock(name="service_account_credentials")
    with (
        patch("mailpilot.pubsub._resolve_project_id", return_value="my-project"),
        patch("mailpilot.pubsub._load_credentials", return_value=sentinel) as mock_load,
        patch("mailpilot.pubsub.PublisherClient") as mock_pub_cls,
        patch("mailpilot.pubsub.SubscriberClient") as mock_sub_cls,
    ):
        setup_pubsub(settings)

    mock_load.assert_called_once_with(settings)
    mock_pub_cls.assert_called_once_with(credentials=sentinel)
    mock_sub_cls.assert_called_once_with(credentials=sentinel)


def test_start_subscriber_passes_service_account_credentials() -> None:
    """start_subscriber must use the configured service account, not ADC."""
    from mailpilot.pubsub import start_subscriber

    settings = make_test_settings(
        google_application_credentials="/tmp/creds.json",
    )
    sentinel = MagicMock(name="service_account_credentials")
    with (
        patch("mailpilot.pubsub._resolve_project_id", return_value="my-project"),
        patch("mailpilot.pubsub._load_credentials", return_value=sentinel),
        patch("mailpilot.pubsub.SubscriberClient") as mock_sub_cls,
    ):
        start_subscriber(settings, MagicMock())

    mock_sub_cls.assert_called_once_with(credentials=sentinel)


def test_setup_pubsub_idempotent_when_already_exists() -> None:
    from google.api_core.exceptions import AlreadyExists

    from mailpilot.pubsub import setup_pubsub

    settings = make_test_settings(
        google_application_credentials="/tmp/creds.json",
    )
    with (
        patch("mailpilot.pubsub._resolve_project_id", return_value="my-project"),
        patch("mailpilot.pubsub._load_credentials", return_value=MagicMock()),
        patch("mailpilot.pubsub.PublisherClient") as mock_pub_cls,
        patch("mailpilot.pubsub.SubscriberClient") as mock_sub_cls,
    ):
        mock_pub_cls.return_value.create_topic.side_effect = AlreadyExists("exists")
        mock_sub_cls.return_value.create_subscription.side_effect = AlreadyExists(
            "exists"
        )
        # Should not raise
        setup_pubsub(settings)


def test_setup_pubsub_sets_iam_policy() -> None:
    from mailpilot.pubsub import setup_pubsub

    settings = make_test_settings(
        google_application_credentials="/tmp/creds.json",
    )
    with (
        patch("mailpilot.pubsub._resolve_project_id", return_value="my-project"),
        patch("mailpilot.pubsub._load_credentials", return_value=MagicMock()),
        patch("mailpilot.pubsub.PublisherClient") as mock_pub_cls,
        patch("mailpilot.pubsub.SubscriberClient"),
    ):
        mock_publisher = mock_pub_cls.return_value
        mock_publisher.get_iam_policy.return_value = MagicMock(bindings=[])

        setup_pubsub(settings)

        mock_publisher.set_iam_policy.assert_called_once()


# -- start_subscriber ----------------------------------------------------------


def test_start_subscriber_returns_future() -> None:
    from mailpilot.pubsub import start_subscriber

    settings = make_test_settings(
        google_application_credentials="/tmp/creds.json",
    )
    callback = MagicMock()
    with (
        patch("mailpilot.pubsub._resolve_project_id", return_value="my-project"),
        patch("mailpilot.pubsub._load_credentials", return_value=MagicMock()),
        patch("mailpilot.pubsub.SubscriberClient") as mock_sub_cls,
    ):
        mock_future = MagicMock()
        mock_sub_cls.return_value.subscribe.return_value = mock_future

        result = start_subscriber(settings, callback)

        assert result is mock_future
        mock_sub_cls.return_value.subscribe.assert_called_once()


# -- make_notification_callback ------------------------------------------------


def test_notification_callback_decodes_and_enqueues() -> None:
    import queue

    from mailpilot.pubsub import make_notification_callback

    sync_queue: queue.Queue[str] = queue.Queue()
    callback = make_notification_callback(sync_queue)

    message = MagicMock()
    message.data = base64.urlsafe_b64encode(
        json.dumps({"emailAddress": "user@example.com", "historyId": "12345"}).encode()
    )

    callback(message)

    assert sync_queue.get_nowait() == "user@example.com"
    message.ack.assert_called_once()


def test_notification_callback_sets_wakeup_event() -> None:
    """Pub/Sub notifications must wake the main loop, not just enqueue.

    Without setting wakeup_event, real-time delivery degenerates to plain
    run_interval polling -- the notification sits in the queue until the
    next periodic timer fires.
    """
    import queue
    import threading

    from mailpilot.pubsub import make_notification_callback

    sync_queue: queue.Queue[str] = queue.Queue()
    wakeup_event = threading.Event()
    callback = make_notification_callback(sync_queue, wakeup_event)

    message = MagicMock()
    message.data = base64.urlsafe_b64encode(
        json.dumps({"emailAddress": "user@example.com"}).encode()
    )

    callback(message)

    assert wakeup_event.is_set()
    assert sync_queue.get_nowait() == "user@example.com"
    message.ack.assert_called_once()


def test_notification_callback_acks_on_decode_error() -> None:
    import queue

    from mailpilot.pubsub import make_notification_callback

    sync_queue: queue.Queue[str] = queue.Queue()
    callback = make_notification_callback(sync_queue)

    message = MagicMock()
    message.data = b"not-valid-base64-json!!!"

    callback(message)

    assert sync_queue.empty()
    message.ack.assert_called_once()


def test_notification_callback_acks_on_missing_email_field() -> None:
    import queue

    from mailpilot.pubsub import make_notification_callback

    sync_queue: queue.Queue[str] = queue.Queue()
    callback = make_notification_callback(sync_queue)

    message = MagicMock()
    message.data = base64.urlsafe_b64encode(json.dumps({"historyId": "12345"}).encode())

    callback(message)

    assert sync_queue.empty()
    message.ack.assert_called_once()


# -- renew_watches -------------------------------------------------------------


def test_renew_watches_renews_expiring(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.pubsub import renew_watches

    settings = make_test_settings(
        google_application_credentials="/tmp/creds.json",
        google_pubsub_topic="test-topic",
    )
    # Account with watch expiring in 12 hours (within 24h threshold)
    account = make_test_account(database_connection, email="expiring@example.com")
    soon = datetime.now(UTC) + timedelta(hours=12)
    update_account(database_connection, account.id, watch_expiration=soon)

    new_expiration_ms = str(
        int((datetime.now(UTC) + timedelta(days=7)).timestamp() * 1000)
    )
    with (
        patch("mailpilot.pubsub._resolve_project_id", return_value="my-project"),
        patch("mailpilot.gmail.GmailClient") as mock_gmail_cls,
    ):
        mock_client = mock_gmail_cls.return_value
        mock_client.watch.return_value = {
            "historyId": "99999",
            "expiration": new_expiration_ms,
        }

        count = renew_watches(database_connection, settings)

    assert count == 1
    updated = get_account(database_connection, account.id)
    assert updated is not None
    assert updated.watch_expiration is not None
    assert updated.watch_expiration > soon


def test_renew_watches_skips_fresh_watches(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.pubsub import renew_watches

    settings = make_test_settings(
        google_application_credentials="/tmp/creds.json",
        google_pubsub_topic="test-topic",
    )
    # Account with watch expiring in 3 days (outside 24h threshold)
    account = make_test_account(database_connection, email="fresh@example.com")
    far_future = datetime.now(UTC) + timedelta(days=3)
    update_account(database_connection, account.id, watch_expiration=far_future)

    with (
        patch("mailpilot.pubsub._resolve_project_id", return_value="my-project"),
        patch("mailpilot.gmail.GmailClient") as mock_gmail_cls,
    ):
        count = renew_watches(database_connection, settings)

    assert count == 0
    mock_gmail_cls.assert_not_called()


def test_renew_watches_renews_null_expiration(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.pubsub import renew_watches

    settings = make_test_settings(
        google_application_credentials="/tmp/creds.json",
        google_pubsub_topic="test-topic",
    )
    # Account with no watch set (watch_expiration is NULL)
    make_test_account(database_connection, email="new@example.com")

    new_expiration_ms = str(
        int((datetime.now(UTC) + timedelta(days=7)).timestamp() * 1000)
    )
    with (
        patch("mailpilot.pubsub._resolve_project_id", return_value="my-project"),
        patch("mailpilot.gmail.GmailClient") as mock_gmail_cls,
    ):
        mock_client = mock_gmail_cls.return_value
        mock_client.watch.return_value = {
            "historyId": "99999",
            "expiration": new_expiration_ms,
        }

        count = renew_watches(database_connection, settings)

    assert count == 1
