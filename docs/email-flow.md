# Email Flow

## Sync Loop Lifecycle

`mailpilot run` (entry point: `start_sync_loop` in `src/mailpilot/sync.py`) is a single foreground process managed by systemd. It integrates four real-time signals on a single shared `wakeup_event`:

- **Pub/Sub streaming-pull subscriber** -- Gmail watch notifications (`make_notification_callback` in `pubsub.py`).
- **PG LISTEN/NOTIFY on `task_pending`** -- tasks created by routing or by the agent fire `pg_notify('task_pending')`, waking the loop instantly.
- **Periodic safety-net** -- `wakeup_event.wait(timeout=run_interval)` is the upper-bound fallback, not the primary trigger.
- **Signal handlers** -- SIGTERM / SIGINT set both `shutdown_event` and `wakeup_event` so an in-flight wait unblocks immediately.

Each iteration calls `_run_periodic_iteration` which:
1. Drains the Pub/Sub `sync_queue` via `_drain_sync_queue` (one `sync_account` call per pending account, deduped by email).
2. If the safety-net interval has elapsed, calls `_sync_all_accounts` for accounts not already synced this tick.
3. Bridges newly routed inbound emails to tasks via `create_tasks_for_routed_emails`.
4. Drains the pending task queue via `_drain_pending_tasks` -> `execute_task` (defined in `run.py`).

Watch renewal runs at most once per `_WATCH_RENEWAL_INTERVAL` (1 hour) via `_renew_watches_if_due`.

---

## Inbound Email Flow

### 1. Notification -- `make_notification_callback()` (`pubsub.py`)

Pub/Sub streaming pull callback fires when Gmail publishes a watch notification. Requires `GmailClient.watch()` to be active on the account (renewed by `pubsub.renew_watches` whenever `watch_expiration` is within 24 hours).

- **Input**: base64-decoded JSON `{"emailAddress": "...", "historyId": "..."}` on `message.data`.
- **Action**: parse, push the `emailAddress` onto `sync_queue`, set `wakeup_event`, ack the message. Malformed payloads are still acked (nacking causes infinite redelivery) and reported via `pubsub.notification.decode_error`.
- **Output**: account email queued for `_drain_sync_queue` to consume on the next loop iteration.

The `historyId` from the notification is intentionally ignored -- the sync uses the `gmail_history_id` we have stored on the account row, which is more reliable than racing the notification's value.

### 2. Sync -- `sync_account()` (`sync.py`)

Per-account inbound pipeline. Each call:

- Snapshots the account's current `historyId` via `get_profile()` **before** fetching, so any message that arrives during the run is still above the checkpoint and picked up next time.
- Picks the sync mode in `_collect_new_message_ids`:
  - **Incremental** (`gmail_history_id` set): `GmailClient.get_history(history_types=["messageAdded"], label_id="INBOX")`. Pages results automatically.
  - **Full fallback** (no history ID, or 404 from `get_history`): `GmailClient.list_messages(max_results=100, label_ids=["INBOX"])`.
- **INBOX-only**: both paths filter to the `INBOX` label, so spam, trash, drafts, and sent mail are skipped at the API level.
- Dedupes against the `email` table (`get_email_by_gmail_message_id`), batch-fetches the remaining message bodies via `get_messages_batch`.
- Bulk-resolves every distinct sender to a `contact` row via `_resolve_contacts_for_messages`: one `SELECT ... WHERE email = ANY(...)` for existing rows, one `INSERT ... SELECT FROM unnest(...)` for the missing ones, plus a backfill pass for `first_name`/`last_name` parsed from each `From` header (e.g. `"Jane Smith <jane@co.com>"`).
- For each new message: `_store_inbound_message` extracts plain text, persists the row via `create_email`, emits an `email_received` activity, then applies the routing decision.

**Recency gate** (`_RECENCY_WINDOW = 7 days`): emails older than the window are stored with `is_routed = TRUE` and `workflow_id = NULL` -- they serve as agent context but are not acted upon.

**Pre-routing skips** in `_store_inbound_message` (each emits a `routing.route_email` span with `route_method=skipped_*`):

- `skipped_outside_window` -- `received_at` older than 7 days.
- `skipped_no_workflows` -- no active workflows on the account.
- `skipped_predates_workflows` -- `received_at` is earlier than the earliest active inbound workflow's `created_at` (these emails can never produce tasks; see `create_tasks_for_routed_emails`).

Fresh, eligible emails are handed to `route_email`. After the per-message loop, `update_account` persists `gmail_history_id` (the snapshot from the start of the run) and `last_synced_at`.

### 3. Extract -- `extract_text_from_message()` (`gmail.py`)

Extract and normalize plain text from a Gmail message payload:

- Walk MIME parts recursively.
- Use `text/plain` parts only (no HTML conversion).
- In `multipart/*` containers, prefer the first `text/plain` sub-part.
- Normalize: strip trailing whitespace per line, collapse 3+ consecutive blank lines to 2, strip leading/trailing blank lines.
- If no `text/plain` is found, return an empty string.

### 4. Route -- `route_email()` (`routing.py`)

Determine which workflow handles this email. Idempotent: rows with `is_routed = TRUE` are returned unchanged.

- **Bounce check first**: `_is_bounce` (sender local part `mailer-daemon`/`postmaster`, or any Gmail label containing `BOUNCE`). On hit, `_handle_bounce` marks the original outbound email `status = 'bounced'`, disables the original recipient (`contact.status = 'bounced'`), and stops here.
- **Step 1 -- Thread match** (`_try_thread_match`): if a prior email in the same `gmail_thread_id` (same account, different row) has a non-null `workflow_id`, use the most recent such workflow. Status-agnostic (active or paused) for the no-ghosting guarantee.
- **Step 2 -- RFC 2822 message-id match** (`_try_rfc_message_id_match`): if no thread match, walk the inbound email's `In-Reply-To` and `References` headers and look them up against `email.rfc2822_message_id` within the same account. Closes the recipient-side re-threading gap (Gmail can assign a fresh `threadId` even when the message cites our original `Message-ID`).
- **Step 3 -- LLM classification** (`_try_classify` -> `agent.classify.classify_email`): single-turn structured-output call against active **inbound** workflows for the account (paused, draft, and outbound workflows are excluded).
- **Step 4 -- Unrouted**: store with `workflow_id = NULL`, `is_routed = TRUE`.

On a successful match, `_ensure_enrollment` creates the `(workflow_id, contact_id)` enrollment row if missing (`ON CONFLICT DO NOTHING`) and emits an `enrollment_added` activity once. See ADR-04 for the full pipeline contract.

### 5. Classify -- `classify_email()` (`agent/classify.py`)

Single-turn LLM call to route emails that fell through both deterministic steps:

- **Input**: email subject, body (truncated to 16384 chars), sender + JSON list of active inbound workflows (`id`, `name`, `objective`).
- **Output**: `ClassificationResult(workflow_id, reasoning)` via Pydantic AI structured output.
- **Model**: `settings.anthropic_model`. The `(api_key, model_name)` -> model object pair is `lru_cache`-d.
- **Hallucination guard**: if the returned `workflow_id` is not in the candidate set, it is coerced to `None`.
- **No tools, no agent run** -- pure routing decision.

### 6. Bridge to tasks -- `create_tasks_for_routed_emails()` (`database.py`)

After `_drain_sync_queue` and `_sync_all_accounts`, `_run_periodic_iteration` calls `create_tasks_for_routed_emails`. It selects every inbound email with `workflow_id` set and no corresponding task row, and inserts a task with `description = "handle inbound email"` and `scheduled_at = now()`. The `INSERT` fires `pg_notify('task_pending')`, which the PG LISTEN thread translates into a `wakeup_event.set()` -- so the loop drains the new task on its next iteration without waiting for the safety-net timer.

The filter uses `e.created_at >= w.created_at` (DB insert time, not Gmail's `received_at`), so emails received before a workflow existed but synced into our DB after still qualify.

### 7. Execute -- `execute_task()` -> `invoke_workflow_agent()` (`run.py`, `agent/invoke.py`)

`_drain_pending_tasks` walks each row from `list_pending_tasks` and calls `execute_task`. Pre-flight checks:

- Workflow loaded and `status = 'active'`. Otherwise the task is cancelled with `reason = "workflow inactive or not found"`.
- Contact loaded and `status` is not `bounced`/`unsubscribed`. Otherwise cancelled.
- Enrollment exists and `status = 'active'`. Otherwise cancelled.

Then `invoke_workflow_agent` is called with the workflow, contact, and (if the task has `email_id`) the trigger email. `invoke_workflow_agent`:

- Acquires a non-blocking PostgreSQL advisory lock on `(workflow_id, contact_id)`. If the lock is held, the run is skipped (returns `None`).
- Loads email history scoped to `(account_id, contact_id, workflow_id)`. The list is workflow-scoped: each `Email` summary is hydrated to a full row via `get_email` so the agent prompt has `body_text`.
- Builds a Pydantic AI agent with the workflow's `instructions` plus a fixed `_SYSTEM_PREFIX`.
- Tools registered on the agent (see `agent/invoke.py`):
  - `send_email` -- new outbound, with contact-status + 30-day cooldown guards (via `email_ops.send_email`).
  - `reply_email` -- in-thread reply, auto-derives recipient/subject/`In-Reply-To`, no cooldown.
  - `create_task` -- schedule deferred work.
  - `cancel_task` -- cancel a pending task.
  - `record_enrollment_outcome` -- write `enrollment_completed` / `enrollment_failed` activity.
  - `disable_contact` -- set global block (`bounced` / `unsubscribed`).
  - `list_enrollments` -- list contacts enrolled in the current workflow.
  - `search_emails` -- LIKE-search the account's email history.
  - `read_contact` / `read_company` / `read_email` -- CRM lookups.
  - `noop(reason)` -- explicit "do nothing" escape hatch.
- **Tool-use enforcement**: a run with zero tool calls raises `AgentDidNotUseToolsError`. `noop` exists so the agent can satisfy this contract when no action is appropriate.
- Records usage (input/output/total tokens, request count, tool-call count) and any tool-return error payloads on the span.

The advisory lock is released in `finally`. The trigger email's body is included verbatim in the prompt -- the agent should not call `read_email` on it again.

---

## Manual Workflow Run (Outbound + ad-hoc Inbound)

### 1. Initiate -- `mailpilot enrollment run`

CLI entry point: `mailpilot enrollment run --workflow-id ID --contact-id ID` (handler at `cli.enrollment_run`). Works for both inbound and outbound workflows -- it is the operator-driven equivalent of the loop's automatic invocation.

- Validates: workflow exists and `status = 'active'`; contact exists; enrollment exists and `status = 'active'`.
- For **outbound** workflows: no trigger email; the agent's prompt explains it is an outbound invocation.
- For **inbound** workflows: looks up the most recent unprocessed inbound email via `get_unprocessed_inbound_email` (most recent inbound row in this `(workflow, contact)` with no task) and passes it as the trigger.
- Calls `invoke_workflow_agent` directly (not via `create_task`) so the manual run is immediate and does not race the loop's `task_pending` listener.

### 2. Execute -- `invoke_workflow_agent()`

Same machinery as inbound step 7. The agent receives:

- Workflow `instructions` and `objective`.
- Contact details (email, name, position, domain).
- Email history scoped to `(account, contact, workflow)`.
- A trigger section: the latest inbound email (inbound workflows), the task description (deferred runs), or "outbound invocation" boilerplate (manual outbound runs).

The agent calls `send_email` (or `reply_email` for follow-ups in an existing thread) to deliver the message.

### 3. Send -- `email_ops.send_email()` -> `sync.send_email()` (`email_ops.py`, `sync.py`)

The agent's `send_email` tool delegates to the `email_ops` policy layer:

- **Auto-resolve contact** via `get_contact_by_email(to)`.
- **Contact-status guard**: if `contact.status != 'active'` (bounced or unsubscribed), raise `ContactDisabledError` (`code = "contact_disabled"`).
- **Cooldown guard** (`workflow_id` set only): `get_last_cold_outbound` returns the last unsolicited outbound to this contact for this workflow. If `created_at` is within 30 days, raise `CooldownError` (`code = "cooldown"`). Replies (`reply_email`) bypass cooldown entirely. Ad-hoc CLI sends without a workflow have no cooldown.

If guards pass, `sync.send_email` does the actual send:

- Renders the Markdown body to themed HTML via `email_renderer.render_email_html` (workflow `theme` -> `EmailTheme`), and pairs it with a stripped-control-chars plaintext part.
- Builds a `multipart/alternative` MIME message with both parts.
- Resolves threading headers (`_resolve_threading_headers`): an explicit `in_reply_to` (agent `reply_email` path) is honoured verbatim; otherwise CLI `email send --thread-id` derives `In-Reply-To` from the latest local row's `rfc2822_message_id` and a `References` chain from prior rows in the same account+thread.
- Calls `GmailClient.send_message`, passing `account_id` so Gmail (`gmail.py`) can stamp the outgoing message with `X-MailPilot-Version` and `X-MailPilot-Account-Id` headers.
- Fetches the sent message's `Message-ID` via a metadata GET (`_fetch_sent_rfc2822_message_id`) so the next reply in this thread can build a proper `References` chain. Best-effort -- a failure leaves `rfc2822_message_id = NULL`.
- Persists the row via `create_email` with `direction = 'outbound'`, `is_routed = TRUE`, `status = 'sent'`, `sent_at = now()`. Outbound rows skip the routing pipeline entirely.
- Typed `EmailOpsError` subclasses (`code = "contact_disabled"` / `"cooldown"` / `"not_found"` / `"no_thread"` / `"no_contact"`) are converted by `agent/tools.py` into the LLM-facing `{"error": code, "message": ...}` shape, and by `cli.py` into `output_error(...)` JSON.

---

## Task Execution Flow

Tasks are the universal execution primitive -- inbound routing, deferred follow-ups created by the agent, and re-runs all flow through the same queue.

### 1. Create

Tasks are created in two ways:

- `create_tasks_for_routed_emails` (per-iteration in the sync loop) bridges newly routed inbound emails into tasks.
- The agent's `create_task` tool schedules deferred work with an explicit `scheduled_at`.

Both paths fire `pg_notify('task_pending')` via the schema's `INSERT` trigger.

### 2. Wake -- PG LISTEN thread (`_start_task_listener` in `sync.py`)

A daemon thread holds a dedicated `LISTEN task_pending` connection. On each notification it sets `wakeup_event` and continues listening, so the main loop drains the queue immediately rather than waiting for `run_interval`.

### 3. Drain -- `_drain_pending_tasks()` (`sync.py`)

`list_pending_tasks` selects rows with `status = 'pending'` and `scheduled_at <= now()`. Each is handed to `execute_task` (see inbound step 7).

### 4. Execute -- `execute_task()` (`run.py`)

- Cancels with a structured `result.reason` if workflow is inactive, contact is disabled, or enrollment is missing/paused.
- Calls `invoke_workflow_agent`. On exception: rollback, then `complete_task(status='failed', result={'reason': str(exc)})`.
- On success: `complete_task(status='completed', result=...)`. On lock-held skip (returns `None`): the row is left pending so the next iteration retries.
