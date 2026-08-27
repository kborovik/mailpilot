"""§V.195 read-only TUI for companies and contacts."""

from __future__ import annotations

import ast
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import psycopg
import pytest
from click.testing import CliRunner
from psycopg import sql

from conftest import (
    TEST_DATABASE_URL,
    make_test_company,
    make_test_contact,
)
from mailpilot.cli import main
from mailpilot.database import (
    disable_company,
    disable_contact,
    list_company_inspect_contacts,
    load_company_view,
    load_contact_view,
)
from mailpilot.models import CompanySummary, ContactSummary
from mailpilot.tui import (
    COMPANY_LIMIT,
    CONTACT_LIMIT,
    MailpilotTui,
    TuiConnectError,
    format_company_detail,
    hide_disabled,
    is_truncated,
    open_readonly_connection,
)

REPO = Path(__file__).resolve().parents[1]
TUI_PATH = REPO / "src" / "mailpilot" / "tui.py"
PYPROJECT = REPO / "pyproject.toml"

_ALLOWED_DATABASE_IMPORTS = frozenset(
    {
        "determine_schema_verdict",
        "list_companies",
        "list_company_inspect_contacts",
        "list_contacts",
        "load_company_view",
        "load_contact_view",
        "search_companies",
        "search_contacts",
        "_connect_database",
    }
)
_BANNED_PREFIXES = (
    "create_",
    "update_",
    "disable_",
    "enable_",
    "delete_",
)


def _database_import_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        if not node.module.startswith("mailpilot.database"):
            continue
        for alias in node.names:
            names.add(alias.name)
    return names


def test_tui_is_separate_top_level_command() -> None:
    """§V.195: mailpilot tui is a top-level command, not a show subcommand."""
    assert "tui" in main.commands
    show = main.commands["show"]
    assert hasattr(show, "commands")
    assert "tui" not in show.commands  # type: ignore[union-attr]


def test_show_queue_is_not_textualized() -> None:
    """§V.166 / §V.195: show queue stays the ASCII tabulate report."""
    show_src = (REPO / "src" / "mailpilot" / "cli" / "show.py").read_text(
        encoding="utf-8"
    )
    queue_src = (REPO / "src" / "mailpilot" / "queue.py").read_text(encoding="utf-8")
    assert "textual" not in show_src
    assert "textual" not in queue_src
    assert "tabulate" in show_src
    result = CliRunner().invoke(main, ["show", "queue", "--help"])
    assert result.exit_code == 0
    assert "table" in result.output.lower()


def test_tui_piped_stdout_error_envelope_zero_escapes() -> None:
    """§V.195 / §V.4: piped stdout is the error envelope, exit 1, no escapes."""
    completed = subprocess.run(
        [sys.executable, "-m", "mailpilot", "tui"],
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 1
    assert b"\x1b" not in completed.stdout
    assert completed.stdout == b""
    payload = json.loads(completed.stderr.decode())
    assert payload["ok"] is False
    assert payload["error"] == "validation_error"
    assert "TTY" in payload["message"]
    assert "record_count" not in payload


def test_tui_cli_runner_non_tty_envelope() -> None:
    """CliRunner stdout is not a TTY, so the command refuses."""
    result = CliRunner().invoke(main, ["tui"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"] == "validation_error"
    assert b"\x1b" not in result.stdout.encode()


def test_missing_textual_names_tui_extra() -> None:
    """§V.195: missing textual names mailpilot-crm[tui] and exits 1."""
    with (
        patch(
            "mailpilot.cli.tui._import_tui",
            side_effect=ImportError("No module named 'textual'", name="textual"),
        ),
        patch("mailpilot.cli.tui._stdout_is_tty", return_value=True),
    ):
        result = CliRunner().invoke(main, ["tui"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"] == "validation_error"
    assert "mailpilot-crm[tui]" in payload["message"]


def test_textual_is_optional_extra_not_main_dep() -> None:
    """§V.195: textual is extra tui, not a main dependency."""
    text = PYPROJECT.read_text(encoding="utf-8")
    deps_block = text.split("[project.optional-dependencies]", 1)[0]
    assert "textual" not in deps_block.split("dependencies = [", 1)[1]
    assert "tui = [" in text
    assert "textual" in text.split("[project.optional-dependencies]", 1)[1]


def test_tui_py_database_imports_allowlisted() -> None:
    """§V.195: tui.py database imports are allowlisted reads only."""
    names = _database_import_names(TUI_PATH)
    assert names == _ALLOWED_DATABASE_IMPORTS
    tree = ast.parse(TUI_PATH.read_text(encoding="utf-8"))
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
    for banned in (
        "initialize_database",
        "provision_database",
        "migrate_database",
    ):
        assert banned not in identifiers
    for name in names:
        assert not name.startswith(_BANNED_PREFIXES), name
        for token in ("provision", "migrate", "export", "import"):
            assert token not in name, name


def test_ast_allowlist_rejects_write_import(tmp_path: Path) -> None:
    """AST contract rejects a non-read database import."""
    fake = tmp_path / "tui.py"
    fake.write_text(
        "from mailpilot.database import list_companies, create_company\n",
        encoding="utf-8",
    )
    names = _database_import_names(fake)
    assert "create_company" in names
    assert any(n.startswith("create_") for n in names)


def test_hide_disabled_filters_client_side() -> None:
    """Search include-disabled is a client-side filter on disabled_reason."""
    from datetime import UTC, datetime

    now = datetime(2024, 1, 1, tzinfo=UTC)
    live = CompanySummary(
        id="1",
        name="Live",
        domain="live.test",
        has_profile=False,
        contact_count=0,
        created_at=now,
    )
    gone = CompanySummary(
        id="2",
        name="Gone",
        domain="gone.test",
        has_profile=False,
        contact_count=0,
        disabled_reason="retired",
        created_at=now,
    )
    assert hide_disabled([live, gone], include_disabled=False) == [live]
    assert hide_disabled([live, gone], include_disabled=True) == [live, gone]


def test_truncated_status_when_fetch_hits_limit() -> None:
    """Status truncated marker fires when fetch count equals the cap."""
    assert is_truncated(COMPANY_LIMIT, COMPANY_LIMIT) is True
    assert is_truncated(COMPANY_LIMIT - 1, COMPANY_LIMIT) is False
    assert is_truncated(CONTACT_LIMIT, CONTACT_LIMIT) is True
    assert is_truncated(0, CONTACT_LIMIT) is False


def test_empty_database_connect_refuses_without_provisioning() -> None:
    """§V.195: empty DB connect errors; tui never creates tables."""
    admin = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    dbname = "mailpilot_tui_empty"
    ident = sql.Identifier(dbname)
    admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(ident))
    admin.execute(sql.SQL("CREATE DATABASE {}").format(ident))
    empty_url = f"{TEST_DATABASE_URL.rsplit('/', 1)[0]}/{dbname}"
    try:
        with pytest.raises(TuiConnectError) as excinfo:
            open_readonly_connection(empty_url)
        assert excinfo.value.code == "schema_drift"
        assert "does not provision" in excinfo.value.message
        probe = psycopg.connect(empty_url, autocommit=True)
        try:
            row = probe.execute("SELECT to_regclass('account') AS oid").fetchone()
            assert row is None or row[0] is None
        finally:
            probe.close()
    finally:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(ident))
        admin.close()


def test_tui_connection_rejects_writes() -> None:
    """Any write over the TUI connection fails at the database."""
    connection = open_readonly_connection(TEST_DATABASE_URL)
    try:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            connection.execute(
                "INSERT INTO company (id, name, domain) VALUES (%s, %s, %s)",
                ("01234567-0000-7000-0000-000000000099", "Nope", "nope.tui"),
            )
    finally:
        connection.close()


def test_tui_connect_does_not_call_initialize_database() -> None:
    """Connect path never calls initialize_database or provision."""
    with (
        patch("mailpilot.database.initialize_database") as init,
        patch("mailpilot.database.provision_database") as provision,
    ):
        connection = open_readonly_connection(TEST_DATABASE_URL)
        connection.close()
    init.assert_not_called()
    provision.assert_not_called()


def test_company_and_contact_detail_match_shared_loaders(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Core detail fields equal company view / contact view loaders."""
    company = make_test_company(
        database_connection, name="View Co", domain="viewco.tui"
    )
    contact = make_test_contact(
        database_connection, email="ada@viewco.tui", company_id=company.id
    )
    database_connection.commit()
    expected_company = load_company_view(database_connection, company.id)
    expected_contact = load_contact_view(database_connection, contact.id)
    assert expected_company is not None
    assert expected_contact is not None
    extras = list_company_inspect_contacts(database_connection, company.id)
    tui_conn = open_readonly_connection(TEST_DATABASE_URL)
    try:
        app = MailpilotTui(tui_conn, detail_delay=0)

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                assert app.company_view is not None
                assert app.company_view.model_dump(
                    mode="json"
                ) == expected_company.model_dump(mode="json")
                assert "contacts" not in expected_company.model_dump(mode="json")
                assert app.company_child_contacts == extras
                tabs = app.query_one("#tabs")
                tabs.active = "contacts"  # type: ignore[attr-defined]
                await pilot.pause()
                app._select_row("contact-table", contact.id)  # pyright: ignore[reportPrivateUsage]
                app._load_detail("contacts", contact.id)  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()
                assert app.contact_view is not None
                assert app.contact_view.model_dump(
                    mode="json"
                ) == expected_contact.model_dump(mode="json")

        asyncio.run(body())
    finally:
        tui_conn.close()


def test_child_contacts_are_extras_not_lean_view_fields(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Child contacts are company view --full extras, not lean view fields."""
    company = make_test_company(database_connection, name="Kids Co", domain="kids.tui")
    make_test_contact(database_connection, email="kid@kids.tui", company_id=company.id)
    database_connection.commit()
    view = load_company_view(database_connection, company.id)
    assert view is not None
    dump = view.model_dump(mode="json")
    assert "contacts" not in dump
    extras = list_company_inspect_contacts(database_connection, company.id)
    assert extras
    assert extras[0]["email"] == "kid@kids.tui"
    text = format_company_detail(view, child_contacts=extras)
    assert "not lean view" not in text
    assert f"contacts: {len(extras)}" in text
    assert extras[0]["email"] not in text


def _run_pilot(
    database_connection: psycopg.Connection[dict[str, Any]],
    seed: bool = True,
) -> tuple[MailpilotTui, psycopg.Connection[dict[str, Any]], dict[str, Any]]:
    ids: dict[str, Any] = {}
    if seed:
        company = make_test_company(
            database_connection, name="Acme Tui", domain="acme.tui"
        )
        other = make_test_company(
            database_connection, name="Beta Tui", domain="beta.tui"
        )
        contact = make_test_contact(
            database_connection,
            email="lead@acme.tui",
            company_id=company.id,
        )
        disabled_co = make_test_company(
            database_connection, name="Gone Tui", domain="gone.tui"
        )
        disable_company(database_connection, disabled_co.id, "retired")
        disabled_ct = make_test_contact(
            database_connection, email="gone@gone.tui", company_id=disabled_co.id
        )
        disable_contact(database_connection, disabled_ct.id, "retired")
        database_connection.commit()
        ids = {
            "company": company,
            "other": other,
            "contact": contact,
            "disabled_co": disabled_co,
            "disabled_ct": disabled_ct,
        }
    tui_conn = open_readonly_connection(TEST_DATABASE_URL)
    app = MailpilotTui(tui_conn, detail_delay=0)
    return app, tui_conn, ids


def test_pilot_tab_switch_search_disabled(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Pilot: tab switch, search submit path, disabled toggle."""
    from textual.widgets import DataTable, TabbedContent

    app, tui_conn, _ids = _run_pilot(database_connection)
    try:

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                company_table = app.query_one("#company-table", DataTable)
                assert company_table.row_count >= 2
                tabs = app.query_one("#tabs", TabbedContent)
                tabs.active = "contacts"
                await pilot.pause()
                assert tabs.active == "contacts"
                contact_table = app.query_one("#contact-table", DataTable)
                assert contact_table.row_count >= 1
                tabs.active = "companies"
                await pilot.pause()
                app.search_query["companies"] = "acme"
                app._reload("companies")  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()
                assert len(app.company_rows) == 1
                assert app.company_rows[0].domain == "acme.tui"
                status = str(app.query_one("#status").render())
                assert "truncated" not in status
                assert "q=acme" in status
                app.search_query["companies"] = ""
                app._reload("companies")  # pyright: ignore[reportPrivateUsage]
                enabled_count = len(app.company_rows)
                app.include_disabled = True
                app._reload("companies")  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()
                assert len(app.company_rows) > enabled_count
                assert any(r.disabled_reason for r in app.company_rows)
                await pilot.press("/")
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()

        asyncio.run(body())
    finally:
        tui_conn.close()


def test_pilot_cross_link(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Pilot: Enter cross-link company child contact and contact domain."""
    from textual.widgets import TabbedContent

    app, tui_conn, ids = _run_pilot(database_connection)
    try:

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                app.cross_link_to_contact(ids["contact"].id)
                await pilot.pause()
                assert app.query_one("#tabs", TabbedContent).active == "contacts"
                assert app.contact_view is not None
                assert app.contact_view.email == "lead@acme.tui"
                app.cross_link_to_company(ids["contact"].id)
                await pilot.pause()
                assert app.query_one("#tabs", TabbedContent).active == "companies"
                assert app.company_view is not None
                assert app.company_view.domain == "acme.tui"

        asyncio.run(body())
    finally:
        tui_conn.close()


def test_pilot_reload_cancels_stale_detail_timer(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Reload stops a pending detail timer so an empty table keeps (no rows)."""
    make_test_company(database_connection, name="Timer Co", domain="timer.tui")
    database_connection.commit()
    tui_conn = open_readonly_connection(TEST_DATABASE_URL)
    app = MailpilotTui(tui_conn, detail_delay=0.05)
    try:

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                assert app.company_view is not None
                old_id = app.company_rows[0].id
                app._schedule_detail("companies", old_id)  # pyright: ignore[reportPrivateUsage]
                app.search_query["companies"] = "zzz-no-match-tui"
                app._reload("companies")  # pyright: ignore[reportPrivateUsage]
                await asyncio.sleep(0.12)
                await pilot.pause()
                assert app.company_rows == []
                assert app.company_view is None
                assert "(no rows)" in str(app.query_one("#company-detail").render())

        asyncio.run(body())
    finally:
        tui_conn.close()


def test_pilot_cross_link_shows_disabled_child(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Enter on a disabled child contact turns include-disabled on and lands."""
    from textual.widgets import TabbedContent

    company = make_test_company(database_connection, name="Mix Co", domain="mix.tui")
    gone = make_test_contact(
        database_connection, email="gone@mix.tui", company_id=company.id
    )
    disable_contact(database_connection, gone.id, "retired")
    database_connection.commit()
    tui_conn = open_readonly_connection(TEST_DATABASE_URL)
    app = MailpilotTui(tui_conn, detail_delay=0)
    try:

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                assert any(
                    str(child.get("id")) == gone.id
                    for child in app.company_child_contacts
                )
                assert app.include_disabled is False
                app.cross_link_to_contact(gone.id)
                await pilot.pause()
                assert app.include_disabled is True
                assert app.query_one("#tabs", TabbedContent).active == "contacts"
                assert app.contact_view is not None
                assert app.contact_view.email == "gone@mix.tui"
                assert "disabled=on" in str(app.query_one("#status").render())

        asyncio.run(body())
    finally:
        tui_conn.close()


def test_pilot_cross_link_shows_disabled_parent_company(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Enter from an enabled contact whose company is disabled turns d on."""
    from textual.widgets import TabbedContent

    company = make_test_company(
        database_connection, name="Parent Gone", domain="parentgone.tui"
    )
    contact = make_test_contact(
        database_connection, email="live@parentgone.tui", company_id=company.id
    )
    disable_company(database_connection, company.id, "retired")
    database_connection.commit()
    tui_conn = open_readonly_connection(TEST_DATABASE_URL)
    app = MailpilotTui(tui_conn, detail_delay=0)
    try:

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                tabs = app.query_one("#tabs", TabbedContent)
                tabs.active = "contacts"
                await pilot.pause()
                app.search_query["contacts"] = contact.email
                app._reload("contacts")  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()
                assert [row.email for row in app.contact_rows] == [contact.email]
                assert app.contact_rows[0].company_domain == "parentgone.tui"
                app._select_row("contact-table", contact.id)  # pyright: ignore[reportPrivateUsage]
                app._load_detail("contacts", contact.id)  # pyright: ignore[reportPrivateUsage]
                assert app.contact_view is not None
                assert app.include_disabled is False
                app.cross_link_to_company(contact.id)
                await pilot.pause()
                assert app.include_disabled is True
                assert app.company_view is not None
                assert app.company_view.domain == "parentgone.tui"
                assert "disabled=on" in str(app.query_one("#status").render())
                assert any(row.domain == "parentgone.tui" for row in app.company_rows)

        asyncio.run(body())
    finally:
        tui_conn.close()


def test_tui_help_has_no_spec_cite() -> None:
    """§V.111: tui --help has zero SPEC citations."""
    result = CliRunner().invoke(main, ["tui", "--help"])
    assert result.exit_code == 0
    assert "§" not in result.output
    assert "read-only" in result.output.lower()


def test_cli_tui_module_is_click_only() -> None:
    """§V.2: cli/tui.py module-level imports stay click-only."""
    source = (REPO / "src" / "mailpilot" / "cli" / "tui.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "textual" not in imported
    assert "mailpilot.tui" not in imported
    assert "mailpilot.database" not in imported


def test_hide_disabled_contacts() -> None:
    from datetime import UTC, datetime

    now = datetime(2024, 1, 1, tzinfo=UTC)
    live = ContactSummary(
        id="1",
        email="a@t.tui",
        first_name="A",
        last_name="Live",
        title=None,
        company_id=None,
        company_domain=None,
        email_confidence=None,
        disabled_reason=None,
        created_at=now,
    )
    gone = ContactSummary(
        id="2",
        email="b@t.tui",
        first_name="B",
        last_name="Gone",
        title=None,
        company_id=None,
        company_domain=None,
        email_confidence=None,
        disabled_reason="bounced",
        created_at=now,
    )
    assert [r.email for r in hide_disabled([live, gone], include_disabled=False)] == [
        "a@t.tui"
    ]
