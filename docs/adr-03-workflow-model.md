# ADR-03: Workflow Model

## Status

Accepted

## Context

MailPilot has two objectives:

1. **Outbound** -- send emails to contacts, handle replies per instructions
2. **Inbound** -- respond to incoming emails per instructions

Both require: an email account, a set of LLM instructions, and email tracking. The instructions are general -- they may use RAG, tool calling, or any other LLM capability. The abstraction must not assume a specific tool or pipeline.

## Decision

**Workflow** is the central abstraction. A workflow binds an account to a set of instructions and a direction (inbound or outbound). Each workflow is executed by a Pydantic AI agent.

### Workflow Types

**Outbound workflow:**

- Targets contacts individually via `workflow_contact`
- Agent generates email subject and body per the workflow's instructions
- Handles replies using the same instructions
- Status: `draft` -> `active` -> `paused`

**Inbound workflow:**

- Tracks contacts individually via `workflow_contact` (added automatically on classification)
- An account can have multiple active inbound workflows (e.g., product questions, billing, partnerships)
- Responds using the workflow's instructions
- Status: `draft` -> `active` -> `paused`

### State Model

Three levels of status tracking, from global to per-workflow:

1. **Contact status** -- can we email this person at all? (system-enforced)
2. **Email status** -- what happened to this message? (system-set)
3. **Workflow status** -- is this workflow running? (operational)
4. **Workflow-contact status** -- did we achieve this workflow's objective? (agent-driven)

**Contact status** (global, across all workflows):

| `status`       | Meaning                                    | Set by                     |
| -------------- | ------------------------------------------ | -------------------------- |
| `active`       | Can be emailed (default)                   | System                     |
| `bounced`      | Email invalid, delivery failed             | System (bounce detection)  |
| `unsubscribed` | Contact requested no further emails        | Agent via `disable_contact` |

The `send_email` tool checks `contact.status = 'active'` before sending -- same pattern as the cooldown guard. If the contact is not active, the tool refuses with a clear message. This is a hard block across all workflows.

`status_reason` holds the explanation ("hard bounce on 2026-04-10", "replied: do not contact me again").

**Email status** (what happened to this message?):

| `status`   | Meaning                            | Set by                    |
| ---------- | ---------------------------------- | ------------------------- |
| `sent`     | Delivered to Gmail API (outbound)  | System after send         |
| `received` | Synced from Gmail (inbound)        | System during sync        |
| `bounced`  | Delivery failed (outbound)         | System (bounce detection) |

No `draft` state -- the agent sends immediately via `send_email` tool, and inbound emails arrive already delivered. Set on creation based on direction.

Workflow status is purely operational. Outcome tracking lives on `workflow_contact`.

**Workflow status** (is this workflow running?):

| `status` | Behavior                                                              |
| -------- | --------------------------------------------------------------------- |
| `draft`  | Created, not running. Editing instructions and objective.             |
| `active` | Running. Outbound sends to pending contacts. Inbound receives emails. |
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
| `draft`  | `active` | `workflow activate ID` | instructions and objective must be non-empty |
| `active` | `paused` | `workflow pause ID`    | none                                         |
| `paused` | `active` | `workflow activate ID` | none                                         |

All other transitions are invalid. `draft -> paused` is meaningless (nothing to pause). `active -> draft` and `paused -> draft` are not allowed -- edit instructions while active or paused. No terminal state: workflows are paused, not deleted.

**Paused semantics:** A paused workflow still handles replies to existing threads (via thread match routing) and executes pending tasks. It does NOT accept new contacts (outbound) or classify new emails (inbound).

**Workflow-contact status** (did we achieve this workflow's objective with this contact?):

| `status`    | Meaning                                       |
| ----------- | --------------------------------------------- |
| `pending`   | Queued, not yet contacted                     |
| `active`    | Conversation in progress                      |
| `completed` | Agent determined objective achieved           |
| `failed`    | Agent determined objective cannot be achieved |

**Workflow-Contact status transitions:**

```
  pending -----> active -----> completed
                 ^ ^ |
          retry  | | | retry
                 | v |
                 failed
```

Agent has full discretion -- any transition is valid, including non-linear ones (`pending -> failed` on bounce, `completed -> active` on re-engagement, `failed -> active` on retry). No system-level guards. The agent must provide a `reason` explaining each transition via `update_contact_status`.

### Email Routing

See `docs/adr-04-email-routing.md`. Inbound emails are routed to workflows via thread match then LLM classification. Paused workflows receive replies to existing threads but are excluded from classification (no new conversations).

### Scope

A workflow is scoped: **account (1) -> workflow (N) -> contact (M via workflow_contact)**. The `workflow_contact` join table binds contacts to workflows and tracks per-workflow-contact outcome. The `reason` field holds the agent's explanation ("meeting booked for Tuesday", "contact explicitly declined", "no response after 3 follow-ups").

- **Outbound**: contacts are added before sending. `workflow send` queries `WHERE status = 'pending'`.
- **Inbound**: contacts are added automatically when classification routes an email.

A contact can be in multiple workflows (different accounts, different campaigns). The composite PK `(workflow_id, contact_id)` prevents duplicates within the same workflow.

### Objective

Each workflow has an `objective` -- a clear statement of what the agent is trying to achieve. The agent evaluates each interaction against this objective and updates the contact status accordingly.

Examples:

- Outbound sales: "Book a demo meeting"
- Inbound support: "Answer the product question"
- Inbound partnership: "Qualify and forward to sales team"

The agent uses the `update_contact_status` tool to report outcomes. The system never decides success or failure -- only the agent does.

## Agent Execution

Each workflow is executed by a Pydantic AI agent. The agent is **stateless** -- each invocation gets fresh context from the database. No persistent conversation history, no context window management. The agent makes all business decisions: what to send, when to follow up, when to give up.

### Events

The agent is invoked by three types of events:

| Event         | Trigger              | Agent receives                    |
| ------------- | -------------------- | --------------------------------- |
| Email arrives | Pub/Sub sync         | New email + workflow instructions |
| Task due      | Periodic task runner | Task description + context        |
| Manual send   | CLI command          | Contact list + instructions       |

### Concurrency

Multiple events can arrive for the same contact simultaneously (e.g., two emails in quick succession, or an email arriving while a task fires). Without coordination, parallel agent invocations would read the same database state and may produce duplicate replies.

**Per-contact mutex**: Before invoking the agent for a `(workflow_id, contact_id)` pair, acquire a PostgreSQL advisory lock keyed on that pair. If the lock is already held, skip the invocation -- the in-progress agent will see the new email when it reads context. The skipped event is not lost: the agent's next invocation (via task or next email) will pick it up.

This is a "skip if busy" pattern, not a queue. It avoids deadlocks and keeps the system simple. Advisory locks are automatically released when the connection/transaction ends.

### Contact History and Cooldown

When the agent processes a contact, it receives the **full email history between this account and this contact across all workflows** -- not just the current workflow's thread. This lets the agent make informed decisions ("we pitched them 45 days ago with no reply, adjust the angle" or "they replied negatively last month, skip").

The `send_email` tool enforces a **cooldown guard** on unsolicited outreach only:

- **Reply** (`thread_id` provided) -- always allowed, no cooldown. The contact wrote to us and deserves a response regardless of prior outreach history.
- **New conversation** (no `thread_id`) -- check the last unsolicited outbound email to this contact from this account. If sent within the cooldown period (configurable, default 43200 minutes / 30 days), refuse to send.

### Tools

The agent interacts with the system through tools only. Tool signatures below show only the parameters the **agent** passes. `workflow_id` and `account_id` are **injected by the system** via Pydantic AI dependency injection -- the agent always operates within a single workflow and account. This prevents the agent from accidentally acting on a different workflow.

Starting set:

- `send_email(to, subject, body, thread_id)` -- send via Gmail API. Guards: (1) contact must be `active` (not bounced/unsubscribed), (2) cooldown on new conversations only; replies always allowed
- `create_task(contact_id, description, scheduled_at, context, email_id)` -- schedule deferred work. `contact_id` is required (every task targets a contact); `email_id` is optional context
- `cancel_task(task_id)` -- cancel a pending task (e.g., follow-up no longer needed because the contact replied)
- `update_contact_status(contact_id, status, reason)` -- report per-workflow outcome (active, completed, failed)
- `disable_contact(contact_id, status, reason)` -- set global contact status to `bounced` or `unsubscribed`. Hard block across all workflows
- `search_emails(query)` -- query email history
- `list_workflow_contacts(workflow_id)` -- list contacts in the workflow with their status. Lets the agent coordinate across contacts (e.g., skip person B if person A at the same company already completed the objective)
- `read_contact(email)` / `read_company(domain)` -- CRM lookups

Additional tools (file access, web search, SQL queries) can be added per workflow as needed.

### Task Planning

When the agent cannot complete work in a single invocation, it creates a **task** -- a deferred action with a scheduled execution time. Tasks are the only planning mechanism.

Examples:

- "Follow up with contact X in 5 days if no reply"
- "Send the next batch of outbound emails tomorrow at 9am"
- "Re-check this thread in 2 hours for a response"

The task runner is a periodic loop alongside the sync loop. For each due task: load the workflow, invoke the agent with the task context, mark task as completed or failed.

See `docs/email-flow.md` for detailed execution flows.

## Schema

See `src/mailpilot/schema.sql`. Tables: `workflow`, `workflow_contact`, `task`, and `workflow_id` on `email`.

## Consequences

### Positive

- One abstraction for both objectives -- no campaign/responder split
- Instructions are general -- no assumption about RAG, tools, or pipeline
- Multiple inbound workflows per account -- flexible routing by business purpose
- Per-account isolation maintained -- workflows are scoped to accounts
- Stateless agent invocations -- simple, predictable, no state management
- Task-based planning -- deferred work is just database rows with timestamps, no complex orchestration
- Two-level status model -- global contact blocks (system-enforced) + per-workflow outcomes (agent-driven)

### Negative

- Agent-driven outcomes depend on LLM quality -- mitigated by well-crafted workflow objectives and instructions
- Stateless invocations mean the agent re-reads context each time (acceptable -- database reads are cheap, and it avoids stale state)
- See `docs/adr-04-email-routing.md` for routing-specific trade-offs
