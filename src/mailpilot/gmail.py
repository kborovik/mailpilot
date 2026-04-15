"""Gmail API client using service account with domain-wide delegation.

Authentication: service account credentials resolved via
``GOOGLE_APPLICATION_CREDENTIALS`` env var, falling back to Application
Default Credentials (ADC). Per-account impersonation via ``.with_subject()``.

Scope: ``https://www.googleapis.com/auth/gmail.modify``
"""

from __future__ import annotations

import base64
import time
from email.mime.text import MIMEText
from functools import wraps
from typing import Any

import logfire

_GMAIL_SCOPE = ["https://www.googleapis.com/auth/gmail.modify"]

_TRANSIENT_STATUS_CODES = frozenset({429, 500, 502, 503, 504, 529})
_MAX_RETRIES = 5
_MAX_BACKOFF = 30.0

# Custom headers added to all outgoing emails.
_MAILPILOT_VERSION = "0.1.0"


def _retry_on_transient(func: Any) -> Any:
    """Retry decorator with exponential backoff for transient Gmail API errors."""

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        from googleapiclient.errors import HttpError

        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except HttpError as exc:
                if exc.resp.status not in _TRANSIENT_STATUS_CODES:
                    raise
                last_error = exc
                backoff = min(2**attempt, _MAX_BACKOFF)
                logfire.warn(
                    "gmail api transient error, retrying",
                    status=exc.resp.status,
                    attempt=attempt + 1,
                    backoff=backoff,
                )
                time.sleep(backoff)
        raise last_error  # type: ignore[misc]

    return wrapper


def build_gmail_service(email: str) -> Any:
    """Build a Gmail API service instance with delegated credentials.

    Uses service account credentials to impersonate the given email address.
    Credentials are resolved from ``GOOGLE_APPLICATION_CREDENTIALS`` env var
    or Application Default Credentials (ADC).

    Args:
        email: Gmail address to impersonate via domain-wide delegation.

    Returns:
        Gmail API service resource.
    """
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    credentials = Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
        _resolve_credentials_path(),
        scopes=_GMAIL_SCOPE,
    )
    delegated = credentials.with_subject(email)
    return build("gmail", "v1", credentials=delegated)


def _resolve_credentials_path() -> str:
    """Resolve the service account credentials file path.

    Returns:
        Path to the service account JSON file.

    Raises:
        SystemExit: If no credentials file is configured.
    """
    import os

    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not path:
        raise SystemExit(
            "GOOGLE_APPLICATION_CREDENTIALS env var not set -- "
            "point it to a service account JSON file"
        )
    return path


@_retry_on_transient
def get_profile(service: Any, user_id: str = "me") -> dict[str, Any]:
    """Fetch Gmail user profile.

    Args:
        service: Gmail API service instance.
        user_id: Gmail user ID (default "me" for delegated user).

    Returns:
        Profile dict with emailAddress, messagesTotal, etc.
    """
    result: dict[str, Any] = service.users().getProfile(userId=user_id).execute()
    return result


@_retry_on_transient
def list_messages(
    service: Any,
    query: str = "",
    max_results: int = 100,
    user_id: str = "me",
) -> list[dict[str, Any]]:
    """List messages matching a Gmail search query.

    Args:
        service: Gmail API service instance.
        query: Gmail search query (e.g. "is:unread in:inbox").
        max_results: Maximum number of messages to return.
        user_id: Gmail user ID.

    Returns:
        List of message stubs with id and threadId.
    """
    response: dict[str, Any] = (
        service.users()
        .messages()
        .list(userId=user_id, q=query, maxResults=max_results)
        .execute()
    )
    messages: list[dict[str, Any]] = response.get("messages", [])
    return messages


@_retry_on_transient
def get_message(
    service: Any,
    message_id: str,
    user_id: str = "me",
    format_: str = "full",
) -> dict[str, Any] | None:
    """Fetch a single message by ID.

    Args:
        service: Gmail API service instance.
        message_id: Gmail message ID.
        user_id: Gmail user ID.
        format_: Message format (full, metadata, minimal, raw).

    Returns:
        Full message dict, or None if message was deleted.
    """
    from googleapiclient.errors import HttpError

    try:
        result: dict[str, Any] = (
            service.users()
            .messages()
            .get(userId=user_id, id=message_id, format=format_)
            .execute()
        )
        return result
    except HttpError as exc:
        if exc.resp.status == 404:
            logfire.debug("gmail message not found (deleted)", message_id=message_id)
            return None
        raise


@_retry_on_transient
def send_message(  # noqa: PLR0913
    service: Any,
    to: str,
    subject: str,
    body: str,
    from_email: str = "",
    thread_id: str | None = None,
    account_id: str = "",
    user_id: str = "me",
) -> dict[str, Any]:
    """Send an email message via Gmail API.

    Args:
        service: Gmail API service instance.
        to: Recipient email address.
        subject: Email subject.
        body: Email body (plain text).
        from_email: Sender email (for From header).
        thread_id: Gmail thread ID for threading replies.
        account_id: MailPilot account ID for traceability header.
        user_id: Gmail user ID.

    Returns:
        Sent message dict with id, threadId, labelIds.
    """
    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    if from_email:
        message["from"] = from_email
    message["X-MailPilot-Version"] = _MAILPILOT_VERSION
    if account_id:
        message["X-MailPilot-Account-Id"] = account_id

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    send_body: dict[str, Any] = {"raw": raw}
    if thread_id:
        send_body["threadId"] = thread_id

    result: dict[str, Any] = (
        service.users().messages().send(userId=user_id, body=send_body).execute()
    )
    logfire.info("gmail message sent", message_id=result.get("id"), to=to)
    return result


@_retry_on_transient
def modify_message(
    service: Any,
    message_id: str,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    user_id: str = "me",
) -> dict[str, Any]:
    """Modify labels on a message.

    Args:
        service: Gmail API service instance.
        message_id: Gmail message ID.
        add_labels: Label IDs to add.
        remove_labels: Label IDs to remove.
        user_id: Gmail user ID.

    Returns:
        Modified message dict.
    """
    body: dict[str, list[str]] = {}
    if add_labels:
        body["addLabelIds"] = add_labels
    if remove_labels:
        body["removeLabelIds"] = remove_labels

    result: dict[str, Any] = (
        service.users()
        .messages()
        .modify(userId=user_id, id=message_id, body=body)
        .execute()
    )
    return result


@_retry_on_transient
def get_history(
    service: Any,
    start_history_id: str,
    user_id: str = "me",
) -> list[dict[str, Any]]:
    """Fetch mailbox changes since a history ID.

    Args:
        service: Gmail API service instance.
        start_history_id: History ID to start from.
        user_id: Gmail user ID.

    Returns:
        List of history records.
    """
    response: dict[str, Any] = (
        service.users()
        .history()
        .list(userId=user_id, startHistoryId=start_history_id)
        .execute()
    )
    history: list[dict[str, Any]] = response.get("history", [])
    return history


@_retry_on_transient
def watch(
    service: Any,
    topic_name: str,
    user_id: str = "me",
) -> dict[str, Any]:
    """Set up Gmail push notifications via Pub/Sub.

    Args:
        service: Gmail API service instance.
        topic_name: Full Pub/Sub topic name (projects/{project}/topics/{topic}).
        user_id: Gmail user ID.

    Returns:
        Watch response with historyId and expiration.
    """
    body = {
        "topicName": topic_name,
        "labelIds": ["INBOX"],
    }
    result: dict[str, Any] = service.users().watch(userId=user_id, body=body).execute()
    logfire.info(
        "gmail watch registered", topic=topic_name, expiration=result.get("expiration")
    )
    return result


def create_label_if_not_exists(
    service: Any,
    label_name: str,
    user_id: str = "me",
) -> str:
    """Create a Gmail label or return existing label ID.

    Args:
        service: Gmail API service instance.
        label_name: Label name to create.
        user_id: Gmail user ID.

    Returns:
        Label ID.
    """
    from googleapiclient.errors import HttpError

    # Check existing labels.
    response = service.users().labels().list(userId=user_id).execute()
    for label in response.get("labels", []):
        if label.get("name") == label_name:
            label_id: str = label["id"]
            return label_id

    # Create new label.
    try:
        result = (
            service.users()
            .labels()
            .create(
                userId=user_id,
                body={
                    "name": label_name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
        created_id: str = result["id"]
        logfire.info("gmail label created", name=label_name, id=created_id)
        return created_id
    except HttpError as exc:
        logfire.warn("gmail label creation failed", name=label_name, error=str(exc))
        raise


def extract_text_from_message(message: dict[str, Any]) -> str:
    """Extract plain text from a Gmail message payload.

    Walks MIME parts recursively. Uses text/plain parts only.
    Returns empty string if no text/plain part found (per ADR-02).

    Args:
        message: Full Gmail message dict (format="full").

    Returns:
        Extracted plain text body.
    """
    payload = message.get("payload", {})
    return _extract_text_from_part(payload)


def _extract_text_from_part(part: dict[str, Any]) -> str:
    """Recursively extract text from a MIME part."""
    mime_type = part.get("mimeType", "")
    body = part.get("body", {})
    parts = part.get("parts", [])

    if mime_type == "text/plain":
        data = body.get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    if mime_type.startswith("multipart/"):
        # Prefer text/plain in multipart/alternative.
        plain_parts = [p for p in parts if p.get("mimeType") == "text/plain"]
        if plain_parts:
            return _extract_text_from_part(plain_parts[0])
        # Fall back to any part with content.
        for sub_part in parts:
            text = _extract_text_from_part(sub_part)
            if text.strip():
                return text

    return ""


def get_message_headers(
    message: dict[str, Any],
) -> dict[str, str]:
    """Extract headers from a Gmail message as a dict.

    Args:
        message: Full Gmail message dict.

    Returns:
        Dict mapping lowercase header names to values.
    """
    payload = message.get("payload", {})
    headers: dict[str, str] = {}
    for header in payload.get("headers", []):
        name = header.get("name", "").lower()
        value = header.get("value", "")
        headers[name] = value
    return headers
