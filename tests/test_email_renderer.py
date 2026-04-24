"""Tests for Markdown-to-HTML email rendering."""

from mailpilot.email_renderer import (
    THEMES,
    get_theme,
    render_email_html,
    strip_markdown,
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
    assert "TITLE" in result


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
