"""Tests for Gmail utility functions (no service/network needed)."""

import base64
from unittest.mock import patch

import pytest
from logfire.testing import CaptureLogfire

from mailpilot import gmail
from mailpilot.gmail import (
    extract_text_from_message,
    get_message_headers,
    parse_sender,
)
from mailpilot.settings import Settings


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def test_extract_plain_text():
    message = {
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": _b64("Hello world")},
        }
    }
    assert extract_text_from_message(message) == "Hello world"


def test_extract_multipart_prefers_plain():
    message = {
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _b64("Plain text")},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": _b64("<p>HTML</p>")},
                },
            ],
        }
    }
    assert extract_text_from_message(message) == "Plain text"


def test_extract_html_only_returns_empty():
    message = {
        "payload": {
            "mimeType": "text/html",
            "body": {"data": _b64("<p>HTML only</p>")},
        }
    }
    assert extract_text_from_message(message) == ""


def test_extract_nested_multipart():
    message = {
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": _b64("Nested plain")},
                        },
                    ],
                },
            ],
        }
    }
    assert extract_text_from_message(message) == "Nested plain"


def test_extract_normalizes_whitespace():
    raw = "Hello  \n\n\n\n\nWorld\nEnd  "
    message = {
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": _b64(raw)},
        }
    }
    result = extract_text_from_message(message)
    assert result == "Hello\n\n\nWorld\nEnd"


def test_extract_strips_leading_trailing_blanks():
    raw = "\n\n\nContent\n\n\n"
    message = {
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": _b64(raw)},
        }
    }
    assert extract_text_from_message(message) == "Content"


def test_extract_empty_payload():
    assert extract_text_from_message({}) == ""
    assert extract_text_from_message({"payload": {}}) == ""


def test_extract_strips_control_characters():
    """C0 control bytes (NUL, BEL, ESC, ...) break strict JSON parsers."""
    raw = "Hello\x00world\x07\x1bend"
    message = {
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": _b64(raw)},
        }
    }
    assert extract_text_from_message(message) == "Helloworldend"


def test_extract_preserves_tabs_and_newlines():
    raw = "Line 1\tcol\nLine 2"
    message = {
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": _b64(raw)},
        }
    }
    assert extract_text_from_message(message) == "Line 1\tcol\nLine 2"


def test_extract_round_trips_through_json_strict_mode():
    """Body must be safe to embed in a JSON document with strict parsing."""
    import json

    raw = "Header\x00\x01\x02body\x1ftrailer\nDone"
    message = {
        "payload": {
            "mimeType": "text/plain",
            "body": {"data": _b64(raw)},
        }
    }
    body = extract_text_from_message(message)
    payload = json.dumps({"body_text": body})
    parsed = json.loads(payload)  # strict by default
    assert parsed["body_text"] == body


def test_get_message_headers():
    message = {
        "payload": {
            "headers": [
                {"name": "From", "value": "alice@example.com"},
                {"name": "Subject", "value": "Hello"},
                {"name": "X-Custom", "value": "test"},
            ]
        }
    }
    headers = get_message_headers(message)
    assert headers["from"] == "alice@example.com"
    assert headers["subject"] == "Hello"
    assert headers["x-custom"] == "test"


def test_get_message_headers_empty():
    assert get_message_headers({}) == {}
    assert get_message_headers({"payload": {}}) == {}


# -- parse_sender --------------------------------------------------------------


def test_parse_sender_full_name():
    email, first, last = parse_sender("John Doe <john@example.com>")
    assert email == "john@example.com"
    assert first == "John"
    assert last == "Doe"


def test_parse_sender_email_only():
    email, first, last = parse_sender("alice@example.com")
    assert email == "alice@example.com"
    assert first is None
    assert last is None


def test_parse_sender_angle_brackets_no_name():
    email, first, last = parse_sender("<bob@example.com>")
    assert email == "bob@example.com"
    assert first is None
    assert last is None


def test_parse_sender_quoted_name():
    email, first, last = parse_sender('"Jane Smith" <jane@example.com>')
    assert email == "jane@example.com"
    assert first == "Jane"
    assert last == "Smith"


def test_parse_sender_single_word_name():
    email, first, last = parse_sender("Madonna <madonna@example.com>")
    assert email == "madonna@example.com"
    assert first == "Madonna"
    assert last is None


def test_parse_sender_three_part_name():
    email, first, last = parse_sender("Mary Jane Watson <mj@example.com>")
    assert email == "mj@example.com"
    assert first == "Mary"
    assert last == "Jane Watson"


# -- credentials resolution ---------------------------------------------------


def test_resolve_credentials_path_from_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    settings = Settings(google_application_credentials="/tmp/service-account.json")
    with patch("mailpilot.settings.get_settings", return_value=settings):
        assert (
            gmail._resolve_credentials_path()  # pyright: ignore[reportPrivateUsage]
            == "/tmp/service-account.json"
        )


def test_resolve_credentials_path_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/env/key.json")
    settings = Settings(google_application_credentials="")
    with patch("mailpilot.settings.get_settings", return_value=settings):
        assert (
            gmail._resolve_credentials_path()  # pyright: ignore[reportPrivateUsage]
            == "/env/key.json"
        )


def test_resolve_credentials_path_settings_wins_over_env(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/env/key.json")
    settings = Settings(google_application_credentials="/cfg/key.json")
    with patch("mailpilot.settings.get_settings", return_value=settings):
        assert (
            gmail._resolve_credentials_path()  # pyright: ignore[reportPrivateUsage]
            == "/cfg/key.json"
        )


def test_resolve_credentials_path_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    settings = Settings(google_application_credentials="")
    with (
        patch("mailpilot.settings.get_settings", return_value=settings),
        pytest.raises(SystemExit, match="No service account credentials"),
    ):
        gmail._resolve_credentials_path()  # pyright: ignore[reportPrivateUsage]


# -- get_messages_batch --------------------------------------------------------


def test_get_messages_batch_returns_fetched_messages():
    from unittest.mock import MagicMock

    from mailpilot.gmail import GmailClient

    service = MagicMock()
    client = GmailClient.from_service("test@example.com", service)

    msg1 = {"id": "m1", "threadId": "t1", "payload": {}}
    msg2 = {"id": "m2", "threadId": "t2", "payload": {}}

    # Simulate new_batch_http_request: capture callbacks, invoke them.
    callbacks: list[tuple[str, object, object]] = []

    def fake_add(request: object, callback: object, request_id: str) -> None:
        callbacks.append((request_id, request, callback))

    batch = MagicMock()
    batch.add = fake_add

    def fake_execute() -> None:
        for request_id, _request, callback in callbacks:
            data = msg1 if request_id == "m1" else msg2
            callback(request_id, data, None)  # type: ignore[operator]

    batch.execute = fake_execute
    service.new_batch_http_request.return_value = batch

    results = client.get_messages_batch(["m1", "m2"])

    assert len(results) == 2
    assert results[0]["id"] == "m1"
    assert results[1]["id"] == "m2"


def test_get_messages_batch_skips_404_errors():
    from unittest.mock import MagicMock

    from googleapiclient.errors import HttpError

    from mailpilot.gmail import GmailClient

    service = MagicMock()
    client = GmailClient.from_service("test@example.com", service)

    msg1 = {"id": "m1", "threadId": "t1", "payload": {}}
    resp_404 = MagicMock()
    resp_404.status = 404
    error_404 = HttpError(resp=resp_404, content=b"Not Found")

    callbacks: list[tuple[str, object, object]] = []

    def fake_add(request: object, callback: object, request_id: str) -> None:
        callbacks.append((request_id, request, callback))

    batch = MagicMock()
    batch.add = fake_add

    def fake_execute() -> None:
        for request_id, _request, callback in callbacks:
            if request_id == "m1":
                callback(request_id, msg1, None)  # type: ignore[operator]
            else:
                callback(request_id, None, error_404)  # type: ignore[operator]

    batch.execute = fake_execute
    service.new_batch_http_request.return_value = batch

    results = client.get_messages_batch(["m1", "m2"])

    assert len(results) == 1
    assert results[0]["id"] == "m1"


def test_get_messages_batch_empty_list():
    from unittest.mock import MagicMock

    from mailpilot.gmail import GmailClient

    service = MagicMock()
    client = GmailClient.from_service("test@example.com", service)

    results = client.get_messages_batch([])

    assert results == []
    service.new_batch_http_request.assert_not_called()


def test_get_messages_batch_skips_non_404_errors():
    from unittest.mock import MagicMock

    from googleapiclient.errors import HttpError

    from mailpilot.gmail import GmailClient

    service = MagicMock()
    client = GmailClient.from_service("test@example.com", service)

    msg1 = {"id": "m1", "threadId": "t1", "payload": {}}
    resp_403 = MagicMock()
    resp_403.status = 403
    error_403 = HttpError(resp=resp_403, content=b"Forbidden")

    callbacks: list[tuple[str, object, object]] = []

    def fake_add(request: object, callback: object, request_id: str) -> None:
        callbacks.append((request_id, request, callback))

    batch = MagicMock()
    batch.add = fake_add

    def fake_execute() -> None:
        for request_id, _request, callback in callbacks:
            if request_id == "m1":
                callback(request_id, msg1, None)  # type: ignore[operator]
            else:
                callback(request_id, None, error_403)  # type: ignore[operator]

    batch.execute = fake_execute
    service.new_batch_http_request.return_value = batch

    results = client.get_messages_batch(["m1", "m2"])

    assert len(results) == 1
    assert results[0]["id"] == "m1"


def test_get_messages_batch_chunks_large_lists():
    from unittest.mock import MagicMock

    from mailpilot.gmail import GmailClient

    service = MagicMock()
    client = GmailClient.from_service("test@example.com", service)

    # 150 message IDs -> should produce 2 batches (100 + 50).
    message_ids = [f"m{i}" for i in range(150)]

    batch_execute_count = 0

    def make_batch() -> MagicMock:
        batch_callbacks: list[tuple[str, object, object]] = []

        def fake_add(request: object, callback: object, request_id: str) -> None:
            batch_callbacks.append((request_id, request, callback))

        def fake_execute() -> None:
            nonlocal batch_execute_count
            batch_execute_count += 1
            for request_id, _request, callback in batch_callbacks:
                callback(request_id, {"id": request_id, "payload": {}}, None)  # type: ignore[operator]

        batch = MagicMock()
        batch.add = fake_add
        batch.execute = fake_execute
        return batch

    service.new_batch_http_request.side_effect = [make_batch(), make_batch()]

    results = client.get_messages_batch(message_ids)

    assert len(results) == 150
    assert batch_execute_count == 2


# -- stop_watch ----------------------------------------------------------------


def test_stop_watch_calls_users_stop():
    from unittest.mock import MagicMock

    from mailpilot.gmail import GmailClient

    service = MagicMock()
    client = GmailClient.from_service("test@example.com", service)

    client.stop_watch()

    service.users().stop.assert_called_once_with(userId="me")
    service.users().stop().execute.assert_called_once()


# -- get_profile span regression -----------------------------------------------


def test_get_profile_does_not_emit_span_on_success(capfire: CaptureLogfire) -> None:
    """get_profile is called every sync iteration -- avoid span noise.

    Regression target for the 2026-04-26 smoke test observation that
    gmail.get_profile dominated logfire volume (42 spans in ~5 minutes
    for two accounts). The parent sync.account.run span captures the
    failure mode; the per-call span adds no diagnostic value.
    """
    from unittest.mock import MagicMock

    from mailpilot.gmail import GmailClient

    service = MagicMock()
    service.users.return_value.getProfile.return_value.execute.return_value = {
        "emailAddress": "test@example.com",
        "historyId": "100",
    }
    client = GmailClient.from_service("test@example.com", service)

    client.get_profile()

    span_names = [s["name"] for s in capfire.exporter.exported_spans_as_dict()]
    assert "gmail.get_profile" not in span_names
