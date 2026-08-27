"""Read-only companies+contacts TTY browser."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from mailpilot.cli.main import (
    main,
    output_error,
)

if TYPE_CHECKING:
    from mailpilot.tui import TuiConnectError


def _stdout_is_tty() -> bool:
    """Return True when stdout is an interactive terminal."""
    import sys

    return sys.stdout.isatty()


def _import_tui() -> tuple[type[TuiConnectError], Callable[[], None]]:
    """Lazy-import the Textual app (optional extra)."""
    from mailpilot.tui import TuiConnectError, run_tui

    return TuiConnectError, run_tui


@main.command()
def tui() -> None:
    """Read-only TTY browser for companies and contacts.

    Requires a TTY and the mailpilot-crm[tui] extra. Refuses to start
    when stdout is not a terminal. Does not provision schema or write.
    """
    if not _stdout_is_tty():
        output_error(
            "mailpilot tui requires a TTY; stdout is not a terminal",
            "validation_error",
        )
    try:
        connect_error, run_tui = _import_tui()
    except ImportError as exc:
        missing = getattr(exc, "name", None) or ""
        if missing == "textual" or "textual" in str(exc):
            output_error(
                "textual is not installed; pip install 'mailpilot-crm[tui]' "
                "(or the uv equivalent)",
                "validation_error",
            )
        raise
    try:
        run_tui()
    except connect_error as exc:
        output_error(exc.message, exc.code)
