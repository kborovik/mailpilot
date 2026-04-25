# ADR-08: CRM Evolution -- Activity Timeline, Tags, and Notes

## Status

Accepted.

## Context

The system has two layers of intelligence:

1. **Claude Code** -- strategic orchestrator. Creates workflows, assigns contacts, reviews outcomes, generates reports. Operates via CLI.
2. **Internal Pydantic AI agent** -- subordinate tactical executor. Handles real-time reactive work (inbound classification, auto-replies, follow-ups).

Claude Code needs:

- A unified timeline of all interactions per contact (for relationship health monitoring)
- Flexible segmentation (no formal pipeline -- tags like "prospect", "contacted", "client")
- Freeform annotations on contacts and companies
- Efficient query paths for reporting (activity summaries, campaign effectiveness, pipeline snapshots)

The existing schema (account, company, contact, workflow, enrollment, email, task, sync_status) lacks these CRM capabilities.

## Decision

### Identity

Reframe MailPilot from "AI email platform" to "CRM with Gmail as the primary communication channel." The Project Overview in CLAUDE.md reflects this shift.

### Three new tables

**`activity`** -- unified per-contact timeline. Every significant event (email sent/received, note added, tag changed, status changed, workflow assigned/completed/failed) is recorded as an immutable row. `contact_id` is NOT NULL (primary query axis). `summary` holds a human-readable one-liner. `detail` is a JSONB bag for type-specific data. Append-only -- no updates, no deletes.

**`tag`** -- flexible segmentation. Polymorphic table (`entity_type` + `entity_id`) for labeling contacts and companies. Tags replace formal pipeline stages. Convention: lowercase, hyphenated names. UNIQUE constraint prevents duplicates. `create_tag` normalizes names via `name.strip().lower()`.

**`note`** -- freeform annotations. Same polymorphic pattern as tags. Append-only. Creating a note also creates a `note_added` activity.

### No changes to existing tables

All eight existing tables retain their current schema. No columns added, no constraints modified. The new tables extend the system without altering what works.

### No formal pipeline

Tags serve as lightweight status markers (prospect, contacted, interested, client). No ordered stages, no deal values, no win/loss tracking. If patterns emerge from usage, frequently-used tags can be promoted to structured fields in a future migration.

### Activity creation

Initially manual -- Claude Code creates activities via CLI (`activity create`) and tag/note commands auto-create corresponding activities. Auto-creation from the sync pipeline and send function is deferred to a follow-up.

## Consequences

### Positive

- Unified timeline gives Claude Code a single query target for all relationship reporting
- Tags are maximally flexible -- no premature commitment to pipeline stages
- Polymorphic entity pattern reuses one table for contacts and companies (two tables instead of four)
- Append-only activity and note tables are simple -- no concurrency concerns, no update conflicts
- No changes to existing modules -- zero risk of regression

### Negative

- Polymorphic FK on tag and note means no database-level referential integrity on `entity_id`. Orphaned rows from deleted contacts/companies are possible but harmless and easily cleaned up.
- Activity table grows unboundedly. Acceptable for solo consultant volume. If it exceeds 100K rows, add a retention policy or archival.
- Manual activity creation initially means the timeline is incomplete until auto-creation is wired in. The CLI `activity create` command provides a manual workaround.
- Tag names are freeform -- no enforcement of a controlled vocabulary. Convention (documented in CLAUDE.md) mitigates inconsistency. Claude Code can audit and normalize.
