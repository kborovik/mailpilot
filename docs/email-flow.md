# Email Flow

## Inbound Email Flow

### 1. Notification -- `receive_notification()`

Pub/Sub streaming pull callback receives a notification:

- **Input**: base64-encoded JSON `{"emailAddress": "...", "historyId": "..."}`
- **Action**: decode, look up account by email, submit sync task to ThreadPoolExecutor
- **Output**: account ID + history ID passed to sync

### 2. Sync -- `sync_account()`

Fetch changes from Gmail and store new messages:

- **If account has `gmail_history_id`**: call Gmail History API (`historyTypes=messageAdded`), page through results
- **If no history ID or 404**: full sync via `list_messages(max_results=500)`
- For each new message: call `fetch_message()` then `store_email()`
- **Update**: `gmail_history_id` and `last_synced_at` on account

### 3. Extract -- `extract_text_from_message()`

Extract plain text from Gmail message payload:

- Walk MIME parts recursively
- Use `text/plain` parts only (no HTML conversion)
- If no `text/plain` found, store empty string

### 4. Route -- `route_email()`

Determine which workflow handles this email:

- **Step 1 -- Thread match**: query `email` table by `gmail_thread_id`. If found, use the existing `workflow_id`.
- **Step 2 -- LLM classification**: if no thread match, call `classify_email()`.
- **Step 3 -- Unrouted**: if classification returns no match, store email with `workflow_id = NULL`.

### 5. Classify -- `classify_email()`

Lightweight LLM call to route unmatched emails:

- **Input**: email subject, body, sender + list of active workflows (name, description) for the account
- **Output**: `workflow_id` or `None`
- **Model**: fast/cheap model (e.g., Haiku) via Pydantic AI structured output
- **No tools, no agent** -- pure routing decision

### 6. Execute -- `invoke_workflow_agent()`

Run the workflow's Pydantic AI agent:

- **Input**: workflow instructions (system prompt) + email content + contact email history (cross-workflow)
- **Tools available**: `send_email()`, `create_task()`, `update_contact_status()`, `search_emails()`, `read_contact()`, `read_company()`
- **Agent decides**: reply, create follow-up task, update contact status, or take no action
- **Stateless**: no persistent conversation, no cleanup

---

## Outbound Email Flow

### 1. Initiate -- `send_campaign()`

CLI entry point: `mailpilot workflow send ID --limit N`

- **Input**: workflow ID + contact limit
- **Action**: load workflow, verify status is `active` and type is `outbound`
- **Output**: list of target contacts for this workflow

### 2. Per-Contact -- `process_outbound_contact()`

For each contact in the target list:

- Load full email history between this account and this contact (cross-workflow)
- Call `check_cooldown()` -- if within cooldown, skip contact
- Invoke `invoke_workflow_agent()` with contact + history + instructions

### 3. Cooldown -- `check_cooldown()`

Guard against duplicate unsolicited outreach:

- Query last unsolicited outbound email (no `gmail_thread_id`) to this contact from this account
- If `sent_at` is within the cooldown period (configurable, default 43200 minutes): **refuse**
- Replies (`thread_id` provided) bypass cooldown entirely

### 4. Execute -- `invoke_workflow_agent()`

Same as inbound step 6. The agent receives:

- Workflow instructions + contact details + email history
- Agent calls `send_email()` tool to deliver the message

### 5. Send -- `send_email()`

Agent tool that sends via Gmail API:

- **Cooldown re-check**: enforced at tool level as a final guard
- **Compose**: RFC 2822 message with custom headers (`X-MailPilot-Version`, `X-MailPilot-Account-Id`)
- **Threading**: set `threadId` if replying to existing conversation
- **Store**: create `email` row with `workflow_id`, `contact_id`, `direction = 'outbound'`

---

## Task Execution Flow

### 1. Poll -- `run_task_runner()`

Periodic loop alongside the sync loop:

- Query: `SELECT * FROM task WHERE scheduled_at <= now() AND status = 'pending' ORDER BY scheduled_at`
- For each due task: submit to ThreadPoolExecutor

### 2. Execute -- `execute_task()`

Run the task's workflow agent with task context:

- Load workflow by `task.workflow_id`
- Load related email by `task.email_id` (if set)
- Call `invoke_workflow_agent()` with task description + context
- **On success**: set `task.status = 'completed'`, `task.completed_at = now()`
- **On failure**: set `task.status = 'failed'`, log error
