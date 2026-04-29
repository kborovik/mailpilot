# ADR-02: Email Body Storage Strategy

## Status

Accepted

## Context

Gmail API returns email bodies as-is. Since Gmail maintains the complete email archive, local HTML storage creates unnecessary redundancy. Email bodies are consumed by LLM agents for classification and qualification -- plain text is the only format they need.

MailPilot handles cold outreach emails and prospect replies. Business email clients (Gmail, Outlook) always include a `text/plain` part. HTML-only emails (marketing, newsletters) are outside the application's scope.

## Decision

Store only `body_text TEXT` in the `email` table (`schema.sql`: `body_text TEXT NOT NULL DEFAULT ''`). No HTML column, no HTML-to-text conversion on the storage path.

The rule applies in both directions:

- **Inbound**: extract `text/plain` from the Gmail payload; HTML parts are ignored. Implementation: `extract_text_from_message` in `gmail.py`. See `docs/email-flow.md` step 3.
- **Outbound**: the agent's Markdown body is the source of truth. `sync.send_email` renders it to themed HTML via `email_renderer.render_email_html` and ships a `multipart/alternative` MIME envelope (plain + HTML), but persists only the plain text source on the `email` row. The HTML is not retained -- Gmail keeps the sent message.

Gmail remains the authoritative source for the original wire format. If the operator ever needs the original HTML of an inbound message, it can be fetched from the Gmail API via `gmail_message_id`.

### MIME Extraction (inbound)

Recursive walk of MIME parts during sync (`extract_text_from_message` -> `_extract_text_from_part` in `gmail.py`):

1. `text/plain` available -- use directly
2. `multipart/*` -- prefer the first `text/plain` sub-part; ignore `text/html` siblings
3. Nested multipart -- recurse
4. No `text/plain` found -- store empty string

Extracted text is then normalized:

- Strip trailing whitespace per line.
- Collapse three or more consecutive blank lines to two.
- Strip leading/trailing blank lines.

Normalization keeps quoted-reply chains and signature blocks readable in the agent prompt without changing semantics.

### Plain text on outbound

Markdown is designed to be readable as plain text, so the agent's Markdown is the `text/plain` part verbatim (with `strip_control_chars` applied to remove non-printable bytes). The HTML part is generated only for delivery and is not stored.

## Consequences

### Positive

- Simpler schema -- single `body_text` column.
- No `html2text` dependency on the inbound path.
- Reduced storage -- no redundant HTML for either direction.
- Outbound stored body is human-readable Markdown, which is also what the agent reasons over when reviewing prior outbound messages in history.
- Gmail handles archival and compliance for the original wire format.

### Negative

- HTML-only inbound emails are stored with empty body (acceptable -- outside application scope).
- HTML display of an inbound message requires a Gmail API call (added latency, quota cost).
- Outbound HTML is not auditable from the database -- to inspect the rendered version of a sent email, fetch it via Gmail.

## References

- [Gmail API Message Format](https://developers.google.com/gmail/api/reference/rest/v1/users.messages)
- `src/mailpilot/gmail.py` -- `extract_text_from_message`, `_extract_text_from_part`
- `src/mailpilot/email_renderer.py` -- outbound Markdown -> themed HTML rendering
- `docs/email-flow.md` step 3 -- inbound text extraction in context
