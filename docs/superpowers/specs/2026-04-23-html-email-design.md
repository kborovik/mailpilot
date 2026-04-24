# HTML Email Rendering Design

## Overview

LLM agents generate email bodies in Markdown. Python converts Markdown to themed HTML for rich email rendering with tables, larger fonts, and workflow-based color schemes. Emails are sent as `multipart/alternative` (plain text + HTML).

## Architecture

### Pipeline

```
Agent tool (send_email / reply_email)
  |  body = "## Product Comparison\n| Model | Flow |\n..."
  v
sync.send_email(body=markdown_body, workflow_id=...)
  |  1. Look up workflow -> get theme
  |  2. render_email_html(markdown_body, theme) -> html
  |  3. strip_markdown(markdown_body) -> plain_text (for DB + MIME)
  |  4. Build MIMEMultipart("alternative") with plain_text + html
  v
GmailClient.send_message(mime_message)  # accepts pre-built MIME object
```

### Conversion Layer: `sync.send_email()`

The single choke point for all outbound email. Responsibilities:

1. Receives Markdown `body` from agent tools or CLI.
2. Looks up the workflow to get the theme (falls back to `blue` if no workflow or unknown theme).
3. Renders Markdown to HTML via `EmailRenderer` (mistune custom renderer with inline styles).
4. Strips Markdown to plain text via `PlainTextRenderer` (no `**`, `__`, `#`, `|---|`).
5. Builds `MIMEMultipart("alternative")` with `MIMEText(plain_text, "plain")` + `MIMEText(html, "html")`.
6. Passes the assembled MIME message to `GmailClient.send_message()`.
7. Stores `plain_text` (stripped) in DB `body_text` column.

### GmailClient Changes

`GmailClient.send_message()` signature changes to accept a pre-built `MIMEBase` message instead of a plain text `body` string. It adds headers (To, Subject, From, In-Reply-To, X-MailPilot-*) and sends. This keeps `GmailClient` as a dumb transport.

### DB Storage

`body_text` stores clean plain text -- no Markdown syntax, no HTML. The Markdown source is an intermediate format that exists only during the send call. This is better for search, CLI display, and activity summaries.

## Markdown Rendering

### Library: mistune

- Zero dependencies, 53.6 KB wheel
- Fastest Python Markdown parser
- Table support via plugin: `plugins=['table']`
- Custom `HTMLRenderer` subclass for inline styles

### EmailRenderer

Subclass of `mistune.HTMLRenderer` that injects inline `style=""` attributes during conversion. No separate CSS inlining step needed.

Styled elements:

| Element | Style |
|---------|-------|
| `<h1>` | primary color, 24px, bold |
| `<h2>` | primary color, 20px, bold |
| `<h3>` | primary color, 18px, bold |
| `<p>` | #333, 16px, line-height 1.5 |
| `<table>` | full-width, border-collapse |
| `<th>` | accent background, dark text, bold |
| `<td>` | padding, bottom border |
| `<a>` | primary color, underline |
| `<code>` | light gray background, monospace |
| `<ul>`, `<ol>` | standard padding |
| `<hr>` | border color, 1px top border |

### PlainTextRenderer

Separate renderer that outputs clean plain text:

- Headings: uppercase text with blank line
- Tables: aligned columns (or simplified format)
- Links: `text (url)`
- Bold/italic: text only, no markers
- Code: text only, no backticks
- Lists: `- item` or `1. item`

### HTML Wrapper

No chrome (no bars, headers, or visual frame). Content-only styling:

```html
<div style="font-family:Arial,'Helvetica Neue',Helvetica,sans-serif;
            font-size:16px; line-height:1.5; color:#333333; max-width:600px;">
  {rendered_content}
</div>
```

Theme colors appear only on content elements the agent generates (headings, table headers, links).

## Theme Model

### EmailTheme

```python
@dataclasses.dataclass
class EmailTheme:
    primary: str    # headings, links
    accent: str     # table header background
    border: str     # table/hr borders
```

### Predefined Palettes

| Name | Primary | Accent | Border |
|------|---------|--------|--------|
| `blue` | `#2563eb` | `#dbeafe` | `#bfdbfe` |
| `green` | `#16a34a` | `#dcfce7` | `#bbf7d0` |
| `orange` | `#ea580c` | `#ffedd5` | `#fed7aa` |
| `purple` | `#7c3aed` | `#ede9fe` | `#ddd6fe` |
| `red` | `#dc2626` | `#fee2e2` | `#fecaca` |
| `slate` | `#475569` | `#f1f5f9` | `#e2e8f0` |

### Schema Change

```sql
ALTER TABLE workflow ADD COLUMN theme TEXT NOT NULL DEFAULT 'blue';
```

### CLI

- `workflow create --theme green`
- `workflow update ID --theme orange`
- Validates against palette names. Invalid theme -> error.
- Default: `blue`.

## What Doesn't Change

- **Agent tools API**: `send_email(body=...)` and `reply_email(body=...)` still take `body: str`. The agent doesn't know about HTML.
- **Agent instructions**: No changes needed. LLMs naturally produce Markdown.
- **DB schema for email**: `body_text` stores stripped plain text. No new column.
- **Inbound email handling**: `body_text` on inbound emails stays as plain text extracted from Gmail.

## Dependencies

One new dependency:

- `mistune` -- Markdown parser (zero deps, 53.6 KB wheel, BSD-3-Clause)

No `css-inline` needed -- the custom renderer handles inline styles directly.

## Email Client Compatibility

Based on research (caniemail.com, Litmus):

- All CSS is inline `style=""` -- Gmail strips `<style>` tags.
- Font stack: `Arial, 'Helvetica Neue', Helvetica, sans-serif` (web-safe).
- Body font: 16px (industry standard, accessible).
- Content tables use `border-collapse: collapse` with inline styles per cell.
- Max-width 600px on container div.
- No flex/grid/position/float. No CSS variables.
- `border-radius` works everywhere except Outlook desktop (acceptable degradation).
