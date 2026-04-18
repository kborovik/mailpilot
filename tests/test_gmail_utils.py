"""Tests for Gmail utility functions (no service/network needed)."""

import base64
from unittest.mock import patch

import pytest

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
