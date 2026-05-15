"""Span-emission and operator-event contract tests for CLI mutations.

Enforces SPEC §V.47: every operator-initiated CRM-config CLI mutation
(noun in {account, company, contact, workflow, enrollment, tag, note};
verb in {create, update, add, remove, start, stop, import}) wraps its
DB call in ``logfire.span("<noun>.<verb>", ...)`` and emits a paired
``operator_event``. On uncaught DB exception (simulated here via
``psycopg.OperationalError``), the handler emits ``logfire.exception``
and ``operator_event("error", source="<noun>.<verb>", ...)``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from click.testing import CliRunner
from logfire.testing import CaptureLogfire

from conftest import make_test_settings
from mailpilot.cli import main
from mailpilot.models import (
    Account,
    Company,
    Contact,
    Enrollment,
    Note,
    Tag,
    Workflow,
)

_NOW = datetime(2024, 1, 1, tzinfo=UTC)
_ACCOUNT_ID = "01234567-0000-7000-0000-0000000000aa"
_COMPANY_ID = "01234567-0000-7000-0000-0000000000bb"
_CONTACT_ID = "01234567-0000-7000-0000-0000000000cc"
_WORKFLOW_ID = "01234567-0000-7000-0000-0000000000dd"
_TAG_ID = "01234567-0000-7000-0000-0000000000ee"
_NOTE_ID = "01234567-0000-7000-0000-0000000000ff"


def _make_account(**overrides: Any) -> Account:
    defaults: dict[str, Any] = {
        "id": _ACCOUNT_ID,
        "email": "outbound@lab5.ca",
        "display_name": "Outbound",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Account(**{**defaults, **overrides})


def _make_company(**overrides: Any) -> Company:
    defaults: dict[str, Any] = {
        "id": _COMPANY_ID,
        "name": "Acme",
        "domain": "acme.test",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Company(**{**defaults, **overrides})


def _make_contact(**overrides: Any) -> Contact:
    defaults: dict[str, Any] = {
        "id": _CONTACT_ID,
        "email": "lead@acme.test",
        "domain": "acme.test",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Contact(**{**defaults, **overrides})


def _make_workflow(**overrides: Any) -> Workflow:
    defaults: dict[str, Any] = {
        "id": _WORKFLOW_ID,
        "name": "Outbound Test",
        "template": "outbound-general",
        "type": "outbound",
        "account_id": _ACCOUNT_ID,
        "status": "draft",
        "theme": "blue",
        "objective": "",
        "instructions": "",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Workflow(**{**defaults, **overrides})


def _make_enrollment(**overrides: Any) -> Enrollment:
    defaults: dict[str, Any] = {
        "workflow_id": _WORKFLOW_ID,
        "contact_id": _CONTACT_ID,
        "status": "active",
        "reason": "",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Enrollment(**{**defaults, **overrides})


def _make_tag(**overrides: Any) -> Tag:
    defaults: dict[str, Any] = {
        "id": _TAG_ID,
        "name": "prospect",
        "contact_id": _CONTACT_ID,
        "company_id": None,
        "created_at": _NOW,
    }
    return Tag(**{**defaults, **overrides})


def _make_note(**overrides: Any) -> Note:
    defaults: dict[str, Any] = {
        "id": _NOTE_ID,
        "body": "Hello",
        "contact_id": _CONTACT_ID,
        "company_id": None,
        "created_at": _NOW,
    }
    return Note(**{**defaults, **overrides})


def _spans_named(capfire: CaptureLogfire, span_name: str) -> list[dict[str, Any]]:
    return [
        span
        for span in capfire.exporter.exported_spans_as_dict()
        if span["name"] == span_name
    ]


def _err_event(stderr: str, source: str) -> str | None:
    """Return the first stderr operator_event line with `event=error` for source."""
    for line in stderr.splitlines():
        if "event=error" in line and f"source={source}" in line:
            return line
    return None


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mock_connection() -> MagicMock:
    return MagicMock()


@pytest.fixture(autouse=True)
def _preserve_capfire_exporter(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """Stop ``configure_logging`` from re-configuring logfire mid-test.

    ``cli.main`` calls ``configure_logging`` per invocation; that re-runs
    ``logfire.configure(...)`` which replaces the ``capfire`` fixture's
    ``TestExporter``. Patching it to a no-op preserves the test exporter
    so the §V.47 spans land where the assertions can read them.
    """
    monkeypatch.setattr("mailpilot.cli.configure_logging", lambda debug=False: None)


# -- account.create ------------------------------------------------------------


def test_account_create_emits_span_and_event(
    runner: CliRunner,
    mock_connection: MagicMock,
    capfire: CaptureLogfire,
    capsys: pytest.CaptureFixture[str],
) -> None:
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_account", return_value=account),
    ):
        result = runner.invoke(
            main,
            ["account", "create", "--email", account.email, "--display-name", "X"],
        )

    assert result.exit_code == 0, result.output
    assert _spans_named(capfire, "account.create")
    err = result.stderr
    assert "event=account.create" in err
    assert f"entity_id={account.id}" in err
    assert "changed=" in err


def test_account_create_emits_error_on_db_raise(
    runner: CliRunner,
    mock_connection: MagicMock,
    capfire: CaptureLogfire,
    capsys: pytest.CaptureFixture[str],
) -> None:
    boom = psycopg.OperationalError("db down")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_account", side_effect=boom),
    ):
        result = runner.invoke(
            main,
            ["account", "create", "--email", "x@y.z", "--display-name", ""],
        )

    assert result.exit_code != 0
    assert _err_event(result.stderr, "account.create") is not None
    failed = _spans_named(capfire, "account.create.failed")
    assert failed
    assert failed[0]["attributes"]["logfire.level_num"] >= 17


# -- account.update ------------------------------------------------------------


def test_account_update_changed_diff(
    runner: CliRunner,
    mock_connection: MagicMock,
    capfire: CaptureLogfire,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = _make_account(display_name="Old")
    after = _make_account(display_name="New")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=before),
        patch("mailpilot.database.update_account", return_value=after),
    ):
        result = runner.invoke(
            main,
            ["account", "update", before.id, "--display-name", "New"],
        )

    assert result.exit_code == 0, result.output
    assert _spans_named(capfire, "account.update")
    err = result.stderr
    assert "event=account.update" in err
    assert "changed=['display_name']" in err


def test_account_update_idempotent_pre_equals_post(
    runner: CliRunner,
    mock_connection: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    same = _make_account(display_name="Same")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=same),
        patch("mailpilot.database.update_account", return_value=same),
    ):
        result = runner.invoke(
            main,
            ["account", "update", same.id, "--display-name", "Same"],
        )

    assert result.exit_code == 0, result.output
    err = result.stderr
    assert "event=account.update" in err
    assert "changed=[]" in err


# -- company.create / update / import ------------------------------------------


def test_company_create_emits_span_and_event(
    runner: CliRunner,
    mock_connection: MagicMock,
    capfire: CaptureLogfire,
    capsys: pytest.CaptureFixture[str],
) -> None:
    company = _make_company()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_company", return_value=company),
    ):
        result = runner.invoke(
            main, ["company", "create", "--domain", "acme.test", "--name", "Acme"]
        )

    assert result.exit_code == 0, result.output
    assert _spans_named(capfire, "company.create")
    err = result.stderr
    assert "event=company.create" in err
    assert f"entity_id={company.id}" in err


def test_company_update_changed_diff(
    runner: CliRunner,
    mock_connection: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = _make_company(name="Old")
    after = _make_company(name="New")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_company", return_value=before),
        patch("mailpilot.database.update_company", return_value=after),
    ):
        result = runner.invoke(main, ["company", "update", before.id, "--name", "New"])

    assert result.exit_code == 0, result.output
    err = result.stderr
    assert "changed=['name']" in err


def test_company_import_idempotent_on_duplicate_rows(
    runner: CliRunner,
    mock_connection: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Re-run import on already-present rows -> every row event has changed=[]."""
    existing = [_make_company(name="Acme", domain="acme.test")]
    payload = json.dumps([{"name": "Acme", "domain": "acme.test"}])
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_companies", return_value=existing),
        patch("mailpilot.database.create_company") as mock_create,
    ):
        result = runner.invoke(main, ["company", "import"], input=payload)

    assert result.exit_code == 0, result.output
    mock_create.assert_not_called()
    err = result.stderr
    assert "event=company.import" in err
    assert err.count("changed=[]") >= 1


def test_company_create_with_note_includes_note_in_changed(
    runner: CliRunner,
    mock_connection: MagicMock,
    capfire: CaptureLogfire,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--note STR` extends operator_event `changed=` with `'note'` (§T.47)."""
    company = _make_company()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_company", return_value=company),
        patch("mailpilot.database.add_company_note"),
    ):
        result = runner.invoke(
            main,
            [
                "company",
                "create",
                "--domain",
                "acme.test",
                "--name",
                "Acme",
                "--note",
                "Inbound demo request.",
            ],
        )

    assert result.exit_code == 0, result.output
    assert _spans_named(capfire, "company.create")
    err = result.stderr
    assert "event=company.create" in err
    assert "'note'" in err


def test_contact_create_with_note_includes_note_in_changed(
    runner: CliRunner,
    mock_connection: MagicMock,
    capfire: CaptureLogfire,
    capsys: pytest.CaptureFixture[str],
) -> None:
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_contact", return_value=contact),
        patch("mailpilot.database.add_contact_note"),
    ):
        result = runner.invoke(
            main,
            [
                "contact",
                "create",
                "--email",
                contact.email,
                "--note",
                "First touch.",
            ],
        )

    assert result.exit_code == 0, result.output
    assert _spans_named(capfire, "contact.create")
    err = result.stderr
    assert "event=contact.create" in err
    assert "'note'" in err


def test_company_import_emits_changed_on_created_row(
    runner: CliRunner,
    mock_connection: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = json.dumps([{"name": "Acme", "domain": "acme.test"}])
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_companies", return_value=[]),
        patch("mailpilot.database.create_company", return_value=_make_company()),
    ):
        result = runner.invoke(main, ["company", "import"], input=payload)

    assert result.exit_code == 0, result.output
    err = result.stderr
    assert "event=company.import" in err
    # operator_event quotes values that contain whitespace (lists render with spaces).
    assert "changed=\"['name', 'domain']\"" in err


# -- contact.create / update / import ------------------------------------------


def test_contact_create_emits_span_and_event(
    runner: CliRunner,
    mock_connection: MagicMock,
    capfire: CaptureLogfire,
    capsys: pytest.CaptureFixture[str],
) -> None:
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.create_contact", return_value=contact),
    ):
        result = runner.invoke(main, ["contact", "create", "--email", contact.email])

    assert result.exit_code == 0, result.output
    assert _spans_named(capfire, "contact.create")
    err = result.stderr
    assert "event=contact.create" in err


def test_contact_update_changed_diff(
    runner: CliRunner,
    mock_connection: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = _make_contact(first_name="Alice")
    after = _make_contact(first_name="Bob")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=before),
        patch("mailpilot.database.update_contact", return_value=after),
    ):
        result = runner.invoke(
            main, ["contact", "update", before.id, "--first-name", "Bob"]
        )

    assert result.exit_code == 0, result.output
    err = result.stderr
    assert "changed=['first_name']" in err


def test_contact_import_idempotent_on_duplicate_rows(
    runner: CliRunner,
    mock_connection: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    existing = [_make_contact(email="lead@acme.test")]
    payload = json.dumps([{"email": "lead@acme.test"}])
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_contacts", return_value=existing),
        patch("mailpilot.database.create_contact") as mock_create,
    ):
        result = runner.invoke(main, ["contact", "import"], input=payload)

    assert result.exit_code == 0, result.output
    mock_create.assert_not_called()
    err = result.stderr
    assert err.count("changed=[]") >= 1


# -- workflow.create / update / start / stop / import --------------------------


def test_workflow_create_emits_span_and_event(
    runner: CliRunner,
    mock_connection: MagicMock,
    capfire: CaptureLogfire,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow = _make_workflow()
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.create_workflow", return_value=workflow),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "create",
                "--name",
                "Outbound",
                "--template",
                "outbound-general",
                "--account-id",
                account.id,
                "--draft",
            ],
        )

    assert result.exit_code == 0, result.output
    spans = _spans_named(capfire, "workflow.create")
    assert spans
    assert spans[0]["attributes"]["account_id"] == account.id
    err = result.stderr
    assert "event=workflow.create" in err


def test_workflow_update_changed_diff(
    runner: CliRunner,
    mock_connection: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = _make_workflow(name="Old")
    after = _make_workflow(name="New")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=before),
        patch("mailpilot.database.update_workflow", return_value=after),
    ):
        result = runner.invoke(main, ["workflow", "update", before.id, "--name", "New"])

    assert result.exit_code == 0, result.output
    err = result.stderr
    assert "changed=['name']" in err


def test_workflow_start_emits_status_change(
    runner: CliRunner,
    mock_connection: MagicMock,
    capfire: CaptureLogfire,
    capsys: pytest.CaptureFixture[str],
) -> None:
    activated = _make_workflow(status="active", objective="x", instructions="y")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.activate_workflow", return_value=activated),
    ):
        result = runner.invoke(main, ["workflow", "start", activated.id])

    assert result.exit_code == 0, result.output
    assert _spans_named(capfire, "workflow.start")
    err = result.stderr
    assert "event=workflow.start" in err
    assert "changed=['status']" in err


def test_workflow_stop_emits_status_change(
    runner: CliRunner,
    mock_connection: MagicMock,
    capfire: CaptureLogfire,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paused = _make_workflow(status="paused", objective="x", instructions="y")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.pause_workflow", return_value=paused),
    ):
        result = runner.invoke(main, ["workflow", "stop", paused.id])

    assert result.exit_code == 0, result.output
    assert _spans_named(capfire, "workflow.stop")
    err = result.stderr
    assert "event=workflow.stop" in err
    assert "changed=['status']" in err


def test_workflow_import_idempotent_on_unchanged_rows(
    runner: CliRunner,
    mock_connection: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Re-run import on already-up-to-date rows -> every row event has changed=[]."""
    existing = _make_workflow(
        name="Outbound", objective="x", instructions="y", theme="blue"
    )
    account = _make_account()
    payload = json.dumps(
        [
            {
                "name": "Outbound",
                "template": "outbound-general",
                "objective": "x",
                "instructions": "y",
                "theme": "blue",
            }
        ]
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.list_workflows_full", return_value=[existing]),
        patch("mailpilot.database.update_workflow") as mock_update,
    ):
        result = runner.invoke(
            main,
            ["workflow", "import", "--account-id", account.id],
            input=payload,
        )

    assert result.exit_code == 0, result.output
    mock_update.assert_not_called()
    err = result.stderr
    assert "event=workflow.import" in err
    assert "changed=[]" in err


# -- enrollment.add / remove / update ------------------------------------------


def test_enrollment_add_emits_span_and_event(
    runner: CliRunner,
    mock_connection: MagicMock,
    capfire: CaptureLogfire,
    capsys: pytest.CaptureFixture[str],
) -> None:
    enrollment = _make_enrollment()
    workflow = _make_workflow()
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.create_enrollment", return_value=enrollment),
        patch("mailpilot.database.create_activity"),
    ):
        result = runner.invoke(
            main,
            [
                "enrollment",
                "add",
                "--workflow-id",
                workflow.id,
                "--contact-id",
                contact.id,
            ],
        )

    assert result.exit_code == 0, result.output
    assert _spans_named(capfire, "enrollment.add")
    err = result.stderr
    assert "event=enrollment.add" in err
    assert "changed=['status']" in err


def test_enrollment_remove_emits_span_and_event(
    runner: CliRunner,
    mock_connection: MagicMock,
    capfire: CaptureLogfire,
    capsys: pytest.CaptureFixture[str],
) -> None:
    deleted = _make_enrollment(status="paused")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.delete_enrollment", return_value=deleted),
    ):
        result = runner.invoke(
            main,
            [
                "enrollment",
                "remove",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
            ],
        )

    assert result.exit_code == 0, result.output
    assert _spans_named(capfire, "enrollment.remove")
    err = result.stderr
    assert "event=enrollment.remove" in err


def test_enrollment_update_changed_diff(
    runner: CliRunner,
    mock_connection: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = _make_enrollment(status="active")
    after = _make_enrollment(status="paused")
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_enrollment", return_value=before),
        patch("mailpilot.database.update_enrollment", return_value=after),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.create_activity"),
    ):
        result = runner.invoke(
            main,
            [
                "enrollment",
                "update",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
                "--status",
                "paused",
            ],
        )

    assert result.exit_code == 0, result.output
    err = result.stderr
    assert "event=enrollment.update" in err
    assert "changed=['status']" in err


# -- tag.add / remove ----------------------------------------------------------


def test_tag_add_emits_span_and_event(
    runner: CliRunner,
    mock_connection: MagicMock,
    capfire: CaptureLogfire,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tag = _make_tag()
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.add_contact_tag", return_value=tag),
    ):
        result = runner.invoke(
            main, ["tag", "add", "--contact-id", contact.id, "prospect"]
        )

    assert result.exit_code == 0, result.output
    assert _spans_named(capfire, "tag.add")
    err = result.stderr
    assert "event=tag.add" in err
    assert "owner_type=contact" in err
    assert f"owner_id={contact.id}" in err


def test_tag_remove_emits_span_and_event(
    runner: CliRunner,
    mock_connection: MagicMock,
    capfire: CaptureLogfire,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tag = _make_tag()
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.remove_contact_tag", return_value=tag),
    ):
        result = runner.invoke(
            main, ["tag", "remove", "--contact-id", contact.id, "prospect"]
        )

    assert result.exit_code == 0, result.output
    assert _spans_named(capfire, "tag.remove")
    err = result.stderr
    assert "event=tag.remove" in err


# -- note.add ------------------------------------------------------------------


def test_note_add_emits_span_and_event(
    runner: CliRunner,
    mock_connection: MagicMock,
    capfire: CaptureLogfire,
    capsys: pytest.CaptureFixture[str],
) -> None:
    note = _make_note()
    contact = _make_contact()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.add_contact_note", return_value=note),
    ):
        result = runner.invoke(
            main, ["note", "add", "--contact-id", contact.id, "--body", "Hello"]
        )

    assert result.exit_code == 0, result.output
    assert _spans_named(capfire, "note.add")
    err = result.stderr
    assert "event=note.add" in err
    assert f"owner_id={contact.id}" in err
    assert "changed=['body']" in err
