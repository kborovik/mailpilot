# ADR-03: Email Body Storage Strategy

## Status

Needs review

## Context

Since Gmail API maintains the complete email archive, local HTML storage creates unnecessary redundancy.

**Gmail API Behavior**: Returns messages as-is without automatic HTML-to-text conversion. Many emails are HTML-only, requiring robust conversion during ingestion.

**LLM Processing**: Email bodies are consumed by LLM agents for classification and mission evaluation. Raw extracted text contains signatures, legal disclaimers, and boilerplate that waste tokens and reduce classification accuracy.

## Decision

Store only `body_text TEXT` (plain text). No HTML column:

- Primary storage: Plain text only in `emails.body_text`
- HTML access: Fetch from Gmail API on-demand
- Two-phase pipeline: raw extraction during sync, then LLM cleaning during classification
- Gmail remains authoritative source for original content

## Two-Phase Body Text Pipeline

### Phase 1: Raw Extraction (Gmail Sync)

During `gmail_message_to_db_email()`, the body is extracted and stored as-is:

1. `extract_text_from_message()` recursively walks MIME parts
2. Prefers `text/plain` parts; falls back to `text/html` converted via `html2text`
3. Line endings normalized to LF
4. Result stored in `body_text`

### Phase 2: LLM Cleaning (Classification)

During email routing, the LLM classifies the email and returns a `cleaned_body`:

1. LLM strips signatures, legal disclaimers, confidentiality notices, and boilerplate
2. Preserves actual message content exactly as written
3. `update_email_body_after_classification()` overwrites `body_text` with the cleaned version
4. `is_classified` set to `true`

The raw version is not preserved -- Gmail API is the authoritative source if the original is needed.

## Consequences

### Positive

- Reduced storage (no HTML column)
- Simpler schema -- single `body_text` column serves both ingestion and LLM consumption
- Better LLM performance -- cleaned text has no boilerplate noise
- Gmail handles archival/compliance

### Negative

- HTML display requires API calls (added latency)
- Original body text lost after classification (must re-fetch from Gmail)
- Counts against API quotas

### Neutral

- Reversible decision
- Clear separation: database for processing, Gmail for archival

## Implementation Details

### HTML-to-Text Conversion

Uses `html2text` library (configured in `gmail/sync.py`):

- Images ignored (`ignore_images = True`)
- Emphasis ignored (`ignore_emphasis = True`)
- Links preserved (`ignore_links = False`)
- No line wrapping (`body_width = 0`)
- Unicode preserved (`unicode_snob = True`)
- Post-processing: excess whitespace cleaned, consecutive blank lines limited to 2

### MIME Handling

Recursive extraction in `_extract_text_from_part()`:

- Plain text only: Direct extraction
- HTML only: Convert to plain text via `html2text`
- Multipart/alternative: Prefer `text/plain`, fall back to `text/html`
- Nested multipart: Recursive extraction

### Key Files

- `src/pilot/gmail/sync.py` -- extraction, HTML-to-text conversion, message-to-model mapping
- `src/pilot/gmail/operations/emails.py` -- `update_email_body_after_classification()`
- `src/pilot/missions/llm/models.py` -- `ClassificationDecision.cleaned_body` field
- `src/pilot/missions/workflows/email_routing.py` -- orchestrates classification and body update
