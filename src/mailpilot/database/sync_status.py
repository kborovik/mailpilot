"""Sync-loop heartbeat row."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import Any

import psycopg

from mailpilot.models import (
    SyncStatus,
)

# -- Sync Status ---------------------------------------------------------------


def upsert_sync_status(
    connection: psycopg.Connection[dict[str, Any]],
    pid: int,
) -> SyncStatus:
    """Insert or update the singleton sync status row.

    Args:
        connection: Open database connection.
        pid: Process ID of the running sync loop.

    Returns:
        Current sync status.
    """
    row = connection.execute(
        """\
        INSERT INTO sync_status (id, pid)
        VALUES ('singleton', %(pid)s)
        ON CONFLICT (id) DO UPDATE
            SET pid = %(pid)s,
                started_at = CURRENT_TIMESTAMP,
                heartbeat_at = CURRENT_TIMESTAMP
        RETURNING *
        """,
        {"pid": pid},
    ).fetchone()
    connection.commit()
    return SyncStatus.model_validate(row)


def get_sync_status(
    connection: psycopg.Connection[dict[str, Any]],
) -> SyncStatus | None:
    """Get the current sync status.

    Args:
        connection: Open database connection.

    Returns:
        SyncStatus if sync is registered, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM sync_status WHERE id = 'singleton'"
    ).fetchone()
    if row is None:
        return None
    return SyncStatus.model_validate(row)


def delete_sync_status(
    connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Delete the sync status row (called on clean shutdown).

    Args:
        connection: Open database connection.
    """
    connection.execute("DELETE FROM sync_status WHERE id = 'singleton'")
    connection.commit()


def update_sync_heartbeat(
    connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Update the heartbeat timestamp to signal liveness.

    Args:
        connection: Open database connection.
    """
    connection.execute(
        """\
        UPDATE sync_status
        SET heartbeat_at = CURRENT_TIMESTAMP
        WHERE id = 'singleton'
        """
    )
    connection.commit()
