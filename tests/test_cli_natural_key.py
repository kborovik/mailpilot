"""§V.107 natural-key / polymorphic-resolution tests for the CLI surface.

Covers the natural-key sweep (§T.162): keyed-entity verb targets and
Scope/owner options resolve a value polymorphically -- a UUID-shaped value by
id, any other value by the natural key (account email, company domain, contact
email) via the existing ``get_<entity>_by_<key>`` helpers (§V.90). Unknown keys
exit ``not_found`` with no write (§V.94). Also covers ``account sync``'s
optional ``--account-email`` plus the ``--since`` full-INBOX backfill bound
(§V.75).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from conftest import make_test_settings
from mailpilot.cli import (
    _looks_like_uuid,  # pyright: ignore[reportPrivateUsage]
    main,
)
from mailpilot.models import (
    Account,
    Company,
    CompanyView,
    Contact,
    ContactView,
    Workflow,
)

_NOW = datetime(2024, 1, 1, tzinfo=UTC)
_UUID = "01234567-0000-7000-0000-000000000099"


def _account(**over: Any) -> Account:
    base: dict[str, Any] = {
        "id": _UUID,
        "email": "owner@example.com",
        "display_name": "Owner",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Account(**{**base, **over})


def _company(**over: Any) -> Company:
    base: dict[str, Any] = {
        "id": _UUID,
        "name": "Acme",
        "domain": "acme.com",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Company(**{**base, **over})


def _contact(**over: Any) -> Contact:
    base: dict[str, Any] = {
        "id": _UUID,
        "email": "lead@acme.com",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Contact(**{**base, **over})


def _workflow(**over: Any) -> Workflow:
    base: dict[str, Any] = {
        "id": _UUID,
        "name": "ai-engineering",
        "template": "outbound-general",
        "type": "outbound",
        "account_id": _UUID,
        "account_email": "owner@example.com",
        "status": "draft",
        "goal": "",
        "instructions": "",
        "theme": "blue",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Workflow(**{**base, **over})


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def conn() -> MagicMock:
    return MagicMock()


@pytest.fixture(autouse=True)
def _silence_operator_event(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    monkeypatch.setattr("mailpilot.operator_log.operator_event", lambda *_a, **_k: None)


# -- _looks_like_uuid (the polymorphic discriminator, §V.107) ------------------


@pytest.mark.parametrize(
    "value",
    [
        "01234567-0000-7000-0000-000000000001",
        "ABCDEF01-1234-7ABC-89AB-CDEF01234567",
    ],
)
def test_looks_like_uuid_accepts_uuid_shape(value: str) -> None:
    assert _looks_like_uuid(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "owner@example.com",  # email carries an at-sign
        "acme.com",  # domain carries dots
        "no-contacts-found",  # tag-style free text
        "0123456-0000-7000-0000-000000000001",  # first group too short
        "0123456g-0000-7000-0000-000000000001",  # non-hex digit
        "",
    ],
)
def test_looks_like_uuid_rejects_natural_keys(value: str) -> None:
    assert _looks_like_uuid(value) is False


# -- keyed-entity verb targets resolve polymorphically (§V.107, §V.90) ---------


def test_account_view_by_email_uses_natural_key(
    runner: CliRunner, conn: MagicMock
) -> None:
    account = _account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=conn),
        patch(
            "mailpilot.database.get_account_by_email", return_value=account
        ) as by_email,
        patch("mailpilot.database.get_account") as by_id,
    ):
        result = runner.invoke(main, ["account", "view", "owner@example.com"])

    assert result.exit_code == 0, result.output
    by_email.assert_called_once_with(conn, "owner@example.com")
    by_id.assert_not_called()


def test_account_view_by_uuid_uses_id(runner: CliRunner, conn: MagicMock) -> None:
    account = _account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=conn),
        patch("mailpilot.database.get_account", return_value=account) as by_id,
        patch("mailpilot.database.get_account_by_email") as by_email,
    ):
        result = runner.invoke(main, ["account", "view", _UUID])

    assert result.exit_code == 0, result.output
    by_id.assert_called_once_with(conn, _UUID)
    by_email.assert_not_called()


def test_account_view_unknown_email_not_found(
    runner: CliRunner, conn: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=conn),
        patch("mailpilot.database.get_account_by_email", return_value=None),
    ):
        result = runner.invoke(main, ["account", "view", "ghost@example.com"])

    assert result.exit_code == 1
    import json

    assert json.loads(result.output)["error"] == "not_found"


def test_company_view_by_domain_resolves_then_loads(
    runner: CliRunner, conn: MagicMock
) -> None:
    company = _company()
    view = CompanyView(
        id=company.id,
        name="Acme",
        domain="acme.com",
        created_at=_NOW,
        updated_at=_NOW,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=conn),
        patch(
            "mailpilot.database.get_company_by_domain", return_value=company
        ) as by_domain,
        patch("mailpilot.database.load_company_view", return_value=view) as load_view,
    ):
        result = runner.invoke(main, ["company", "view", "acme.com"])

    assert result.exit_code == 0, result.output
    by_domain.assert_called_once_with(conn, "acme.com")
    load_view.assert_called_once_with(conn, company.id)


def test_contact_view_by_email_resolves_then_loads(
    runner: CliRunner, conn: MagicMock
) -> None:
    contact = _contact()
    view = ContactView(
        id=contact.id, email="lead@acme.com", created_at=_NOW, updated_at=_NOW
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=conn),
        patch(
            "mailpilot.database.get_contact_by_email", return_value=contact
        ) as by_email,
        patch("mailpilot.database.load_contact_view", return_value=view) as load_view,
    ):
        result = runner.invoke(main, ["contact", "view", "lead@acme.com"])

    assert result.exit_code == 0, result.output
    by_email.assert_called_once_with(conn, "lead@acme.com")
    load_view.assert_called_once_with(conn, contact.id)


def test_workflow_view_by_name_uses_natural_key(
    runner: CliRunner, conn: MagicMock
) -> None:
    workflow = _workflow()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=conn),
        patch(
            "mailpilot.database.get_workflow_by_name", return_value=workflow
        ) as by_name,
        patch("mailpilot.database.get_workflow", return_value=workflow) as by_id,
    ):
        result = runner.invoke(main, ["workflow", "view", "ai-engineering"])

    assert result.exit_code == 0, result.output
    by_name.assert_called_once_with(conn, "ai-engineering")
    by_id.assert_called_once_with(conn, workflow.id)


def test_workflow_view_by_uuid_uses_id(runner: CliRunner, conn: MagicMock) -> None:
    workflow = _workflow()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=conn),
        patch("mailpilot.database.get_workflow", return_value=workflow) as by_id,
        patch("mailpilot.database.get_workflow_by_name") as by_name,
    ):
        result = runner.invoke(main, ["workflow", "view", _UUID])

    assert result.exit_code == 0, result.output
    by_id.assert_called_once_with(conn, _UUID)
    by_name.assert_not_called()


def test_workflow_view_unknown_name_not_found(
    runner: CliRunner, conn: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=conn),
        patch("mailpilot.database.get_workflow_by_name", return_value=None),
    ):
        result = runner.invoke(main, ["workflow", "view", "no-such-flow"])

    assert result.exit_code == 1
    import json

    assert json.loads(result.output)["error"] == "not_found"


# -- owner refs validate by natural key before mutation (§V.94) ----------------


def test_note_add_owner_by_email_resolves_to_id(
    runner: CliRunner, conn: MagicMock
) -> None:
    contact = _contact()
    note = MagicMock()
    note.model_dump.return_value = {"id": "n-1"}
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=conn),
        patch("mailpilot.database.get_contact_by_email", return_value=contact),
        patch("mailpilot.database.add_contact_note", return_value=note) as add_note,
    ):
        result = runner.invoke(
            main,
            ["note", "add", "--contact-email", "lead@acme.com", "--body", "hi"],
        )

    assert result.exit_code == 0, result.output
    add_note.assert_called_once_with(conn, contact_id=contact.id, body="hi")


def test_note_add_unknown_company_domain_not_found_no_write(
    runner: CliRunner, conn: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=conn),
        patch("mailpilot.database.get_company_by_domain", return_value=None),
        patch("mailpilot.database.add_company_note") as add_note,
    ):
        result = runner.invoke(
            main,
            ["note", "add", "--company-domain", "ghost.com", "--body", "hi"],
        )

    assert result.exit_code == 1
    import json

    assert json.loads(result.output)["error"] == "not_found"
    add_note.assert_not_called()


# -- account sync: optional --account-email + --since backfill (§V.75, §V.107) -


def test_account_sync_account_email_polymorphic(
    runner: CliRunner, conn: MagicMock
) -> None:
    account = _account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=conn),
        patch(
            "mailpilot.database.get_account_by_email", return_value=account
        ) as by_email,
        patch("mailpilot.database.list_accounts") as list_accounts,
        patch("mailpilot.gmail.GmailClient"),
        patch("mailpilot.gmail.has_google_credentials", return_value=False),
        patch("mailpilot.sync.sync_account", return_value=0) as sync,
    ):
        result = runner.invoke(
            main, ["account", "sync", "--account-email", "owner@example.com"]
        )

    assert result.exit_code == 0, result.output
    by_email.assert_called_once_with(conn, "owner@example.com")
    list_accounts.assert_not_called()
    assert sync.call_args.kwargs["backfill_since"] is None


def test_account_sync_since_passes_backfill_bound(
    runner: CliRunner, conn: MagicMock
) -> None:
    account = _account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=conn),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.gmail.GmailClient"),
        patch("mailpilot.gmail.has_google_credentials", return_value=False),
        patch("mailpilot.sync.sync_account", return_value=0) as sync,
    ):
        result = runner.invoke(
            main,
            [
                "account",
                "sync",
                "--account-email",
                _UUID,
                "--since",
                "2026-01-01T00:00:00+00:00",
            ],
        )

    assert result.exit_code == 0, result.output
    passed = sync.call_args.kwargs["backfill_since"]
    assert passed == datetime(2026, 1, 1, tzinfo=UTC)


def test_account_sync_invalid_since_validation_error(
    runner: CliRunner, conn: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=conn),
        patch("mailpilot.sync.sync_account") as sync,
    ):
        result = runner.invoke(
            main, ["account", "sync", "--account-email", _UUID, "--since", "nope"]
        )

    assert result.exit_code == 1
    import json

    assert json.loads(result.output)["error"] == "validation_error"
    sync.assert_not_called()


def test_collect_new_message_ids_applies_after_predicate_on_full_path() -> None:
    """§V.75: backfill_since becomes a Gmail ``after:`` epoch on the full path."""
    from mailpilot.sync import (
        _collect_new_message_ids,  # pyright: ignore[reportPrivateUsage]
    )

    account = _account()
    # No history + never synced -> full-listing path.
    account = account.model_copy(
        update={"gmail_history_id": None, "last_synced_at": None}
    )
    client = MagicMock()
    client.list_messages.return_value = []
    since = datetime(2026, 1, 1, tzinfo=UTC)

    ids, mode = _collect_new_message_ids(account, client, since)

    assert mode == "full"
    assert ids == []
    query = client.list_messages.call_args.kwargs["query"]
    assert query == f"after:{int(since.timestamp())}"


def test_collect_new_message_ids_no_backfill_empty_query() -> None:
    """Without a bound, the full-listing query stays empty (no ``after:``)."""
    from mailpilot.sync import (
        _collect_new_message_ids,  # pyright: ignore[reportPrivateUsage]
    )

    account = _account().model_copy(
        update={"gmail_history_id": None, "last_synced_at": None}
    )
    client = MagicMock()
    client.list_messages.return_value = []

    _collect_new_message_ids(account, client)

    assert client.list_messages.call_args.kwargs["query"] == ""
