"""Shared test fixtures."""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import logfire
import psycopg
import pytest
from psycopg.rows import dict_row

from mailpilot.database import (
    create_account,
    create_activity,
    create_company,
    create_contact,
    create_workflow,
    initialize_database,
)
from mailpilot.models import Account, Activity, Company, Contact, Workflow
from mailpilot.settings import Settings

FIXTURES_DIR = Path(__file__).parent / "fixtures"
TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://localhost/mailpilot_test"
)


def make_test_settings(**overrides: Any) -> Settings:
    """Create a Settings instance with test defaults."""
    return Settings(
        database_url=TEST_DATABASE_URL,  # pyright: ignore[reportArgumentType]
        **overrides,
    )


@pytest.fixture(autouse=True)
def _disable_logfire_export(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    real_configure = logfire.configure

    def no_cloud(*args: object, **kwargs: object) -> object:
        kwargs["send_to_logfire"] = False
        return real_configure(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(logfire, "configure", no_cloud)


@pytest.fixture(scope="session", autouse=True)
def _apply_schema() -> None:  # pyright: ignore[reportUnusedFunction]
    """Apply schema once per test session using a dedicated connection."""
    conn = initialize_database(TEST_DATABASE_URL)
    conn.close()


@pytest.fixture
def database_connection() -> Iterator[psycopg.Connection[dict[str, Any]]]:
    """Yield a fresh connection with all tables truncated before the test."""
    conn = cast(
        psycopg.Connection[dict[str, Any]],
        psycopg.connect(TEST_DATABASE_URL, row_factory=dict_row),  # type: ignore[arg-type]
    )
    conn.execute(
        "TRUNCATE TABLE activity, sync_status, task, email, workflow_contact, "
        "workflow, contact, company, account CASCADE"
    )
    conn.commit()
    yield conn
    conn.close()


def load_fixture(name: str) -> dict[str, object]:
    """Load a JSON fixture file by name."""
    fixture_path = FIXTURES_DIR / name
    with open(fixture_path) as f:
        data: dict[str, object] = json.load(f)
    return data


# -- Entity factories ----------------------------------------------------------


def make_test_account(
    connection: psycopg.Connection[dict[str, Any]],
    email: str = "test@example.com",
    display_name: str = "Test Account",
) -> Account:
    """Create a test account in the database."""
    return create_account(connection, email=email, display_name=display_name)


def make_test_company(
    connection: psycopg.Connection[dict[str, Any]],
    name: str = "Test Corp",
    domain: str = "testcorp.com",
) -> Company:
    """Create a test company in the database."""
    return create_company(connection, name=name, domain=domain)


def make_test_contact(
    connection: psycopg.Connection[dict[str, Any]],
    email: str = "contact@testcorp.com",
    domain: str = "testcorp.com",
    company_id: str | None = None,
) -> Contact:
    """Create a test contact in the database."""
    return create_contact(connection, email=email, domain=domain, company_id=company_id)


def make_test_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    name: str = "Test Workflow",
    workflow_type: str = "outbound",
) -> Workflow:
    """Create a test workflow in the database."""
    return create_workflow(
        connection,
        name=name,
        workflow_type=workflow_type,
        account_id=account_id,
    )


def make_test_activity(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
    activity_type: str = "email_sent",
    summary: str = "Test activity",
    detail: dict[str, object] | None = None,
    company_id: str | None = None,
) -> Activity:
    """Create a test activity in the database."""
    return create_activity(
        connection,
        contact_id=contact_id,
        activity_type=activity_type,
        summary=summary,
        detail=detail or {},
        company_id=company_id,
    )
