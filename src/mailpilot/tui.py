"""Read-only Textual browser for companies and contacts."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, ClassVar, Protocol

import psycopg
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Input, Static, TabbedContent, TabPane

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

/        search (submit Enter; empty restores list)
d        include-disabled (default off)
r        refresh current tab
Enter    cross-link (company child contact -> Contacts;
         contact company_domain -> Companies)
Escape   return focus to the table
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


def _format_notes(notes: list[Note], total: int) -> str:
    """Format capped notes plus the true total."""
    lines = [f"notes ({len(notes)} of {total}, cap {NOTES_CAP}):"]
    if not notes:
        lines.append("(none)")
        return "\n".join(lines)
    for note in notes:
        lines.append(f"- {note.body}")
    return "\n".join(lines)


def format_company_detail(
    view: CompanyView,
    *,
    child_contacts: list[dict[str, Any]] | None = None,
) -> str:
    """Render core CompanyView fields; child contacts are extras."""
    disabled = view.disabled_reason or "(enabled)"
    tags = ", ".join(view.tags) if view.tags else "(none)"
    aliases = ", ".join(view.aliases) if view.aliases else "(none)"
    lines = [
        f"name: {view.name}",
        f"domain: {view.domain}",
        f"id: {view.id}",
        f"disabled: {disabled}",
        f"tags: {tags}",
        f"aliases: {aliases}",
        f"created_at: {view.created_at.isoformat()}",
        f"updated_at: {view.updated_at.isoformat()}",
        "profile:",
        format_profile(view.profile),
        _format_notes(view.notes, view.notes_total),
    ]
    if child_contacts is not None:
        lines.append(f"contacts (extra, not lean view): {len(child_contacts)}")
        for child in child_contacts:
            email = child.get("email", "")
            title = child.get("title") or ""
            lines.append(f"- {email} {title}".rstrip())
    return "\n".join(lines)


def format_contact_detail(view: ContactView) -> str:
    """Render core ContactView fields (same loader as contact view)."""
    disabled = view.disabled_reason or "(enabled)"
    tags = ", ".join(view.tags) if view.tags else "(none)"
    confidence = (
        str(view.email_confidence) if view.email_confidence is not None else "(none)"
    )
    return "\n".join(
        [
            f"name: {contact_display_name(view)}",
            f"email: {view.email}",
            f"id: {view.id}",
            f"title: {view.title or '(none)'}",
            f"company_domain: {view.company_domain or '(none)'}",
            f"email_confidence: {confidence}",
            f"disabled: {disabled}",
            f"tags: {tags}",
            f"created_at: {view.created_at.isoformat()}",
            f"updated_at: {view.updated_at.isoformat()}",
            _format_notes(view.notes, view.notes_total),
            "company_notes:",
            _format_notes(view.company_notes, view.company_notes_total),
        ]
    )


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

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close_help", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        """Render help text."""
        yield Static(HELP_TEXT, id="help-body")

    def action_close_help(self) -> None:
        """Close the overlay."""
        self.dismiss()


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


class MailpilotTui(App[None]):
    """Read-only master-detail browser for companies and contacts."""

    CSS = """
    #status { dock: bottom; height: 1; }
    TabbedContent { height: 1fr; }
    .pane { height: 1fr; }
    #company-table, #contact-table { width: 3fr; height: 1fr; }
    #company-detail-wrap, #contact-detail-wrap { width: 2fr; height: 1fr; }
    #company-child-contacts { height: 12; }
    Input.search { dock: bottom; height: 3; }
    #help-body { padding: 1 2; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("/", "focus_search", "Search"),
        Binding("d", "toggle_disabled", "Disabled"),
        Binding("r", "refresh", "Refresh"),
        Binding("question_mark", "help", "Help"),
        Binding("escape", "return_table", "Back", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        connection: psycopg.Connection[dict[str, Any]],
        *,
        detail_delay: float = 0.15,
    ) -> None:
        super().__init__()
        self.connection = connection
        self.detail_delay = detail_delay
        self.include_disabled = False
        self.search_query = {"companies": "", "contacts": ""}
        self.truncated = {"companies": False, "contacts": False}
        self.company_rows: list[CompanySummary] = []
        self.contact_rows: list[ContactSummary] = []
        self.company_view: CompanyView | None = None
        self.contact_view: ContactView | None = None
        self.company_child_contacts: list[dict[str, Any]] = []
        self._detail_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        """Two tabs, each master-detail, plus status."""
        with TabbedContent(id="tabs"):
            with TabPane("Companies", id="companies"):
                with Horizontal(classes="pane"):
                    yield DataTable(id="company-table", cursor_type="row")
                    with Vertical(id="company-detail-wrap"):
                        yield VerticalScroll(Static(id="company-detail"))
                        yield DataTable(
                            id="company-child-contacts",
                            cursor_type="row",
                        )
                yield Input(
                    placeholder="/ search companies",
                    id="company-search",
                    classes="search",
                )
            with TabPane("Contacts", id="contacts"):
                with Horizontal(classes="pane"):
                    yield DataTable(id="contact-table", cursor_type="row")
                    with Vertical(id="contact-detail-wrap"):
                        yield VerticalScroll(Static(id="contact-detail"))
                yield Input(
                    placeholder="/ search contacts",
                    id="contact-search",
                    classes="search",
                )
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        """Load both tabs and focus the company table."""
        child = self.query_one("#company-child-contacts", DataTable)
        child.add_columns("email", "name", "title")
        child.cursor_type = "row"
        self._reload("companies")
        self._reload("contacts")
        self.query_one("#company-table", DataTable).focus()

    def _active_tab(self) -> str:
        """Return the active TabPane id (companies or contacts)."""
        tabs = self.query_one("#tabs", TabbedContent)
        return str(tabs.active)

    def _search_input(self, tab: str) -> Input:
        """Return the search Input for a tab."""
        widget_id = "company-search" if tab == "companies" else "contact-search"
        return self.query_one(f"#{widget_id}", Input)

    def _master_table(self, tab: str) -> DataTable[str]:
        """Return the master DataTable for a tab."""
        widget_id = "company-table" if tab == "companies" else "contact-table"
        return self.query_one(f"#{widget_id}", DataTable)

    def action_focus_search(self) -> None:
        """Focus the search input for the active tab."""
        self._search_input(self._active_tab()).focus()

    def action_toggle_disabled(self) -> None:
        """Toggle include-disabled (default off) and reload lists."""
        focused = self.focused
        if isinstance(focused, Input):
            return
        self.include_disabled = not self.include_disabled
        self._reload("companies")
        self._reload("contacts")

    def action_refresh(self) -> None:
        """Reload the active tab list and focused detail."""
        self._reload(self._active_tab())

    def action_help(self) -> None:
        """Show the keybinding overlay."""
        self.push_screen(HelpScreen())

    def action_return_table(self) -> None:
        """Clear search focus or return focus to the table."""
        self._master_table(self._active_tab()).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Run search on submit; empty query restores the list path."""
        tab = "companies" if event.input.id == "company-search" else "contacts"
        self.search_query[tab] = event.value.strip()
        self._reload(tab)
        self._master_table(tab).focus()

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        """Refresh status when the operator switches tabs."""
        del event
        self._update_status()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Cursor motion updates the detail pane (debounced)."""
        table_id = event.data_table.id
        row_key = str(event.row_key.value)
        if table_id == "company-table":
            self._schedule_detail("companies", row_key)
        elif table_id == "contact-table":
            self._schedule_detail("contacts", row_key)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter cross-links company child contacts and contact domains."""
        table_id = event.data_table.id
        row_key = str(event.row_key.value)
        if table_id == "company-child-contacts":
            self.cross_link_to_contact(row_key)
        elif table_id == "contact-table":
            self.cross_link_to_company(row_key)

    def _schedule_detail(self, tab: str, entity_id: str) -> None:
        """Debounce load_*_view on rapid cursor motion."""
        if self._detail_timer is not None:
            self._detail_timer.stop()
            self._detail_timer = None
        if self.detail_delay <= 0:
            self._load_detail(tab, entity_id)
            return
        self._detail_timer = self.set_timer(
            self.detail_delay,
            lambda: self._load_detail(tab, entity_id),
        )

    def _load_detail(self, tab: str, entity_id: str) -> None:
        """Load shared view loaders into the right pane."""
        if tab == "companies":
            view = load_company_view(self.connection, entity_id)
            self.company_view = view
            if view is None:
                self.company_child_contacts = []
                self.query_one("#company-detail", Static).update("(not found)")
                self._fill_child_contacts([])
                return
            children = list_company_inspect_contacts(self.connection, view.id)
            self.company_child_contacts = children
            self.query_one("#company-detail", Static).update(
                format_company_detail(view, child_contacts=children)
            )
            self._fill_child_contacts(children)
            return
        view = load_contact_view(self.connection, entity_id)
        self.contact_view = view
        if view is None:
            self.query_one("#contact-detail", Static).update("(not found)")
            return
        self.query_one("#contact-detail", Static).update(format_contact_detail(view))

    def _fill_child_contacts(self, children: list[dict[str, Any]]) -> None:
        """Fill the company-detail extra contact table."""
        table = self.query_one("#company-child-contacts", DataTable)
        table.clear()
        for child in children:
            email = str(child.get("email") or "")
            first = child.get("first_name") or ""
            last = child.get("last_name") or ""
            name = f"{first} {last}".strip() or email
            table.add_row(
                email,
                name,
                str(child.get("title") or ""),
                key=str(child.get("id") or email),
            )

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
        self._load_first_detail(tab)

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

    def _load_first_detail(self, tab: str) -> None:
        """Load detail for the first master row when present."""
        rows: Sequence[CompanySummary | ContactSummary]
        rows = self.company_rows if tab == "companies" else self.contact_rows
        if not rows:
            if tab == "companies":
                self.company_view = None
                self.company_child_contacts = []
                self.query_one("#company-detail", Static).update("(no rows)")
                self._fill_child_contacts([])
            else:
                self.contact_view = None
                self.query_one("#contact-detail", Static).update("(no rows)")
            return
        self._load_detail(tab, rows[0].id)

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

    def cross_link_to_contact(self, contact_id: str) -> None:
        """Focus a contact in the Contacts tab (Enter from company extras)."""
        email = ""
        for child in self.company_child_contacts:
            if str(child.get("id")) == contact_id:
                email = str(child.get("email") or "")
                break
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.active = "contacts"
        if not any(row.id == contact_id for row in self.contact_rows) and email:
            self.search_query["contacts"] = email
            self._search_input("contacts").value = email
            self._reload("contacts")
        if self._select_row("contact-table", contact_id):
            self._load_detail("contacts", contact_id)
        self._update_status()

    def cross_link_to_company(self, contact_id: str) -> None:
        """Focus the parent company in the Companies tab."""
        domain: str | None = None
        for row in self.contact_rows:
            if row.id == contact_id:
                domain = row.company_domain
                break
        if domain is None and self.contact_view is not None:
            domain = self.contact_view.company_domain
        if not domain:
            return
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.active = "companies"
        match = next((row for row in self.company_rows if row.domain == domain), None)
        if match is None:
            self.search_query["companies"] = domain
            self._search_input("companies").value = domain
            self._reload("companies")
            match = next(
                (row for row in self.company_rows if row.domain == domain),
                None,
            )
        if match is None:
            return
        if self._select_row("company-table", match.id):
            self._load_detail("companies", match.id)
        self._update_status()


def run_tui() -> None:
    """Connect read-only and run the TUI until quit."""
    connection = open_readonly_connection(bootstrap_database_url())
    try:
        MailpilotTui(connection).run()
    finally:
        connection.close()
