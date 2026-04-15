"""Tests for Gmail utility functions (no service/network needed)."""

import base64

from mailpilot.gmail import extract_text_from_message, get_message_headers


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
