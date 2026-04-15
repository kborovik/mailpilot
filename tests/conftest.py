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

from mailpilot.database import initialize_database
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
        "TRUNCATE TABLE email, workflow_contact, workflow, contact, company, account CASCADE"
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
