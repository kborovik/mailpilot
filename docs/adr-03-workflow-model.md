# ADR-03: Workflow Model

## Status

Accepted

## Context

MailPilot has two objectives:

1. **Outbound** -- send emails to contacts, handle replies per instructions
2. **Inbound** -- respond to incoming emails per instructions

Both require: an email account, a set of LLM instructions, and email tracking. The instructions are general -- they may use RAG, tool calling, or any other LLM capability. The abstraction must not assume a specific tool or pipeline.

## Decision

**Workflow** is the central abstraction. A workflow binds an account to a set of instructions and a direction (inbound or outbound). Each workflow is executed by a Pydantic AI agent (`pydantic-ai-slim[anthropic]`).

### Workflow Types

**Outbound workflow:**

- Has a target list of contacts
- Sends emails using a subject/body template
- Replies to responses using the workflow's instructions
- Status: `draft` -> `active` -> `paused` -> `completed`

**Inbound workflow:**

- Registered on an account to handle unsolicited emails
- Any incoming email that doesn't match an existing thread is routed here
- Responds using the workflow's instructions
- Status: `draft` -> `active` -> `paused`

### Constraints

- An account can have **at most one active inbound workflow**. If an unsolicited email arrives, it must route to exactly one handler. Multiple active inbound workflows on the same account would create ambiguity.
- An account can have **multiple outbound workflows** (different campaigns, different audiences).

### Email Routing

When an email arrives for an account:

1. **Match by thread**: look up `gmail_thread_id` in the `email` table. If a match exists, route to the workflow that owns that thread (via `workflow_id` on the email row).
2. **Unmatched inbound**: if no thread match, check if the account has an active inbound workflow. If yes, route there. If no, store the email unrouted.

## Agent Execution

Each workflow is executed by a Pydantic AI agent. The agent is **stateless** -- each invocation gets fresh context from the database. No persistent conversation history, no context window management.

### Events

The agent is invoked by three types of events:

| Event         | Trigger              | Agent receives                    |
| ------------- | -------------------- | --------------------------------- |
| Email arrives | Pub/Sub sync         | New email + workflow instructions |
| Task due      | Periodic task runner | Task description + context        |
| Manual send   | CLI command          | Contact list + template           |

### Tools

The agent interacts with the system through tools only. Starting set:

- `send_email(to, subject, body, thread_id)` -- send via Gmail API
- `create_task(description, scheduled_at, context)` -- schedule deferred work
- `search_emails(query)` -- query email history
- `read_contact(email)` / `read_company(domain)` -- CRM lookups

Additional tools (file access, web search, SQL queries) can be added per workflow as needed.

### Task Planning

When the agent cannot complete work in a single invocation, it creates a **task** -- a deferred action with a scheduled execution time. Tasks are the only planning mechanism.

Examples:

- "Follow up with contact X in 5 days if no reply"
- "Send the next batch of outbound emails tomorrow at 9am"
- "Re-check this thread in 2 hours for a response"

The task runner is a periodic loop alongside the sync loop:

```sql
SELECT * FROM task
WHERE scheduled_at <= now() AND status = 'pending'
ORDER BY scheduled_at
```

For each due task: load the workflow, invoke the agent with the task context, mark task as completed or failed.

### Execution Flow

```
Event (email / task / manual)
  |
  +-> Load workflow instructions (system prompt)
  +-> Load event context (email body, task description, contact list)
  +-> Invoke pydantic-ai agent with tools
  +-> Agent acts: sends emails, creates tasks, queries data
  +-> Done (stateless, no cleanup)
```

## Schema

Replace `campaign` with `workflow`:

```sql
CREATE TABLE IF NOT EXISTS workflow (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    type              TEXT NOT NULL,            -- 'inbound' or 'outbound'
    account_id        TEXT NOT NULL REFERENCES account(id),
    status            TEXT NOT NULL DEFAULT 'draft',
    instructions      TEXT NOT NULL DEFAULT '', -- LLM system prompt
    template_subject  TEXT NOT NULL DEFAULT '', -- outbound only
    template_body     TEXT NOT NULL DEFAULT '', -- outbound only
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS task (
    id            TEXT PRIMARY KEY,
    workflow_id   TEXT NOT NULL REFERENCES workflow(id),
    email_id      TEXT REFERENCES email(id),
    description   TEXT NOT NULL,
    context       JSONB NOT NULL DEFAULT '{}',
    scheduled_at  TIMESTAMPTZ NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at  TIMESTAMPTZ
);
```

Replace `campaign_id` with `workflow_id` on the `email` table:

```sql
-- on email table
workflow_id TEXT REFERENCES workflow(id)
```

### CLI

```
mailpilot workflow create --name N --type inbound|outbound --account-id ID
mailpilot workflow list [--account-id ID]
mailpilot workflow view ID
mailpilot workflow update ID [--name N] [--instructions-file F]
mailpilot workflow activate ID          -- set status to active (register inbound)
mailpilot workflow pause ID             -- set status to paused
mailpilot workflow send ID [--limit N]  -- outbound only: send to contacts
```

## Consequences

### Positive

- One abstraction for both objectives -- no campaign/responder split
- Instructions are general -- no assumption about RAG, tools, or pipeline
- Per-account isolation maintained -- workflows are scoped to accounts
- Email routing is deterministic: thread match first, then inbound fallback
- Stateless agent invocations -- simple, predictable, no state management
- Task-based planning -- deferred work is just database rows with timestamps, no complex orchestration

### Negative

- `template_subject` and `template_body` are only relevant for outbound -- wasted columns on inbound rows (acceptable, keeps schema simple)
- One active inbound workflow per account limits flexibility (acceptable -- instructions within a workflow can handle sub-routing)
- Stateless invocations mean the agent re-reads context each time (acceptable -- database reads are cheap, and it avoids stale state)
