"""CLI tests for account subcommands."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from conftest import make_test_settings
from mailpilot.cli import main
from mailpilot.models import Account

_NOW = datetime(2024, 1, 1, tzinfo=UTC)


def _make_account(**overrides: Any) -> Account:
    defaults: dict[str, Any] = {
        "id": "01234567-0000-7000-0000-000000000001",
        "email": "test@example.com",
        "display_name": "Test Account",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Account(**{**defaults, **overrides})


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mock_connection() -> MagicMock:
    return MagicMock()


# -- account create ------------------------------------------------------------


def test_account_create(runner: CliRunner, mock_connection: MagicMock) -> None:
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_account", return_value=account) as mock_create,
    ):
        result = runner.invoke(
            main,
            [
                "account",
                "create",
                "--email",
                "test@example.com",
                "--display-name",
                "Test Account",
            ],
        )

    assert result.exit_code == 0
    mock_create.assert_called_once_with(
        mock_connection, email="test@example.com", display_name="Test Account"
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["email"] == "test@example.com"
    assert data["display_name"] == "Test Account"


def test_account_create_email_only(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    account = _make_account(display_name="")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_account", return_value=account) as mock_create,
    ):
        result = runner.invoke(
            main, ["account", "create", "--email", "test@example.com"]
        )

    assert result.exit_code == 0
    mock_create.assert_called_once_with(
        mock_connection, email="test@example.com", display_name=""
    )


# -- account list --------------------------------------------------------------


def test_account_list(runner: CliRunner, mock_connection: MagicMock) -> None:
    accounts = [
        _make_account(id="id-1", email="a@example.com"),
        _make_account(id="id-2", email="b@example.com"),
    ]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_accounts", return_value=accounts),
    ):
        result = runner.invoke(main, ["account", "list"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert len(data["accounts"]) == 2
    assert data["accounts"][0]["email"] == "a@example.com"
    assert data["accounts"][1]["email"] == "b@example.com"


def test_account_list_empty(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_accounts", return_value=[]),
    ):
        result = runner.invoke(main, ["account", "list"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["accounts"] == []


# -- account view --------------------------------------------------------------


def test_account_view(runner: CliRunner, mock_connection: MagicMock) -> None:
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account) as mock_get,
    ):
        result = runner.invoke(main, ["account", "view", account.id])

    assert result.exit_code == 0
    mock_get.assert_called_once_with(mock_connection, account.id)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["id"] == account.id


def test_account_view_not_found(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=None),
    ):
        result = runner.invoke(main, ["account", "view", "nonexistent-id"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "not_found"


# -- account update ------------------------------------------------------------


def test_account_update_display_name(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    updated = _make_account(display_name="New Name")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.update_account", return_value=updated) as mock_update,
    ):
        result = runner.invoke(
            main, ["account", "update", updated.id, "--display-name", "New Name"]
        )

    assert result.exit_code == 0
    mock_update.assert_called_once_with(
        mock_connection, updated.id, display_name="New Name"
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["display_name"] == "New Name"


def test_account_update_no_fields(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.update_account", return_value=account) as mock_update,
    ):
        result = runner.invoke(main, ["account", "update", account.id])

    assert result.exit_code == 0
    mock_update.assert_called_once_with(mock_connection, account.id)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["id"] == account.id


def test_account_update_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.update_account", return_value=None),
    ):
        result = runner.invoke(
            main, ["account", "update", "nonexistent-id", "--display-name", "X"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "not_found"
