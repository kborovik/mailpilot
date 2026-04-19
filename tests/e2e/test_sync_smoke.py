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

from mailpilot.database import (
    create_account,
    get_email,
    initialize_database,
    list_accounts,
)
from mailpilot.gmail import GmailClient
from mailpilot.models import Account
from mailpilot.settings import Settings, get_settings
from mailpilot.sync import send_email, sync_account

pytestmark = pytest.mark.e2e

E2E_DATABASE_URL = os.environ.get(
    "E2E_DATABASE_URL", "postgresql://localhost/mailpilot_e2e"
)
INBOUND_EMAIL = "inbound@lab5.ca"
OUTBOUND_EMAIL = "outbound@lab5.ca"


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


@pytest.fixture
def e2e_outbound_account(
    e2e_database_connection: psycopg.Connection[dict[str, Any]],
) -> Account:
    for account in list_accounts(e2e_database_connection):
        if account.email == OUTBOUND_EMAIL:
            return account
    return create_account(
        e2e_database_connection, email=OUTBOUND_EMAIL, display_name="E2E Outbound"
    )


def test_send_email_delivers_and_records_row(
    e2e_database_connection: psycopg.Connection[dict[str, Any]],
    e2e_outbound_account: Account,
    e2e_settings: Settings,
) -> None:
    """Send a real email from outbound@lab5.ca to inbound@lab5.ca.

    Covers the full outbound path: service account delegation, Gmail
    ``users.messages.send``, and the outbound DB insert with
    ``sent_at``. Does not verify inbound delivery -- that is the
    sync test's job.
    """
    client = GmailClient(OUTBOUND_EMAIL)
    email = send_email(
        e2e_database_connection,
        account=e2e_outbound_account,
        gmail_client=client,
        settings=e2e_settings,
        to=INBOUND_EMAIL,
        subject="mailpilot e2e smoke: send_email",
        body="This message was sent by the mailpilot e2e smoke test.",
    )
    assert email.direction == "outbound"
    assert email.status == "sent"
    assert email.gmail_message_id
    assert email.sent_at is not None
    # The row must be queryable after commit.
    stored = get_email(e2e_database_connection, email.id)
    assert stored is not None
    assert stored.gmail_message_id == email.gmail_message_id
