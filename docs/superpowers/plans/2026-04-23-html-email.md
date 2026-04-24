# HTML Email Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert agent-generated Markdown email bodies into themed HTML emails with tables, readable fonts, and workflow-based color schemes.

**Architecture:** `sync.send_email()` converts Markdown to HTML via a custom mistune renderer with inline styles, strips Markdown to plain text for DB storage, and sends `multipart/alternative` MIME. `GmailClient.send_message()` changes to accept a pre-built MIME message. A `theme` column on the workflow table selects one of 6 predefined color palettes.

**Tech Stack:** mistune (Markdown parser), Python email.mime (MIME construction), dataclasses (EmailTheme)

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `src/mailpilot/email_renderer.py` | Create | `EmailTheme`, `THEMES`, `EmailRenderer`, `PlainTextRenderer`, `render_email_html()`, `strip_markdown()` |
| `tests/test_email_renderer.py` | Create | Unit tests for rendering and stripping |
| `src/mailpilot/schema.sql` | Modify | Add `theme` column to `workflow` table |
| `src/mailpilot/models.py` | Modify | Add `theme` field to `Workflow` model |
| `src/mailpilot/database.py` | Modify | Accept `theme` in `create_workflow()` and `update_workflow()` |
| `tests/test_cli.py` | Modify | Update workflow create/update tests for `--theme` |
| `src/mailpilot/cli.py` | Modify | Add `--theme` option to workflow create/update |
| `src/mailpilot/gmail.py` | Modify | Change `send_message()` to accept `MIMEBase` |
| `tests/test_sync.py` | Modify | Update all `send_email` tests for MIME changes |
| `src/mailpilot/sync.py` | Modify | Build multipart MIME, render HTML, strip to plain text |
| `tests/test_agent_tools.py` | Modify | Verify agent tools pass body correctly through new pipeline |

---

### Task 1: Add mistune dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add mistune to dependencies**

```toml
# In [project] dependencies, add after the last entry:
    "mistune>=3.1.0,<4.0.0",
```

- [ ] **Step 2: Install and verify**

Run: `uv sync`
Expected: Clean install, no conflicts.

Run: `uv run python -c "import mistune; print(mistune.__version__)"`
Expected: Prints version >= 3.1.0

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add mistune dependency for Markdown email rendering"
```

---

### Task 2: EmailTheme and renderers

**Files:**
- Create: `src/mailpilot/email_renderer.py`
- Create: `tests/test_email_renderer.py`

- [ ] **Step 1: Write failing tests for EmailTheme and THEMES**

```python
# tests/test_email_renderer.py
"""Tests for Markdown-to-HTML email rendering."""

from mailpilot.email_renderer import (
    EmailTheme,
    THEMES,
    get_theme,
)


def test_themes_contains_six_palettes():
    assert len(THEMES) == 6
    assert set(THEMES.keys()) == {"blue", "green", "orange", "purple", "red", "slate"}


def test_each_theme_has_three_hex_colors():
    for name, theme in THEMES.items():
        for field in ("primary", "accent", "border"):
            value = getattr(theme, field)
            assert value.startswith("#"), f"{name}.{field} = {value}"
            assert len(value) == 7, f"{name}.{field} = {value}"


def test_get_theme_returns_named_theme():
    theme = get_theme("green")
    assert theme.primary == "#16a34a"


def test_get_theme_falls_back_to_blue():
    theme = get_theme("nonexistent")
    assert theme == THEMES["blue"]


def test_get_theme_none_falls_back_to_blue():
    theme = get_theme(None)
    assert theme == THEMES["blue"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_email_renderer.py -v`
Expected: FAIL with import errors.

- [ ] **Step 3: Implement EmailTheme and THEMES**

```python
# src/mailpilot/email_renderer.py
"""Markdown-to-HTML email rendering with inline styles and theme support."""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class EmailTheme:
    """Color palette for themed email rendering."""

    primary: str  # headings, links
    accent: str  # table header background
    border: str  # table/hr borders


THEMES: dict[str, EmailTheme] = {
    "blue": EmailTheme(primary="#2563eb", accent="#dbeafe", border="#bfdbfe"),
    "green": EmailTheme(primary="#16a34a", accent="#dcfce7", border="#bbf7d0"),
    "orange": EmailTheme(primary="#ea580c", accent="#ffedd5", border="#fed7aa"),
    "purple": EmailTheme(primary="#7c3aed", accent="#ede9fe", border="#ddd6fe"),
    "red": EmailTheme(primary="#dc2626", accent="#fee2e2", border="#fecaca"),
    "slate": EmailTheme(primary="#475569", accent="#f1f5f9", border="#e2e8f0"),
}

THEME_NAMES: set[str] = set(THEMES.keys())

DEFAULT_THEME = "blue"


def get_theme(name: str | None) -> EmailTheme:
    """Look up a theme by name, falling back to blue."""
    if name is None:
        return THEMES[DEFAULT_THEME]
    return THEMES.get(name, THEMES[DEFAULT_THEME])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_email_renderer.py -v`
Expected: All PASS.

- [ ] **Step 5: Write failing tests for EmailRenderer (render_email_html)**

Add to `tests/test_email_renderer.py`:

```python
from mailpilot.email_renderer import render_email_html


def test_render_wraps_in_container_div():
    html = render_email_html("Hello", get_theme("blue"))
    assert html.startswith('<div style="')
    assert "max-width:600px" in html
    assert html.endswith("</div>")


def test_render_heading_uses_primary_color():
    theme = get_theme("blue")
    html = render_email_html("## Title", theme)
    assert f"color:{theme.primary}" in html
    assert "<h2" in html
    assert "Title" in html


def test_render_paragraph_uses_body_styles():
    html = render_email_html("Hello world", get_theme("blue"))
    assert "<p" in html
    assert "font-size:16px" in html


def test_render_table_with_themed_headers():
    md = "| Model | Flow |\n|-------|------|\n| WS48 | 150 |"
    theme = get_theme("green")
    html = render_email_html(md, theme)
    assert "<table" in html
    assert "<th" in html
    assert f"background-color:{theme.accent}" in html
    assert "WS48" in html
    assert "150" in html


def test_render_link_uses_primary_color():
    theme = get_theme("orange")
    html = render_email_html("[Lab5](https://lab5.ca)", theme)
    assert f"color:{theme.primary}" in html
    assert 'href="https://lab5.ca"' in html


def test_render_bold_and_italic():
    html = render_email_html("**bold** and *italic*", get_theme("blue"))
    assert "<strong>" in html
    assert "<em>" in html


def test_render_unordered_list():
    html = render_email_html("- one\n- two", get_theme("blue"))
    assert "<ul" in html
    assert "<li" in html


def test_render_ordered_list():
    html = render_email_html("1. first\n2. second", get_theme("blue"))
    assert "<ol" in html


def test_render_horizontal_rule():
    theme = get_theme("blue")
    html = render_email_html("---", theme)
    assert "<hr" in html
    assert f"border-top:1px solid {theme.border}" in html


def test_render_inline_code():
    html = render_email_html("Use `cmd` here", get_theme("blue"))
    assert "<code" in html
    assert "cmd" in html
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `uv run pytest tests/test_email_renderer.py -v`
Expected: FAIL with `ImportError` for `render_email_html`.

- [ ] **Step 7: Implement EmailRenderer and render_email_html**

Add to `src/mailpilot/email_renderer.py`:

```python
import mistune


class EmailRenderer(mistune.HTMLRenderer):  # type: ignore[misc]
    """Custom HTML renderer that injects inline styles for email clients."""

    def __init__(self, theme: EmailTheme) -> None:
        super().__init__()
        self.theme = theme

    def heading(self, text: str, level: int, **attrs: object) -> str:
        sizes = {1: "24px", 2: "20px", 3: "18px"}
        size = sizes.get(level, "16px")
        color = self.theme.primary if level <= 3 else "#333333"
        style = (
            f"color:{color}; font-size:{size}; font-weight:bold; "
            f"line-height:1.3; margin:16px 0 8px 0"
        )
        return f'<h{level} style="{style}">{text}</h{level}>\n'

    def paragraph(self, text: str) -> str:
        style = "font-size:16px; line-height:1.5; color:#333333; margin:0 0 16px 0"
        return f'<p style="{style}">{text}</p>\n'

    def link(self, text: str, url: str, title: str | None = None) -> str:
        style = f"color:{self.theme.primary}; text-decoration:underline"
        title_attr = f' title="{title}"' if title else ""
        return f'<a href="{url}" style="{style}"{title_attr}>{text}</a>'

    def strong(self, text: str) -> str:
        return f"<strong>{text}</strong>"

    def emphasis(self, text: str) -> str:
        return f"<em>{text}</em>"

    def codespan(self, text: str) -> str:
        style = (
            "background-color:#f3f4f6; padding:2px 6px; border-radius:3px; "
            "font-family:'Courier New',Courier,monospace; font-size:14px"
        )
        return f'<code style="{style}">{text}</code>'

    def thematic_break(self) -> str:
        style = (
            f"border:none; border-top:1px solid {self.theme.border}; "
            f"margin:24px 0"
        )
        return f'<hr style="{style}">\n'

    def list(self, text: str, ordered: bool, **attrs: object) -> str:
        tag = "ol" if ordered else "ul"
        style = "padding-left:24px; margin:0 0 16px 0; font-size:16px; line-height:1.5; color:#333333"
        return f'<{tag} style="{style}">{text}</{tag}>\n'

    def list_item(self, text: str, **attrs: object) -> str:
        return f'<li style="margin:4px 0">{text}</li>\n'

    def table(self, text: str) -> str:
        style = "width:100%; border-collapse:collapse; font-size:14px; margin:0 0 16px 0"
        return f'<table style="{style}">{text}</table>\n'

    def table_head(self, text: str) -> str:
        return f"<thead>{text}</thead>\n"

    def table_body(self, text: str) -> str:
        return f"<tbody>{text}</tbody>\n"

    def table_row(self, text: str) -> str:
        return f"<tr>{text}</tr>\n"

    def table_cell(
        self, text: str, head: bool = False, align: str | None = None, **attrs: object
    ) -> str:
        tag = "th" if head else "td"
        text_align = f"text-align:{align}; " if align else "text-align:left; "
        if head:
            style = (
                f"background-color:{self.theme.accent}; color:#1a1a1a; "
                f"font-weight:bold; padding:8px 12px; {text_align}"
                f"border-bottom:2px solid {self.theme.border}"
            )
        else:
            style = (
                f"padding:8px 12px; {text_align}"
                f"border-bottom:1px solid {self.theme.border}"
            )
        return f'<{tag} style="{style}">{text}</{tag}>\n'


_CONTAINER_STYLE = (
    "font-family:Arial,'Helvetica Neue',Helvetica,sans-serif; "
    "font-size:16px; line-height:1.5; color:#333333; max-width:600px"
)


def render_email_html(markdown_body: str, theme: EmailTheme) -> str:
    """Convert Markdown to email-safe HTML with inline styles.

    Args:
        markdown_body: Markdown source from the LLM agent.
        theme: Color palette for headings, tables, links.

    Returns:
        Complete HTML string with inline styles, wrapped in a container div.
    """
    renderer = EmailRenderer(theme)
    md = mistune.create_markdown(renderer=renderer, plugins=["table"])
    content = md(markdown_body)
    return f'<div style="{_CONTAINER_STYLE}">{content}</div>'
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest tests/test_email_renderer.py -v`
Expected: All PASS.

- [ ] **Step 9: Write failing tests for PlainTextRenderer (strip_markdown)**

Add to `tests/test_email_renderer.py`:

```python
from mailpilot.email_renderer import strip_markdown


def test_strip_removes_bold_markers():
    assert "bold" in strip_markdown("**bold** text")
    assert "**" not in strip_markdown("**bold** text")


def test_strip_removes_italic_markers():
    assert "italic" in strip_markdown("*italic* text")
    result = strip_markdown("*italic* text")
    assert result.startswith("italic")


def test_strip_removes_heading_markers():
    result = strip_markdown("## Title\n\nBody")
    assert "##" not in result
    assert "TITLE" in result  # uppercased


def test_strip_renders_link_with_url():
    result = strip_markdown("[Lab5](https://lab5.ca)")
    assert "Lab5" in result
    assert "https://lab5.ca" in result


def test_strip_renders_table_rows():
    md = "| Model | Flow |\n|-------|------|\n| WS48 | 150 |"
    result = strip_markdown(md)
    assert "Model" in result
    assert "WS48" in result
    assert "150" in result


def test_strip_removes_hr_markers():
    result = strip_markdown("above\n\n---\n\nbelow")
    assert "---" not in result
    assert "above" in result
    assert "below" in result


def test_strip_removes_code_backticks():
    result = strip_markdown("Use `cmd` here")
    assert "`" not in result
    assert "cmd" in result


def test_strip_preserves_list_format():
    result = strip_markdown("- one\n- two")
    assert "one" in result
    assert "two" in result
```

- [ ] **Step 10: Run tests to verify they fail**

Run: `uv run pytest tests/test_email_renderer.py::test_strip_removes_bold_markers -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 11: Implement PlainTextRenderer and strip_markdown**

Add to `src/mailpilot/email_renderer.py`:

```python
class PlainTextRenderer(mistune.HTMLRenderer):  # type: ignore[misc]
    """Renderer that outputs clean plain text with no Markdown syntax."""

    def text(self, text: str) -> str:
        return text

    def heading(self, text: str, level: int, **attrs: object) -> str:
        return f"\n{text.upper()}\n\n"

    def paragraph(self, text: str) -> str:
        return f"{text}\n\n"

    def link(self, text: str, url: str, title: str | None = None) -> str:
        if text == url:
            return url
        return f"{text} ({url})"

    def strong(self, text: str) -> str:
        return text

    def emphasis(self, text: str) -> str:
        return text

    def codespan(self, text: str) -> str:
        return text

    def thematic_break(self) -> str:
        return "\n"

    def list(self, text: str, ordered: bool, **attrs: object) -> str:
        return f"{text}\n"

    def list_item(self, text: str, **attrs: object) -> str:
        return f"- {text}\n"

    def table(self, text: str) -> str:
        return f"{text}\n"

    def table_head(self, text: str) -> str:
        return text

    def table_body(self, text: str) -> str:
        return text

    def table_row(self, text: str) -> str:
        return f"{text}\n"

    def table_cell(
        self, text: str, head: bool = False, align: str | None = None, **attrs: object
    ) -> str:
        return f"{text}\t"

    def linebreak(self) -> str:
        return "\n"


def strip_markdown(markdown_body: str) -> str:
    """Convert Markdown to clean plain text with no formatting syntax.

    Args:
        markdown_body: Markdown source from the LLM agent.

    Returns:
        Plain text with headings uppercased, links expanded, tables
        tab-separated, and all Markdown markers removed.
    """
    renderer = PlainTextRenderer()
    md = mistune.create_markdown(renderer=renderer, plugins=["table"])
    return md(markdown_body).strip()
```

- [ ] **Step 12: Run tests to verify they pass**

Run: `uv run pytest tests/test_email_renderer.py -v`
Expected: All PASS.

- [ ] **Step 13: Run lint and type check**

Run: `uv run ruff check --fix && uv run basedpyright`
Expected: Clean.

- [ ] **Step 14: Commit**

```bash
git add src/mailpilot/email_renderer.py tests/test_email_renderer.py
git commit -m "feat(email): add Markdown-to-HTML renderer with theme support"
```

---

### Task 3: Add theme column to workflow

**Files:**
- Modify: `src/mailpilot/schema.sql:51-63`
- Modify: `src/mailpilot/models.py:70-82`
- Modify: `src/mailpilot/database.py:731-763` (create_workflow)
- Modify: `src/mailpilot/database.py:850-879` (update_workflow)
- Modify: `tests/conftest.py:114-126` (make_test_workflow)

- [ ] **Step 1: Write failing test for workflow theme in DB**

Add a new test file `tests/test_workflow_theme.py`:

```python
"""Tests for workflow theme support."""

from typing import Any

import psycopg

from mailpilot.database import create_workflow, get_workflow, update_workflow
from tests.conftest import make_test_account


def test_create_workflow_default_theme(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = create_workflow(
        database_connection, name="Test", workflow_type="outbound", account_id=account.id
    )
    assert workflow.theme == "blue"


def test_create_workflow_custom_theme(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = create_workflow(
        database_connection,
        name="Themed",
        workflow_type="outbound",
        account_id=account.id,
        theme="green",
    )
    assert workflow.theme == "green"


def test_update_workflow_theme(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = create_workflow(
        database_connection, name="W", workflow_type="outbound", account_id=account.id
    )
    updated = update_workflow(database_connection, workflow.id, theme="orange")
    assert updated is not None
    assert updated.theme == "orange"


def test_get_workflow_includes_theme(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    created = create_workflow(
        database_connection,
        name="Get",
        workflow_type="inbound",
        account_id=account.id,
        theme="purple",
    )
    fetched = get_workflow(database_connection, created.id)
    assert fetched is not None
    assert fetched.theme == "purple"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_workflow_theme.py -v`
Expected: FAIL -- `Workflow` model has no `theme` field.

- [ ] **Step 3: Add theme column to schema.sql**

In `src/mailpilot/schema.sql`, add after the `instructions` line in the workflow table:

```sql
    theme             TEXT NOT NULL DEFAULT 'blue',
```

- [ ] **Step 4: Add theme field to Workflow model**

In `src/mailpilot/models.py`, add to the `Workflow` class after `instructions`:

```python
    theme: str = "blue"
```

- [ ] **Step 5: Update create_workflow to accept theme**

In `src/mailpilot/database.py`, modify `create_workflow`:

```python
def create_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    workflow_type: str,
    account_id: str,
    theme: str = "blue",
) -> Workflow:
```

Update the INSERT query:

```python
    row = connection.execute(
        """\
        INSERT INTO workflow (id, name, type, account_id, theme)
        VALUES (%(id)s, %(name)s, %(type)s, %(account_id)s, %(theme)s)
        RETURNING *
        """,
        {
            "id": _new_id(),
            "name": name,
            "type": workflow_type,
            "account_id": account_id,
            "theme": theme,
        },
    ).fetchone()
```

- [ ] **Step 6: Update update_workflow to allow theme**

In `src/mailpilot/database.py`, modify `update_workflow` to add `"theme"` to the `allowed` set:

```python
    allowed = {"name", "objective", "instructions", "theme"}
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_workflow_theme.py -v`
Expected: All PASS.

- [ ] **Step 8: Run full test suite to check for regressions**

Run: `uv run pytest -x`
Expected: All PASS. Existing tests should not break because `theme` defaults to `"blue"`.

- [ ] **Step 9: Run lint and type check**

Run: `uv run ruff check --fix && uv run basedpyright`
Expected: Clean.

- [ ] **Step 10: Commit**

```bash
git add src/mailpilot/schema.sql src/mailpilot/models.py src/mailpilot/database.py tests/test_workflow_theme.py
git commit -m "feat(schema): add theme column to workflow table"
```

---

### Task 4: CLI --theme option

**Files:**
- Modify: `src/mailpilot/cli.py:1260-1345` (workflow create)
- Modify: `src/mailpilot/cli.py:1364-1400` (workflow update)
- Modify: `tests/test_cli.py` (workflow create/update tests)

- [ ] **Step 1: Write failing test for workflow create --theme**

Add to `tests/test_cli.py` near the other workflow create tests:

```python
def test_workflow_create_with_theme(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    mock_connection.execute.return_value.fetchone.side_effect = [
        _MOCK_ACCOUNT_ROW,  # get_account
        {**_MOCK_WORKFLOW_ROW, "theme": "green"},  # create_workflow
        {**_MOCK_WORKFLOW_ROW, "theme": "green"},  # activate_workflow
    ]
    result = runner.invoke(
        main,
        [
            "workflow", "create",
            "--name", "Themed",
            "--type", "outbound",
            "--account-id", MOCK_ID,
            "--theme", "green",
        ],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["theme"] == "green"


def test_workflow_create_invalid_theme(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    mock_connection.execute.return_value.fetchone.return_value = _MOCK_ACCOUNT_ROW
    result = runner.invoke(
        main,
        [
            "workflow", "create",
            "--name", "Bad",
            "--type", "outbound",
            "--account-id", MOCK_ID,
            "--theme", "rainbow",
        ],
    )
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py::test_workflow_create_with_theme -v`
Expected: FAIL.

- [ ] **Step 3: Add --theme option to workflow create in cli.py**

Add a new `@click.option` before the `workflow_create` function, after the `--draft` option:

```python
@click.option(
    "--theme",
    default=None,
    help="Email color theme (blue, green, orange, purple, red, slate).",
)
```

Add `theme: str | None` to the function signature.

Inside the function body, after the empty-name validation, add theme validation:

```python
    if theme is not None:
        from mailpilot.email_renderer import THEME_NAMES

        if theme not in THEME_NAMES:
            output_error(
                f"invalid theme '{theme}', must be one of: {', '.join(sorted(THEME_NAMES))}",
                "validation_error",
            )
```

Pass `theme` to `create_workflow()`:

```python
    workflow = create_workflow(
        connection,
        name=name,
        workflow_type=workflow_type,
        account_id=account_id,
        theme=theme or "blue",
    )
```

- [ ] **Step 4: Add --theme option to workflow update in cli.py**

Add the same `@click.option` before `workflow_update`.

Add `theme: str | None` to the function signature.

Inside the function body, add theme validation and field assignment:

```python
    if theme is not None:
        from mailpilot.email_renderer import THEME_NAMES

        if theme not in THEME_NAMES:
            output_error(
                f"invalid theme '{theme}', must be one of: {', '.join(sorted(THEME_NAMES))}",
                "validation_error",
            )
        fields["theme"] = theme
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -k "workflow_create_with_theme or workflow_create_invalid_theme" -v`
Expected: All PASS.

- [ ] **Step 6: Run full test suite**

Run: `uv run pytest -x`
Expected: All PASS. Existing workflow tests should not break -- `theme` is optional and defaults.

- [ ] **Step 7: Run lint and type check**

Run: `uv run ruff check --fix && uv run basedpyright`
Expected: Clean.

- [ ] **Step 8: Commit**

```bash
git add src/mailpilot/cli.py tests/test_cli.py
git commit -m "feat(cli): add --theme option to workflow create/update"
```

---

### Task 5: Change GmailClient.send_message() to accept MIMEBase

**Files:**
- Modify: `src/mailpilot/gmail.py:264-324`
- Modify: `tests/test_sync.py` (all send_email tests that inspect MIME)

- [ ] **Step 1: Write failing test for new send_message signature**

Add to `tests/test_sync.py`:

```python
def test_send_message_accepts_mime_message(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """GmailClient.send_message() accepts a pre-built MIMEBase message."""
    from email.mime.text import MIMEText as MIMETextClass

    account = make_test_account(database_connection, email="mime@example.com")
    client, service = _make_send_client(account.email)

    mime_msg = MIMETextClass("Hello", _charset="utf-8")
    client.send_message(
        message=mime_msg,
        to="recipient@example.com",
        subject="Test",
        from_email=account.email,
        account_id=account.id,
    )

    send_call = service.users.return_value.messages.return_value.send
    assert send_call.call_count == 1
    call_body = send_call.call_args.kwargs["body"]
    raw = base64.urlsafe_b64decode(call_body["raw"])
    msg = message_from_bytes(raw)
    assert msg["To"] == "recipient@example.com"
    assert msg["Subject"] == "Test"
    assert msg["X-MailPilot-Version"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_sync.py::test_send_message_accepts_mime_message -v`
Expected: FAIL -- `send_message()` does not have a `message` parameter.

- [ ] **Step 3: Refactor GmailClient.send_message()**

Replace the `send_message` method in `src/mailpilot/gmail.py`. The new version accepts a `message: MIMEBase` parameter instead of `body: str`. It adds headers to the pre-built MIME message and sends it.

```python
    @_retry_on_transient
    def send_message(
        self,
        message: MIMEBase,
        to: str,
        subject: str,
        from_email: str = "",
        thread_id: str | None = None,
        account_id: str = "",
        cc: str | None = None,
        bcc: str | None = None,
        in_reply_to: str | None = None,
        user_id: str = "me",
    ) -> dict[str, Any]:
        """Send an email message via Gmail API.

        Args:
            message: Pre-built MIME message (text/plain, text/html,
                or multipart/alternative).
            to: Recipient email address(es), comma-separated for multiple.
            subject: Email subject.
            from_email: Sender email (for From header).
            thread_id: Gmail thread ID for threading replies.
            account_id: MailPilot account ID for traceability header.
            cc: CC recipient(s), comma-separated.
            bcc: BCC recipient(s), comma-separated.
            in_reply_to: RFC 2822 Message-ID of the email being replied to.
                Sets In-Reply-To and References headers for cross-client
                thread grouping.
            user_id: Gmail user ID.

        Returns:
            Sent message dict with id, threadId, labelIds.
        """
        message["To"] = to
        message["Subject"] = subject
        if from_email:
            message["From"] = from_email
        if cc:
            message["Cc"] = cc
        if bcc:
            message["Bcc"] = bcc
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
            message["References"] = in_reply_to
        message["X-MailPilot-Version"] = _MAILPILOT_VERSION
        if account_id:
            message["X-MailPilot-Account-Id"] = account_id

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        send_body: dict[str, Any] = {"raw": raw}
        if thread_id:
            send_body["threadId"] = thread_id

        result: dict[str, Any] = (
            self._service.users()
            .messages()
            .send(userId=user_id, body=send_body)
            .execute()
        )
        return result
```

Update the imports at the top of `gmail.py` -- add `from email.mime.base import MIMEBase`. Remove the `from email.mime.text import MIMEText` import if it's no longer used elsewhere in the file (check first).

- [ ] **Step 4: Run new test to verify it passes**

Run: `uv run pytest tests/test_sync.py::test_send_message_accepts_mime_message -v`
Expected: PASS.

- [ ] **Step 5: Fix existing sync tests**

The existing `send_email` tests in `test_sync.py` call `sync.send_email()` which still builds the MIME message itself (will be updated in Task 6). For now, existing tests will break because `sync.send_email()` still passes `body=` to `GmailClient.send_message()`. This is expected -- they will be fixed in Task 6.

Run: `uv run ruff check --fix && uv run basedpyright`
Expected: Type check clean on gmail.py changes. Some test failures expected until Task 6.

- [ ] **Step 6: Commit**

```bash
git add src/mailpilot/gmail.py
git commit -m "refactor(gmail): accept pre-built MIMEBase in send_message"
```

---

### Task 6: Update sync.send_email() to build multipart MIME

**Files:**
- Modify: `src/mailpilot/sync.py:475-560`
- Modify: `tests/test_sync.py` (update existing send_email tests)

- [ ] **Step 1: Write failing test for multipart MIME output**

Add to `tests/test_sync.py`:

```python
def test_send_email_produces_multipart_alternative(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """send_email builds multipart/alternative with plain text and HTML parts."""
    from email import message_from_bytes

    account = make_test_account(database_connection, email="mp@example.com")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    client, service = _make_send_client(account.email)

    send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Hello",
        body="**Bold** and a [link](https://lab5.ca)",
        workflow_id=workflow.id,
    )

    send_body = service.users.return_value.messages.return_value.send.call_args.kwargs[
        "body"
    ]
    raw = base64.urlsafe_b64decode(send_body["raw"])
    msg = message_from_bytes(raw)
    assert msg.get_content_type() == "multipart/alternative"
    parts = msg.get_payload()
    assert len(parts) == 2
    plain_part = parts[0]
    html_part = parts[1]
    assert plain_part.get_content_type() == "text/plain"
    assert html_part.get_content_type() == "text/html"
    # HTML part has styled content
    html_body = html_part.get_payload(decode=True).decode()
    assert "<strong>" in html_body or "<b>" in html_body
    assert "lab5.ca" in html_body
    # Plain text part has no markdown markers
    plain_body = plain_part.get_payload(decode=True).decode()
    assert "**" not in plain_body
    assert "Bold" in plain_body


def test_send_email_stores_plain_text_in_db(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """body_text in DB contains stripped plain text, not Markdown."""
    account = make_test_account(database_connection, email="db@example.com")
    client, _service = _make_send_client(account.email)

    email = send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Hello",
        body="**Bold** text",
    )

    assert "**" not in email.body_text
    assert "Bold" in email.body_text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sync.py::test_send_email_produces_multipart_alternative -v`
Expected: FAIL.

- [ ] **Step 3: Update sync.send_email() to build multipart MIME**

In `src/mailpilot/sync.py`, modify the `send_email` function. Key changes:

1. Import `render_email_html`, `strip_markdown`, `get_theme` from `email_renderer`.
2. Import `MIMEMultipart` and `MIMEText` from `email.mime`.
3. Look up the workflow to get the theme (if `workflow_id` is provided).
4. Render HTML and strip plain text from the Markdown body.
5. Build a `MIMEMultipart("alternative")` with both parts.
6. Pass the MIME message to `gmail_client.send_message()`.
7. Store the stripped plain text in the DB.

```python
    # At top of function body, after the settings delete:
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    from mailpilot.email_renderer import get_theme, render_email_html, strip_markdown

    # Look up theme from workflow
    theme_name: str | None = None
    if workflow_id is not None:
        workflow = get_workflow(connection, workflow_id)
        if workflow is not None:
            theme_name = workflow.theme
    theme = get_theme(theme_name)

    # Render HTML and strip to plain text
    html_body = render_email_html(body, theme)
    plain_body = strip_markdown(body)

    # Build multipart/alternative MIME
    mime_message = MIMEMultipart("alternative")
    mime_message.attach(MIMEText(plain_body, "plain", "utf-8"))
    mime_message.attach(MIMEText(html_body, "html", "utf-8"))
```

Update the `gmail_client.send_message()` call to pass `message=mime_message` instead of `body=body`.

Update the `create_email()` call to pass `body_text=plain_body` instead of `body_text=body`.

Add `from mailpilot.database import get_workflow` to the imports inside the span block.

- [ ] **Step 4: Run new tests to verify they pass**

Run: `uv run pytest tests/test_sync.py::test_send_email_produces_multipart_alternative tests/test_sync.py::test_send_email_stores_plain_text_in_db -v`
Expected: PASS.

- [ ] **Step 5: Fix existing send_email tests**

Existing tests that inspect the raw MIME payload need updates because:
- The message is now `multipart/alternative` instead of `text/plain`
- To inspect headers, parse the multipart message
- The `body_text` assertion for `"Body text"` stays valid since plain ASCII without Markdown markers strips identically

Tests that check `email.body_text == "Body text"` should still pass (no Markdown markers in "Body text"). Tests that decode the raw MIME need to handle multipart. Tests that check `Content-Transfer-Encoding` need updating.

Update `test_send_email_always_uses_utf8_base64_encoding` -- this test becomes obsolete since the encoding is now per-part in a multipart message. Replace it with a test verifying both parts exist and use UTF-8:

```python
def test_send_email_always_uses_utf8_base64_encoding(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Both MIME parts use UTF-8 charset."""
    from email import message_from_bytes

    account = make_test_account(database_connection, email="enc@example.com")
    client, service = _make_send_client(account.email)

    send_email(
        database_connection,
        account=account,
        gmail_client=client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Hello",
        body="Plain ASCII body with no special characters.",
    )

    send_body = service.users.return_value.messages.return_value.send.call_args.kwargs[
        "body"
    ]
    raw = base64.urlsafe_b64decode(send_body["raw"])
    msg = message_from_bytes(raw)
    assert msg.get_content_type() == "multipart/alternative"
    parts = msg.get_payload()
    for part in parts:
        assert part.get_content_charset() == "utf-8"
```

Update other tests that decode `raw` to handle the multipart structure. The pattern:

```python
    msg = message_from_bytes(raw)
    # For multipart, get the plain text part (first) to check content:
    if msg.get_content_type() == "multipart/alternative":
        plain_part = msg.get_payload()[0]
        body_text = plain_part.get_payload(decode=True).decode()
    else:
        body_text = msg.get_payload(decode=True).decode()
```

- [ ] **Step 6: Run full test suite**

Run: `uv run pytest -x`
Expected: All PASS.

- [ ] **Step 7: Run lint and type check**

Run: `uv run ruff check --fix && uv run basedpyright`
Expected: Clean.

- [ ] **Step 8: Commit**

```bash
git add src/mailpilot/sync.py tests/test_sync.py
git commit -m "feat(sync): render Markdown to HTML and send multipart MIME"
```

---

### Task 7: Verify agent tools work through new pipeline

**Files:**
- Modify: `tests/test_agent_tools.py`

- [ ] **Step 1: Write test verifying agent reply_email produces HTML**

Add to `tests/test_agent_tools.py`:

```python
def test_reply_email_sends_multipart_html(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """reply_email body goes through Markdown->HTML pipeline in sync.send_email."""
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="sender@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)

    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Question",
        contact_id=contact.id,
        workflow_id=workflow.id,
        gmail_message_id="inbound-html",
        gmail_thread_id="thread-html",
    )
    assert inbound is not None

    gmail_client = _make_gmail_client(account)

    result = reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        email_id=inbound.id,
        body="## Summary\n\n**Important** info here.",
    )

    assert "error" not in result
    gmail_client.send_message.assert_called_once()
    call_kwargs = gmail_client.send_message.call_args.kwargs
    # sync.send_email now passes message= (MIMEBase), not body=
    assert "message" in call_kwargs
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `uv run pytest tests/test_agent_tools.py::test_reply_email_sends_multipart_html -v`

If it fails, investigate -- the agent tools call `sync.send_email()` which was updated in Task 6. The mock `gmail_client` should still work since `send_message` is mocked.

- [ ] **Step 3: Fix any agent tool test failures**

The agent tools tests mock `gmail_client.send_message`. Since `sync.send_email` now passes `message=mime_msg` instead of `body=text`, update any assertions that check `call_kwargs["body"]` to check `call_kwargs["message"]` instead.

- [ ] **Step 4: Run full test suite**

Run: `uv run pytest -x`
Expected: All PASS.

- [ ] **Step 5: Run lint and type check**

Run: `uv run ruff check --fix && uv run basedpyright`
Expected: Clean.

- [ ] **Step 6: Commit**

```bash
git add tests/test_agent_tools.py
git commit -m "test(agent): verify agent tools work with multipart HTML pipeline"
```

---

### Task 8: Final integration check

- [ ] **Step 1: Run make check**

Run: `make check`
Expected: All lint, type checks, and tests pass.

- [ ] **Step 2: Manual verification with CLI**

Test the full pipeline by sending an email with Markdown body:

```bash
uv run mailpilot workflow list
# Pick a workflow ID, or create one:
# uv run mailpilot workflow create --name "HTML Test" --type outbound --account-id <ID> --theme green

uv run mailpilot email send \
  --account-id <OUTBOUND_ACCOUNT_ID> \
  --to inbound@lab5.ca \
  --subject "HTML Email Test" \
  --body "## Product Comparison

| Model | Flow Rate | Capacity |
|-------|-----------|----------|
| WS48  | 150 GPM   | 48,000   |
| WS54  | 200 GPM   | 54,000   |

**Recommendation:** The WS54 is better for your needs.

Visit [Lab5](https://lab5.ca) for details."
```

Check the received email in Gmail to verify:
- Headings are colored and larger
- Table renders with styled headers
- Bold text renders correctly
- Link is clickable and colored

- [ ] **Step 3: Verify DB stores plain text**

```bash
uv run mailpilot email list --direction outbound --limit 1
# Check body_text has no ** or ## markers
uv run mailpilot email view <EMAIL_ID>
```

- [ ] **Step 4: Commit any final fixes if needed**
