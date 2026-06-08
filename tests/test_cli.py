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
    CompanySummary,
    CompanyView,
    Contact,
    ContactSummary,
    ContactView,
    Email,
    Enrollment,
    EnrollmentSummary,
    Note,
    Tag,
    Task,
    Workflow,
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


@pytest.fixture(autouse=True)
def _silence_operator_event(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """Mute stderr operator_event emission for JSON-envelope-only CLI tests.

    `mailpilot.operator_log.operator_event` writes one line to stderr per
    SPEC §V.54 mutation. Click 8.2+ `result.output` interleaves stdout and
    stderr in write order, so the leading event line would corrupt the
    `json.loads(result.output)` assertions this file is built around.
    The dedicated telemetry tests live in `tests/test_cli_telemetry.py`
    and intentionally do not pull in this fixture.
    """
    monkeypatch.setattr("mailpilot.operator_log.operator_event", lambda *_a, **_k: None)


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


# -- --skill -------------------------------------------------------------------


def test_skill_prints_packaged_body_verbatim() -> None:
    from importlib.resources import files

    expected = files("mailpilot").joinpath("SKILL.md").read_text(encoding="utf-8")
    result = CliRunner().invoke(main, ["--skill"])
    assert result.exit_code == 0
    assert result.stdout == expected
    assert result.stderr == ""


def test_skill_resource_path_resolves() -> None:
    from importlib.resources import files

    skill_path = files("mailpilot").joinpath("SKILL.md")
    assert skill_path.is_file()


def test_skill_missing_file_hard_fails() -> None:
    mock_resource = MagicMock()
    mock_resource.read_text.side_effect = FileNotFoundError("no SKILL.md")
    with patch("importlib.resources.files") as mock_files:
        mock_files.return_value.joinpath.return_value = mock_resource
        result = CliRunner().invoke(main, ["--skill"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "SKILL.md missing" in result.stderr


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
    assert data["account"]["email"] == "test@example.com"
    assert data["account"]["display_name"] == "Test Account"


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


def test_account_list_limit_and_since(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_accounts", return_value=[]) as mock_list,
    ):
        result = runner.invoke(
            main,
            ["account", "list", "--limit", "5", "--since", "2024-01-01T00:00:00"],
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection, limit=5, since="2024-01-01T00:00:00"
    )


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
    assert data["account"]["id"] == account.id


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
    before = _make_account(display_name="Old Name")
    updated = _make_account(display_name="New Name")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=before),
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
    assert data["account"]["display_name"] == "New Name"


def test_account_update_no_fields(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.update_account", return_value=account) as mock_update,
    ):
        result = runner.invoke(main, ["account", "update", account.id])

    assert result.exit_code == 0
    mock_update.assert_called_once_with(mock_connection, account.id)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["account"]["id"] == account.id


def test_account_update_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=None),
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
        patch("mailpilot.database.get_account", side_effect=[acc_a, acc_b]),
        patch("mailpilot.gmail.GmailClient") as mock_client_cls,
        patch("mailpilot.sync.sync_account", side_effect=[3, 5]) as mock_sync,
    ):
        result = runner.invoke(main, ["account", "sync"])

    assert result.exit_code == 0, result.output
    assert mock_client_cls.call_count == 2
    assert mock_sync.call_count == 2
    data = json.loads(result.output)
    assert data["ok"] is True
    assert set(data.keys()) == {"accounts", "ok"}
    assert [r["email"] for r in data["accounts"]] == ["a@example.com", "b@example.com"]
    assert [r["stored"] for r in data["accounts"]] == [3, 5]
    assert sum(r["stored"] for r in data["accounts"]) == 8


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
    assert set(data.keys()) == {"accounts", "ok"}
    assert len(data["accounts"]) == 1
    assert data["accounts"][0]["email"] == "only@example.com"
    assert data["accounts"][0]["stored"] == 2


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
        patch("mailpilot.database.get_account", side_effect=[acc_a, acc_b]),
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
    assert set(data.keys()) == {"accounts", "ok"}
    assert data["accounts"][0]["error"] == "gmail 500"
    assert "stored" not in data["accounts"][0]
    assert data["accounts"][1]["stored"] == 4


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
    assert data["company"]["domain"] == "acme.com"
    assert data["company"]["name"] == "Acme Corp"


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


def test_company_create_with_note_appends_note_atomically(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """`--note STR` writes a note row under the same cli_mutation span."""
    company = _make_company()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_company", return_value=company),
        patch("mailpilot.database.add_company_note") as mock_note,
    ):
        result = runner.invoke(
            main,
            [
                "company",
                "create",
                "--domain",
                "acme.com",
                "--name",
                "Acme",
                "--note",
                "Met at conference.",
            ],
        )

    assert result.exit_code == 0
    mock_note.assert_called_once_with(mock_connection, company.id, "Met at conference.")


def test_company_create_without_note_skips_note_call(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    company = _make_company()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_company", return_value=company),
        patch("mailpilot.database.add_company_note") as mock_note,
    ):
        result = runner.invoke(
            main, ["company", "create", "--domain", "acme.com", "--name", "Acme"]
        )

    assert result.exit_code == 0
    mock_note.assert_not_called()


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
    mock_list.assert_called_once_with(mock_connection, limit=5, since=None)


def test_company_list_with_since(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_companies", return_value=[]) as mock_list,
    ):
        result = runner.invoke(
            main, ["company", "list", "--since", "2024-01-01T00:00:00"]
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection, limit=100, since="2024-01-01T00:00:00"
    )


# -- company view --------------------------------------------------------------


def test_company_view(runner: CliRunner, mock_connection: MagicMock) -> None:
    company = _make_company()
    view = CompanyView(
        id=company.id,
        name=company.name,
        domain=company.domain,
        created_at=company.created_at,
        updated_at=company.updated_at,
        notes=[],
        notes_total=0,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.load_company_view", return_value=view) as mock_load,
    ):
        result = runner.invoke(main, ["company", "view", company.id])

    assert result.exit_code == 0
    mock_load.assert_called_once_with(mock_connection, company.id)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["company"]["id"] == company.id
    assert data["company"]["notes"] == []
    assert data["company"]["notes_total"] == 0


def test_company_view_not_found(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.load_company_view", return_value=None),
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
    before = _make_company(name="Old Name")
    updated = _make_company(name="New Name")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=before),
        patch("mailpilot.database.update_company", return_value=updated) as mock_update,
    ):
        result = runner.invoke(
            main, ["company", "update", updated.id, "--name", "New Name"]
        )

    assert result.exit_code == 0
    mock_update.assert_called_once_with(mock_connection, updated.id, name="New Name")
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["company"]["name"] == "New Name"


def test_company_update_no_fields(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    company = _make_company()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=company),
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
        patch("mailpilot.database.get_company", return_value=None),
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


def test_company_export_envelope_and_file(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: Any
) -> None:
    company_a = _make_company(id="id-1", domain="acme.com")
    company_b = _make_company(id="id-2", domain="beta.com")
    summaries = [
        CompanySummary.model_validate(c.model_dump()) for c in (company_a, company_b)
    ]
    export_file = str(tmp_path / "companies.json")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_companies", return_value=summaries),
        patch("mailpilot.database.get_company", side_effect=[company_a, company_b]),
    ):
        result = runner.invoke(main, ["company", "export", "--file", export_file])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert isinstance(data["companies"], list)
    assert len(data["companies"]) == 2
    assert data["companies"][0]["id"] == "id-1"
    assert "profile_summary" not in data["companies"][0]
    assert data["companies"][0]["domain"] == "acme.com"
    on_disk = json.loads(pathlib.Path(export_file).read_text())
    assert on_disk == data["companies"]


def test_company_export_stdout_only(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    company = _make_company(id="id-1", domain="acme.com")
    summaries = [CompanySummary.model_validate(company.model_dump())]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_companies", return_value=summaries),
        patch("mailpilot.database.get_company", return_value=company),
    ):
        result = runner.invoke(main, ["company", "export"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert len(data["companies"]) == 1


# -- company import ------------------------------------------------------------


def test_company_import_creates_via_file(
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
        patch("mailpilot.database.list_companies", return_value=[]),
        patch(
            "mailpilot.database.create_company",
            side_effect=[_make_company(domain=e["domain"]) for e in entries],
        ) as mock_create,
    ):
        result = runner.invoke(main, ["company", "import", "--file", str(import_file)])

    assert result.exit_code == 0
    assert mock_create.call_count == 2
    mock_create.assert_any_call(mock_connection, name="Acme Corp", domain="acme.com")
    mock_create.assert_any_call(mock_connection, name="Beta Inc", domain="beta.com")
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["companies"] == [
        {"name": "Acme Corp", "action": "created"},
        {"name": "Beta Inc", "action": "created"},
    ]


def test_company_import_via_stdin(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    entries = [{"name": "Acme Corp", "domain": "acme.com"}]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_companies", return_value=[]),
        patch(
            "mailpilot.database.create_company",
            return_value=_make_company(domain="acme.com"),
        ),
    ):
        result = runner.invoke(main, ["company", "import"], input=json.dumps(entries))

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["companies"] == [{"name": "Acme Corp", "action": "created"}]


def test_company_import_per_row_error_continues_batch(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """Duplicate domain on row 1 must not block create on row 2 (§V.63)."""
    existing = [_make_company(id="id-existing", domain="acme.com")]
    existing_summaries = [
        CompanySummary.model_validate(c.model_dump()) for c in existing
    ]
    entries = [
        {"name": "Acme Corp", "domain": "acme.com"},
        {"name": "Beta Inc", "domain": "beta.com"},
    ]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_companies", return_value=existing_summaries),
        patch(
            "mailpilot.database.create_company",
            return_value=_make_company(domain="beta.com"),
        ) as mock_create,
    ):
        result = runner.invoke(main, ["company", "import"], input=json.dumps(entries))

    assert result.exit_code == 0
    mock_create.assert_called_once_with(
        mock_connection, name="Beta Inc", domain="beta.com"
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["companies"][0]["error"] == "duplicate"
    assert data["companies"][0]["name"] == "Acme Corp"
    assert data["companies"][1] == {"name": "Beta Inc", "action": "created"}


def test_company_import_malformed_json(runner: CliRunner) -> None:
    with patch("mailpilot.settings.get_settings", return_value=make_test_settings()):
        result = runner.invoke(main, ["company", "import"], input="not json")

    assert result.exit_code == 1
    err = json.loads(result.output)
    assert err["ok"] is False
    assert err["error"] == "validation_error"


# -- contact helpers -----------------------------------------------------------


def _make_contact(**overrides: Any) -> Contact:
    defaults: dict[str, Any] = {
        "id": "01234567-0000-7000-0000-000000000003",
        "email": "alice@example.com",
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
        first_name="Alice",
        last_name="Smith",
        company_id=None,
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["contact"]["email"] == "alice@example.com"
    assert data["contact"]["first_name"] == "Alice"


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


def test_contact_create_with_note_appends_note_atomically(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """`--note STR` writes a note row under the same cli_mutation span."""
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_contact", return_value=contact),
        patch("mailpilot.database.add_contact_note") as mock_note,
    ):
        result = runner.invoke(
            main,
            [
                "contact",
                "create",
                "--email",
                "alice@example.com",
                "--note",
                "Prospect from web form.",
            ],
        )

    assert result.exit_code == 0
    mock_note.assert_called_once_with(
        mock_connection, contact.id, "Prospect from web form."
    )


def test_contact_create_without_note_skips_note_call(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_contact", return_value=contact),
        patch("mailpilot.database.add_contact_note") as mock_note,
    ):
        result = runner.invoke(
            main, ["contact", "create", "--email", "alice@example.com"]
        )

    assert result.exit_code == 0
    mock_note.assert_not_called()


# -- contact update ------------------------------------------------------------


def test_contact_update_first_name(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    before = _make_contact(first_name="Alice")
    updated = _make_contact(first_name="Alicia")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=before),
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
    assert data["contact"]["first_name"] == "Alicia"


def test_contact_update_no_fields(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=contact),
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
        patch("mailpilot.database.get_contact", return_value=None),
        patch("mailpilot.database.update_contact", return_value=None),
    ):
        result = runner.invoke(
            main, ["contact", "update", "nonexistent-id", "--first-name", "X"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "not_found"


# -- contact disable -----------------------------------------------------------


def test_contact_disable_happy_path(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    before = _make_contact(disabled_reason=None)
    after = _make_contact(disabled_reason="bounced: hard")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=before),
        patch("mailpilot.database.disable_contact", return_value=after) as mock_disable,
    ):
        result = runner.invoke(
            main,
            ["contact", "disable", before.id, "--reason", "bounced: hard"],
        )

    assert result.exit_code == 0, result.output
    mock_disable.assert_called_once_with(mock_connection, before.id, "bounced: hard")
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["contact"]["disabled_reason"] == "bounced: hard"


def test_contact_disable_empty_reason(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact") as mock_get,
        patch("mailpilot.database.disable_contact") as mock_disable,
    ):
        result = runner.invoke(
            main,
            ["contact", "disable", "any-id", "--reason", "   "],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "validation_error"
    mock_get.assert_not_called()
    mock_disable.assert_not_called()


def test_contact_disable_missing_reason(runner: CliRunner) -> None:
    result = runner.invoke(main, ["contact", "disable", "any-id"])
    assert result.exit_code != 0
    assert "--reason" in result.output


def test_contact_disable_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=None),
        patch("mailpilot.database.disable_contact") as mock_disable,
    ):
        result = runner.invoke(
            main,
            ["contact", "disable", "nonexistent-id", "--reason", "spam"],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "not_found"
    mock_disable.assert_not_called()


def test_contact_disable_help_lists_verb(runner: CliRunner) -> None:
    result = runner.invoke(main, ["contact", "--help"])
    assert result.exit_code == 0
    for verb in ("create", "update", "disable", "search", "list", "view"):
        assert verb in result.output


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
                "--company-id",
                "cid-1",
            ],
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        limit=5,
        company_id="cid-1",
        since=None,
        include_disabled=False,
    )


def test_contact_list_include_disabled(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_contacts", return_value=[]) as mock_list,
    ):
        result = runner.invoke(main, ["contact", "list", "--include-disabled"])

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        limit=100,
        company_id=None,
        since=None,
        include_disabled=True,
    )


def test_contact_list_with_since(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_contacts", return_value=[]) as mock_list,
    ):
        result = runner.invoke(
            main, ["contact", "list", "--since", "2024-01-01T00:00:00"]
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        limit=100,
        company_id=None,
        since="2024-01-01T00:00:00",
        include_disabled=False,
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
    view = ContactView(
        id=contact.id,
        email=contact.email,
        company_id=contact.company_id,
        first_name=contact.first_name,
        last_name=contact.last_name,
        disabled_reason=contact.disabled_reason,
        created_at=contact.created_at,
        updated_at=contact.updated_at,
        notes=[],
        notes_total=0,
        company_notes=[],
        company_notes_total=0,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.load_contact_view", return_value=view) as mock_load,
    ):
        result = runner.invoke(main, ["contact", "view", contact.id])

    assert result.exit_code == 0
    mock_load.assert_called_once_with(mock_connection, contact.id)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["contact"]["id"] == contact.id
    assert data["contact"]["notes"] == []
    assert data["contact"]["notes_total"] == 0
    assert data["contact"]["company_notes"] == []
    assert data["contact"]["company_notes_total"] == 0


def test_contact_view_not_found(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.load_contact_view", return_value=None),
    ):
        result = runner.invoke(main, ["contact", "view", "nonexistent-id"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "not_found"


# -- contact export ------------------------------------------------------------


def test_contact_export_envelope_and_file(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: Any
) -> None:
    contact_a = _make_contact(id="id-1", email="alice@acme.com")
    contact_b = _make_contact(id="id-2", email="bob@beta.com")
    summaries = [
        ContactSummary.model_validate(c.model_dump()) for c in (contact_a, contact_b)
    ]
    export_file = str(tmp_path / "contacts.json")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_contacts", return_value=summaries),
        patch("mailpilot.database.get_contact", side_effect=[contact_a, contact_b]),
    ):
        result = runner.invoke(main, ["contact", "export", "--file", export_file])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert isinstance(data["contacts"], list)
    assert len(data["contacts"]) == 2
    assert data["contacts"][0]["id"] == "id-1"
    assert "domain" not in data["contacts"][0]
    assert "disabled_reason" in data["contacts"][0]
    on_disk = json.loads(pathlib.Path(export_file).read_text())
    assert on_disk == data["contacts"]


def test_contact_export_stdout_only(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    contact = _make_contact(id="id-1", email="alice@acme.com")
    summaries = [ContactSummary.model_validate(contact.model_dump())]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_contacts", return_value=summaries),
        patch("mailpilot.database.get_contact", return_value=contact),
    ):
        result = runner.invoke(main, ["contact", "export"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert len(data["contacts"]) == 1


# -- contact import ------------------------------------------------------------


def test_contact_import_creates_via_file(
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
        patch("mailpilot.database.list_contacts", return_value=[]),
        patch(
            "mailpilot.database.create_contact",
            side_effect=[_make_contact(email=e["email"]) for e in entries],
        ) as mock_create,
    ):
        result = runner.invoke(main, ["contact", "import", "--file", str(import_file)])

    assert result.exit_code == 0
    assert mock_create.call_count == 2
    mock_create.assert_any_call(
        mock_connection,
        email="alice@acme.com",
        first_name="Alice",
        last_name="Smith",
        company_id=None,
    )
    mock_create.assert_any_call(
        mock_connection,
        email="bob@beta.com",
        first_name=None,
        last_name=None,
        company_id=None,
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["contacts"] == [
        {"email": "alice@acme.com", "action": "created"},
        {"email": "bob@beta.com", "action": "created"},
    ]


def test_contact_import_via_stdin(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    entries = [{"email": "alice@acme.com"}]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_contacts", return_value=[]),
        patch(
            "mailpilot.database.create_contact",
            return_value=_make_contact(email="alice@acme.com"),
        ),
    ):
        result = runner.invoke(main, ["contact", "import"], input=json.dumps(entries))

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["contacts"] == [{"email": "alice@acme.com", "action": "created"}]


def test_contact_import_per_row_error_continues_batch(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """Duplicate email on row 1 must not block create on row 2 (§V.63)."""
    existing = [_make_contact(id="id-existing", email="alice@acme.com")]
    existing_summaries = [
        ContactSummary.model_validate(c.model_dump()) for c in existing
    ]
    entries = [
        {"email": "alice@acme.com"},
        {"email": "bob@beta.com"},
    ]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_contacts", return_value=existing_summaries),
        patch(
            "mailpilot.database.create_contact",
            return_value=_make_contact(email="bob@beta.com"),
        ) as mock_create,
    ):
        result = runner.invoke(main, ["contact", "import"], input=json.dumps(entries))

    assert result.exit_code == 0
    mock_create.assert_called_once_with(
        mock_connection,
        email="bob@beta.com",
        first_name=None,
        last_name=None,
        company_id=None,
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["contacts"][0]["error"] == "duplicate"
    assert data["contacts"][0]["email"] == "alice@acme.com"
    assert data["contacts"][1] == {"email": "bob@beta.com", "action": "created"}


def test_contact_import_malformed_json(runner: CliRunner) -> None:
    with patch("mailpilot.settings.get_settings", return_value=make_test_settings()):
        result = runner.invoke(main, ["contact", "import"], input="not json")

    assert result.exit_code == 1
    err = json.loads(result.output)
    assert err["ok"] is False
    assert err["error"] == "validation_error"


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
        sender=None,
        recipient=None,
        route_method=None,
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
        sender=None,
        recipient=None,
        route_method=None,
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
        sender=None,
        recipient=None,
        route_method=None,
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
    assert data["email"]["id"] == email.id


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


def test_email_list_body_text_with_newlines_is_valid_json(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """body_text containing newlines must serialize as valid JSON (RFC 8259).

    Regression: defect 3 -- raw \n bytes inside string literals broke
    `python -c json.load` and `jq` for downstream agents.
    """
    body = "line one\nline two\nline three"
    email = _make_email(body_text=body)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_emails", return_value=[email]),
    ):
        result = runner.invoke(main, ["email", "list"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["emails"][0]["body_text"] == body


def test_email_view_body_text_with_newlines_is_valid_json(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """`email view` must escape control characters in body_text (RFC 8259)."""
    body = "line one\nline two\nline three"
    email = _make_email(body_text=body)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_email", return_value=email),
    ):
        result = runner.invoke(main, ["email", "view", email.id])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["email"]["body_text"] == body


def test_output_escapes_all_control_characters() -> None:
    """The `output()` helper must escape every control character so the result
    parses cleanly with `json.loads`. Control chars include \\n, \\r, \\t, \\v
    and arbitrary low bytes such as \\x00 and \\x1c."""
    from mailpilot.cli import output

    runner_local = CliRunner()
    payload = {"body_text": "a\x00b\x01c\nd\re\tf\x0bg\x1ch"}
    with runner_local.isolation() as (out, _err, _mix):
        output(payload)
    raw = out.getvalue().decode("utf-8")
    parsed = json.loads(raw)
    assert parsed["body_text"] == payload["body_text"]


def test_output_preserves_non_ascii_as_utf8() -> None:
    """`ensure_ascii=False` keeps glyphs like em-dashes readable in the JSON
    body instead of `\\u2014`. Output must still parse cleanly."""
    from mailpilot.cli import output

    runner_local = CliRunner()
    payload = {"body_text": "hello \u2014 world"}
    with runner_local.isolation() as (out, _err, _mix):
        output(payload)
    raw = out.getvalue().decode("utf-8")
    assert "\u2014" in raw  # em-dash glyph, not the escaped form
    parsed = json.loads(raw)
    assert parsed["body_text"] == payload["body_text"]


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


def test_email_list_with_from_and_to_filters(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_emails", return_value=[]) as mock_list,
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "list",
                "--from",
                "alice@example.com",
                "--to",
                "bob@example.com",
            ],
        )
    assert result.exit_code == 0
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
        sender="alice@example.com",
        recipient="bob@example.com",
        route_method=None,
    )


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
        patch("mailpilot.email_ops.send_email", return_value=sent) as mock_send,
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
    assert kwargs["workflow_id"] is None
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["email"]["id"] == sent.id
    assert data["email"]["direction"] == "outbound"
    assert data["email"]["status"] == "sent"


def test_email_send_with_workflow_id(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    account = _make_account()
    workflow = _make_workflow(account_id=account.id)
    sent = _make_email(
        direction="outbound",
        status="sent",
        workflow_id=workflow.id,
        sent_at=_NOW,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.gmail.GmailClient"),
        patch("mailpilot.email_ops.send_email", return_value=sent) as mock_send,
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
                "--workflow-id",
                workflow.id,
            ],
        )

    assert result.exit_code == 0, result.output
    kwargs = mock_send.call_args.kwargs
    assert kwargs["workflow_id"] == workflow.id


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
        patch("mailpilot.email_ops.send_email", side_effect=RuntimeError("gmail 500")),
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


def test_email_send_with_cc_and_bcc(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    account = _make_account()
    sent = _make_email(
        direction="outbound",
        status="sent",
        sent_at=_NOW,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.gmail.GmailClient"),
        patch("mailpilot.email_ops.send_email", return_value=sent) as mock_send,
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
                "--cc",
                "cc@example.com",
                "--bcc",
                "bcc@example.com",
            ],
        )

    assert result.exit_code == 0, result.output
    kwargs = mock_send.call_args.kwargs
    assert kwargs["cc"] == "cc@example.com"
    assert kwargs["bcc"] == "bcc@example.com"


def test_email_send_with_multiple_to(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    account = _make_account()
    sent = _make_email(
        direction="outbound",
        status="sent",
        sent_at=_NOW,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.gmail.GmailClient"),
        patch("mailpilot.email_ops.send_email", return_value=sent) as mock_send,
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "send",
                "--account-id",
                account.id,
                "--to",
                "a@example.com",
                "--to",
                "b@example.com",
                "--subject",
                "Hello",
                "--body",
                "Body",
            ],
        )

    assert result.exit_code == 0, result.output
    kwargs = mock_send.call_args.kwargs
    assert kwargs["to"] == "a@example.com,b@example.com"


def test_email_send_contact_disabled_returns_error(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    from mailpilot.email_ops import ContactDisabledError

    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.gmail.GmailClient"),
        patch(
            "mailpilot.email_ops.send_email",
            side_effect=ContactDisabledError("contact is bounced: hard fail"),
        ),
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
    assert data["error"] == "contact_disabled"
    assert "bounced" in data["message"]


def test_email_send_cooldown_returns_error(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    from mailpilot.email_ops import CooldownError

    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.gmail.GmailClient"),
        patch(
            "mailpilot.email_ops.send_email",
            side_effect=CooldownError("last unsolicited email sent ...; cooldown"),
        ),
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
    assert data["error"] == "cooldown"


# -- email reply ---------------------------------------------------------------


def test_email_reply_success(runner: CliRunner, mock_connection: MagicMock) -> None:
    account = _make_account()
    sent = _make_email(
        direction="outbound",
        status="sent",
        subject="Re: Hi",
        body_text="Reply body",
        gmail_message_id="gm-2",
        gmail_thread_id="gt-1",
        sent_at=_NOW,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.gmail.GmailClient") as mock_client_cls,
        patch("mailpilot.email_ops.reply_email", return_value=sent) as mock_reply,
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "reply",
                "--account-id",
                account.id,
                "--email-id",
                "original-email-1",
                "--body",
                "Reply body",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_client_cls.assert_called_once_with(account.email)
    kwargs = mock_reply.call_args.kwargs
    assert kwargs["email_id"] == "original-email-1"
    assert kwargs["body"] == "Reply body"
    assert kwargs["workflow_id"] is None
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["email"]["id"] == sent.id


def test_email_reply_account_not_found(
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
                "reply",
                "--account-id",
                "missing",
                "--email-id",
                "x",
                "--body",
                "b",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_email_reply_empty_body_rejected(runner: CliRunner) -> None:
    result = runner.invoke(
        main,
        [
            "email",
            "reply",
            "--account-id",
            "a",
            "--email-id",
            "e",
            "--body",
            "   ",
        ],
    )
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"


def test_email_reply_original_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    from mailpilot.email_ops import OriginalNotFoundError

    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.gmail.GmailClient"),
        patch(
            "mailpilot.email_ops.reply_email",
            side_effect=OriginalNotFoundError("email not found: x"),
        ),
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "reply",
                "--account-id",
                account.id,
                "--email-id",
                "x",
                "--body",
                "b",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_email_reply_contact_disabled(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    from mailpilot.email_ops import ContactDisabledError

    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.gmail.GmailClient"),
        patch(
            "mailpilot.email_ops.reply_email",
            side_effect=ContactDisabledError("contact is bounced: hard fail"),
        ),
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "reply",
                "--account-id",
                account.id,
                "--email-id",
                "x",
                "--body",
                "b",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "contact_disabled"


def test_email_reply_with_workflow_id(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    account = _make_account()
    workflow = _make_workflow(account_id=account.id)
    sent = _make_email(direction="outbound", status="sent", sent_at=_NOW)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.gmail.GmailClient"),
        patch("mailpilot.email_ops.reply_email", return_value=sent) as mock_reply,
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "reply",
                "--account-id",
                account.id,
                "--email-id",
                "original-1",
                "--body",
                "hi",
                "--workflow-id",
                workflow.id,
            ],
        )

    assert result.exit_code == 0, result.output
    assert mock_reply.call_args.kwargs["workflow_id"] == workflow.id


# -- workflow helpers ----------------------------------------------------------


_WORKFLOW_ID = "01234567-0000-7000-0000-000000000005"
_ACCOUNT_ID = "01234567-0000-7000-0000-000000000001"
_CONTACT_ID = "01234567-0000-7000-0000-000000000006"
_ENROLLMENT_ID = "01234567-0000-7000-0000-000000000007"


def _make_workflow(**overrides: Any) -> Workflow:
    defaults: dict[str, Any] = {
        "id": _WORKFLOW_ID,
        "name": "Demo outreach",
        "template": "outbound-general",
        "type": "outbound",
        "account_id": _ACCOUNT_ID,
        "account_email": "test@example.com",
        "status": "draft",
        "objective": "",
        "instructions": "",
        "theme": "blue",
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
                "--template",
                "outbound-general",
                "--account-id",
                _ACCOUNT_ID,
                "--draft",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_create.assert_called_once_with(
        mock_connection,
        name="Demo outreach",
        template="outbound-general",
        account_id=_ACCOUNT_ID,
        theme="blue",
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["workflow"]["id"] == workflow.id
    assert data["workflow"]["type"] == "outbound"


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
                "--template",
                "outbound-general",
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
    assert data["workflow"]["status"] == "active"


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
                "--template",
                "outbound-general",
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
    assert data["workflow"]["status"] == "active"


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
                "--template",
                "outbound-general",
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
            "--template",
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
                "--template",
                "outbound-general",
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
                "--template",
                "outbound-general",
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
                "--template",
                "outbound-general",
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
    assert data["workflow"]["status"] == "active"


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
                "--template",
                "outbound-general",
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
    assert data["workflow"]["status"] == "draft"


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
                "--template",
                "outbound-general",
                "--account-id",
                _ACCOUNT_ID,
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "--draft" in data["message"]


def test_workflow_create_with_theme(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    workflow = _make_workflow(theme="green")
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
                "Themed",
                "--template",
                "outbound-general",
                "--account-id",
                _ACCOUNT_ID,
                "--theme",
                "green",
                "--draft",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_create.assert_called_once_with(
        mock_connection,
        name="Themed",
        template="outbound-general",
        account_id=_ACCOUNT_ID,
        theme="green",
    )
    data = json.loads(result.output)
    assert data["workflow"]["theme"] == "green"


def test_workflow_create_invalid_theme(
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
                "Bad",
                "--template",
                "outbound-general",
                "--account-id",
                _ACCOUNT_ID,
                "--theme",
                "rainbow",
                "--draft",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "rainbow" in data["message"]


# -- workflow update -----------------------------------------------------------


def test_workflow_update_name(runner: CliRunner, mock_connection: MagicMock) -> None:
    before = _make_workflow(name="Original")
    updated = _make_workflow(name="Renamed")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=before),
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
    assert data["workflow"]["name"] == "Renamed"


def test_workflow_update_with_instructions_file(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    instructions_file = tmp_path / "instructions.md"
    instructions_file.write_text("Reply politely.")
    before = _make_workflow(instructions="")
    updated = _make_workflow(instructions="Reply politely.")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=before),
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
    before = _make_workflow(instructions="")
    updated = _make_workflow(instructions="Be concise.")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=before),
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
        patch("mailpilot.database.get_workflow", return_value=None),
        patch("mailpilot.database.update_workflow", return_value=None),
    ):
        result = runner.invoke(main, ["workflow", "update", "nope", "--name", "X"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_workflow_update_theme(runner: CliRunner, mock_connection: MagicMock) -> None:
    before = _make_workflow(theme="blue")
    updated = _make_workflow(theme="orange")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=before),
        patch(
            "mailpilot.database.update_workflow", return_value=updated
        ) as mock_update,
    ):
        result = runner.invoke(
            main, ["workflow", "update", _WORKFLOW_ID, "--theme", "orange"]
        )

    assert result.exit_code == 0, result.output
    mock_update.assert_called_once_with(mock_connection, _WORKFLOW_ID, theme="orange")
    data = json.loads(result.output)
    assert data["workflow"]["theme"] == "orange"


def test_workflow_update_invalid_theme(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(
            main, ["workflow", "update", _WORKFLOW_ID, "--theme", "rainbow"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "rainbow" in data["message"]


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
        mock_connection,
        account_id=None,
        status=None,
        workflow_type=None,
        template=None,
        limit=100,
        since=None,
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
        mock_connection,
        account_id=_ACCOUNT_ID,
        status=None,
        workflow_type=None,
        template=None,
        limit=100,
        since=None,
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
        mock_connection,
        account_id=None,
        status="active",
        workflow_type="outbound",
        template=None,
        limit=100,
        since=None,
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
    assert data["workflow"]["id"] == _WORKFLOW_ID


def test_workflow_list_envelope_includes_account_email(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.5 parent-NI clause: list row carries ``account_email`` alongside ``account_id``."""
    workflows = [
        _make_workflow(id="id-1", account_email="one@example.com"),
        _make_workflow(id="id-2", account_email="two@example.com"),
    ]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_workflows", return_value=workflows),
    ):
        result = runner.invoke(main, ["workflow", "list"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["workflows"][0]["account_id"] == _ACCOUNT_ID
    assert data["workflows"][0]["account_email"] == "one@example.com"
    assert data["workflows"][1]["account_email"] == "two@example.com"


def test_workflow_view_envelope_includes_account_email(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.5 parent-NI clause (extends to view): full-row envelope carries ``account_email``."""
    workflow = _make_workflow(account_email="owner@parent-ni.test")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
    ):
        result = runner.invoke(main, ["workflow", "view", _WORKFLOW_ID])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["workflow"]["account_id"] == _ACCOUNT_ID
    assert data["workflow"]["account_email"] == "owner@parent-ni.test"


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
    assert data["workflow"]["status"] == "active"


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
    assert data["workflow"]["status"] == "paused"


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


# -- workflow export -----------------------------------------------------------


def test_workflow_export_envelope(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    account = _make_account()
    workflow = _make_workflow(
        objective="Book demos",
        instructions="You are a sales rep.",
        theme="green",
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch(
            "mailpilot.database.list_workflows_full", return_value=[workflow]
        ) as mock_list,
    ):
        result = runner.invoke(
            main, ["workflow", "export", "--account-id", _ACCOUNT_ID]
        )

    assert result.exit_code == 0, result.output
    mock_list.assert_called_once_with(mock_connection, _ACCOUNT_ID)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert isinstance(data["workflows"], list)
    assert len(data["workflows"]) == 1
    row = data["workflows"][0]
    assert set(row.keys()) == {
        "name",
        "template",
        "objective",
        "instructions",
        "theme",
    }
    assert row["name"] == "Demo outreach"
    assert row["template"] == "outbound-general"
    assert row["objective"] == "Book demos"
    assert row["instructions"] == "You are a sales rep."
    assert row["theme"] == "green"


def test_workflow_export_account_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=None),
    ):
        result = runner.invoke(
            main, ["workflow", "export", "--account-id", "acc-missing"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "account" in data["message"]


def test_workflow_export_preserves_db_order(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    account = _make_account()
    ordered = [
        _make_workflow(id="01234567-0000-7000-0000-00000000000a", name="Alpha"),
        _make_workflow(id="01234567-0000-7000-0000-00000000000b", name="Bravo"),
        _make_workflow(id="01234567-0000-7000-0000-00000000000c", name="Charlie"),
    ]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.list_workflows_full", return_value=ordered),
    ):
        result = runner.invoke(
            main, ["workflow", "export", "--account-id", _ACCOUNT_ID]
        )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    names = [row["name"] for row in data["workflows"]]
    assert names == ["Alpha", "Bravo", "Charlie"]


# -- workflow import -----------------------------------------------------------


def _import_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Demo outreach",
        "template": "outbound-general",
        "objective": "Book demos",
        "instructions": "You are a sales rep.",
        "theme": "green",
    }
    return {**base, **overrides}


def test_workflow_import_create_path_activates(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    account = _make_account()
    created = _make_workflow(theme="green")
    file = tmp_path / "wf.json"
    file.write_text(json.dumps([_import_payload()]))
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.list_workflows_full", return_value=[]),
        patch(
            "mailpilot.database.create_workflow", return_value=created
        ) as mock_create,
        patch(
            "mailpilot.database.update_workflow", return_value=created
        ) as mock_update,
        patch(
            "mailpilot.database.activate_workflow", return_value=created
        ) as mock_activate,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "import",
                "--account-id",
                _ACCOUNT_ID,
                "--file",
                str(file),
            ],
        )

    assert result.exit_code == 0, result.output
    mock_create.assert_called_once_with(
        mock_connection,
        name="Demo outreach",
        template="outbound-general",
        account_id=_ACCOUNT_ID,
        theme="green",
    )
    mock_update.assert_called_once_with(
        mock_connection,
        created.id,
        objective="Book demos",
        instructions="You are a sales rep.",
    )
    mock_activate.assert_called_once_with(mock_connection, created.id)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["workflows"] == [{"name": "Demo outreach", "action": "created"}]


def test_workflow_import_create_path_draft_no_activation(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    account = _make_account()
    created = _make_workflow(objective="", instructions="")
    file = tmp_path / "wf.json"
    file.write_text(
        json.dumps([_import_payload(objective="Book demos", instructions="")])
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.list_workflows_full", return_value=[]),
        patch("mailpilot.database.create_workflow", return_value=created),
        patch("mailpilot.database.update_workflow", return_value=created),
        patch("mailpilot.database.activate_workflow") as mock_activate,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "import",
                "--account-id",
                _ACCOUNT_ID,
                "--file",
                str(file),
            ],
        )

    assert result.exit_code == 0, result.output
    mock_activate.assert_not_called()


def test_workflow_import_update_path_diff_only(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    account = _make_account()
    existing = _make_workflow(
        objective="Old objective",
        instructions="Old instructions",
        theme="blue",
        status="active",
    )
    file = tmp_path / "wf.json"
    file.write_text(
        json.dumps(
            [
                _import_payload(
                    objective="New objective",
                    instructions="Old instructions",
                    theme="blue",
                )
            ]
        )
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.list_workflows_full", return_value=[existing]),
        patch("mailpilot.database.create_workflow") as mock_create,
        patch(
            "mailpilot.database.update_workflow", return_value=existing
        ) as mock_update,
        patch("mailpilot.database.activate_workflow") as mock_activate,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "import",
                "--account-id",
                _ACCOUNT_ID,
                "--file",
                str(file),
            ],
        )

    assert result.exit_code == 0, result.output
    mock_create.assert_not_called()
    mock_activate.assert_not_called()
    mock_update.assert_called_once_with(
        mock_connection, existing.id, objective="New objective"
    )
    data = json.loads(result.output)
    assert data["workflows"] == [{"name": "Demo outreach", "action": "updated"}]


def test_workflow_import_unchanged_no_mutation(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    account = _make_account()
    existing = _make_workflow(
        objective="Book demos",
        instructions="You are a sales rep.",
        theme="green",
        status="active",
    )
    file = tmp_path / "wf.json"
    file.write_text(json.dumps([_import_payload()]))
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.list_workflows_full", return_value=[existing]),
        patch("mailpilot.database.create_workflow") as mock_create,
        patch("mailpilot.database.update_workflow") as mock_update,
        patch("mailpilot.database.activate_workflow") as mock_activate,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "import",
                "--account-id",
                _ACCOUNT_ID,
                "--file",
                str(file),
            ],
        )

    assert result.exit_code == 0, result.output
    mock_create.assert_not_called()
    mock_update.assert_not_called()
    mock_activate.assert_not_called()
    data = json.loads(result.output)
    assert data["workflows"] == [{"name": "Demo outreach", "action": "unchanged"}]


def test_workflow_import_template_immutable_row_error(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    account = _make_account()
    existing = _make_workflow(template="inbound-general", type="inbound")
    file = tmp_path / "wf.json"
    file.write_text(
        json.dumps(
            [
                _import_payload(template="outbound-general"),
                _import_payload(name="Other workflow", template="inbound-general"),
            ]
        )
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.list_workflows_full", return_value=[existing]),
        patch(
            "mailpilot.database.create_workflow",
            return_value=_make_workflow(name="Other workflow"),
        ) as mock_create,
        patch("mailpilot.database.update_workflow", return_value=existing),
        patch("mailpilot.database.activate_workflow"),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "import",
                "--account-id",
                _ACCOUNT_ID,
                "--file",
                str(file),
            ],
        )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ok"] is True
    rows = data["workflows"]
    assert len(rows) == 2
    assert rows[0]["name"] == "Demo outreach"
    assert rows[0]["error"] == "template_immutable"
    assert "inbound-general" in rows[0]["message"]
    assert rows[1] == {"name": "Other workflow", "action": "created"}
    mock_create.assert_called_once()


def test_workflow_import_from_stdin(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    account = _make_account()
    created = _make_workflow()
    payload = json.dumps([_import_payload()])
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.list_workflows_full", return_value=[]),
        patch("mailpilot.database.create_workflow", return_value=created),
        patch("mailpilot.database.update_workflow", return_value=created),
        patch("mailpilot.database.activate_workflow", return_value=created),
    ):
        result = runner.invoke(
            main,
            ["workflow", "import", "--account-id", _ACCOUNT_ID],
            input=payload,
        )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["workflows"] == [{"name": "Demo outreach", "action": "created"}]


def test_workflow_import_malformed_json_top_error(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    account = _make_account()
    file = tmp_path / "wf.json"
    file.write_text("{not json")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "import",
                "--account-id",
                _ACCOUNT_ID,
                "--file",
                str(file),
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "JSON" in data["message"] or "json" in data["message"].lower()


def test_workflow_import_payload_not_array(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    account = _make_account()
    file = tmp_path / "wf.json"
    file.write_text(json.dumps({"workflows": []}))
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "import",
                "--account-id",
                _ACCOUNT_ID,
                "--file",
                str(file),
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"


def test_workflow_import_account_not_found(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    file = tmp_path / "wf.json"
    file.write_text(json.dumps([_import_payload()]))
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=None),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "import",
                "--account-id",
                "acc-missing",
                "--file",
                str(file),
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "account" in data["message"]


# -- §V.8 stdin TTY guard -----------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        ("company", "import"),
        ("contact", "import"),
        ("workflow", "import", "--account-id", _ACCOUNT_ID),
    ],
    ids=["company", "contact", "workflow"],
)
def test_import_tty_stdin_errors_without_read(
    runner: CliRunner, command: tuple[str, ...]
) -> None:
    """§V.8: TTY stdin (no --file, no pipe) -> validation_error, no read()."""
    from click.testing import _NamedTextIOWrapper  # pyright: ignore[reportPrivateUsage]

    read_mock = MagicMock(
        side_effect=AssertionError(
            "sys.stdin.read() must not be called when isatty()==True"
        )
    )

    with (
        patch.object(_NamedTextIOWrapper, "isatty", return_value=True),
        patch.object(_NamedTextIOWrapper, "read", read_mock),
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
    ):
        result = runner.invoke(main, list(command))

    assert result.exit_code == 1, result.output
    assert read_mock.call_count == 0
    err = json.loads(result.stderr)
    assert err["ok"] is False
    assert err["error"] == "validation_error"
    assert "no input" in err["message"]
    assert "--file" in err["message"]


# -- enrollment run ------------------------------------------------------------


def test_enrollment_run(runner: CliRunner, mock_connection: MagicMock) -> None:
    """Manual run invokes the agent directly -- no task row, no NOTIFY race.

    Going through ``create_task`` triggers ``pg_notify('task_pending')``,
    which races a parallel ``mailpilot run`` loop for the same task.
    Synchronous CLI runs bypass the queue entirely.
    """
    workflow = _make_workflow(
        status="active",
        objective="Book demo",
        instructions="You are a sales rep.",
    )
    contact = Contact(
        id=_CONTACT_ID,
        email="lead@acme.com",
        created_at=_NOW,
        updated_at=_NOW,
    )
    wc = _make_enrollment()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.get_enrollment_by_id", return_value=wc),
        patch("mailpilot.database.create_task") as mock_create_task,
        patch(
            "mailpilot.agent.invoke_workflow_agent",
            return_value={
                "workflow_id": _WORKFLOW_ID,
                "contact_id": _CONTACT_ID,
                "status": "completed",
                "tool_calls": 2,
                "reasoning": "Sent intro.",
            },
        ) as mock_invoke,
    ):
        result = runner.invoke(
            main,
            ["enrollment", "run", _ENROLLMENT_ID],
        )

    assert result.exit_code == 0, result.output
    mock_invoke.assert_called_once()
    mock_create_task.assert_not_called()
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["status"] == "completed"
    assert data["result"]["reasoning"] == "Sent intro."
    assert data["result"]["tool_calls"] == 2


def test_enrollment_run_enrollment_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_enrollment_by_id", return_value=None),
    ):
        result = runner.invoke(main, ["enrollment", "run", "nope"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_enrollment_run_requires_active(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    workflow = _make_workflow(status="draft")
    wc = _make_enrollment()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_enrollment_by_id", return_value=wc),
        patch("mailpilot.database.get_workflow", return_value=workflow),
    ):
        result = runner.invoke(main, ["enrollment", "run", _ENROLLMENT_ID])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "invalid_state"


def test_enrollment_run_inbound_with_email(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """Inbound manual run forwards the unprocessed email to the agent."""
    workflow = _make_workflow(type="inbound", status="active")
    contact = Contact(
        id=_CONTACT_ID,
        email="lead@acme.com",
        created_at=_NOW,
        updated_at=_NOW,
    )
    wc = _make_enrollment()
    inbound_email = _make_email(
        contact_id=_CONTACT_ID,
        workflow_id=_WORKFLOW_ID,
        direction="inbound",
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.get_enrollment_by_id", return_value=wc),
        patch(
            "mailpilot.database.get_unprocessed_inbound_email",
            return_value=inbound_email,
        ),
        patch(
            "mailpilot.agent.invoke_workflow_agent",
            return_value={
                "workflow_id": _WORKFLOW_ID,
                "contact_id": _CONTACT_ID,
                "status": "completed",
                "tool_calls": 1,
                "reasoning": "Replied to inquiry.",
            },
        ) as mock_invoke,
    ):
        result = runner.invoke(main, ["enrollment", "run", _ENROLLMENT_ID])

    assert result.exit_code == 0, result.output
    mock_invoke.assert_called_once()
    # §V.30: the enrollment_run path passes trigger="enrollment_run" and
    # MUST NOT synthesize a task_description -- prompt framing is owned
    # by the trigger branch in _format_trigger.
    call_kwargs = mock_invoke.call_args[1]
    assert call_kwargs["email"] == inbound_email
    assert call_kwargs["trigger"] == "enrollment_run"
    assert "task_description" not in call_kwargs
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["status"] == "completed"


def test_enrollment_run_inbound_no_email(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """Inbound manual run with no unprocessed email still invokes the agent."""
    workflow = _make_workflow(type="inbound", status="active")
    contact = Contact(
        id=_CONTACT_ID,
        email="lead@acme.com",
        created_at=_NOW,
        updated_at=_NOW,
    )
    wc = _make_enrollment()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.get_enrollment_by_id", return_value=wc),
        patch("mailpilot.database.get_unprocessed_inbound_email", return_value=None),
        patch(
            "mailpilot.agent.invoke_workflow_agent",
            return_value={
                "workflow_id": _WORKFLOW_ID,
                "contact_id": _CONTACT_ID,
                "status": "completed",
                "tool_calls": 1,
                "reasoning": "No new email, reviewed history.",
            },
        ) as mock_invoke,
    ):
        result = runner.invoke(main, ["enrollment", "run", _ENROLLMENT_ID])

    assert result.exit_code == 0, result.output
    mock_invoke.assert_called_once()
    # §V.30: enrollment_run path no longer synthesizes a task_description; the
    # `trigger` arg drives prompt framing.
    call_kwargs = mock_invoke.call_args[1]
    assert call_kwargs["email"] is None
    assert call_kwargs["trigger"] == "enrollment_run"
    assert "task_description" not in call_kwargs
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["status"] == "completed"


def test_enrollment_run_contact_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    workflow = _make_workflow(status="active")
    wc = _make_enrollment()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_enrollment_by_id", return_value=wc),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.get_contact", return_value=None),
    ):
        result = runner.invoke(main, ["enrollment", "run", _ENROLLMENT_ID])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_enrollment_run_paused_enrollment(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """Manual run is rejected when the enrollment is paused."""
    workflow = _make_workflow(status="active")
    contact = Contact(
        id=_CONTACT_ID,
        email="lead@acme.com",
        created_at=_NOW,
        updated_at=_NOW,
    )
    paused = _make_enrollment(status="paused", reason="operator hold")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.get_enrollment_by_id", return_value=paused),
        patch("mailpilot.agent.invoke_workflow_agent") as mock_invoke,
    ):
        result = runner.invoke(main, ["enrollment", "run", _ENROLLMENT_ID])
    assert result.exit_code == 1, result.output
    mock_invoke.assert_not_called()
    data = json.loads(result.output)
    assert data["error"] == "invalid_state"
    assert "paused" in data["message"]


def test_enrollment_run_agent_failed(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """Agent exceptions surface as a failed result envelope."""
    workflow = _make_workflow(status="active")
    contact = Contact(
        id=_CONTACT_ID,
        email="lead@acme.com",
        created_at=_NOW,
        updated_at=_NOW,
    )
    wc = _make_enrollment()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.get_enrollment_by_id", return_value=wc),
        patch(
            "mailpilot.agent.invoke_workflow_agent",
            side_effect=RuntimeError("agent error"),
        ),
    ):
        result = runner.invoke(main, ["enrollment", "run", _ENROLLMENT_ID])
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
        company_id=None,
        activity_type="email_sent",
        summary="Sent intro",
        detail={},
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["activity"]["type"] == "email_sent"


def test_activity_create_company_only(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """Company-only activity rows are allowed (#102 sugg 2)."""
    activity = _make_activity(contact_id=None, company_id="comp-1", type="note_added")
    company = _make_company(id="comp-1")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=company),
        patch("mailpilot.database.create_activity", return_value=activity),
    ):
        result = runner.invoke(
            main,
            [
                "activity",
                "create",
                "--company-id",
                "comp-1",
                "--type",
                "note_added",
                "--summary",
                "Company note",
            ],
        )

    assert result.exit_code == 0


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
        company_id=None,
        activity_type="email_sent",
        summary="Sent intro",
        detail={"email_id": "e-1"},
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
        "contact_id": "01234567-0000-7000-0000-000000000003",
        "company_id": None,
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
        patch("mailpilot.database.add_contact_tag", return_value=tag) as mock_add,
        patch("mailpilot.database.get_contact", return_value=contact),
    ):
        result = runner.invoke(
            main,
            ["tag", "add", "--contact-id", "cid-1", "prospect"],
        )

    assert result.exit_code == 0
    mock_add.assert_called_once_with(
        mock_connection,
        contact_id="cid-1",
        name="prospect",
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["tag"]["name"] == "prospect"


def test_tag_add_on_company(runner: CliRunner, mock_connection: MagicMock) -> None:
    tag = _make_tag(contact_id=None, company_id="comp-1", name="enterprise")
    company = _make_company(id="comp-1")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.add_company_tag", return_value=tag) as mock_add,
        patch("mailpilot.database.get_company", return_value=company),
    ):
        result = runner.invoke(
            main,
            ["tag", "add", "--company-id", "comp-1", "enterprise"],
        )

    assert result.exit_code == 0
    mock_add.assert_called_once_with(
        mock_connection,
        company_id="comp-1",
        name="enterprise",
    )


def test_tag_add_already_exists(runner: CliRunner, mock_connection: MagicMock) -> None:
    contact = _make_contact(id="cid-1")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.add_contact_tag", return_value=None),
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
    assert data["error"] == "validation_error"


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


def test_tag_add_rejects_invalid_name(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=contact),
    ):
        result = runner.invoke(
            main, ["tag", "add", "--contact-id", "cid-1", "hot/lead"]
        )

    assert result.exit_code != 0
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "invalid tag" in data["message"].lower()


# -- tag remove ----------------------------------------------------------------


def test_tag_remove(runner: CliRunner, mock_connection: MagicMock) -> None:
    """§V.4 hard-DELETE branch: payload ≡ natural-identifier projection only.

    §V.54: `remove` operator-event `changed` ≡ relational key set
    (composite key for contact-tag is `(contact_id, name)`).
    Regression guard against §B.33 (pre-delete entity blob leaked into response).
    """
    contact = _make_contact()
    removed = _make_tag(contact_id="cid-1", name="prospect")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.remove_contact_tag", return_value=removed
        ) as mock_delete,
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.operator_log.operator_event") as mock_event,
    ):
        result = runner.invoke(
            main,
            ["tag", "remove", "--contact-id", "cid-1", "prospect"],
        )

    assert result.exit_code == 0
    mock_delete.assert_called_once_with(
        mock_connection,
        contact_id="cid-1",
        name="prospect",
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["tag"] == {
        "name": "prospect",
        "owner_type": "contact",
        "owner_id": "cid-1",
    }
    remove_events = [
        call for call in mock_event.call_args_list if call.args[:1] == ("tag.remove",)
    ]
    assert len(remove_events) == 1
    assert remove_events[0].kwargs == {
        "name": "prospect",
        "owner_type": "contact",
        "owner_id": "cid-1",
        "changed": ["contact_id", "name"],
    }


def test_tag_remove_not_found(runner: CliRunner, mock_connection: MagicMock) -> None:
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.remove_contact_tag", return_value=None),
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
        patch("mailpilot.database.list_tags", return_value=tags) as mock_list,
    ):
        result = runner.invoke(main, ["tag", "list", "--contact-id", "cid-1"])

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        contact_id="cid-1",
        limit=100,
        since=None,
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert len(data["tags"]) == 2


def test_tag_list_limit_and_since(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.list_tags", return_value=[]) as mock_list,
    ):
        result = runner.invoke(
            main,
            [
                "tag",
                "list",
                "--contact-id",
                "cid-1",
                "--limit",
                "5",
                "--since",
                "2024-01-01T00:00:00",
            ],
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        contact_id="cid-1",
        limit=5,
        since="2024-01-01T00:00:00",
    )


def test_tag_list_no_entity(runner: CliRunner, mock_connection: MagicMock) -> None:
    """tag list without --contact-id or --company-id should error."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(main, ["tag", "list"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"


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
        mock_connection, name="prospect", owner=None, limit=100
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
        mock_connection, name="prospect", owner="contact", limit=5
    )


# -- note helpers --------------------------------------------------------------


def _make_note(**overrides: Any) -> Note:
    defaults: dict[str, Any] = {
        "id": "01234567-0000-7000-0000-000000000012",
        "contact_id": "01234567-0000-7000-0000-000000000003",
        "company_id": None,
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
        patch("mailpilot.database.add_contact_note", return_value=note) as mock_create,
        patch("mailpilot.database.get_contact", return_value=contact),
    ):
        result = runner.invoke(
            main,
            ["note", "add", "--contact-id", "cid-1", "--body", "Test note body"],
        )

    assert result.exit_code == 0
    mock_create.assert_called_once_with(
        mock_connection,
        contact_id="cid-1",
        body="Test note body",
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["note"]["body"] == "Test note body"


def test_note_add_on_company(runner: CliRunner, mock_connection: MagicMock) -> None:
    note = _make_note(contact_id=None, company_id="comp-1")
    company = _make_company(id="comp-1")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.add_company_note", return_value=note) as mock_add,
        patch("mailpilot.database.get_company", return_value=company),
    ):
        result = runner.invoke(
            main,
            ["note", "add", "--company-id", "comp-1", "--body", "Company note"],
        )

    assert result.exit_code == 0
    mock_add.assert_called_once_with(
        mock_connection,
        company_id="comp-1",
        body="Company note",
    )
    data = json.loads(result.output)
    assert data["ok"] is True


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
    assert data["error"] == "validation_error"


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
        mock_connection, contact_id="cid-1", limit=5, since=None
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
        contact_id="cid-1",
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
    assert data["error"] == "validation_error"


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
    assert data["note"]["id"] == note.id
    assert data["note"]["body"] == "Test note body"


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


# -- Enrollment commands -------------------------------------------------------


def _make_enrollment(**overrides: Any) -> Enrollment:
    defaults: dict[str, Any] = {
        "id": _ENROLLMENT_ID,
        "workflow_id": _WORKFLOW_ID,
        "workflow_name": "Outbound Campaign",
        "contact_id": _CONTACT_ID,
        "contact_email": "alice@example.com",
        "contact_name": "Alice Smith",
        "status": "active",
        "reason": "",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Enrollment(**{**defaults, **overrides})


def _make_enrollment_summary(**overrides: Any) -> EnrollmentSummary:
    defaults: dict[str, Any] = {
        "id": _ENROLLMENT_ID,
        "workflow_id": _WORKFLOW_ID,
        "workflow_name": "Outbound Campaign",
        "contact_id": _CONTACT_ID,
        "contact_email": "alice@example.com",
        "contact_name": "Alice Smith",
        "status": "active",
        "updated_at": _NOW,
    }
    return EnrollmentSummary(**{**defaults, **overrides})


# -- enrollment add ------------------------------------------------------------


def test_enrollment_add(runner: CliRunner, mock_connection: MagicMock) -> None:
    enrollment = _make_enrollment()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=_make_workflow()),
        patch("mailpilot.database.get_contact", return_value=_make_contact()),
        patch("mailpilot.database.get_account", return_value=_make_account()),
        patch(
            "mailpilot.database.create_enrollment", return_value=enrollment
        ) as mock_create,
        patch("mailpilot.database.create_activity") as mock_activity,
    ):
        result = runner.invoke(
            main,
            [
                "enrollment",
                "add",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
            ],
        )

    assert result.exit_code == 0
    mock_create.assert_called_once_with(mock_connection, _WORKFLOW_ID, _CONTACT_ID)
    activity_kwargs = mock_activity.call_args.kwargs
    assert activity_kwargs["activity_type"] == "enrollment_added"
    assert activity_kwargs["contact_id"] == _CONTACT_ID
    assert activity_kwargs["workflow_id"] == _WORKFLOW_ID
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["enrollment"]["workflow_id"] == _WORKFLOW_ID
    assert data["enrollment"]["contact_id"] == _CONTACT_ID
    assert data["enrollment"]["status"] == "active"


def test_enrollment_add_idempotent(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """When enrollment already exists, return existing row (no error)."""
    existing = _make_enrollment(status="active")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=_make_workflow()),
        patch("mailpilot.database.get_contact", return_value=_make_contact()),
        patch("mailpilot.database.get_account", return_value=_make_account()),
        patch("mailpilot.database.create_enrollment", return_value=None),
        patch("mailpilot.database.get_enrollment", return_value=existing),
        patch("mailpilot.database.create_activity") as mock_activity,
    ):
        result = runner.invoke(
            main,
            [
                "enrollment",
                "add",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
            ],
        )

    assert result.exit_code == 0
    mock_activity.assert_not_called()
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["enrollment"]["status"] == "active"


def test_enrollment_add_with_scheduled_at_outbound_creates_task(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.32: --scheduled-at on outbound wf inserts a first-touch task row."""
    enrollment = _make_enrollment()
    workflow = _make_workflow(type="outbound")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.get_contact", return_value=_make_contact()),
        patch("mailpilot.database.get_account", return_value=_make_account()),
        patch("mailpilot.database.create_enrollment", return_value=enrollment),
        patch("mailpilot.database.create_activity"),
        patch(
            "mailpilot.database.find_pending_first_touch_task", return_value=None
        ) as mock_find,
        patch("mailpilot.database.create_task") as mock_create_task,
    ):
        result = runner.invoke(
            main,
            [
                "enrollment",
                "add",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
                "--scheduled-at",
                "2026-06-01T10:00:00+00:00",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_find.assert_called_once_with(mock_connection, _ENROLLMENT_ID)
    create_kwargs = mock_create_task.call_args.kwargs
    assert create_kwargs["enrollment_id"] == _ENROLLMENT_ID
    assert create_kwargs["workflow_id"] == _WORKFLOW_ID
    assert create_kwargs["contact_id"] == _CONTACT_ID
    assert create_kwargs["description"] == "scheduled first reach-out"
    assert create_kwargs["scheduled_at"] == "2026-06-01T10:00:00+00:00"
    assert create_kwargs["context"] == {"trigger": "enrollment_schedule"}
    assert create_kwargs["email_id"] is None
    data = json.loads(result.output)
    assert data["enrollment"]["workflow_id"] == _WORKFLOW_ID


def test_enrollment_add_with_scheduled_at_idempotent(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.32 idempotency: re-running w/ scheduled-at on enrollment that already
    has a pending first-touch task does not insert a duplicate."""
    existing_enrollment = _make_enrollment()
    existing_task = Task(
        id="01234567-0000-7000-0000-aaaaaaaaaaaa",
        enrollment_id=_ENROLLMENT_ID,
        workflow_id=_WORKFLOW_ID,
        contact_id=_CONTACT_ID,
        email_id=None,
        description="scheduled first reach-out",
        context={"trigger": "enrollment_schedule"},
        scheduled_at=_NOW,
        status="pending",
        result={},
        completed_at=None,
        created_at=_NOW,
    )
    workflow = _make_workflow(type="outbound")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.get_contact", return_value=_make_contact()),
        patch("mailpilot.database.get_account", return_value=_make_account()),
        patch("mailpilot.database.create_enrollment", return_value=None),
        patch("mailpilot.database.get_enrollment", return_value=existing_enrollment),
        patch(
            "mailpilot.database.find_pending_first_touch_task",
            return_value=existing_task,
        ),
        patch("mailpilot.database.create_task") as mock_create_task,
    ):
        result = runner.invoke(
            main,
            [
                "enrollment",
                "add",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
                "--scheduled-at",
                "2026-06-01T10:00:00+00:00",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_create_task.assert_not_called()
    data = json.loads(result.output)
    assert data["enrollment"]["workflow_id"] == _WORKFLOW_ID


def test_enrollment_add_scheduled_at_inbound_rejected(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.32: --scheduled-at on inbound wf -> invalid_state error envelope."""
    workflow = _make_workflow(type="inbound", template="inbound-general")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.create_enrollment") as mock_create_enroll,
        patch("mailpilot.database.create_task") as mock_create_task,
    ):
        result = runner.invoke(
            main,
            [
                "enrollment",
                "add",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
                "--scheduled-at",
                "2026-06-01T10:00:00+00:00",
            ],
        )

    assert result.exit_code == 1
    mock_create_enroll.assert_not_called()
    mock_create_task.assert_not_called()
    data = json.loads(result.output)
    assert data["error"] == "invalid_state"
    assert "--scheduled-at" in data["message"]


def test_enrollment_add_workflow_not_found(
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
                "enrollment",
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


def test_enrollment_add_contact_not_found(
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
                "enrollment",
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


def test_enrollment_add_self_loop_outbound_rejected(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.33: outbound wf + contact.email == account.email -> self_loop."""
    account = _make_account(email="hello@lab5.ca")
    workflow = _make_workflow(account_id=account.id)
    contact = _make_contact(email="hello@lab5.ca")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.create_enrollment") as mock_create,
        patch("mailpilot.database.create_activity") as mock_activity,
        patch("mailpilot.operator_log.operator_event") as mock_event,
    ):
        result = runner.invoke(
            main,
            [
                "enrollment",
                "add",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
            ],
        )

    assert result.exit_code == 1
    mock_create.assert_not_called()
    mock_activity.assert_not_called()
    assert not any(
        call.args and call.args[0] == "enrollment.add"
        for call in mock_event.call_args_list
    )
    data = json.loads(result.output)
    assert data["error"] == "self_loop"
    assert "hello@lab5.ca" in data["message"]
    assert "account email" in data["message"]


def test_enrollment_add_self_loop_inbound_rejected(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.33: inbound wf same rule applies -- direction-agnostic."""
    account = _make_account(email="hello@lab5.ca")
    workflow = _make_workflow(
        account_id=account.id, type="inbound", template="inbound-general"
    )
    contact = _make_contact(email="hello@lab5.ca")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.create_enrollment") as mock_create,
    ):
        result = runner.invoke(
            main,
            [
                "enrollment",
                "add",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
            ],
        )

    assert result.exit_code == 1
    mock_create.assert_not_called()
    data = json.loads(result.output)
    assert data["error"] == "self_loop"


def test_enrollment_add_self_loop_case_insensitive(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.33: comparison is case-insensitive (Gmail addresses)."""
    account = _make_account(email="hello@lab5.ca")
    workflow = _make_workflow(account_id=account.id)
    contact = _make_contact(email="HELLO@lab5.ca")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.create_enrollment") as mock_create,
    ):
        result = runner.invoke(
            main,
            [
                "enrollment",
                "add",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
            ],
        )

    assert result.exit_code == 1
    mock_create.assert_not_called()
    data = json.loads(result.output)
    assert data["error"] == "self_loop"


# -- enrollment disable --------------------------------------------------------


def test_enrollment_disable(runner: CliRunner, mock_connection: MagicMock) -> None:
    """§V.10(+) enrollment coverage: soft-disable returns full updated row.

    §V.4: row retained ∴ full Enrollment model in singular envelope (⊥ hard-
    DELETE composite-key projection). §V.54: `changed` = ['status',
    'disabled_reason'] on first disable.
    """
    before = _make_enrollment(status="active", disabled_reason=None)
    after = _make_enrollment(status="disabled", disabled_reason="left company")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_enrollment_by_id", return_value=before),
        patch(
            "mailpilot.database.disable_enrollment", return_value=after
        ) as mock_disable,
        patch("mailpilot.operator_log.operator_event") as mock_event,
    ):
        result = runner.invoke(
            main,
            [
                "enrollment",
                "disable",
                _ENROLLMENT_ID,
                "--reason",
                "left company",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_disable.assert_called_once_with(
        mock_connection, _ENROLLMENT_ID, "left company"
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["enrollment"]["id"] == _ENROLLMENT_ID
    assert data["enrollment"]["status"] == "disabled"
    assert data["enrollment"]["disabled_reason"] == "left company"
    disable_events = [
        call
        for call in mock_event.call_args_list
        if call.args[:1] == ("enrollment.disable",)
    ]
    assert len(disable_events) == 1
    assert disable_events[0].kwargs == {
        "entity_id": _ENROLLMENT_ID,
        "changed": ["status", "disabled_reason"],
    }


def test_enrollment_disable_idempotent_re_invoke(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """Re-disabling with the same reason yields empty `changed` list."""
    before = _make_enrollment(status="disabled", disabled_reason="left company")
    after = _make_enrollment(status="disabled", disabled_reason="left company")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_enrollment_by_id", return_value=before),
        patch("mailpilot.database.disable_enrollment", return_value=after),
        patch("mailpilot.operator_log.operator_event") as mock_event,
    ):
        result = runner.invoke(
            main,
            [
                "enrollment",
                "disable",
                _ENROLLMENT_ID,
                "--reason",
                "left company",
            ],
        )

    assert result.exit_code == 0, result.output
    disable_events = [
        call
        for call in mock_event.call_args_list
        if call.args[:1] == ("enrollment.disable",)
    ]
    assert len(disable_events) == 1
    assert disable_events[0].kwargs == {
        "entity_id": _ENROLLMENT_ID,
        "changed": [],
    }


def test_enrollment_disable_rejects_empty_reason(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(
            main, ["enrollment", "disable", _ENROLLMENT_ID, "--reason", "   "]
        )
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "reason" in data["message"]


def test_enrollment_disable_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_enrollment_by_id", return_value=None),
    ):
        result = runner.invoke(
            main,
            ["enrollment", "disable", _ENROLLMENT_ID, "--reason", "left company"],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "enrollment" in data["message"]


# -- enrollment view -----------------------------------------------------------


def test_enrollment_view_returns_record(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    enrollment = _make_enrollment()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.get_enrollment_by_id", return_value=enrollment
        ) as mock_get,
    ):
        result = runner.invoke(main, ["enrollment", "view", _ENROLLMENT_ID])

    assert result.exit_code == 0
    mock_get.assert_called_once_with(mock_connection, _ENROLLMENT_ID)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["enrollment"]["id"] == _ENROLLMENT_ID
    assert data["enrollment"]["workflow_id"] == _WORKFLOW_ID
    assert data["enrollment"]["contact_id"] == _CONTACT_ID


def test_enrollment_view_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_enrollment_by_id", return_value=None),
    ):
        result = runner.invoke(main, ["enrollment", "view", _ENROLLMENT_ID])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_enrollment_view_parent_denorm_matches_list(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.5: ``enrollment view`` envelope inner-dict ⊇ ``enrollment list`` row
    on parent-denorm set {workflow_id, workflow_name, contact_id,
    contact_email, contact_name}.

    Regression test for §B.49 — view returned bare Enrollment (raw FKs only)
    while list returned summary w/ denorm, inverting the usual drill-down
    direction (operator scans list, sees parent context, runs view to dig
    deeper, loses context).
    """
    enrollment = _make_enrollment()
    summary = _make_enrollment_summary()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=_make_workflow()),
        patch("mailpilot.database.list_enrollments_detailed", return_value=[summary]),
        patch("mailpilot.database.get_enrollment_by_id", return_value=enrollment),
    ):
        list_result = runner.invoke(
            main,
            ["enrollment", "list", "--workflow-id", _WORKFLOW_ID],
        )
        view_result = runner.invoke(main, ["enrollment", "view", _ENROLLMENT_ID])

    assert list_result.exit_code == 0
    assert view_result.exit_code == 0
    list_row = json.loads(list_result.output)["enrollments"][0]
    view_row = json.loads(view_result.output)["enrollment"]
    parent_denorm_fields = {
        "workflow_id",
        "workflow_name",
        "contact_id",
        "contact_email",
        "contact_name",
    }
    for field in parent_denorm_fields:
        assert field in list_row, f"list row missing parent-denorm field {field!r}"
        assert field in view_row, f"view row missing parent-denorm field {field!r}"
        assert list_row[field] == view_row[field], (
            f"parent-denorm field {field!r} differs between list and view"
        )


# -- enrollment list -----------------------------------------------------------


def test_enrollment_list(runner: CliRunner, mock_connection: MagicMock) -> None:
    summary = _make_enrollment_summary()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=_make_workflow()),
        patch(
            "mailpilot.database.list_enrollments_detailed", return_value=[summary]
        ) as mock_list,
    ):
        result = runner.invoke(
            main,
            ["enrollment", "list", "--workflow-id", _WORKFLOW_ID],
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        workflow_id=_WORKFLOW_ID,
        contact_id=None,
        status=None,
        limit=100,
        since=None,
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert len(data["enrollments"]) == 1
    assert data["enrollments"][0]["contact_email"] == "alice@example.com"
    assert "reason" not in data["enrollments"][0]
    assert "created_at" not in data["enrollments"][0]


def test_enrollment_list_with_status(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=_make_workflow()),
        patch(
            "mailpilot.database.list_enrollments_detailed", return_value=[]
        ) as mock_list,
    ):
        result = runner.invoke(
            main,
            [
                "enrollment",
                "list",
                "--workflow-id",
                _WORKFLOW_ID,
                "--status",
                "paused",
            ],
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        workflow_id=_WORKFLOW_ID,
        contact_id=None,
        status="paused",
        limit=100,
        since=None,
    )


def test_enrollment_list_with_limit(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=_make_workflow()),
        patch(
            "mailpilot.database.list_enrollments_detailed", return_value=[]
        ) as mock_list,
    ):
        result = runner.invoke(
            main,
            [
                "enrollment",
                "list",
                "--workflow-id",
                _WORKFLOW_ID,
                "--limit",
                "5",
            ],
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        workflow_id=_WORKFLOW_ID,
        contact_id=None,
        status=None,
        limit=5,
        since=None,
    )


def test_enrollment_list_filters_by_contact(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=_make_contact()),
        patch(
            "mailpilot.database.list_enrollments_detailed", return_value=[]
        ) as mock_list,
    ):
        result = runner.invoke(
            main,
            ["enrollment", "list", "--contact-id", _CONTACT_ID],
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        workflow_id=None,
        contact_id=_CONTACT_ID,
        status=None,
        limit=100,
        since=None,
    )


def test_enrollment_list_workflow_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=None),
    ):
        result = runner.invoke(
            main,
            ["enrollment", "list", "--workflow-id", "wf-missing"],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "workflow" in data["message"]


# -- enrollment update ---------------------------------------------------------


def test_enrollment_update_paused_emits_activity(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    before = _make_enrollment(status="active")
    updated = _make_enrollment(status="paused", reason="vacation")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_enrollment_by_id", return_value=before),
        patch(
            "mailpilot.database.update_enrollment", return_value=updated
        ) as mock_update,
        patch("mailpilot.database.get_contact", return_value=_make_contact()),
        patch("mailpilot.database.create_activity") as mock_activity,
    ):
        result = runner.invoke(
            main,
            [
                "enrollment",
                "update",
                _ENROLLMENT_ID,
                "--status",
                "paused",
                "--reason",
                "vacation",
            ],
        )

    assert result.exit_code == 0
    mock_update.assert_called_once_with(
        mock_connection,
        _ENROLLMENT_ID,
        status="paused",
        reason="vacation",
    )
    activity_kwargs = mock_activity.call_args.kwargs
    assert activity_kwargs["activity_type"] == "enrollment_paused"
    assert activity_kwargs["summary"] == "vacation"
    assert activity_kwargs["workflow_id"] == _WORKFLOW_ID
    assert activity_kwargs["enrollment_id"] == _ENROLLMENT_ID


def test_enrollment_update_active_emits_resumed_activity(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """paused -> active transition emits enrollment_resumed."""
    before = _make_enrollment(status="paused")
    updated = _make_enrollment(status="active")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_enrollment_by_id", return_value=before),
        patch("mailpilot.database.update_enrollment", return_value=updated),
        patch("mailpilot.database.get_contact", return_value=_make_contact()),
        patch("mailpilot.database.create_activity") as mock_activity,
    ):
        result = runner.invoke(
            main,
            ["enrollment", "update", _ENROLLMENT_ID, "--status", "active"],
        )

    assert result.exit_code == 0
    assert mock_activity.call_args.kwargs["activity_type"] == "enrollment_resumed"


def test_enrollment_update_unchanged_status_does_not_emit_activity(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """No status change -- no activity."""
    before = _make_enrollment(status="active")
    updated = _make_enrollment(status="active")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_enrollment_by_id", return_value=before),
        patch("mailpilot.database.update_enrollment", return_value=updated),
        patch("mailpilot.database.create_activity") as mock_activity,
    ):
        result = runner.invoke(
            main,
            ["enrollment", "update", _ENROLLMENT_ID, "--status", "active"],
        )

    assert result.exit_code == 0
    mock_activity.assert_not_called()


def test_enrollment_update_rejects_legacy_status(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """`completed`/`failed`/`pending` are no longer valid CLI statuses."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(
            main,
            ["enrollment", "update", _ENROLLMENT_ID, "--status", "completed"],
        )

    assert result.exit_code != 0


def test_enrollment_update_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_enrollment_by_id", return_value=None),
    ):
        result = runner.invoke(
            main,
            ["enrollment", "update", _ENROLLMENT_ID, "--status", "active"],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


# -- Task CLI ------------------------------------------------------------------

_TASK_ID = "01234567-0000-7000-0000-a00000000001"


def _make_task(**overrides: Any) -> Task:
    defaults: dict[str, Any] = {
        "id": _TASK_ID,
        "enrollment_id": _ENROLLMENT_ID,
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
        mock_connection,
        workflow_id=None,
        contact_id=None,
        status=None,
        limit=100,
        since=None,
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
        since=None,
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
    assert data["task"]["id"] == task_obj.id
    assert data["task"]["description"] == "follow up"


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
    assert data["task"]["status"] == "cancelled"


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


# -- task retry ---------------------------------------------------------------


def test_task_retry_resets_failed_row(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    failed = _make_task(status="failed")
    reset = _make_task(status="pending", attempt_count=0)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_task", return_value=failed),
        patch("mailpilot.database.manual_retry_task", return_value=reset) as mock_retry,
    ):
        result = runner.invoke(main, ["task", "retry", "--task-id", failed.id])

    assert result.exit_code == 0, result.output
    mock_retry.assert_called_once_with(mock_connection, failed.id)
    data = json.loads(result.output)
    assert data["task"]["status"] == "pending"
    assert data["task"]["attempt_count"] == 0


def test_task_retry_not_found(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_task", return_value=None),
    ):
        result = runner.invoke(main, ["task", "retry", "--task-id", "missing"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_task_retry_pending_invalid_state(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    pending = _make_task(status="pending")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_task", return_value=pending),
        patch("mailpilot.database.manual_retry_task", return_value=None),
    ):
        result = runner.invoke(main, ["task", "retry", "--task-id", pending.id])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "invalid_state"


def test_task_retry_completed_invalid_state(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.49: completed rows refuse retry -- replay risks duplicate
    side-effects since tools already fired."""
    completed = _make_task(status="completed")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_task", return_value=completed),
        patch("mailpilot.database.manual_retry_task", return_value=None),
    ):
        result = runner.invoke(main, ["task", "retry", "--task-id", completed.id])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "invalid_state"


# -- run command ---------------------------------------------------------------


def test_run_command(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.sync.start_sync_loop") as mock_loop,
    ):
        result = runner.invoke(main, ["run"])

    assert result.exit_code == 0, result.output
    mock_loop.assert_called_once_with(mock_connection, make_test_settings())


# -- envelope shape contract (SPEC §V.4) --------------------------------------


def test_envelope_view_wraps_under_singular_key(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """`<entity> view` MUST emit `{"<singular>": {...}, "ok": true}`."""
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
    ):
        result = runner.invoke(main, ["account", "view", account.id])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert set(data.keys()) == {"account", "ok"}
    assert data["account"]["id"] == account.id


def test_envelope_create_wraps_under_singular_key(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """`<entity> create` MUST emit `{"<singular>": {...}, "ok": true}`."""
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_account", return_value=account),
    ):
        result = runner.invoke(
            main, ["account", "create", "--email", "test@example.com"]
        )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert set(data.keys()) == {"account", "ok"}
    assert data["account"]["email"] == account.email


def test_envelope_update_wraps_under_singular_key(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """`<entity> update` MUST emit `{"<singular>": {...}, "ok": true}`."""
    before = _make_account(display_name="Original")
    account = _make_account(display_name="Renamed")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=before),
        patch("mailpilot.database.update_account", return_value=account),
    ):
        result = runner.invoke(
            main, ["account", "update", account.id, "--display-name", "Renamed"]
        )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert set(data.keys()) == {"account", "ok"}
    assert data["account"]["display_name"] == "Renamed"


def test_envelope_list_wraps_under_plural_key(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """`<entity> list` MUST emit `{"<plural>": [...], "ok": true}` (symmetric with view)."""
    accounts = [_make_account(id="01234567-0000-7000-0000-0000000000a1")]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_accounts", return_value=accounts),
    ):
        result = runner.invoke(main, ["account", "list"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert set(data.keys()) == {"accounts", "ok"}
    assert isinstance(data["accounts"], list)


# -- template list / view ------------------------------------------------------


def test_template_list(runner: CliRunner) -> None:
    """`template list` returns all 3 templates with summary fields."""
    result = runner.invoke(main, ["template", "list"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ok"] is True
    assert set(data.keys()) == {"templates", "ok"}
    names = {t["name"] for t in data["templates"]}
    assert names == {"outbound-general", "inbound-general", "inbound-google-drive"}
    for tpl in data["templates"]:
        assert set(tpl.keys()) == {"name", "direction", "description", "tool_count"}
        assert tpl["tool_count"] >= 1


def test_template_list_filter_by_direction_inbound(runner: CliRunner) -> None:
    """`--direction inbound` returns only inbound templates."""
    result = runner.invoke(main, ["template", "list", "--direction", "inbound"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    names = {t["name"] for t in data["templates"]}
    assert names == {"inbound-general", "inbound-google-drive"}


def test_template_list_filter_by_direction_outbound(runner: CliRunner) -> None:
    """`--direction outbound` returns only outbound templates."""
    result = runner.invoke(main, ["template", "list", "--direction", "outbound"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    names = {t["name"] for t in data["templates"]}
    assert names == {"outbound-general"}


def test_template_view_returns_full_record(runner: CliRunner) -> None:
    """`template view <name>` returns name, direction, description, tools, protocol."""
    result = runner.invoke(main, ["template", "view", "inbound-google-drive"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ok"] is True
    assert set(data.keys()) == {"template", "ok"}
    record = data["template"]
    assert record["name"] == "inbound-google-drive"
    assert record["direction"] == "inbound"
    assert isinstance(record["protocol"], str)
    assert record["protocol"]
    assert "search_drive_markdown" in record["tools"]
    assert "list_drive_markdown" in record["tools"]
    assert "read_drive_markdown" in record["tools"]


def test_template_view_unknown_returns_not_found(runner: CliRunner) -> None:
    """Unknown template name -> error envelope with not_found code."""
    result = runner.invoke(main, ["template", "view", "made-up-template"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "not_found"
