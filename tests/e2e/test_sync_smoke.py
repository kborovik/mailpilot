"""Smoke tests for the inbound sync pipeline against the live Gmail API.

E2E tests are opt-in: ``pytest`` skips them by default via
``addopts = -m 'not e2e'``. Invoke with ``make e2e`` or
``uv run pytest -m e2e tests/e2e/``.

Prerequisites:

- ``mailpilot config set google_application_credentials /path/to/key.json``
  (or ``GOOGLE_APPLICATION_CREDENTIALS`` env var)
- ``createdb mailpilot_e2e`` (the Makefile target creates it if missing)
- Service account delegated to ``inbound@lab5.ca``
"""

import os
from collections.abc import Iterator
from typing import Any, cast

import psycopg
import pytest
from psycopg.rows import dict_row

from mailpilot.database import create_account, initialize_database, list_accounts
from mailpilot.gmail import GmailClient
from mailpilot.models import Account
from mailpilot.settings import Settings, get_settings
from mailpilot.sync import sync_account

pytestmark = pytest.mark.e2e

E2E_DATABASE_URL = os.environ.get(
    "E2E_DATABASE_URL", "postgresql://localhost/mailpilot_e2e"
)
INBOUND_EMAIL = "inbound@lab5.ca"


def _require_service_account_credentials() -> None:
    path = get_settings().google_application_credentials or os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS", ""
    )
    if not path or not os.path.isfile(path):
        pytest.skip(
            "Service account credentials not configured -- set via "
            "'mailpilot config set google_application_credentials ...' "
            "or GOOGLE_APPLICATION_CREDENTIALS"
        )


@pytest.fixture(scope="module")
def e2e_settings() -> Settings:
    _require_service_account_credentials()
    return get_settings()


@pytest.fixture
def e2e_database_connection(
    e2e_settings: Settings,
) -> Iterator[psycopg.Connection[dict[str, Any]]]:
    """Yield a connection to ``mailpilot_e2e``. Preserves state across runs."""
    del e2e_settings  # force skip-on-missing-creds first
    try:
        initialize_database(E2E_DATABASE_URL).close()
    except SystemExit as exc:
        pytest.skip(f"e2e database unreachable: {exc}")
    conn = cast(
        psycopg.Connection[dict[str, Any]],
        psycopg.connect(E2E_DATABASE_URL, row_factory=dict_row),  # type: ignore[arg-type]
    )
    yield conn
    conn.close()


@pytest.fixture
def e2e_inbound_account(
    e2e_database_connection: psycopg.Connection[dict[str, Any]],
) -> Account:
    for account in list_accounts(e2e_database_connection):
        if account.email == INBOUND_EMAIL:
            return account
    return create_account(
        e2e_database_connection, email=INBOUND_EMAIL, display_name="E2E Inbound"
    )


@pytest.fixture
def e2e_inbound_gmail_client(e2e_settings: Settings) -> GmailClient:
    del e2e_settings
    return GmailClient(INBOUND_EMAIL)


def test_gmail_profile_reachable(e2e_inbound_gmail_client: GmailClient) -> None:
    """Service account delegation + Gmail API connectivity for inbound@lab5.ca."""
    profile = e2e_inbound_gmail_client.get_profile()
    assert profile.get("emailAddress") == INBOUND_EMAIL
    assert profile.get("historyId")


def test_sync_account_inbound_does_not_raise(
    e2e_database_connection: psycopg.Connection[dict[str, Any]],
    e2e_inbound_account: Account,
    e2e_inbound_gmail_client: GmailClient,
    e2e_settings: Settings,
) -> None:
    """Full sync cycle against the live inbound mailbox: completes without error.

    Regression guard for the pipeline end to end -- Gmail API auth,
    history/full-sync dispatch, message fetch, and DB insertion. Does
    not assert a specific row count because the inbox is live.
    """
    stored = sync_account(
        e2e_database_connection,
        e2e_inbound_account,
        e2e_inbound_gmail_client,
        e2e_settings,
    )
    assert stored >= 0
