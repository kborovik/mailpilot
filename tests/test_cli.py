"""CLI tests for account and company subcommands."""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime
from typing import Any, NoReturn
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
    ContactView,
    Email,
    Enrollment,
    EnrollmentSummary,
    Meeting,
    MeetingAttendee,
    MeetingSummary,
    MeetingView,
    Note,
    SchemaStatus,
    Tag,
    TagAssignment,
    TagSummary,
    Task,
    TaskStats,
    Workflow,
    WorkflowCheck,
    WorkflowCheckEntry,
    WorkflowStats,
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


def test_version(runner: CliRunner) -> None:
    from importlib.metadata import version

    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert version("mailpilot-crm") in result.output


def test_version_wheel_install_renders_plain_version() -> None:
    from mailpilot import cli

    dist = MagicMock()
    dist.version = "1.2.3"
    dist.read_text.return_value = None
    with patch.object(cli, "distribution", return_value=dist):
        assert cli._version() == "1.2.3"  # pyright: ignore[reportPrivateUsage]


def test_version_editable_install_renders_dev_marker() -> None:
    from mailpilot import cli

    dist = MagicMock()
    dist.version = "1.2.3"
    dist.read_text.return_value = json.dumps(
        {"url": "file:///tmp/checkout", "dir_info": {"editable": True}}
    )
    with patch.object(cli, "distribution", return_value=dist):
        assert cli._version() == "1.2.3+dev (/tmp/checkout)"  # pyright: ignore[reportPrivateUsage]


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


# -- record_count envelope (§V.4) -----------------------------------------------


def test_record_count_list_envelope(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """Array-bearing payload carries record_count == array len (§V.4)."""
    accounts = [
        _make_account(id="01234567-0000-7000-0000-000000000001", email="a@example.com"),
        _make_account(id="01234567-0000-7000-0000-000000000002", email="b@example.com"),
    ]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_accounts", return_value=accounts),
    ):
        result = runner.invoke(main, ["account", "list"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["record_count"] == 2


def test_record_count_empty_list_envelope(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """Empty array-bearing payload carries record_count == 0 (§V.4)."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_accounts", return_value=[]),
    ):
        result = runner.invoke(main, ["account", "list"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["record_count"] == 0


def test_record_count_single_entity_envelope(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """Single-object payload carries record_count == 1 (§V.4)."""
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
    ):
        result = runner.invoke(main, ["account", "view", account.id])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["record_count"] == 1


def test_record_count_multi_key_payload_is_one(runner: CliRunner) -> None:
    """Multi-key payload (config get KEY) counts as one record, never a list
    value's len (§V.4)."""
    with patch("mailpilot.settings.get_settings", return_value=make_test_settings()):
        result = runner.invoke(main, ["config", "get", "run_interval"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["record_count"] == 1


def test_record_count_absent_on_error(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """Error envelope omits record_count (§V.4)."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=None),
    ):
        result = runner.invoke(
            main, ["account", "view", "01234567-0000-7000-0000-0000000000ff"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert "record_count" not in data


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
        mock_connection,
        limit=5,
        since="2024-01-01T00:00:00",
        until=None,
        include_disabled=False,
    )


def test_account_list_include_disabled_flag(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.118: --include-disabled forwards include_disabled=True."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_accounts", return_value=[]) as mock_list,
    ):
        result = runner.invoke(main, ["account", "list", "--include-disabled"])

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        limit=100,
        since=None,
        until=None,
        include_disabled=True,
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
        result = runner.invoke(
            main, ["account", "view", "01234567-0000-7000-0000-0000000000ff"]
        )

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
            main,
            [
                "account",
                "update",
                "01234567-0000-7000-0000-0000000000ff",
                "--display-name",
                "X",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "not_found"


# -- account disable -----------------------------------------------------------


def test_account_disable_happy_path(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.118: account disable writes disabled_reason and returns the account."""
    before = _make_account(disabled_reason=None)
    after = _make_account(disabled_reason="out of business")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=before),
        patch("mailpilot.database.disable_account", return_value=after) as mock_disable,
    ):
        result = runner.invoke(
            main,
            ["account", "disable", before.id, "--reason", "out of business"],
        )

    assert result.exit_code == 0, result.output
    mock_disable.assert_called_once_with(mock_connection, before.id, "out of business")
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["account"]["disabled_reason"] == "out of business"


def test_account_disable_already_disabled(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.118: double-disable is rejected by the disabled_reason IS NULL gate."""
    before = _make_account(disabled_reason="out of business")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=before),
        patch("mailpilot.database.disable_account") as mock_disable,
    ):
        result = runner.invoke(
            main, ["account", "disable", before.id, "--reason", "again"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "validation_error"
    assert "already disabled" in data["message"]
    mock_disable.assert_not_called()


def test_account_disable_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.118: disabling a missing account yields a not_found envelope."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=None),
        patch("mailpilot.database.disable_account") as mock_disable,
    ):
        result = runner.invoke(
            main,
            [
                "account",
                "disable",
                "01234567-0000-7000-0000-0000000000fd",
                "--reason",
                "x",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "not_found"
    mock_disable.assert_not_called()


def test_account_disable_empty_reason(runner: CliRunner) -> None:
    """§V.118: an empty reason is rejected before any DB call."""
    with patch("mailpilot.settings.get_settings", return_value=make_test_settings()):
        result = runner.invoke(
            main, ["account", "disable", "some-id", "--reason", "  "]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "validation_error"


def test_account_enable_happy_path(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.118: account enable clears disabled_reason; §V.54 changed=['disabled_reason']."""
    before = _make_account(disabled_reason="out of business")
    after = _make_account(disabled_reason=None)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=before),
        patch("mailpilot.database.enable_account", return_value=after) as mock_enable,
        patch("mailpilot.operator_log.operator_event") as mock_event,
    ):
        result = runner.invoke(main, ["account", "enable", before.id])

    assert result.exit_code == 0, result.output
    mock_enable.assert_called_once_with(mock_connection, before.id)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["account"]["disabled_reason"] is None
    enable_events = [
        call
        for call in mock_event.call_args_list
        if call.args[:1] == ("account.enable",)
    ]
    assert len(enable_events) == 1
    assert enable_events[0].kwargs == {
        "entity_id": before.id,
        "changed": ["disabled_reason"],
    }


def test_account_enable_not_disabled(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.118: enabling an active account is rejected before any write."""
    before = _make_account(disabled_reason=None)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=before),
        patch("mailpilot.database.enable_account") as mock_enable,
    ):
        result = runner.invoke(main, ["account", "enable", before.id])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "not disabled" in data["message"]
    mock_enable.assert_not_called()


def test_account_enable_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.118: enabling a missing account yields a not_found envelope."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=None),
        patch("mailpilot.database.enable_account") as mock_enable,
    ):
        result = runner.invoke(
            main, ["account", "enable", "01234567-0000-7000-0000-0000000000fd"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    mock_enable.assert_not_called()


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
        patch("mailpilot.gmail.has_google_credentials", return_value=False),
        patch("mailpilot.sync.sync_account", side_effect=[3, 5]) as mock_sync,
    ):
        result = runner.invoke(main, ["account", "sync"])

    assert result.exit_code == 0, result.output
    assert mock_client_cls.call_count == 2
    assert mock_sync.call_count == 2
    data = json.loads(result.output)
    assert data["ok"] is True
    assert set(data.keys()) == {"accounts", "record_count", "ok"}
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
        patch("mailpilot.gmail.has_google_credentials", return_value=False),
        patch("mailpilot.sync.sync_account", return_value=2),
    ):
        result = runner.invoke(main, ["account", "sync", "--account-email", account.id])

    assert result.exit_code == 0, result.output
    mock_get.assert_called_once_with(mock_connection, account.id)
    mock_list.assert_not_called()
    data = json.loads(result.output)
    assert set(data.keys()) == {"accounts", "record_count", "ok"}
    assert len(data["accounts"]) == 1
    assert data["accounts"][0]["email"] == "only@example.com"
    assert data["accounts"][0]["stored"] == 2


def test_account_sync_unknown_id(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=None),
    ):
        result = runner.invoke(
            main,
            [
                "account",
                "sync",
                "--account-email",
                "01234567-0000-7000-0000-0000000000fe",
            ],
        )

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
        patch("mailpilot.gmail.has_google_credentials", return_value=False),
        patch("logfire.exception"),
        patch(
            "mailpilot.sync.sync_account",
            side_effect=[RuntimeError("gmail 500"), 4],
        ),
    ):
        result = runner.invoke(main, ["account", "sync"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert set(data.keys()) == {"accounts", "record_count", "ok"}
    assert data["accounts"][0]["error"] == "gmail 500"
    assert "stored" not in data["accounts"][0]
    assert data["accounts"][1]["stored"] == 4


def test_account_sync_polls_calendar_per_account(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.126: account sync polls each account's calendar after Gmail sync."""
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
        patch("mailpilot.gmail.has_google_credentials", return_value=True),
        patch("mailpilot.sync.sync_account", side_effect=[3, 5]),
        patch("mailpilot.sync._poll_account_calendar", return_value=None) as mock_poll,
    ):
        result = runner.invoke(main, ["account", "sync"])

    assert result.exit_code == 0, result.output
    assert mock_poll.call_count == 2
    polled_emails = [call.args[1].email for call in mock_poll.call_args_list]
    assert polled_emails == ["a@example.com", "b@example.com"]
    data = json.loads(result.output)
    assert [r["stored"] for r in data["accounts"]] == [3, 5]
    assert all("calendar_error" not in r for r in data["accounts"])


def test_account_sync_skips_calendar_without_credentials(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.126: no Google credentials -> the calendar poll is gated out."""
    account = _make_account(email="only@example.com")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.gmail.GmailClient"),
        patch("mailpilot.gmail.has_google_credentials", return_value=False),
        patch("mailpilot.sync.sync_account", return_value=2),
        patch("mailpilot.sync._poll_account_calendar") as mock_poll,
    ):
        result = runner.invoke(main, ["account", "sync", "--account-email", account.id])

    assert result.exit_code == 0, result.output
    mock_poll.assert_not_called()


def test_account_sync_calendar_error_isolated_from_gmail_success(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.126: a calendar fault is recorded on the row but the Gmail sync,
    whose store count survives, never aborts (command exits 0)."""
    account = _make_account(email="only@example.com")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.gmail.GmailClient"),
        patch("mailpilot.gmail.has_google_credentials", return_value=True),
        patch("mailpilot.sync.sync_account", return_value=7),
        patch("mailpilot.sync._poll_account_calendar", return_value="calendar 500"),
    ):
        result = runner.invoke(main, ["account", "sync", "--account-email", account.id])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    row = data["accounts"][0]
    assert row["stored"] == 7
    assert row["calendar_error"] == "calendar 500"
    assert "error" not in row


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
    view = CompanyView(
        id=company.id,
        name=company.name,
        domain=company.domain,
        aliases=[],
        created_at=company.created_at,
        updated_at=company.updated_at,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_company", return_value=company) as mock_create,
        patch("mailpilot.database.load_company_view", return_value=view),
    ):
        result = runner.invoke(
            main, ["company", "create", "--domain", "acme.com", "--name", "Acme Corp"]
        )

    assert result.exit_code == 0
    mock_create.assert_called_once_with(
        mock_connection, name="Acme Corp", domain="acme.com", aliases=None
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["created"] is True
    assert data["company"]["domain"] == "acme.com"
    assert data["company"]["name"] == "Acme Corp"


def test_company_create_duplicate_without_upsert(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.147: without --upsert, natural-key conflict stays already_exists."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_company", return_value=None),
    ):
        result = runner.invoke(
            main, ["company", "create", "--domain", "acme.com", "--name", "Acme"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "already_exists"


def test_company_create_upsert_updates_name_preserves_profile(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.147: second create --upsert updates name; never passes profile."""
    existing = _make_company(
        name="Old Name",
        profile={
            "summary": "Keep me",
            "products": ["Acumatica"],
            "target_customers": "Mid-market",
            "sources": ["https://acme.com"],
        },
    )
    updated = _make_company(name="New Name", profile=existing.profile)
    view = CompanyView(
        id=updated.id,
        name=updated.name,
        domain=updated.domain,
        aliases=[],
        profile=updated.profile,
        created_at=updated.created_at,
        updated_at=updated.updated_at,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_company", return_value=None),
        patch(
            "mailpilot.database.get_company_by_domain_exact", return_value=existing
        ),
        patch(
            "mailpilot.database.update_company", return_value=updated
        ) as mock_update,
        patch("mailpilot.database.load_company_view", return_value=view),
    ):
        result = runner.invoke(
            main,
            [
                "company",
                "create",
                "--domain",
                "acme.com",
                "--name",
                "New Name",
                "--upsert",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_update.assert_called_once_with(mock_connection, existing.id, name="New Name")
    assert "profile" not in mock_update.call_args.kwargs
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["created"] is False
    assert data["company"]["name"] == "New Name"
    assert data["company"]["profile"]["summary"] == "Keep me"
    assert data["record_count"] == 1


def test_company_create_domain_only(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    company = _make_company(name="")
    view = CompanyView(
        id=company.id,
        name="",
        domain=company.domain,
        aliases=[],
        created_at=company.created_at,
        updated_at=company.updated_at,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_company", return_value=company) as mock_create,
        patch("mailpilot.database.load_company_view", return_value=view),
    ):
        result = runner.invoke(main, ["company", "create", "--domain", "acme.com"])

    assert result.exit_code == 0
    mock_create.assert_called_once_with(
        mock_connection, name="", domain="acme.com", aliases=None
    )


def test_company_create_with_aliases(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.142: create --alias registers aliases and view projects them."""
    company = _make_company()
    view = CompanyView(
        id=company.id,
        name=company.name,
        domain=company.domain,
        aliases=["consulting.acme.com"],
        created_at=company.created_at,
        updated_at=company.updated_at,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_company", return_value=company) as mock_create,
        patch("mailpilot.database.load_company_view", return_value=view),
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
                "--alias",
                "consulting.acme.com",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_create.assert_called_once_with(
        mock_connection,
        name="Acme",
        domain="acme.com",
        aliases=["consulting.acme.com"],
    )
    data = json.loads(result.output)
    assert data["company"]["aliases"] == ["consulting.acme.com"]


def test_company_create_with_note_appends_note_atomically(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """`--note STR` writes a note row under the same cli_mutation span."""
    company = _make_company()
    view = CompanyView(
        id=company.id,
        name=company.name,
        domain=company.domain,
        created_at=company.created_at,
        updated_at=company.updated_at,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_company", return_value=company),
        patch("mailpilot.database.load_company_view", return_value=view),
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
    view = CompanyView(
        id=company.id,
        name=company.name,
        domain=company.domain,
        created_at=company.created_at,
        updated_at=company.updated_at,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_company", return_value=company),
        patch("mailpilot.database.load_company_view", return_value=view),
        patch("mailpilot.database.add_company_note") as mock_note,
    ):
        result = runner.invoke(
            main, ["company", "create", "--domain", "acme.com", "--name", "Acme"]
        )

    assert result.exit_code == 0
    mock_note.assert_not_called()


def test_company_merge_cli(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.143: merge CLI absorbs source into survivor and returns view."""
    source = _make_company(id="from-id", domain="nexvue.com", name="Nexvue")
    survivor = _make_company(id="into-id", domain="netatwork.com", name="Net@Work")
    view = CompanyView(
        id=survivor.id,
        name=survivor.name,
        domain=survivor.domain,
        aliases=["nexvue.com"],
        created_at=survivor.created_at,
        updated_at=survivor.updated_at,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.get_company_by_domain_exact", return_value=source
        ),
        patch("mailpilot.database.get_company_by_domain", return_value=survivor),
        patch("mailpilot.database.merge_companies", return_value=survivor) as mock_merge,
        patch("mailpilot.database.load_company_view", return_value=view),
    ):
        result = runner.invoke(
            main,
            [
                "company",
                "merge",
                "--from",
                "nexvue.com",
                "--into",
                "netatwork.com",
                "--move-contacts",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_merge.assert_called_once_with(
        mock_connection,
        source.id,
        survivor.id,
        move_contacts=True,
        original_from_domain="nexvue.com",
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["company"]["domain"] == "netatwork.com"
    assert data["company"]["aliases"] == ["nexvue.com"]


def test_skill_documents_company_aliases_and_merge() -> None:
    """§V.142/§V.143: packaged SKILL.md documents alias + merge recipes."""
    from importlib.resources import files

    body = files("mailpilot").joinpath("SKILL.md").read_text(encoding="utf-8")
    assert "--alias" in body
    assert "company merge" in body
    assert "--move-contacts" in body
    assert "merged:into" in body
    assert "aliases" in body
    assert "§V." not in body
    assert "§T." not in body


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
    mock_list.assert_called_once_with(
        mock_connection,
        limit=5,
        since=None,
        until=None,
        has_profile=None,
        max_contacts=None,
        min_contacts=None,
        include_disabled=False,
        tag=None,
        exclude_tags=[],
        full=False,
        status=None,
    )


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
        mock_connection,
        limit=100,
        since="2024-01-01T00:00:00",
        until=None,
        has_profile=None,
        max_contacts=None,
        min_contacts=None,
        include_disabled=False,
        tag=None,
        exclude_tags=[],
        full=False,
        status=None,
    )


def test_company_list_has_profile_flag(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_companies", return_value=[]) as mock_list,
    ):
        result = runner.invoke(main, ["company", "list", "--has-profile"])

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        limit=100,
        since=None,
        until=None,
        has_profile=True,
        max_contacts=None,
        min_contacts=None,
        include_disabled=False,
        tag=None,
        exclude_tags=[],
        full=False,
        status=None,
    )


def test_company_list_no_profile_flag(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_companies", return_value=[]) as mock_list,
    ):
        result = runner.invoke(main, ["company", "list", "--no-profile"])

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        limit=100,
        since=None,
        until=None,
        has_profile=False,
        max_contacts=None,
        min_contacts=None,
        include_disabled=False,
        tag=None,
        exclude_tags=[],
        full=False,
        status=None,
    )


def test_company_list_profile_presence_tristate(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.115 family 4: --has-profile/--no-profile is one tri-state flag.

    Passing both switches no longer errors -- the last one wins, the Click
    way for a single boolean flag. There is no two-flag XOR and no
    ``--no-has-profile`` artifact (covered in tests/test_filters.py).
    """
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_companies", return_value=[]) as mock_list,
    ):
        result = runner.invoke(
            main, ["company", "list", "--has-profile", "--no-profile"]
        )

    assert result.exit_code == 0
    _, kwargs = mock_list.call_args
    assert kwargs["has_profile"] is False


def test_company_list_max_contacts_flag(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.96: --max-contacts N flows to list_companies as the upper bound."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_companies", return_value=[]) as mock_list,
    ):
        result = runner.invoke(main, ["company", "list", "--max-contacts", "4"])

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        limit=100,
        since=None,
        until=None,
        has_profile=None,
        max_contacts=4,
        min_contacts=None,
        include_disabled=False,
        tag=None,
        exclude_tags=[],
        full=False,
        status=None,
    )


def test_company_list_min_contacts_flag(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.96: --min-contacts N flows to list_companies as the lower bound."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_companies", return_value=[]) as mock_list,
    ):
        result = runner.invoke(main, ["company", "list", "--min-contacts", "1"])

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        limit=100,
        since=None,
        until=None,
        has_profile=None,
        max_contacts=None,
        min_contacts=1,
        include_disabled=False,
        tag=None,
        exclude_tags=[],
        full=False,
        status=None,
    )


def test_company_list_include_disabled_flag(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.114: --include-disabled forwards include_disabled=True."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_companies", return_value=[]) as mock_list,
    ):
        result = runner.invoke(main, ["company", "list", "--include-disabled"])

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        limit=100,
        since=None,
        until=None,
        has_profile=None,
        max_contacts=None,
        min_contacts=None,
        include_disabled=True,
        tag=None,
        exclude_tags=[],
        full=False,
        status=None,
    )


def test_company_list_full_flag(runner: CliRunner, mock_connection: MagicMock) -> None:
    """§V.8: --full forwards full=True for lean profile.summary embed."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_companies", return_value=[]) as mock_list,
    ):
        result = runner.invoke(main, ["company", "list", "--full"])

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        limit=100,
        since=None,
        until=None,
        has_profile=None,
        max_contacts=None,
        min_contacts=None,
        include_disabled=False,
        tag=None,
        exclude_tags=[],
        full=True,
        status=None,
    )


def test_company_list_status_flag(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.138: --status ready flows to list_companies."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_companies", return_value=[]) as mock_list,
    ):
        result = runner.invoke(main, ["company", "list", "--status", "ready"])

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        limit=100,
        since=None,
        until=None,
        has_profile=None,
        max_contacts=None,
        min_contacts=None,
        include_disabled=False,
        tag=None,
        exclude_tags=[],
        full=False,
        status="ready",
    )


def test_company_list_status_disabled_forces_include(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.138: --status disabled forces include_disabled=True."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_companies", return_value=[]) as mock_list,
    ):
        result = runner.invoke(main, ["company", "list", "--status", "disabled"])

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        limit=100,
        since=None,
        until=None,
        has_profile=None,
        max_contacts=None,
        min_contacts=None,
        include_disabled=True,
        tag=None,
        exclude_tags=[],
        full=False,
        status="disabled",
    )


def test_company_list_status_rejects_out_of_set(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.115/§V.138: out-of-set --status is rejected at parse time."""
    result = runner.invoke(main, ["company", "list", "--status", "bogus"])
    assert result.exit_code != 0


def test_company_list_help_documents_status_rules(
    runner: CliRunner,
) -> None:
    """§V.138/§V.111: --help documents cohort rules without SPEC cites."""
    result = runner.invoke(main, ["company", "list", "--help"])
    assert result.exit_code == 0
    assert "--status" in result.output
    assert "ready" in result.output
    assert "needs_contacts" in result.output
    assert "needs_profile" in result.output
    assert "disabled" in result.output
    assert "§V." not in result.output
    assert "§T." not in result.output


def test_skill_documents_company_pipeline_status() -> None:
    """§V.138: packaged SKILL.md documents --status cohort rules."""
    from importlib.resources import files

    body = files("mailpilot").joinpath("SKILL.md").read_text(encoding="utf-8")
    assert "--status" in body
    assert "ready" in body
    assert "needs_contacts" in body
    assert "needs_profile" in body
    assert "disabled_reason" in body
    assert "§V." not in body
    assert "§T." not in body


# -- company export / import (tracker) -----------------------------------------


def test_company_export_stdout_ndjson(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.145: without --out, stream NDJSON lines on stdout (no envelope)."""
    rows = [
        {
            "domain": "alpha.com",
            "name": "Alpha",
            "tags": [],
            "has_profile": False,
            "contact_count": 0,
            "disabled_reason": None,
        },
        {
            "domain": "beta.com",
            "name": "Beta",
            "tags": ["vip"],
            "has_profile": True,
            "contact_count": 2,
            "disabled_reason": None,
        },
    ]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.export_companies", return_value=rows) as mock_export,
    ):
        result = runner.invoke(main, ["company", "export"])

    assert result.exit_code == 0, result.output
    mock_export.assert_called_once()
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["domain"] == "alpha.com"
    assert first["tags"] == []
    assert "ok" not in result.output
    assert "company_export" not in result.output


def test_company_export_out_status_envelope(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    """§V.145: --out writes NDJSON file + company_export status envelope."""
    rows = [
        {
            "domain": "a.com",
            "name": "A",
            "tags": [],
            "has_profile": False,
            "contact_count": 0,
            "disabled_reason": None,
        }
    ]
    out = tmp_path / "companies.jsonl"
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.export_companies", return_value=rows),
    ):
        result = runner.invoke(main, ["company", "export", "--out", str(out)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["record_count"] == 1
    assert data["company_export"]["path"] == str(out)
    assert data["company_export"]["format"] == "jsonl"
    assert data["company_export"]["record_count"] == 1
    file_lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(file_lines) == 1
    assert json.loads(file_lines[0])["domain"] == "a.com"


def test_company_export_empty_stdout(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.145: empty set streams zero lines (no envelope)."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.export_companies", return_value=[]),
    ):
        result = runner.invoke(main, ["company", "export"])

    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_company_export_full_and_filters_flow(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.145: --full and list-family filters flow to export_companies."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.export_companies", return_value=[]) as mock_export,
        patch(
            "mailpilot.database.get_tag_by_name",
            return_value=Tag(id="tag-1", name="vip", created_at=_NOW),
        ),
    ):
        result = runner.invoke(
            main,
            [
                "company",
                "export",
                "--full",
                "--status",
                "ready",
                "--has-profile",
                "--min-contacts",
                "1",
                "--tag",
                "vip",
                "--include-disabled",
            ],
        )

    assert result.exit_code == 0, result.output
    kwargs = mock_export.call_args.kwargs
    assert kwargs["full"] is True
    assert kwargs["status"] == "ready"
    assert kwargs["has_profile"] is True
    assert kwargs["min_contacts"] == 1
    assert kwargs["include_disabled"] is True
    assert kwargs["tag"] == "tag-1"


def test_company_export_help_no_spec_cites(runner: CliRunner) -> None:
    """§V.111: company export --help has zero SPEC citations."""
    result = runner.invoke(main, ["company", "export", "--help"])
    assert result.exit_code == 0
    assert "§V." not in result.output
    assert "§T." not in result.output
    assert "--out" in result.output
    assert "--full" in result.output
    assert "jsonl" in result.output


def test_company_import_dry_run(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    """§V.146: --from + --dry-run emits company_import_diff envelope."""
    tracker = tmp_path / "tracker.jsonl"
    tracker.write_text(
        '{"domain":"ready.com","name":"Ready"}\n'
        '{"domain":"missing.com","name":"Missing"}\n',
        encoding="utf-8",
    )
    diff = {
        "missing_in_crm": ["missing.com"],
        "missing_profile": [],
        "zero_contacts": [],
        "disabled": [],
        "extra_in_crm": ["extra.com"],
        "record_count": 3,
    }
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.company_import_diff", return_value=diff
        ) as mock_diff,
    ):
        result = runner.invoke(
            main,
            ["company", "import", "--from", str(tracker), "--dry-run"],
        )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["record_count"] == 3
    assert data["company_import_diff"]["missing_in_crm"] == ["missing.com"]
    assert data["company_import_diff"]["extra_in_crm"] == ["extra.com"]
    file_domains = mock_diff.call_args.args[1]
    assert file_domains == {"ready.com", "missing.com"}


def test_company_import_requires_dry_run(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    """§V.146: apply path rejected without --dry-run."""
    tracker = tmp_path / "tracker.jsonl"
    tracker.write_text('{"domain":"a.com"}\n', encoding="utf-8")
    with patch("mailpilot.settings.get_settings", return_value=make_test_settings()):
        result = runner.invoke(
            main, ["company", "import", "--from", str(tracker)]
        )

    assert result.exit_code == 1
    err = json.loads(result.stderr)
    assert err["ok"] is False
    assert err["error"] == "validation_error"
    assert "dry-run" in err["message"]


def test_company_import_missing_file(runner: CliRunner) -> None:
    """§V.146: missing tracker file -> not_found."""
    with patch("mailpilot.settings.get_settings", return_value=make_test_settings()):
        result = runner.invoke(
            main,
            [
                "company",
                "import",
                "--from",
                "/no/such/tracker.jsonl",
                "--dry-run",
            ],
        )

    assert result.exit_code == 1
    err = json.loads(result.stderr)
    assert err["error"] == "not_found"


def test_company_import_invalid_ndjson(
    runner: CliRunner, tmp_path: pathlib.Path
) -> None:
    """§V.146: bad NDJSON line -> validation_error."""
    tracker = tmp_path / "bad.jsonl"
    tracker.write_text("not-json\n", encoding="utf-8")
    with patch("mailpilot.settings.get_settings", return_value=make_test_settings()):
        result = runner.invoke(
            main,
            ["company", "import", "--from", str(tracker), "--dry-run"],
        )

    assert result.exit_code == 1
    err = json.loads(result.stderr)
    assert err["error"] == "validation_error"


def test_company_import_help_no_spec_cites(runner: CliRunner) -> None:
    """§V.111: company import --help has zero SPEC citations."""
    result = runner.invoke(main, ["company", "import", "--help"])
    assert result.exit_code == 0
    assert "§V." not in result.output
    assert "§T." not in result.output
    assert "--dry-run" in result.output
    assert "--from" in result.output


def test_skill_documents_company_tracker_export_import() -> None:
    """§V.145/§V.146: packaged SKILL.md documents tracker export + dry-run import."""
    from importlib.resources import files

    body = files("mailpilot").joinpath("SKILL.md").read_text(encoding="utf-8")
    assert "company export" in body
    assert "company import" in body
    assert "--dry-run" in body
    assert "jsonl" in body
    assert "missing_in_crm" in body
    assert "company_import_diff" in body
    assert "§V." not in body
    assert "§T." not in body


def test_company_list_envelope_projects_tags_and_disabled_reason(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.8/§V.116: list JSON rows include tags[] and disabled_reason."""
    companies = [
        CompanySummary(
            id="01234567-0000-7000-0000-0000000000aa",
            name="Acme",
            domain="acme.com",
            has_profile=True,
            contact_count=2,
            tags=["vip", "partner"],
            disabled_reason=None,
            profile=None,
            created_at=_NOW,
        ),
        CompanySummary(
            id="01234567-0000-7000-0000-0000000000bb",
            name="Gone",
            domain="gone.com",
            has_profile=False,
            contact_count=0,
            tags=[],
            disabled_reason="absorbed-brand",
            profile=None,
            created_at=_NOW,
        ),
    ]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_companies", return_value=companies),
    ):
        result = runner.invoke(main, ["company", "list", "--include-disabled"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["companies"][0]["tags"] == ["vip", "partner"]
    assert data["companies"][0]["disabled_reason"] is None
    assert data["companies"][1]["tags"] == []
    assert data["companies"][1]["disabled_reason"] == "absorbed-brand"


def test_company_list_full_envelope_embeds_profile_summary(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.8: --full list envelope embeds profile.summary only."""
    companies = [
        CompanySummary(
            id="01234567-0000-7000-0000-0000000000aa",
            name="Acme",
            domain="acme.com",
            has_profile=True,
            contact_count=0,
            tags=[],
            disabled_reason=None,
            profile={"summary": "Acme builds widgets."},
            created_at=_NOW,
        ),
    ]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_companies", return_value=companies),
    ):
        result = runner.invoke(main, ["company", "list", "--full"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    row = data["companies"][0]
    assert row["profile"] == {"summary": "Acme builds widgets."}
    assert "products" not in (row["profile"] or {})


# -- company disable -----------------------------------------------------------


def test_company_disable_happy_path(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.114: company disable writes disabled_reason and returns the company."""
    before = _make_company(disabled_reason=None)
    after = _make_company(disabled_reason="no_contacts_found:2026-06-18")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=before),
        patch("mailpilot.database.disable_company", return_value=after) as mock_disable,
    ):
        result = runner.invoke(
            main,
            [
                "company",
                "disable",
                before.id,
                "--reason",
                "no_contacts_found:2026-06-18",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_disable.assert_called_once_with(
        mock_connection, before.id, "no_contacts_found:2026-06-18"
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["company"]["disabled_reason"] == "no_contacts_found:2026-06-18"


def test_company_disable_already_disabled(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.114: double-disable is rejected by the disabled_reason IS NULL gate."""
    before = _make_company(disabled_reason="no_contacts_found:2026-06-01")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=before),
        patch("mailpilot.database.disable_company") as mock_disable,
    ):
        result = runner.invoke(
            main, ["company", "disable", before.id, "--reason", "again"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "validation_error"
    assert "already disabled" in data["message"]
    mock_disable.assert_not_called()


def test_company_disable_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.114: disabling a missing company yields a not_found envelope."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=None),
        patch("mailpilot.database.disable_company") as mock_disable,
    ):
        result = runner.invoke(
            main,
            [
                "company",
                "disable",
                "01234567-0000-7000-0000-0000000000fd",
                "--reason",
                "x",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "not_found"
    mock_disable.assert_not_called()


def test_company_disable_empty_reason(runner: CliRunner) -> None:
    """§V.114: an empty reason is rejected before any DB call."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
    ):
        result = runner.invoke(
            main, ["company", "disable", "some-id", "--reason", "  "]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "validation_error"


def test_company_disable_stdin_mixed_ok_error(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.139: batch disable continues past errors; full results envelope."""
    active = _make_company(domain="a.com", disabled_reason=None)
    already = _make_company(
        id="01234567-0000-7000-0000-0000000000bb",
        domain="b.com",
        disabled_reason="prior",
    )
    disabled = _make_company(
        id="01234567-0000-7000-0000-0000000000aa",
        domain="a.com",
        disabled_reason="absorbed-brand",
    )

    def _by_domain(_conn: object, domain: str) -> Company | None:
        return {"a.com": active, "b.com": already}.get(domain)

    stdin = "\n".join(
        [
            json.dumps({"domain": "a.com", "reason": "absorbed-brand"}),
            json.dumps({"domain": "missing.com", "reason": "gone"}),
            json.dumps({"domain": "b.com", "reason": "again"}),
            "{not-json",
        ]
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company_by_domain", side_effect=_by_domain),
        patch(
            "mailpilot.database.disable_company", return_value=disabled
        ) as mock_disable,
    ):
        result = runner.invoke(main, ["company", "disable", "--stdin"], input=stdin)

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["record_count"] == 4
    assert data["results"] == [
        {"ref": "a.com", "status": "ok"},
        {
            "ref": "missing.com",
            "status": "error",
            "error": "not_found",
            "message": "company not found: missing.com",
        },
        {"ref": "b.com", "status": "ok"},
        {
            "ref": "line:4",
            "status": "error",
            "error": "validation_error",
            "message": data["results"][3]["message"],
        },
    ]
    assert "invalid JSON" in data["results"][3]["message"]
    mock_disable.assert_called_once_with(mock_connection, active.id, "absorbed-brand")


def test_company_disable_stdin_all_ok_exit_0(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.139: zero error rows -> exit 0 + ok:true results envelope."""
    company = _make_company(domain="ok.com", disabled_reason=None)
    after = _make_company(domain="ok.com", disabled_reason="x")
    stdin = json.dumps({"domain": "ok.com", "reason": "x"}) + "\n"
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company_by_domain", return_value=company),
        patch("mailpilot.database.disable_company", return_value=after),
    ):
        result = runner.invoke(main, ["company", "disable", "--stdin"], input=stdin)

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data == {
        "results": [{"ref": "ok.com", "status": "ok"}],
        "record_count": 1,
        "ok": True,
    }


def test_company_disable_stdin_exclusive_with_positional(
    runner: CliRunner,
) -> None:
    """§V.139: --stdin exclusive with COMPANY_REF."""
    with patch("mailpilot.settings.get_settings", return_value=make_test_settings()):
        result = runner.invoke(
            main,
            ["company", "disable", "acme.com", "--stdin"],
            input='{"domain":"x.com","reason":"y"}\n',
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "validation_error"
    assert "exclusive" in data["message"]


def test_company_disable_help_documents_stdin(runner: CliRunner) -> None:
    """§V.139/§V.111: --help documents --stdin without SPEC cites."""
    result = runner.invoke(main, ["company", "disable", "--help"])
    assert result.exit_code == 0
    assert "--stdin" in result.output
    assert "NDJSON" in result.output
    assert "§V." not in result.output


def test_company_enable_happy_path(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.114: company enable clears disabled_reason; §V.54 changed=['disabled_reason']."""
    before = _make_company(disabled_reason="no_contacts_found:2026-06-18")
    after = _make_company(disabled_reason=None)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=before),
        patch("mailpilot.database.enable_company", return_value=after) as mock_enable,
        patch("mailpilot.operator_log.operator_event") as mock_event,
    ):
        result = runner.invoke(main, ["company", "enable", before.id])

    assert result.exit_code == 0, result.output
    mock_enable.assert_called_once_with(mock_connection, before.id)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["company"]["disabled_reason"] is None
    enable_events = [
        call
        for call in mock_event.call_args_list
        if call.args[:1] == ("company.enable",)
    ]
    assert len(enable_events) == 1
    assert enable_events[0].kwargs == {
        "entity_id": before.id,
        "changed": ["disabled_reason"],
    }


def test_company_enable_not_disabled(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.114: enabling an active company is rejected before any write."""
    before = _make_company(disabled_reason=None)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=before),
        patch("mailpilot.database.enable_company") as mock_enable,
    ):
        result = runner.invoke(main, ["company", "enable", before.id])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "not disabled" in data["message"]
    mock_enable.assert_not_called()


def test_company_enable_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.114: enabling a missing company yields a not_found envelope."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=None),
        patch("mailpilot.database.enable_company") as mock_enable,
    ):
        result = runner.invoke(
            main, ["company", "enable", "01234567-0000-7000-0000-0000000000fd"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    mock_enable.assert_not_called()


# -- company view --------------------------------------------------------------


def test_company_view(runner: CliRunner, mock_connection: MagicMock) -> None:
    company = _make_company()
    view = CompanyView(
        id=company.id,
        name=company.name,
        domain=company.domain,
        tags=["vip"],
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
    assert data["company"]["tags"] == ["vip"]


def test_company_view_not_found(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.load_company_view", return_value=None),
    ):
        result = runner.invoke(
            main, ["company", "view", "01234567-0000-7000-0000-0000000000ff"]
        )

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
            main,
            [
                "company",
                "update",
                "01234567-0000-7000-0000-0000000000ff",
                "--name",
                "X",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "not_found"


def test_company_update_profile_json_valid(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.72: ``--profile-json`` forwards parsed dict to ``update_company``."""
    before = _make_company()
    profile = {
        "summary": "Acme makes widgets.",
        "products": ["Widget X"],
        "target_customers": "Aerospace OEMs.",
        "timezone": "America/Toronto",
        "sources": ["https://acme.com/"],
    }
    after = _make_company()
    after = after.model_copy(update={"profile": profile})
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=before),
        patch("mailpilot.database.update_company", return_value=after) as mock_update,
    ):
        result = runner.invoke(
            main,
            [
                "company",
                "update",
                before.id,
                "--profile-json",
                json.dumps(profile),
            ],
        )

    assert result.exit_code == 0
    mock_update.assert_called_once_with(mock_connection, before.id, profile=profile)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["company"]["profile"] == profile


def test_company_update_profile_json_invalid_text(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.72: malformed JSON text emits ``validation_error`` envelope."""
    company = _make_company()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=company),
    ):
        result = runner.invoke(
            main,
            ["company", "update", company.id, "--profile-json", "{not json"],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "validation_error"
    assert "invalid JSON" in data["message"]


def test_company_update_profile_validation_error_envelope(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.72/§V.54: ``ValidationError`` is translated to ``validation_error``."""
    from pydantic import ValidationError

    from mailpilot.models import CompanyProfile

    company = _make_company()
    try:
        CompanyProfile.model_validate({"products": ["x"]})
    except ValidationError as exc:
        validation_error = exc
    else:  # pragma: no cover - sanity guard
        raise AssertionError("CompanyProfile should reject empty payload")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=company),
        patch("mailpilot.cli.configure_logging", lambda debug=False: None),
        patch(
            "mailpilot.database.update_company", side_effect=validation_error
        ) as mock_update,
    ):
        result = runner.invoke(
            main,
            [
                "company",
                "update",
                company.id,
                "--profile-json",
                json.dumps({"products": ["x"]}),
            ],
        )

    mock_update.assert_called_once()
    assert result.exit_code == 1
    assert '"error": "validation_error"' in result.output
    assert '"ok": false' in result.output


_VALID_PROFILE = {
    "summary": "Acme makes widgets.",
    "products": ["Widget X"],
    "target_customers": "Aerospace OEMs.",
    "timezone": "America/Toronto",
    "sources": ["https://acme.com/"],
}


def test_company_update_profile_file(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    """§V.140: --profile-file full-replaces via the same schema as --profile-json."""
    before = _make_company()
    after = before.model_copy(update={"profile": _VALID_PROFILE})
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(_VALID_PROFILE), encoding="utf-8")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=before),
        patch("mailpilot.database.update_company", return_value=after) as mock_update,
    ):
        result = runner.invoke(
            main,
            ["company", "update", before.id, "--profile-file", str(profile_path)],
        )

    assert result.exit_code == 0, result.output
    mock_update.assert_called_once_with(
        mock_connection, before.id, profile=_VALID_PROFILE
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["record_count"] == 1
    assert data["company"]["profile"] == _VALID_PROFILE


def test_company_update_profile_stdin(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.140: --profile - reads full-replace JSON from stdin."""
    before = _make_company()
    after = before.model_copy(update={"profile": _VALID_PROFILE})
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=before),
        patch("mailpilot.database.update_company", return_value=after) as mock_update,
    ):
        result = runner.invoke(
            main,
            ["company", "update", before.id, "--profile", "-"],
            input=json.dumps(_VALID_PROFILE),
        )

    assert result.exit_code == 0, result.output
    mock_update.assert_called_once_with(
        mock_connection, before.id, profile=_VALID_PROFILE
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["company"]["profile"] == _VALID_PROFILE


def test_company_update_profile_replace_options_exclusive(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    """§V.140: full-replace options are exclusive XOR."""
    company = _make_company()
    profile_path = tmp_path / "p.json"
    profile_path.write_text(json.dumps(_VALID_PROFILE), encoding="utf-8")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=company),
        patch("mailpilot.database.update_company") as mock_update,
    ):
        result = runner.invoke(
            main,
            [
                "company",
                "update",
                company.id,
                "--profile-json",
                json.dumps(_VALID_PROFILE),
                "--profile-file",
                str(profile_path),
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "validation_error"
    assert "exclusive" in data["message"]
    mock_update.assert_not_called()


def test_company_update_profile_replace_exclusive_with_patch(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.140: full-replace exclusive with any field-patch flag."""
    company = _make_company()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=company),
        patch("mailpilot.database.update_company") as mock_update,
    ):
        result = runner.invoke(
            main,
            [
                "company",
                "update",
                company.id,
                "--profile-json",
                json.dumps(_VALID_PROFILE),
                "--summary",
                "patched",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "validation_error"
    assert "exclusive" in data["message"]
    mock_update.assert_not_called()


def test_company_update_profile_field_patch_merge(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.140: field-patch merges into existing profile; multi flags replace lists."""
    before = _make_company(profile=dict(_VALID_PROFILE))
    merged = {
        "summary": "Updated summary.",
        "products": ["Acumatica", "Dynamics BC"],
        "target_customers": "Aerospace OEMs.",
        "timezone": "America/Chicago",
        "sources": ["https://acme.com/", "lab5-leads tracker"],
    }
    after = before.model_copy(update={"profile": merged})
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=before),
        patch("mailpilot.database.update_company", return_value=after) as mock_update,
    ):
        result = runner.invoke(
            main,
            [
                "company",
                "update",
                before.id,
                "--summary",
                "Updated summary.",
                "--product",
                "Acumatica",
                "--product",
                "Dynamics BC",
                "--source",
                "https://acme.com/",
                "--source",
                "lab5-leads tracker",
                "--timezone",
                "America/Chicago",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_update.assert_called_once_with(mock_connection, before.id, profile=merged)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["record_count"] == 1
    assert data["company"]["profile"] == merged


def test_company_update_profile_patch_null_existing_incomplete(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.140/§V.72: patch on null profile still validates full object (no partial)."""
    from pydantic import ValidationError

    from mailpilot.models import CompanyProfile

    before = _make_company(profile=None)
    try:
        CompanyProfile.model_validate({"summary": "only summary"})
    except ValidationError as exc:
        validation_error = exc
    else:  # pragma: no cover
        raise AssertionError("incomplete profile must fail validation")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=before),
        patch("mailpilot.cli.configure_logging", lambda debug=False: None),
        patch(
            "mailpilot.database.update_company", side_effect=validation_error
        ) as mock_update,
    ):
        result = runner.invoke(
            main,
            ["company", "update", before.id, "--summary", "only summary"],
        )

    mock_update.assert_called_once_with(
        mock_connection, before.id, profile={"summary": "only summary"}
    )
    assert result.exit_code == 1
    assert '"error": "validation_error"' in result.output
    assert '"ok": false' in result.output


def test_company_update_profile_patch_null_existing_complete(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.140: null existing + full patch fields builds a valid profile."""
    before = _make_company(profile=None)
    after = before.model_copy(update={"profile": _VALID_PROFILE})
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=before),
        patch("mailpilot.database.update_company", return_value=after) as mock_update,
    ):
        result = runner.invoke(
            main,
            [
                "company",
                "update",
                before.id,
                "--summary",
                _VALID_PROFILE["summary"],
                "--product",
                "Widget X",
                "--target-customers",
                _VALID_PROFILE["target_customers"],
                "--timezone",
                "America/Toronto",
                "--source",
                "https://acme.com/",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_update.assert_called_once_with(
        mock_connection, before.id, profile=_VALID_PROFILE
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["company"]["profile"] == _VALID_PROFILE


def test_company_update_profile_non_object_json(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.140: non-object JSON root is validation_error before DB write."""
    company = _make_company()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=company),
        patch("mailpilot.database.update_company") as mock_update,
    ):
        result = runner.invoke(
            main,
            ["company", "update", company.id, "--profile-json", '["not", "object"]'],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "JSON object" in data["message"]
    mock_update.assert_not_called()


def test_company_update_profile_flag_rejects_path(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.140: --profile only accepts '-' (use --profile-file for paths)."""
    company = _make_company()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=company),
        patch("mailpilot.database.update_company") as mock_update,
    ):
        result = runner.invoke(
            main,
            ["company", "update", company.id, "--profile", "/tmp/p.json"],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "--profile-file" in data["message"]
    mock_update.assert_not_called()


def test_company_update_help_documents_profile_paths(runner: CliRunner) -> None:
    """§V.140/§V.111: --help documents profile write flags without SPEC cites."""
    result = runner.invoke(main, ["company", "update", "--help"])
    assert result.exit_code == 0
    assert "--profile-file" in result.output
    assert "--profile" in result.output
    assert "--summary" in result.output
    assert "--product" in result.output
    assert "--source" in result.output
    assert "--timezone" in result.output
    assert "--target-customers" in result.output
    assert "§V." not in result.output


def test_skill_documents_company_profile_write() -> None:
    """§V.140: packaged SKILL.md documents file/stdin replace + field patch."""
    from importlib.resources import files

    body = files("mailpilot").joinpath("SKILL.md").read_text(encoding="utf-8")
    assert "--profile-file" in body
    assert "--profile -" in body
    assert "--summary" in body
    assert "--product" in body
    assert "§V." not in body
    assert "§T." not in body


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
        title=None,
        email_confidence=None,
        verification_meta=None,
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["created"] is True
    assert data["contact"]["email"] == "alice@example.com"
    assert data["contact"]["first_name"] == "Alice"


def test_contact_create_duplicate_without_upsert(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.147: without --upsert, natural-key conflict stays duplicate_key."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_contact", return_value=None),
    ):
        result = runner.invoke(
            main, ["contact", "create", "--email", "alice@example.com"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "duplicate_key"


def test_contact_create_upsert_updates_supplied_fields(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.147: second create --upsert updates title/confidence; skips omitted."""
    existing = _make_contact(
        first_name="Alice",
        last_name="Smith",
        title="Analyst",
        email_confidence=50,
    )
    updated = _make_contact(
        first_name="Alice",
        last_name="Smith",
        title="VP Sales",
        email_confidence=90,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_contact", return_value=None),
        patch("mailpilot.database.get_contact_by_email", return_value=existing),
        patch(
            "mailpilot.database.update_contact", return_value=updated
        ) as mock_update,
    ):
        result = runner.invoke(
            main,
            [
                "contact",
                "create",
                "--email",
                "alice@example.com",
                "--title",
                "VP Sales",
                "--email-confidence",
                "90",
                "--upsert",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_update.assert_called_once_with(
        mock_connection,
        existing.id,
        title="VP Sales",
        email_confidence=90,
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["created"] is False
    assert data["contact"]["title"] == "VP Sales"
    assert data["contact"]["email_confidence"] == 90
    assert data["contact"]["first_name"] == "Alice"
    assert data["record_count"] == 1


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
        title=None,
        email_confidence=None,
        verification_meta=None,
    )


def test_contact_create_with_lead_metadata(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.95/§V.54: --title + --email-confidence wire to create + changed list."""
    contact = _make_contact(title="VP Sales", email_confidence=88)
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
                "--title",
                "VP Sales",
                "--email-confidence",
                "88",
            ],
        )

    assert result.exit_code == 0
    mock_create.assert_called_once_with(
        mock_connection,
        email="alice@example.com",
        first_name=None,
        last_name=None,
        company_id=None,
        title="VP Sales",
        email_confidence=88,
        verification_meta=None,
    )
    data = json.loads(result.output)
    assert data["contact"]["title"] == "VP Sales"
    assert data["contact"]["email_confidence"] == 88


def test_contact_create_with_verification_meta(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.144: --meta-json stores operator-only verification_meta."""
    meta = {"bouncer_status": "deliverable", "source": "hunter_pattern"}
    contact = _make_contact(email_confidence=98, verification_meta=meta)
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
                "--email-confidence",
                "98",
                "--meta-json",
                json.dumps(meta),
            ],
        )

    assert result.exit_code == 0
    mock_create.assert_called_once_with(
        mock_connection,
        email="alice@example.com",
        first_name=None,
        last_name=None,
        company_id=None,
        title=None,
        email_confidence=98,
        verification_meta=meta,
    )
    data = json.loads(result.output)
    assert data["contact"]["verification_meta"] == meta


def test_contact_create_meta_json_must_be_object(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.144: --meta-json array/non-object fails validation_error."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(
            main,
            [
                "contact",
                "create",
                "--email",
                "alice@example.com",
                "--meta-json",
                '["not","object"]',
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "object" in data["message"]


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
                "--company-domain",
                "01234567-0000-7000-0000-0000000000c2",
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


def test_contact_create_stdin_mixed_ok_error(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.139: batch create continues past errors; duplicate is ok skip."""
    company = _make_company(domain="acme.com")
    created = _make_contact(email="new@acme.com")

    def _create(
        _conn: object,
        *,
        email: str,
        first_name: str | None = None,
        last_name: str | None = None,
        company_id: str | None = None,
        title: str | None = None,
        email_confidence: int | None = None,
        verification_meta: dict[str, object] | None = None,
    ) -> Contact | None:
        if email == "dup@acme.com":
            return None
        return created

    stdin = "\n".join(
        [
            json.dumps(
                {
                    "email": "new@acme.com",
                    "first_name": "New",
                    "company_domain": "acme.com",
                }
            ),
            json.dumps({"email": "orphan@x.com", "company_domain": "missing.com"}),
            json.dumps({"email": "dup@acme.com"}),
            json.dumps({"first_name": "NoEmail"}),
        ]
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.get_company_by_domain",
            side_effect=lambda _c, d: company if d == "acme.com" else None,
        ),
        patch("mailpilot.database.create_contact", side_effect=_create) as mock_create,
    ):
        result = runner.invoke(main, ["contact", "create", "--stdin"], input=stdin)

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["record_count"] == 4
    assert data["results"][0] == {"ref": "new@acme.com", "status": "ok"}
    assert data["results"][1]["status"] == "error"
    assert data["results"][1]["error"] == "not_found"
    assert data["results"][1]["ref"] == "orphan@x.com"
    assert data["results"][2] == {"ref": "dup@acme.com", "status": "ok"}
    assert data["results"][3]["status"] == "error"
    assert data["results"][3]["error"] == "validation_error"
    assert "email is required" in data["results"][3]["message"]
    assert mock_create.call_count == 2


def test_contact_create_stdin_all_ok_exit_0(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.139: zero error rows -> exit 0."""
    contact = _make_contact(email="solo@example.com")
    stdin = json.dumps({"email": "solo@example.com"}) + "\n"
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_contact", return_value=contact),
    ):
        result = runner.invoke(main, ["contact", "create", "--stdin"], input=stdin)

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data == {
        "results": [{"ref": "solo@example.com", "status": "ok"}],
        "record_count": 1,
        "ok": True,
    }


def test_contact_create_stdin_exclusive_with_email(runner: CliRunner) -> None:
    """§V.139: --stdin exclusive with single-entity create options."""
    with patch("mailpilot.settings.get_settings", return_value=make_test_settings()):
        result = runner.invoke(
            main,
            ["contact", "create", "--stdin", "--email", "a@b.com"],
            input='{"email":"c@d.com"}\n',
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "validation_error"
    assert "exclusive" in data["message"]


def test_contact_create_help_documents_stdin(runner: CliRunner) -> None:
    """§V.139/§V.111: --help documents --stdin without SPEC cites."""
    result = runner.invoke(main, ["contact", "create", "--help"])
    assert result.exit_code == 0
    assert "--stdin" in result.output
    assert "NDJSON" in result.output
    assert "§V." not in result.output


def test_skill_documents_batch_stdin() -> None:
    """§V.139: packaged SKILL.md documents batch disable + contact create."""
    from importlib.resources import files

    body = files("mailpilot").joinpath("SKILL.md").read_text(encoding="utf-8")
    assert "company disable --stdin" in body
    assert "contact create --stdin" in body
    assert "record_count" in body
    assert "§V." not in body
    assert "§T." not in body


def test_contact_create_stdin_upsert_updates(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.147/§V.139: stdin upsert:true field-selective update on duplicate."""
    existing = _make_contact(email="dup@acme.com", title="Old")
    updated = _make_contact(email="dup@acme.com", title="New")

    def _create(
        _conn: object,
        *,
        email: str,
        first_name: str | None = None,
        last_name: str | None = None,
        company_id: str | None = None,
        title: str | None = None,
        email_confidence: int | None = None,
        verification_meta: dict[str, object] | None = None,
    ) -> Contact | None:
        return None

    stdin = json.dumps({"email": "dup@acme.com", "title": "New", "upsert": True}) + "\n"
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_contact", side_effect=_create),
        patch("mailpilot.database.get_contact_by_email", return_value=existing),
        patch(
            "mailpilot.database.update_contact", return_value=updated
        ) as mock_update,
    ):
        result = runner.invoke(main, ["contact", "create", "--stdin"], input=stdin)

    assert result.exit_code == 0, result.output
    mock_update.assert_called_once_with(
        mock_connection, existing.id, title="New"
    )
    data = json.loads(result.output)
    assert data["results"] == [{"ref": "dup@acme.com", "status": "ok"}]


def test_skill_documents_create_upsert() -> None:
    """§V.147: packaged SKILL.md documents create --upsert as preferred path."""
    from importlib.resources import files

    body = files("mailpilot").joinpath("SKILL.md").read_text(encoding="utf-8")
    assert "company create" in body
    assert "--upsert" in body
    assert "created: true" in body or "created: true" in body.replace(" ", "")
    assert "Preferred agent path" in body or "preferred agent path" in body.lower()
    assert "§V." not in body
    assert "§T." not in body


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


def test_contact_update_lead_metadata(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.95: --title + --email-confidence flow through update_contact."""
    before = _make_contact()
    updated = _make_contact(title="Founder", email_confidence=55)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=before),
        patch("mailpilot.database.update_contact", return_value=updated) as mock_update,
    ):
        result = runner.invoke(
            main,
            [
                "contact",
                "update",
                updated.id,
                "--title",
                "Founder",
                "--email-confidence",
                "55",
            ],
        )

    assert result.exit_code == 0
    mock_update.assert_called_once_with(
        mock_connection, updated.id, title="Founder", email_confidence=55
    )
    data = json.loads(result.output)
    assert data["contact"]["title"] == "Founder"
    assert data["contact"]["email_confidence"] == 55


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
            main,
            [
                "contact",
                "update",
                "01234567-0000-7000-0000-0000000000ff",
                "--first-name",
                "X",
            ],
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
            [
                "contact",
                "disable",
                "01234567-0000-7000-0000-0000000000ff",
                "--reason",
                "spam",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "not_found"
    mock_disable.assert_not_called()


def test_contact_disable_help_lists_verb(runner: CliRunner) -> None:
    result = runner.invoke(main, ["contact", "--help"])
    assert result.exit_code == 0
    for verb in ("create", "update", "disable", "enable", "search", "list", "view"):
        assert verb in result.output


def test_contact_enable_happy_path(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.80: contact enable clears any reason, including an unsubscribe block."""
    before = _make_contact(disabled_reason="unsubscribed: opt-out")
    after = _make_contact(disabled_reason=None)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=before),
        patch("mailpilot.database.enable_contact", return_value=after) as mock_enable,
        patch("mailpilot.operator_log.operator_event") as mock_event,
    ):
        result = runner.invoke(main, ["contact", "enable", before.id])

    assert result.exit_code == 0, result.output
    mock_enable.assert_called_once_with(mock_connection, before.id)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["contact"]["disabled_reason"] is None
    enable_events = [
        call
        for call in mock_event.call_args_list
        if call.args[:1] == ("contact.enable",)
    ]
    assert len(enable_events) == 1
    assert enable_events[0].kwargs == {
        "entity_id": before.id,
        "changed": ["disabled_reason"],
    }


def test_contact_enable_not_disabled(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.80: enabling an active contact is rejected before any write."""
    before = _make_contact(disabled_reason=None)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=before),
        patch("mailpilot.database.enable_contact") as mock_enable,
    ):
        result = runner.invoke(main, ["contact", "enable", before.id])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "not disabled" in data["message"]
    mock_enable.assert_not_called()


def test_contact_enable_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.80: enabling a missing contact yields a not_found envelope."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=None),
        patch("mailpilot.database.enable_contact") as mock_enable,
    ):
        result = runner.invoke(
            main, ["contact", "enable", "01234567-0000-7000-0000-0000000000ff"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    mock_enable.assert_not_called()


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
        patch("mailpilot.database.get_company_by_domain", return_value=company),
        patch("mailpilot.database.list_contacts", return_value=[]) as mock_list,
    ):
        result = runner.invoke(
            main,
            [
                "contact",
                "list",
                "--limit",
                "5",
                "--company-domain",
                company.domain,
            ],
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        limit=5,
        company_id=company.id,
        since=None,
        until=None,
        include_disabled=False,
        max_email_confidence=None,
        min_email_confidence=None,
        title=None,
        tag=None,
        exclude_tags=[],
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
        until=None,
        include_disabled=True,
        max_email_confidence=None,
        min_email_confidence=None,
        title=None,
        tag=None,
        exclude_tags=[],
    )


def test_contact_list_max_email_confidence(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.95: --max-email-confidence N flows to list_contacts as the filter."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_contacts", return_value=[]) as mock_list,
    ):
        result = runner.invoke(
            main, ["contact", "list", "--max-email-confidence", "40"]
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        limit=100,
        company_id=None,
        since=None,
        until=None,
        include_disabled=False,
        max_email_confidence=40,
        min_email_confidence=None,
        title=None,
        tag=None,
        exclude_tags=[],
    )


def test_contact_list_min_email_confidence(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.95: --min-email-confidence N flows to list_contacts as the lower bound."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_contacts", return_value=[]) as mock_list,
    ):
        result = runner.invoke(
            main, ["contact", "list", "--min-email-confidence", "60"]
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        limit=100,
        company_id=None,
        since=None,
        until=None,
        include_disabled=False,
        max_email_confidence=None,
        min_email_confidence=60,
        title=None,
        tag=None,
        exclude_tags=[],
    )


def test_contact_list_company_domain(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.5/§V.107: --company-domain is a Scope ref -- resolve then filter by id."""
    company = _make_company(domain="acme.com")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.get_company_by_domain", return_value=company
        ) as mock_by_domain,
        patch("mailpilot.database.list_contacts", return_value=[]) as mock_list,
    ):
        result = runner.invoke(
            main, ["contact", "list", "--company-domain", "acme.com"]
        )

    assert result.exit_code == 0
    mock_by_domain.assert_called_once_with(mock_connection, "acme.com")
    mock_list.assert_called_once_with(
        mock_connection,
        limit=100,
        company_id=company.id,
        since=None,
        until=None,
        include_disabled=False,
        max_email_confidence=None,
        min_email_confidence=None,
        title=None,
        tag=None,
        exclude_tags=[],
    )


def test_contact_list_company_domain_unknown_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.5/§V.107: unknown --company-domain exits not_found, not a silent []."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company_by_domain", return_value=None),
        patch("mailpilot.database.list_contacts") as mock_list,
    ):
        result = runner.invoke(
            main, ["contact", "list", "--company-domain", "ghost.com"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    mock_list.assert_not_called()


def test_contact_list_title(runner: CliRunner, mock_connection: MagicMock) -> None:
    """§V.115 family 5: --title flows to list_contacts as an exact-match filter.

    The exact-vs-substring semantics live in the database layer; the CLI passes
    the raw term through. Substring title matching is the ``contact search``
    verb's job (covered in tests/test_database.py).
    """
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_contacts", return_value=[]) as mock_list,
    ):
        result = runner.invoke(main, ["contact", "list", "--title", "VP"])

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        limit=100,
        company_id=None,
        since=None,
        until=None,
        include_disabled=False,
        max_email_confidence=None,
        min_email_confidence=None,
        title="VP",
        tag=None,
        exclude_tags=[],
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
        until=None,
        include_disabled=False,
        max_email_confidence=None,
        min_email_confidence=None,
        title=None,
        tag=None,
        exclude_tags=[],
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
            main,
            [
                "contact",
                "list",
                "--company-domain",
                "01234567-0000-7000-0000-0000000000c2",
            ],
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
    assert "verification_meta" not in data["contact"]


def test_contact_view_include_meta(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.144: --include-meta projects operator-only verification_meta."""
    meta = {"bouncer_status": "deliverable", "source": "hunter_pattern"}
    contact = _make_contact(verification_meta=meta)
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
        patch("mailpilot.database.load_contact_view", return_value=view),
        patch("mailpilot.database.get_contact", return_value=contact) as mock_get,
    ):
        result = runner.invoke(
            main, ["contact", "view", contact.id, "--include-meta"]
        )

    assert result.exit_code == 0
    mock_get.assert_called_once_with(mock_connection, contact.id)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["contact"]["verification_meta"] == meta


def test_skill_documents_contact_verification_meta() -> None:
    """§V.144: packaged SKILL.md documents meta write + --include-meta allowlist."""
    from importlib.resources import files

    body = files("mailpilot").joinpath("SKILL.md").read_text(encoding="utf-8")
    assert "--meta-json" in body
    assert "--include-meta" in body
    assert "verification_meta" in body
    assert "email_confidence" in body
    assert "§V." not in body
    assert "§T." not in body


def test_contact_view_not_found(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.load_contact_view", return_value=None),
    ):
        result = runner.invoke(
            main, ["contact", "view", "01234567-0000-7000-0000-0000000000ff"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "not_found"


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
        until=None,
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
                "--contact-email",
                contact.id,
                "--account-email",
                account.id,
            ],
        )
    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        limit=5,
        contact_id=contact.id,
        account_id=account.id,
        since=None,
        until=None,
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
        until=None,
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
        result = runner.invoke(
            main, ["email", "view", "01234567-0000-7000-0000-0000000000ff"]
        )
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
        result = runner.invoke(
            main,
            [
                "email",
                "list",
                "--contact-email",
                "01234567-0000-7000-0000-0000000000c1",
            ],
        )

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
        result = runner.invoke(
            main,
            [
                "email",
                "list",
                "--account-email",
                "01234567-0000-7000-0000-0000000000c3",
            ],
        )

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
        until=None,
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
                "--account-email",
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
                "--account-email",
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
                "--account-email",
                "01234567-0000-7000-0000-0000000000fe",
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
                "--account-email",
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
                "--account-email",
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
                "--account-email",
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
                "--account-email",
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
                "--account-email",
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
                "--account-email",
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
                "--account-email",
                "01234567-0000-7000-0000-0000000000fe",
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
            "--account-email",
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
                "--account-email",
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
                "--account-email",
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
                "--account-email",
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
        "goal": "",
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
                "--account-email",
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


def test_workflow_create_with_goal_and_instructions(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    workflow = _make_workflow(goal="Book demo", instructions="You are a sales rep.")
    activated = _make_workflow(
        status="active", goal="Book demo", instructions="You are a sales rep."
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
                "--account-email",
                _ACCOUNT_ID,
                "--goal",
                "Book demo",
                "--instructions-file",
                str(instructions_file),
            ],
        )

    assert result.exit_code == 0, result.output
    mock_update.assert_called_once_with(
        mock_connection,
        _WORKFLOW_ID,
        goal="Book demo",
        instructions="You are a sales rep.",
    )
    mock_activate.assert_called_once_with(mock_connection, _WORKFLOW_ID)
    data = json.loads(result.output)
    assert data["workflow"]["status"] == "active"


def test_workflow_create_with_inline_instructions(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    workflow = _make_workflow(goal="Book demo", instructions="You are a sales rep.")
    activated = _make_workflow(
        status="active", goal="Book demo", instructions="You are a sales rep."
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
                "--account-email",
                _ACCOUNT_ID,
                "--goal",
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
                "--account-email",
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
            "--account-email",
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
                "--account-email",
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
                "--account-email",
                "01234567-0000-7000-0000-0000000000c3",
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
    updated = _make_workflow(goal="Book demo", instructions="You are a sales rep.")
    activated = _make_workflow(
        status="active", goal="Book demo", instructions="You are a sales rep."
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
                "--account-email",
                _ACCOUNT_ID,
                "--goal",
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
    workflow = _make_workflow(goal="Book demo", instructions="You are a sales rep.")
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
                "--account-email",
                _ACCOUNT_ID,
                "--goal",
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
                "--account-email",
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
                "--account-email",
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
                "--account-email",
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


def test_workflow_update_account_email_rebinds(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.103: update re-binds the owning account -- the sole non-def write."""
    other_id = "01234567-0000-7000-0000-0000000000aa"
    before = _make_workflow()
    other = _make_account(id=other_id, email="other@example.com")
    updated = _make_workflow(account_id=other_id, account_email="other@example.com")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=before),
        patch("mailpilot.database.get_account", return_value=other),
        patch(
            "mailpilot.database.update_workflow", return_value=updated
        ) as mock_update,
    ):
        result = runner.invoke(
            main, ["workflow", "update", _WORKFLOW_ID, "--account-email", other_id]
        )

    assert result.exit_code == 0, result.output
    mock_update.assert_called_once_with(
        mock_connection, _WORKFLOW_ID, account_id=other_id
    )
    data = json.loads(result.output)
    assert data["workflow"]["account_id"] == other_id


def test_workflow_update_requires_account_email(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.103: with no non-def field supplied, update is a validation_error."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(main, ["workflow", "update", _WORKFLOW_ID])

    assert result.exit_code == 1, result.output
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "import-only" in data["message"]


@pytest.mark.parametrize("flag", ["--name", "--goal", "--instructions", "--theme"])
def test_workflow_update_rejects_def_field_options(
    runner: CliRunner, flag: str
) -> None:
    """§V.103: def-field options are gone from `workflow update` (import-only)."""
    result = runner.invoke(main, ["workflow", "update", _WORKFLOW_ID, flag, "x"])

    assert result.exit_code == 2
    assert "No such option" in result.output


def test_workflow_update_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow_by_name", return_value=None),
    ):
        result = runner.invoke(
            main,
            ["workflow", "update", "nope", "--account-email", "test@example.com"],
        )
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
        mock_connection,
        account_id=None,
        status=None,
        workflow_type=None,
        template=None,
        limit=100,
        since=None,
        until=None,
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
        result = runner.invoke(
            main, ["workflow", "list", "--account-email", _ACCOUNT_ID]
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        account_id=_ACCOUNT_ID,
        status=None,
        workflow_type=None,
        template=None,
        limit=100,
        since=None,
        until=None,
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
            main,
            [
                "workflow",
                "list",
                "--account-email",
                "01234567-0000-7000-0000-0000000000c3",
            ],
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
            ["workflow", "list", "--status", "active", "--direction", "outbound"],
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
        until=None,
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
        patch("mailpilot.database.get_workflow_by_name", return_value=None),
        patch("mailpilot.database.get_workflow", return_value=None),
    ):
        result = runner.invoke(main, ["workflow", "view", "nope"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_workflow_stats_envelope(runner: CliRunner, mock_connection: MagicMock) -> None:
    """§V.132/§V.4: `workflow stats` ships the aggregate under `workflow_stats`."""
    stats = WorkflowStats(
        workflow_id=_WORKFLOW_ID,
        workflow_name="Demo outreach",
        enrolled=8,
        sent=5,
        bounced=1,
        replied=3,
        meeting_booked=1,
        contact_later=1,
        do_not_contact=1,
        active=4,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow_stats", return_value=stats),
    ):
        result = runner.invoke(main, ["workflow", "stats", _WORKFLOW_ID])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert "workflow" not in data
    funnel = data["workflow_stats"]
    assert funnel["workflow_id"] == _WORKFLOW_ID
    assert funnel["enrolled"] == 8
    assert funnel["sent"] == 5
    assert funnel["bounced"] == 1
    assert funnel["replied"] == 3
    assert funnel["meeting_booked"] == 1
    assert funnel["contact_later"] == 1
    assert funnel["do_not_contact"] == 1
    assert funnel["active"] == 4


def test_workflow_stats_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.107: an unknown workflow ref exits not_found."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow_by_name", return_value=None),
        patch("mailpilot.database.get_workflow_stats", return_value=None),
    ):
        result = runner.invoke(main, ["workflow", "stats", "nope"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_workflow_check_envelope(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    """§V.134/§V.4: `workflow check` ships the report under `workflow_check`.

    Report-only: out_of_sync + orphaned states still exit 0 with ``ok:true``
    -- the check is never a deploy gate (cf ``db check`` which exits 1).
    """
    toml_path = tmp_path / "demo-flow.toml"
    toml_path.write_text(
        'name = "demo-flow"\ntemplate = "outbound-general"\ntheme = "blue"\n'
        'goal = "Book demos."\ninstructions = "Be brief."\n'
    )
    report = WorkflowCheck(
        workflows=[
            WorkflowCheckEntry(
                name="demo-flow",
                state="out_of_sync",
                catalog_hash="a" * 64,
                row_hash="b" * 64,
            ),
            WorkflowCheckEntry(
                name="ghost-flow",
                state="orphaned",
                catalog_hash=None,
                row_hash="c" * 64,
            ),
        ],
        in_sync=0,
        out_of_sync=1,
        not_imported=0,
        orphaned=1,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.check_workflow_wording", return_value=report
        ) as mock_check,
    ):
        result = runner.invoke(main, ["workflow", "check", "--file", str(tmp_path)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ok"] is True
    assert "workflow" not in data
    payload = data["workflow_check"]
    assert payload["out_of_sync"] == 1
    assert payload["orphaned"] == 1
    assert {w["name"] for w in payload["workflows"]} == {"demo-flow", "ghost-flow"}
    # The catalog passed to the DB join is keyed by the TOML 'name' field.
    catalog_arg = mock_check.call_args.args[1]
    assert "demo-flow" in catalog_arg


def test_workflow_check_keys_on_name_field_not_stem(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    """§V.134: check joins by the TOML `name` field, never the file stem."""
    toml_path = tmp_path / "file-stem.toml"
    toml_path.write_text('name = "real-name"\ntemplate = "outbound-general"\n')
    report = WorkflowCheck(
        workflows=[], in_sync=0, out_of_sync=0, not_imported=0, orphaned=0
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.check_workflow_wording", return_value=report
        ) as mock_check,
    ):
        result = runner.invoke(main, ["workflow", "check", "--file", str(tmp_path)])
    assert result.exit_code == 0, result.output
    catalog_arg = mock_check.call_args.args[1]
    assert "real-name" in catalog_arg
    assert "file-stem" not in catalog_arg


def test_workflow_check_malformed_toml_errors(
    runner: CliRunner, tmp_path: pathlib.Path
) -> None:
    """§V.54: a malformed catalog file exits validation_error before the DB join."""
    toml_path = tmp_path / "broken-flow.toml"
    toml_path.write_text("name = \nthis is not valid toml")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.check_workflow_wording") as mock_check,
    ):
        result = runner.invoke(main, ["workflow", "check", "--file", str(toml_path)])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    mock_check.assert_not_called()


def test_workflow_check_multiple_files_scope_to_catalog(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    """§V.134: repeatable --file reads every file and scopes the report to them."""
    alpha = tmp_path / "alpha.toml"
    alpha.write_text('name = "alpha"\ntemplate = "outbound-general"\n')
    beta = tmp_path / "beta.toml"
    beta.write_text('name = "beta"\ntemplate = "outbound-general"\n')
    report = WorkflowCheck(
        workflows=[], in_sync=0, out_of_sync=0, not_imported=0, orphaned=0
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.check_workflow_wording", return_value=report
        ) as mock_check,
    ):
        result = runner.invoke(
            main,
            ["workflow", "check", "--file", str(alpha), "--file", str(beta)],
        )
    assert result.exit_code == 0, result.output
    catalog_arg = mock_check.call_args.args[1]
    assert {"alpha", "beta"} <= set(catalog_arg)
    assert mock_check.call_args.kwargs["scope_to_catalog"] is True


def test_workflow_check_directory_reports_orphaned(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    """§V.134: a directory is the full catalog, so orphaned rows still surface."""
    (tmp_path / "x.toml").write_text('name = "x"\ntemplate = "outbound-general"\n')
    report = WorkflowCheck(
        workflows=[], in_sync=0, out_of_sync=0, not_imported=0, orphaned=0
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.check_workflow_wording", return_value=report
        ) as mock_check,
    ):
        result = runner.invoke(main, ["workflow", "check", "--file", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert mock_check.call_args.kwargs["scope_to_catalog"] is False


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
        goal="Book demo",
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


def test_workflow_start_missing_goal(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.activate_workflow",
            side_effect=ValueError("goal must be non-empty to activate"),
        ),
    ):
        result = runner.invoke(main, ["workflow", "start", _WORKFLOW_ID])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "invalid_state"
    assert "re-import" in data["message"]
    assert "goal" in data["message"]


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
    assert "re-import" in data["message"]
    assert "instructions" in data["message"]


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


def test_workflow_export_writes_toml_files_and_json_envelope(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    """§V.103/§V.3: export writes one '*.toml'/workflow to --out-dir; stdout = JSON.

    The TOML file re-parses to the def fields, and stdout carries only the JSON
    status envelope of paths written -- TOML never reaches stdout.
    """
    import tomllib

    account = _make_account()
    workflow = _make_workflow(
        name="Demo outreach",
        goal="Book demos",
        instructions="You are a sales rep.\nCite the source file.\n",
        theme="green",
    )
    out_dir = tmp_path / "catalog"
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch(
            "mailpilot.database.list_workflows_full", return_value=[workflow]
        ) as mock_list,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "export",
                "--account-email",
                _ACCOUNT_ID,
                "--out-dir",
                str(out_dir),
            ],
        )

    assert result.exit_code == 0, result.output
    mock_list.assert_called_once_with(mock_connection, _ACCOUNT_ID)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert len(data["workflows"]) == 1
    written = data["workflows"][0]
    assert written["name"] == "Demo outreach"
    toml_path = pathlib.Path(written["path"])
    assert toml_path.parent == out_dir
    assert toml_path.suffix == ".toml"

    with toml_path.open("rb") as handle:
        parsed = tomllib.load(handle)
    assert parsed == {
        "name": "Demo outreach",
        "template": "outbound-general",
        "theme": "green",
        "goal": "Book demos",
        "instructions": "You are a sales rep.\nCite the source file.\n",
    }


def test_workflow_export_account_not_found(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
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
                "export",
                "--account-email",
                "01234567-0000-7000-0000-0000000000c3",
                "--out-dir",
                str(tmp_path),
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "account" in data["message"]


def test_workflow_export_preserves_db_order(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
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
            main,
            [
                "workflow",
                "export",
                "--account-email",
                _ACCOUNT_ID,
                "--out-dir",
                str(tmp_path),
            ],
        )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    names = [row["name"] for row in data["workflows"]]
    assert names == ["Alpha", "Bravo", "Charlie"]


# -- workflow import (TOML-only, §V.103) ---------------------------------------


def _import_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "demo-outreach",
        "template": "outbound-general",
        "goal": "Book demos",
        "instructions": "You are a sales rep.",
        "theme": "green",
    }
    return {**base, **overrides}


def _write_workflow_toml(path: pathlib.Path, payload: dict[str, Any]) -> None:
    """Write a single-workflow ``.toml`` from an import payload dict (test helper).

    Single-line fields use TOML basic strings; ``instructions`` uses a multi-line
    literal string, mirroring the catalog convention ``workflow export`` emits.
    """
    lines: list[str] = []
    for key in ("name", "template", "theme", "goal"):
        if key in payload:
            value = payload[key].replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key} = "{value}"')
    body = "\n".join(lines)
    if "instructions" in payload:
        body += f"\ninstructions = '''\n{payload['instructions']}'''"
    path.write_text(body + "\n")


def test_workflow_import_create_path_activates(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    account = _make_account()
    created = _make_workflow(theme="green")
    file = tmp_path / "demo-outreach.toml"
    _write_workflow_toml(file, _import_payload())
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
                "--account-email",
                _ACCOUNT_ID,
                "--file",
                str(file),
            ],
        )

    assert result.exit_code == 0, result.output
    mock_create.assert_called_once_with(
        mock_connection,
        name="demo-outreach",
        template="outbound-general",
        account_id=_ACCOUNT_ID,
        theme="green",
    )
    mock_update.assert_called_once_with(
        mock_connection,
        created.id,
        goal="Book demos",
        instructions="You are a sales rep.",
    )
    mock_activate.assert_called_once_with(mock_connection, created.id)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["workflows"] == [{"name": "demo-outreach", "action": "created"}]
    assert data["applied"] == 1
    assert data["rejected"] == 0
    assert data["record_count"] == 1


def test_workflow_import_create_path_draft_no_activation(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    account = _make_account()
    created = _make_workflow(goal="", instructions="")
    file = tmp_path / "demo-outreach.toml"
    _write_workflow_toml(file, _import_payload(goal="Book demos", instructions=""))
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
                "--account-email",
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
        name="demo-outreach",
        goal="Old goal",
        instructions="Old instructions",
        theme="blue",
        status="active",
    )
    file = tmp_path / "demo-outreach.toml"
    _write_workflow_toml(
        file,
        _import_payload(
            goal="New goal",
            instructions="Old instructions",
            theme="blue",
        ),
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
                "--account-email",
                _ACCOUNT_ID,
                "--file",
                str(file),
            ],
        )

    assert result.exit_code == 0, result.output
    mock_create.assert_not_called()
    mock_activate.assert_not_called()
    mock_update.assert_called_once_with(mock_connection, existing.id, goal="New goal")
    data = json.loads(result.output)
    assert data["workflows"] == [{"name": "demo-outreach", "action": "updated"}]


def test_workflow_import_unchanged_no_mutation(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    account = _make_account()
    existing = _make_workflow(
        name="demo-outreach",
        goal="Book demos",
        instructions="You are a sales rep.",
        theme="green",
        status="active",
    )
    file = tmp_path / "demo-outreach.toml"
    _write_workflow_toml(file, _import_payload())
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
                "--account-email",
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
    assert data["workflows"] == [{"name": "demo-outreach", "action": "unchanged"}]


def test_workflow_import_template_immutable_row_error(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    """§V.63/§V.103: a per-row template_immutable error does not abort the batch.

    Two ``.toml`` files in a catalog dir: the first collides with an existing
    workflow on a changed ``template`` (error), the second is a fresh create.
    """
    account = _make_account()
    existing = _make_workflow(
        name="demo-outreach", template="inbound-general", type="inbound"
    )
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    _write_workflow_toml(
        catalog / "demo-outreach.toml", _import_payload(template="outbound-general")
    )
    _write_workflow_toml(
        catalog / "other-workflow.toml",
        _import_payload(name="other-workflow", template="inbound-general"),
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.list_workflows_full", return_value=[existing]),
        patch(
            "mailpilot.database.create_workflow",
            return_value=_make_workflow(name="other-workflow"),
        ) as mock_create,
        patch("mailpilot.database.update_workflow", return_value=existing),
        patch("mailpilot.database.activate_workflow"),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "import",
                "--account-email",
                _ACCOUNT_ID,
                "--file",
                str(catalog),
            ],
        )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ok"] is True
    rows = data["workflows"]
    assert len(rows) == 2
    by_name = {row["name"]: row for row in rows}
    assert by_name["demo-outreach"]["error"] == "template_immutable"
    assert "inbound-general" in by_name["demo-outreach"]["message"]
    assert by_name["other-workflow"] == {"name": "other-workflow", "action": "created"}
    assert data["applied"] == 1
    assert data["rejected"] == 1
    assert data["record_count"] == 2
    mock_create.assert_called_once()


def test_workflow_import_requires_file(runner: CliRunner) -> None:
    """§V.103/§V.63: omitting --file -> validation_error envelope, exit 1 (no stdin).

    Workflow import is TOML-only; with no ``--file`` it never falls back to stdin
    -- the DB is never initialized, so no ``initialize_database`` patch is needed.
    """
    with patch("mailpilot.settings.get_settings", return_value=make_test_settings()):
        result = runner.invoke(
            main, ["workflow", "import", "--account-email", _ACCOUNT_ID]
        )

    assert result.exit_code == 1, result.output
    data = json.loads(result.stderr)
    assert data["error"] == "validation_error"
    assert "no input" in data["message"]
    assert "--file" in data["message"]


def test_workflow_import_account_not_found(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    file = tmp_path / "wf.toml"
    _write_workflow_toml(file, _import_payload())
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
                "--account-email",
                "01234567-0000-7000-0000-0000000000c3",
                "--file",
                str(file),
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "account" in data["message"]


# -- §V.103 TOML catalog import ------------------------------------------------


def test_workflow_import_toml_multiline_literal_preserved(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    """§V.103: TOML multi-line literal instructions pass through verbatim.

    No escape processing -- pipes and quotes survive, which is the whole reason
    the catalog uses a literal string instead of a JSON-escaped one-liner.
    """
    account = _make_account()
    created = _make_workflow(theme="blue")
    toml_file = tmp_path / "demo.toml"
    toml_file.write_text(
        'name = "demo"\n'
        'template = "inbound-google-drive"\n'
        'theme = "blue"\n'
        'goal = "Answer questions."\n'
        "instructions = '''\n"
        'Line one with a literal | pipe and "quotes".\n'
        "Line two.\n"
        "'''\n"
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.list_workflows_full", return_value=[]),
        patch("mailpilot.database.create_workflow", return_value=created),
        patch(
            "mailpilot.database.update_workflow", return_value=created
        ) as mock_update,
        patch("mailpilot.database.activate_workflow", return_value=created),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "import",
                "--account-email",
                _ACCOUNT_ID,
                "--file",
                str(toml_file),
            ],
        )

    assert result.exit_code == 0, result.output
    _, kwargs = mock_update.call_args
    assert (
        kwargs["instructions"]
        == 'Line one with a literal | pipe and "quotes".\nLine two.\n'
    )


def test_workflow_import_directory_globs_toml(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    """§V.103: --file <dir> imports every *.toml in sorted order, ignores non-TOML."""
    account = _make_account()
    created = _make_workflow()
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "alpha.toml").write_text(
        'name = "alpha"\ntemplate = "outbound-general"\n'
        'goal = "o"\ninstructions = "i"\ntheme = "green"\n'
    )
    (catalog / "bravo.toml").write_text(
        'name = "bravo"\ntemplate = "inbound-general"\n'
        'goal = "o"\ninstructions = "i"\ntheme = "blue"\n'
    )
    (catalog / "notes.md").write_text("not a workflow")
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
            [
                "workflow",
                "import",
                "--account-email",
                _ACCOUNT_ID,
                "--file",
                str(catalog),
            ],
        )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert [row["name"] for row in data["workflows"]] == ["alpha", "bravo"]


def test_workflow_import_toml_malformed_top_error(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    """§V.54/§V.103: a single malformed .toml -> top-level validation_error, exit 1."""
    account = _make_account()
    toml_file = tmp_path / "bad.toml"
    toml_file.write_text('name = "x"\ntemplate =\n')
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
                "--account-email",
                _ACCOUNT_ID,
                "--file",
                str(toml_file),
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "toml" in data["message"].lower()


def test_workflow_import_directory_parse_error_continues_batch(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    """§V.63/§V.103: a malformed file in a dir is a per-row error; batch continues."""
    account = _make_account()
    created = _make_workflow(name="good")
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "bad.toml").write_text("template = =\n")
    (catalog / "good.toml").write_text(
        'name = "good"\ntemplate = "outbound-general"\n'
        'goal = "o"\ninstructions = "i"\ntheme = "green"\n'
    )
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
            [
                "workflow",
                "import",
                "--account-email",
                _ACCOUNT_ID,
                "--file",
                str(catalog),
            ],
        )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    rows = data["workflows"]
    assert len(rows) == 2
    assert rows[0]["error"] == "validation_error"
    assert "bad.toml" in rows[0]["message"]
    assert rows[1] == {"name": "good", "action": "created"}
    assert data["applied"] == 1
    assert data["rejected"] == 1
    assert data["record_count"] == 2


def test_workflow_import_toml_missing_required_field(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    """§V.103/§B.123: sole row missing a required field -> import_failed, exit 1.

    The per-row ``validation_error`` stays inside ``workflows``, but with zero
    rows applied the terminal envelope is the loud-failure ``import_failed``
    error on stderr, never ``ok: true``.
    """
    account = _make_account()
    toml_file = tmp_path / "wf.toml"
    toml_file.write_text(
        'name = "No template"\ngoal = "o"\ninstructions = "i"\ntheme = "blue"\n'
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.list_workflows_full", return_value=[]),
        patch("mailpilot.database.create_workflow") as mock_create,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "import",
                "--account-email",
                _ACCOUNT_ID,
                "--file",
                str(toml_file),
            ],
        )

    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    data = json.loads(result.stderr)
    assert data["ok"] is False
    assert data["error"] == "import_failed"
    assert data["applied"] == 0
    assert data["rejected"] == 1
    rows = data["workflows"]
    assert rows[0]["name"] == "No template"
    assert rows[0]["error"] == "validation_error"
    mock_create.assert_not_called()


def test_workflow_import_rejects_non_kebab_name(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    """§V.103/§B.123: a non-kebab `name` rejects the sole row -> import_failed, exit 1."""
    account = _make_account()
    toml_file = tmp_path / "demo-outreach.toml"
    _write_workflow_toml(toml_file, _import_payload(name="Demo_Outreach"))
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.list_workflows_full", return_value=[]),
        patch("mailpilot.database.create_workflow") as mock_create,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "import",
                "--account-email",
                _ACCOUNT_ID,
                "--file",
                str(toml_file),
            ],
        )

    assert result.exit_code == 1, result.output
    data = json.loads(result.stderr)
    assert data["ok"] is False
    assert data["error"] == "import_failed"
    assert data["applied"] == 0
    assert data["rejected"] == 1
    row = data["workflows"][0]
    assert row["name"] == "Demo_Outreach"
    assert row["error"] == "validation_error"
    assert "kebab" in row["message"]
    mock_create.assert_not_called()


def test_workflow_import_rejects_name_not_file_stem(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    """§V.103/§B.123: a kebab `name` unequal to the file stem rejects the sole row.

    The file stem is the canonical cross-environment key, so the in-file
    ``name`` must match it; here ``demo-outreach`` lives in ``other-name.toml``.
    Zero rows applied -> ``import_failed`` on stderr, exit 1.
    """
    account = _make_account()
    toml_file = tmp_path / "other-name.toml"
    _write_workflow_toml(toml_file, _import_payload(name="demo-outreach"))
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.list_workflows_full", return_value=[]),
        patch("mailpilot.database.create_workflow") as mock_create,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "import",
                "--account-email",
                _ACCOUNT_ID,
                "--file",
                str(toml_file),
            ],
        )

    assert result.exit_code == 1, result.output
    data = json.loads(result.stderr)
    assert data["ok"] is False
    assert data["error"] == "import_failed"
    row = data["workflows"][0]
    assert row["name"] == "demo-outreach"
    assert row["error"] == "validation_error"
    assert "stem" in row["message"]
    mock_create.assert_not_called()


def test_workflow_import_zero_applied_directory_exits_nonzero(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    """§V.103/§B.123: a directory import applying zero rows fails loudly.

    Both rows are rejected (one malformed TOML, one non-kebab ``name``), so the
    command exits 1 with an ``import_failed`` envelope on stderr; the per-row
    errors ride inside the envelope under ``workflows``.
    """
    account = _make_account()
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "bad.toml").write_text("template = =\n")
    _write_workflow_toml(
        catalog / "demo-outreach.toml", _import_payload(name="Demo_Outreach")
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.list_workflows_full", return_value=[]),
        patch("mailpilot.database.create_workflow") as mock_create,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "import",
                "--account-email",
                _ACCOUNT_ID,
                "--file",
                str(catalog),
            ],
        )

    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    data = json.loads(result.stderr)
    assert data["ok"] is False
    assert data["error"] == "import_failed"
    assert data["applied"] == 0
    assert data["rejected"] == 2
    assert len(data["workflows"]) == 2
    mock_create.assert_not_called()


def test_workflow_import_empty_directory_exits_nonzero(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    """§V.103/§B.123: a directory holding no ``*.toml`` imports nothing -> exit 1."""
    account = _make_account()
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.list_workflows_full", return_value=[]),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "import",
                "--account-email",
                _ACCOUNT_ID,
                "--file",
                str(catalog),
            ],
        )

    assert result.exit_code == 1, result.output
    data = json.loads(result.stderr)
    assert data["ok"] is False
    assert data["error"] == "import_failed"
    assert data["applied"] == 0
    assert data["rejected"] == 0
    assert data["workflows"] == []


# -- enrollment run ------------------------------------------------------------


def test_enrollment_run(runner: CliRunner, mock_connection: MagicMock) -> None:
    """Manual run invokes the agent directly -- no task row, no NOTIFY race.

    Going through ``create_task`` triggers ``pg_notify('task_pending')``,
    which races a parallel ``mailpilot run`` loop for the same task.
    Synchronous CLI runs bypass the queue entirely.
    """
    workflow = _make_workflow(
        status="active",
        goal="Book demo",
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


def test_enrollment_run_disabled_enrollment(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """Manual run is rejected when the enrollment is not active (§V.15/§V.83)."""
    workflow = _make_workflow(status="active")
    contact = Contact(
        id=_CONTACT_ID,
        email="lead@acme.com",
        created_at=_NOW,
        updated_at=_NOW,
    )
    disabled = _make_enrollment(status="disabled", disabled_reason="operator hold")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.get_enrollment_by_id", return_value=disabled),
        patch("mailpilot.agent.invoke_workflow_agent") as mock_invoke,
    ):
        result = runner.invoke(main, ["enrollment", "run", _ENROLLMENT_ID])
    assert result.exit_code == 1, result.output
    mock_invoke.assert_not_called()
    data = json.loads(result.output)
    assert data["error"] == "invalid_state"
    assert "disabled" in data["message"]


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


# -- activity add --------------------------------------------------------------


def test_activity_add(runner: CliRunner, mock_connection: MagicMock) -> None:
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
                "add",
                "--contact-email",
                "01234567-0000-7000-0000-000000000003",
                "--type",
                "email_sent",
                "--summary",
                "Sent intro",
            ],
        )

    assert result.exit_code == 0
    mock_create.assert_called_once_with(
        mock_connection,
        contact_id="01234567-0000-7000-0000-000000000003",
        company_id=None,
        activity_type="email_sent",
        summary="Sent intro",
        detail={},
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["activity"]["type"] == "email_sent"


def test_activity_add_company_only(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """Company-only activity rows are allowed (#102 sugg 2)."""
    activity = _make_activity(
        contact_id=None,
        company_id="01234567-0000-7000-0000-000000000002",
        type="note_added",
    )
    company = _make_company(id="01234567-0000-7000-0000-000000000002")
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
                "add",
                "--company-domain",
                "01234567-0000-7000-0000-000000000002",
                "--type",
                "note_added",
                "--summary",
                "Company note",
            ],
        )

    assert result.exit_code == 0


def test_activity_add_with_detail(
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
                "add",
                "--contact-email",
                "01234567-0000-7000-0000-000000000003",
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
        contact_id="01234567-0000-7000-0000-000000000003",
        company_id=None,
        activity_type="email_sent",
        summary="Sent intro",
        detail={"email_id": "e-1"},
    )


def test_activity_add_empty_summary(
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
                "add",
                "--contact-email",
                "01234567-0000-7000-0000-000000000003",
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


def test_activity_add_contact_not_found(
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
                "add",
                "--contact-email",
                "01234567-0000-7000-0000-0000000000c1",
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


def test_activity_add_company_not_found(
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
                "add",
                "--contact-email",
                "01234567-0000-7000-0000-000000000003",
                "--type",
                "note_added",
                "--summary",
                "Test",
                "--company-domain",
                "01234567-0000-7000-0000-0000000000c2",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "company" in data["message"]


def test_activity_create_verb_retired(runner: CliRunner) -> None:
    """§I.cli: the owner-attaching verb is `activity add`; `activity create`
    no longer resolves (the standard reconciles the outlier to `add`)."""
    with patch("mailpilot.settings.get_settings", return_value=make_test_settings()):
        result = runner.invoke(
            main,
            [
                "activity",
                "create",
                "--contact-email",
                "01234567-0000-7000-0000-000000000003",
                "--type",
                "email_sent",
                "--summary",
                "Sent intro",
            ],
        )

    assert result.exit_code == 2
    assert "No such command" in result.output


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
        result = runner.invoke(
            main,
            [
                "activity",
                "list",
                "--contact-email",
                "01234567-0000-7000-0000-000000000003",
            ],
        )

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
        result = runner.invoke(
            main,
            [
                "activity",
                "list",
                "--contact-email",
                "01234567-0000-7000-0000-000000000003",
            ],
        )

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
                "--contact-email",
                "01234567-0000-7000-0000-000000000003",
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
        contact_id="01234567-0000-7000-0000-000000000003",
        company_id=None,
        activity_type="email_sent",
        limit=5,
        since="2024-01-01T00:00:00Z",
        until=None,
    )


def test_activity_list_no_filter(runner: CliRunner, mock_connection: MagicMock) -> None:
    """activity list without --contact-email or --company-domain should error."""
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
            main,
            [
                "activity",
                "list",
                "--contact-email",
                "01234567-0000-7000-0000-0000000000c1",
            ],
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
            main,
            [
                "activity",
                "list",
                "--company-domain",
                "01234567-0000-7000-0000-0000000000c2",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "company" in data["message"]


# -- Tag -----------------------------------------------------------------------


_TAG_ID = "01234567-0000-7000-0000-000000000011"
_TAG_CONTACT_ID = "01234567-0000-7000-0000-000000000003"
_TAG_COMPANY_ID = "01234567-0000-7000-0000-000000000002"


def _make_tag(**overrides: Any) -> Tag:
    defaults: dict[str, Any] = {
        "id": _TAG_ID,
        "name": "prospect",
        "disabled_reason": None,
        "created_at": _NOW,
    }
    return Tag(**{**defaults, **overrides})


def _make_tag_summary(**overrides: Any) -> TagSummary:
    defaults: dict[str, Any] = {
        "id": _TAG_ID,
        "name": "prospect",
        "usage_count": 0,
        "disabled_reason": None,
        "created_at": _NOW,
    }
    return TagSummary(**{**defaults, **overrides})


def _make_tag_assignment(**overrides: Any) -> TagAssignment:
    defaults: dict[str, Any] = {
        "id": "01234567-0000-7000-0000-0000000000a1",
        "tag_id": _TAG_ID,
        "contact_id": _TAG_CONTACT_ID,
        "company_id": None,
        "created_at": _NOW,
    }
    return TagAssignment(**{**defaults, **overrides})


# -- tag create ----------------------------------------------------------------


def test_tag_create(runner: CliRunner, mock_connection: MagicMock) -> None:
    tag = _make_tag(name="vip")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_tag", return_value=tag) as mock_create,
        patch("mailpilot.operator_log.operator_event") as mock_event,
    ):
        result = runner.invoke(main, ["tag", "create", "vip"])

    assert result.exit_code == 0
    mock_create.assert_called_once_with(mock_connection, name="vip")
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["tag"]["name"] == "vip"
    create_events = [
        call for call in mock_event.call_args_list if call.args[:1] == ("tag.create",)
    ]
    assert len(create_events) == 1


def test_tag_create_already_exists(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_tag", return_value=None),
    ):
        result = runner.invoke(main, ["tag", "create", "vip"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "already_exists"


def test_tag_create_empty_name(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(main, ["tag", "create", ""])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "name" in data["message"]


def test_tag_create_rejects_invalid_name(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.create_tag",
            side_effect=ValueError("invalid tag name: 'hot/lead'"),
        ),
    ):
        result = runner.invoke(main, ["tag", "create", "hot/lead"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "invalid tag" in data["message"].lower()


# -- tag view ------------------------------------------------------------------


def test_tag_view(runner: CliRunner, mock_connection: MagicMock) -> None:
    summary = _make_tag_summary(name="vip", usage_count=3)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.get_tag_summary_by_name", return_value=summary
        ) as mock_view,
    ):
        result = runner.invoke(main, ["tag", "view", "vip"])

    assert result.exit_code == 0
    mock_view.assert_called_once_with(mock_connection, "vip")
    data = json.loads(result.output)
    assert data["tag"]["name"] == "vip"
    assert data["tag"]["usage_count"] == 3


def test_tag_view_not_found(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_tag_summary_by_name", return_value=None),
    ):
        result = runner.invoke(main, ["tag", "view", "ghost"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


# -- tag add -------------------------------------------------------------------


def test_tag_add_on_contact(runner: CliRunner, mock_connection: MagicMock) -> None:
    tag = _make_tag(name="prospect")
    contact = _make_contact(id=_TAG_CONTACT_ID)
    assignment = _make_tag_assignment()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_tag_by_name", return_value=tag),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch(
            "mailpilot.database.assign_tag_to_contact", return_value=assignment
        ) as mock_assign,
    ):
        result = runner.invoke(
            main,
            ["tag", "add", "--tag", "prospect", "--contact-email", _TAG_CONTACT_ID],
        )

    assert result.exit_code == 0
    mock_assign.assert_called_once_with(
        mock_connection, tag_id=_TAG_ID, contact_id=_TAG_CONTACT_ID
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["tag_assignment"]["tag_id"] == _TAG_ID
    assert data["tag_assignment"]["contact_id"] == _TAG_CONTACT_ID


def test_tag_add_on_company(runner: CliRunner, mock_connection: MagicMock) -> None:
    tag = _make_tag(name="enterprise")
    company = _make_company(id=_TAG_COMPANY_ID)
    assignment = _make_tag_assignment(contact_id=None, company_id=_TAG_COMPANY_ID)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_tag_by_name", return_value=tag),
        patch("mailpilot.database.get_company", return_value=company),
        patch(
            "mailpilot.database.assign_tag_to_company", return_value=assignment
        ) as mock_assign,
    ):
        result = runner.invoke(
            main,
            ["tag", "add", "--tag", "enterprise", "--company-domain", _TAG_COMPANY_ID],
        )

    assert result.exit_code == 0
    mock_assign.assert_called_once_with(
        mock_connection, tag_id=_TAG_ID, company_id=_TAG_COMPANY_ID
    )


def test_tag_add_undefined_tag_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.116: `tag add` errors not_found on an undefined tag, never creates it."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_tag_by_name", return_value=None),
    ):
        result = runner.invoke(
            main,
            ["tag", "add", "--tag", "ghost", "--contact-email", _TAG_CONTACT_ID],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "tag" in data["message"]


def test_tag_add_already_linked(runner: CliRunner, mock_connection: MagicMock) -> None:
    tag = _make_tag(name="prospect")
    contact = _make_contact(id=_TAG_CONTACT_ID)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_tag_by_name", return_value=tag),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.assign_tag_to_contact", return_value=None),
    ):
        result = runner.invoke(
            main,
            ["tag", "add", "--tag", "prospect", "--contact-email", _TAG_CONTACT_ID],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "already_exists"


def test_tag_add_contact_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    tag = _make_tag(name="prospect")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_tag_by_name", return_value=tag),
        patch("mailpilot.database.get_contact", return_value=None),
    ):
        result = runner.invoke(
            main,
            [
                "tag",
                "add",
                "--tag",
                "prospect",
                "--contact-email",
                "01234567-0000-7000-0000-0000000000c1",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "contact" in data["message"]


def test_tag_add_no_owner(runner: CliRunner, mock_connection: MagicMock) -> None:
    """tag add without --contact-email or --company-domain should error."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(main, ["tag", "add", "--tag", "prospect"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"


def test_tag_add_mixed_owner_kinds_rejected(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.141: owner-kind XOR — cannot mix company and contact owners."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(
            main,
            [
                "tag",
                "add",
                "--tag",
                "prospect",
                "--company-domain",
                "a.com",
                "--contact-email",
                "x@y.com",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "not both" in data["message"]


def test_tag_add_multi_company_results_envelope(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.141: one tag on multiple companies → results envelope, exit 0."""
    tag = _make_tag(name="acumatica-var")
    company_a = _make_company(id=_TAG_COMPANY_ID, domain="a.com")
    company_b_id = "01234567-0000-7000-0000-0000000000b2"
    company_b = _make_company(id=company_b_id, domain="b.com")
    assignment = _make_tag_assignment(contact_id=None, company_id=_TAG_COMPANY_ID)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_tag_by_name", return_value=tag),
        patch(
            "mailpilot.database.get_company_by_domain",
            side_effect=lambda _c, domain: {
                "a.com": company_a,
                "b.com": company_b,
            }.get(domain),
        ),
        patch(
            "mailpilot.database.assign_tag_to_company", return_value=assignment
        ) as mock_assign,
    ):
        result = runner.invoke(
            main,
            [
                "tag",
                "add",
                "--tag",
                "acumatica-var",
                "--company-domain",
                "a.com",
                "--company-domain",
                "b.com",
            ],
        )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["record_count"] == 2
    assert data["results"] == [
        {"ref": "a.com", "status": "ok"},
        {"ref": "b.com", "status": "ok"},
    ]
    assert mock_assign.call_count == 2
    mock_assign.assert_any_call(
        mock_connection, tag_id=_TAG_ID, company_id=_TAG_COMPANY_ID
    )
    mock_assign.assert_any_call(
        mock_connection, tag_id=_TAG_ID, company_id=company_b_id
    )


def test_tag_add_multi_already_linked_ok_skip(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.141: multi-owner already-linked row is status ok skip, not error."""
    tag = _make_tag(name="vip")
    company_a = _make_company(id=_TAG_COMPANY_ID, domain="a.com")
    company_b_id = "01234567-0000-7000-0000-0000000000b2"
    company_b = _make_company(id=company_b_id, domain="b.com")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_tag_by_name", return_value=tag),
        patch(
            "mailpilot.database.get_company_by_domain",
            side_effect=lambda _c, domain: {
                "a.com": company_a,
                "b.com": company_b,
            }.get(domain),
        ),
        patch(
            "mailpilot.database.assign_tag_to_company",
            side_effect=[None, _make_tag_assignment(contact_id=None, company_id=company_b_id)],
        ),
    ):
        result = runner.invoke(
            main,
            [
                "tag",
                "add",
                "--tag",
                "vip",
                "--company-domain",
                "a.com",
                "--company-domain",
                "b.com",
            ],
        )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["results"] == [
        {"ref": "a.com", "status": "ok"},
        {"ref": "b.com", "status": "ok"},
    ]


def test_tag_add_multi_partial_not_found_exit_1(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.141/§V.139: multi partial not_found still emits full results, exit 1."""
    tag = _make_tag(name="vip")
    company_a = _make_company(id=_TAG_COMPANY_ID, domain="a.com")
    assignment = _make_tag_assignment(contact_id=None, company_id=_TAG_COMPANY_ID)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_tag_by_name", return_value=tag),
        patch(
            "mailpilot.database.get_company_by_domain",
            side_effect=lambda _c, domain: {
                "a.com": company_a,
            }.get(domain),
        ),
        patch("mailpilot.database.assign_tag_to_company", return_value=assignment),
    ):
        result = runner.invoke(
            main,
            [
                "tag",
                "add",
                "--tag",
                "vip",
                "--company-domain",
                "a.com",
                "--company-domain",
                "ghost.com",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["record_count"] == 2
    assert data["results"][0] == {"ref": "a.com", "status": "ok"}
    assert data["results"][1]["ref"] == "ghost.com"
    assert data["results"][1]["status"] == "error"
    assert data["results"][1]["error"] == "not_found"


# -- tag set -------------------------------------------------------------------


def test_tag_set_company_returns_entity_with_tags(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.141: tag set company replaces set; company envelope carries final tags[]."""
    from mailpilot.models import CompanyView

    company = _make_company(id=_TAG_COMPANY_ID, domain="a.com")
    tag_a = _make_tag(id=_TAG_ID, name="acumatica-var")
    tag_b = _make_tag(
        id="01234567-0000-7000-0000-0000000000t2", name="dynamics-365-var"
    )
    view = CompanyView(
        **company.model_dump(),
        tags=["acumatica-var", "dynamics-365-var"],
        notes=[],
        notes_total=0,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company_by_domain", return_value=company),
        patch(
            "mailpilot.database.get_tag_by_name",
            side_effect=lambda _c, name: {
                "acumatica-var": tag_a,
                "dynamics-365-var": tag_b,
            }.get(name),
        ),
        patch(
            "mailpilot.database.set_company_tags",
            return_value=["acumatica-var", "dynamics-365-var"],
        ) as mock_set,
        patch("mailpilot.database.load_company_view", return_value=view),
    ):
        result = runner.invoke(
            main,
            [
                "tag",
                "set",
                "--company-domain",
                "a.com",
                "--tags",
                "acumatica-var,dynamics-365-var",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_set.assert_called_once_with(
        mock_connection,
        company_id=_TAG_COMPANY_ID,
        tag_ids=[_TAG_ID, tag_b.id],
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["record_count"] == 1
    assert data["company"]["domain"] == "a.com"
    assert data["company"]["tags"] == ["acumatica-var", "dynamics-365-var"]


def test_tag_set_company_empty_clears(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.141: empty --tags clears all company assignments."""
    from mailpilot.models import CompanyView

    company = _make_company(id=_TAG_COMPANY_ID, domain="a.com")
    view = CompanyView(
        **company.model_dump(), tags=[], notes=[], notes_total=0
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company_by_domain", return_value=company),
        patch(
            "mailpilot.database.set_company_tags", return_value=[]
        ) as mock_set,
        patch("mailpilot.database.load_company_view", return_value=view),
    ):
        result = runner.invoke(
            main,
            ["tag", "set", "--company-domain", "a.com", "--tags", ""],
        )

    assert result.exit_code == 0, result.output
    mock_set.assert_called_once_with(
        mock_connection, company_id=_TAG_COMPANY_ID, tag_ids=[]
    )
    data = json.loads(result.output)
    assert data["company"]["tags"] == []


def test_tag_set_undefined_tag_not_found_zero_writes(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.141/§V.116: undefined name in set → not_found, set_company_tags not called."""
    company = _make_company(id=_TAG_COMPANY_ID, domain="a.com")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company_by_domain", return_value=company),
        patch("mailpilot.database.get_tag_by_name", return_value=None),
        patch("mailpilot.database.set_company_tags") as mock_set,
    ):
        result = runner.invoke(
            main,
            [
                "tag",
                "set",
                "--company-domain",
                "a.com",
                "--tags",
                "ghost-var",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    mock_set.assert_not_called()


def test_tag_set_no_owner(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(main, ["tag", "set", "--tags", "vip"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"


# -- tag remove ----------------------------------------------------------------


def test_tag_remove_on_contact(runner: CliRunner, mock_connection: MagicMock) -> None:
    tag = _make_tag(name="prospect")
    contact = _make_contact(id=_TAG_CONTACT_ID)
    assignment = _make_tag_assignment()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_tag_by_name", return_value=tag),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch(
            "mailpilot.database.remove_tag_from_contact", return_value=assignment
        ) as mock_remove,
        patch("mailpilot.operator_log.operator_event") as mock_event,
    ):
        result = runner.invoke(
            main,
            ["tag", "remove", "--tag", "prospect", "--contact-email", _TAG_CONTACT_ID],
        )

    assert result.exit_code == 0
    mock_remove.assert_called_once_with(
        mock_connection, tag_id=_TAG_ID, contact_id=_TAG_CONTACT_ID
    )
    data = json.loads(result.output)
    assert data["tag_assignment"]["tag_id"] == _TAG_ID
    remove_events = [
        call for call in mock_event.call_args_list if call.args[:1] == ("tag.remove",)
    ]
    assert len(remove_events) == 1


def test_tag_remove_not_linked_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    tag = _make_tag(name="prospect")
    contact = _make_contact(id=_TAG_CONTACT_ID)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_tag_by_name", return_value=tag),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.remove_tag_from_contact", return_value=None),
    ):
        result = runner.invoke(
            main,
            ["tag", "remove", "--tag", "prospect", "--contact-email", _TAG_CONTACT_ID],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_tag_remove_undefined_tag_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_tag_by_name", return_value=None),
    ):
        result = runner.invoke(
            main,
            ["tag", "remove", "--tag", "ghost", "--contact-email", _TAG_CONTACT_ID],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


# -- tag disable ---------------------------------------------------------------


def test_tag_disable(runner: CliRunner, mock_connection: MagicMock) -> None:
    """§V.116: vocabulary-level disable; §V.54 changed=['disabled_reason']."""
    active = _make_tag(name="prospect")
    disabled = _make_tag(name="prospect", disabled_reason="stale")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_tag_by_name", return_value=active),
        patch("mailpilot.database.disable_tag", return_value=disabled) as mock_disable,
        patch("mailpilot.operator_log.operator_event") as mock_event,
    ):
        result = runner.invoke(
            main, ["tag", "disable", "prospect", "--reason", "stale"]
        )

    assert result.exit_code == 0
    mock_disable.assert_called_once_with(
        mock_connection, name="prospect", reason="stale"
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["tag"]["name"] == "prospect"
    assert data["tag"]["disabled_reason"] == "stale"
    disable_events = [
        call for call in mock_event.call_args_list if call.args[:1] == ("tag.disable",)
    ]
    assert len(disable_events) == 1
    assert disable_events[0].kwargs == {
        "entity_id": "prospect",
        "changed": ["disabled_reason"],
    }


def test_tag_disable_undefined_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_tag_by_name", return_value=None),
    ):
        result = runner.invoke(main, ["tag", "disable", "ghost", "--reason", "stale"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_tag_disable_already_disabled(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.10: double-disable is rejected before any write."""
    already = _make_tag(name="prospect", disabled_reason="stale")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_tag_by_name", return_value=already),
        patch("mailpilot.database.disable_tag") as mock_disable,
    ):
        result = runner.invoke(
            main, ["tag", "disable", "prospect", "--reason", "again"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "already disabled" in data["message"]
    mock_disable.assert_not_called()


def test_tag_disable_empty_reason(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(main, ["tag", "disable", "prospect", "--reason", "   "])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "reason" in data["message"]


def test_tag_enable(runner: CliRunner, mock_connection: MagicMock) -> None:
    """§V.10: tag enable clears disabled_reason; §V.54 changed=['disabled_reason']."""
    disabled = _make_tag(name="prospect", disabled_reason="stale")
    active = _make_tag(name="prospect", disabled_reason=None)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_tag_by_name", return_value=disabled),
        patch("mailpilot.database.enable_tag", return_value=active) as mock_enable,
        patch("mailpilot.operator_log.operator_event") as mock_event,
    ):
        result = runner.invoke(main, ["tag", "enable", "prospect"])

    assert result.exit_code == 0, result.output
    mock_enable.assert_called_once_with(mock_connection, name="prospect")
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["tag"]["name"] == "prospect"
    assert data["tag"]["disabled_reason"] is None
    enable_events = [
        call for call in mock_event.call_args_list if call.args[:1] == ("tag.enable",)
    ]
    assert len(enable_events) == 1
    assert enable_events[0].kwargs == {
        "entity_id": "prospect",
        "changed": ["disabled_reason"],
    }


def test_tag_enable_undefined_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_tag_by_name", return_value=None),
    ):
        result = runner.invoke(main, ["tag", "enable", "ghost"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_tag_enable_not_disabled(runner: CliRunner, mock_connection: MagicMock) -> None:
    """§V.10: enabling an active tag is rejected before any write."""
    active = _make_tag(name="prospect", disabled_reason=None)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_tag_by_name", return_value=active),
        patch("mailpilot.database.enable_tag") as mock_enable,
    ):
        result = runner.invoke(main, ["tag", "enable", "prospect"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "not disabled" in data["message"]
    mock_enable.assert_not_called()


# -- tag list ------------------------------------------------------------------


def test_tag_list_vocabulary_owner_free(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.116: tag list needs no owner -- it lists the vocabulary."""
    tags = [
        _make_tag_summary(id="id-1", name="cold", usage_count=2),
        _make_tag_summary(id="id-2", name="prospect", usage_count=0),
    ]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_tags", return_value=tags) as mock_list,
    ):
        result = runner.invoke(main, ["tag", "list"])

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        contact_id=None,
        company_id=None,
        limit=100,
        since=None,
        until=None,
        include_disabled=False,
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert len(data["tags"]) == 2
    assert data["tags"][0]["usage_count"] == 2


def test_tag_list_by_contact(runner: CliRunner, mock_connection: MagicMock) -> None:
    contact = _make_contact(id=_TAG_CONTACT_ID)
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
                "--contact-email",
                _TAG_CONTACT_ID,
                "--include-disabled",
            ],
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        contact_id=_TAG_CONTACT_ID,
        company_id=None,
        limit=100,
        since=None,
        until=None,
        include_disabled=True,
    )


def test_tag_list_both_owners_errors(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(
            main,
            [
                "tag",
                "list",
                "--contact-email",
                _TAG_CONTACT_ID,
                "--company-domain",
                _TAG_COMPANY_ID,
            ],
        )

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
        result = runner.invoke(
            main,
            ["tag", "list", "--contact-email", "01234567-0000-7000-0000-0000000000c1"],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "contact" in data["message"]


# -- tag search ----------------------------------------------------------------


def test_tag_search(runner: CliRunner, mock_connection: MagicMock) -> None:
    tags = [_make_tag_summary(name="prospect", usage_count=1)]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.search_tags", return_value=tags) as mock_search,
    ):
        result = runner.invoke(main, ["tag", "search", "pro", "--limit", "5"])

    assert result.exit_code == 0
    mock_search.assert_called_once_with(
        mock_connection,
        name="pro",
        limit=5,
        include_disabled=False,
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert len(data["tags"]) == 1
    assert data["tags"][0]["usage_count"] == 1


def test_tag_search_include_disabled_flag(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.search_tags", return_value=[]) as mock_search,
    ):
        result = runner.invoke(
            main, ["tag", "search", "prospect", "--include-disabled"]
        )

    assert result.exit_code == 0
    mock_search.assert_called_once_with(
        mock_connection,
        name="prospect",
        limit=100,
        include_disabled=True,
    )


# -- company/contact list membership filters (§V.116) --------------------------


def test_company_list_filter_by_tag(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    tag = _make_tag(name="vip")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_tag_by_name", return_value=tag),
        patch("mailpilot.database.list_companies", return_value=[]) as mock_list,
    ):
        result = runner.invoke(main, ["company", "list", "--tag", "vip"])

    assert result.exit_code == 0
    _, kwargs = mock_list.call_args
    assert kwargs["tag"] == _TAG_ID
    assert kwargs["exclude_tags"] == []


def test_company_list_no_tag_filter(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    tag = _make_tag(name="no-contacts-found")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_tag_by_name", return_value=tag),
        patch("mailpilot.database.list_companies", return_value=[]) as mock_list,
    ):
        result = runner.invoke(
            main, ["company", "list", "--no-tag", "no-contacts-found"]
        )

    assert result.exit_code == 0
    _, kwargs = mock_list.call_args
    assert kwargs["tag"] is None
    assert kwargs["exclude_tags"] == [_TAG_ID]


def test_company_list_repeatable_no_tag(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.116: --no-tag is repeatable -> one resolved id per occurrence."""
    no_dm = _make_tag(name="no-contacts-found", id=_TAG_ID)
    exhausted = _make_tag(
        name="contacts-exhausted", id="01234567-0000-7000-0000-0000000000ee"
    )
    by_name = {"no-contacts-found": no_dm, "contacts-exhausted": exhausted}
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.get_tag_by_name",
            side_effect=lambda _conn, name: by_name[name],
        ),
        patch("mailpilot.database.list_companies", return_value=[]) as mock_list,
    ):
        result = runner.invoke(
            main,
            [
                "company",
                "list",
                "--no-tag",
                "no-contacts-found",
                "--no-tag",
                "contacts-exhausted",
            ],
        )

    assert result.exit_code == 0
    _, kwargs = mock_list.call_args
    assert kwargs["exclude_tags"] == [no_dm.id, exhausted.id]


def test_company_list_tag_undefined_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_tag_by_name", return_value=None),
    ):
        result = runner.invoke(main, ["company", "list", "--tag", "ghost"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_contact_list_filter_by_tag(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    tag = _make_tag(name="vip")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_tag_by_name", return_value=tag),
        patch("mailpilot.database.list_contacts", return_value=[]) as mock_list,
    ):
        result = runner.invoke(main, ["contact", "list", "--tag", "vip"])

    assert result.exit_code == 0
    _, kwargs = mock_list.call_args
    assert kwargs["tag"] == _TAG_ID
    assert kwargs["exclude_tags"] == []


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
            [
                "note",
                "add",
                "--contact-email",
                "01234567-0000-7000-0000-000000000003",
                "--body",
                "Test note body",
            ],
        )

    assert result.exit_code == 0
    mock_create.assert_called_once_with(
        mock_connection,
        contact_id="01234567-0000-7000-0000-000000000003",
        body="Test note body",
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["note"]["body"] == "Test note body"


def test_note_add_on_company(runner: CliRunner, mock_connection: MagicMock) -> None:
    note = _make_note(
        contact_id=None, company_id="01234567-0000-7000-0000-000000000002"
    )
    company = _make_company(id="01234567-0000-7000-0000-000000000002")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.add_company_note", return_value=note) as mock_add,
        patch("mailpilot.database.get_company", return_value=company),
    ):
        result = runner.invoke(
            main,
            [
                "note",
                "add",
                "--company-domain",
                "01234567-0000-7000-0000-000000000002",
                "--body",
                "Company note",
            ],
        )

    assert result.exit_code == 0
    mock_add.assert_called_once_with(
        mock_connection,
        company_id="01234567-0000-7000-0000-000000000002",
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
            [
                "note",
                "add",
                "--contact-email",
                "01234567-0000-7000-0000-0000000000c1",
                "--body",
                "Some note",
            ],
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
            [
                "note",
                "add",
                "--company-domain",
                "01234567-0000-7000-0000-0000000000c2",
                "--body",
                "Some note",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "company" in data["message"]


def test_note_add_no_entity(runner: CliRunner, mock_connection: MagicMock) -> None:
    """note add without --contact-email or --company-domain should error."""
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
            main,
            [
                "note",
                "add",
                "--contact-email",
                "01234567-0000-7000-0000-000000000003",
                "--body",
                "",
            ],
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
            main,
            [
                "note",
                "add",
                "--contact-email",
                "01234567-0000-7000-0000-000000000003",
                "--body",
                "   ",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "empty" in data["message"]


def test_note_add_missing_body(runner: CliRunner, mock_connection: MagicMock) -> None:
    """note add without --body should error."""
    result = runner.invoke(
        main, ["note", "add", "--contact-email", "01234567-0000-7000-0000-000000000003"]
    )
    assert result.exit_code != 0


# -- note remove ---------------------------------------------------------------


def test_note_remove_deletes_note(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    note = _make_note()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_note", return_value=note),
        patch("mailpilot.database.delete_note", return_value=True) as mock_delete,
    ):
        result = runner.invoke(main, ["note", "remove", note.id])

    assert result.exit_code == 0
    mock_delete.assert_called_once_with(mock_connection, note.id)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["note"]["id"] == note.id
    assert data["note"]["body"] == note.body


def test_note_remove_not_found(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_note", return_value=None),
        patch("mailpilot.database.delete_note") as mock_delete,
    ):
        result = runner.invoke(
            main, ["note", "remove", "01234567-0000-7000-0000-0000000000ff"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    mock_delete.assert_not_called()


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
        result = runner.invoke(
            main,
            ["note", "list", "--contact-email", "01234567-0000-7000-0000-000000000003"],
        )

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
            main,
            [
                "note",
                "list",
                "--contact-email",
                "01234567-0000-7000-0000-000000000003",
                "--limit",
                "5",
            ],
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        contact_id="01234567-0000-7000-0000-000000000003",
        limit=5,
        since=None,
        until=None,
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
                "--contact-email",
                "01234567-0000-7000-0000-000000000003",
                "--since",
                "2024-01-01T00:00:00Z",
            ],
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        contact_id="01234567-0000-7000-0000-000000000003",
        limit=100,
        since="2024-01-01T00:00:00Z",
        until=None,
    )


def test_note_list_no_entity(runner: CliRunner, mock_connection: MagicMock) -> None:
    """note list without --contact-email or --company-domain should error."""
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
        result = runner.invoke(
            main, ["note", "view", "01234567-0000-7000-0000-0000000000ff"]
        )

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
        result = runner.invoke(
            main,
            ["note", "list", "--contact-email", "01234567-0000-7000-0000-0000000000c1"],
        )

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
        result = runner.invoke(
            main,
            [
                "note",
                "list",
                "--company-domain",
                "01234567-0000-7000-0000-0000000000c2",
            ],
        )

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
        patch(
            "mailpilot.database.get_contact",
            return_value=_make_contact(id=_CONTACT_ID),
        ),
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
                "--contact-email",
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
                "--contact-email",
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
        patch(
            "mailpilot.database.get_contact",
            return_value=_make_contact(id=_CONTACT_ID),
        ),
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
                "--contact-email",
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
                "--contact-email",
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
                "--contact-email",
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
                "--contact-email",
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
                "--contact-email",
                "01234567-0000-7000-0000-0000000000c1",
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
                "--contact-email",
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
                "--contact-email",
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
                "--contact-email",
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


def test_enrollment_enable(runner: CliRunner, mock_connection: MagicMock) -> None:
    """§V.15: enrollment enable flips status disabled->active + clears reason.

    §V.54: `changed` = ['status', 'disabled_reason'].
    """
    before = _make_enrollment(status="disabled", disabled_reason="left company")
    after = _make_enrollment(status="active", disabled_reason=None)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_enrollment_by_id", return_value=before),
        patch(
            "mailpilot.database.enable_enrollment", return_value=after
        ) as mock_enable,
        patch("mailpilot.operator_log.operator_event") as mock_event,
    ):
        result = runner.invoke(main, ["enrollment", "enable", _ENROLLMENT_ID])

    assert result.exit_code == 0, result.output
    mock_enable.assert_called_once_with(mock_connection, _ENROLLMENT_ID)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["enrollment"]["status"] == "active"
    assert data["enrollment"]["disabled_reason"] is None
    enable_events = [
        call
        for call in mock_event.call_args_list
        if call.args[:1] == ("enrollment.enable",)
    ]
    assert len(enable_events) == 1
    assert enable_events[0].kwargs == {
        "entity_id": _ENROLLMENT_ID,
        "changed": ["status", "disabled_reason"],
    }


def test_enrollment_enable_not_disabled(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.15: enabling a live (active) enrollment is rejected before any write."""
    before = _make_enrollment(status="active", disabled_reason=None)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_enrollment_by_id", return_value=before),
        patch("mailpilot.database.enable_enrollment") as mock_enable,
    ):
        result = runner.invoke(main, ["enrollment", "enable", _ENROLLMENT_ID])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "not disabled" in data["message"]
    mock_enable.assert_not_called()


def test_enrollment_enable_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_enrollment_by_id", return_value=None),
    ):
        result = runner.invoke(main, ["enrollment", "enable", _ENROLLMENT_ID])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


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
        until=None,
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
                "disabled",
            ],
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        workflow_id=_WORKFLOW_ID,
        contact_id=None,
        status="disabled",
        limit=100,
        since=None,
        until=None,
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
        until=None,
    )


def test_enrollment_list_filters_by_contact(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.get_contact",
            return_value=_make_contact(id=_CONTACT_ID),
        ),
        patch(
            "mailpilot.database.list_enrollments_detailed", return_value=[]
        ) as mock_list,
    ):
        result = runner.invoke(
            main,
            ["enrollment", "list", "--contact-email", _CONTACT_ID],
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        workflow_id=None,
        contact_id=_CONTACT_ID,
        status=None,
        limit=100,
        since=None,
        until=None,
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


# -- enrollment update removed (§V.15) -----------------------------------------


def test_enrollment_update_command_removed(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.15: `enrollment update` is gone; disable/enable are the sole surface."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(
            main,
            ["enrollment", "update", _ENROLLMENT_ID, "--status", "disabled"],
        )

    # Click rejects an unknown subcommand at parse time (usage error, exit 2).
    assert result.exit_code == 2


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
        trigger=None,
        limit=100,
        since=None,
        until=None,
    )
    data = json.loads(result.output)
    assert len(data["tasks"]) == 1


def test_task_list_with_filters(runner: CliRunner, mock_connection: MagicMock) -> None:
    workflow = _make_workflow()
    contact = _make_contact(id=_CONTACT_ID)
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
                "--contact-email",
                _CONTACT_ID,
                "--status",
                "pending",
                "--trigger",
                "enrollment_schedule",
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
        trigger="enrollment_schedule",
        limit=10,
        since=None,
        until=None,
    )


def test_task_list_workflow_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=None),
    ):
        result = runner.invoke(
            main,
            ["task", "list", "--workflow-id", "01234567-0000-7000-0000-0000000000fe"],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


# -- task stats ----------------------------------------------------------------


def _make_task_stats() -> TaskStats:
    return TaskStats(
        total=4,
        pending=3,
        completed=0,
        failed=0,
        cancelled=1,
        distinct_scheduled_days=3,
        first_scheduled_at=datetime(2026, 4, 22, 9, tzinfo=UTC),
        last_scheduled_at=datetime(2026, 4, 26, 12, tzinfo=UTC),
    )


def test_task_stats_envelope(runner: CliRunner, mock_connection: MagicMock) -> None:
    """§V.133/§V.4: `task stats` ships the aggregate under `task_stats`."""
    stats = _make_task_stats()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_task_stats", return_value=stats) as mock_stats,
    ):
        result = runner.invoke(main, ["task", "stats"])

    assert result.exit_code == 0, result.output
    mock_stats.assert_called_once_with(
        mock_connection,
        workflow_id=None,
        trigger=None,
        bucket_tz="UTC",
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert "task" not in data
    funnel = data["task_stats"]
    assert funnel["total"] == 4
    assert funnel["pending"] == 3
    assert funnel["cancelled"] == 1
    assert funnel["distinct_scheduled_days"] == 3
    assert funnel["first_scheduled_at"] == "2026-04-22T09:00:00Z"
    assert funnel["last_scheduled_at"] == "2026-04-26T12:00:00Z"


def test_task_stats_threads_filters(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.133/§V.107/§V.26: --workflow-id, --trigger, --bucket-tz reach the fn."""
    workflow = _make_workflow()
    stats = _make_task_stats()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.get_task_stats", return_value=stats) as mock_stats,
    ):
        result = runner.invoke(
            main,
            [
                "task",
                "stats",
                "--workflow-id",
                _WORKFLOW_ID,
                "--trigger",
                "enrollment_schedule",
                "--bucket-tz",
                "America/New_York",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_stats.assert_called_once_with(
        mock_connection,
        workflow_id=_WORKFLOW_ID,
        trigger="enrollment_schedule",
        bucket_tz="America/New_York",
    )


def test_task_stats_unknown_workflow_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.107: an unknown --workflow-id exits not_found."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=None),
    ):
        result = runner.invoke(
            main,
            ["task", "stats", "--workflow-id", "01234567-0000-7000-0000-0000000000fe"],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_task_stats_invalid_bucket_tz_validation_error(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.4: an unknown IANA timezone exits validation_error (clean envelope)."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_task_stats") as mock_stats,
    ):
        result = runner.invoke(
            main, ["task", "stats", "--bucket-tz", "Mars/Olympus_Mons"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    mock_stats.assert_not_called()


def test_task_stats_rejects_unknown_trigger(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.26: --trigger is a closed Choice; an off-taxonomy value is rejected."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(main, ["task", "stats", "--trigger", "bogus"])

    assert result.exit_code != 0


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
        result = runner.invoke(
            main, ["task", "view", "01234567-0000-7000-0000-0000000000fe"]
        )

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
        result = runner.invoke(main, ["task", "retry", failed.id])

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
        result = runner.invoke(
            main, ["task", "retry", "01234567-0000-7000-0000-0000000000fe"]
        )

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
        result = runner.invoke(main, ["task", "retry", pending.id])

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
        result = runner.invoke(main, ["task", "retry", completed.id])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "invalid_state"


def test_task_retry_rejects_task_id_option(runner: CliRunner) -> None:
    """§V.107: the retry target is a positional `<task_id>`, never a
    `--task-id` option -- the flag no longer exists."""
    with patch("mailpilot.settings.get_settings", return_value=make_test_settings()):
        result = runner.invoke(main, ["task", "retry", "--task-id", "t-1"])

    assert result.exit_code == 2
    assert "No such option" in result.output


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
    assert set(data.keys()) == {"account", "record_count", "ok"}
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
    assert set(data.keys()) == {"account", "record_count", "ok"}
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
    assert set(data.keys()) == {"account", "record_count", "ok"}
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
    assert set(data.keys()) == {"accounts", "record_count", "ok"}
    assert isinstance(data["accounts"], list)


# -- template list / view ------------------------------------------------------


def test_template_list(runner: CliRunner) -> None:
    """`template list` returns all 3 templates with summary fields."""
    result = runner.invoke(main, ["template", "list"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ok"] is True
    assert set(data.keys()) == {"templates", "record_count", "ok"}
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
    assert set(data.keys()) == {"template", "record_count", "ok"}
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


# -- §V.107 account-ref resolver (--account-email, polymorphic) ---------------


def test_email_send_resolves_account_email(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.107: `email send --account-email` resolves the owning account id."""
    account = _make_account()
    sent = _make_email(direction="outbound", status="sent", sent_at=_NOW)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.get_account_by_email", return_value=account
        ) as mock_by_email,
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.gmail.GmailClient") as mock_client_cls,
        patch("mailpilot.email_ops.send_email", return_value=sent) as mock_send,
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "send",
                "--account-email",
                "TEST@example.com",
                "--to",
                "recipient@example.com",
                "--subject",
                "Hi",
                "--body",
                "Hello",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_by_email.assert_called_once_with(mock_connection, "TEST@example.com")
    mock_client_cls.assert_called_once_with(account.email)
    assert mock_send.call_args.kwargs["account"] == account


def test_email_send_requires_an_account_ref(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.107: missing --account-email -> validation_error."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "send",
                "--to",
                "recipient@example.com",
                "--subject",
                "Hi",
                "--body",
                "Hello",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "validation_error"


def test_email_send_unknown_account_email_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.107/§V.94: unknown --account-email -> not_found, no send."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account_by_email", return_value=None),
        patch("mailpilot.email_ops.send_email") as mock_send,
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "send",
                "--account-email",
                "missing@example.com",
                "--to",
                "recipient@example.com",
                "--subject",
                "Hi",
                "--body",
                "Hello",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "not_found"
    mock_send.assert_not_called()


def test_email_reply_resolves_account_email(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.107: `email reply --account-email` resolves the owning account id."""
    account = _make_account()
    sent = _make_email(direction="outbound", status="sent", sent_at=_NOW)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.get_account_by_email", return_value=account
        ) as mock_by_email,
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.gmail.GmailClient"),
        patch("mailpilot.email_ops.reply_email", return_value=sent) as mock_reply,
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "reply",
                "--account-email",
                "test@example.com",
                "--email-id",
                "original-1",
                "--body",
                "hi",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_by_email.assert_called_once_with(mock_connection, "test@example.com")
    assert mock_reply.call_args.kwargs["account"] == account


def test_workflow_create_resolves_account_email(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.107: `workflow create --account-email` resolves the owning account id."""
    workflow = _make_workflow()
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.get_account_by_email", return_value=account
        ) as mock_by_email,
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
                "--account-email",
                "test@example.com",
                "--draft",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_by_email.assert_called_once_with(mock_connection, "test@example.com")
    assert mock_create.call_args.kwargs["account_id"] == account.id


def test_workflow_create_requires_an_account_ref(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.107: workflow create with neither account ref -> validation_error."""
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.create_workflow") as mock_create,
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
                "--draft",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    mock_create.assert_not_called()


def test_workflow_export_resolves_account_email(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    """§V.107: `workflow export --account-email` resolves the owning account id."""
    account = _make_account()
    workflow = _make_workflow(name="Demo outreach")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.get_account_by_email", return_value=account
        ) as mock_by_email,
        patch("mailpilot.database.get_account", return_value=account),
        patch(
            "mailpilot.database.list_workflows_full", return_value=[workflow]
        ) as mock_list,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "export",
                "--account-email",
                "test@example.com",
                "--out-dir",
                str(tmp_path / "catalog"),
            ],
        )

    assert result.exit_code == 0, result.output
    mock_by_email.assert_called_once_with(mock_connection, "test@example.com")
    mock_list.assert_called_once_with(mock_connection, account.id)


def test_workflow_import_resolves_account_email(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    """§V.107: `workflow import --account-email` resolves the owning account id."""
    account = _make_account()
    created = _make_workflow(theme="green")
    file = tmp_path / "demo-outreach.toml"
    _write_workflow_toml(file, _import_payload())
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.get_account_by_email", return_value=account
        ) as mock_by_email,
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.list_workflows_full", return_value=[]),
        patch(
            "mailpilot.database.create_workflow", return_value=created
        ) as mock_create,
        patch("mailpilot.database.update_workflow", return_value=created),
        patch("mailpilot.database.activate_workflow", return_value=created),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "import",
                "--account-email",
                "test@example.com",
                "--file",
                str(file),
            ],
        )

    assert result.exit_code == 0, result.output
    mock_by_email.assert_called_once_with(mock_connection, "test@example.com")
    assert mock_create.call_args.kwargs["account_id"] == account.id


# -- write-path schema gate wiring (§V.109) -----------------------------------


def _gate_db_mock() -> MagicMock:
    """Connection mock whose `account` probe reports an initialized DB."""
    probe_cursor = MagicMock()
    probe_cursor.fetchone.return_value = {"oid": "account"}
    connection = MagicMock()

    def execute_side_effect(query: Any, *_params: Any) -> MagicMock:
        if "to_regclass" in str(query):
            return probe_cursor
        return MagicMock()

    connection.execute.side_effect = execute_side_effect
    return connection


def test_run_dead_stops_when_schema_not_current(runner: CliRunner) -> None:
    """`run` refuses to start the sync loop on a non-current schema (§V.109)."""
    drift = SchemaStatus(
        verdict="drift",
        recorded_hash="a" * 64,
        current_hash="b" * 64,
        applied=1,
        pending=0,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.psycopg.connect", return_value=_gate_db_mock()),
        patch("mailpilot.database.determine_schema_verdict", return_value=drift),
        patch("mailpilot.sync.start_sync_loop") as mock_loop,
    ):
        result = runner.invoke(main, ["run"])

    assert result.exit_code == 1
    mock_loop.assert_not_called()


def test_mutation_dead_stops_when_schema_pending(runner: CliRunner) -> None:
    """A mutation (`company create`) refuses to write on a pending schema."""
    pending = SchemaStatus(
        verdict="pending",
        recorded_hash="a" * 64,
        current_hash="b" * 64,
        applied=1,
        pending=1,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.psycopg.connect", return_value=_gate_db_mock()),
        patch("mailpilot.database.determine_schema_verdict", return_value=pending),
        patch("mailpilot.database.create_company") as mock_create,
    ):
        result = runner.invoke(
            main, ["company", "create", "--name", "Acme", "--domain", "acme.com"]
        )

    assert result.exit_code == 1
    mock_create.assert_not_called()


# -- db noun: init / migrate / check (§V.110, §V.108, §V.109) ------------------


def _init_report(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "provisioned": False,
        "verdict": "current",
        "recorded_hash": "a" * 64,
        "current_hash": "a" * 64,
        "applied": 1,
        "pending": 0,
    }
    return {**base, **overrides}


def test_db_init_provisions_empty(runner: CliRunner) -> None:
    """Empty DB → provision; ok envelope under the `db` singular key (§V.110)."""
    report = _init_report(provisioned=True)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.provision_database", return_value=report),
    ):
        result = runner.invoke(main, ["db", "init"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["db"]["provisioned"] is True
    assert data["db"]["message"] == "database provisioned"


def test_db_init_noop_when_already_current(runner: CliRunner) -> None:
    """Account present + current → idempotent no-op-with-message, exit 0 (§V.110)."""
    report = _init_report(provisioned=False, verdict="current")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.provision_database", return_value=report),
    ):
        result = runner.invoke(main, ["db", "init"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["db"]["message"] == "database already initialized"


def test_db_init_refuses_when_account_exists_not_current(runner: CliRunner) -> None:
    """Account present + pending → refuses (no --force), error envelope, exit 1."""
    report = _init_report(provisioned=False, verdict="pending", pending=2)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.provision_database", return_value=report),
    ):
        result = runner.invoke(main, ["db", "init"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "already_initialized"


def test_db_migrate_applies_pending(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """`db migrate` reports applied migrations under the `db` key (§V.108)."""
    applied = [{"version": 2, "name": "add_widget"}]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.migrate_database", return_value=applied),
    ):
        result = runner.invoke(main, ["db", "migrate"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["db"]["count"] == 1
    assert data["db"]["applied"] == applied


def test_db_migrate_noop_when_nothing_pending(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.migrate_database", return_value=[]),
    ):
        result = runner.invoke(main, ["db", "migrate"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["db"]["count"] == 0
    assert data["db"]["applied"] == []


def test_db_check_current_is_ok(runner: CliRunner, mock_connection: MagicMock) -> None:
    """verdict=current → ok envelope + exit 0, report keys per §I.cli."""
    status = SchemaStatus(
        verdict="current",
        recorded_hash="a" * 64,
        current_hash="a" * 64,
        applied=1,
        pending=0,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.determine_schema_verdict", return_value=status),
    ):
        result = runner.invoke(main, ["db", "check"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ok"] is True
    assert set(data["db"].keys()) == {
        "recorded_hash",
        "current_hash",
        "applied",
        "pending",
        "verdict",
    }
    assert data["db"]["verdict"] == "current"


def test_db_check_pending_exits_one_with_report(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """verdict=pending → schema_migration_pending envelope, report inlined, exit 1."""
    status = SchemaStatus(
        verdict="pending",
        recorded_hash="a" * 64,
        current_hash="b" * 64,
        applied=1,
        pending=3,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.determine_schema_verdict", return_value=status),
    ):
        result = runner.invoke(main, ["db", "check"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "schema_migration_pending"
    assert data["report"]["verdict"] == "pending"
    assert data["report"]["pending"] == 3


def test_db_check_drift_exits_one_with_report(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """verdict=drift → schema_drift envelope, report inlined, exit 1 (§V.109)."""
    status = SchemaStatus(
        verdict="drift",
        recorded_hash="dead" * 16,
        current_hash="beef" * 16,
        applied=1,
        pending=0,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.determine_schema_verdict", return_value=status),
    ):
        result = runner.invoke(main, ["db", "check"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "schema_drift"
    assert data["report"]["verdict"] == "drift"


# -- db noun: export / import snapshot bundle (§V.121, §V.119, §B.104) ----------


def test_db_export_writes_bundle_and_status(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: Any
) -> None:
    """`db export` writes the bundle to disk + a `{"db":{...}}` status (§V.121)."""
    bundle = {
        "schema_version": 1,
        "exported_at": "2026-06-22T00:00:00+00:00",
        "tags": [{"name": "lead", "disabled_reason": None}],
        "companies": [
            {
                "name": "Acme",
                "domain": "acme.com",
                "profile": None,
                "disabled_reason": None,
                "tags": ["lead"],
            }
        ],
        "contacts": [
            {
                "email": "a@acme.com",
                "first_name": "Ann",
                "last_name": None,
                "title": None,
                "email_confidence": None,
                "disabled_reason": None,
                "company_domain": "acme.com",
                "tags": [],
            }
        ],
    }
    export_file = str(tmp_path / "snap.json")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.export_snapshot", return_value=bundle),
    ):
        result = runner.invoke(main, ["db", "export", "--file", export_file])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["db"]["path"] == export_file
    assert data["db"]["companies"] == 1
    assert data["db"]["contacts"] == 1
    assert data["db"]["tags"] == 1
    on_disk = json.loads(pathlib.Path(export_file).read_text())
    assert on_disk == bundle


def test_db_export_requires_file(runner: CliRunner) -> None:
    """`db export` has no stdout-bundle path: --file is required (§V.121)."""
    with patch("mailpilot.settings.get_settings", return_value=make_test_settings()):
        result = runner.invoke(main, ["db", "export"])

    assert result.exit_code != 0
    assert "--file" in result.output


def test_db_import_restores_and_reports_status(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: Any
) -> None:
    """`db import` reports restored counts under the `db` key (§V.121)."""
    bundle = {
        "schema_version": 1,
        "tags": [{"name": "lead", "disabled_reason": None}],
        "companies": [
            {
                "name": "Acme",
                "domain": "acme.com",
                "profile": None,
                "disabled_reason": None,
                "tags": ["lead"],
            }
        ],
        "contacts": [],
    }
    import_file = tmp_path / "snap.json"
    import_file.write_text(json.dumps(bundle))
    restore_result = {"tags": 1, "companies": 1, "contacts": 0, "errors": []}
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.import_snapshot", return_value=restore_result
        ) as mock_import,
    ):
        result = runner.invoke(main, ["db", "import", "--file", str(import_file)])

    assert result.exit_code == 0, result.output
    mock_import.assert_called_once_with(mock_connection, bundle)
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["db"]["path"] == str(import_file)
    assert data["db"]["tags"] == 1
    assert data["db"]["companies"] == 1
    assert data["db"]["contacts"] == 0
    assert data["db"]["errors"] == []


def test_db_import_surfaces_per_row_errors(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: Any
) -> None:
    """Per-row errors ride the status envelope, batch still exits 0 (§V.121)."""
    bundle = {"schema_version": 1, "tags": [], "companies": [], "contacts": []}
    import_file = tmp_path / "snap.json"
    import_file.write_text(json.dumps(bundle))
    restore_result = {
        "tags": 0,
        "companies": 0,
        "contacts": 1,
        "errors": [
            {
                "entity": "contact",
                "key": "orphan@nowhere.com",
                "error": "foreign_key_violation",
                "message": "company domain 'gone.com' not found",
            }
        ],
    }
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.import_snapshot", return_value=restore_result),
    ):
        result = runner.invoke(main, ["db", "import", "--file", str(import_file)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ok"] is True
    assert len(data["db"]["errors"]) == 1
    assert data["db"]["errors"][0]["error"] == "foreign_key_violation"


def test_db_import_malformed_json(runner: CliRunner, tmp_path: Any) -> None:
    """A malformed bundle exits validation_error before any DB touch (§V.3)."""
    import_file = tmp_path / "snap.json"
    import_file.write_text("not json")
    with patch("mailpilot.settings.get_settings", return_value=make_test_settings()):
        result = runner.invoke(main, ["db", "import", "--file", str(import_file)])

    assert result.exit_code == 1
    err = json.loads(result.output)
    assert err["ok"] is False
    assert err["error"] == "validation_error"


def test_db_import_requests_write_path_schema_gate(
    runner: CliRunner, tmp_path: Any
) -> None:
    """`db import` is a mutation: it requests the §V.109 gate, and a dead-stop
    leaves the restore unreached (no partial write)."""
    bundle = {"schema_version": 1, "tags": [], "companies": [], "contacts": []}
    import_file = tmp_path / "snap.json"
    import_file.write_text(json.dumps(bundle))
    captured: dict[str, Any] = {}

    def fake_init(_url: str, *, require_current_schema: bool = False) -> NoReturn:
        captured["require_current_schema"] = require_current_schema
        raise SystemExit(1)

    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", side_effect=fake_init),
        patch("mailpilot.database.import_snapshot") as mock_import,
    ):
        result = runner.invoke(main, ["db", "import", "--file", str(import_file)])

    assert result.exit_code == 1
    assert captured["require_current_schema"] is True
    assert mock_import.call_count == 0


# -- §V.115 six-family filter taxonomy -----------------------------------------


def test_account_list_until_flows_to_db(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.115 family 6: --until wires the inclusive upper time bound."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_accounts", return_value=[]) as mock_list,
    ):
        result = runner.invoke(
            main, ["account", "list", "--until", "2024-12-31T23:59:59"]
        )

    assert result.exit_code == 0
    mock_list.assert_called_once_with(
        mock_connection,
        limit=100,
        since=None,
        until="2024-12-31T23:59:59",
        include_disabled=False,
    )


def test_task_list_until_flows_to_db(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.115 family 6: task list --until covers the scheduled_at window."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.list_tasks", return_value=[]) as mock_list,
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(main, ["task", "list", "--until", "2025-01-01T00:00:00"])

    assert result.exit_code == 0
    _, kwargs = mock_list.call_args
    assert kwargs["until"] == "2025-01-01T00:00:00"
    assert kwargs["since"] is None


def test_email_list_route_method_accepts_enum_value(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.20/§V.88: --route-method is a Choice over the 7 persisted decisions."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.list_emails", return_value=[]) as mock_list,
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(
            main, ["email", "list", "--route-method", "skipped_no_workflows"]
        )

    assert result.exit_code == 0
    _, kwargs = mock_list.call_args
    assert kwargs["route_method"] == "skipped_no_workflows"


def test_email_list_route_method_rejects_out_of_set(runner: CliRunner) -> None:
    """§V.88: an out-of-set --route-method value is rejected at parse time."""
    with patch("mailpilot.settings.get_settings", return_value=make_test_settings()):
        result = runner.invoke(main, ["email", "list", "--route-method", "bogus"])

    assert result.exit_code != 0
    assert "bogus" in result.output


def test_workflow_list_type_flag_removed(runner: CliRunner) -> None:
    """§V.115: workflow --type is renamed to --direction with no back-compat alias."""
    with patch("mailpilot.settings.get_settings", return_value=make_test_settings()):
        result = runner.invoke(main, ["workflow", "list", "--type", "inbound"])

    assert result.exit_code != 0
    assert "no such option" in result.output.lower()


# -- Meeting CLI ---------------------------------------------------------------

_MEETING_ID = "01234567-0000-7000-0000-b00000000001"


def _make_meeting(**overrides: Any) -> Meeting:
    defaults: dict[str, Any] = {
        "id": _MEETING_ID,
        "google_event_id": "evt-1",
        "meet_url": "https://meet.google.com/abc-defg-hij",
        "summary": "Intro call",
        "scheduled_at": _NOW,
        "ends_at": _NOW,
        "status": "scheduled",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Meeting(**{**defaults, **overrides})


def _make_meeting_attendee(**overrides: Any) -> MeetingAttendee:
    defaults: dict[str, Any] = {
        "id": "01234567-0000-7000-0000-b00000000099",
        "meeting_id": _MEETING_ID,
        "contact_id": "01234567-0000-7000-0000-000000000003",
        "created_at": _NOW,
    }
    return MeetingAttendee(**{**defaults, **overrides})


def _make_meeting_summary(**overrides: Any) -> MeetingSummary:
    defaults: dict[str, Any] = {
        "id": _MEETING_ID,
        "google_event_id": "evt-1",
        "meet_url": "https://meet.google.com/abc-defg-hij",
        "summary": "Intro call",
        "scheduled_at": _NOW,
        "ends_at": _NOW,
        "status": "scheduled",
        "attendee_emails": ["alice@acme.com", "bob@acme.com"],
        "attendee_count": 2,
        "created_at": _NOW,
    }
    return MeetingSummary(**{**defaults, **overrides})


def _make_meeting_view(**overrides: Any) -> MeetingView:
    contact = _make_contact(email="alice@acme.com")
    defaults: dict[str, Any] = {
        "id": _MEETING_ID,
        "google_event_id": "evt-1",
        "meet_url": "https://meet.google.com/abc-defg-hij",
        "summary": "Intro call",
        "scheduled_at": _NOW,
        "ends_at": _NOW,
        "status": "scheduled",
        "attendees": [contact],
        "attendee_emails": [contact.email],
        "attendee_count": 1,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return MeetingView(**{**defaults, **overrides})


def test_meeting_no_create_command(runner: CliRunner) -> None:
    """§V.126: meeting rows are ingested, never operator-created -- no `create`."""
    with patch("mailpilot.settings.get_settings", return_value=make_test_settings()):
        result = runner.invoke(main, ["meeting", "create"])

    # Click rejects the unknown subcommand at parse time (usage error, exit 2).
    assert result.exit_code == 2


def test_meeting_list(runner: CliRunner, mock_connection: MagicMock) -> None:
    """§V.8/§V.96: meeting list rows carry the attendee summary (names who attends)."""
    meetings = [_make_meeting_summary()]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_meetings", return_value=meetings) as mock_list,
    ):
        result = runner.invoke(main, ["meeting", "list"])

    assert result.exit_code == 0, result.output
    mock_list.assert_called_once_with(
        mock_connection,
        limit=100,
        contact_id=None,
        status=None,
        since=None,
        until=None,
    )
    data = json.loads(result.output)
    assert len(data["meetings"]) == 1
    assert data["meetings"][0]["id"] == _MEETING_ID
    assert data["meetings"][0]["attendee_emails"] == ["alice@acme.com", "bob@acme.com"]
    assert data["meetings"][0]["attendee_count"] == 2


def test_meeting_list_with_filters(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    contact = _make_contact()
    meetings = [_make_meeting()]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact_by_email", return_value=contact),
        patch("mailpilot.database.list_meetings", return_value=meetings) as mock_list,
    ):
        result = runner.invoke(
            main,
            [
                "meeting",
                "list",
                "--contact-email",
                contact.email,
                "--status",
                "completed",
                "--limit",
                "5",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_list.assert_called_once_with(
        mock_connection,
        limit=5,
        contact_id=contact.id,
        status="completed",
        since=None,
        until=None,
    )


def test_meeting_list_status_rejects_out_of_set(runner: CliRunner) -> None:
    """§V.88: an out-of-set --status value is rejected at parse time."""
    with patch("mailpilot.settings.get_settings", return_value=make_test_settings()):
        result = runner.invoke(main, ["meeting", "list", "--status", "bogus"])

    assert result.exit_code != 0
    assert "bogus" in result.output


def test_meeting_view(runner: CliRunner, mock_connection: MagicMock) -> None:
    """§V.8/§B.112: meeting view inlines attendee contacts."""
    view = _make_meeting_view()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.load_meeting_view", return_value=view),
    ):
        result = runner.invoke(main, ["meeting", "view", view.id])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["meeting"]["id"] == view.id
    assert data["meeting"]["summary"] == "Intro call"
    assert data["meeting"]["attendees"][0]["email"] == "alice@acme.com"
    assert data["meeting"]["attendee_emails"] == ["alice@acme.com"]
    assert data["meeting"]["attendee_count"] == 1


def test_meeting_view_not_found(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.load_meeting_view", return_value=None),
    ):
        result = runner.invoke(main, ["meeting", "view", _MEETING_ID])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_meeting_add_attendee(runner: CliRunner, mock_connection: MagicMock) -> None:
    meeting = _make_meeting()
    contact = _make_contact()
    link = _make_meeting_attendee(contact_id=contact.id)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_meeting", return_value=meeting),
        patch("mailpilot.database.get_contact_by_email", return_value=contact),
        patch(
            "mailpilot.database.link_meeting_attendee", return_value=link
        ) as mock_link,
    ):
        result = runner.invoke(
            main,
            ["meeting", "add", meeting.id, "--contact-email", contact.email],
        )

    assert result.exit_code == 0, result.output
    mock_link.assert_called_once_with(mock_connection, meeting.id, contact.id)
    data = json.loads(result.output)
    assert data["meeting_attendee"]["meeting_id"] == meeting.id
    assert data["meeting_attendee"]["contact_id"] == contact.id


def test_meeting_add_meeting_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.94: a missing meeting errors not_found before any link write."""
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_meeting", return_value=None),
    ):
        result = runner.invoke(
            main, ["meeting", "add", _MEETING_ID, "--contact-email", "x@acme.com"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_meeting_add_duplicate_pair(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    """§V.125: re-linking the same pair errors already_exists."""
    meeting = _make_meeting()
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_meeting", return_value=meeting),
        patch("mailpilot.database.get_contact_by_email", return_value=contact),
        patch("mailpilot.database.link_meeting_attendee", return_value=None),
    ):
        result = runner.invoke(
            main,
            ["meeting", "add", meeting.id, "--contact-email", contact.email],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "already_exists"


def test_meeting_update(runner: CliRunner, mock_connection: MagicMock) -> None:
    before = _make_meeting(summary="Intro call", status="scheduled")
    after = _make_meeting(summary="Renamed", status="completed")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_meeting", return_value=before),
        patch("mailpilot.database.update_meeting", return_value=after) as mock_update,
    ):
        result = runner.invoke(
            main,
            [
                "meeting",
                "update",
                before.id,
                "--summary",
                "Renamed",
                "--status",
                "completed",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_update.assert_called_once_with(
        mock_connection, before.id, summary="Renamed", status="completed"
    )
    data = json.loads(result.output)
    assert data["meeting"]["summary"] == "Renamed"
    assert data["meeting"]["status"] == "completed"


def test_meeting_update_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_meeting", return_value=None),
    ):
        result = runner.invoke(
            main, ["meeting", "update", _MEETING_ID, "--summary", "x"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_meeting_update_status_rejects_out_of_set(runner: CliRunner) -> None:
    """§V.88: an out-of-set --status value is rejected at parse time."""
    with patch("mailpilot.settings.get_settings", return_value=make_test_settings()):
        result = runner.invoke(
            main, ["meeting", "update", _MEETING_ID, "--status", "bogus"]
        )

    assert result.exit_code != 0
    assert "bogus" in result.output


def test_meeting_cancel(runner: CliRunner, mock_connection: MagicMock) -> None:
    before = _make_meeting(status="scheduled")
    after = _make_meeting(status="cancelled")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_meeting", return_value=before),
        patch("mailpilot.database.update_meeting", return_value=after) as mock_update,
    ):
        result = runner.invoke(main, ["meeting", "cancel", before.id])

    assert result.exit_code == 0, result.output
    mock_update.assert_called_once_with(mock_connection, before.id, status="cancelled")
    data = json.loads(result.output)
    assert data["meeting"]["status"] == "cancelled"


def test_meeting_cancel_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_meeting", return_value=None),
    ):
        result = runner.invoke(main, ["meeting", "cancel", _MEETING_ID])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
