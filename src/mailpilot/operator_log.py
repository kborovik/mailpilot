"""Operator-facing console output for ``mailpilot run``.

Independent of Logfire's console exporter. Always emits to stderr
(captured by journald under systemd). Use Logfire for deep traces;
use this for the lifecycle/error layer operators monitor.

Stderr keeps stdout strict-JSON for single-shot CLI commands like
``mailpilot enrollment run`` whose stdout is consumed by parsers.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any


def operator_event(event_name: str, /, **fields: Any) -> None:
    """Write one structured line to stderr.

    Format: ``HH:MM:SS event=NAME k1=v1 k2=v2`` -- single line, ASCII-only.
    Values containing whitespace are double-quoted with internal quotes
    escaped. Embedded newlines in values are collapsed to spaces so a
    multi-line ``str(exc)`` cannot break the one-line-per-event contract
    that journald grep relies on.

    ``event_name`` is positional-only so callers may pass ``name=`` as a
    structured field without colliding with this parameter.
    """
    timestamp = time.strftime("%H:%M:%S")
    parts = [f"event={event_name}"]
    for key, value in fields.items():
        text = str(value).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        if any(character.isspace() for character in text):
            text = '"' + text.replace('"', '\\"') + '"'
        parts.append(f"{key}={text}")
    line = f"{timestamp} " + " ".join(parts)
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


_PSYCOPG_ERROR_CODES: tuple[tuple[str, str], ...] = (
    ("UniqueViolation", "duplicate_key"),
    ("ForeignKeyViolation", "foreign_key_violation"),
    ("NotNullViolation", "not_null_violation"),
    ("CheckViolation", "check_violation"),
)


@contextmanager
def cli_mutation(noun: str, verb: str, /, **attrs: Any) -> Generator[None]:
    """Wrap an operator-initiated CRM-config CLI mutation per SPEC §V.54.

    Enters ``logfire.span("<noun>.<verb>", **attrs)``. On uncaught
    ``Exception`` (e.g. ``psycopg.OperationalError``), emits
    ``logfire.exception`` and a paired ``operator_event("error",
    source="<noun>.<verb>", message=str(exc))`` before re-raising.

    On ``psycopg.Error``, after the observability pair, translates the
    constraint-class to a structured ``output_error(str(exc), code)``
    envelope (per §V.54 envelope-translation clause). Click's default
    handler never sees the ``psycopg.Error`` so stderr stays clean of
    raw ``Traceback`` dumps; stdout stays strict-JSON-empty per §V.3.

    ``pydantic.ValidationError`` (e.g. ``CompanyProfile`` validation per
    §V.72) is translated to ``validation_error`` via the same path so
    invalid JSON payloads surface as a structured envelope.

    Caller is responsible for emitting the success-path
    ``operator_event("<noun>.<verb>", ...)`` with the verb-appropriate
    ``changed=[...]`` field after the mutation lands.
    """
    import logfire

    span_name = f"{noun}.{verb}"
    failure_name = span_name + ".failed"
    with logfire.span(span_name, **attrs):
        try:
            yield
        except Exception as exc:
            logfire.exception(failure_name, **attrs)
            operator_event("error", source=span_name, message=str(exc))
            import psycopg
            from pydantic import ValidationError

            from mailpilot.cli import output_error

            if isinstance(exc, ValidationError):
                output_error(str(exc), "validation_error")
            if isinstance(exc, psycopg.Error):
                code = "database_error"
                for class_name, mapped in _PSYCOPG_ERROR_CODES:
                    subclass = getattr(psycopg.errors, class_name, None)
                    if subclass is not None and isinstance(exc, subclass):
                        code = mapped
                        break
                output_error(str(exc), code)
            raise
