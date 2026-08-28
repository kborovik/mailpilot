"""Read-only Textual browser for companies and contacts."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar, Protocol

import psycopg
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
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
_PROFILE_FIELDS = (
    "summary",
    "products",
    "target_customers",
    "sources",
)

HELP_TEXT = """\
mailpilot tui -- read-only companies and contacts

/        compact centered search overlay (Enter submits; empty restores list)
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
    """Render a company profile as Markdown fields (never a JSON fence)."""
    return "\n".join(_profile_markdown_lines(profile)).rstrip() + "\n"


def _profile_markdown_lines(profile: dict[str, Any] | None) -> list[str]:
    """Markdown list lines for nested-company profile fields, or (no profile).

    Timezone is omitted from TUI documents (CLI JSON keeps it).
    """
    if profile is None:
        return ["(no profile)", ""]
    lines: list[str] = []
    for key in _PROFILE_FIELDS:
        value = profile.get(key)
        if isinstance(value, list):
            if not value:
                lines.append(f"- {key}: (none)")
            else:
                lines.append(f"- {key}:")
                lines.extend(f"  - {item}" for item in value)
        elif value in (None, ""):
            lines.append(f"- {key}: (none)")
        else:
            lines.append(f"- {key}: {value}")
    lines.append("")
    return lines


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
        parts = note.body.splitlines() or [""]
        lines.append(f"- {parts[0]}")
        lines.extend(f"  {part}" for part in parts[1:])
    lines.append("")
    return lines


def _is_url_shaped(value: str) -> bool:
    """True when a source string is an http(s) URL."""
    return value.startswith(("http://", "https://"))


def _https_url(domain: str) -> str:
    """Return ``https://{domain}`` unless the value already has a scheme."""
    if domain.startswith(("http://", "https://")):
        return domain
    return f"https://{domain}"


def _markdown_link(url: str) -> str:
    """Clickable Markdown link whose label is the URL."""
    return f"[{url}]({url})"


def _h2_block(title: str, body: list[str]) -> list[str]:
    """H2 section; empty body becomes ``(none)``."""
    lines = [f"## {title}", ""]
    if not body:
        lines.append("(none)")
    else:
        lines.extend(body)
    lines.append("")
    return lines


def _website_lines(view: CompanyView) -> list[str]:
    """Markdown-link list for the primary domain plus aliases."""
    domains = [view.domain, *view.aliases]
    return [f"- {_markdown_link(_https_url(domain))}" for domain in domains if domain]


def _source_lines(values: Any) -> list[str]:
    """List items; URL-shaped sources become Markdown links."""
    if not isinstance(values, list) or not values:
        return []
    lines: list[str] = []
    for item in values:
        text = str(item)
        if _is_url_shaped(text):
            lines.append(f"- {_markdown_link(text)}")
        else:
            lines.append(f"- {text}")
    return lines


def _paragraph_lines(value: Any) -> list[str]:
    """Non-empty paragraph, or empty so the H2 emits ``(none)``."""
    if value in (None, ""):
        return []
    return [str(value)]


def _item_lines(values: Any) -> list[str]:
    """Bullet list, or empty so the H2 emits ``(none)``."""
    if not isinstance(values, list) or not values:
        return []
    return [f"- {item}" for item in values]


def _company_start_page_markdown(view: CompanyView) -> list[str]:
    """Company start-page outline: H1 name, H1 Profile, H2 fields, H2 Notes."""
    lines = [f"# {view.name}", ""]
    if view.disabled_reason:
        lines.append(view.disabled_reason)
        lines.append("")
    lines.extend(["# Profile", ""])
    if view.profile is None:
        lines.extend(["(no profile)", ""])
    else:
        profile = view.profile
        lines.extend(_h2_block("Websites", _website_lines(view)))
        lines.extend(_h2_block("Summary", _paragraph_lines(profile.get("summary"))))
        lines.extend(_h2_block("Products", _item_lines(profile.get("products"))))
        lines.extend(
            _h2_block(
                "Target Customers",
                _paragraph_lines(profile.get("target_customers")),
            )
        )
        lines.extend(_h2_block("Sources", _source_lines(profile.get("sources"))))
    lines.extend(_notes_markdown("Notes", view.notes, view.notes_total, level=2))
    return lines


def _nested_company_markdown(view: CompanyView) -> list[str]:
    """Contact nested-company section: field list, not the start-page outline."""
    disabled = view.disabled_reason or "(enabled)"
    profile_block = _profile_markdown_lines(view.profile)
    lines = [
        "## Company",
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
        "### Profile",
        "",
        *profile_block,
    ]
    lines.extend(_notes_markdown("Notes", view.notes, view.notes_total, level=3))
    return lines


def _child_contact_line(child: dict[str, Any]) -> str:
    """One Markdown list line for a company-view --full extra contact."""
    email = str(child.get("email") or "")
    first = child.get("first_name") or ""
    last = child.get("last_name") or ""
    name = f"{first} {last}".strip()
    title = str(child.get("title") or "")
    extra = " ".join(part for part in (name, title) if part)
    reason = child.get("disabled_reason")
    if reason:
        marker = f"disabled: {reason}"
        extra = f"{extra}; {marker}" if extra else marker
    if extra:
        return f"- {email} ({extra})"
    return f"- {email}"


def format_company_markdown(
    view: CompanyView,
    *,
    child_contacts: list[dict[str, Any]] | None = None,
) -> str:
    """Render company start-page Markdown (contacts are --full extras)."""
    lines = _company_start_page_markdown(view)
    if child_contacts is not None:
        lines.extend(["# Contacts", ""])
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
        lines.extend(_nested_company_markdown(company))
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

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "app.quit", "Quit", show=False),
    ]

    def compose(self) -> ComposeResult:
        """Render help text."""
        yield Static(HELP_TEXT, id="help-body")


class SearchScreen(ModalScreen[str]):
    """Centered search overlay."""

    def __init__(self, title: str, initial: str = "") -> None:
        super().__init__()
        self._title = title
        self._initial = initial

    def compose(self) -> ComposeResult:
        """Centered dialog: title plus Input."""
        with Vertical(id="search-dialog"):
            yield Static(self._title, id="search-title")
            yield Input(value=self._initial, id="search-input")

    def on_mount(self) -> None:
        """Focus the query Input."""
        self.query_one("#search-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Return the stripped query to the app."""
        self.dismiss(event.value.strip())


class DetailScreen(ModalScreen[None]):
    """Markdown detail overlay (company+contacts or contact+company)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "app.quit", "Quit", show=False),
    ]

    def __init__(self, markdown: str) -> None:
        super().__init__()
        self._markdown = markdown

    def compose(self) -> ComposeResult:
        """Render a scrollable Markdown document."""
        with VerticalScroll(id="detail-scroll"):
            yield Markdown(self._markdown, id="detail-markdown", open_links=True)


class MailpilotTui(App[None]):
    """Read-only one-table browser for companies and contacts."""

    CSS = """
    #status { dock: bottom; height: 1; }
    TabbedContent { height: 1fr; }
    DataTable { height: 1fr; }
    #help-body { padding: 1 2; }
    #detail-scroll { height: 1fr; }
    #detail-markdown { padding: 1 2; height: auto; overflow-y: auto; }
    SearchScreen { align: center middle; }
    #search-dialog {
        width: 48;
        max-width: 90%;
        height: auto;
        padding: 1 2;
        border: solid $accent;
        background: $surface;
    }
    #search-title { width: 100%; text-align: center; padding-bottom: 1; }
    #search-input { width: 100%; }
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
        """Two tabs, one master table each, plus status."""
        with TabbedContent(id="tabs"):
            with TabPane("Companies", id="companies"):
                yield DataTable(id="company-table", cursor_type="row")
            with TabPane("Contacts", id="contacts"):
                yield DataTable(id="contact-table", cursor_type="row")
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

    def _search_visible(self) -> bool:
        """Return True when the compact search overlay is open."""
        return isinstance(self.screen, SearchScreen)

    def _search_title(self, tab: str) -> str:
        """Overlay title for the active tab."""
        return "Search companies" if tab == "companies" else "Search contacts"

    def _master_table(self, tab: str) -> DataTable[str]:
        """Return the master DataTable for a tab."""
        widget_id = "company-table" if tab == "companies" else "contact-table"
        return self.query_one(f"#{widget_id}", DataTable)

    def _clear_search_and_restore(self, tab: str) -> None:
        """Hide overlay, clear the filter, restore the list, focus the table."""
        if isinstance(self.screen, SearchScreen):
            self.pop_screen()
        self.search_query[tab] = ""
        self._reload(tab)
        self._master_table(tab).focus()

    def _apply_search(self, query: str | None) -> None:
        """Apply a submitted overlay query; empty restores the list path."""
        if query is None:
            return
        tab = self._active_tab()
        self.search_query[tab] = query
        self._reload(tab)
        self._master_table(tab).focus()

    def action_focus_search(self) -> None:
        """Show the compact centered search overlay."""
        if isinstance(self.screen, (DetailScreen, HelpScreen, SearchScreen)):
            return
        tab = self._active_tab()
        self.push_screen(
            SearchScreen(self._search_title(tab), self.search_query[tab]),
            self._apply_search,
        )

    def action_toggle_disabled(self) -> None:
        """Toggle include-disabled (default off) and reload lists."""
        if isinstance(self.focused, Input):
            return
        if isinstance(self.screen, (DetailScreen, HelpScreen, SearchScreen)):
            return
        self.include_disabled = not self.include_disabled
        self._reload("companies")
        self._reload("contacts")

    def action_refresh(self) -> None:
        """Reload the active tab list."""
        if isinstance(self.screen, (DetailScreen, HelpScreen, SearchScreen)):
            return
        self._reload(self._active_tab())

    def action_help(self) -> None:
        """Show the keybinding overlay."""
        if isinstance(self.screen, (DetailScreen, HelpScreen, SearchScreen)):
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

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        """Refresh status and move focus to the active table."""
        del event
        self._update_status()
        self._master_table(self._active_tab()).focus()

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
