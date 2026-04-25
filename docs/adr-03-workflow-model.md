# ADR-03: Workflow Model

## Status

Updated 2026-04-25. Field definitions formerly in ADR-06 are folded into this ADR. The join entity formerly named `workflow_contact` was renamed to `enrollment` in the same change.

## Context

MailPilot has two objectives:

1. **Outbound** -- send emails to contacts, handle replies per instructions
2. **Inbound** -- respond to incoming emails per instructions

Both require: an email account, a set of LLM instructions, and email tracking. The instructions are general -- they may use RAG, tool calling, or any other LLM capability. The abstraction must not assume a specific tool or pipeline.

## Decision

**Workflow** is the central abstraction. A workflow binds an account to a set of instructions and a direction (inbound or outbound). Each workflow is executed by a Pydantic AI agent.

### Workflow Types

**Outbound workflow:**

- Targets contacts individually via `enrollment`
- Agent generates email subject and body per the workflow's instructions
- Handles replies using the same instructions
- Status: `draft` -> `active` -> `paused`

**Inbound workflow:**

- Tracks contacts individually via `enrollment` (added automatically on classification)
- An account can have multiple active inbound workflows (e.g., product questions, billing, partnerships)
- Responds using the workflow's instructions
- Status: `draft` -> `active` -> `paused`

### State Model

Three levels of status tracking, from global to per-enrollment:

1. **Contact status** -- can we email this person at all? (system-enforced)
2. **Email status** -- what happened to this message? (system-set)
3. **Workflow status** -- is this workflow running? (operational)
4. **Enrollment status** -- did we achieve this workflow's objective for this contact? (agent-driven)

**Contact status** (global, across all workflows):

| `status`       | Meaning                                    | Set by                     |
| -------------- | ------------------------------------------ | -------------------------- |
| `active`       | Can be emailed (default)                   | System                     |
| `bounced`      | Email invalid, delivery failed             | System (bounce detection)  |
| `unsubscribed` | Contact requested no further emails        | Agent via `disable_contact` |

The `send_email` tool checks `contact.status = 'active'` before sending. If the contact is not active, the tool refuses with a clear message. This is a hard block across all workflows. `status_reason` holds the explanation ("hard bounce on 2026-04-10", "replied: do not contact me again").

**Email status** (what happened to this message?):

| `status`   | Meaning                            | Set by                    |
| ---------- | ---------------------------------- | ------------------------- |
| `sent`     | Delivered to Gmail API (outbound)  | System after send         |
| `received` | Synced from Gmail (inbound)        | System during sync        |
| `bounced`  | Delivery failed (outbound)         | System (bounce detection) |

**Workflow status** (is this workflow running?):

| `status` | Behavior                                                              |
| -------- | --------------------------------------------------------------------- |
| `draft`  | Created, not running. Editing instructions and objective.             |
| `active` | Running. Outbound sends to pending enrollments. Inbound receives emails. |
| `paused` | No new work. Existing threads still handled (no ghosting mid-thread). |

**Workflow status transitions:**

```
         activate          pause
  draft ---------> active --------> paused
                     ^                 |
                     |     resume      |
                     +-----------------+
```

| From     | To       | Trigger                | Guard                                        |
| -------- | -------- | ---------------------- | -------------------------------------------- |
| `draft`  | `active` | `workflow start ID`    | instructions and objective must be non-empty |
| `active` | `paused` | `workflow stop ID`     | none                                         |
| `paused` | `active` | `workflow start ID`    | none                                         |

All other transitions are invalid. `draft -> paused` is meaningless (nothing to pause). `active -> draft` and `paused -> draft` are not allowed -- edit instructions while active or paused. No terminal state: workflows are paused, not deleted.

**Paused semantics:** A paused workflow still handles replies to existing threads (via thread match routing) and executes pending tasks. It does NOT accept new contacts (outbound) or classify new emails (inbound).

**Enrollment status** (did we achieve this workflow's objective for this contact?):

| `status`    | Meaning                                       |
| ----------- | --------------------------------------------- |
| `pending`   | Queued, not yet contacted                     |
| `active`    | Conversation in progress                      |
| `completed` | Agent determined objective achieved           |
| `failed`    | Agent determined objective cannot be achieved |

**Enrollment status transitions:**

```
  pending -----> active -----> completed
                 ^ ^ |
          retry  | | | retry
                 | v |
                 failed
```

The agent has full discretion -- any transition is valid, including non-linear ones (`pending -> failed` on bounce, `completed -> active` on re-engagement, `failed -> active` on retry). No system-level guards. The agent must provide a `reason` explaining each transition via `update_enrollment_status`.

The system makes one automatic transition: `_activate_enrollment_if_pending` flips `pending -> active` when a successful outbound send occurs. All other transitions are agent-driven.

### Email Routing

See `docs/adr-04-email-routing.md`. Inbound emails are routed to workflows via thread match then LLM classification. Paused workflows receive replies to existing threads but are excluded from classification (no new conversations). Successful routing creates an `enrollment` row if one does not already exist.

### Scope

A workflow is scoped: **account (1) -> workflow (N) -> contact (M via enrollment)**. The `enrollment` join table binds contacts to workflows and tracks per-enrollment outcome. The `reason` field holds the agent's explanation ("meeting booked for Tuesday", "contact explicitly declined", "no response after 3 follow-ups").

- **Outbound**: contacts are added before sending. `enrollment run` requires the contact to be enrolled.
- **Inbound**: contacts are enrolled automatically when classification routes an email.

A contact can be enrolled in multiple workflows (different accounts, different campaigns). The composite PK `(workflow_id, contact_id)` prevents duplicates within the same workflow.

### Field definitions

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
| Consumers  | Agent outcome evaluation, classifier routing, `update_enrollment_status` decisions |
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

**`theme`** -- Email rendering palette.

| Property   | Value                                               |
| ---------- | --------------------------------------------------- |
| SQL        | `TEXT NOT NULL DEFAULT 'blue'`                       |
| Mutability | Mutable via `update_workflow()`                     |
| Consumers  | `email_renderer.render_html()` for outbound emails  |
| Format     | One of `THEME_NAMES` (blue, green, orange, purple, red, slate) |

**Examples**

Outbound -- Sales:

```
type:         outbound
name:         Series-A CTO Outreach
objective:    Book a 30-minute demo meeting
instructions: You are a sales development representative for Acme DevOps.
              Your goal is to get the contact to agree to a 30-minute demo.
              Be professional but conversational. Reference their company's
              tech stack if available. If they express interest, suggest
              specific times this week. If they decline, thank them and
              mark the enrollment as failed. Follow up once after 5 days
              if no reply.
```

Inbound -- Support:

```
type:         inbound
name:         Product Questions
objective:    Answer the question and offer a demo
instructions: You are a product specialist for Acme DevOps.
              Answer product questions accurately. If the question is
              about pricing, share the pricing page link. After answering,
              offer to schedule a demo. If the inquiry is about billing
              or a bug report, mark the enrollment as failed with reason
              "wrong workflow -- billing/bug".
```

### Status transition enforcement

Status updates on `workflow` flow through dedicated functions, not generic `update_workflow()`:

- **`activate_workflow(connection, workflow_id)`** -- transitions `draft -> active` or `paused -> active`. Guards: `objective` and `instructions` must be non-empty (stripped). Returns the updated workflow or raises `ValueError`.
- **`pause_workflow(connection, workflow_id)`** -- transitions `active -> paused`. No guard. Returns the updated workflow or raises `ValueError`.

`update_workflow()` accepts only `{"name", "objective", "instructions", "theme"}`. It does not accept `status`, `type`, or `account_id`.

## Agent Execution

Each workflow is executed by a Pydantic AI agent. The agent is **stateless** -- each invocation gets fresh context from the database. No persistent conversation history, no context window management. The agent makes all business decisions: what to send, when to follow up, when to give up.

### Events

The agent is invoked by three types of events:

| Event         | Trigger              | Agent receives                    |
| ------------- | -------------------- | --------------------------------- |
| Email arrives | Pub/Sub sync         | New email + workflow instructions |
| Task due      | Periodic task runner | Task description + context        |
| Manual run    | `mailpilot enrollment run` | Contact + instructions          |

### Concurrency

Multiple events can arrive for the same `(workflow_id, contact_id)` pair simultaneously. Without coordination, parallel agent invocations would read the same database state and may produce duplicate replies.

**Per-pair mutex**: Before invoking the agent for a `(workflow_id, contact_id)` pair, acquire a PostgreSQL advisory lock keyed on that pair (CRC-32 of each ID, two-argument advisory lock). If the lock is already held, skip the invocation -- the in-progress agent will see the new email when it reads context. The skipped event is not lost: the agent's next invocation (via task or next email) will pick it up.

This is a "skip if busy" pattern, not a queue. Advisory locks are released when the connection ends.

### Contact history and cooldown

When the agent processes a contact, it receives the **email history scoped to this workflow + contact** -- the `invoke_workflow_agent` flow loads `list_emails(contact_id=, account_id=, workflow_id=)`. This keeps the prompt focused on the current workflow's conversation thread.

The `send_email` tool enforces a **cooldown guard** on unsolicited outreach only:

- **Reply** (use `reply_email` tool) -- always allowed, no cooldown. The contact wrote to us and deserves a response regardless of prior outreach history.
- **New conversation** (`send_email`) -- check the last unsolicited outbound email to this contact from this account. If sent within the cooldown period (currently 30 days, defined as `_COOLDOWN_DAYS` in `agent/tools.py`), refuse to send.

### Tools

The agent interacts with the system through tools only. Tool signatures below show only the parameters the **agent** passes. `workflow_id` and `account` are **injected by the system** via Pydantic AI dependency injection (`AgentDeps`) -- the agent always operates within a single workflow and account.

Tools registered in `_TOOLS` (in `agent/invoke.py`):

| Tool                          | Mutates    | Purpose                                                            |
| ----------------------------- | ---------- | ------------------------------------------------------------------ |
| `send_email`                  | `email`    | Send a new outbound email. Guards: contact active, cooldown.       |
| `reply_email`                 | `email`    | Reply in-thread to an existing email. Always allowed.              |
| `create_task`                 | `task`     | Schedule deferred work for later execution.                        |
| `cancel_task`                 | `task`     | Cancel a pending task.                                             |
| `update_enrollment_status`    | `enrollment` | Report per-workflow outcome (active, completed, failed).         |
| `disable_contact`             | `contact`  | Set global contact block (`bounced` or `unsubscribed`).            |
| `list_enrollments`            | -          | List all enrollments in this workflow with their status.           |
| `search_emails`               | -          | Query email history for the current account.                       |
| `read_contact`                | -          | CRM lookup by email.                                               |
| `read_company`                | -          | CRM lookup by domain.                                              |
| `noop`                        | -          | Explicit "no action needed" -- still counts as a tool call.        |

The agent must call at least one tool per run. A run with zero tool calls raises `AgentDidNotUseToolsError`. `noop(reason)` is the explicit escape hatch.

### Task planning

When the agent cannot complete work in a single invocation, it creates a **task** -- a deferred action with a scheduled execution time. Tasks are the only planning mechanism.

Examples:

- "Follow up with contact X in 5 days if no reply"
- "Send the next batch of outbound emails tomorrow at 9am"
- "Re-check this thread in 2 hours for a response"

Task INSERT fires PG `NOTIFY task_pending`. The sync loop's `LISTEN` handler wakes immediately and drains due tasks; a periodic timer is the upper-bound fallback.

See `docs/email-flow.md` for detailed execution flows.

## Schema

See `src/mailpilot/schema.sql`. Workflow-related tables: `workflow`, `enrollment`, `task`, plus `workflow_id` columns on `email` and `task`.

## Consequences

### Positive

- One abstraction for both objectives -- no campaign/responder split
- Instructions are general -- no assumption about RAG, tools, or pipeline
- Multiple inbound workflows per account -- flexible routing by business purpose
- Per-account isolation maintained -- workflows are scoped to accounts
- Stateless agent invocations -- simple, predictable, no state management
- Task-based planning -- deferred work is just database rows with timestamps
- Two-level status model -- global contact blocks (system-enforced) + per-enrollment outcomes (agent-driven)
- Each workflow field has a precise contract: who writes it, who reads it, when required
- CHECK constraints enforce valid values at the database level regardless of client
- Immutability of `type` and `account_id` prevents corruption of FK relationships
- Dedicated `activate_workflow()` / `pause_workflow()` enforce the state machine
- `UNIQUE (account_id, name)` prevents confusing duplicates and improves classification reliability

### Negative

- Agent-driven outcomes depend on LLM quality -- mitigated by well-crafted workflow objectives and instructions
- Stateless invocations mean the agent re-reads context each time (acceptable -- database reads are cheap, and it avoids stale state)
- See `docs/adr-04-email-routing.md` for routing-specific trade-offs
