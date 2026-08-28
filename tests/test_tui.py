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
    CompanyStartScreen,
    DetailScreen,
    HelpScreen,
    MailpilotTui,
    SearchScreen,
    TuiConnectError,
    format_company_markdown,
    format_contact_markdown,
    format_profile,
    hide_disabled,
    is_truncated,
    open_readonly_connection,
    visible_search_rows,
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
        app = MailpilotTui(tui_conn)

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                app._select_row("company-table", company.id)  # pyright: ignore[reportPrivateUsage]
                app._open_detail("companies", company.id)  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()
                assert app.company_view is not None
                assert app.company_view.model_dump(
                    mode="json"
                ) == expected_company.model_dump(mode="json")
                assert "contacts" not in expected_company.model_dump(mode="json")
                assert app.company_child_contacts == extras
                await pilot.press("escape")
                await pilot.pause()
                tabs = app.query_one("#tabs")
                tabs.active = "contacts"  # type: ignore[attr-defined]
                await pilot.pause()
                app._select_row("contact-table", contact.id)  # pyright: ignore[reportPrivateUsage]
                app._open_detail("contacts", contact.id)  # pyright: ignore[reportPrivateUsage]
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
    text = format_company_markdown(view)
    assert extras[0]["email"] not in text
    assert "## Contacts" in text


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
    app = MailpilotTui(tui_conn)
    return app, tui_conn, ids


def test_pilot_tab_switch_search_disabled(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Pilot: tab switch, search submit path, disabled toggle."""
    from textual.widgets import DataTable, Input, TabbedContent

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
                await pilot.press("/")
                await pilot.pause()
                assert isinstance(app.screen, SearchScreen)
                search = app.screen.query_one("#search-input", Input)
                search.value = "acme"
                await search.action_submit()
                await pilot.pause()
                assert len(app.company_rows) == 1
                assert app.company_rows[0].domain == "acme.tui"
                status = str(app.query_one("#status").render())
                assert "truncated" not in status
                assert "q=acme" in status
                assert not isinstance(app.screen, SearchScreen)
                await pilot.press("escape")
                await pilot.pause()
                assert app.search_query["companies"] == ""
                enabled_count = len(app.company_rows)
                app.include_disabled = True
                app._reload("companies")  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()
                assert len(app.company_rows) > enabled_count
                assert any(r.disabled_reason for r in app.company_rows)

        asyncio.run(body())
    finally:
        tui_conn.close()


def test_layout_one_table_per_tab_search_hidden(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Each tab is one DataTable; search overlay is hidden; no child table."""
    from textual.widgets import DataTable, Footer, Input

    app, tui_conn, _ids = _run_pilot(database_connection)
    try:

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                table_ids = {widget.id for widget in app.query(DataTable)}
                assert table_ids == {"company-table", "contact-table"}
                assert len(app.query("#company-child-contacts")) == 0
                assert len(app.query("#search")) == 0
                assert len(app.query(Input)) == 0
                assert not isinstance(app.screen, SearchScreen)
                assert len(app.query(Footer)) == 1

        asyncio.run(body())
    finally:
        tui_conn.close()


def test_slash_shows_search_escape_hides(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Slash shows the search overlay; Esc hides it and restores the list."""
    from textual.widgets import Static

    app, tui_conn, _ids = _run_pilot(database_connection)
    try:

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                assert not isinstance(app.screen, SearchScreen)
                await pilot.press("/")
                await pilot.pause()
                assert isinstance(app.screen, SearchScreen)
                title = str(app.screen.query_one("#search-title", Static).render())
                assert title == "Search companies"
                await pilot.press("escape")
                await pilot.pause()
                assert not isinstance(app.screen, SearchScreen)
                assert app.search_query["companies"] == ""

        asyncio.run(body())
    finally:
        tui_conn.close()


def test_slash_types_into_focused_search(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """A second slash inserts '/' into the query instead of resetting it."""
    from textual.widgets import Input

    app, tui_conn, _ids = _run_pilot(database_connection)
    try:

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                await pilot.press("/")
                await pilot.pause()
                assert isinstance(app.screen, SearchScreen)
                search = app.screen.query_one("#search-input", Input)
                search.value = "VP "
                search.cursor_position = len(search.value)
                await pilot.press("/")
                await pilot.pause()
                assert search.value == "VP /"
                assert isinstance(app.screen, SearchScreen)

        asyncio.run(body())
    finally:
        tui_conn.close()


def test_search_overlay_title_per_tab(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Overlay title is Search companies or Search contacts per tab."""
    from textual.widgets import Static, TabbedContent

    app, tui_conn, _ids = _run_pilot(database_connection)
    try:

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                await pilot.press("/")
                await pilot.pause()
                title = str(app.screen.query_one("#search-title", Static).render())
                assert title == "Search companies"
                await pilot.press("escape")
                await pilot.pause()
                tabs = app.query_one("#tabs", TabbedContent)
                tabs.active = "contacts"
                await pilot.pause()
                await pilot.press("/")
                await pilot.pause()
                title = str(app.screen.query_one("#search-title", Static).render())
                assert title == "Search contacts"

        asyncio.run(body())
    finally:
        tui_conn.close()


def test_search_overlay_is_compact_centered_not_docked(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Search overlay is bounded width, centered, and not a full-width dock."""
    app, tui_conn, _ids = _run_pilot(database_connection)
    try:

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                await pilot.press("/")
                await pilot.pause()
                assert isinstance(app.screen, SearchScreen)
                dialog = app.screen.query_one("#search-dialog")
                assert dialog.region.width < app.size.width
                assert dialog.region.width <= 48
                assert dialog.region.x > 0
                assert dialog.region.y > 0
                assert dialog.region.x + dialog.region.width < app.size.width
                dock = str(dialog.styles.dock)
                assert dock in {"", "none"}
                css = MailpilotTui.CSS
                assert "align: center middle" in css
                assert "Input.search { dock: bottom" not in css

        asyncio.run(body())
    finally:
        tui_conn.close()


def test_escape_idle_table_is_noop(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Esc on an idle table does not quit or change the list."""
    from textual.widgets import DataTable

    app, tui_conn, _ids = _run_pilot(database_connection)
    try:

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                before = len(app.company_rows)
                await pilot.press("escape")
                await pilot.pause()
                assert app.is_running
                assert len(app.company_rows) == before
                assert app.focused is app.query_one("#company-table", DataTable)
                assert len(app.query("#detail-markdown")) == 0

        asyncio.run(body())
    finally:
        tui_conn.close()


def test_enter_company_markdown_includes_contacts(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Enter on a company row opens start page with Contacts DataTable extras."""
    from textual.widgets import DataTable, Markdown

    app, tui_conn, ids = _run_pilot(database_connection)
    try:

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                app._select_row("company-table", ids["company"].id)  # pyright: ignore[reportPrivateUsage]
                app.query_one("#company-table", DataTable).action_select_cursor()
                await pilot.pause()
                assert isinstance(app.screen, CompanyStartScreen)
                markdown = app.screen.query_one("#detail-markdown", Markdown)
                source = markdown.source
                assert ids["company"].name in source
                assert ids["contact"].email not in source
                assert "## Contacts" in source
                assert source.startswith(f"# {ids['company'].name}")
                assert "# Profile" not in source
                assert "```json" not in source
                assert len(app.screen.query("#detail-scroll")) == 1
                table = app.screen.query_one("#company-contacts-table", DataTable)
                emails = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
                assert ids["contact"].email in emails
                await pilot.press("escape")
                await pilot.pause()
                assert not isinstance(app.screen, CompanyStartScreen)

        asyncio.run(body())
    finally:
        tui_conn.close()


def test_enter_contact_markdown_includes_company(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Enter on a contact row opens Markdown with contact+company."""
    from textual.widgets import DataTable, Markdown, TabbedContent

    app, tui_conn, ids = _run_pilot(database_connection)
    try:

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                tabs = app.query_one("#tabs", TabbedContent)
                tabs.active = "contacts"
                await pilot.pause()
                app._select_row("contact-table", ids["contact"].id)  # pyright: ignore[reportPrivateUsage]
                app.query_one("#contact-table", DataTable).action_select_cursor()
                await pilot.pause()
                assert isinstance(app.screen, DetailScreen)
                markdown = app.screen.query_one("#detail-markdown", Markdown)
                source = markdown.source
                assert ids["contact"].email in source
                assert ids["company"].domain in source
                assert ids["company"].name in source
                assert "## Company" in source
                assert "Ada" in source or ids["contact"].email in source
                assert source.startswith("#")
                assert "```json" not in source
                await pilot.press("escape")
                await pilot.pause()
                assert not isinstance(app.screen, DetailScreen)

        asyncio.run(body())
    finally:
        tui_conn.close()


def test_format_contact_markdown_includes_company_view() -> None:
    """Contact Markdown embeds the parent CompanyView when provided."""
    from datetime import UTC, datetime

    from mailpilot.models import CompanyView, ContactView

    now = datetime(2024, 1, 1, tzinfo=UTC)
    company = CompanyView(
        id="co-1",
        name="View Co",
        domain="viewco.tui",
        profile=None,
        tags=["vip"],
        aliases=[],
        disabled_reason=None,
        notes=[],
        notes_total=0,
        created_at=now,
        updated_at=now,
    )
    row = ContactView(
        id="ct-1",
        email="ada@viewco.tui",
        company_id="co-1",
        company_domain="viewco.tui",
        first_name="Ada",
        last_name="Lovelace",
        title="VP",
        email_confidence=90,
        disabled_reason=None,
        tags=[],
        notes=[],
        notes_total=0,
        company_notes=[],
        company_notes_total=0,
        created_at=now,
        updated_at=now,
    )
    text = format_contact_markdown(row, company=company)
    assert "ada@viewco.tui" in text
    assert "View Co" in text
    assert "viewco.tui" in text
    assert "vip" in text


def test_format_profile_is_markdown_not_json_fence() -> None:
    """§V.72 / §V.195: nested profile fields are Markdown, never a JSON fence."""
    empty = format_profile(None)
    assert empty.strip() == "(no profile)"
    assert "```" not in empty
    text = format_profile(
        {
            "summary": "ERP reseller.",
            "products": ["Acumatica", "BC"],
            "target_customers": "Mid-market manufacturers.",
            "timezone": "America/Chicago",
            "sources": ["https://example.com/"],
        }
    )
    assert text.startswith("- summary: ERP reseller.\n")
    assert "- products:\n  - Acumatica\n  - BC\n" in text
    assert "- target_customers: Mid-market manufacturers.\n" in text
    assert "timezone" not in text
    assert "- sources:\n  - https://example.com/\n" in text
    assert "```json" not in text
    assert '"summary"' not in text
    missing = format_profile(
        {
            "summary": "Only summary.",
            "products": [],
            "target_customers": "",
            "timezone": None,
            "sources": [],
        }
    )
    assert "- products: (none)" in missing
    assert "- target_customers: (none)" in missing
    assert "timezone" not in missing
    assert "- sources: (none)" in missing


def test_format_company_markdown_is_document_not_record_dump() -> None:
    """§V.195: company start page is the H1/H2 outline, not a JSON dump."""
    from datetime import UTC, datetime

    from mailpilot.models import CompanyView

    now = datetime(2024, 1, 1, tzinfo=UTC)
    view = CompanyView(
        id="co-1",
        name="View Co",
        domain="viewco.tui",
        profile={
            "summary": "ERP reseller.",
            "products": ["Acumatica"],
            "target_customers": "Manufacturers.",
            "timezone": "America/Chicago",
            "sources": ["https://viewco.tui/"],
        },
        tags=["vip"],
        aliases=["alt.viewco.tui"],
        disabled_reason=None,
        notes=[],
        notes_total=0,
        created_at=now,
        updated_at=now,
    )
    text = format_company_markdown(view)
    assert text.startswith("# View Co\n")
    assert "# Profile" not in text
    assert "## Websites" in text
    assert "- [https://viewco.tui](https://viewco.tui)" in text
    assert "- [https://alt.viewco.tui](https://alt.viewco.tui)" in text
    assert "## Summary" in text
    assert "ERP reseller." in text
    assert "## Products" in text
    assert "- Acumatica" in text
    assert "## Target Customers" in text
    assert "Manufacturers." in text
    assert "## Sources" in text
    assert "- [https://viewco.tui/](https://viewco.tui/)" in text
    assert "## Contacts" in text
    assert "ada@viewco.tui" not in text
    assert "```json" not in text
    assert '"id":' not in text
    assert '"summary":' not in text
    assert "- name:" not in text
    assert "- domain:" not in text
    assert "- tags:" not in text
    assert "- aliases:" not in text
    assert "- id:" not in text
    assert "created_at" not in text
    assert "updated_at" not in text
    assert "timezone" not in text
    assert "America/Chicago" not in text
    assert "vip" not in text


def test_company_start_page_heading_order() -> None:
    """§V.195: no H1 Profile; H2 Contacts after Target Customers; Sources last."""
    from datetime import UTC, datetime

    from mailpilot.models import CompanyView, Note

    now = datetime(2024, 1, 1, tzinfo=UTC)
    view = CompanyView(
        id="co-1",
        name="View Co",
        domain="viewco.tui",
        profile={
            "summary": "ERP reseller.",
            "products": ["Acumatica"],
            "target_customers": "Manufacturers.",
            "timezone": "America/Chicago",
            "sources": ["https://viewco.tui/"],
        },
        tags=["vip"],
        aliases=["alt.viewco.tui"],
        disabled_reason=None,
        notes=[Note(id="n-1", company_id="co-1", body="keep me", created_at=now)],
        notes_total=1,
        created_at=now,
        updated_at=now,
    )
    text = format_company_markdown(view)
    headings = [line for line in text.splitlines() if line.startswith("#")]
    assert headings == [
        "# View Co",
        "## Websites",
        "## Summary",
        "## Products",
        "## Target Customers",
        "## Contacts",
        "## Notes (1 of 1, cap 10)",
        "## Sources",
    ]
    assert "# Profile" not in text


def test_company_start_page_empty_profile_keeps_notes_and_contacts() -> None:
    """§V.195: empty profile is (no profile); Notes and Contacts still emit."""
    from datetime import UTC, datetime

    from mailpilot.models import CompanyView, Note

    now = datetime(2024, 1, 1, tzinfo=UTC)
    view = CompanyView(
        id="co-1",
        name="Bare Co",
        domain="bare.tui",
        profile=None,
        tags=[],
        aliases=[],
        disabled_reason=None,
        notes=[Note(id="n-1", company_id="co-1", body="keep me", created_at=now)],
        notes_total=1,
        created_at=now,
        updated_at=now,
    )
    text = format_company_markdown(view)
    assert text.startswith("# Bare Co\n")
    assert "# Profile" not in text
    assert "(no profile)" in text
    assert "## Websites" not in text
    assert "## Summary" not in text
    assert "## Notes" in text
    assert "- keep me" in text
    assert "## Contacts" in text
    assert "## Contacts\n\n(none)\n" not in text
    assert "## Sources" not in text
    headings = [line for line in text.splitlines() if line.startswith("#")]
    assert headings == [
        "# Bare Co",
        "## Contacts",
        "## Notes (1 of 1, cap 10)",
    ]


def test_company_start_page_empty_h2_is_none() -> None:
    """§V.195: empty H2 sections render (none)."""
    from datetime import UTC, datetime

    from mailpilot.models import CompanyView

    now = datetime(2024, 1, 1, tzinfo=UTC)
    view = CompanyView(
        id="co-1",
        name="Empty Co",
        domain="empty.tui",
        profile={
            "summary": "",
            "products": [],
            "target_customers": None,
            "timezone": "America/Chicago",
            "sources": [],
        },
        tags=[],
        aliases=[],
        disabled_reason=None,
        notes=[],
        notes_total=0,
        created_at=now,
        updated_at=now,
    )
    text = format_company_markdown(view)
    assert "## Websites" in text
    assert "- [https://empty.tui](https://empty.tui)" in text
    assert "## Summary\n\n(none)\n" in text
    assert "## Products\n\n(none)\n" in text
    assert "## Target Customers\n\n(none)\n" in text
    assert "## Contacts" in text
    assert "## Contacts\n\n(none)\n" not in text
    assert "## Sources\n\n(none)\n" in text
    assert "timezone" not in text
    headings = [line for line in text.splitlines() if line.startswith("#")]
    assert headings.index("## Target Customers") < headings.index("## Contacts")
    assert headings.index("## Contacts") < headings.index("## Notes (0 of 0, cap 10)")
    assert headings[-1] == "## Sources"


def test_company_start_page_disabled_reason_under_h1() -> None:
    """§V.195: disabled_reason is one line under the name H1."""
    from datetime import UTC, datetime

    from mailpilot.models import CompanyView

    now = datetime(2024, 1, 1, tzinfo=UTC)
    view = CompanyView(
        id="co-1",
        name="Gone Co",
        domain="gone.tui",
        profile=None,
        tags=[],
        aliases=[],
        disabled_reason="retired",
        notes=[],
        notes_total=0,
        created_at=now,
        updated_at=now,
    )
    text = format_company_markdown(view)
    assert text.startswith("# Gone Co\n\nretired\n\n(no profile)\n")
    assert "# Profile" not in text
    assert "- disabled:" not in text


def test_company_start_page_sources_link_url_shaped_only() -> None:
    """§V.195: URL-shaped sources are Markdown links; others stay plain."""
    from datetime import UTC, datetime

    from mailpilot.models import CompanyView

    now = datetime(2024, 1, 1, tzinfo=UTC)
    view = CompanyView(
        id="co-1",
        name="Mix Co",
        domain="mix.tui",
        profile={
            "summary": "Mix.",
            "products": ["A"],
            "target_customers": "Buyers.",
            "sources": ["https://mix.tui/about", "LinkedIn", "http://old.mix.tui"],
        },
        tags=[],
        aliases=[],
        disabled_reason=None,
        notes=[],
        notes_total=0,
        created_at=now,
        updated_at=now,
    )
    text = format_company_markdown(view)
    assert "- [https://mix.tui/about](https://mix.tui/about)" in text
    assert "- LinkedIn" in text
    assert "- [http://old.mix.tui](http://old.mix.tui)" in text
    assert "[LinkedIn]" not in text


def test_company_contacts_datatable_columns_and_rows(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.195: Contacts DataTable cols email, name, title; row-key is contact id."""
    from textual.widgets import DataTable

    from mailpilot.database import create_contact

    company = make_test_company(database_connection, name="Seat Co", domain="seat.tui")
    contact = create_contact(
        database_connection,
        email="ada@seat.tui",
        company_id=company.id,
        first_name="Ada",
        last_name="Lovelace",
        title="VP",
    )
    assert contact is not None
    database_connection.commit()
    tui_conn = open_readonly_connection(TEST_DATABASE_URL)
    app = MailpilotTui(tui_conn)
    try:

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                app._select_row("company-table", company.id)  # pyright: ignore[reportPrivateUsage]
                app._open_detail("companies", company.id)  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()
                assert isinstance(app.screen, CompanyStartScreen)
                table = app.screen.query_one("#company-contacts-table", DataTable)
                assert [col.label.plain for col in table.columns.values()] == [
                    "email",
                    "name",
                    "title",
                ]
                assert table.row_count == 1
                assert tuple(table.get_row_at(0)) == (
                    "ada@seat.tui",
                    "Ada Lovelace",
                    "VP",
                )
                keys = [str(row.key.value) for row in table.ordered_rows]
                assert keys == [contact.id]
                source = app.screen.query_one("#detail-markdown").source  # type: ignore[attr-defined]
                assert "ada@seat.tui" not in str(source)

        asyncio.run(body())
    finally:
        tui_conn.close()


def test_company_contacts_datatable_empty_is_zero_rows(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.195: empty Contacts extras are 0 DataTable rows, not a Markdown list."""
    from textual.widgets import DataTable, Markdown

    app, tui_conn, ids = _run_pilot(database_connection)
    try:

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                app._select_row("company-table", ids["other"].id)  # pyright: ignore[reportPrivateUsage]
                app._open_detail("companies", ids["other"].id)  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()
                assert isinstance(app.screen, CompanyStartScreen)
                table = app.screen.query_one("#company-contacts-table", DataTable)
                assert table.row_count == 0
                upper = app.screen.query_one("#detail-markdown", Markdown).source
                assert "## Contacts" in upper
                assert "- " not in upper.split("## Contacts", 1)[1]
                assert "(none)" not in upper.split("## Contacts", 1)[1]

        asyncio.run(body())
    finally:
        tui_conn.close()


def test_enter_company_contact_row_opens_contact_markdown(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.195: Enter on start-page Contacts row opens contact Markdown."""
    from textual.widgets import DataTable, Markdown

    app, tui_conn, ids = _run_pilot(database_connection)
    try:

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                app._select_row("company-table", ids["company"].id)  # pyright: ignore[reportPrivateUsage]
                app._open_detail("companies", ids["company"].id)  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()
                table = app.screen.query_one("#company-contacts-table", DataTable)
                for index, row in enumerate(table.ordered_rows):
                    if str(row.key.value) == ids["contact"].id:
                        table.move_cursor(row=index)
                        table.action_select_cursor()
                        break
                await pilot.pause()
                assert isinstance(app.screen, DetailScreen)
                source = app.screen.query_one("#detail-markdown", Markdown).source
                assert ids["contact"].email in source
                assert source.startswith("#")
                assert "## Company" in source
                await pilot.press("escape")
                await pilot.pause()
                assert isinstance(app.screen, CompanyStartScreen)
                assert len(app.screen.query("#company-contacts-table")) == 1
                await pilot.press("escape")
                await pilot.pause()
                assert not isinstance(app.screen, (CompanyStartScreen, DetailScreen))
                assert app.focused is app.query_one("#company-table", DataTable)

        asyncio.run(body())
    finally:
        tui_conn.close()


def test_company_contacts_datatable_includes_disabled_extras(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.168 extras: disabled child contacts still appear as DataTable rows."""
    from textual.widgets import DataTable

    from mailpilot.database import create_contact

    company = make_test_company(
        database_connection, name="Mix Seat", domain="mixseat.tui"
    )
    live = create_contact(
        database_connection,
        email="ada@mixseat.tui",
        company_id=company.id,
        first_name="Ada",
        last_name="Lovelace",
        title="VP",
    )
    retired = create_contact(
        database_connection,
        email="bob@mixseat.tui",
        company_id=company.id,
    )
    assert live is not None
    assert retired is not None
    disable_contact(database_connection, retired.id, "retired")
    database_connection.commit()
    tui_conn = open_readonly_connection(TEST_DATABASE_URL)
    app = MailpilotTui(tui_conn)
    try:

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                app._select_row("company-table", company.id)  # pyright: ignore[reportPrivateUsage]
                app._open_detail("companies", company.id)  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()
                table = app.screen.query_one("#company-contacts-table", DataTable)
                emails = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
                assert live.email in emails
                assert retired.email in emails
                labels = [col.label.plain for col in table.columns.values()]
                assert labels == ["email", "name", "title"]
                source = str(app.screen.query_one("#detail-markdown").source)  # type: ignore[attr-defined]
                assert "disabled: retired" not in source

        asyncio.run(body())
    finally:
        tui_conn.close()


def test_detail_markdown_opens_links(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.195: company start-page Markdown widget opens links."""
    from textual.widgets import DataTable, Markdown

    app, tui_conn, ids = _run_pilot(database_connection)
    try:

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                app._select_row("company-table", ids["company"].id)  # pyright: ignore[reportPrivateUsage]
                app.query_one("#company-table", DataTable).action_select_cursor()
                await pilot.pause()
                markdown = app.screen.query_one("#detail-markdown", Markdown)
                assert markdown._open_links is True  # pyright: ignore[reportPrivateUsage]

        asyncio.run(body())
    finally:
        tui_conn.close()


def test_notes_markdown_indents_continuation_lines() -> None:
    """Multi-line notes stay one Markdown list item."""
    from datetime import UTC, datetime

    from mailpilot.models import CompanyView, Note

    now = datetime(2024, 1, 1, tzinfo=UTC)
    view = CompanyView(
        id="co-1",
        name="View Co",
        domain="viewco.tui",
        profile=None,
        tags=[],
        aliases=[],
        disabled_reason=None,
        notes=[
            Note(
                id="n-1",
                company_id="co-1",
                body="First line\nsecond line",
                created_at=now,
            )
        ],
        notes_total=1,
        created_at=now,
        updated_at=now,
    )
    text = format_company_markdown(view)
    assert "- First line\n  second line" in text


def test_format_contact_markdown_is_document_not_record_dump() -> None:
    """§V.195: contact detail is H1 + lists; company profile stays Markdown."""
    from datetime import UTC, datetime

    from mailpilot.models import CompanyView, ContactView

    now = datetime(2024, 1, 1, tzinfo=UTC)
    company = CompanyView(
        id="co-1",
        name="View Co",
        domain="viewco.tui",
        profile={
            "summary": "ERP reseller.",
            "products": ["Acumatica"],
            "target_customers": "Manufacturers.",
            "timezone": None,
            "sources": ["https://viewco.tui/"],
        },
        tags=["vip"],
        aliases=[],
        disabled_reason=None,
        notes=[],
        notes_total=0,
        created_at=now,
        updated_at=now,
    )
    row = ContactView(
        id="ct-1",
        email="ada@viewco.tui",
        company_id="co-1",
        company_domain="viewco.tui",
        first_name="Ada",
        last_name="Lovelace",
        title="VP",
        email_confidence=90,
        disabled_reason=None,
        tags=["sales-seat"],
        notes=[],
        notes_total=0,
        company_notes=[],
        company_notes_total=0,
        created_at=now,
        updated_at=now,
    )
    text = format_contact_markdown(row, company=company)
    assert text.startswith("# Ada Lovelace\n")
    assert "- email: ada@viewco.tui" in text
    assert "- title: VP" in text
    assert "- tags: sales-seat" in text
    assert "## Company" in text
    assert "### Profile" in text
    assert "\n# Profile\n" not in text
    assert "## Websites" not in text
    assert "- summary: ERP reseller." in text
    assert "timezone" not in text
    assert "```json" not in text
    assert '"email":' not in text


def test_tab_switch_focuses_master_table(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """After a tab change, Enter targets the highlighted row."""
    from textual.widgets import DataTable, TabbedContent

    app, tui_conn, _ids = _run_pilot(database_connection)
    try:

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                tabs = app.query_one("#tabs", TabbedContent)
                tabs.active = "contacts"
                await pilot.pause()
                assert app.focused is app.query_one("#contact-table", DataTable)

        asyncio.run(body())
    finally:
        tui_conn.close()


def test_q_quits_from_help(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Documented q quits from the Help overlay."""
    app, tui_conn, _ids = _run_pilot(database_connection)
    try:

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                await pilot.press("question_mark")
                await pilot.pause()
                assert isinstance(app.screen, HelpScreen)
                await pilot.press("q")
                await pilot.pause()
                assert not app.is_running

        asyncio.run(body())
    finally:
        tui_conn.close()


def test_q_quits_from_detail(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Documented q quits from the company start page."""
    from textual.widgets import DataTable

    app, tui_conn, ids = _run_pilot(database_connection)
    try:

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                app._select_row("company-table", ids["company"].id)  # pyright: ignore[reportPrivateUsage]
                app.query_one("#company-table", DataTable).action_select_cursor()
                await pilot.pause()
                assert isinstance(app.screen, CompanyStartScreen)
                await pilot.press("q")
                await pilot.pause()
                assert not app.is_running

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


class _DisabledRow:
    """Minimal row for visible_search_rows."""

    def __init__(self, name: str, disabled_reason: str | None) -> None:
        self.name = name
        self.disabled_reason = disabled_reason


def test_visible_search_rows_skips_disabled_before_display_cap() -> None:
    """Search hide-disabled runs before slicing to the display cap."""
    dead = [_DisabledRow(f"d{i}", "retired") for i in range(5)]
    live = [_DisabledRow(f"l{i}", None) for i in range(3)]
    rows, truncated = visible_search_rows(
        [*dead, *live],
        display_limit=3,
        include_disabled=False,
        fetch_limit=8,
    )
    assert [row.name for row in rows] == ["l0", "l1", "l2"]
    assert truncated is True


def test_visible_search_rows_include_disabled_keeps_order() -> None:
    """Opt-in disabled search keeps disabled rows and the fetch-cap marker."""
    dead = _DisabledRow("gone", "retired")
    live = _DisabledRow("live", None)
    rows, truncated = visible_search_rows(
        [dead, live],
        display_limit=2,
        include_disabled=True,
        fetch_limit=2,
    )
    assert [row.name for row in rows] == ["gone", "live"]
    assert truncated is True


def test_company_start_page_does_not_focus_contacts_table(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Start-page focus stays on the scroll document, not the Contacts table."""
    from textual.widgets import DataTable

    app, tui_conn, ids = _run_pilot(database_connection)
    try:

        async def body() -> None:
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                app._select_row("company-table", ids["company"].id)  # pyright: ignore[reportPrivateUsage]
                app._open_detail("companies", ids["company"].id)  # pyright: ignore[reportPrivateUsage]
                await pilot.pause()
                assert isinstance(app.screen, CompanyStartScreen)
                table = app.screen.query_one("#company-contacts-table", DataTable)
                assert app.focused is not table

        asyncio.run(body())
    finally:
        tui_conn.close()
