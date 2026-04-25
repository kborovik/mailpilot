# Email Flow

## Inbound Email Flow

### 1. Notification -- `receive_notification()`

Pub/Sub streaming pull callback receives a notification. Requires `GmailClient.watch()` to be active on the account (renewed before `watch_expiration`).

- **Input**: base64-encoded JSON `{"emailAddress": "...", "historyId": "..."}`
- **Action**: decode, look up account by email, submit sync task to ThreadPoolExecutor
- **Output**: account ID + history ID passed to sync

### 2. Sync -- `sync_account()`

Fetch changes from Gmail and collect new messages:

- **INBOX only**: both sync paths filter to INBOX label to skip spam, trash, and sent mail
- **If account has `gmail_history_id`**: call `GmailClient.get_history(history_types=["messageAdded"], label_id="INBOX")`, pages through results automatically
- **If no history ID or 404**: full sync via `GmailClient.list_messages(max_results=100, label_ids=["INBOX"])`
- For each new message: call `GmailClient.get_message()`, then `extract_text_from_message()`, then `create_email()` with the extracted text
- **Auto-contact**: look up sender by email in `contact` table. If not found, create a contact with `email`, `domain` (from address), and `first_name`/`last_name` (parsed from the `From` header display name, e.g., `"Jane Smith <jane@co.com>"`). Set `email.contact_id` to the resolved contact
- **Recency gate**: only emails received within the last 7 days are routed (passed to step 4). Older messages are stored with `is_routed = TRUE` and `workflow_id = NULL` -- they serve as context for agent history but are not acted upon
- **Update**: `gmail_history_id` and `last_synced_at` on account

### 3. Extract -- `extract_text_from_message()`

Extract and normalize plain text from Gmail message payload:

- Walk MIME parts recursively via `_extract_text_from_part()`
- Use `text/plain` parts only (no HTML conversion)
- In `multipart/*` containers, prefer the first `text/plain` sub-part
- Normalize: strip trailing whitespace per line, collapse 3+ consecutive blank lines to 2, strip leading/trailing blank lines
- If no `text/plain` found, return empty string

### 4. Route -- `route_email()`

Determine which workflow handles this email:

- **Prerequisite**: `contact_id` is set on the email (auto-contact runs during sync, before routing)
- **Step 1 -- Thread match**: query `email` table by `gmail_thread_id`. If a prior email has a non-null `workflow_id`, use the most recent one. If all prior emails are unrouted (`workflow_id = NULL`), fall through to classification.
- **Step 2 -- LLM classification**: if no thread match, call `classify_email()`.
- **Step 3 -- Unrouted**: if classification returns no match, store email with `workflow_id = NULL`.

### 5. Classify -- `classify_email()`

Lightweight LLM call to route unmatched emails:

- **Input**: email subject, body, sender + list of active workflows (name, objective) for the account
- **Output**: `workflow_id` or `None`
- **Model**: fast/cheap model (e.g., Haiku) via Pydantic AI structured output
- **No tools, no agent** -- pure routing decision

### 6. Execute -- `invoke_workflow_agent()`

Run the workflow's Pydantic AI agent:

- **Input**: workflow instructions (system prompt) + email content + contact email history (cross-workflow)
- **Tools available**: `send_email()`, `create_task()`, `update_enrollment_status()`, `search_emails()`, `read_contact()`, `read_company()`
- **Agent decides**: reply, create follow-up task, update enrollment status, or take no action
- **Stateless**: no persistent conversation, no cleanup

---

## Outbound Email Flow

### 1. Initiate -- `workflow run`

CLI entry point: `mailpilot workflow run --workflow-id ID --contact-id ID`

- **Input**: workflow ID + contact ID
- **Action**: load workflow, verify status is `active` and type is `outbound`
- Load full email history between this account and this contact (cross-workflow)

### 2. Cooldown -- `check_cooldown()`

Guard against duplicate unsolicited outreach:

- Query last unsolicited outbound email (no `gmail_thread_id`) to this contact from this account
- If `sent_at` is within the cooldown period (configurable, default 43200 minutes / 30 days): **refuse**
- Replies (`thread_id` provided) bypass cooldown entirely

### 3. Execute -- `invoke_workflow_agent()`

Same as inbound step 6. The agent receives:

- Workflow instructions + contact details + email history
- Agent calls `send_email()` tool to deliver the message

### 4. Send -- `send_email()`

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
