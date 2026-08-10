"""Markdown-to-HTML email rendering with inline styles and theme support."""

from __future__ import annotations

import dataclasses
import html
from typing import cast

import mistune

from mailpilot.models import Account, AccountSignature


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


class EmailRenderer(mistune.HTMLRenderer):  # type: ignore[misc]
    """Custom HTML renderer that injects inline styles for email clients."""

    def __init__(self, theme: EmailTheme) -> None:
        super().__init__()
        self.theme = theme

    def heading(self, text: str, level: int, **attrs: object) -> str:
        """Render heading with themed primary color."""
        sizes = {1: "24px", 2: "20px", 3: "18px"}
        size = sizes.get(level, "16px")
        color = self.theme.primary if level <= 3 else "#333333"
        style = (
            f"color:{color}; font-size:{size}; font-weight:bold; "
            f"line-height:1.3; margin:16px 0 8px 0"
        )
        return f'<h{level} style="{style}">{text}</h{level}>\n'

    def paragraph(self, text: str) -> str:
        """Render paragraph with body text styles."""
        style = "font-size:16px; line-height:1.5; color:#333333; margin:0 0 16px 0"
        return f'<p style="{style}">{text}</p>\n'

    def link(self, text: str, url: str, title: str | None = None) -> str:
        """Render link with themed primary color."""
        style = f"color:{self.theme.primary}; text-decoration:underline"
        title_attr = f' title="{title}"' if title else ""
        return f'<a href="{url}" style="{style}"{title_attr}>{text}</a>'

    def strong(self, text: str) -> str:
        """Render bold text."""
        return f"<strong>{text}</strong>"

    def emphasis(self, text: str) -> str:
        """Render italic text."""
        return f"<em>{text}</em>"

    def codespan(self, text: str) -> str:
        """Render inline code with monospace background."""
        style = (
            "background-color:#f3f4f6; padding:2px 6px; "
            "border-radius:3px; "
            "font-family:'Courier New',Courier,monospace; font-size:14px"
        )
        return f'<code style="{style}">{text}</code>'

    def thematic_break(self) -> str:
        """Render horizontal rule with themed border color."""
        style = f"border:none; border-top:1px solid {self.theme.border}; margin:24px 0"
        return f'<hr style="{style}">\n'

    def list(self, text: str, ordered: bool, **attrs: object) -> str:
        """Render ordered or unordered list."""
        tag = "ol" if ordered else "ul"
        style = (
            "padding-left:24px; margin:0 0 16px 0; "
            "font-size:16px; line-height:1.5; color:#333333"
        )
        return f'<{tag} style="{style}">{text}</{tag}>\n'

    def list_item(self, text: str, **attrs: object) -> str:
        """Render list item with spacing."""
        return f'<li style="margin:4px 0">{text}</li>\n'

    def table(self, text: str) -> str:
        """Render table with collapsed borders."""
        style = (
            "width:100%; border-collapse:collapse; font-size:14px; margin:0 0 16px 0"
        )
        return f'<table style="{style}">{text}</table>\n'

    def table_head(self, text: str) -> str:
        """Render table header section."""
        return f"<thead>{text}</thead>\n"

    def table_body(self, text: str) -> str:
        """Render table body section."""
        return f"<tbody>{text}</tbody>\n"

    def table_row(self, text: str) -> str:
        """Render table row."""
        return f"<tr>{text}</tr>\n"

    def table_cell(
        self,
        text: str,
        align: str | None = None,
        head: bool = False,
        **attrs: object,
    ) -> str:
        """Render table cell with themed header background."""
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
    r"""Convert Markdown to email-safe HTML with inline styles.

    Args:
        markdown_body: Markdown source from the LLM agent.
        theme: Color palette for headings, tables, links.

    Returns:
        Complete HTML string with inline styles, wrapped in a container div.

    Soft newlines (single ``\n`` inside a paragraph) become ``<br>`` via
    mistune ``hard_wrap=True`` so signatures and bare URL lists keep their
    line structure in HTML email clients (§V.92, closes §B.126).
    """
    renderer = EmailRenderer(theme)
    md = mistune.create_markdown(
        renderer=renderer,
        plugins=["table"],
        hard_wrap=True,
    )
    content = cast(str, md(markdown_body))
    return f'<div style="{_CONTAINER_STYLE}">{content}</div>'


# Lab5 signature palette (§V.151) — fixed; body THEMES never recolor these.
_SIG_NAME_COLOR = "#1f2328"
_SIG_MUTED_COLOR = "#424a53"
_SIG_LINK_COLOR = "#0969da"
_SIG_BORDER_COLOR = "#d1d9e0"


def _signature_fields(
    signature: Account | AccountSignature | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Extract normalized signature fields from Account or AccountSignature."""
    if signature is None:
        return None, None, None, None
    if isinstance(signature, Account):
        nested = signature.account_signature()
        if nested is None:
            return None, None, None, None
        return nested.full_name, nested.title, nested.website, nested.phone
    return signature.full_name, signature.title, signature.website, signature.phone


def render_signature_html(
    signature: Account | AccountSignature | None,
) -> str:
    """Render account signature as HTML block for wire MIME (§V.151).

    Lab5 palette constants (not body theme): name ``#1f2328``, title/phone
    ``#424a53``, website link ``#0969da``, border-top ``#d1d9e0``. Inline
    styles only; no background band. Empty fields omitted; all-empty returns
    empty string (no block, no separator).
    """
    full_name, title, website, phone = _signature_fields(signature)
    if full_name is None and title is None and website is None and phone is None:
        return ""

    lines: list[str] = []
    if full_name is not None:
        safe = html.escape(full_name)
        lines.append(
            f'<div style="color:{_SIG_NAME_COLOR}; font-size:14px; '
            f'font-weight:600; line-height:1.4; margin:0 0 2px 0">'
            f"{safe}</div>"
        )
    if title is not None:
        safe = html.escape(title)
        lines.append(
            f'<div style="color:{_SIG_MUTED_COLOR}; font-size:13px; '
            f'line-height:1.4; margin:0 0 2px 0">{safe}</div>'
        )
    if website is not None:
        safe_url = html.escape(website, quote=True)
        safe_text = html.escape(website)
        lines.append(
            f'<div style="font-size:13px; line-height:1.4; margin:0 0 2px 0">'
            f'<a href="{safe_url}" style="color:{_SIG_LINK_COLOR}; '
            f'text-decoration:none">{safe_text}</a></div>'
        )
    if phone is not None:
        safe = html.escape(phone)
        lines.append(
            f'<div style="color:{_SIG_MUTED_COLOR}; font-size:13px; '
            f'line-height:1.4; margin:0">{safe}</div>'
        )

    block_style = (
        f"margin-top:24px; padding-top:16px; "
        f"border-top:1px solid {_SIG_BORDER_COLOR}; "
        f"font-family:Arial,'Helvetica Neue',Helvetica,sans-serif"
    )
    return f'<div style="{block_style}">{"".join(lines)}</div>'


def render_signature_text(
    signature: Account | AccountSignature | None,
) -> str:
    """Render account signature as plain-text block for wire MIME (§V.151).

    Returns lines joined by newlines without a leading delimiter. Caller
    prepends the classic ``--`` separator when appending to the body.
    All-empty returns empty string.
    """
    full_name, title, website, phone = _signature_fields(signature)
    if full_name is None and title is None and website is None and phone is None:
        return ""
    lines: list[str] = []
    if full_name is not None:
        lines.append(full_name)
    if title is not None:
        lines.append(title)
    if website is not None:
        lines.append(website)
    if phone is not None:
        lines.append(phone)
    return "\n".join(lines)
