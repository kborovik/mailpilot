# ADR-06: Workflow Field Definitions

## Status

Accepted

Amends ADR-03 (Workflow Model).

## Context

ADR-03 introduced the workflow model with four content fields (`type`, `name`, `objective`, `instructions`) and a `description` column. ADR-04 specified that the email classifier consumes `(name, description, objective)` for routing decisions.

A gap analysis revealed:

1. **`description` overlaps with `objective`** -- both are human-readable text about the workflow's purpose. The classifier can route effectively with `name` + `objective` alone. `description` adds cognitive load without clear payoff.
2. **No CHECK constraints** -- `type` and `status` are `TEXT NOT NULL` without database-level validation. Invalid values are only caught by Pydantic on read, not on write.
3. **`type` and `account_id` are mutable** -- `update_workflow()` allows changing both, contradicting ADR-03 (type is set on creation) and risking FK corruption (email/task/workflow_contact reference the workflow).
4. **Activation guards are documented but unenforced** -- ADR-03 specifies that `objective` and `instructions` must be non-empty for `draft -> active`, but `update_workflow()` accepts any status value without validation.
5. **Status transitions are unenforced** -- the state machine in ADR-03 (`draft -> active -> paused`, `paused -> active`) is not checked in code.
6. **No name uniqueness** -- two workflows in the same account can share a name, confusing both users and the classifier.

## Decision

### Remove `description`

Drop the `description` column. The classifier routes on `(name, objective)`. The `name` provides identity ("Series-A CTO Outreach"), the `objective` provides purpose ("Book a 30-minute demo meeting"). No third field is needed.

### Field Contracts

**`type`** -- Workflow direction.

| Property   | Value                                          |
| ---------- | ---------------------------------------------- |
| SQL        | `TEXT NOT NULL CHECK (type IN ('inbound', 'outbound'))` |
| Pydantic   | `Literal["inbound", "outbound"]`               |
| Set at     | Creation only                                  |
| Mutability | Immutable                                      |
| Consumers  | Run guard (outbound check), classification filter, email-flow routing |

**`name`** -- Human-readable workflow identifier.

| Property   | Value                                         |
| ---------- | --------------------------------------------- |
| SQL        | `TEXT NOT NULL`, `UNIQUE (account_id, name)`  |
| Mutability | Mutable via `update_workflow()`               |
| Consumers  | CLI listing, classifier routing (identity signal) |
| Format     | Free text, descriptive of audience and channel |

**`objective`** -- Concise agent goal statement.

| Property   | Value                                                |
| ---------- | ---------------------------------------------------- |
| SQL        | `TEXT NOT NULL DEFAULT ''`                            |
| Activation | Required (must be non-empty after stripping)         |
| Consumers  | Agent outcome evaluation, classifier routing, `update_contact_status` decisions |
| Format     | Imperative phrase starting with a verb: "Book...", "Answer...", "Qualify...", "Resolve..." |
| Guidance   | One sentence, under 100 characters. Outcome-oriented (what success looks like), not process-oriented (how to achieve it -- that is what `instructions` is for) |

**`instructions`** -- Agent system prompt.

| Property   | Value                                               |
| ---------- | --------------------------------------------------- |
| SQL        | `TEXT NOT NULL DEFAULT ''`                           |
| Activation | Required (must be non-empty after stripping)        |
| Consumers  | `invoke_workflow_agent()` -- passed as system prompt |
| Format     | Free-form text, no structured format imposed         |
| Guidance   | Complete instructions for agent behavior: tone, rules, escalation criteria, tool usage hints. The agent receives this on every invocation alongside fresh database context |

### Examples

**Outbound -- Sales:**

```
type:         outbound
name:         Series-A CTO Outreach
objective:    Book a 30-minute demo meeting
instructions: You are a sales development representative for Acme DevOps.
              Your goal is to get the contact to agree to a 30-minute demo.
              Be professional but conversational. Reference their company's
              tech stack if available. If they express interest, suggest
              specific times this week. If they decline, thank them and
              mark as failed. Follow up once after 5 days if no reply.
```

**Outbound -- Re-engagement:**

```
type:         outbound
name:         Webinar Follow-up
objective:    Get a reply expressing interest in a trial
instructions: Follow up with attendees who registered but did not
              convert. Reference the webinar topic. Offer a free trial.
              If no reply after one follow-up, mark as failed.
```

**Inbound -- Support:**

```
type:         inbound
name:         Product Questions
objective:    Answer the question and offer a demo
instructions: You are a product specialist for Acme DevOps.
              Answer product questions accurately. If the question is
              about pricing, share the pricing page link. After answering,
              offer to schedule a demo. If the inquiry is about billing
              or a bug report, mark as failed with reason
              "wrong workflow -- billing/bug".
```

**Inbound -- Routing:**

```
type:         inbound
name:         Partnership Inquiries
objective:    Qualify the opportunity and forward to partnerships@acme.com
instructions: You are the first point of contact for partnership inquiries.
              Ask qualifying questions: what is their product, what
              integration are they looking for, what is their user base.
              Once qualified, forward the thread to partnerships@acme.com
              with a summary. Mark as completed after forwarding.
              If clearly spam or irrelevant, mark as failed.
```

**Inbound -- Triage:**

```
type:         inbound
name:         Billing Support
objective:    Resolve the billing issue or escalate to support
instructions: Triage billing and subscription issues from existing
              customers. Check the contact's company for context.
              For simple questions (upgrade, cancel, invoice), answer
              directly. For complex issues (refunds, disputes), escalate
              to support@acme.com and mark as completed.
```

### Status Transition Enforcement

Replace generic `status` updates in `update_workflow()` with dedicated functions:

- **`activate_workflow(connection, workflow_id)`** -- transitions `draft -> active` or `paused -> active`. Guards: `objective` and `instructions` must be non-empty (stripped). Returns the updated workflow or raises `ValueError`.
- **`pause_workflow(connection, workflow_id)`** -- transitions `active -> paused`. No guard. Returns the updated workflow or raises `ValueError`.

`update_workflow()` no longer accepts `status` as a field. Only `name`, `objective`, and `instructions` are updatable.

Invalid transitions raise `ValueError` with a message like `"cannot activate workflow in status 'active'"`.

### Schema Changes

Applied to `schema.sql` (drop-and-recreate on `make clean`):

```sql
-- workflow table: remove description, add CHECK constraints, add UNIQUE
CREATE TABLE IF NOT EXISTS workflow (
    id                TEXT PRIMARY KEY,
    account_id        TEXT NOT NULL REFERENCES account(id),
    type              TEXT NOT NULL CHECK (type IN ('inbound', 'outbound')),
    name              TEXT NOT NULL,
    objective         TEXT NOT NULL DEFAULT '',
    instructions      TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft', 'active', 'paused')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (account_id, name)
);
```

CHECK constraints on other tables (same gap):

```sql
-- contact.status
CHECK (status IN ('active', 'bounced', 'unsubscribed'))

-- workflow_contact.status
CHECK (status IN ('pending', 'active', 'completed', 'failed'))

-- email.direction
CHECK (direction IN ('inbound', 'outbound'))

-- email.status
CHECK (status IN ('sent', 'received', 'bounced'))

-- task.status
CHECK (status IN ('pending', 'completed', 'failed', 'cancelled'))
```

### Code Changes

**`database.py`:**

- `update_workflow()` allowed set: `{"name", "objective", "instructions"}`
- New: `activate_workflow(connection, workflow_id) -> Workflow`
- New: `pause_workflow(connection, workflow_id) -> Workflow`

**`models.py`:**

- Remove `description: str = ""` from `Workflow`

**`agent/classify.py`:**

- Update docstring: classifier receives `(name, objective)`, not `(name, description, objective)`

**`docs/adr-03-workflow-model.md`:**

- Add "Amended by ADR-06" note in Status section

**`docs/email-flow.md`:**

- Update classifier input: `(name, objective)` instead of `(name, description, objective)`

## Consequences

### Positive

- Each field has a precise contract: who writes it, who reads it, when required
- `description` removal eliminates semantic overlap and simplifies the model
- CHECK constraints enforce valid values at the database level regardless of client
- Immutability of `type` and `account_id` prevents corruption of FK relationships
- Dedicated `activate_workflow()` / `pause_workflow()` functions enforce the state machine documented in ADR-03
- `UNIQUE (account_id, name)` prevents confusing duplicates and improves classification reliability
- Concrete examples for both workflow types reduce ambiguity

### Negative

- Existing code referencing `description` must be updated (model, database, tests)
- `update_workflow()` becomes more restrictive -- callers that set status directly must switch to `activate_workflow()` / `pause_workflow()`
- CHECK constraints across all tables mean any new enum value requires a schema change
