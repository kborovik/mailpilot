"""Status payload assembly."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import urllib.parse
from typing import Any

import logfire
import psycopg

from mailpilot.database._common import (
    _MAILPILOT_VERSION,
)
from mailpilot.settings import SECRET_KEYS, Settings

# -- Status --------------------------------------------------------------------


def _scrub_database_url(url: str) -> str:
    """Reduce a PostgreSQL URL to ``scheme://host[:port]/db``, dropping creds.

    Uses ``urllib.parse.urlsplit`` so a passwordless URL round-trips unchanged
    and a credentialed URL loses its userinfo netloc segment. Path is preserved
    verbatim (the leading ``/`` is part of urlsplit's ``path``).
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port is not None else host
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _schema_block(
    connection: psycopg.Connection[dict[str, Any]],
) -> dict[str, object]:
    """Return the `schema` block per §V.11: three-state verdict + facts.

    Carries ``verdict`` in {current, pending, drift} (not a bare drift bool,
    §V.109), the recorded vs canonical schema hashes, and applied/pending
    migration counts. Read-only diagnosis: reports whatever the verdict is,
    never dead-stops (the gate lives on the write-path per §V.18).
    """
    from mailpilot import database as db

    status = db.determine_schema_verdict(connection)
    return {
        "verdict": status.verdict,
        "recorded_hash": status.recorded_hash,
        "current_hash": status.current_hash,
        "applied": status.applied,
        "pending": status.pending,
    }


def _sync_loop_block(
    connection: psycopg.Connection[dict[str, Any]],
) -> dict[str, object] | None:
    """Return the `sync_loop` block per §V.11, or None when not running."""
    row = connection.execute(
        """\
        SELECT
            pid,
            started_at,
            heartbeat_at,
            EXTRACT(EPOCH FROM (now() - heartbeat_at))::int AS heartbeat_age_seconds
        FROM sync_status WHERE id = 'singleton'
        """
    ).fetchone()
    if row is None:
        return None
    return {
        "running": True,
        "pid": row["pid"],  # type: ignore[index]
        "started_at": row["started_at"].isoformat(),  # type: ignore[index]
        "heartbeat_at": row["heartbeat_at"].isoformat(),  # type: ignore[index]
        "heartbeat_age_seconds": row["heartbeat_age_seconds"],  # type: ignore[index]
    }


def _accounts_block(
    connection: psycopg.Connection[dict[str, Any]],
) -> list[dict[str, object]]:
    """Return per-account status rows per §V.11.

    Server-computed ages (``last_synced_age_seconds``, ``watch_expires_in_hours``)
    avoid Python clock skew; null inputs → null ages.
    """
    rows = connection.execute(
        """\
        SELECT
            id,
            email,
            (disabled_reason IS NOT NULL) AS disabled,
            gmail_history_id,
            watch_expiration,
            last_synced_at,
            EXTRACT(EPOCH FROM (now() - last_synced_at))::int
                AS last_synced_age_seconds,
            (EXTRACT(EPOCH FROM (watch_expiration - now()))::int / 3600)
                AS watch_expires_in_hours
        FROM account
        ORDER BY email
        """
    ).fetchall()
    accounts: list[dict[str, object]] = []
    for row in rows:
        watch_expiration = row["watch_expiration"]
        last_synced_at = row["last_synced_at"]
        accounts.append(
            {
                "id": row["id"],
                "email": row["email"],
                "disabled": row["disabled"],
                "last_synced_at": (
                    last_synced_at.isoformat() if last_synced_at is not None else None
                ),
                "last_synced_age_seconds": row["last_synced_age_seconds"],
                "gmail_history_id": row["gmail_history_id"],
                "watch_expiration": (
                    watch_expiration.isoformat()
                    if watch_expiration is not None
                    else None
                ),
                "watch_expires_in_hours": row["watch_expires_in_hours"],
            }
        )
    return accounts


def _tasks_block(
    connection: psycopg.Connection[dict[str, Any]],
) -> dict[str, object]:
    """Return task-queue aggregates per §V.11.

    Single SQL statement: pending counts split by due-vs-future, oldest
    pending age (due-only), max attempt_count among pending, and failed_24h.
    """
    row = connection.execute(
        """\
        SELECT
            (SELECT count(*) FROM task WHERE status = 'pending') AS pending,
            (SELECT count(*) FROM task
             WHERE status = 'pending' AND scheduled_at > now())
                AS scheduled_future,
            (SELECT EXTRACT(EPOCH FROM (now() - min(scheduled_at)))::int
             FROM task
             WHERE status = 'pending' AND scheduled_at <= now())
                AS oldest_pending_age_seconds,
            (SELECT max(attempt_count) FROM task WHERE status = 'pending')
                AS max_attempt_count_pending,
            (SELECT count(*) FROM task
             WHERE status = 'failed'
               AND completed_at >= now() - interval '24 hours')
                AS failed_24h
        """
    ).fetchone()
    return {
        "pending": row["pending"],  # type: ignore[index]
        "failed_24h": row["failed_24h"],  # type: ignore[index]
        "scheduled_future": row["scheduled_future"],  # type: ignore[index]
        "oldest_pending_age_seconds": row["oldest_pending_age_seconds"],  # type: ignore[index]
        "max_attempt_count_pending": row["max_attempt_count_pending"],  # type: ignore[index]
    }


def _counts_block(
    connection: psycopg.Connection[dict[str, Any]],
) -> dict[str, object]:
    """Return entity counts (sanity tail per §V.11). ``accounts`` excluded."""
    row = connection.execute(
        """\
        SELECT
            (SELECT COUNT(*) FROM company) AS companies,
            (SELECT COUNT(*) FROM contact) AS contacts,
            (SELECT COUNT(*) FROM workflow) AS workflows,
            (SELECT COUNT(*) FROM email) AS emails,
            (SELECT COUNT(*) FROM activity) AS activities,
            (SELECT COUNT(*) FROM tag) AS tags,
            (SELECT COUNT(*) FROM note) AS notes
        """
    ).fetchone()
    return {
        "companies": row["companies"],  # type: ignore[index]
        "contacts": row["contacts"],  # type: ignore[index]
        "workflows": row["workflows"],  # type: ignore[index]
        "emails": row["emails"],  # type: ignore[index]
        "activities": row["activities"],  # type: ignore[index]
        "tags": row["tags"],  # type: ignore[index]
        "notes": row["notes"],  # type: ignore[index]
    }


def _config_block(settings: Settings) -> dict[str, object]:
    """Return the `config` block per §V.11.

    Secret keys collapsed to ``*_set: bool`` so values never reach agent
    transcripts or Logfire spans. ``database_url`` is scrubbed of userinfo
    rather than dropped entirely so operators can still see host/db/port.
    """
    assert "anthropic_api_key" in SECRET_KEYS
    assert "xai_api_key" in SECRET_KEYS
    assert "logfire_token" in SECRET_KEYS
    assert "database_url" in SECRET_KEYS
    assert "google_application_credentials" in SECRET_KEYS
    return {
        "llm_provider": settings.llm_provider,
        "anthropic_api_key_set": bool(settings.anthropic_api_key),
        "anthropic_model": settings.anthropic_model,
        "xai_api_key_set": bool(settings.xai_api_key),
        "xai_model": settings.xai_model,
        "environment": settings.environment,
        "logfire_token_set": bool(settings.logfire_token),
        "google_pubsub_topic": settings.google_pubsub_topic,
        "google_pubsub_subscription": settings.google_pubsub_subscription,
        "database_url": _scrub_database_url(str(settings.database_url)),
    }


def get_status_payload(
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
) -> dict[str, object]:
    """Build the full ``mailpilot status`` payload per §V.11.

    Top-level blocks (``version``, ``schema``, ``sync_loop``, ``accounts``,
    ``tasks``, ``config``, ``counts``) are layout-stable for LLM-agent
    troubleshooting; secrets are collapsed to booleans and the database URL
    is scrubbed (§V.11).

    Args:
        connection: Open database connection.
        settings: Loaded settings (callers pass ``get_settings()``).

    Returns:
        Dict matching the §V.11 envelope, ready to wrap as
        ``{"status": <payload>, "ok": true}``.
    """
    with logfire.span("db.status.payload"):
        return {
            "version": _MAILPILOT_VERSION,
            "schema": _schema_block(connection),
            "sync_loop": _sync_loop_block(connection),
            "accounts": _accounts_block(connection),
            "tasks": _tasks_block(connection),
            "config": _config_block(settings),
            "counts": _counts_block(connection),
        }
