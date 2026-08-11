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
    "font-size:16px; line-height:1.5; color:#333333"
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


# Lab5 signature chrome (§V.151) — fixed; body THEMES never recolor these.
# Palette + layout match the operator-authored mark signature (table, logo,
# four-colour rule, monospace contact rows).
_SIG_NAME_COLOR = "#101820"
_SIG_TITLE_COLOR = "#0969da"
_SIG_BODY_COLOR = "#101820"
_SIG_LABEL_COLOR = "#8A939B"
_SIG_BAR_COLORS = ("#0969da", "#cf222e", "#f9c513", "#1f883d")
_SIG_MONO = "Consolas,Menlo,'DejaVu Sans Mono',monospace"
_SIG_SANS = "Helvetica,Arial,sans-serif"

# Embedded lab5 mark (PNG data URI) — same asset as the design HTML signature.
_LAB5_LOGO_DATA_URI = (
    "data:image/png;base64,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"
    "CC"
)


def _tel_href(phone: str) -> str:
    """Build a ``tel:`` href from a display phone number.

    Keeps a leading ``+`` and digits only; separators are presentation.
    """
    digits = "".join(ch for ch in phone if ch.isdigit())
    prefix = "+" if phone.lstrip().startswith("+") else ""
    return f"tel:{prefix}{digits}"


def _website_display(website: str) -> str:
    """Strip scheme for link text (href keeps the absolute URL)."""
    text = website.strip()
    lower = text.lower()
    for prefix in ("https://", "http://"):
        if lower.startswith(prefix):
            text = text[len(prefix) :]
            break
    return text.rstrip("/")


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


def _sig_detail_rows(
    full_name: str | None,
    title: str | None,
    website: str | None,
    phone: str | None,
) -> str:
    """Build the right-hand detail table rows (empty fields omitted)."""
    rows: list[str] = []
    if full_name is not None:
        safe = html.escape(full_name)
        rows.append(
            "<tr>"
            f'<td style="font-family:{_SIG_SANS}; font-size:16px; '
            f"line-height:20px; font-weight:bold; color:{_SIG_NAME_COLOR}; "
            f'letter-spacing:0.2px; padding:0 0 3px 0;">{safe}</td>'
            "</tr>"
        )
    if title is not None:
        safe = html.escape(title)
        rows.append(
            "<tr>"
            f'<td style="font-family:{_SIG_MONO}; font-size:11px; '
            f"line-height:15px; color:{_SIG_TITLE_COLOR}; letter-spacing:1.6px; "
            f'text-transform:uppercase; padding:0 0 11px 0;">{safe}</td>'
            "</tr>"
        )
    if website is not None:
        safe_url = html.escape(website, quote=True)
        safe_text = html.escape(_website_display(website))
        rows.append(
            "<tr>"
            f'<td style="padding:0 0 5px 0; font-family:{_SIG_MONO}; '
            f'font-size:12px; line-height:17px; color:{_SIG_BODY_COLOR};">'
            f'<span style="color:{_SIG_LABEL_COLOR};">web&nbsp;&nbsp;</span>'
            f'<a href="{safe_url}" style="color:{_SIG_BODY_COLOR}; '
            f'text-decoration:none;">{safe_text}</a>'
            "</td>"
            "</tr>"
        )
    if phone is not None:
        safe = html.escape(phone)
        safe_href = html.escape(_tel_href(phone), quote=True)
        rows.append(
            "<tr>"
            f'<td style="font-family:{_SIG_MONO}; font-size:12px; '
            f'line-height:17px; color:{_SIG_BODY_COLOR};">'
            f'<span style="color:{_SIG_LABEL_COLOR};">cell&nbsp;&nbsp;</span>'
            f'<a href="{safe_href}" style="color:{_SIG_BODY_COLOR}; '
            f'text-decoration:none;">{safe}</a>'
            "</td>"
            "</tr>"
        )
    return "".join(rows)


def _sig_color_bar() -> str:
    """Four-colour vertical rule drawn from the lab5 mark."""
    segments: list[str] = []
    for color in _SIG_BAR_COLORS:
        segments.append(
            "<tr>"
            f'<td height="15" bgcolor="{color}" '
            f'style="height:15px; background-color:{color}; '
            f'font-size:1px; line-height:15px;">&nbsp;</td>'
            "</tr>"
        )
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'width="2" style="width:2px; border-collapse:collapse;">'
        f"{''.join(segments)}"
        "</table>"
    )


def render_signature_html(
    signature: Account | AccountSignature | None,
) -> str:
    """Render account signature as HTML block for wire MIME (§V.151).

    Table layout matching the lab5 mark signature: 60px logo, four-colour
    vertical rule, then stacked detail rows for name / title / web / cell.
    Hierarchy is bold name + uppercase monospace title + muted labels —
    not pipe separators. Empty fields omit their rows; all-empty returns
    the empty string (no block). Logo + colour bar always accompany a
    non-empty signature. Inline styles only; body theme never recolors.
    """
    full_name, title, website, phone = _signature_fields(signature)
    if full_name is None and title is None and website is None and phone is None:
        return ""

    details = _sig_detail_rows(full_name, title, website, phone)
    bar = _sig_color_bar()
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="border-collapse:collapse; font-family:{_SIG_SANS}; '
        'margin-top:20px;">'
        "<tr>"
        # logo
        '<td width="60" valign="top" style="width:60px; padding:2px 0 0 0;">'
        f'<img src="{_LAB5_LOGO_DATA_URI}" width="60" height="60" '
        'alt="lab5.ca" style="display:block; width:60px; height:60px; '
        'border:0; outline:none; text-decoration:none;">'
        "</td>"
        # spacer
        '<td width="18" style="width:18px; font-size:1px; line-height:1px;">'
        "&nbsp;</td>"
        # colour bar
        '<td width="2" valign="top" style="width:2px; padding:2px 0 0 0;">'
        f"{bar}"
        "</td>"
        # spacer
        '<td width="18" style="width:18px; font-size:1px; line-height:1px;">'
        "&nbsp;</td>"
        # details
        '<td valign="top" style="padding:0;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="border-collapse:collapse;">'
        f"{details}"
        "</table>"
        "</td>"
        "</tr>"
        "</table>"
    )


def render_signature_text(
    signature: Account | AccountSignature | None,
) -> str:
    """Render account signature as plain-text block for wire MIME (§V.151).

    Mirrors the HTML stacked layout: name, title, ``web  host``, ``cell  phone``
    on separate lines. Returns lines joined by newlines without a leading
    delimiter; the caller prepends the classic ``--`` separator when
    appending to the body. Empty fields are omitted; all-empty returns the
    empty string.
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
        lines.append(f"web  {_website_display(website)}")
    if phone is not None:
        lines.append(f"cell  {phone}")
    return "\n".join(lines)
