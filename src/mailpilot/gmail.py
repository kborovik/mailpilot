"""Gmail API client using service account with domain-wide delegation.

Credentials and the delegated-service factory live in
:mod:`mailpilot.google_auth`. This module owns Gmail-specific retry,
``GmailClient``, and MIME text extraction.

Scope: ``https://www.googleapis.com/auth/gmail.modify``
"""

from __future__ import annotations

import base64
import time
from email.mime.base import MIMEBase
from email.utils import parseaddr
from functools import wraps
from importlib.metadata import version
from typing import Any, ClassVar

import logfire

from mailpilot.exceptions import GmailBatchFetchError
from mailpilot.google_auth import GOOGLE_TRANSIENT_STATUSES, GoogleClient

# Translation table that drops every C0 control byte except \t (0x09) and \n
# (0x0A). Strict JSON parsers (RFC 8259) reject bare C0 controls in strings;
# \t and \n are kept because Python's json module escapes them correctly and
# they appear in legitimate email bodies. \r is intentionally dropped: inbound
# Gmail bodies arrive with CRLF line endings, so removing \r leaves clean
# \n-delimited lines for downstream splitlines/normalisation.
_CONTROL_CHAR_TABLE = dict.fromkeys(
    [i for i in range(0x20) if i not in (0x09, 0x0A)],
    None,
)


def strip_control_chars(text: str) -> str:
    """Remove C0 control bytes that break strict JSON parsing of body_text."""
    return text.translate(_CONTROL_CHAR_TABLE)


GmailService = Any
"""Type alias for the Gmail API service resource (untyped by Google)."""

_GMAIL_SCOPE = ["https://www.googleapis.com/auth/gmail.modify"]

_MAX_RETRIES = 5
_MAX_BACKOFF = 30.0

# Custom headers added to all outgoing emails. Distribution name is
# mailpilot-crm; the import package stays mailpilot.
_MAILPILOT_VERSION = version("mailpilot-crm")


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
                    if exc.resp.status not in GOOGLE_TRANSIENT_STATUSES:
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


# -- GmailClient --------------------------------------------------------------


class GmailClient(GoogleClient):
    """Thin wrapper around Gmail API service for per-account operations.

    Holds the service instance so callers don't pass it to every function.
    Initialized with an email address; builds the delegated service internally.

    Usage::

        client = GmailClient("user@example.com")
        profile = client.get_profile()
        client.send_message(to="x@y.com", subject="Hi", body="Hello")
    """

    _api = "gmail"
    _version = "v1"
    _scopes: ClassVar[list[str]] = _GMAIL_SCOPE

    def get_profile(self) -> dict[str, Any]:
        """Fetch Gmail user profile.

        Returns:
            Profile dict with emailAddress, messagesTotal, etc.
        """
        result: dict[str, Any] = self._service.users().getProfile(userId="me").execute()
        return result

    @_retry_on_transient
    def list_messages(
        self,
        query: str = "",
        max_results: int = 100,
        label_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """List messages matching a Gmail search query.

        Args:
            query: Gmail search query (e.g. "is:unread in:inbox").
            max_results: Maximum number of messages to return.
            label_ids: Filter by label IDs (e.g., ["INBOX"]). AND logic.

        Returns:
            List of message stubs with id and threadId.
        """
        kwargs: dict[str, Any] = {
            "userId": "me",
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
        format_: str = "full",
    ) -> dict[str, Any] | None:
        """Fetch a single message by ID.

        Args:
            message_id: Gmail message ID.
            format_: Message format (full, metadata, minimal, raw).

        Returns:
            Full message dict, or None if message was deleted.
        """
        from googleapiclient.errors import HttpError

        try:
            result: dict[str, Any] = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format=format_)
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
            self._service.users().messages().send(userId="me", body=send_body).execute()
        )
        return result

    @_retry_on_transient
    def get_history(
        self,
        start_history_id: str,
        history_types: list[str] | None = None,
        label_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch mailbox changes since a history ID.

        Pages through all results automatically.

        Args:
            start_history_id: History ID to start from.
            history_types: Filter by history type (e.g., ["messageAdded"]).
            label_id: Filter by label (e.g., "INBOX").

        Returns:
            List of history records.
        """
        kwargs: dict[str, Any] = {
            "userId": "me",
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
    ) -> dict[str, Any]:
        """Set up Gmail push notifications via Pub/Sub.

        Args:
            topic_name: Full Pub/Sub topic name (projects/{project}/topics/{topic}).

        Returns:
            Watch response with historyId and expiration.
        """
        body = {
            "topicName": topic_name,
            "labelIds": ["INBOX"],
        }
        result: dict[str, Any] = (
            self._service.users().watch(userId="me", body=body).execute()
        )
        return result

    _BATCH_SIZE = 25
    """Maximum messages per ``new_batch_http_request()`` call.

    Kept well below Gmail's per-user concurrent-request cap: every sub-request
    in a batch counts as a concurrent call against the same user, so a large
    batch trips "Too many concurrent requests for user" (HTTP 429) (`§V.75`,
    `§B.105`).
    """

    def _fetch_batch_once(
        self,
        message_ids: list[str],
        format_: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Run one batched fetch pass over ``message_ids``.

        Classifies each per-message callback outcome: a 404 is skipped
        (deleted), a transient failure (429 / 5xx) is collected for retry, and
        any other error is logged and skipped.

        Returns:
            Tuple of (fetched message dicts, message ids that failed
            transiently and should be retried).
        """
        from googleapiclient.errors import HttpError

        fetched: list[dict[str, Any]] = []
        transient_failed: list[str] = []

        def _callback(
            request_id: str,
            response: dict[str, Any] | None,
            exception: Exception | None,
        ) -> None:
            if exception is not None:
                status = (
                    exception.resp.status if isinstance(exception, HttpError) else None
                )
                if status == 404:
                    logfire.debug(
                        "gmail message not found (deleted)",
                        message_id=request_id,
                    )
                    return
                if status in GOOGLE_TRANSIENT_STATUSES:
                    transient_failed.append(request_id)
                    return
                logfire.warn(
                    "gmail batch message error",
                    message_id=request_id,
                    error=str(exception),
                )
                return
            if response is not None:
                fetched.append(response)

        total_batches = (len(message_ids) + self._BATCH_SIZE - 1) // self._BATCH_SIZE
        for batch_index, start in enumerate(
            range(0, len(message_ids), self._BATCH_SIZE)
        ):
            chunk = message_ids[start : start + self._BATCH_SIZE]
            with logfire.span(
                "gmail.get_messages_batch.chunk",
                count=len(chunk),
                batch_index=batch_index,
                total_batches=total_batches,
                user_id=self.email,
            ) as span:
                batch = self._service.new_batch_http_request()
                for msg_id in chunk:
                    request = (
                        self._service.users()
                        .messages()
                        .get(userId="me", id=msg_id, format=format_)
                    )
                    batch.add(request, callback=_callback, request_id=msg_id)
                batch.execute()
                span.set_attribute("transient_failed_count", len(transient_failed))

        return fetched, transient_failed

    def get_messages_batch(
        self,
        message_ids: list[str],
        format_: str = "full",
    ) -> list[dict[str, Any]]:
        """Fetch multiple messages in batched HTTP requests.

        Uses ``new_batch_http_request()`` to multiplex individual gets into
        batched HTTP round-trips. A deleted/404 message is skipped (same
        semantics as ``get_message`` returning None). A transient per-message
        failure (Gmail 429 "Too many concurrent requests" or 5xx) is retried
        with bounded backoff; if it survives the retry budget the call raises
        ``GmailBatchFetchError`` rather than returning a partial list, so a
        caller never advances its sync checkpoint past unfetched mail
        (`§V.75`, `§B.105`). Whole-call retry is not applied; only per-item
        retry runs (`§V.189`).

        Args:
            message_ids: Gmail message IDs to fetch.
            format_: Message format (full, metadata, minimal, raw).

        Returns:
            List of successfully fetched message dicts (order not guaranteed).

        Raises:
            GmailBatchFetchError: transient failures survived all retries.
        """
        if not message_ids:
            return []

        results: list[dict[str, Any]] = []
        pending: list[str] = list(message_ids)

        for attempt in range(_MAX_RETRIES):
            fetched, transient_failed = self._fetch_batch_once(pending, format_)
            results.extend(fetched)
            if not transient_failed:
                return results

            pending = transient_failed
            if attempt + 1 < _MAX_RETRIES:
                backoff = min(2**attempt, _MAX_BACKOFF)
                logfire.warn(
                    "gmail batch transient errors, retrying",
                    count=len(pending),
                    attempt=attempt + 1,
                    backoff=backoff,
                )
                time.sleep(backoff)

        logfire.error(
            "gmail.get_messages_batch.retry.exhausted",
            unfetched=len(pending),
            attempts=_MAX_RETRIES,
        )
        raise GmailBatchFetchError(
            f"{len(pending)} message(s) unfetched after {_MAX_RETRIES} "
            "attempts (Gmail rate limit)"
        )


# -- Standalone utilities (no service needed) ----------------------------------


def extract_text_from_message(message: dict[str, Any]) -> str:
    """Extract plain text from a Gmail message payload.

    Walks MIME parts recursively. Uses text/plain parts only.
    Normalizes whitespace: strips trailing spaces per line, collapses
    runs of 3+ blank lines to 2, and strips leading/trailing blank lines.
    Returns empty string if no text/plain part found (per §C plain-text-only rule).

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
    text = strip_control_chars(text)
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
