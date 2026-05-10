"""Tests for the bounded auto-retry classifier (`§V.44`)."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from anthropic import APIStatusError, APITimeoutError
from googleapiclient.errors import HttpError

from mailpilot.agent.retry import BACKOFF_SECONDS, MAX_ATTEMPTS, is_transient


def _http_error(status: int) -> HttpError:
    """Build a googleapiclient HttpError carrying a given status."""
    resp = MagicMock()
    resp.status = status
    resp.reason = "synthetic"
    return HttpError(resp, b"{}", uri="https://example.com")


def _api_status_error(status_code: int) -> APIStatusError:
    """Build an Anthropic APIStatusError carrying a given status."""
    response = MagicMock()
    response.status_code = status_code
    response.headers = {}
    body = {"error": {"message": "synthetic"}}
    return APIStatusError("synthetic", response=response, body=body)


def test_constants() -> None:
    """V44: 4 attempts total, backoff schedule [30, 120, 300]s."""
    assert MAX_ATTEMPTS == 4
    assert BACKOFF_SECONDS == (30, 120, 300)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_google_http_error_transient_statuses(status: int) -> None:
    assert is_transient(_http_error(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_google_http_error_non_transient_statuses(status: int) -> None:
    assert is_transient(_http_error(status)) is False


@pytest.mark.parametrize("status", [502, 503, 529])
def test_anthropic_api_status_error_transient(status: int) -> None:
    assert is_transient(_api_status_error(status)) is True


@pytest.mark.parametrize("status", [400, 401, 404, 500, 504])
def test_anthropic_api_status_error_non_transient(status: int) -> None:
    """500/504 deliberately excluded -- only 502/503/529 are server-side
    overload signals on Anthropic."""
    assert is_transient(_api_status_error(status)) is False


def test_socket_timeout_transient() -> None:
    assert is_transient(TimeoutError("read timed out")) is True


def test_timeout_error_transient() -> None:
    assert is_transient(TimeoutError("read timed out")) is True


def test_anthropic_api_timeout_error_not_transient_v43_exclusion() -> None:
    """V43 exclusion: LLM read-timeout cannot be safely retried mid-turn."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = APITimeoutError(request=request)
    assert is_transient(err) is False


def test_httpx_read_timeout_not_transient_v43_exclusion() -> None:
    """V43 exclusion: bare httpx.ReadTimeout from the Anthropic transport
    must not be retried."""
    assert is_transient(httpx.ReadTimeout("timeout")) is False


def test_arbitrary_exception_not_transient() -> None:
    assert is_transient(RuntimeError("oh no")) is False
    assert is_transient(ValueError("bad")) is False
