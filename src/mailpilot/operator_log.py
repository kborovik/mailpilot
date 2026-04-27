"""Operator-facing console output for ``mailpilot run``.

Independent of Logfire's console exporter. Always emits to stdout
(captured by journald under systemd). Use Logfire for deep traces;
use this for the lifecycle/error layer operators monitor.

See ADR-07 "Operator log layer".
"""

from __future__ import annotations

import sys
import time
from typing import Any


def operator_event(name: str, **fields: Any) -> None:
    """Write one structured line to stdout.

    Format: ``HH:MM:SS event=NAME k1=v1 k2=v2`` -- single line, ASCII-only.
    Values containing whitespace are double-quoted with internal quotes
    escaped. Embedded newlines in values are collapsed to spaces so a
    multi-line ``str(exc)`` cannot break the one-line-per-event contract
    that journald grep relies on.
    """
    timestamp = time.strftime("%H:%M:%S")
    parts = [f"event={name}"]
    for key, value in fields.items():
        text = str(value).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        if any(character.isspace() for character in text):
            text = '"' + text.replace('"', '\\"') + '"'
        parts.append(f"{key}={text}")
    line = f"{timestamp} " + " ".join(parts)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
