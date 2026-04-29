# ADR-08: CRM Design -- Activity Timeline, Tags, and Notes

## Status

Accepted.

## Context

MailPilot is a CRM with Gmail as the primary communication channel. Claude Code is the strategic orchestrator and primary operator; an internal Pydantic AI agent handles real-time tactical work within workflows.

To support relationship reporting, segmentation, and freeform annotation, the schema includes three tables beyond the core (account, company, contact, workflow, enrollment, email, task, sync_status):

- `activity` -- unified per-contact and per-company timeline.
- `tag` -- flexible label on contacts or companies.
- `note` -- freeform text annotation on contacts or companies.

## Decision

### Identity

MailPilot is a CRM. Gmail is the channel. The CLI is the operator surface for Claude Code.

### `activity` -- typed FKs, contact-or-company

Every significant event is an immutable row. Either `contact_id` or `company_id` (or both) is set. Structured FK columns -- `email_id`, `workflow_id`, `task_id` -- let reports join activity to source records without parsing JSON. `detail` JSONB remains for type-specific metadata that does not need to be queryable as a column.

Composite indexes `idx_activity_contact_timeline (contact_id, created_at DESC)` and `idx_activity_company_timeline (company_id, created_at DESC)` serve the common "newest-first timeline for this entity" query.

Activity vocabulary:

- `email_sent`, `email_received` -- send/receive on the Gmail channel.
- `note_added` -- note created on contact or company.
- `tag_added`, `tag_removed` -- tag mutations.
- `status_changed` -- contact-level lifecycle (bounced, unsubscribed).
- `enrollment_added` -- contact joined a workflow.
- `enrollment_completed`, `enrollment_failed` -- agent-declared outcomes (timeline-only, not state on enrollment).
- `enrollment_paused`, `enrollment_resumed` -- operator pause/resume.

### `tag` -- typed nullable FKs (no polymorphism)

`tag.contact_id` and `tag.company_id` are both nullable. A CHECK constraint enforces XOR (exactly one is set). Two partial unique indexes on `(contact_id, name)` and `(company_id, name)` prevent duplicates within an owner.

Tag names are normalized to lowercase, hyphenated form via `_normalize_tag_name`: whitespace and underscores collapse to single hyphens, repeated hyphens collapse, leading/trailing hyphens are trimmed. The result must match `[a-z0-9][a-z0-9-]*`. Names that fail validation raise `ValueError` -- there is no silent salvage.

### `note` -- typed nullable FKs

Same XOR pattern as `tag`. Notes are append-only.

### `enrollment.status` -- operational only

States: `active` (agent considers this contact when the workflow runs) and `paused` (operator/agent has suspended). Outcomes (`completed`, `failed`) live entirely in the activity timeline as `enrollment_completed` / `enrollment_failed` events. The relationship persists across declared outcomes -- a late inbound reply does not require a state transition.

### Atomic timeline writes

Combined helpers in `database.py` write the domain row and its activity in a single transaction:

- `add_contact_tag` / `add_company_tag` / `remove_contact_tag` / `remove_company_tag`
- `add_contact_note` / `add_company_note`

CLI commands and other callers use these helpers instead of two separate writes.

### Activity creation in runtime paths

| Trigger                          | Module                                       | Activity                                                        |
| -------------------------------- | -------------------------------------------- | --------------------------------------------------------------- |
| Outbound send                    | `email_ops.py`                               | `email_sent` (with `email_id`, `workflow_id` FKs)               |
| Inbound store                    | `sync.py`                                    | `email_received` (with `email_id` FK)                           |
| Routing -> enrollment created    | `routing.py`                                 | `enrollment_added` (with `workflow_id` FK)                      |
| Agent declares outcome           | `agent/tools.py` `record_enrollment_outcome` | `enrollment_completed` / `enrollment_failed`                    |
| CLI `enrollment update`          | `cli.py`                                     | `enrollment_paused` / `enrollment_resumed` (on transition only) |
| CLI `tag add/remove`, `note add` | `cli.py` via atomic helper                   | `tag_added` / `tag_removed` / `note_added`                      |

## Consequences

### Positive

- One unified timeline for relationship reporting, joinable to source records via real FKs.
- Maximum segmentation flexibility through tags; no premature commitment to pipeline stages.
- Database-level referential integrity on every CRM relationship (no orphans from polymorphic FKs).
- Simple enrollment state -- two states, no terminal/reactivation problem; outcomes are timeline events.
- Atomic timeline writes prevent CRM/timeline divergence on partial failures.
- Strict tag normalization prevents operational drift (`Hot Lead`, `hot_lead`, `hot--lead` collapse to a single canonical form).

### Negative

- "Which enrollments completed?" requires querying the activity timeline rather than filtering enrollment.status. Acceptable at solo-consultant volume; can be optimized with a materialized view if it becomes a bottleneck.
- Activity table grows unboundedly. Acceptable for current volume; if it exceeds 100K rows, add retention or archival.
- Tag normalization rejects some inputs that the previous lenient `strip().lower()` would have silently accepted. CLI surfaces a `validation_error` so the operator can correct.
