"""Gmail API client using service account with domain-wide delegation.

Authentication: service account credentials path resolved from the
``google_application_credentials`` setting, falling back to the
``GOOGLE_APPLICATION_CREDENTIALS`` env var. Per-account impersonation via
``.with_subject()``.

Scope: ``https://www.googleapis.com/auth/gmail.modify``
"""

from __future__ import annotations

import base64
import time
from email.mime.base import MIMEBase
from email.utils import parseaddr
from functools import wraps
from importlib.metadata import version
from typing import Any

import logfire

GmailService = Any
"""Type alias for the Gmail API service resource (untyped by Google)."""

_GMAIL_SCOPE = ["https://www.googleapis.com/auth/gmail.modify"]

_TRANSIENT_STATUS_CODES = frozenset({429, 500, 502, 503, 504, 529})
_MAX_RETRIES = 5
_MAX_BACKOFF = 30.0

# Custom headers added to all outgoing emails.
_MAILPILOT_VERSION = version("mailpilot")


def _retry_on_transient(func: Any) -> Any:
    """Retry decorator with exponential backoff for transient Gmail API errors.

    Wraps every invocation in a ``logfire.span("gmail.<method>")`` so that
    Gmail API latency is visible in traces. Transient retries are recorded
    as span events; the final ``attempts`` count and any error ``status``
    are set as span attributes.
    """

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        from googleapiclient.errors import HttpError

        # Resolve user_id from the GmailClient instance (first positional arg).
        user_id = getattr(args[0], "email", "") if args else ""
        span_name = f"gmail.{func.__name__}"

        with logfire.span(span_name, method=func.__name__, user_id=user_id) as span:
            last_error: Exception | None = None
            backoff = 0.0
            for attempt in range(_MAX_RETRIES):
                try:
                    result = func(*args, **kwargs)
                    span.set_attribute("attempts", attempt + 1)
                    return result
                except HttpError as exc:
                    if exc.resp.status not in _TRANSIENT_STATUS_CODES:
                        span.set_attribute("status", exc.resp.status)
                        span.set_attribute("attempts", attempt + 1)
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
            # All retries exhausted -- emit a dedicated error log for alerting.
            logfire.error(
                "gmail.retry.exhausted",
                method=func.__name__,
                status=last_error.resp.status,  # pyright: ignore[reportOptionalMemberAccess]
                attempts=_MAX_RETRIES,
                last_backoff=backoff,
            )
            span.set_attribute("attempts", _MAX_RETRIES)
            span.set_attribute(
                "status",
                last_error.resp.status,  # pyright: ignore[reportOptionalMemberAccess]
            )
            raise last_error  # type: ignore[misc]

    return wrapper


def _resolve_credentials_path() -> str:
    """Resolve the service account credentials file path.

    Reads ``google_application_credentials`` from settings first, then
    falls back to the ``GOOGLE_APPLICATION_CREDENTIALS`` env var.

    Returns:
        Path to the service account JSON file.

    Raises:
        SystemExit: If no credentials file is configured.
    """
    import os

    from mailpilot.settings import get_settings

    path = get_settings().google_application_credentials
    if not path:
        path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not path:
        raise SystemExit(
            "No service account credentials configured -- set via "
            "'mailpilot config set google_application_credentials "
            "/path/to/key.json' or the GOOGLE_APPLICATION_CREDENTIALS env var"
        )
    return path


def build_gmail_service(email: str) -> GmailService:
    """Build a Gmail API service instance with delegated credentials.

    Uses service account credentials to impersonate the given email address.
    Credentials are resolved from the ``google_application_credentials``
    setting or the ``GOOGLE_APPLICATION_CREDENTIALS`` env var.

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


# -- GmailClient --------------------------------------------------------------


class GmailClient:
    """Thin wrapper around Gmail API service for per-account operations.

    Holds the service instance so callers don't pass it to every function.
    Initialized with an email address; builds the delegated service internally.

    Usage::

        client = GmailClient("user@example.com")
        profile = client.get_profile()
        client.send_message(to="x@y.com", subject="Hi", body="Hello")
    """

    def __init__(self, email: str) -> None:
        self.email = email
        self._service: GmailService = build_gmail_service(email)

    @classmethod
    def from_service(cls, email: str, service: GmailService) -> GmailClient:
        """Create a client with a pre-built service (for testing).

        Args:
            email: Gmail address.
            service: Pre-built Gmail API service resource.

        Returns:
            GmailClient using the provided service.
        """
        client = cls.__new__(cls)
        client.email = email
        client._service = service
        return client

    @_retry_on_transient
    def get_profile(self, user_id: str = "me") -> dict[str, Any]:
        """Fetch Gmail user profile.

        Args:
            user_id: Gmail user ID (default "me" for delegated user).

        Returns:
            Profile dict with emailAddress, messagesTotal, etc.
        """
        result: dict[str, Any] = (
            self._service.users().getProfile(userId=user_id).execute()
        )
        return result

    @_retry_on_transient
    def list_messages(
        self,
        query: str = "",
        max_results: int = 100,
        label_ids: list[str] | None = None,
        user_id: str = "me",
    ) -> list[dict[str, Any]]:
        """List messages matching a Gmail search query.

        Args:
            query: Gmail search query (e.g. "is:unread in:inbox").
            max_results: Maximum number of messages to return.
            label_ids: Filter by label IDs (e.g., ["INBOX"]). AND logic.
            user_id: Gmail user ID.

        Returns:
            List of message stubs with id and threadId.
        """
        kwargs: dict[str, Any] = {
            "userId": user_id,
            "q": query,
            "maxResults": max_results,
        }
        if label_ids is not None:
            kwargs["labelIds"] = label_ids
        response: dict[str, Any] = (
            self._service.users().messages().list(**kwargs).execute()
        )
        messages: list[dict[str, Any]] = response.get("messages", [])
        return messages

    @_retry_on_transient
    def get_message(
        self,
        message_id: str,
        user_id: str = "me",
        format_: str = "full",
    ) -> dict[str, Any] | None:
        """Fetch a single message by ID.

        Args:
            message_id: Gmail message ID.
            user_id: Gmail user ID.
            format_: Message format (full, metadata, minimal, raw).

        Returns:
            Full message dict, or None if message was deleted.
        """
        from googleapiclient.errors import HttpError

        try:
            result: dict[str, Any] = (
                self._service.users()
                .messages()
                .get(userId=user_id, id=message_id, format=format_)
                .execute()
            )
            return result
        except HttpError as exc:
            if exc.resp.status == 404:
                logfire.debug(
                    "gmail message not found (deleted)",
                    message_id=message_id,
                )
                return None
            raise

    @_retry_on_transient
    def send_message(
        self,
        message: MIMEBase,
        to: str,
        subject: str,
        from_email: str = "",
        thread_id: str | None = None,
        account_id: str = "",
        cc: str | None = None,
        bcc: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
        user_id: str = "me",
    ) -> dict[str, Any]:
        """Send an email message via Gmail API.

        Args:
            message: Pre-built MIME message (e.g. multipart/alternative).
            to: Recipient email address(es), comma-separated for multiple.
            subject: Email subject.
            from_email: Sender email (for From header).
            thread_id: Gmail thread ID for threading replies.
            account_id: MailPilot account ID for traceability header.
            cc: CC recipient(s), comma-separated.
            bcc: BCC recipient(s), comma-separated.
            in_reply_to: RFC 2822 Message-ID of the email being replied to.
                Sets the In-Reply-To header for cross-client thread grouping.
            references: Space-separated RFC 2822 Message-ID chain of prior
                messages in the thread (RFC 5322 section 3.6.4). Falls back
                to ``in_reply_to`` when omitted, which is correct for replies
                to a single prior message.
            user_id: Gmail user ID.

        Returns:
            Sent message dict with id, threadId, labelIds.
        """
        message["To"] = to
        message["Subject"] = subject
        if from_email:
            message["From"] = from_email
        if cc:
            message["Cc"] = cc
        if bcc:
            message["Bcc"] = bcc
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
            message["References"] = references or in_reply_to
        message["X-MailPilot-Version"] = _MAILPILOT_VERSION
        if account_id:
            message["X-MailPilot-Account-Id"] = account_id

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        send_body: dict[str, Any] = {"raw": raw}
        if thread_id:
            send_body["threadId"] = thread_id

        result: dict[str, Any] = (
            self._service.users()
            .messages()
            .send(userId=user_id, body=send_body)
            .execute()
        )
        return result

    @_retry_on_transient
    def modify_message(
        self,
        message_id: str,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
        user_id: str = "me",
    ) -> dict[str, Any]:
        """Modify labels on a message.

        Args:
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
            self._service.users()
            .messages()
            .modify(userId=user_id, id=message_id, body=body)
            .execute()
        )
        return result

    @_retry_on_transient
    def get_history(
        self,
        start_history_id: str,
        user_id: str = "me",
        history_types: list[str] | None = None,
        label_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch mailbox changes since a history ID.

        Pages through all results automatically.

        Args:
            start_history_id: History ID to start from.
            user_id: Gmail user ID.
            history_types: Filter by history type (e.g., ["messageAdded"]).
            label_id: Filter by label (e.g., "INBOX").

        Returns:
            List of history records.
        """
        kwargs: dict[str, Any] = {
            "userId": user_id,
            "startHistoryId": start_history_id,
        }
        if history_types is not None:
            kwargs["historyTypes"] = history_types
        if label_id is not None:
            kwargs["labelId"] = label_id
        all_history: list[dict[str, Any]] = []
        while True:
            response: dict[str, Any] = (
                self._service.users().history().list(**kwargs).execute()
            )
            all_history.extend(response.get("history", []))
            next_page_token = response.get("nextPageToken")
            if next_page_token is None:
                break
            kwargs["pageToken"] = next_page_token
        return all_history

    @_retry_on_transient
    def watch(
        self,
        topic_name: str,
        user_id: str = "me",
    ) -> dict[str, Any]:
        """Set up Gmail push notifications via Pub/Sub.

        Args:
            topic_name: Full Pub/Sub topic name (projects/{project}/topics/{topic}).
            user_id: Gmail user ID.

        Returns:
            Watch response with historyId and expiration.
        """
        body = {
            "topicName": topic_name,
            "labelIds": ["INBOX"],
        }
        result: dict[str, Any] = (
            self._service.users().watch(userId=user_id, body=body).execute()
        )
        return result

    @_retry_on_transient
    def stop_watch(self, user_id: str = "me") -> None:
        """Stop Gmail push notifications for this account.

        Args:
            user_id: Gmail user ID (default "me" for delegated user).
        """
        self._service.users().stop(userId=user_id).execute()

    _BATCH_SIZE = 100
    """Maximum messages per ``new_batch_http_request()`` call."""

    @_retry_on_transient
    def get_messages_batch(
        self,
        message_ids: list[str],
        user_id: str = "me",
        format_: str = "full",
    ) -> list[dict[str, Any]]:
        """Fetch multiple messages in batched HTTP requests.

        Uses ``new_batch_http_request()`` to multiplex up to 100 individual
        gets into a single HTTP round-trip.  Deleted/404 messages are
        silently skipped (same semantics as ``get_message`` returning None).

        Args:
            message_ids: Gmail message IDs to fetch.
            user_id: Gmail user ID.
            format_: Message format (full, metadata, minimal, raw).

        Returns:
            List of successfully fetched message dicts (order not guaranteed).
        """
        if not message_ids:
            return []

        results: list[dict[str, Any]] = []
        failed_ids: list[str] = []
        total_batches = (len(message_ids) + self._BATCH_SIZE - 1) // self._BATCH_SIZE

        def _callback(
            request_id: str,
            response: dict[str, Any] | None,
            exception: Exception | None,
        ) -> None:
            if exception is not None:
                from googleapiclient.errors import HttpError

                if isinstance(exception, HttpError) and exception.resp.status == 404:
                    logfire.debug(
                        "gmail message not found (deleted)",
                        message_id=request_id,
                    )
                    return
                logfire.warn(
                    "gmail batch message error",
                    message_id=request_id,
                    error=str(exception),
                )
                failed_ids.append(request_id)
                return
            if response is not None:
                results.append(response)

        for batch_index, start in enumerate(
            range(0, len(message_ids), self._BATCH_SIZE)
        ):
            chunk = message_ids[start : start + self._BATCH_SIZE]
            with logfire.span(
                "gmail.get_messages_batch.chunk",
                count=len(chunk),
                batch_index=batch_index,
                total_batches=total_batches,
                user_id=user_id,
            ) as span:
                batch = self._service.new_batch_http_request()
                for msg_id in chunk:
                    request = (
                        self._service.users()
                        .messages()
                        .get(userId=user_id, id=msg_id, format=format_)
                    )
                    batch.add(request, callback=_callback, request_id=msg_id)
                batch.execute()
                span.set_attribute("failed_count", len(failed_ids))

        return results

    def create_label_if_not_exists(
        self,
        label_name: str,
        user_id: str = "me",
    ) -> str:
        """Create a Gmail label or return existing label ID.

        Args:
            label_name: Label name to create.
            user_id: Gmail user ID.

        Returns:
            Label ID.
        """
        from googleapiclient.errors import HttpError

        # Check existing labels.
        response = self._service.users().labels().list(userId=user_id).execute()
        for label in response.get("labels", []):
            if label.get("name") == label_name:
                label_id: str = label["id"]
                return label_id

        # Create new label.
        try:
            result = (
                self._service.users()
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
            logfire.warn(
                "gmail label creation failed",
                name=label_name,
                error=str(exc),
            )
            raise


# -- Standalone utilities (no service needed) ----------------------------------


def extract_text_from_message(message: dict[str, Any]) -> str:
    """Extract plain text from a Gmail message payload.

    Walks MIME parts recursively. Uses text/plain parts only.
    Normalizes whitespace: strips trailing spaces per line, collapses
    runs of 3+ blank lines to 2, and strips leading/trailing blank lines.
    Returns empty string if no text/plain part found (per ADR-02).

    Args:
        message: Full Gmail message dict (format="full").

    Returns:
        Extracted and normalized plain text body.
    """
    payload = message.get("payload", {})
    raw = _extract_text_from_part(payload)
    return _normalize_text(raw)


def _normalize_text(text: str) -> str:
    """Normalize extracted email text.

    Strips trailing whitespace per line, collapses 3+ consecutive
    blank lines to 2, and strips leading/trailing blank lines.
    """
    if not text:
        return ""
    lines = [line.rstrip() for line in text.splitlines()]
    collapsed: list[str] = []
    blank_count = 0
    for line in lines:
        if line == "":
            blank_count += 1
            if blank_count <= 2:
                collapsed.append(line)
        else:
            blank_count = 0
            collapsed.append(line)
    return "\n".join(collapsed).strip()


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


def parse_sender(from_header: str) -> tuple[str, str | None, str | None]:
    """Parse a From header into email, first name, and last name.

    Handles formats like:
    - ``"John Doe <john@example.com>"``
    - ``"john@example.com"``
    - ``"<john@example.com>"``
    - ``'"Jane Smith" <jane@example.com>'``

    Args:
        from_header: Raw From header value.

    Returns:
        Tuple of (email, first_name, last_name). Name fields are None
        if no display name is present.
    """
    display_name, email_address = parseaddr(from_header)
    if not email_address:
        email_address = from_header.strip()
    if not display_name:
        return (email_address, None, None)
    parts = display_name.strip().split(None, 1)
    first_name = parts[0] if parts else None
    last_name = parts[1] if len(parts) > 1 else None
    return (email_address, first_name, last_name)
