"""Sync loop lifecycle: startup, heartbeat, signal handling, shutdown.

Runs as a foreground process managed by systemd. The actual Pub/Sub
subscriber, per-account sync, and watch renewal are not yet implemented --
this module provides the lifecycle shell that future pipeline code plugs into.

Usage::

    mailpilot run          # blocks until SIGTERM/SIGINT
    systemctl start mailpilot
    systemctl stop mailpilot   # sends SIGTERM -> graceful shutdown
"""

from __future__ import annotations

import os
import signal
import threading
from typing import Any

import click
import logfire
import psycopg

from mailpilot.database import (
    delete_sync_status,
    get_sync_status,
    update_sync_heartbeat,
    upsert_sync_status,
)

_HEARTBEAT_INTERVAL = 30  # seconds


def is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists but we can't signal it
    return True


def start_sync_loop(connection: psycopg.Connection[dict[str, Any]]) -> None:
    """Run the sync loop (blocks until SIGTERM/SIGINT).

    Lifecycle:
    1. Check for stale sync_status row (dead PID) and overwrite
    2. Register current PID in sync_status
    3. Register SIGTERM/SIGINT handlers
    4. Loop: heartbeat every 30s, check shutdown event
    5. On shutdown: delete sync_status row

    Args:
        connection: Open database connection.
    """
    pid = os.getpid()
    shutdown_event = threading.Event()

    # Check for stale sync_status from a crashed process.
    existing = get_sync_status(connection)
    if existing is not None and is_pid_alive(existing.pid):
        logfire.warn(
            "sync.loop.already_running",
            pid=pid,
            existing_pid=existing.pid,
        )
        raise SystemExit(
            f"sync loop already running (pid {existing.pid}) -- "
            "stop it first or check with 'mailpilot status'"
        )
    if existing is not None:
        click.echo(f"Removing stale sync status (pid {existing.pid} is dead)")

    # Register this process.
    upsert_sync_status(connection, pid)
    logfire.info("sync.loop.start", pid=pid)
    click.echo(f"Sync loop started (pid {pid})")
    click.echo(f"Heartbeat interval: {_HEARTBEAT_INTERVAL}s")
    click.echo("Press Ctrl+C or send SIGTERM to stop")

    # Signal handlers set the shutdown event.
    def _handle_shutdown(signum: int, frame: object) -> None:
        signal_name = signal.Signals(signum).name
        logfire.info("sync.shutdown.signal_received", pid=pid, signal=signum)
        click.echo(f"\nReceived {signal_name}, shutting down...")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    # Main loop: heartbeat until shutdown.
    try:
        while not shutdown_event.is_set():
            shutdown_event.wait(timeout=_HEARTBEAT_INTERVAL)
            if not shutdown_event.is_set():
                update_sync_heartbeat(connection)
                logfire.debug("sync.loop.heartbeat", pid=pid)
    finally:
        delete_sync_status(connection)
        logfire.info("sync.loop.stop", pid=pid)
        click.echo("Sync loop stopped")
