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
    assign_tag_to_company,
    assign_tag_to_contact,
    create_account,
    create_activity,
    create_company,
    create_contact,
    create_enrollment,
    create_note,
    create_tag,
    create_workflow,
    get_tag_by_name,
    initialize_database,
)
from mailpilot.models import (
    Account,
    Activity,
    Company,
    Contact,
    Enrollment,
    Note,
    Tag,
    TagAssignment,
    Workflow,
)
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
def _isolate_config_file(  # pyright: ignore[reportUnusedFunction]
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point ``CONFIG_PATH`` at a nonexistent temp file.

    ``Settings`` reads ``~/.mailpilot/config.json`` through ``JsonConfigSource``.
    Without isolation the operator's real config (for example a Novita
    ``anthropic_base_url``) leaks into every test that constructs ``Settings``
    or calls ``make_test_settings``, overriding the field defaults under test.
    A nonexistent path makes ``JsonConfigSource`` return an empty dict, so tests
    see field defaults unless they override explicitly.
    """
    config_path = tmp_path_factory.mktemp("mailpilot_config") / "config.json"
    monkeypatch.setattr("mailpilot.settings.CONFIG_PATH", config_path)


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
        "TRUNCATE TABLE meeting_attendee, meeting, note, tag_assignment, tag, "
        "activity, sync_status, task, email, enrollment, workflow, contact, "
        "company, account CASCADE"
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
    """Create a test account in the database.

    Asserts the row was inserted (i.e. ``email`` did not already exist).
    Tests that intentionally probe the ON CONFLICT branch per §V.16(+)
    should call ``create_account`` directly.
    """
    account = create_account(connection, email=email, display_name=display_name)
    assert account is not None, f"account with email={email!r} already exists"
    return account


def make_test_company(
    connection: psycopg.Connection[dict[str, Any]],
    name: str = "Test Corp",
    domain: str = "testcorp.com",
) -> Company:
    """Create a test company in the database.

    Asserts the row was inserted (i.e. ``domain`` did not already exist).
    Tests that intentionally probe the ON CONFLICT branch per §V.16(+)
    should call ``create_company`` directly.
    """
    company = create_company(connection, name=name, domain=domain)
    assert company is not None, f"company with domain={domain!r} already exists"
    return company


def make_test_contact(
    connection: psycopg.Connection[dict[str, Any]],
    email: str = "contact@testcorp.com",
    company_id: str | None = None,
) -> Contact:
    """Create a test contact in the database.

    Asserts the row was inserted (i.e. ``email`` did not already exist).
    Tests that intentionally probe the ON CONFLICT branch per §V.16(+)
    should call ``create_contact`` directly.
    """
    contact = create_contact(connection, email=email, company_id=company_id)
    assert contact is not None, f"contact with email={email!r} already exists"
    return contact


def make_test_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    name: str = "Test Workflow",
    workflow_type: str = "outbound",
    template: str | None = None,
) -> Workflow:
    """Create a test workflow in the database.

    Tests may pass ``template`` directly. Legacy callers pass ``workflow_type``
    (``inbound`` / ``outbound``) which maps to the corresponding ``-general``
    template. Asserts the row was inserted; tests that intentionally probe
    the ON CONFLICT branch per §V.16(+) should call ``create_workflow``
    directly.
    """
    if template is None:
        template = (
            "inbound-general" if workflow_type == "inbound" else "outbound-general"
        )
    workflow = create_workflow(
        connection,
        name=name,
        template=template,
        account_id=account_id,
    )
    assert workflow is not None, (
        f"workflow with (account_id={account_id!r}, name={name!r}) already exists"
    )
    return workflow


def make_test_enrollment(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> Enrollment:
    """Create a test enrollment in the database.

    Asserts the row was inserted (i.e. ``(workflow_id, contact_id)`` did not
    already exist). Tests that intentionally probe the ON CONFLICT branch
    should call ``create_enrollment`` directly.
    """
    enrollment = create_enrollment(connection, workflow_id, contact_id)
    assert enrollment is not None, (
        f"enrollment already exists for ({workflow_id}, {contact_id})"
    )
    return enrollment


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


def make_test_tag(
    connection: psycopg.Connection[dict[str, Any]],
    name: str = "prospect",
) -> Tag:
    """Define a tag in the controlled vocabulary (§V.116)."""
    tag = create_tag(connection, name=name)
    assert tag is not None, f"tag '{name}' already exists"
    return tag


def make_test_tag_assignment(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str | None = None,
    company_id: str | None = None,
    name: str = "prospect",
) -> TagAssignment:
    """Define a vocabulary tag (if needed) and link it to one owner (§V.116)."""
    existing = get_tag_by_name(connection, name)
    tag = existing if existing is not None else make_test_tag(connection, name=name)
    if contact_id is not None:
        assignment = assign_tag_to_contact(
            connection, tag_id=tag.id, contact_id=contact_id
        )
    else:
        assert company_id is not None
        assignment = assign_tag_to_company(
            connection, tag_id=tag.id, company_id=company_id
        )
    assert assignment is not None, f"tag '{name}' already on owner"
    return assignment


def make_test_note(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str | None = None,
    company_id: str | None = None,
    body: str = "Test note body",
) -> Note:
    """Create a test note in the database."""
    return create_note(
        connection,
        body=body,
        contact_id=contact_id,
        company_id=company_id,
    )
