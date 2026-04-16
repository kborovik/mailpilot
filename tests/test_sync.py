"""Tests for sync status database operations and stale detection."""

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from mailpilot.database import (
    delete_sync_status,
    get_sync_status,
    update_sync_heartbeat,
    upsert_sync_status,
)
from mailpilot.sync import is_pid_alive


def test_upsert_and_get_sync_status(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    pid = os.getpid()
    status = upsert_sync_status(database_connection, pid)
    assert status.pid == pid
    assert status.id == "singleton"

    fetched = get_sync_status(database_connection)
    assert fetched is not None
    assert fetched.pid == pid


def test_upsert_overwrites_existing(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    upsert_sync_status(database_connection, 1111)
    updated = upsert_sync_status(database_connection, 2222)
    assert updated.pid == 2222

    fetched = get_sync_status(database_connection)
    assert fetched is not None
    assert fetched.pid == 2222


def test_delete_sync_status(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    upsert_sync_status(database_connection, os.getpid())
    delete_sync_status(database_connection)
    assert get_sync_status(database_connection) is None


def test_update_heartbeat(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    status = upsert_sync_status(database_connection, os.getpid())
    original_heartbeat = status.heartbeat_at

    update_sync_heartbeat(database_connection)

    fetched = get_sync_status(database_connection)
    assert fetched is not None
    assert fetched.heartbeat_at >= original_heartbeat


def test_get_sync_status_when_empty(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    assert get_sync_status(database_connection) is None


def testis_pid_alive_current_process():
    assert is_pid_alive(os.getpid()) is True


def testis_pid_alive_dead_process():
    # PID 99999999 is almost certainly not running.
    assert is_pid_alive(99999999) is False


def test_heartbeat_staleness(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Verify heartbeat_at can be compared for staleness detection."""
    status = upsert_sync_status(database_connection, os.getpid())
    stale_threshold = datetime.now(tz=UTC) - timedelta(minutes=2)
    assert status.heartbeat_at > stale_threshold
