# ADR-04: Email Routing

## Status

Accepted

## Context

MailPilot accounts can have multiple active workflows (e.g., sales outreach, support, partnerships). When an inbound email arrives, the system must decide which workflow handles it. When an outbound email bounces, the system must detect and record the failure.

Routing must be fast, deterministic when possible, and never lose emails. An unrouted email is better than a misrouted one.

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

**Step 1 -- Thread match**: Look up `gmail_thread_id` in the `email` table. If a prior email in the same thread exists, route to its `workflow_id`. Fast and cheap, but unreliable -- users forward instead of reply, start new threads, and email clients break threading. Thread match works regardless of workflow status (active or paused) to honor the "no ghosting" guarantee.

**Step 2 -- LLM classification**: If no thread match, classify via a single-turn LLM call. Returns a `workflow_id` or `None`. Only **active** workflows are candidates -- paused and draft workflows are excluded. This means a new email about a paused workflow's topic will go unrouted. This is intentional: paused workflows only handle existing threads, not new conversations.

**Step 3 -- Unrouted**: If classification returns no match, store the email with `workflow_id = NULL`. The email is preserved and can be viewed via `mailpilot email list --account-id ID` and `mailpilot email view ID`.

### Classification

A lightweight, single-turn LLM call using Pydantic AI structured output:

- **Input**: email subject, body, sender + list of active workflows (name, description, objective) for the account
- **Output**: `workflow_id` or `None` (unrouted)
- **No tools, no agent** -- pure routing decision, no side effects
- **Fast model** -- can use a smaller model (e.g., Haiku) since this is a classification task

The classifier sees the workflow `objective` alongside name and description, since the objective is the most concise statement of what the workflow does.

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
- Thread match handles the common case (replies) cheaply
- LLM classification handles broken threads, forwards, and new conversations
- `is_routed` flag prevents non-deterministic re-classification on re-sync
- Paused workflow exclusion from classification is consistent with "no new work" semantics
- Bounce detection feeds into global contact status for cross-workflow protection

### Negative

- LLM classification is non-deterministic -- same email may route differently on retry (acceptable, mitigated by storing the routing decision via `is_routed`)
- No re-routing mechanism -- once routed, an email stays with its workflow. Manual correction requires direct database update. Acceptable for now; a `mailpilot email route ID --workflow-id WID` command can be added later if needed
- Thread match can route to paused workflows -- intentional (no ghosting), but may surprise users who expect "paused" to mean "fully stopped"
