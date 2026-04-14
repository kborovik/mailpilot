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

- Handles incoming emails that match the workflow's purpose
- An account can have multiple active inbound workflows (e.g., product questions, billing, partnerships)
- Responds using the workflow's instructions
- Status: `draft` -> `active` -> `paused`

### State Model

| `type`     | `status`    | Behavior                                                  |
| ---------- | ----------- | --------------------------------------------------------- |
| `outbound` | `draft`     | Created, not sending. Editing instructions and template.  |
| `outbound` | `active`    | Sending to contacts. Agent handles replies.               |
| `outbound` | `paused`    | Sending stopped. Existing threads still handled.          |
| `outbound` | `completed` | All contacts contacted. Only reply handling remains.      |
| `inbound`  | `draft`     | Created, not receiving. Editing instructions.             |
| `inbound`  | `active`    | Receiving emails. LLM classifier routes emails here.      |
| `inbound`  | `paused`    | Classifier skips this workflow. Existing threads handled. |

### Email Routing

When an email arrives for an account:

1. **Thread match**: look up `gmail_thread_id` in the `email` table. If a match exists, route to the workflow that owns that thread (via `workflow_id`). This is a fast, cheap check but unreliable -- users forward instead of reply, start new threads, and email clients break threading.
2. **LLM classification**: if no thread match, classify the email via a dedicated LLM call (not an agent). The classifier sees the email content and the list of active workflows on the account (name + description). Returns a `workflow_id` or `unrouted`.
3. **Unrouted**: if classification finds no match, store the email without a workflow.

### Email Classification

A lightweight, single-turn LLM call using Pydantic AI structured output:

- **Input**: email subject, body, sender + list of active workflows (name, description) for the account
- **Output**: `workflow_id` or `None` (unrouted)
- **No tools, no agent** -- pure routing decision, no side effects
- **Fast model** -- can use a smaller model (e.g., Haiku) since this is a classification task

## Agent Execution

Each workflow is executed by a Pydantic AI agent. The agent is **stateless** -- each invocation gets fresh context from the database. No persistent conversation history, no context window management.

### Events

The agent is invoked by three types of events:

| Event         | Trigger              | Agent receives                    |
| ------------- | -------------------- | --------------------------------- |
| Email arrives | Pub/Sub sync         | New email + workflow instructions |
| Task due      | Periodic task runner | Task description + context        |
| Manual send   | CLI command          | Contact list + template           |

### Contact History and Cooldown

When the agent processes a contact, it receives the **full email history between this account and this contact across all workflows** -- not just the current workflow's thread. This lets the agent make informed decisions ("we pitched them 45 days ago with no reply, adjust the angle" or "they replied negatively last month, skip").

The `send_email` tool enforces a **cooldown guard** on unsolicited outreach only:

- **Reply** (`thread_id` provided) -- always allowed, no cooldown. The contact wrote to us and deserves a response regardless of prior outreach history.
- **New conversation** (no `thread_id`) -- check the last unsolicited outbound email to this contact from this account. If sent within the cooldown period (configurable, default 43200 minutes / 30 days), refuse to send.

### Tools

The agent interacts with the system through tools only. Starting set:

- `send_email(to, subject, body, thread_id)` -- send via Gmail API (cooldown on new conversations only; replies always allowed)
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

The task runner is a periodic loop alongside the sync loop. For each due task: load the workflow, invoke the agent with the task context, mark task as completed or failed.

See `docs/email-flow.md` for detailed execution flows.

## Schema

See `src/mailpilot/schema.sql`. Tables: `workflow` (replaces `campaign`), `task`, and `workflow_id` on `email`.

## Consequences

### Positive

- One abstraction for both objectives -- no campaign/responder split
- Instructions are general -- no assumption about RAG, tools, or pipeline
- Multiple inbound workflows per account -- flexible routing by business purpose
- LLM classification handles broken threads, forwards, and new conversations
- Per-account isolation maintained -- workflows are scoped to accounts
- Stateless agent invocations -- simple, predictable, no state management
- Task-based planning -- deferred work is just database rows with timestamps, no complex orchestration

### Negative

- `template_subject` and `template_body` are only relevant for outbound -- wasted columns on inbound rows (acceptable, keeps schema simple)
- LLM classification is non-deterministic -- same email may route differently on retry (acceptable, mitigated by storing the routing decision on the email row)
- Stateless invocations mean the agent re-reads context each time (acceptable -- database reads are cheap, and it avoids stale state)
