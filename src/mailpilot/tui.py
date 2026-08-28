"""Read-only Textual browser for companies and contacts."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, ClassVar, Protocol

import psycopg
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Input,
    Markdown,
    Static,
    TabbedContent,
    TabPane,
)

from mailpilot.database import (
    determine_schema_verdict,
    list_companies,
    list_company_inspect_contacts,
    list_contacts,
    load_company_view,
    load_contact_view,
    search_companies,
    search_contacts,
)
from mailpilot.database.schema import _connect_database
from mailpilot.models import (
    CompanySummary,
    CompanyView,
    ContactSummary,
    ContactView,
    Note,
)
from mailpilot.settings import bootstrap_database_url

COMPANY_LIMIT = 500
CONTACT_LIMIT = 100
NOTES_CAP = 10

HELP_TEXT = """\
mailpilot tui -- read-only companies and contacts

/        show search (Enter submits; empty restores list)
d        include-disabled (default off)
r        refresh current tab
Enter    open Markdown pane (company+contacts / contact+company)
Escape   close Markdown; or hide search and clear filter;
         idle table does nothing
q        quit
?        this help

Limits: companies 500, contacts 100. Status shows truncated
when the fetch hits the limit. No paging. No writes.
"""


class TuiConnectError(Exception):
    """Schema refused; CLI maps this to the error envelope."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class _HasDisabled(Protocol):
    """Summary rows that project disabled_reason."""

    disabled_reason: str | None


def hide_disabled[T: _HasDisabled](
    rows: Sequence[T], *, include_disabled: bool
) -> list[T]:
    """Return rows, hiding disabled unless include-disabled is on.

    Args:
        rows: Summaries with ``disabled_reason``.
        include_disabled: When False, drop rows with a disabled reason.

    Returns:
        Visible rows in the same order.
    """
    if include_disabled:
        return list(rows)
    return [row for row in rows if row.disabled_reason is None]


def is_truncated(fetched_count: int, limit: int) -> bool:
    """Return True when a fetch hit the documented cap (no paging).

    Args:
        fetched_count: Number of rows returned by list/search.
        limit: Cap passed to the fetch.

    Returns:
        True when ``fetched_count`` is at least ``limit``.
    """
    return fetched_count >= limit


def contact_display_name(row: ContactSummary | ContactView) -> str:
    """Return first+last, or email when the name is empty."""
    parts = [part for part in (row.first_name, row.last_name) if part]
    if parts:
        return " ".join(parts)
    return row.email


def format_profile(profile: dict[str, Any] | None) -> str:
    """Pretty-print a company profile as JSON."""
    if profile is None:
        return "(no profile)"
    return json.dumps(profile, indent=2, ensure_ascii=False, default=str)


def _join_names(values: list[str]) -> str:
    """Comma-join names, or (none) when empty."""
    return ", ".join(values) if values else "(none)"


def _notes_markdown(
    heading: str, notes: list[Note], total: int, *, level: int = 2
) -> list[str]:
    """Markdown section for capped notes plus the true total."""
    prefix = "#" * level
    lines = [f"{prefix} {heading} ({len(notes)} of {total}, cap {NOTES_CAP})", ""]
    if not notes:
        lines.append("(none)")
        lines.append("")
        return lines
    for note in notes:
        lines.append(f"- {note.body}")
    lines.append("")
    return lines


def _company_core_markdown(
    view: CompanyView, *, heading: str | None = None
) -> list[str]:
    """Core CompanyView fields as Markdown lines (no contacts extras)."""
    disabled = view.disabled_reason or "(enabled)"
    title = heading if heading is not None else f"# {view.name}"
    sub_level = 2 if heading is None else 3
    sub = "#" * sub_level
    if view.profile is None:
        profile_block = ["(no profile)", ""]
    else:
        profile_block = ["```json", format_profile(view.profile), "```", ""]
    lines = [
        title,
        "",
        f"- name: {view.name}",
        f"- domain: {view.domain}",
        f"- id: {view.id}",
        f"- disabled: {disabled}",
        f"- tags: {_join_names(view.tags)}",
        f"- aliases: {_join_names(view.aliases)}",
        f"- created_at: {view.created_at.isoformat()}",
        f"- updated_at: {view.updated_at.isoformat()}",
        "",
        f"{sub} Profile",
        "",
        *profile_block,
    ]
    lines.extend(
        _notes_markdown("Notes", view.notes, view.notes_total, level=sub_level)
    )
    return lines


def _child_contact_line(child: dict[str, Any]) -> str:
    """One Markdown list line for a company-view --full extra contact."""
    email = str(child.get("email") or "")
    first = child.get("first_name") or ""
    last = child.get("last_name") or ""
    name = f"{first} {last}".strip()
    title = str(child.get("title") or "")
    extra = " ".join(part for part in (name, title) if part)
    if extra:
        return f"- {email} ({extra})"
    return f"- {email}"


def format_company_markdown(
    view: CompanyView,
    *,
    child_contacts: list[dict[str, Any]] | None = None,
) -> str:
    """Render company+contacts Markdown (contacts are --full extras)."""
    lines = _company_core_markdown(view)
    if child_contacts is not None:
        lines.extend(["## Contacts", ""])
        if child_contacts:
            lines.extend(_child_contact_line(child) for child in child_contacts)
        else:
            lines.append("(none)")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def format_contact_markdown(
    view: ContactView,
    *,
    company: CompanyView | None = None,
) -> str:
    """Render contact+company Markdown from shared view loaders."""
    disabled = view.disabled_reason or "(enabled)"
    confidence = (
        str(view.email_confidence) if view.email_confidence is not None else "(none)"
    )
    lines = [
        f"# {contact_display_name(view)}",
        "",
        f"- email: {view.email}",
        f"- id: {view.id}",
        f"- title: {view.title or '(none)'}",
        f"- company_domain: {view.company_domain or '(none)'}",
        f"- email_confidence: {confidence}",
        f"- disabled: {disabled}",
        f"- tags: {_join_names(view.tags)}",
        f"- created_at: {view.created_at.isoformat()}",
        f"- updated_at: {view.updated_at.isoformat()}",
        "",
    ]
    lines.extend(_notes_markdown("Notes", view.notes, view.notes_total))
    if company is not None:
        lines.extend(_company_core_markdown(company, heading="## Company"))
    else:
        lines.extend(
            [
                "## Company",
                "",
                f"- company_domain: {view.company_domain or '(none)'}",
                "",
            ]
        )
        lines.extend(
            _notes_markdown(
                "Company notes", view.company_notes, view.company_notes_total
            )
        )
    return "\n".join(lines).strip() + "\n"


def open_readonly_connection(
    database_url: str,
) -> psycopg.Connection[dict[str, Any]]:
    """Plain open, session READ ONLY, diagnose schema. Never provision.

    Args:
        database_url: Bootstrap PostgreSQL URL.

    Returns:
        Open autocommit connection whose transactions are read-only.

    Raises:
        TuiConnectError: Empty or broken schema (no auto-provision).
    """
    connection = _connect_database(database_url)
    connection.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    status = determine_schema_verdict(connection)
    if status.verdict == "current":
        return connection
    connection.close()
    if status.verdict == "pending":
        raise TuiConnectError(
            f"{status.pending} schema migration(s) pending; "
            "run 'mailpilot db migrate' -- tui does not provision",
            "schema_migration_pending",
        )
    raise TuiConnectError(
        "schema is empty or broken; run 'mailpilot db init' -- tui does not provision",
        "schema_drift",
    )


class HelpScreen(ModalScreen[None]):
    """Keybinding overlay."""

    def compose(self) -> ComposeResult:
        """Render help text."""
        yield Static(HELP_TEXT, id="help-body")


class DetailScreen(ModalScreen[None]):
    """Markdown detail overlay (company+contacts or contact+company)."""

    def __init__(self, markdown: str) -> None:
        super().__init__()
        self._markdown = markdown

    def compose(self) -> ComposeResult:
        """Render the Markdown body."""
        yield Markdown(self._markdown, id="detail-markdown", open_links=False)


class MailpilotTui(App[None]):
    """Read-only one-table browser for companies and contacts."""

    CSS = """
    #status { dock: bottom; height: 1; }
    TabbedContent { height: 1fr; }
    DataTable { height: 1fr; }
    Input.search { dock: bottom; height: 3; display: none; }
    #help-body { padding: 1 2; }
    #detail-markdown { padding: 1 2; height: 1fr; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("/", "focus_search", "Search"),
        Binding("d", "toggle_disabled", "Disabled"),
        Binding("r", "refresh", "Refresh"),
        Binding("question_mark", "help", "Help"),
        Binding("escape", "escape", "Back", show=False, priority=True),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        connection: psycopg.Connection[dict[str, Any]],
    ) -> None:
        super().__init__()
        self.connection = connection
        self.include_disabled = False
        self.search_query = {"companies": "", "contacts": ""}
        self.truncated = {"companies": False, "contacts": False}
        self.company_rows: list[CompanySummary] = []
        self.contact_rows: list[ContactSummary] = []
        self.company_view: CompanyView | None = None
        self.contact_view: ContactView | None = None
        self.company_child_contacts: list[dict[str, Any]] = []
        self.detail_markdown: str = ""

    def compose(self) -> ComposeResult:
        """Two tabs, one master table each, plus hidden search and status."""
        with TabbedContent(id="tabs"):
            with TabPane("Companies", id="companies"):
                yield DataTable(id="company-table", cursor_type="row")
            with TabPane("Contacts", id="contacts"):
                yield DataTable(id="contact-table", cursor_type="row")
        yield Input(
            placeholder="/ search companies",
            id="search",
            classes="search",
        )
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        """Load both tabs and focus the company table."""
        self._reload("companies")
        self._reload("contacts")
        self.query_one("#company-table", DataTable).focus()

    def _active_tab(self) -> str:
        """Return the active TabPane id (companies or contacts)."""
        tabs = self.query_one("#tabs", TabbedContent)
        return str(tabs.active)

    def _search_input(self) -> Input:
        """Return the on-screen search Input (hidden at rest)."""
        return self.query_one("#search", Input)

    def _search_visible(self) -> bool:
        """Return True when the search Input is on screen."""
        return bool(self._search_input().display)

    def _master_table(self, tab: str) -> DataTable[str]:
        """Return the master DataTable for a tab."""
        widget_id = "company-table" if tab == "companies" else "contact-table"
        return self.query_one(f"#{widget_id}", DataTable)

    def _hide_search(self) -> None:
        """Hide the search Input without changing the filter."""
        search = self._search_input()
        search.display = False

    def _clear_search_and_restore(self, tab: str) -> None:
        """Hide search, clear the filter, restore the list, focus the table."""
        search = self._search_input()
        search.display = False
        search.value = ""
        self.search_query[tab] = ""
        self._reload(tab)
        self._master_table(tab).focus()

    def action_focus_search(self) -> None:
        """Show the on-screen search Input and focus it."""
        if isinstance(self.screen, (DetailScreen, HelpScreen)):
            return
        tab = self._active_tab()
        search = self._search_input()
        search.display = True
        search.placeholder = f"/ search {tab}"
        search.value = self.search_query[tab]
        search.focus()

    def action_toggle_disabled(self) -> None:
        """Toggle include-disabled (default off) and reload lists."""
        if isinstance(self.focused, Input):
            return
        if isinstance(self.screen, (DetailScreen, HelpScreen)):
            return
        self.include_disabled = not self.include_disabled
        self._reload("companies")
        self._reload("contacts")

    def action_refresh(self) -> None:
        """Reload the active tab list."""
        if isinstance(self.screen, (DetailScreen, HelpScreen)):
            return
        self._reload(self._active_tab())

    def action_help(self) -> None:
        """Show the keybinding overlay."""
        if isinstance(self.screen, (DetailScreen, HelpScreen)):
            return
        self.push_screen(HelpScreen())

    def action_escape(self) -> None:
        """Contextual Esc: close Markdown, or hide search/clear filter, or no-op."""
        if isinstance(self.screen, (DetailScreen, HelpScreen)):
            self.pop_screen()
            self._master_table(self._active_tab()).focus()
            return
        tab = self._active_tab()
        if self._search_visible() or self.search_query[tab]:
            self._clear_search_and_restore(tab)
            return

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Run search on submit; empty query restores the list path."""
        if event.input.id != "search":
            return
        tab = self._active_tab()
        self.search_query[tab] = event.value.strip()
        self._reload(tab)
        self._hide_search()
        self._master_table(tab).focus()

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        """Refresh status and search placeholder when the operator switches tabs."""
        del event
        tab = self._active_tab()
        search = self._search_input()
        search.placeholder = f"/ search {tab}"
        if self._search_visible():
            search.value = self.search_query[tab]
        self._update_status()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a highlighted row opens the Markdown pane."""
        table_id = event.data_table.id
        row_key = str(event.row_key.value)
        if table_id == "company-table":
            self._open_detail("companies", row_key)
        elif table_id == "contact-table":
            self._open_detail("contacts", row_key)

    def _open_detail(self, tab: str, entity_id: str) -> None:
        """Load shared view loaders into a Markdown overlay."""
        if tab == "companies":
            view = load_company_view(self.connection, entity_id)
            self.company_view = view
            if view is None:
                self.company_child_contacts = []
                self.detail_markdown = ""
                return
            children = list_company_inspect_contacts(self.connection, view.id)
            self.company_child_contacts = children
            markdown = format_company_markdown(view, child_contacts=children)
            self.detail_markdown = markdown
            self.push_screen(DetailScreen(markdown))
            return
        view = load_contact_view(self.connection, entity_id)
        self.contact_view = view
        if view is None:
            self.detail_markdown = ""
            return
        company: CompanyView | None = None
        if view.company_id is not None:
            company = load_company_view(self.connection, view.company_id)
        markdown = format_contact_markdown(view, company=company)
        self.detail_markdown = markdown
        self.push_screen(DetailScreen(markdown))

    def _reload(self, tab: str) -> None:
        """Fetch list or search for one tab and refill the master table."""
        query = self.search_query[tab]
        if tab == "companies":
            if query:
                fetched = search_companies(self.connection, query, limit=COMPANY_LIMIT)
            else:
                fetched = list_companies(
                    self.connection,
                    limit=COMPANY_LIMIT,
                    include_disabled=self.include_disabled,
                )
            self.truncated[tab] = is_truncated(len(fetched), COMPANY_LIMIT)
            rows = hide_disabled(fetched, include_disabled=self.include_disabled)
            self.company_rows = rows
            self._fill_company_table(rows)
        else:
            if query:
                fetched = search_contacts(self.connection, query, limit=CONTACT_LIMIT)
            else:
                fetched = list_contacts(
                    self.connection,
                    limit=CONTACT_LIMIT,
                    include_disabled=self.include_disabled,
                )
            self.truncated[tab] = is_truncated(len(fetched), CONTACT_LIMIT)
            rows = hide_disabled(fetched, include_disabled=self.include_disabled)
            self.contact_rows = rows
            self._fill_contact_table(rows)
        self._update_status()

    def _fill_company_table(self, rows: list[CompanySummary]) -> None:
        """Replace company master rows."""
        table = self.query_one("#company-table", DataTable)
        table.clear(columns=True)
        headers = ["name", "domain", "profile", "contacts"]
        if self.include_disabled:
            headers.append("disabled")
        table.add_columns(*headers)
        table.cursor_type = "row"
        for row in rows:
            table.add_row(
                *_company_cells(row, include_disabled=self.include_disabled),
                key=row.id,
            )

    def _fill_contact_table(self, rows: list[ContactSummary]) -> None:
        """Replace contact master rows."""
        table = self.query_one("#contact-table", DataTable)
        table.clear(columns=True)
        headers = ["name", "email", "title", "company", "confidence"]
        if self.include_disabled:
            headers.append("disabled")
        table.add_columns(*headers)
        table.cursor_type = "row"
        for row in rows:
            table.add_row(
                *_contact_cells(row, include_disabled=self.include_disabled),
                key=row.id,
            )

    def _update_status(self) -> None:
        """Show tab, count, truncated flag, disabled toggle, search."""
        tab = self._active_tab()
        count = len(self.company_rows) if tab == "companies" else len(self.contact_rows)
        truncated = " truncated" if self.truncated[tab] else ""
        disabled = "on" if self.include_disabled else "off"
        query = self.search_query[tab]
        query_bit = f" q={query}" if query else ""
        label = "Companies" if tab == "companies" else "Contacts"
        self.query_one("#status", Static).update(
            f"{label}  {count}{truncated}  disabled={disabled}{query_bit}"
        )

    def _select_row(self, table_id: str, key: str) -> bool:
        """Move the cursor to a row key. Return True when found."""
        table = self.query_one(f"#{table_id}", DataTable)
        for index, row in enumerate(table.ordered_rows):
            if str(row.key.value) == key:
                table.move_cursor(row=index)
                table.focus()
                return True
        return False


def _cell(value: object) -> str:
    """Render a table cell as a short ASCII string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _company_cells(row: CompanySummary, *, include_disabled: bool) -> tuple[str, ...]:
    """Master-table cells for a company summary."""
    cells = (
        row.name,
        row.domain,
        _cell(row.has_profile),
        _cell(row.contact_count),
    )
    if include_disabled:
        return (*cells, row.disabled_reason or "")
    return cells


def _contact_cells(row: ContactSummary, *, include_disabled: bool) -> tuple[str, ...]:
    """Master-table cells for a contact summary."""
    cells = (
        contact_display_name(row),
        row.email,
        row.title or "",
        row.company_domain or "",
        _cell(row.email_confidence),
    )
    if include_disabled:
        return (*cells, row.disabled_reason or "")
    return cells


def run_tui() -> None:
    """Connect read-only and run the TUI until quit."""
    connection = open_readonly_connection(bootstrap_database_url())
    try:
        MailpilotTui(connection).run()
    finally:
        connection.close()
