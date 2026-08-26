"""Transient-failure classification for the agent task loop.

Implements the bounded auto-retry policy described in `§V.49`. The
classifier inspects exception type and (where applicable) HTTP status
code, returning ``True`` only for failures that are safely re-drivable
without risking duplicate side-effects on the next attempt.

Carve-out (`§V.48`): Anthropic LLM read-timeouts (``httpx.ReadTimeout``,
``anthropic.APITimeoutError``) are *not* transient for retry purposes -
they may interrupt a multi-turn run mid tool-call, after the underlying
side-effect (``send_email``, ``reply_email``, Drive read) has already
fired. Idempotency is not guaranteed across SMTP / Drive boundaries, so
those failures bubble up as terminal and require operator replay.
"""

from __future__ import annotations

import socket

from mailpilot.google_auth import GOOGLE_TRANSIENT_STATUSES

MAX_ATTEMPTS = 4
"""Total attempts (1 initial + 3 retries) before a transient failure
is escalated to terminal ``failed``."""

BACKOFF_SECONDS: tuple[int, ...] = (30, 120, 300)
"""Backoff schedule keyed by retry attempt index.

Index 0 is the delay before the *first* retry (after the initial
attempt failed); index 1 before the second; index 2 before the third.
After the third retry, ``MAX_ATTEMPTS`` is exhausted and the row goes
terminal.
"""

_ANTHROPIC_TRANSIENT_STATUSES = frozenset({502, 503, 529})
"""HTTP status codes treated as transient on Anthropic
``APIStatusError``. 502/503 = upstream gateway / service blip;
529 = Anthropic-specific "overloaded".

Critically excludes the LLM's own *timeout* class (``APITimeoutError``)
- those are filtered earlier in :func:`is_transient` so the `§V.48`
exclusion holds even if the timeout exception inherits from
``APIStatusError`` in some SDK version.
"""

_INVALID_KEY_MARKERS = (
    "incorrect api key",
    "invalid api key",
    "invalid api_key",
    "invalid x-api-key",
)
"""Substrings on ``ModelAPIError`` that mean present-but-wrong LLM key."""


def _is_llm_read_timeout(exc: BaseException) -> bool:
    """Anthropic LLM read-timeout: bubble as terminal per `§V.48`.

    A timeout on the LLM HTTP call may have interrupted a multi-turn run
    after a tool already fired (``send_email``, ``reply_email``, Drive
    read), so re-driving the task could duplicate side-effects.
    """
    import httpx
    from anthropic import APITimeoutError

    return isinstance(exc, (APITimeoutError, httpx.ReadTimeout))


def _google_status(exc: BaseException) -> int | None:
    """Status code on a googleapiclient ``HttpError``, else ``None``."""
    from googleapiclient.errors import HttpError

    if isinstance(exc, HttpError):
        return getattr(exc.resp, "status", None)
    return None


def _anthropic_status(exc: BaseException) -> int | None:
    """Status code on an Anthropic ``APIStatusError``, else ``None``.

    ``APITimeoutError`` is not handled here -- the caller filters it
    out earlier so it bubbles as terminal per `§V.48`.
    """
    from anthropic import APIStatusError

    if isinstance(exc, APIStatusError):
        return getattr(exc, "status_code", None)
    return None


def _walk_exception_chain(exc: BaseException) -> list[BaseException]:
    """Return ``exc`` plus ``__cause__`` / ``__context__`` without cycles."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = (
            current.__cause__ if current.__cause__ is not None else current.__context__
        )
    return chain


def _message_looks_like_invalid_key(exc: BaseException) -> bool:
    """True when ``exc``'s message names an incorrect/invalid API key."""
    text = str(exc).lower()
    return any(marker in text for marker in _INVALID_KEY_MARKERS)


def is_invalid_provider_key(exc: BaseException) -> bool:
    """Return ``True`` if ``exc`` is a present-but-wrong LLM API key.

    §V.47 / §B.152 host-config class (not a per-task failure): xAI
    ``pydantic_ai.exceptions.ModelAPIError`` with "Incorrect API key" (or
    equivalent) and Anthropic 401. Walks ``__cause__`` / ``__context__``
    so a wrapped raise still matches. Gmail ``HttpError`` 401 is not this
    class.

    Args:
        exc: Exception raised by ``invoke_workflow_agent``.

    Returns:
        ``True`` for invalid-key signals that should skip remaining drain.
    """
    from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

    for current in _walk_exception_chain(exc):
        if isinstance(current, ModelHTTPError) and current.status_code == 401:
            return True
        anthropic_status = _anthropic_status(current)
        if anthropic_status == 401:
            return True
        if isinstance(current, ModelAPIError) and _message_looks_like_invalid_key(
            current
        ):
            return True
    return False


def invalid_provider_key_message(llm_provider: str) -> str:
    """Operator message naming ``mailpilot config set`` for the active provider.

    Args:
        llm_provider: Active ``Settings.llm_provider`` (``xai`` or ``anthropic``).

    Returns:
        One-line instruction; never names ``MAILPILOT_*_API_KEY``.
    """
    if llm_provider == "anthropic":
        return (
            "anthropic_api_key is invalid; "
            "set it via `mailpilot config set anthropic_api_key`"
        )
    return "xai_api_key is invalid; set it via `mailpilot config set xai_api_key`"


def is_transient(exc: BaseException) -> bool:
    """Return ``True`` if ``exc`` is safe to retry per `§V.49`.

    Args:
        exc: Exception raised by ``invoke_workflow_agent``.

    Returns:
        ``True`` for the §V.49 allow-list (Google statuses from the shared
        set, Anthropic 502/503/529, Drive socket timeouts). ``False``
        otherwise --
        including for the §V.48 exclusion (Anthropic LLM read-timeouts)
        and any unrecognised exception class.
    """
    if _is_llm_read_timeout(exc):
        return False

    google_status = _google_status(exc)
    if google_status is not None:
        return google_status in GOOGLE_TRANSIENT_STATUSES

    anthropic_status = _anthropic_status(exc)
    if anthropic_status is not None:
        return anthropic_status in _ANTHROPIC_TRANSIENT_STATUSES

    # ``socket.timeout`` aliases ``TimeoutError`` on Python 3.10+; the
    # Drive httplib2 path bounded by ``Http(timeout=...)`` raises
    # subclasses of either, so this single check covers both.
    return isinstance(exc, (socket.timeout, TimeoutError))
