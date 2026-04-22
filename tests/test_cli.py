"""CLI tests for account and company subcommands."""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from conftest import make_test_settings
from mailpilot.cli import main
from mailpilot.models import (
    Account,
    Activity,
    Company,
    Contact,
    Email,
    Note,
    Tag,
    Task,
    Workflow,
    WorkflowContact,
    WorkflowContactDetail,
)

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


# -- --completion --------------------------------------------------------------


def test_completion_zsh(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--completion", "zsh"])
    assert result.exit_code == 0
    assert "#compdef mailpilot" in result.output
    assert "_MAILPILOT_COMPLETE=zsh_complete" in result.output


def test_completion_bash(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--completion", "bash"])
    assert result.exit_code == 0
    assert "_MAILPILOT_COMPLETE=bash_complete" in result.output


def test_completion_unsupported_shell(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--completion", "tcsh"])
    assert result.exit_code != 0


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


def test_account_create_empty_email(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(main, ["account", "create", "--email", ""])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "email" in data["message"]


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


# -- account sync --------------------------------------------------------------


def test_account_sync_all_accounts(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    acc_a = _make_account(
        id="01234567-0000-7000-0000-0000000000a1", email="a@example.com"
    )
    acc_b = _make_account(
        id="01234567-0000-7000-0000-0000000000b2", email="b@example.com"
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_accounts", return_value=[acc_a, acc_b]),
        patch("mailpilot.gmail.GmailClient") as mock_client_cls,
        patch("mailpilot.sync.sync_account", side_effect=[3, 5]) as mock_sync,
    ):
        result = runner.invoke(main, ["account", "sync"])

    assert result.exit_code == 0, result.output
    assert mock_client_cls.call_count == 2
    assert mock_sync.call_count == 2
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["total_stored"] == 8
    assert [r["email"] for r in data["results"]] == ["a@example.com", "b@example.com"]
    assert [r["stored"] for r in data["results"]] == [3, 5]


def test_account_sync_single_account(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    account = _make_account(email="only@example.com")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account) as mock_get,
        patch("mailpilot.database.list_accounts") as mock_list,
        patch("mailpilot.gmail.GmailClient"),
        patch("mailpilot.sync.sync_account", return_value=2),
    ):
        result = runner.invoke(main, ["account", "sync", "--account-id", account.id])

    assert result.exit_code == 0, result.output
    mock_get.assert_called_once_with(mock_connection, account.id)
    mock_list.assert_not_called()
    data = json.loads(result.output)
    assert data["total_stored"] == 2
    assert len(data["results"]) == 1
    assert data["results"][0]["email"] == "only@example.com"


def test_account_sync_unknown_id(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=None),
    ):
        result = runner.invoke(main, ["account", "sync", "--account-id", "missing"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "not_found"


def test_account_sync_error_isolated_per_account(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    acc_a = _make_account(
        id="01234567-0000-7000-0000-0000000000a1", email="a@example.com"
    )
    acc_b = _make_account(
        id="01234567-0000-7000-0000-0000000000b2", email="b@example.com"
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_accounts", return_value=[acc_a, acc_b]),
        patch("mailpilot.gmail.GmailClient"),
        patch("logfire.exception"),
        patch(
            "mailpilot.sync.sync_account",
            side_effect=[RuntimeError("gmail 500"), 4],
        ),
    ):
        result = runner.invoke(main, ["account", "sync"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["total_stored"] == 4
    assert data["results"][0]["error"] == "gmail 500"
    assert "stored" not in data["results"][0]
    assert data["results"][1]["stored"] == 4


# -- company helpers -----------------------------------------------------------


def _make_company(**overrides: Any) -> Company:
    defaults: dict[str, Any] = {
        "id": "01234567-0000-7000-0000-000000000002",
        "name": "Acme Corp",
        "domain": "acme.com",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Company(**{**defaults, **overrides})


# -- company create ------------------------------------------------------------


def test_company_create(runner: CliRunner, mock_connection: MagicMock) -> None:
    company = _make_company()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_company", return_value=company) as mock_create,
    ):
        result = runner.invoke(
            main, ["company", "create", "--domain", "acme.com", "--name", "Acme Corp"]
        )

    assert result.exit_code == 0
    mock_create.assert_called_once_with(
        mock_connection, name="Acme Corp", domain="acme.com"
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["domain"] == "acme.com"
    assert data["name"] == "Acme Corp"


def test_company_create_domain_only(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    company = _make_company(name="")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_company", return_value=company) as mock_create,
    ):
        result = runner.invoke(main, ["company", "create", "--domain", "acme.com"])

    assert result.exit_code == 0
    mock_create.assert_called_once_with(mock_connection, name="", domain="acme.com")


# -- company list --------------------------------------------------------------


def test_company_list(runner: CliRunner, mock_connection: MagicMock) -> None:
    companies = [
        _make_company(id="id-1", domain="a.com"),
        _make_company(id="id-2", domain="b.com"),
    ]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_companies", return_value=companies),
    ):
        result = runner.invoke(main, ["company", "list"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert len(data["companies"]) == 2
    assert data["companies"][0]["domain"] == "a.com"


def test_company_list_empty(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_companies", return_value=[]),
    ):
        result = runner.invoke(main, ["company", "list"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["companies"] == []


def test_company_list_with_limit(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_companies", return_value=[]) as mock_list,
    ):
        result = runner.invoke(main, ["company", "list", "--limit", "5"])

    assert result.exit_code == 0
    mock_list.assert_called_once_with(mock_connection, limit=5)


# -- company view --------------------------------------------------------------


def test_company_view(runner: CliRunner, mock_connection: MagicMock) -> None:
    company = _make_company()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=company) as mock_get,
    ):
        result = runner.invoke(main, ["company", "view", company.id])

    assert result.exit_code == 0
    mock_get.assert_called_once_with(mock_connection, company.id)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["id"] == company.id


def test_company_view_not_found(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=None),
    ):
        result = runner.invoke(main, ["company", "view", "nonexistent-id"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "not_found"


# -- company search ------------------------------------------------------------


def test_company_search(runner: CliRunner, mock_connection: MagicMock) -> None:
    companies = [_make_company()]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.search_companies", return_value=companies
        ) as mock_search,
    ):
        result = runner.invoke(main, ["company", "search", "acme"])

    assert result.exit_code == 0
    mock_search.assert_called_once_with(mock_connection, "acme", limit=100)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert len(data["companies"]) == 1


def test_company_search_with_limit(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.search_companies", return_value=[]) as mock_search,
    ):
        result = runner.invoke(main, ["company", "search", "acme", "--limit", "10"])

    assert result.exit_code == 0
    mock_search.assert_called_once_with(mock_connection, "acme", limit=10)


def test_company_create_empty_domain(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(main, ["company", "create", "--domain", ""])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "domain" in data["message"]


# -- company update ------------------------------------------------------------


def test_company_update_name(runner: CliRunner, mock_connection: MagicMock) -> None:
    updated = _make_company(name="New Name")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.update_company", return_value=updated) as mock_update,
    ):
        result = runner.invoke(
            main, ["company", "update", updated.id, "--name", "New Name"]
        )

    assert result.exit_code == 0
    mock_update.assert_called_once_with(mock_connection, updated.id, name="New Name")
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["name"] == "New Name"


def test_company_update_no_fields(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    company = _make_company()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.update_company", return_value=company) as mock_update,
    ):
        result = runner.invoke(main, ["company", "update", company.id])

    assert result.exit_code == 0
    mock_update.assert_called_once_with(mock_connection, company.id)
    data = json.loads(result.output)
    assert data["ok"] is True


def test_company_update_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.update_company", return_value=None),
    ):
        result = runner.invoke(
            main, ["company", "update", "nonexistent-id", "--name", "X"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "not_found"


# -- company export ------------------------------------------------------------


def test_company_export(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: Any
) -> None:
    companies = [_make_company(id="id-1"), _make_company(id="id-2")]
    export_file = str(tmp_path / "companies.json")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_companies", return_value=companies),
    ):
        result = runner.invoke(main, ["company", "export", export_file])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["exported"] == 2
    exported = json.loads(pathlib.Path(export_file).read_text())
    assert len(exported) == 2
    assert exported[0]["id"] == "id-1"


# -- company import ------------------------------------------------------------


def test_company_import(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: Any
) -> None:
    entries = [
        {"name": "Acme Corp", "domain": "acme.com"},
        {"name": "Beta Inc", "domain": "beta.com"},
    ]
    import_file = tmp_path / "companies.json"
    import_file.write_text(json.dumps(entries))
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.create_company",
            side_effect=[_make_company(domain=e["domain"]) for e in entries],
        ) as mock_create,
    ):
        result = runner.invoke(main, ["company", "import", str(import_file)])

    assert result.exit_code == 0
    assert mock_create.call_count == 2
    mock_create.assert_any_call(mock_connection, name="Acme Corp", domain="acme.com")
    mock_create.assert_any_call(mock_connection, name="Beta Inc", domain="beta.com")
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["imported"] == 2


# -- contact helpers -----------------------------------------------------------


def _make_contact(**overrides: Any) -> Contact:
    defaults: dict[str, Any] = {
        "id": "01234567-0000-7000-0000-000000000003",
        "email": "alice@example.com",
        "domain": "example.com",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Contact(**{**defaults, **overrides})


# -- contact create ------------------------------------------------------------


def test_contact_create(runner: CliRunner, mock_connection: MagicMock) -> None:
    contact = _make_contact(first_name="Alice", last_name="Smith")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_contact", return_value=contact) as mock_create,
    ):
        result = runner.invoke(
            main,
            [
                "contact",
                "create",
                "--email",
                "alice@example.com",
                "--first-name",
                "Alice",
                "--last-name",
                "Smith",
            ],
        )

    assert result.exit_code == 0
    mock_create.assert_called_once_with(
        mock_connection,
        email="alice@example.com",
        domain="example.com",
        first_name="Alice",
        last_name="Smith",
        company_id=None,
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["email"] == "alice@example.com"
    assert data["first_name"] == "Alice"


def test_contact_create_email_only(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_contact", return_value=contact) as mock_create,
    ):
        result = runner.invoke(
            main, ["contact", "create", "--email", "alice@example.com"]
        )

    assert result.exit_code == 0
    mock_create.assert_called_once_with(
        mock_connection,
        email="alice@example.com",
        domain="example.com",
        first_name=None,
        last_name=None,
        company_id=None,
    )


def test_contact_create_company_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=None),
    ):
        result = runner.invoke(
            main,
            [
                "contact",
                "create",
                "--email",
                "a@example.com",
                "--company-id",
                "comp-missing",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "company" in data["message"]


# -- contact update ------------------------------------------------------------


def test_contact_update_first_name(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    updated = _make_contact(first_name="Alicia")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.update_contact", return_value=updated) as mock_update,
    ):
        result = runner.invoke(
            main, ["contact", "update", updated.id, "--first-name", "Alicia"]
        )

    assert result.exit_code == 0
    mock_update.assert_called_once_with(
        mock_connection, updated.id, first_name="Alicia"
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["first_name"] == "Alicia"


def test_contact_update_no_fields(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.update_contact", return_value=contact) as mock_update,
    ):
        result = runner.invoke(main, ["contact", "update", contact.id])

    assert result.exit_code == 0
    mock_update.assert_called_once_with(mock_connection, contact.id)


def test_contact_update_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.update_contact", return_value=None),
    ):
        result = runner.invoke(
            main, ["contact", "update", "nonexistent-id", "--first-name", "X"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "not_found"


# -- contact search ------------------------------------------------------------


def test_contact_search(runner: CliRunner, mock_connection: MagicMock) -> None:
    contacts = [_make_contact()]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.search_contacts", return_value=contacts
        ) as mock_search,
    ):
        result = runner.invoke(main, ["contact", "search", "alice"])

    assert result.exit_code == 0
    mock_search.assert_called_once_with(mock_connection, "alice", limit=100)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert len(data["contacts"]) == 1


def test_contact_search_with_limit(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.search_contacts", return_value=[]) as mock_search,
    ):
        result = runner.invoke(main, ["contact", "search", "alice", "--limit", "10"])

    assert result.exit_code == 0
    mock_search.assert_called_once_with(mock_connection, "alice", limit=10)


# -- contact list --------------------------------------------------------------


def test_contact_list(runner: CliRunner, mock_connection: MagicMock) -> None:
    contacts = [
        _make_contact(id="id-1", email="a@example.com"),
        _make_contact(id="id-2", email="b@example.com"),
    ]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_contacts", return_value=contacts),
    ):
        result = runner.invoke(main, ["contact", "list"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert len(data["contacts"]) == 2


def test_contact_list_empty(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_contacts", return_value=[]),
    ):
        result = runner.invoke(main, ["contact", "list"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["contacts"] == []


def test_contact_list_with_filters(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    company = _make_company()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=company),
        patch("mailpilot.database.list_contacts", return_value=[]) as mock_list,
    ):
        result = runner.invoke(
            main,
            [
                "contact",
                "list",
                "--limit",
                "5",
                "--domain",
                "example.com",
                "--company-id",
                "cid-1",
            ],
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection, limit=5, domain="example.com", company_id="cid-1", status=None
    )


def test_contact_list_with_status(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_contacts", return_value=[]) as mock_list,
    ):
        result = runner.invoke(main, ["contact", "list", "--status", "bounced"])

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection, limit=100, domain=None, company_id=None, status="bounced"
    )


def test_contact_list_company_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=None),
    ):
        result = runner.invoke(
            main, ["contact", "list", "--company-id", "comp-missing"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "company" in data["message"]


# -- contact view --------------------------------------------------------------


def test_contact_view(runner: CliRunner, mock_connection: MagicMock) -> None:
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=contact) as mock_get,
    ):
        result = runner.invoke(main, ["contact", "view", contact.id])

    assert result.exit_code == 0
    mock_get.assert_called_once_with(mock_connection, contact.id)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["id"] == contact.id


def test_contact_view_not_found(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=None),
    ):
        result = runner.invoke(main, ["contact", "view", "nonexistent-id"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "not_found"


# -- contact export ------------------------------------------------------------


def test_contact_export(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: Any
) -> None:
    contacts = [_make_contact(id="id-1"), _make_contact(id="id-2")]
    export_file = str(tmp_path / "contacts.json")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_contacts", return_value=contacts),
    ):
        result = runner.invoke(main, ["contact", "export", export_file])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["exported"] == 2
    exported = json.loads(pathlib.Path(export_file).read_text())
    assert len(exported) == 2
    assert exported[0]["id"] == "id-1"


# -- contact import ------------------------------------------------------------


def test_contact_import(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: Any
) -> None:
    entries = [
        {"email": "alice@acme.com", "first_name": "Alice", "last_name": "Smith"},
        {"email": "bob@beta.com"},
    ]
    import_file = tmp_path / "contacts.json"
    import_file.write_text(json.dumps(entries))
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.create_contact",
            side_effect=[
                _make_contact(email=e["email"], domain=e["email"].split("@")[-1])
                for e in entries
            ],
        ) as mock_create,
    ):
        result = runner.invoke(main, ["contact", "import", str(import_file)])

    assert result.exit_code == 0
    assert mock_create.call_count == 2
    mock_create.assert_any_call(
        mock_connection,
        email="alice@acme.com",
        domain="acme.com",
        first_name="Alice",
        last_name="Smith",
        company_id=None,
    )
    mock_create.assert_any_call(
        mock_connection,
        email="bob@beta.com",
        domain="beta.com",
        first_name=None,
        last_name=None,
        company_id=None,
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["imported"] == 2


# -- Email ---------------------------------------------------------------------


def _make_email(**overrides: Any) -> Email:
    defaults: dict[str, Any] = {
        "id": "01234567-0000-7000-0000-000000000004",
        "account_id": "01234567-0000-7000-0000-000000000001",
        "direction": "inbound",
        "created_at": _NOW,
    }
    return Email(**{**defaults, **overrides})


def test_email_search(runner: CliRunner, mock_connection: MagicMock) -> None:
    email = _make_email()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.search_emails", return_value=[email]) as mock_search,
    ):
        result = runner.invoke(main, ["email", "search", "hello"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert len(data["emails"]) == 1
    mock_search.assert_called_once_with(mock_connection, "hello", limit=100)


def test_email_search_with_limit(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.search_emails", return_value=[]) as mock_search,
    ):
        result = runner.invoke(main, ["email", "search", "hello", "--limit", "10"])
    assert result.exit_code == 0
    mock_search.assert_called_once_with(mock_connection, "hello", limit=10)


def test_email_list(runner: CliRunner, mock_connection: MagicMock) -> None:
    email = _make_email()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_emails", return_value=[email]) as mock_list,
    ):
        result = runner.invoke(main, ["email", "list"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert len(data["emails"]) == 1
    mock_list.assert_called_once_with(
        mock_connection,
        limit=100,
        contact_id=None,
        account_id=None,
        since=None,
        thread_id=None,
        direction=None,
        workflow_id=None,
        status=None,
    )


def test_email_list_empty(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_emails", return_value=[]),
    ):
        result = runner.invoke(main, ["email", "list"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["emails"] == []


def test_email_list_with_filters(runner: CliRunner, mock_connection: MagicMock) -> None:
    contact = _make_contact()
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.list_emails", return_value=[]) as mock_list,
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "list",
                "--limit",
                "5",
                "--contact-id",
                "cid",
                "--account-id",
                "aid",
            ],
        )
    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        limit=5,
        contact_id="cid",
        account_id="aid",
        since=None,
        thread_id=None,
        direction=None,
        workflow_id=None,
        status=None,
    )


def test_email_list_with_new_filters(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    workflow = _make_workflow()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.list_emails", return_value=[]) as mock_list,
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "list",
                "--since",
                "2024-01-01T00:00:00Z",
                "--thread-id",
                "thread_abc",
                "--direction",
                "inbound",
                "--workflow-id",
                _WORKFLOW_ID,
                "--status",
                "received",
            ],
        )
    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        limit=100,
        contact_id=None,
        account_id=None,
        since="2024-01-01T00:00:00Z",
        thread_id="thread_abc",
        direction="inbound",
        workflow_id=_WORKFLOW_ID,
        status="received",
    )


def test_email_list_workflow_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=None),
    ):
        result = runner.invoke(main, ["email", "list", "--workflow-id", "wf-missing"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "workflow" in data["message"]


def test_email_view(runner: CliRunner, mock_connection: MagicMock) -> None:
    email = _make_email()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_email", return_value=email),
    ):
        result = runner.invoke(main, ["email", "view", email.id])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["id"] == email.id


def test_email_view_not_found(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_email", return_value=None),
    ):
        result = runner.invoke(main, ["email", "view", "nonexistent-id"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "not_found"


def test_email_list_contact_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=None),
    ):
        result = runner.invoke(main, ["email", "list", "--contact-id", "cid-missing"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "contact" in data["message"]


def test_email_list_account_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=None),
    ):
        result = runner.invoke(main, ["email", "list", "--account-id", "acc-missing"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "account" in data["message"]


# -- email send ----------------------------------------------------------------


def test_email_send_success(runner: CliRunner, mock_connection: MagicMock) -> None:
    account = _make_account()
    sent = _make_email(
        direction="outbound",
        status="sent",
        subject="Hi",
        body_text="Hello",
        gmail_message_id="gm-1",
        gmail_thread_id="gt-1",
        sent_at=_NOW,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.gmail.GmailClient") as mock_client_cls,
        patch("mailpilot.sync.send_email", return_value=sent) as mock_send,
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "send",
                "--account-id",
                account.id,
                "--to",
                "recipient@example.com",
                "--subject",
                "Hi",
                "--body",
                "Hello",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_client_cls.assert_called_once_with(account.email)
    assert mock_send.call_count == 1
    kwargs = mock_send.call_args.kwargs
    assert kwargs["account"] == account
    assert kwargs["to"] == "recipient@example.com"
    assert kwargs["subject"] == "Hi"
    assert kwargs["body"] == "Hello"
    assert kwargs["contact_id"] is None
    assert kwargs["workflow_id"] is None
    assert kwargs["thread_id"] is None
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["id"] == sent.id
    assert data["direction"] == "outbound"
    assert data["status"] == "sent"


def test_email_send_with_optional_flags(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    account = _make_account()
    sent = _make_email(
        direction="outbound",
        status="sent",
        contact_id="contact-x",
        workflow_id="workflow-y",
        sent_at=_NOW,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.gmail.GmailClient"),
        patch("mailpilot.sync.send_email", return_value=sent) as mock_send,
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "send",
                "--account-id",
                account.id,
                "--to",
                "recipient@example.com",
                "--subject",
                "Hello",
                "--body",
                "Body",
                "--contact-id",
                "contact-x",
                "--workflow-id",
                "workflow-y",
                "--thread-id",
                "gmail-thread-z",
            ],
        )

    assert result.exit_code == 0, result.output
    kwargs = mock_send.call_args.kwargs
    assert kwargs["contact_id"] == "contact-x"
    assert kwargs["workflow_id"] == "workflow-y"
    assert kwargs["thread_id"] == "gmail-thread-z"


def test_email_send_account_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=None),
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "send",
                "--account-id",
                "missing",
                "--to",
                "r@example.com",
                "--subject",
                "s",
                "--body",
                "b",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "not_found"


def test_email_send_gmail_failure_returns_error(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.gmail.GmailClient"),
        patch("logfire.exception"),
        patch("mailpilot.sync.send_email", side_effect=RuntimeError("gmail 500")),
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "send",
                "--account-id",
                account.id,
                "--to",
                "r@example.com",
                "--subject",
                "s",
                "--body",
                "b",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "send_failed"
    assert "gmail 500" in data["message"]


# -- workflow helpers ----------------------------------------------------------


_WORKFLOW_ID = "01234567-0000-7000-0000-000000000005"
_ACCOUNT_ID = "01234567-0000-7000-0000-000000000001"
_CONTACT_ID = "01234567-0000-7000-0000-000000000006"


def _make_workflow(**overrides: Any) -> Workflow:
    defaults: dict[str, Any] = {
        "id": _WORKFLOW_ID,
        "name": "Demo outreach",
        "type": "outbound",
        "account_id": _ACCOUNT_ID,
        "status": "draft",
        "objective": "",
        "instructions": "",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Workflow(**{**defaults, **overrides})


# -- workflow create -----------------------------------------------------------


def test_workflow_create(runner: CliRunner, mock_connection: MagicMock) -> None:
    workflow = _make_workflow()
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch(
            "mailpilot.database.create_workflow", return_value=workflow
        ) as mock_create,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "create",
                "--name",
                "Demo outreach",
                "--type",
                "outbound",
                "--account-id",
                _ACCOUNT_ID,
                "--draft",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_create.assert_called_once_with(
        mock_connection,
        name="Demo outreach",
        workflow_type="outbound",
        account_id=_ACCOUNT_ID,
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["id"] == workflow.id
    assert data["type"] == "outbound"


def test_workflow_create_with_objective_and_instructions(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    workflow = _make_workflow(
        objective="Book demo", instructions="You are a sales rep."
    )
    activated = _make_workflow(
        status="active", objective="Book demo", instructions="You are a sales rep."
    )
    instructions_file = tmp_path / "instructions.md"
    instructions_file.write_text("You are a sales rep.")
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.create_workflow", return_value=_make_workflow()),
        patch(
            "mailpilot.database.update_workflow", return_value=workflow
        ) as mock_update,
        patch(
            "mailpilot.database.activate_workflow", return_value=activated
        ) as mock_activate,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "create",
                "--name",
                "Demo outreach",
                "--type",
                "outbound",
                "--account-id",
                _ACCOUNT_ID,
                "--objective",
                "Book demo",
                "--instructions-file",
                str(instructions_file),
            ],
        )

    assert result.exit_code == 0, result.output
    mock_update.assert_called_once_with(
        mock_connection,
        _WORKFLOW_ID,
        objective="Book demo",
        instructions="You are a sales rep.",
    )
    mock_activate.assert_called_once_with(mock_connection, _WORKFLOW_ID)
    data = json.loads(result.output)
    assert data["status"] == "active"


def test_workflow_create_with_inline_instructions(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    workflow = _make_workflow(
        objective="Book demo", instructions="You are a sales rep."
    )
    activated = _make_workflow(
        status="active", objective="Book demo", instructions="You are a sales rep."
    )
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.create_workflow", return_value=_make_workflow()),
        patch("mailpilot.database.update_workflow", return_value=workflow),
        patch(
            "mailpilot.database.activate_workflow", return_value=activated
        ) as mock_activate,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "create",
                "--name",
                "Demo outreach",
                "--type",
                "outbound",
                "--account-id",
                _ACCOUNT_ID,
                "--objective",
                "Book demo",
                "--instructions",
                "You are a sales rep.",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_activate.assert_called_once_with(mock_connection, _WORKFLOW_ID)
    data = json.loads(result.output)
    assert data["status"] == "active"


def test_workflow_create_instructions_mutual_exclusion(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    instructions_file = tmp_path / "instructions.md"
    instructions_file.write_text("From file.")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "create",
                "--name",
                "Test",
                "--type",
                "outbound",
                "--account-id",
                _ACCOUNT_ID,
                "--instructions",
                "Inline text",
                "--instructions-file",
                str(instructions_file),
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "mutually exclusive" in data["message"]


def test_workflow_create_rejects_invalid_type(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    result = runner.invoke(
        main,
        [
            "workflow",
            "create",
            "--name",
            "Bad",
            "--type",
            "sideways",
            "--account-id",
            _ACCOUNT_ID,
        ],
    )
    assert result.exit_code != 0


def test_workflow_create_empty_name(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "create",
                "--name",
                "",
                "--type",
                "outbound",
                "--account-id",
                "acc-1",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "name" in data["message"]


def test_workflow_create_account_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=None),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "create",
                "--name",
                "Test",
                "--type",
                "outbound",
                "--account-id",
                "acc-missing",
                "--draft",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "account" in data["message"]


def test_workflow_create_auto_activates(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    created = _make_workflow()
    updated = _make_workflow(objective="Book demo", instructions="You are a sales rep.")
    activated = _make_workflow(
        status="active", objective="Book demo", instructions="You are a sales rep."
    )
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.create_workflow", return_value=created),
        patch("mailpilot.database.update_workflow", return_value=updated),
        patch(
            "mailpilot.database.activate_workflow", return_value=activated
        ) as mock_activate,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "create",
                "--name",
                "Demo outreach",
                "--type",
                "outbound",
                "--account-id",
                _ACCOUNT_ID,
                "--objective",
                "Book demo",
                "--instructions",
                "You are a sales rep.",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_activate.assert_called_once_with(mock_connection, _WORKFLOW_ID)
    data = json.loads(result.output)
    assert data["status"] == "active"


def test_workflow_create_draft_skips_activation(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    workflow = _make_workflow(
        objective="Book demo", instructions="You are a sales rep."
    )
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.create_workflow", return_value=_make_workflow()),
        patch("mailpilot.database.update_workflow", return_value=workflow),
        patch("mailpilot.database.activate_workflow") as mock_activate,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "create",
                "--name",
                "Demo outreach",
                "--type",
                "outbound",
                "--account-id",
                _ACCOUNT_ID,
                "--objective",
                "Book demo",
                "--instructions",
                "You are a sales rep.",
                "--draft",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_activate.assert_not_called()
    data = json.loads(result.output)
    assert data["status"] == "draft"


def test_workflow_create_missing_fields_without_draft(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "create",
                "--name",
                "Demo outreach",
                "--type",
                "outbound",
                "--account-id",
                _ACCOUNT_ID,
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "--draft" in data["message"]


# -- workflow update -----------------------------------------------------------


def test_workflow_update_name(runner: CliRunner, mock_connection: MagicMock) -> None:
    updated = _make_workflow(name="Renamed")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.update_workflow", return_value=updated
        ) as mock_update,
    ):
        result = runner.invoke(
            main, ["workflow", "update", _WORKFLOW_ID, "--name", "Renamed"]
        )

    assert result.exit_code == 0, result.output
    mock_update.assert_called_once_with(mock_connection, _WORKFLOW_ID, name="Renamed")
    data = json.loads(result.output)
    assert data["name"] == "Renamed"


def test_workflow_update_with_instructions_file(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    instructions_file = tmp_path / "instructions.md"
    instructions_file.write_text("Reply politely.")
    updated = _make_workflow(instructions="Reply politely.")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.update_workflow", return_value=updated
        ) as mock_update,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "update",
                _WORKFLOW_ID,
                "--instructions-file",
                str(instructions_file),
            ],
        )

    assert result.exit_code == 0, result.output
    mock_update.assert_called_once_with(
        mock_connection, _WORKFLOW_ID, instructions="Reply politely."
    )


def test_workflow_update_with_inline_instructions(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    updated = _make_workflow(instructions="Be concise.")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.update_workflow", return_value=updated
        ) as mock_update,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "update",
                _WORKFLOW_ID,
                "--instructions",
                "Be concise.",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_update.assert_called_once_with(
        mock_connection, _WORKFLOW_ID, instructions="Be concise."
    )


def test_workflow_update_instructions_mutual_exclusion(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    instructions_file = tmp_path / "instructions.md"
    instructions_file.write_text("From file.")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "update",
                _WORKFLOW_ID,
                "--instructions",
                "Inline text",
                "--instructions-file",
                str(instructions_file),
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "mutually exclusive" in data["message"]


def test_workflow_update_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.update_workflow", return_value=None),
    ):
        result = runner.invoke(main, ["workflow", "update", "nope", "--name", "X"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


# -- workflow list / view / search ---------------------------------------------


def test_workflow_list(runner: CliRunner, mock_connection: MagicMock) -> None:
    workflows = [_make_workflow(id="id-1"), _make_workflow(id="id-2", name="Other")]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_workflows", return_value=workflows) as mock_list,
    ):
        result = runner.invoke(main, ["workflow", "list"])

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection, account_id=None, status=None, workflow_type=None
    )
    data = json.loads(result.output)
    assert len(data["workflows"]) == 2


def test_workflow_list_by_account(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.list_workflows", return_value=[]) as mock_list,
    ):
        result = runner.invoke(main, ["workflow", "list", "--account-id", _ACCOUNT_ID])

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection, account_id=_ACCOUNT_ID, status=None, workflow_type=None
    )


def test_workflow_list_account_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=None),
    ):
        result = runner.invoke(
            main, ["workflow", "list", "--account-id", "acc-missing"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "account" in data["message"]


def test_workflow_list_with_filters(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_workflows", return_value=[]) as mock_list,
    ):
        result = runner.invoke(
            main,
            ["workflow", "list", "--status", "active", "--type", "outbound"],
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection, account_id=None, status="active", workflow_type="outbound"
    )


def test_workflow_view(runner: CliRunner, mock_connection: MagicMock) -> None:
    workflow = _make_workflow()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
    ):
        result = runner.invoke(main, ["workflow", "view", _WORKFLOW_ID])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["id"] == _WORKFLOW_ID


def test_workflow_view_not_found(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=None),
    ):
        result = runner.invoke(main, ["workflow", "view", "nope"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_workflow_search(runner: CliRunner, mock_connection: MagicMock) -> None:
    workflows = [_make_workflow(name="Demo")]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.search_workflows", return_value=workflows
        ) as mock_search,
    ):
        result = runner.invoke(main, ["workflow", "search", "demo", "--limit", "5"])

    assert result.exit_code == 0
    mock_search.assert_called_once_with(mock_connection, "demo", limit=5)
    data = json.loads(result.output)
    assert len(data["workflows"]) == 1


# -- workflow start / stop -----------------------------------------------------


def test_workflow_start(runner: CliRunner, mock_connection: MagicMock) -> None:
    activated = _make_workflow(
        status="active",
        objective="Book demo",
        instructions="You are a sales rep.",
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.activate_workflow", return_value=activated
        ) as mock_activate,
    ):
        result = runner.invoke(main, ["workflow", "start", _WORKFLOW_ID])

    assert result.exit_code == 0, result.output
    mock_activate.assert_called_once_with(mock_connection, _WORKFLOW_ID)
    data = json.loads(result.output)
    assert data["status"] == "active"


def test_workflow_start_missing_objective(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.activate_workflow",
            side_effect=ValueError("objective must be non-empty to activate"),
        ),
    ):
        result = runner.invoke(main, ["workflow", "start", _WORKFLOW_ID])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "invalid_state"
    assert "workflow update" in data["message"]
    assert "--objective" in data["message"]


def test_workflow_start_missing_instructions(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.activate_workflow",
            side_effect=ValueError("instructions must be non-empty to activate"),
        ),
    ):
        result = runner.invoke(main, ["workflow", "start", _WORKFLOW_ID])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "invalid_state"
    assert "workflow update" in data["message"]
    assert "--instructions" in data["message"]


def test_workflow_stop(runner: CliRunner, mock_connection: MagicMock) -> None:
    paused = _make_workflow(status="paused")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.pause_workflow", return_value=paused) as mock_pause,
    ):
        result = runner.invoke(main, ["workflow", "stop", _WORKFLOW_ID])

    assert result.exit_code == 0, result.output
    mock_pause.assert_called_once_with(mock_connection, _WORKFLOW_ID)
    data = json.loads(result.output)
    assert data["status"] == "paused"


def test_workflow_stop_invalid_state(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.pause_workflow",
            side_effect=ValueError("cannot pause workflow in status 'draft'"),
        ),
    ):
        result = runner.invoke(main, ["workflow", "stop", _WORKFLOW_ID])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "invalid_state"


# -- workflow run --------------------------------------------------------------


def test_workflow_run(runner: CliRunner, mock_connection: MagicMock) -> None:
    workflow = _make_workflow(
        status="active",
        objective="Book demo",
        instructions="You are a sales rep.",
    )
    contact = Contact(
        id=_CONTACT_ID,
        email="lead@acme.com",
        domain="acme.com",
        created_at=_NOW,
        updated_at=_NOW,
    )
    wc = WorkflowContact(
        workflow_id=_WORKFLOW_ID,
        contact_id=_CONTACT_ID,
        created_at=_NOW,
        updated_at=_NOW,
    )
    task = _make_task()
    completed_task = _make_task(
        status="completed",
        result={"reasoning": "Sent intro.", "tool_calls": 2},
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.get_workflow_contact", return_value=wc),
        patch("mailpilot.database.create_task", return_value=task),
        patch("mailpilot.run.execute_task") as mock_exec,
        patch("mailpilot.database.get_task", return_value=completed_task),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "run",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
            ],
        )

    assert result.exit_code == 0, result.output
    mock_exec.assert_called_once()
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["status"] == "completed"
    assert data["result"]["reasoning"] == "Sent intro."


def test_workflow_run_workflow_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=None),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "run",
                "--workflow-id",
                "nope",
                "--contact-id",
                _CONTACT_ID,
            ],
        )
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_workflow_run_requires_active(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    workflow = _make_workflow(status="draft")
    contact = Contact(
        id=_CONTACT_ID,
        email="lead@acme.com",
        domain="acme.com",
        created_at=_NOW,
        updated_at=_NOW,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.get_contact", return_value=contact),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "run",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
            ],
        )
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "invalid_state"


def test_workflow_run_requires_outbound(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    workflow = _make_workflow(type="inbound", status="active")
    contact = Contact(
        id=_CONTACT_ID,
        email="lead@acme.com",
        domain="acme.com",
        created_at=_NOW,
        updated_at=_NOW,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.get_contact", return_value=contact),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "run",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
            ],
        )
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "invalid_state"


def test_workflow_run_contact_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    workflow = _make_workflow(status="active")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.get_contact", return_value=None),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "run",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                "nope",
            ],
        )
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_workflow_run_contact_not_enrolled(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    workflow = _make_workflow(status="active")
    contact = Contact(
        id=_CONTACT_ID,
        email="lead@acme.com",
        domain="acme.com",
        created_at=_NOW,
        updated_at=_NOW,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.get_workflow_contact", return_value=None),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "run",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
            ],
        )
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "not enrolled" in data["message"]


def test_workflow_run_agent_failed(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    workflow = _make_workflow(status="active")
    contact = Contact(
        id=_CONTACT_ID,
        email="lead@acme.com",
        domain="acme.com",
        created_at=_NOW,
        updated_at=_NOW,
    )
    wc = WorkflowContact(
        workflow_id=_WORKFLOW_ID,
        contact_id=_CONTACT_ID,
        created_at=_NOW,
        updated_at=_NOW,
    )
    task = _make_task()
    failed_task = _make_task(status="failed", result={"reason": "agent error"})
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.get_workflow_contact", return_value=wc),
        patch("mailpilot.database.create_task", return_value=task),
        patch("mailpilot.run.execute_task"),
        patch("mailpilot.database.get_task", return_value=failed_task),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "run",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
            ],
        )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "failed"
    assert data["result"]["reason"] == "agent error"


# -- Activity ------------------------------------------------------------------


def _make_activity(**overrides: Any) -> Activity:
    defaults: dict[str, Any] = {
        "id": "01234567-0000-7000-0000-000000000010",
        "contact_id": "01234567-0000-7000-0000-000000000003",
        "type": "email_sent",
        "summary": "Sent intro email",
        "detail": {},
        "created_at": _NOW,
    }
    return Activity(**{**defaults, **overrides})


# -- activity create -----------------------------------------------------------


def test_activity_create(runner: CliRunner, mock_connection: MagicMock) -> None:
    activity = _make_activity()
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch(
            "mailpilot.database.create_activity", return_value=activity
        ) as mock_create,
    ):
        result = runner.invoke(
            main,
            [
                "activity",
                "create",
                "--contact-id",
                "cid-1",
                "--type",
                "email_sent",
                "--summary",
                "Sent intro",
            ],
        )

    assert result.exit_code == 0
    mock_create.assert_called_once_with(
        mock_connection,
        contact_id="cid-1",
        activity_type="email_sent",
        summary="Sent intro",
        detail={},
        company_id=None,
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["type"] == "email_sent"


def test_activity_create_with_detail(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    activity = _make_activity(detail={"email_id": "e-1"})
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch(
            "mailpilot.database.create_activity", return_value=activity
        ) as mock_create,
    ):
        result = runner.invoke(
            main,
            [
                "activity",
                "create",
                "--contact-id",
                "cid-1",
                "--type",
                "email_sent",
                "--summary",
                "Sent intro",
                "--detail",
                '{"email_id": "e-1"}',
            ],
        )

    assert result.exit_code == 0
    mock_create.assert_called_once_with(
        mock_connection,
        contact_id="cid-1",
        activity_type="email_sent",
        summary="Sent intro",
        detail={"email_id": "e-1"},
        company_id=None,
    )


def test_activity_create_empty_summary(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(
            main,
            [
                "activity",
                "create",
                "--contact-id",
                "cid-1",
                "--type",
                "note_added",
                "--summary",
                "",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "summary" in data["message"]


def test_activity_create_contact_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=None),
    ):
        result = runner.invoke(
            main,
            [
                "activity",
                "create",
                "--contact-id",
                "cid-missing",
                "--type",
                "note_added",
                "--summary",
                "Test",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "contact" in data["message"]


def test_activity_create_company_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.get_company", return_value=None),
    ):
        result = runner.invoke(
            main,
            [
                "activity",
                "create",
                "--contact-id",
                "cid-1",
                "--type",
                "note_added",
                "--summary",
                "Test",
                "--company-id",
                "comp-missing",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "company" in data["message"]


# -- activity list -------------------------------------------------------------


def test_activity_list(runner: CliRunner, mock_connection: MagicMock) -> None:
    activities = [
        _make_activity(id="id-1", summary="first"),
        _make_activity(id="id-2", summary="second"),
    ]
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.list_activities", return_value=activities),
    ):
        result = runner.invoke(main, ["activity", "list", "--contact-id", "cid-1"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert len(data["activities"]) == 2


def test_activity_list_empty(runner: CliRunner, mock_connection: MagicMock) -> None:
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.list_activities", return_value=[]),
    ):
        result = runner.invoke(main, ["activity", "list", "--contact-id", "cid-1"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["activities"] == []


def test_activity_list_with_filters(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.list_activities", return_value=[]) as mock_list,
    ):
        result = runner.invoke(
            main,
            [
                "activity",
                "list",
                "--contact-id",
                "cid-1",
                "--type",
                "email_sent",
                "--limit",
                "5",
                "--since",
                "2024-01-01T00:00:00Z",
            ],
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        contact_id="cid-1",
        company_id=None,
        activity_type="email_sent",
        limit=5,
        since="2024-01-01T00:00:00Z",
    )


def test_activity_list_no_filter(runner: CliRunner, mock_connection: MagicMock) -> None:
    """activity list without --contact-id or --company-id should error."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(main, ["activity", "list"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "missing_filter"


def test_activity_list_contact_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=None),
    ):
        result = runner.invoke(
            main, ["activity", "list", "--contact-id", "cid-missing"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "contact" in data["message"]


def test_activity_list_company_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=None),
    ):
        result = runner.invoke(
            main, ["activity", "list", "--company-id", "comp-missing"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "company" in data["message"]


# -- Tag -----------------------------------------------------------------------


def _make_tag(**overrides: Any) -> Tag:
    defaults: dict[str, Any] = {
        "id": "01234567-0000-7000-0000-000000000011",
        "entity_type": "contact",
        "entity_id": "01234567-0000-7000-0000-000000000003",
        "name": "prospect",
        "created_at": _NOW,
    }
    return Tag(**{**defaults, **overrides})


# -- tag add -------------------------------------------------------------------


def test_tag_add(runner: CliRunner, mock_connection: MagicMock) -> None:
    tag = _make_tag()
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_tag", return_value=tag) as mock_create,
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.create_activity"),
    ):
        result = runner.invoke(
            main,
            ["tag", "add", "--contact-id", "cid-1", "prospect"],
        )

    assert result.exit_code == 0
    mock_create.assert_called_once_with(
        mock_connection,
        entity_type="contact",
        entity_id="cid-1",
        name="prospect",
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["name"] == "prospect"


def test_tag_add_creates_activity(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    tag = _make_tag()
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_tag", return_value=tag),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.create_activity") as mock_activity,
    ):
        runner.invoke(
            main,
            ["tag", "add", "--contact-id", "cid-1", "prospect"],
        )

    mock_activity.assert_called_once_with(
        mock_connection,
        contact_id="cid-1",
        activity_type="tag_added",
        summary="Tagged as prospect",
        detail={"tag": "prospect"},
        company_id=None,
    )


def test_tag_add_already_exists(runner: CliRunner, mock_connection: MagicMock) -> None:
    contact = _make_contact(id="cid-1")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.create_tag", return_value=None),
    ):
        result = runner.invoke(
            main,
            ["tag", "add", "--contact-id", "cid-1", "prospect"],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "already_exists"


def test_tag_add_contact_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=None),
    ):
        result = runner.invoke(
            main,
            ["tag", "add", "--contact-id", "cid-missing", "prospect"],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "contact" in data["message"]


def test_tag_add_company_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=None),
    ):
        result = runner.invoke(
            main,
            ["tag", "add", "--company-id", "cid-missing", "prospect"],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "company" in data["message"]


def test_tag_add_no_entity(runner: CliRunner, mock_connection: MagicMock) -> None:
    """tag add without --contact-id or --company-id should error."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(main, ["tag", "add", "prospect"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "missing_filter"


def test_tag_add_empty_name(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(main, ["tag", "add", "--contact-id", "cid-1", ""])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "name" in data["message"]


# -- tag remove ----------------------------------------------------------------


def test_tag_remove(runner: CliRunner, mock_connection: MagicMock) -> None:
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.delete_tag", return_value=True) as mock_delete,
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.create_activity"),
    ):
        result = runner.invoke(
            main,
            ["tag", "remove", "--contact-id", "cid-1", "prospect"],
        )

    assert result.exit_code == 0
    mock_delete.assert_called_once_with(
        mock_connection,
        entity_type="contact",
        entity_id="cid-1",
        name="prospect",
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["removed"] is True


def test_tag_remove_creates_activity(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.delete_tag", return_value=True),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.create_activity") as mock_activity,
    ):
        runner.invoke(
            main,
            ["tag", "remove", "--contact-id", "cid-1", "prospect"],
        )

    mock_activity.assert_called_once_with(
        mock_connection,
        contact_id="cid-1",
        activity_type="tag_removed",
        summary="Removed tag prospect",
        detail={"tag": "prospect"},
        company_id=None,
    )


def test_tag_remove_not_found(runner: CliRunner, mock_connection: MagicMock) -> None:
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.delete_tag", return_value=False),
    ):
        result = runner.invoke(
            main,
            ["tag", "remove", "--contact-id", "cid-1", "prospect"],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_tag_remove_contact_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=None),
    ):
        result = runner.invoke(
            main, ["tag", "remove", "--contact-id", "cid-missing", "prospect"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "contact" in data["message"]


def test_tag_remove_company_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=None),
    ):
        result = runner.invoke(
            main, ["tag", "remove", "--company-id", "comp-missing", "prospect"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "company" in data["message"]


# -- tag list ------------------------------------------------------------------


def test_tag_list(runner: CliRunner, mock_connection: MagicMock) -> None:
    tags = [
        _make_tag(id="id-1", name="cold"),
        _make_tag(id="id-2", name="prospect"),
    ]
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.list_tags", return_value=tags),
    ):
        result = runner.invoke(main, ["tag", "list", "--contact-id", "cid-1"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert len(data["tags"]) == 2


def test_tag_list_no_entity(runner: CliRunner, mock_connection: MagicMock) -> None:
    """tag list without --contact-id or --company-id should error."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(main, ["tag", "list"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "missing_filter"


def test_tag_list_contact_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=None),
    ):
        result = runner.invoke(main, ["tag", "list", "--contact-id", "cid-missing"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "contact" in data["message"]


def test_tag_list_company_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=None),
    ):
        result = runner.invoke(main, ["tag", "list", "--company-id", "comp-missing"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "company" in data["message"]


# -- tag search ----------------------------------------------------------------


def test_tag_search(runner: CliRunner, mock_connection: MagicMock) -> None:
    tags = [_make_tag(name="prospect")]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.search_tags", return_value=tags) as mock_search,
    ):
        result = runner.invoke(main, ["tag", "search", "prospect"])

    assert result.exit_code == 0
    mock_search.assert_called_once_with(
        mock_connection, name="prospect", entity_type=None, limit=100
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert len(data["tags"]) == 1


def test_tag_search_with_type(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.search_tags", return_value=[]) as mock_search,
    ):
        result = runner.invoke(
            main, ["tag", "search", "prospect", "--type", "contact", "--limit", "5"]
        )

    assert result.exit_code == 0
    mock_search.assert_called_once_with(
        mock_connection, name="prospect", entity_type="contact", limit=5
    )


# -- note helpers --------------------------------------------------------------


def _make_note(**overrides: Any) -> Note:
    defaults: dict[str, Any] = {
        "id": "01234567-0000-7000-0000-000000000012",
        "entity_type": "contact",
        "entity_id": "01234567-0000-7000-0000-000000000003",
        "body": "Test note body",
        "created_at": _NOW,
    }
    return Note(**{**defaults, **overrides})


# -- note add ------------------------------------------------------------------


def test_note_add(runner: CliRunner, mock_connection: MagicMock) -> None:
    note = _make_note()
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_note", return_value=note) as mock_create,
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.create_activity"),
    ):
        result = runner.invoke(
            main,
            ["note", "add", "--contact-id", "cid-1", "--body", "Test note body"],
        )

    assert result.exit_code == 0
    mock_create.assert_called_once_with(
        mock_connection,
        entity_type="contact",
        entity_id="cid-1",
        body="Test note body",
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["body"] == "Test note body"


def test_note_add_on_company(runner: CliRunner, mock_connection: MagicMock) -> None:
    note = _make_note(entity_type="company", entity_id="comp-1")
    company = _make_company(id="comp-1")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_note", return_value=note),
        patch("mailpilot.database.get_company", return_value=company),
    ):
        result = runner.invoke(
            main,
            ["note", "add", "--company-id", "comp-1", "--body", "Company note"],
        )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True


def test_note_add_creates_activity(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    note = _make_note()
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_note", return_value=note),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.create_activity") as mock_activity,
    ):
        runner.invoke(
            main,
            ["note", "add", "--contact-id", "cid-1", "--body", "Test note body"],
        )

    mock_activity.assert_called_once_with(
        mock_connection,
        contact_id="cid-1",
        activity_type="note_added",
        summary="Note added",
        detail={"note_id": note.id},
        company_id=None,
    )


def test_note_add_contact_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=None),
    ):
        result = runner.invoke(
            main,
            ["note", "add", "--contact-id", "cid-missing", "--body", "Some note"],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "contact" in data["message"]


def test_note_add_company_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=None),
    ):
        result = runner.invoke(
            main,
            ["note", "add", "--company-id", "comp-missing", "--body", "Some note"],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "company" in data["message"]


def test_note_add_no_entity(runner: CliRunner, mock_connection: MagicMock) -> None:
    """note add without --contact-id or --company-id should error."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(main, ["note", "add", "--body", "Some note"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "missing_filter"


def test_note_add_empty_body(runner: CliRunner, mock_connection: MagicMock) -> None:
    """note add with empty body should error."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(
            main, ["note", "add", "--contact-id", "cid-1", "--body", ""]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "empty" in data["message"]


def test_note_add_whitespace_body(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """note add with whitespace-only body should error."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(
            main, ["note", "add", "--contact-id", "cid-1", "--body", "   "]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "empty" in data["message"]


def test_note_add_missing_body(runner: CliRunner, mock_connection: MagicMock) -> None:
    """note add without --body should error."""
    result = runner.invoke(main, ["note", "add", "--contact-id", "cid-1"])
    assert result.exit_code != 0


# -- note list -----------------------------------------------------------------


def test_note_list(runner: CliRunner, mock_connection: MagicMock) -> None:
    notes = [
        _make_note(id="id-1", body="First note"),
        _make_note(id="id-2", body="Second note"),
    ]
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.list_notes", return_value=notes),
    ):
        result = runner.invoke(main, ["note", "list", "--contact-id", "cid-1"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert len(data["notes"]) == 2


def test_note_list_with_limit(runner: CliRunner, mock_connection: MagicMock) -> None:
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.list_notes", return_value=[]) as mock_list,
    ):
        result = runner.invoke(
            main, ["note", "list", "--contact-id", "cid-1", "--limit", "5"]
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection, entity_type="contact", entity_id="cid-1", limit=5, since=None
    )


def test_note_list_with_since(runner: CliRunner, mock_connection: MagicMock) -> None:
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.list_notes", return_value=[]) as mock_list,
    ):
        result = runner.invoke(
            main,
            [
                "note",
                "list",
                "--contact-id",
                "cid-1",
                "--since",
                "2024-01-01T00:00:00Z",
            ],
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        entity_type="contact",
        entity_id="cid-1",
        limit=100,
        since="2024-01-01T00:00:00Z",
    )


def test_note_list_no_entity(runner: CliRunner, mock_connection: MagicMock) -> None:
    """note list without --contact-id or --company-id should error."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(main, ["note", "list"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "missing_filter"


# -- note view -----------------------------------------------------------------


def test_note_view(runner: CliRunner, mock_connection: MagicMock) -> None:
    note = _make_note()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_note", return_value=note) as mock_get,
    ):
        result = runner.invoke(main, ["note", "view", note.id])

    assert result.exit_code == 0
    mock_get.assert_called_once_with(mock_connection, note.id)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["id"] == note.id
    assert data["body"] == "Test note body"


def test_note_view_not_found(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_note", return_value=None),
    ):
        result = runner.invoke(main, ["note", "view", "nonexistent-id"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "note" in data["message"]


def test_note_list_contact_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=None),
    ):
        result = runner.invoke(main, ["note", "list", "--contact-id", "cid-missing"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "contact" in data["message"]


def test_note_list_company_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=None),
    ):
        result = runner.invoke(main, ["note", "list", "--company-id", "comp-missing"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "company" in data["message"]


# -- Workflow Contact commands -------------------------------------------------


def _make_workflow_contact(**overrides: Any) -> WorkflowContact:
    defaults: dict[str, Any] = {
        "workflow_id": _WORKFLOW_ID,
        "contact_id": _CONTACT_ID,
        "status": "pending",
        "reason": "",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return WorkflowContact(**{**defaults, **overrides})


def _make_workflow_contact_detail(**overrides: Any) -> WorkflowContactDetail:
    defaults: dict[str, Any] = {
        "workflow_id": _WORKFLOW_ID,
        "contact_id": _CONTACT_ID,
        "contact_email": "alice@example.com",
        "contact_name": "Alice Smith",
        "status": "pending",
        "reason": "",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return WorkflowContactDetail(**{**defaults, **overrides})


# -- workflow contact add ------------------------------------------------------


def test_workflow_contact_add(runner: CliRunner, mock_connection: MagicMock) -> None:
    wc = _make_workflow_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=_make_workflow()),
        patch("mailpilot.database.get_contact", return_value=_make_contact()),
        patch(
            "mailpilot.database.create_workflow_contact", return_value=wc
        ) as mock_create,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "contact",
                "add",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
            ],
        )

    assert result.exit_code == 0
    mock_create.assert_called_once_with(mock_connection, _WORKFLOW_ID, _CONTACT_ID)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["workflow_id"] == _WORKFLOW_ID
    assert data["contact_id"] == _CONTACT_ID
    assert data["status"] == "pending"


def test_workflow_contact_add_idempotent(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """When enrollment already exists, return existing row (no error)."""
    existing = _make_workflow_contact(status="active")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=_make_workflow()),
        patch("mailpilot.database.get_contact", return_value=_make_contact()),
        patch("mailpilot.database.create_workflow_contact", return_value=None),
        patch("mailpilot.database.get_workflow_contact", return_value=existing),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "contact",
                "add",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
            ],
        )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["status"] == "active"


def test_workflow_contact_add_workflow_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=None),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "contact",
                "add",
                "--workflow-id",
                "wf-missing",
                "--contact-id",
                _CONTACT_ID,
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "workflow" in data["message"]


def test_workflow_contact_add_contact_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=_make_workflow()),
        patch("mailpilot.database.get_contact", return_value=None),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "contact",
                "add",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                "cid-missing",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "contact" in data["message"]


# -- workflow contact remove ---------------------------------------------------


def test_workflow_contact_remove(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.delete_workflow_contact", return_value=True
        ) as mock_delete,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "contact",
                "remove",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
            ],
        )

    assert result.exit_code == 0
    mock_delete.assert_called_once_with(mock_connection, _WORKFLOW_ID, _CONTACT_ID)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["workflow_id"] == _WORKFLOW_ID
    assert data["contact_id"] == _CONTACT_ID


def test_workflow_contact_remove_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.delete_workflow_contact", return_value=False),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "contact",
                "remove",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "workflow-contact" in data["message"]


# -- workflow contact list -----------------------------------------------------


def test_workflow_contact_list(runner: CliRunner, mock_connection: MagicMock) -> None:
    detail = _make_workflow_contact_detail()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=_make_workflow()),
        patch(
            "mailpilot.database.list_workflow_contacts_enriched", return_value=[detail]
        ) as mock_list,
    ):
        result = runner.invoke(
            main,
            ["workflow", "contact", "list", "--workflow-id", _WORKFLOW_ID],
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection, _WORKFLOW_ID, status=None, limit=100
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert len(data["contacts"]) == 1
    assert data["contacts"][0]["contact_email"] == "alice@example.com"


def test_workflow_contact_list_with_status(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=_make_workflow()),
        patch(
            "mailpilot.database.list_workflow_contacts_enriched", return_value=[]
        ) as mock_list,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "contact",
                "list",
                "--workflow-id",
                _WORKFLOW_ID,
                "--status",
                "completed",
            ],
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection, _WORKFLOW_ID, status="completed", limit=100
    )


def test_workflow_contact_list_with_limit(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=_make_workflow()),
        patch(
            "mailpilot.database.list_workflow_contacts_enriched", return_value=[]
        ) as mock_list,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "contact",
                "list",
                "--workflow-id",
                _WORKFLOW_ID,
                "--limit",
                "5",
            ],
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection, _WORKFLOW_ID, status=None, limit=5
    )


def test_workflow_contact_list_workflow_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=None),
    ):
        result = runner.invoke(
            main,
            ["workflow", "contact", "list", "--workflow-id", "wf-missing"],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "workflow" in data["message"]


# -- workflow contact update ---------------------------------------------------


def test_workflow_contact_update(runner: CliRunner, mock_connection: MagicMock) -> None:
    updated = _make_workflow_contact(status="completed", reason="Demo booked")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.update_workflow_contact", return_value=updated
        ) as mock_update,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "contact",
                "update",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
                "--status",
                "completed",
                "--reason",
                "Demo booked",
            ],
        )

    assert result.exit_code == 0
    mock_update.assert_called_once_with(
        mock_connection,
        _WORKFLOW_ID,
        _CONTACT_ID,
        status="completed",
        reason="Demo booked",
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["status"] == "completed"
    assert data["reason"] == "Demo booked"


def test_workflow_contact_update_without_reason(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    updated = _make_workflow_contact(status="failed")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.update_workflow_contact", return_value=updated
        ) as mock_update,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "contact",
                "update",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
                "--status",
                "failed",
            ],
        )

    assert result.exit_code == 0
    mock_update.assert_called_once_with(
        mock_connection,
        _WORKFLOW_ID,
        _CONTACT_ID,
        status="failed",
    )


def test_workflow_contact_update_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.update_workflow_contact", return_value=None),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "contact",
                "update",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
                "--status",
                "completed",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


# -- Task CLI ------------------------------------------------------------------

_TASK_ID = "01234567-0000-7000-0000-a00000000001"


def _make_task(**overrides: Any) -> Task:
    defaults: dict[str, Any] = {
        "id": _TASK_ID,
        "workflow_id": _WORKFLOW_ID,
        "contact_id": _CONTACT_ID,
        "email_id": None,
        "description": "follow up",
        "context": {},
        "scheduled_at": _NOW,
        "status": "pending",
        "result": {},
        "completed_at": None,
        "created_at": _NOW,
    }
    return Task(**{**defaults, **overrides})


def test_task_list(runner: CliRunner, mock_connection: MagicMock) -> None:
    tasks = [_make_task()]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_tasks", return_value=tasks) as mock_list,
    ):
        result = runner.invoke(main, ["task", "list"])

    assert result.exit_code == 0, result.output
    mock_list.assert_called_once_with(
        mock_connection, workflow_id=None, contact_id=None, status=None, limit=100
    )
    data = json.loads(result.output)
    assert len(data["tasks"]) == 1


def test_task_list_with_filters(runner: CliRunner, mock_connection: MagicMock) -> None:
    workflow = _make_workflow()
    contact = _make_contact()
    tasks = [_make_task()]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.list_tasks", return_value=tasks) as mock_list,
    ):
        result = runner.invoke(
            main,
            [
                "task",
                "list",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
                "--status",
                "pending",
                "--limit",
                "10",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_list.assert_called_once_with(
        mock_connection,
        workflow_id=_WORKFLOW_ID,
        contact_id=_CONTACT_ID,
        status="pending",
        limit=10,
    )


def test_task_list_workflow_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=None),
    ):
        result = runner.invoke(main, ["task", "list", "--workflow-id", "missing"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


# -- task view -----------------------------------------------------------------


def test_task_view(runner: CliRunner, mock_connection: MagicMock) -> None:
    task_obj = _make_task()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_task", return_value=task_obj),
    ):
        result = runner.invoke(main, ["task", "view", task_obj.id])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["id"] == task_obj.id
    assert data["description"] == "follow up"


def test_task_view_not_found(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_task", return_value=None),
    ):
        result = runner.invoke(main, ["task", "view", "missing"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


# -- task cancel ---------------------------------------------------------------


def test_task_cancel(runner: CliRunner, mock_connection: MagicMock) -> None:
    cancelled = _make_task(status="cancelled")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.cancel_task", return_value=cancelled) as mock_cancel,
    ):
        result = runner.invoke(main, ["task", "cancel", cancelled.id])

    assert result.exit_code == 0, result.output
    mock_cancel.assert_called_once_with(mock_connection, cancelled.id)
    data = json.loads(result.output)
    assert data["status"] == "cancelled"


def test_task_cancel_not_pending(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.cancel_task", return_value=None),
    ):
        result = runner.invoke(main, ["task", "cancel", "some-id"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "not pending" in data["message"]


# -- run command ---------------------------------------------------------------


def test_run_command(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.run.run_loop") as mock_loop,
    ):
        result = runner.invoke(main, ["run"])

    assert result.exit_code == 0, result.output
    assert "Starting execution loop" in result.output
    assert "Ctrl+C to stop" in result.output
    mock_loop.assert_called_once_with(mock_connection, make_test_settings())
