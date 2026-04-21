# Workflow Contact CLI

## Context

The `workflow_contact` junction table exists with full CRUD in the database layer (`create_workflow_contact`, `get_workflow_contact`, `list_workflow_contacts`, `update_workflow_contact`), but has zero CLI exposure. The only command that touches `workflow_contact` is `workflow run`, which requires enrollment to already exist. To enroll a contact, the operator had to drop to raw SQL.

This blocks the primary use case: Claude Code orchestrating outbound campaigns (cold-emailing 10-50 contacts) and reviewing inbound workflow outcomes -- entirely through the CLI.

## CLI Commands

Four new commands under a `workflow contact` subgroup:

```
mailpilot workflow contact add    --workflow-id ID --contact-id ID
mailpilot workflow contact remove --workflow-id ID --contact-id ID
mailpilot workflow contact list   --workflow-id ID [--status pending|active|completed|failed] [--limit N]
mailpilot workflow contact update --workflow-id ID --contact-id ID --status S [--reason R]
```

### `workflow contact add`

Enroll a contact in a workflow. Works for both inbound and outbound workflows.

**Validation:**

1. `--workflow-id` and `--contact-id` are required.
2. Workflow must exist (`get_workflow`). Error: `not_found`.
3. Contact must exist (`get_contact`). Error: `not_found`.

**Behavior:** Calls `create_workflow_contact(conn, workflow_id, contact_id)`. Default status is `pending`. Idempotent -- if enrollment already exists (`create_workflow_contact` returns `None`), fetch and return the existing row via `get_workflow_contact`. No error on duplicate.

**Output:**

```json
{
  "workflow_id": "...",
  "contact_id": "...",
  "status": "pending",
  "reason": "",
  "created_at": "...",
  "updated_at": "...",
  "ok": true
}
```

### `workflow contact remove`

Remove a contact from a workflow.

**Validation:**

1. `--workflow-id` and `--contact-id` are required.
2. Enrollment must exist. Error: `not_found` with message "workflow-contact not found".

**Behavior:** Calls new `delete_workflow_contact(conn, workflow_id, contact_id)`.

**Output:**

```json
{
  "workflow_id": "...",
  "contact_id": "...",
  "ok": true
}
```

### `workflow contact list`

List contacts enrolled in a workflow with enriched contact info.

**Options:**

- `--workflow-id ID` (required)
- `--status pending|active|completed|failed` (optional filter)
- `--limit N` (default: 100)

**Validation:**

1. Workflow must exist (`get_workflow`). Error: `not_found`.

**Behavior:** Calls modified `list_workflow_contacts` that JOINs the `contact` table to include `contact_email` and `contact_name`.

**Output:**

```json
{
  "contacts": [
    {
      "workflow_id": "...",
      "contact_id": "...",
      "contact_email": "alice@co.com",
      "contact_name": "Alice Smith",
      "status": "pending",
      "reason": "",
      "created_at": "...",
      "updated_at": "..."
    }
  ],
  "ok": true
}
```

### `workflow contact update`

Update enrollment status and reason.

**Options:**

- `--workflow-id ID` (required)
- `--contact-id ID` (required)
- `--status pending|active|completed|failed` (required)
- `--reason TEXT` (optional)

**Validation:**

1. `--status` is required (nothing to update without it).
2. Enrollment must exist (`update_workflow_contact` returns `None`). Error: `not_found`.

**Behavior:** Calls `update_workflow_contact(conn, workflow_id, contact_id, status=status, reason=reason)`. Only passes `reason` if provided.

**Output:**

```json
{
  "workflow_id": "...",
  "contact_id": "...",
  "status": "completed",
  "reason": "Demo booked",
  "created_at": "...",
  "updated_at": "...",
  "ok": true
}
```

## Database Layer

### Existing functions (no changes)

- `create_workflow_contact(conn, workflow_id, contact_id) -> WorkflowContact | None`
- `get_workflow_contact(conn, workflow_id, contact_id) -> WorkflowContact | None`
- `update_workflow_contact(conn, workflow_id, contact_id, **fields) -> WorkflowContact | None`

### New function (enriched list)

`list_workflow_contacts_enriched(conn, workflow_id, status=None, limit=100) -> list[WorkflowContactDetail]`

Separate function from `list_workflow_contacts` because the original is used by the agent tools layer and must keep returning `list[WorkflowContact]`. The CLI uses this enriched variant.

```sql
SELECT wc.*, c.email AS contact_email,
       TRIM(COALESCE(c.first_name, '') || ' ' || COALESCE(c.last_name, '')) AS contact_name
FROM workflow_contact wc
JOIN contact c ON c.id = wc.contact_id
WHERE wc.workflow_id = %(workflow_id)s
  [AND wc.status = %(status)s]
ORDER BY wc.created_at
LIMIT %(limit)s
```

The existing `list_workflow_contacts` is unchanged (agent tools keep using it).

### New function

`delete_workflow_contact(conn, workflow_id, contact_id) -> bool`

```sql
DELETE FROM workflow_contact
WHERE workflow_id = %(workflow_id)s AND contact_id = %(contact_id)s
RETURNING workflow_id
```

Returns `True` if a row was deleted, `False` if not found. Commits on success.

## Model

New model in `models.py`:

```python
class WorkflowContactDetail(BaseModel):
    """Enriched workflow-contact with contact info for list display."""

    workflow_id: str
    contact_id: str
    contact_email: str
    contact_name: str
    status: ContactOutcome
    reason: str
    created_at: datetime
    updated_at: datetime
```

## CLAUDE.md Update

Add to CLI reference under the workflow section:

```
mailpilot workflow contact add --workflow-id ID --contact-id ID
mailpilot workflow contact remove --workflow-id ID --contact-id ID
mailpilot workflow contact list --workflow-id ID [--status pending|active|completed|failed] [--limit N]
mailpilot workflow contact update --workflow-id ID --contact-id ID --status S [--reason R]
```

## End-to-End Scenarios

### Outbound cold email (10-50 contacts)

```bash
# 1. Create and configure workflow
mailpilot workflow create --name "Q2 Outreach" --type outbound \
  --account-id $ACCT --objective "Book demo" --instructions-file campaign.md
mailpilot workflow activate $WF

# 2. Enroll contacts (Claude Code loops)
mailpilot workflow contact add --workflow-id $WF --contact-id $C1
mailpilot workflow contact add --workflow-id $WF --contact-id $C2

# 3. Verify enrollment
mailpilot workflow contact list --workflow-id $WF --status pending

# 4. Run per contact (Claude Code loops)
mailpilot workflow run --workflow-id $WF --contact-id $C1
mailpilot workflow run --workflow-id $WF --contact-id $C2

# 5. Review outcomes
mailpilot workflow contact list --workflow-id $WF --status completed
mailpilot workflow contact list --workflow-id $WF --status failed
```

### Inbound auto-reply (demo@lab5.ca scenario)

```bash
# 1. Create account and inbound workflow
mailpilot account create --email demo@lab5.ca --display-name "Demo"
mailpilot workflow create --name "Product Q&A" --type inbound \
  --account-id $ACCT --objective "Answer product questions" \
  --instructions-file demo-agent.md
mailpilot workflow activate $WF

# 2. Optionally pre-seed key accounts
mailpilot workflow contact add --workflow-id $WF --contact-id $KEY_ACCT

# 3. Start watching for emails (routing auto-enrolls unknown senders)
mailpilot run

# 4. Review who has been served
mailpilot workflow contact list --workflow-id $WF
mailpilot workflow contact list --workflow-id $WF --status completed
```

## Verification

1. `make check` -- all lint, types, and tests pass.
2. Manual test against default database:
   - `mailpilot workflow contact add --workflow-id $WF --contact-id $C` -- enrolls contact.
   - `mailpilot workflow contact add --workflow-id $WF --contact-id $C` -- idempotent, returns same row.
   - `mailpilot workflow contact list --workflow-id $WF` -- shows enriched output with email/name.
   - `mailpilot workflow contact list --workflow-id $WF --status pending` -- filters by status.
   - `mailpilot workflow contact update --workflow-id $WF --contact-id $C --status completed --reason "Demo booked"` -- updates status.
   - `mailpilot workflow contact remove --workflow-id $WF --contact-id $C` -- removes enrollment.
   - `mailpilot workflow contact remove --workflow-id $WF --contact-id $C` -- returns not_found.
3. `mailpilot workflow contact list --workflow-id BOGUS` -- returns not_found error.
4. `mailpilot workflow contact add --workflow-id $WF --contact-id BOGUS` -- returns not_found error.
