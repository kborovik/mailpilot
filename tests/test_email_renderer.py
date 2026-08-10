"""Tests for Markdown-to-HTML email rendering and account signatures."""

from mailpilot.email_renderer import (
    THEMES,
    get_theme,
    render_email_html,
    render_signature_html,
    render_signature_text,
)
from mailpilot.models import AccountSignature


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


def test_render_soft_newlines_become_br():
    """Soft newlines in signatures/URL lists must become <br>, not spaces (§V.92)."""
    body = (
        "A few places that show the work:\n"
        "https://lab5.ca/services/tenant-configuration-as-code/\n"
        "https://lab5.ca/blog/acumatica-live-company-to-rebuildable-uat/\n"
        "\n"
        "---\n"
        "Konstantin Borovik\n"
        "DevOps Engineer\n"
        "https://lab5.ca\n"
        "416-670-0621"
    )
    html = render_email_html(body, get_theme("blue"))
    assert "<br" in html
    # Signature lines stay distinct; must not collapse to one space-joined run.
    assert "Konstantin Borovik" in html
    assert "DevOps Engineer" in html
    assert "416-670-0621" in html
    collapsed = "Konstantin Borovik DevOps Engineer https://lab5.ca 416-670-0621"
    assert collapsed not in html
    # Bare URL list lines also hard-wrapped.
    assert html.count("<br") >= 5


# -- Account signature (§V.151) ------------------------------------------------


def test_render_signature_html_empty_returns_empty():
    assert render_signature_html(None) == ""
    assert render_signature_html(AccountSignature()) == ""


def test_render_signature_html_lab5_palette():
    """§V.151: fixed lab5 mark palette; body theme does not recolor signature."""
    sig = AccountSignature(
        full_name="Ada Lovelace",
        title="Engineer",
        website="https://lab5.ca",
        phone="+1-555-0100",
    )
    html = render_signature_html(sig)
    # Name / body / label / title + four-colour bar.
    assert "#101820" in html
    assert "#8A939B" in html
    assert "#0969da" in html
    assert "#cf222e" in html
    assert "#f9c513" in html
    assert "#1f883d" in html
    assert "Ada Lovelace" in html
    assert "Engineer" in html
    assert 'href="https://lab5.ca"' in html
    assert "+1-555-0100" in html
    # Body theme primary (blue #2563eb) must not appear.
    assert "#2563eb" not in html


def test_render_signature_html_mark_layout():
    """§V.151: table chrome = logo + colour bar + stacked detail rows."""
    sig = AccountSignature(
        full_name="Ada Lovelace",
        title="Engineer",
        website="https://lab5.ca",
        phone="+1-555-0100",
    )
    html = render_signature_html(sig)
    assert 'role="presentation"' in html
    assert "data:image/png;base64," in html
    assert 'alt="lab5.ca"' in html
    assert "width:60px" in html
    assert "text-transform:uppercase" in html
    assert "font-weight:bold" in html
    assert "web&nbsp;&nbsp;" in html
    assert "cell&nbsp;&nbsp;" in html
    # Display host without scheme; href keeps absolute URL.
    assert ">lab5.ca<" in html
    assert 'href="https://lab5.ca"' in html
    # No pipe-separator layout.
    assert ">|<" not in html
    # Name precedes title, website precedes phone.
    assert html.index("Ada Lovelace") < html.index("Engineer")
    assert html.index("lab5.ca") < html.index("+1-555-0100")


def test_render_signature_html_phone_is_tel_link():
    html = render_signature_html(AccountSignature(phone="416-670-0621"))
    assert 'href="tel:+14166700621"' in html
    assert "416-670-0621" in html
    assert "cell&nbsp;&nbsp;" in html
    # Brand chrome still present for a non-empty signature.
    assert "data:image/png;base64," in html


def test_render_signature_html_omits_empty_fields():
    html = render_signature_html(AccountSignature(full_name="Only Name"))
    assert "Only Name" in html
    assert "web&nbsp;&nbsp;" not in html
    assert "cell&nbsp;&nbsp;" not in html
    assert "text-transform:uppercase" not in html
    # Logo chrome remains; no contact hrefs.
    assert "data:image/png;base64," in html
    assert "href=" not in html


def test_render_signature_html_partial_contact():
    """Missing title/phone must not leave orphan labels or empty rows."""
    html = render_signature_html(
        AccountSignature(full_name="Ada", website="https://lab5.ca")
    )
    assert "Ada" in html
    assert "web&nbsp;&nbsp;" in html
    assert "cell&nbsp;&nbsp;" not in html
    assert "text-transform:uppercase" not in html
    assert ">lab5.ca<" in html


def test_render_signature_html_name_and_title_only():
    html = render_signature_html(AccountSignature(full_name="Ada", title="Engineer"))
    assert "Ada" in html
    assert "Engineer" in html
    assert "web&nbsp;&nbsp;" not in html
    assert "cell&nbsp;&nbsp;" not in html


def test_render_signature_html_escapes_content():
    html = render_signature_html(
        AccountSignature(
            full_name="<script>x</script>", website="https://a.com/?q=1&b=2"
        )
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "q=1&amp;b=2" in html or "q=1&b=2" in html


def test_render_signature_text_empty_returns_empty():
    assert render_signature_text(None) == ""
    assert render_signature_text(AccountSignature()) == ""


def test_render_signature_text_lines():
    sig = AccountSignature(
        full_name="Ada Lovelace",
        title="Engineer",
        website="https://lab5.ca",
        phone="+1-555-0100",
    )
    text = render_signature_text(sig)
    assert text == "Ada Lovelace\nEngineer\nweb  lab5.ca\ncell  +1-555-0100"


def test_render_signature_text_partial():
    text = render_signature_text(AccountSignature(full_name="Ada", phone="+1"))
    assert text == "Ada\ncell  +1"


def test_render_signature_text_name_and_title_only():
    text = render_signature_text(AccountSignature(full_name="Ada", title="Engineer"))
    assert text == "Ada\nEngineer"
