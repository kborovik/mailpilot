"""Schema CHECK tests for email.route_method per §V.66.

Covers:
- enum CHECK admits every value listed in §I email projection.
- NULL admitted regardless of is_routed (outbound / bounce / pre-route).
- Illegal route_method string raises CheckViolation.
- Coupling CHECK rejects (route_method IS NOT NULL AND is_routed = FALSE).
"""

from typing import Any

import psycopg
import pytest

from conftest import make_test_account
from mailpilot.database import create_email

LEGAL_ROUTE_METHODS = [
    "thread_match",
    "rfc_message_id_match",
    "classified",
    "skipped_outside_window",
    "skipped_no_workflows",
    "skipped_predates_workflows",
    "skipped_no_inbound_workflows",
]


@pytest.mark.parametrize("method", LEGAL_ROUTE_METHODS)
def test_legal_route_method_value_admitted(
    database_connection: psycopg.Connection[dict[str, Any]],
    method: str,
) -> None:
    """Every §I enum value passes the route_method CHECK when is_routed=TRUE."""
    account = make_test_account(database_connection)
    email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        gmail_message_id=f"msg-{method}",
        is_routed=True,
        route_method=method,
    )
    assert email is not None
    assert email.route_method == method


def test_null_route_method_admitted_when_not_routed(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Pre-route / outbound / bounce row: route_method NULL ∧ is_routed FALSE."""
    account = make_test_account(database_connection)
    email = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        gmail_message_id="msg-outbound-null",
        is_routed=False,
        route_method=None,
    )
    assert email is not None
    assert email.route_method is None
    assert email.is_routed is False


def test_null_route_method_admitted_when_routed(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Outbound-as-origin: is_routed=TRUE w/o enum value satisfies coupling CHECK."""
    account = make_test_account(database_connection)
    email = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        gmail_message_id="msg-outbound-routed-null",
        is_routed=True,
        route_method=None,
    )
    assert email is not None
    assert email.route_method is None
    assert email.is_routed is True


def test_illegal_route_method_string_raises(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Stale / typo'd enum value rejected by CHECK."""
    account = make_test_account(database_connection)
    with pytest.raises(psycopg.errors.CheckViolation):
        create_email(
            database_connection,
            account_id=account.id,
            direction="inbound",
            gmail_message_id="msg-illegal",
            is_routed=True,
            route_method="bogus_method",
        )
    database_connection.rollback()


def test_route_method_set_with_is_routed_false_raises(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Coupling CHECK rejects recorded enum value without recorded routing pass."""
    account = make_test_account(database_connection)
    with pytest.raises(psycopg.errors.CheckViolation):
        create_email(
            database_connection,
            account_id=account.id,
            direction="inbound",
            gmail_message_id="msg-coupling",
            is_routed=False,
            route_method="classified",
        )
    database_connection.rollback()
