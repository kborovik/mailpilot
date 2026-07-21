"""Tests for `get_status_payload` (§V.11) and `_scrub_database_url`.

Covers the nine cases enumerated in §T.59:
    (i)   fresh DB → empty accounts, zero task aggregates
    (ii)  last_synced_at = now() - 5min → last_synced_age_seconds ∈ [298, 302]
    (iii) disabled_reason set → disabled: true
    (iv)  pending past → oldest_pending_age_seconds non-null
    (v)   pending future → counted in scheduled_future, not oldest
    (vi)  failed completed_at = now() - 23h → failed_24h: 1; aged to 25h → 0
    (vii) anthropic_api_key set/unset → *_set: bool
    (viii) credentialed database_url scrubbed to scheme://host[:port]/db
    (ix)  pre-seeded stale hash → schema.drift: true ∧ version pinned
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from conftest import make_test_account, make_test_contact, make_test_settings
from mailpilot import database as db_mod
from mailpilot.database import (
    _MAILPILOT_VERSION,  # pyright: ignore[reportPrivateUsage]
    _scrub_database_url,  # pyright: ignore[reportPrivateUsage]
    create_task,
    get_status_payload,
    update_account,
)
from mailpilot.models import SchemaMetadata

# -- (i) fresh DB --------------------------------------------------------------


def test_fresh_db_has_empty_accounts_and_zero_task_aggregates(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    payload = get_status_payload(database_connection, make_test_settings())

    assert payload["accounts"] == []

    tasks = payload["tasks"]
    assert isinstance(tasks, dict)
    assert tasks["pending"] == 0
    assert tasks["failed_24h"] == 0
    assert tasks["scheduled_future"] == 0
    assert tasks["oldest_pending_age_seconds"] is None
    assert tasks["max_attempt_count_pending"] is None

    assert payload["sync_loop"] is None
    assert payload["version"] == _MAILPILOT_VERSION


# -- (ii) last_synced_at age ---------------------------------------------------


def test_account_last_synced_five_minutes_ago_yields_expected_age_seconds(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection, email="sync@example.com")
    database_connection.execute(
        "UPDATE account SET last_synced_at = now() - interval '5 minutes' "
        "WHERE id = %s",
        (account.id,),
    )
    database_connection.commit()

    payload = get_status_payload(database_connection, make_test_settings())
    accounts = payload["accounts"]
    assert isinstance(accounts, list)
    assert len(accounts) == 1
    age = accounts[0]["last_synced_age_seconds"]
    assert isinstance(age, int)
    assert 298 <= age <= 302


# -- (iii) disabled flag mirrors disabled_reason -------------------------------


def test_account_disabled_reason_flips_disabled_flag(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection, email="dis@example.com")
    update_account(
        database_connection,
        account.id,
        disabled_reason="oauth_revoked",
    )

    payload = get_status_payload(database_connection, make_test_settings())
    accounts = payload["accounts"]
    assert isinstance(accounts, list)
    assert len(accounts) == 1
    assert accounts[0]["disabled"] is True
    assert accounts[0]["email"] == "dis@example.com"


def test_account_without_disabled_reason_reports_disabled_false(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    make_test_account(database_connection, email="active@example.com")
    payload = get_status_payload(database_connection, make_test_settings())
    accounts = payload["accounts"]
    assert isinstance(accounts, list)
    assert accounts[0]["disabled"] is False


# -- (iv) oldest_pending_age_seconds for due-now tasks -------------------------


def _make_pending_task(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    scheduled_at: datetime,
) -> str:
    """Insert a pending task and return its id. Account/workflow/contact wired."""
    from conftest import make_test_enrollment, make_test_workflow

    account = make_test_account(connection, email="acct@example.com")
    contact = make_test_contact(connection, email="contact@example.com")
    workflow = make_test_workflow(connection, account_id=account.id)
    enrollment = make_test_enrollment(connection, workflow.id, contact.id)
    task = create_task(
        connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="t",
        scheduled_at=scheduled_at,  # pyright: ignore[reportArgumentType]
    )
    return task.id


def test_pending_past_yields_non_null_oldest_pending_age(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    _make_pending_task(
        database_connection,
        scheduled_at=datetime.now(UTC) - timedelta(seconds=120),
    )

    payload = get_status_payload(database_connection, make_test_settings())
    tasks = payload["tasks"]
    assert isinstance(tasks, dict)
    age = tasks["oldest_pending_age_seconds"]
    assert isinstance(age, int)
    assert age >= 119
    assert tasks["pending"] == 1
    assert tasks["scheduled_future"] == 0


# -- (v) future-scheduled pending split ----------------------------------------


def test_pending_future_counted_in_scheduled_future_not_oldest(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    _make_pending_task(
        database_connection,
        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
    )

    payload = get_status_payload(database_connection, make_test_settings())
    tasks = payload["tasks"]
    assert isinstance(tasks, dict)
    assert tasks["pending"] == 1
    assert tasks["scheduled_future"] == 1
    assert tasks["oldest_pending_age_seconds"] is None


# -- (vi) failed_24h sliding window --------------------------------------------


def test_failed_task_aged_23_hours_counted_in_failed_24h(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    task_id = _make_pending_task(
        database_connection,
        scheduled_at=datetime.now(UTC) - timedelta(hours=23),
    )
    database_connection.execute(
        "UPDATE task SET status = 'failed', completed_at = now() - interval '23 hours' "
        "WHERE id = %s",
        (task_id,),
    )
    database_connection.commit()

    payload = get_status_payload(database_connection, make_test_settings())
    tasks = payload["tasks"]
    assert isinstance(tasks, dict)
    assert tasks["failed_24h"] == 1


def test_failed_task_aged_25_hours_dropped_from_failed_24h(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    task_id = _make_pending_task(
        database_connection,
        scheduled_at=datetime.now(UTC) - timedelta(hours=25),
    )
    database_connection.execute(
        "UPDATE task SET status = 'failed', completed_at = now() - interval '25 hours' "
        "WHERE id = %s",
        (task_id,),
    )
    database_connection.commit()

    payload = get_status_payload(database_connection, make_test_settings())
    tasks = payload["tasks"]
    assert isinstance(tasks, dict)
    assert tasks["failed_24h"] == 0


# -- (vii) anthropic_api_key surfaced as boolean only --------------------------


def test_anthropic_api_key_unset_reports_false(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    payload = get_status_payload(
        database_connection, make_test_settings(anthropic_api_key="")
    )
    config = payload["config"]
    assert isinstance(config, dict)
    assert config["anthropic_api_key_set"] is False


def test_anthropic_api_key_set_reports_true_and_no_value_leak(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    secret = "sk-ant-XYZ-do-not-leak"
    payload = get_status_payload(
        database_connection, make_test_settings(anthropic_api_key=secret)
    )
    config = payload["config"]
    assert isinstance(config, dict)
    assert config["anthropic_api_key_set"] is True
    assert secret not in str(payload)


def test_xai_api_key_set_reports_true_and_no_value_leak(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    secret = "xai-XYZ-do-not-leak"
    payload = get_status_payload(
        database_connection, make_test_settings(xai_api_key=secret)
    )
    config = payload["config"]
    assert isinstance(config, dict)
    assert config["xai_api_key_set"] is True
    assert config["llm_provider"] == "xai"
    assert config["xai_model"] == "grok-4.5"
    assert secret not in str(payload)


# -- (viii) database_url scrubber ---------------------------------------------


def test_scrub_database_url_strips_userinfo():
    assert (
        _scrub_database_url("postgresql://user:pass@host:5432/db")
        == "postgresql://host:5432/db"
    )


def test_scrub_database_url_round_trips_passwordless():
    assert (
        _scrub_database_url("postgresql://localhost/mailpilot")
        == "postgresql://localhost/mailpilot"
    )


def test_status_payload_emits_scrubbed_database_url(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.settings import Settings

    settings = Settings(database_url="postgresql://u:p@host:5432/db")  # pyright: ignore[reportArgumentType]
    payload = get_status_payload(database_connection, settings)
    config = payload["config"]
    assert isinstance(config, dict)
    assert config["database_url"] == "postgresql://host:5432/db"
    assert "u:p" not in str(payload)


# -- (ix) drift + pinned top-level version ------------------------------------


def test_pre_seeded_stale_hash_surfaces_drift_and_pins_version(
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
):
    stale = SchemaMetadata(
        mailpilot_version="0.0.0",
        schema_hash="cafef00d" * 8,
        applied_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    monkeypatch.setattr(db_mod, "_read_schema_metadata", lambda _conn: stale)
    # No migration explains the divergence → verdict=drift (not pending).
    monkeypatch.setattr(db_mod, "_discover_migrations", list)

    payload = get_status_payload(database_connection, make_test_settings())
    schema = payload["schema"]
    assert isinstance(schema, dict)
    assert schema["verdict"] == "drift"
    assert schema["recorded_hash"] == "cafef00d" * 8
    assert payload["version"] == _MAILPILOT_VERSION
