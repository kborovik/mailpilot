# Unified List Filters for CLI Commands

**Date:** 2026-04-21
**Status:** Approved
**Approach:** Per-entity filters (Approach A) -- no shared abstractions

## Problem

The CLI list subcommands have inconsistent filtering support. An LLM agent operating the system must drop to raw SQL for common queries like "show me the reply in this thread" or "show emails since the last sync." The `activity list` command is well-designed with `--contact-id`, `--company-id`, `--type`, `--since`, and `--limit`. Other list commands lag behind.

## Scope

9 new filters across 4 commands. Workflow enrollment CLI is out of scope (separate issue).

## Filter Inventory

| Command | New Filter | Type | DB Column |
|---------|-----------|------|-----------|
| `email list` | `--since ISO` | datetime | `COALESCE(sent_at, received_at)` |
| `email list` | `--thread-id TEXT` | string | `gmail_thread_id` |
| `email list` | `--direction inbound\|outbound` | choice | `direction` |
| `email list` | `--workflow-id UUID` | uuid | `workflow_id` |
| `email list` | `--status sent\|received\|bounced` | choice | `status` |
| `workflow list` | `--status draft\|active\|paused` | choice | `status` |
| `workflow list` | `--type inbound\|outbound` | choice | `type` |
| `contact list` | `--status active\|bounced\|unsubscribed` | choice | `status` |
| `note list` | `--since ISO` | datetime | `created_at` |

### Tiers (priority order)

- **Tier 1** (agent needed these in a real session): `email --since`, `email --thread-id`
- **Tier 2** (avoids SQL in common agent workflows): `email --direction`, `email --workflow-id`, `workflow --status`, `workflow --type`, `contact --status`
- **Tier 3** (consistency/completeness): `note --since`, `email --status`

## Implementation Pattern

Follow the existing `activity list` / `list_activities()` pattern for each filter.

### CLI layer (cli.py)

- Add Click options to the relevant `list` subcommand
- Choice filters use `click.Choice(["value1", "value2", ...])`
- `--since` accepts ISO datetime string, parsed with `datetime.fromisoformat()`
- FK references (`--workflow-id`) get existence validation: `if workflow_id is not None and get_workflow(...) is None: output_error(...)`
- All new parameters default to `None` (no breaking changes to existing invocations)
- Lazy imports maintained per CLAUDE.md conventions

### DB layer (database.py)

- Add optional parameters to `list_X()` functions
- Append `AND column = %(param)s` (or `>= %(param)s` for `--since`) to the query
- Dynamic query building matches the existing pattern in `list_activities()`
- All new parameters default to `None`

### No changes needed to

- `models.py` -- no new models or fields
- `schema.sql` -- no schema changes, filtering on existing columns
- `settings.py` -- no config changes

## CLI Signatures After Implementation

```
mailpilot email list [--limit N] [--contact-id ID] [--account-id ID] \
    [--since ISO] [--thread-id TEXT] [--direction inbound|outbound] \
    [--workflow-id ID] [--status sent|received|bounced]

mailpilot workflow list [--account-id ID] \
    [--status draft|active|paused] [--type inbound|outbound]

mailpilot contact list [--limit N] [--domain D] [--company-id ID] \
    [--status active|bounced|unsubscribed]

mailpilot note list --contact-id ID | --company-id ID [--limit N] \
    [--since ISO]
```

## CLAUDE.md Update

The CLI reference in CLAUDE.md must be updated to reflect the new options on all four commands.

## Testing

Each new filter gets a test that:
1. Creates records with varied values for the filtered column
2. Calls the list command with the filter
3. Asserts only matching records are returned

Existing tests are unaffected since all new parameters default to `None`.

## Acceptance Criteria

- All 9 filters work via CLI and return correct results
- FK filters (`--workflow-id`) validate entity existence before querying
- `--since` parses ISO datetime consistently with `activity list --since`
- `basedpyright` passes in strict mode
- `ruff check` passes
- All new and existing tests pass
- CLAUDE.md CLI reference updated
