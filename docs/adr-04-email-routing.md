# ADR-04: Email Routing

## Status

Accepted

## Context

MailPilot accounts can have multiple active workflows (e.g., sales outreach, support, partnerships). When an inbound email arrives, the system must decide which workflow handles it. When an outbound email bounces, the system must detect and record the failure.

Routing must be fast, deterministic when possible, and never lose emails. An unrouted email is better than a misrouted one. Only INBOX emails are synced -- spam, trash, and sent mail are filtered out at the Gmail API level (see `docs/email-flow.md` step 2).

See `docs/adr-03-workflow-model.md` for the workflow model and agent execution.

## Decision

### Routing Pipeline

When an inbound email arrives for an account, route it through a four-step pipeline. The implementation lives in `src/mailpilot/routing.py` (`route_email`):

```
  email arrives (is_routed=False)
       |
       v
  bounce? --> yes --> _handle_bounce: mark original outbound bounced,
       |                              disable contact, set is_routed=True
       | no
       v
  1. _try_thread_match (gmail_thread_id)  -----> hit -----> route to workflow
       |                                                         |
       | miss                                                    v
       v                                                  set is_routed=true,
  2. _try_rfc_message_id_match                            create enrollment
     (In-Reply-To / References)         -----> hit -----> route to workflow
       |
       | miss
       v
  3. _try_classify (LLM, inbound active) -----> hit -----> route to workflow
       |
       | miss (one of two no-match labels):
       |   - unrouted                       (classifier ran, no candidate matched)
       |   - skipped_no_inbound_workflows   (no active inbound workflows; LLM never ran)
       v
  4. store as unrouted
       |
       v
  is_routed = TRUE, workflow_id = NULL
```

`route_email` always sets `is_routed = TRUE` exactly once at the end of the pipeline (or via `_handle_bounce`). `update_email(..., workflow_id=..., is_routed=True)` does this in a single round-trip.

**Step 1 -- Thread match (`_try_thread_match`).** Look up `gmail_thread_id` in the `email` table. If a prior email in the same thread (same `account_id`, different row) has a non-null `workflow_id`, route to the most recent such email's workflow. Fast and cheap, but unreliable -- users forward instead of reply, start new threads, and email clients break threading. Thread match works regardless of workflow status (active or paused) to honor the "no ghosting" guarantee.

**Step 2 -- RFC 2822 message-id match (`_try_rfc_message_id_match`).** Gmail re-threads on the recipient side: a reply landing on the outbound mailbox can have a fresh `threadId` even though it cites our original send via `In-Reply-To` / `References`. When step 1 returns no match, walk the cited message-ids (parent first via `In-Reply-To`, then ancestors from the whitespace-separated `References`, deduped while preserving order) and look them up against `email.rfc2822_message_id` within the same `account_id`. Scope is intentionally restricted to the inbound email's own account so cross-account collisions on a shared `Message-ID` cannot leak workflow assignments.

**Step 3 -- LLM classification (`_try_classify` -> `mailpilot.agent.classify.classify_email`).** Single-turn Pydantic AI structured-output call. Candidates are limited to **active inbound** workflows (`list_workflows(account_id, status="active")` filtered by `type == "inbound"`); paused, draft, and outbound workflows are excluded. This means a new email about a paused workflow's topic will go unrouted -- intentional, since paused workflows only handle existing threads, not new conversations. When the model returns a `workflow_id` that is not in the candidate set, it is coerced to None.

`_try_classify` returns `(workflow_id, route_method)` so the `routing.route_email` span can distinguish the two no-match cases:

- `route_method=skipped_no_inbound_workflows` -- account has no active inbound workflows (or none survive hydration); the LLM is never called. Distinct from the sync-layer `skipped_no_workflows` short-circuit, which fires only when the account has zero active workflows of any type. The inbound-only label is what fires on the outbound mailbox when an inbound reply lands while only outbound workflows are active.
- `route_method=unrouted` -- the LLM ran on real candidates and rejected all of them (returned `None` or returned a hallucinated workflow_id that was coerced to None). This is the genuine "classifier could not place this email" signal worth investigating in production.

**Step 4 -- Unrouted.** If classification returns no match (either label above), store the email with `workflow_id = NULL` and `is_routed = TRUE`. The email is preserved and can be viewed via `mailpilot email list --account-id ID` and `mailpilot email view ID`.

### Pre-routing skips (sync layer)

`sync._store_inbound_message` short-circuits the pipeline before calling `route_email` in three cases. Each path still sets `is_routed = TRUE` and emits a `routing.route_email` span with a `route_method` attribute, so traces are uniform:

- **Outside recency window.** `received_at` older than 7 days: row is created with `is_routed = TRUE` directly (`route_method=skipped_outside_window`).
- **No active workflows.** Account has no active workflows of any type: `route_method=skipped_no_workflows`.
- **Predates active inbound workflows.** Email's `received_at` is earlier than the earliest active inbound workflow's `created_at`. Such emails can never produce tasks (see `create_tasks_for_routed_emails` in `database.py`, which filters on `e.created_at >= w.created_at`), so calling the LLM is pure waste. `route_method=skipped_predates_workflows`. (#65)

Older messages serve as context for agent email history but are never acted upon.

### Classification

A lightweight, single-turn LLM call using Pydantic AI structured output. Implementation in `src/mailpilot/agent/classify.py`:

- **Input**: email subject, body (truncated to 16384 chars), sender + JSON list of active inbound workflows (`id`, `name`, `objective`).
- **Output**: `ClassificationResult(workflow_id: str | None, reasoning: str)`. The classifier writes `reasoning` as a span attribute for offline inspection.
- **No tools, no agent run** -- pure structured-output call, no side effects.
- **Model**: `settings.anthropic_model` (any Claude model the operator picks, including faster/cheaper options). The model and provider are cached per `(api_key, model_name)` via `lru_cache`.
- **Hallucination guard**: if the returned `workflow_id` is not in the candidate set, it is coerced to None.

The classifier sees the workflow `name` and `objective`, since the objective is the most concise statement of what the workflow does. `_try_classify` hydrates `Workflow` records via `get_workflow(...)` because `WorkflowSummary` does not include `objective`.

### Auto-Contact Creation

During sync (before routing), every distinct sender in the batch is resolved to a `contact` row in two round-trips: one bulk `SELECT ... WHERE email = ANY(...)` (`get_contacts_by_emails`) for existing rows, and one bulk `INSERT ... SELECT FROM unnest(...)` (`create_contacts_bulk`) for the missing ones. A backfill pass updates `first_name` / `last_name` from the `From` header display name (e.g., `"John Doe <john@example.com>"` -> `first_name="John"`, `last_name="Doe"`) when the existing row has nulls. If the `From` header has no display name, the name fields stay `NULL`. A per-message fallback (`create_or_get_contact_by_email`) catches senders missing from the bulk dict, so a single malformed `From` header cannot abort the whole sync.

This ensures every email has a `contact_id`, which is required for agent history queries and cooldown checks. See `docs/email-flow.md` step 2.

### Auto-Enrollment Creation

When `route_email` assigns a `workflow_id` (via any of the three matching steps), `_ensure_enrollment` creates an `enrollment` row keyed on `(workflow_id, contact_id)` if one does not already exist. The insert uses `ON CONFLICT DO NOTHING`, so re-routes within the same thread or duplicate Pub/Sub deliveries do not produce duplicate enrollments. On the initial insert (and only then), an `enrollment_added` activity is appended to the contact timeline with `summary = "Assigned to <workflow_name>"`. This guarantees that any contact whose email landed in a workflow is enrolled and visible to the agent's `list_enrollments` tool.

Bounce-handled emails do not create enrollments -- `_handle_bounce` returns before the routing pipeline runs.

### Recency Gate

Only emails received within the last 7 days are passed to the routing pipeline. The window is defined by `_RECENCY_WINDOW = timedelta(days=7)` in `sync.py`. Older messages synced during a full sweep are stored with `is_routed = TRUE` and `workflow_id = NULL` -- they serve as context for agent email history but are not acted upon. This prevents the agent from replying to stale emails during initial bootstrap.

### The `is_routed` Flag

The `email` table has `is_routed BOOLEAN NOT NULL DEFAULT FALSE`. Semantics:

- Set to `TRUE` after any routing decision (thread match, message-id match, classification, unrouted determination, bounce handling, or any pre-routing skip).
- Outbound rows are created with `is_routed = TRUE` directly (`sync.send_email`) -- they originate from an agent/CLI call and need no routing.
- Prevents re-routing on subsequent syncs -- the History API may re-deliver messages, and re-classification would be non-deterministic. `route_email` is idempotent: rows already marked `is_routed = TRUE` are returned unchanged.
- An email with `is_routed = TRUE` and `workflow_id = NULL` is a deliberate "unrouted" decision, not a missing classification.
- An email with `is_routed = FALSE` has not yet been processed by the routing pipeline.

### Bounce Detection

`_is_bounce` flags an inbound email as a bounce notification when either signal is present:

- The sender's local part is `mailer-daemon` or `postmaster` (case-insensitive).
- Any Gmail label on the message contains `BOUNCE` (case-insensitive substring).

`_handle_bounce` then:

1. Looks up other emails in the same `gmail_thread_id` and finds the most recent **outbound** email on the same account.
2. Sets `email.status = 'bounced'` on that original outbound email (`update_email`).
3. Calls `disable_contact(...)` on the original recipient: `contact.status = 'bounced'` and `contact.status_reason = "Bounce detected on email <id>"`.
4. Marks the bounce notification itself as `is_routed = TRUE` so it is not re-processed.

If the bounce notification has no `gmail_thread_id`, or there is no outbound email in the thread, the bounce row is still marked routed but no contact is disabled (a warning is logged via `logfire.warn`).

Once a contact is `bounced` or `unsubscribed`, the `email_ops.send_email` policy layer (`ContactDisabledError`) prevents further outbound emails to that contact across all workflows.

## Schema

`workflow_id`, `is_routed`, `rfc2822_message_id`, `in_reply_to`, `references_header` on the `email` table. See `src/mailpilot/schema.sql`.

## Consequences

### Positive

- Four-step pipeline ensures no email is lost -- worst case is unrouted, never silently dropped.
- INBOX-only filtering eliminates spam and trash at the API level before any processing.
- Thread match handles the common case (replies) cheaply.
- RFC 2822 message-id match closes the recipient-side re-threading gap that pure thread-id matching cannot cover.
- LLM classification handles broken threads, forwards, and new conversations.
- Pre-routing skips (no active workflows / outside window / predates workflows) keep token spend bounded during initial bootstrap and historical re-syncs.
- `is_routed` flag prevents non-deterministic re-classification on re-sync.
- Paused workflow exclusion from classification is consistent with "no new work" semantics; thread-match still routes prior threads to honor "no ghosting".
- Auto-enrollment on successful routing guarantees the agent's `list_enrollments` view stays in sync with the timeline.
- Bounce detection feeds into global contact status for cross-workflow protection, and is enforced by the `email_ops` policy layer.
- Auto-contact creation ensures every email has a `contact_id` for history queries and cooldown checks.
- Recency gate prevents stale emails from triggering agent actions during full sync.

### Negative

- LLM classification is non-deterministic -- same email may route differently on retry (acceptable, mitigated by storing the routing decision via `is_routed`).
- No re-routing mechanism -- once routed, an email stays with its workflow. Manual correction requires direct database update. Acceptable for now; a `mailpilot email route ID --workflow-id WID` command can be added later if needed.
- Thread match (and message-id match) can route to paused workflows -- intentional (no ghosting), but may surprise users who expect "paused" to mean "fully stopped".
- Auto-contact creation produces basic records (email, domain, first/last name from header) -- further enrichment happens later via other tools.
- Recency gate (7 days) means emails older than that are never routed, even if they match a workflow -- acceptable trade-off to avoid acting on stale context.
- Bounce detection is heuristic (sender local part + label substring). DSNs from non-standard mail servers that omit both signals will be missed.
