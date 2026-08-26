"""Tests for the bounded auto-retry classifier (`§V.49`)."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from anthropic import APIStatusError, APITimeoutError
from googleapiclient.errors import HttpError

from mailpilot.agent.retry import (
    BACKOFF_SECONDS,
    GOOGLE_TRANSIENT_STATUSES,
    MAX_ATTEMPTS,
    is_invalid_provider_key,
    is_transient,
)


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
    """§V.49: 4 attempts total, backoff schedule [30, 120, 300]s."""
    assert MAX_ATTEMPTS == 4
    assert BACKOFF_SECONDS == (30, 120, 300)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_google_http_error_transient_statuses(status: int) -> None:
    assert is_transient(_http_error(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 529])
def test_google_http_error_non_transient_statuses(status: int) -> None:
    """529 is Anthropic-only; Google HttpError 529 is not retried (§V.189)."""
    assert is_transient(_http_error(status)) is False


def test_google_transient_set_is_shared_and_excludes_529() -> None:
    from mailpilot import google_auth

    assert GOOGLE_TRANSIENT_STATUSES is google_auth.GOOGLE_TRANSIENT_STATUSES
    assert 529 not in GOOGLE_TRANSIENT_STATUSES


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
    """§V.48 exclusion: LLM read-timeout cannot be safely retried mid-turn."""
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = APITimeoutError(request=request)
    assert is_transient(err) is False


def test_httpx_read_timeout_not_transient_v43_exclusion() -> None:
    """§V.48 exclusion: bare httpx.ReadTimeout from the Anthropic transport
    must not be retried."""
    assert is_transient(httpx.ReadTimeout("timeout")) is False


def test_httpx2_read_timeout_not_transient_v43_exclusion() -> None:
    """§V.48 exclusion: anthropic 1.x transport raises httpx2.ReadTimeout."""
    import httpx2

    assert is_transient(httpx2.ReadTimeout("timeout")) is False


def test_arbitrary_exception_not_transient() -> None:
    assert is_transient(RuntimeError("oh no")) is False
    assert is_transient(ValueError("bad")) is False


def test_agent_completed_without_reply_not_transient() -> None:
    """§V.120: a dropped inbound reply is terminal, never retried.

    The class is unrecognised by ``is_transient`` -- it falls through to the
    default ``False`` so ``_handle_agent_failure`` takes the task terminal
    ``failed`` with an operator error rather than re-driving a run whose
    tool side-effects may already have fired."""
    from mailpilot.exceptions import AgentCompletedWithoutReplyError

    assert is_transient(AgentCompletedWithoutReplyError("no reply")) is False


def _xai_incorrect_api_key() -> Exception:
    """xAI present-but-wrong key as raised by pydantic-ai (§B.152)."""
    from pydantic_ai.exceptions import ModelAPIError

    return ModelAPIError(
        "grok-4.5",
        "Incorrect API key provided. You can obtain an API key from https://console.x.ai.",
    )


def test_xai_incorrect_api_key_is_invalid_provider_key() -> None:
    """§V.47 / §B.152: xAI ModelAPIError 'Incorrect API key' is host-config."""
    err = _xai_incorrect_api_key()
    assert is_invalid_provider_key(err) is True
    assert is_transient(err) is False


def test_anthropic_401_is_invalid_provider_key() -> None:
    """§V.47: Anthropic 401 is the same host-config class as xAI invalid key."""
    err = _api_status_error(401)
    assert is_invalid_provider_key(err) is True
    assert is_transient(err) is False


def test_model_http_error_401_is_invalid_provider_key() -> None:
    """§V.47: pydantic-ai ModelHTTPError 401 is invalid-key regardless of body."""
    from pydantic_ai.exceptions import ModelHTTPError

    err = ModelHTTPError(401, "claude-sonnet-5", body="authentication_error")
    assert is_invalid_provider_key(err) is True
    assert is_transient(err) is False


def test_wrapped_invalid_key_is_still_host_config() -> None:
    """§V.47: invalid-key signal in ``__cause__`` is the same class."""
    inner = _xai_incorrect_api_key()
    err = RuntimeError("agent run failed")
    err.__cause__ = inner
    assert is_invalid_provider_key(err) is True


def test_other_model_api_error_is_not_invalid_provider_key() -> None:
    """§V.47: non-auth ModelAPIError stays a per-task failure, not host-config."""
    from pydantic_ai.exceptions import ModelAPIError

    err = ModelAPIError("grok-4.5", "Internal server error")
    assert is_invalid_provider_key(err) is False
    assert is_transient(err) is False


def test_google_http_401_is_not_invalid_provider_key() -> None:
    """§V.47: Gmail 401 is not an LLM host-config key error."""
    err = _http_error(401)
    assert is_invalid_provider_key(err) is False
    assert is_transient(err) is False


def test_anthropic_503_is_not_invalid_provider_key() -> None:
    """§V.49: Anthropic overload stays transient retry, not host-config skip."""
    err = _api_status_error(503)
    assert is_invalid_provider_key(err) is False
    assert is_transient(err) is True
