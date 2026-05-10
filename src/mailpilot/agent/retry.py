"""Transient-failure classification for the agent task loop.

Implements the bounded auto-retry policy described in `§V.44`. The
classifier inspects exception type and (where applicable) HTTP status
code, returning ``True`` only for failures that are safely re-drivable
without risking duplicate side-effects on the next attempt.

Carve-out (`§V.43`): Anthropic LLM read-timeouts (``httpx.ReadTimeout``,
``anthropic.APITimeoutError``) are *not* transient for retry purposes -
they may interrupt a multi-turn run mid tool-call, after the underlying
side-effect (``send_email``, ``reply_email``, Drive read) has already
fired. Idempotency is not guaranteed across SMTP / Drive boundaries, so
those failures bubble up as terminal and require operator replay.
"""

from __future__ import annotations

import socket

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

_GOOGLE_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
"""HTTP status codes treated as transient on Google API ``HttpError``
(Drive, Gmail). 429 = quota / rate-limit; 5xx = upstream blip.
"""

_ANTHROPIC_TRANSIENT_STATUSES = frozenset({502, 503, 529})
"""HTTP status codes treated as transient on Anthropic
``APIStatusError``. 502/503 = upstream gateway / service blip;
529 = Anthropic-specific "overloaded".

Critically excludes the LLM's own *timeout* class (``APITimeoutError``)
- those are filtered earlier in :func:`is_transient` so the `§V.43`
exclusion holds even if the timeout exception inherits from
``APIStatusError`` in some SDK version.
"""


def _is_llm_read_timeout(exc: BaseException) -> bool:
    """Anthropic LLM read-timeout: bubble as terminal per `§V.43`.

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
    out earlier so it bubbles as terminal per `§V.43`.
    """
    from anthropic import APIStatusError

    if isinstance(exc, APIStatusError):
        return getattr(exc, "status_code", None)
    return None


def is_transient(exc: BaseException) -> bool:
    """Return ``True`` if ``exc`` is safe to retry per `§V.44`.

    Args:
        exc: Exception raised by ``invoke_workflow_agent``.

    Returns:
        ``True`` for the V44 allow-list (Google 429/5xx, Anthropic
        502/503/529, Drive socket timeouts). ``False`` otherwise --
        including for the V43 exclusion (Anthropic LLM read-timeouts)
        and any unrecognised exception class.
    """
    if _is_llm_read_timeout(exc):
        return False

    google_status = _google_status(exc)
    if google_status is not None:
        return google_status in _GOOGLE_TRANSIENT_STATUSES

    anthropic_status = _anthropic_status(exc)
    if anthropic_status is not None:
        return anthropic_status in _ANTHROPIC_TRANSIENT_STATUSES

    # ``socket.timeout`` aliases ``TimeoutError`` on Python 3.10+; the
    # Drive httplib2 path bounded by ``Http(timeout=...)`` raises
    # subclasses of either, so this single check covers both.
    return isinstance(exc, (socket.timeout, TimeoutError))
