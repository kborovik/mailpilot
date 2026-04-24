"""Tests for workflow theme support."""

from typing import Any

import psycopg

from conftest import make_test_account
from mailpilot.database import create_workflow, get_workflow, update_workflow


def test_create_workflow_default_theme(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    workflow = create_workflow(
        database_connection,
        name="Test",
        workflow_type="outbound",
        account_id=account.id,
    )
    assert workflow.theme == "blue"


def test_create_workflow_custom_theme(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    workflow = create_workflow(
        database_connection,
        name="Themed",
        workflow_type="outbound",
        account_id=account.id,
        theme="green",
    )
    assert workflow.theme == "green"


def test_update_workflow_theme(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    workflow = create_workflow(
        database_connection, name="W", workflow_type="outbound", account_id=account.id
    )
    updated = update_workflow(database_connection, workflow.id, theme="orange")
    assert updated is not None
    assert updated.theme == "orange"


def test_get_workflow_includes_theme(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    created = create_workflow(
        database_connection,
        name="Get",
        workflow_type="inbound",
        account_id=account.id,
        theme="purple",
    )
    fetched = get_workflow(database_connection, created.id)
    assert fetched is not None
    assert fetched.theme == "purple"
