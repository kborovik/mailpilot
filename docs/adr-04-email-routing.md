# ADR-04: Email Routing

## Status

Accepted

## Context

MailPilot accounts can have multiple active workflows (e.g., sales outreach, support, partnerships). When an inbound email arrives, the system must decide which workflow handles it. When an outbound email bounces, the system must detect and record the failure.

Routing must be fast, deterministic when possible, and never lose emails. An unrouted email is better than a misrouted one. Only INBOX emails are synced -- spam, trash, and sent mail are filtered out at the Gmail API level (see `docs/email-flow.md` step 2).

See `docs/adr-03-workflow-model.md` for the workflow model and agent execution.

## Decision

### Routing Pipeline

When an inbound email arrives for an account, route it through a three-step pipeline:

```
  email arrives
       |
       v
  1. thread match -----> found? -----> route to workflow
       |                                     |
       | no match                            v
       v                                set is_routed = true
  2. LLM classify -----> match? -----> route to workflow
       |                                     |
       | no match                            v
       v                                set is_routed = true
  3. store as unrouted
       |
       v
  is_routed = true, workflow_id = NULL
```

**Step 1 -- Thread match**: Look up `gmail_thread_id` in the `email` table. If a prior email in the same thread has a non-null `workflow_id`, route to the `workflow_id` of the most recent such email. If all prior emails in the thread have `workflow_id = NULL` (unrouted), fall through to classification -- this gives the classifier a chance to route the thread now that more context exists. Fast and cheap, but unreliable -- users forward instead of reply, start new threads, and email clients break threading. Thread match works regardless of workflow status (active or paused) to honor the "no ghosting" guarantee.

**Step 2 -- LLM classification**: If no thread match, classify via a single-turn LLM call. Returns a `workflow_id` or `None`. Only **active** workflows are candidates -- paused and draft workflows are excluded. This means a new email about a paused workflow's topic will go unrouted. This is intentional: paused workflows only handle existing threads, not new conversations.

**Step 3 -- Unrouted**: If classification returns no match, store the email with `workflow_id = NULL` and `is_routed = TRUE`. The email is preserved and can be viewed via `mailpilot email list --account-id ID` and `mailpilot email view ID`.

### Classification

A lightweight, single-turn LLM call using Pydantic AI structured output:

- **Input**: email subject, body, sender + list of active workflows (name, objective) for the account. Active workflows are queried via `list_workflows(account_id=..., status="active")`
- **Output**: `workflow_id` or `None` (unrouted)
- **No tools, no agent** -- pure routing decision, no side effects
- **Fast model** -- can use a smaller model (e.g., Haiku) since this is a classification task

The classifier sees the workflow `name` and `objective`, since the objective is the most concise statement of what the workflow does.

### Auto-Contact Creation

During sync (before routing), the system resolves the sender to a `contact` record. If no contact exists for the sender email, a contact is created with `email`, `domain` (extracted from address), and `first_name`/`last_name` (parsed from the `From` header display name, e.g., `"John Doe <john@example.com>"` -> `first_name="John"`, `last_name="Doe"`). If the `From` header has no display name, the name fields are left as `NULL`. This ensures every email has a `contact_id`, which is required for agent history queries and cooldown checks. See `docs/email-flow.md` step 2.

### Recency Gate

Only emails received within the last 7 days are routed (passed to the routing pipeline). Older messages synced during a full sync are stored with `is_routed = TRUE` and `workflow_id = NULL` -- they serve as context for agent email history but are not acted upon. This prevents the agent from replying to stale emails during initial bootstrap.

### The `is_routed` Flag

The `email` table has `is_routed BOOLEAN NOT NULL DEFAULT FALSE`. Semantics:

- Set to `TRUE` after any routing decision (thread match, classification, or unrouted determination)
- Prevents re-routing on subsequent syncs -- the History API may re-deliver messages, and re-classification would be non-deterministic
- An email with `is_routed = TRUE` and `workflow_id = NULL` is a deliberate "unrouted" decision, not a missing classification
- An email with `is_routed = FALSE` has not yet been processed by the routing pipeline

### Bounce Detection

When a bounce notification is detected during sync:

1. Set `email.status = 'bounced'` on the original outbound email
2. Set `contact.status = 'bounced'` and `contact.status_reason` with the bounce details
3. The `send_email` tool guard prevents further emails to bounced contacts across all workflows

Bounce detection relies on Gmail bounce notification emails and labels. The exact detection mechanism (label-based vs. content parsing) is an implementation detail.

## Schema

`workflow_id` and `is_routed` on the `email` table. See `src/mailpilot/schema.sql`.

## Consequences

### Positive

- Three-step pipeline ensures no email is lost -- worst case is unrouted, never silently dropped
- INBOX-only filtering eliminates spam and trash at the API level before any processing
- Thread match handles the common case (replies) cheaply
- LLM classification handles broken threads, forwards, and new conversations
- `is_routed` flag prevents non-deterministic re-classification on re-sync
- Paused workflow exclusion from classification is consistent with "no new work" semantics
- Bounce detection feeds into global contact status for cross-workflow protection
- Auto-contact creation ensures every email has a `contact_id` for history queries and cooldown checks
- Recency gate prevents stale emails from triggering agent actions during full sync

### Negative

- LLM classification is non-deterministic -- same email may route differently on retry (acceptable, mitigated by storing the routing decision via `is_routed`)
- No re-routing mechanism -- once routed, an email stays with its workflow. Manual correction requires direct database update. Acceptable for now; a `mailpilot email route ID --workflow-id WID` command can be added later if needed
- Thread match can route to paused workflows -- intentional (no ghosting), but may surprise users who expect "paused" to mean "fully stopped"
- Auto-contact creation produces basic records (email, domain, first/last name from header) -- further enrichment happens later via other tools
- Recency gate (7 days) means emails older than that are never routed, even if they match a workflow -- acceptable trade-off to avoid acting on stale context
