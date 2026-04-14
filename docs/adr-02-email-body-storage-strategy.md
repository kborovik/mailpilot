# ADR-02: Email Body Storage Strategy

## Status

Accepted

## Context

Gmail API returns email bodies as-is. Since Gmail maintains the complete email archive, local HTML storage creates unnecessary redundancy. Email bodies are consumed by LLM agents for classification and qualification -- plain text is the only format they need.

MailPilot handles cold outreach emails and prospect replies. Business email clients (Gmail, Outlook) always include a `text/plain` part. HTML-only emails (marketing, newsletters) are outside the application's scope.

## Decision

Store only `body_text TEXT` in the `email` table. No HTML column, no HTML-to-text conversion.

- Extract `text/plain` MIME parts only
- If no `text/plain` part exists, store empty string
- Gmail remains the authoritative source -- fetch original on demand if needed

### MIME Extraction

Recursive walk of MIME parts during sync:

1. `text/plain` available -- use directly
2. `multipart/alternative` -- extract `text/plain` part, ignore `text/html`
3. Nested multipart -- recurse
4. No `text/plain` found -- store empty string

## Consequences

### Positive

- Simpler schema -- single `body_text` column
- No `html2text` dependency
- Reduced storage -- no redundant HTML
- Gmail handles archival and compliance

### Negative

- HTML-only emails stored with empty body (acceptable -- outside application scope)
- HTML display requires Gmail API call (added latency, quota cost)

## References

- [Gmail API Message Format](https://developers.google.com/gmail/api/reference/rest/v1/users.messages)
