# ADR-01: Gmail Sync Architecture

## Status

Accepted

## Context

MailPilot needs to sync Gmail messages for multiple email accounts and send emails on their behalf. Each account operates independently -- one account's sync failures must not affect others.

Requirements:

- Real-time notification of new emails per account
- Incremental sync (only fetch what changed)
- Send emails on behalf of any managed account
- No OAuth consent flow -- server-to-server auth
- Google Workspace only (acceptable constraint)

## Decision

**Google API Python Client (synchronous) called serially from a single sync-loop thread.** The application manages a small number of accounts (5-50 in expected use), so asyncio adds complexity without proportional benefit. `google-api-python-client` is Google's official library -- battle-tested, handles auth refresh, discovery, and serialization. Per-account isolation comes from try/except around each `sync_account` call, not from a thread pool.

**Pub/Sub streaming pull via `google-cloud-pubsub`.** The official library handles gRPC reconnection, flow control, and message leasing. Its callback-based subscriber runs in background threads owned by the library; the callback only enqueues the account's email address onto a `queue.Queue` and signals the main loop.

### Dependencies

| Package                    | Purpose                                       |
| -------------------------- | --------------------------------------------- |
| `google-api-python-client` | Gmail API client (service objects, discovery) |
| `google-auth`              | Service account credentials + token refresh   |
| `google-cloud-pubsub`      | Pub/Sub streaming pull subscriber             |

### Concurrency Model

`start_sync_loop` in `src/mailpilot/sync.py` runs one foreground loop thread plus two helper threads. All Gmail API work happens on the main loop thread; the helpers exist only to wake it.

```
Main loop thread (start_sync_loop)
  |
  | wakeup_event.wait(timeout=run_interval)
  |   |
  |   +-- _drain_sync_queue: per email in sync_queue -> sync_account()
  |   +-- _sync_all_accounts (full sweep, time-gated by run_interval)
  |   +-- create_tasks_for_routed_emails  (routed -> task rows)
  |   +-- _drain_pending_tasks            (run agent per pending task)
  |   +-- _renew_watches_if_due           (every _WATCH_RENEWAL_INTERVAL)
  |
  +-- Pub/Sub subscriber (gRPC threads owned by google-cloud-pubsub)
  |     |
  |     +-- callback: decode emailAddress, sync_queue.put, wakeup_event.set, ack
  |
  +-- PG LISTEN thread (_start_task_listener)
  |     |
  |     +-- LISTEN task_pending -> wakeup_event.set
  |
  +-- Signal handlers (SIGTERM / SIGINT) -> shutdown_event.set, wakeup_event.set
```

The loop's `wait()` is on a single shared `wakeup_event`. Pub/Sub notifications, PG `task_pending` events, and signals all set it; the periodic timer is the upper-bound fallback, not the primary trigger (see CLAUDE.md). `wakeup_event.clear()` runs **before** processing so events that arrive mid-iteration re-trigger the next wait.

## Per-Account Sync Model

Each `account` row tracks its own sync state:

| Column             | Purpose                                       |
| ------------------ | --------------------------------------------- |
| `gmail_history_id` | Last synced history ID (for incremental sync) |
| `watch_expiration` | When the Pub/Sub watch expires (epoch ms)     |
| `last_synced_at`   | Timestamp of last successful sync             |

Accounts never share sync state. An account with a stale history ID re-syncs from scratch without affecting other accounts.

## Authentication

Service account with domain-wide delegation for Google Workspace.

- Credentials: `GOOGLE_APPLICATION_CREDENTIALS` env var pointing to JSON key file
- Scope: `https://www.googleapis.com/auth/gmail.modify`
- Per-account impersonation: `credentials.with_subject(email)` creates delegated credentials
- Service build: `build("gmail", "v1", credentials=delegated)` returns a service object

Admin prerequisites:

1. Create service account in Google Cloud Console
2. Enable Gmail API
3. Grant domain-wide delegation in Google Workspace Admin (Security -> API Controls)
4. Authorize the service account client ID with `gmail.modify` scope

## Gmail API

Base URL: `https://gmail.googleapis.com/gmail/v1/users/{userId}`

All calls use `userId = "me"` (resolved from delegated credentials).

### Endpoints

| Operation      | Method | Endpoint                         | Quota Units |
| -------------- | ------ | -------------------------------- | ----------- |
| Get profile    | GET    | `/users/me/profile`              | 1           |
| List messages  | GET    | `/users/me/messages`             | 5           |
| Get message    | GET    | `/users/me/messages/{id}`        | 5           |
| Send message   | POST   | `/users/me/messages/send`        | 100         |
| Modify message | POST   | `/users/me/messages/{id}/modify` | 5           |
| Batch modify   | POST   | `/users/me/messages/batchModify` | 50          |
| List history   | GET    | `/users/me/history`              | 2           |
| Watch          | POST   | `/users/me/watch`                | --          |
| Stop watch     | POST   | `/users/me/stop`                 | --          |
| List labels    | GET    | `/users/me/labels`               | 1           |
| Create label   | POST   | `/users/me/labels`               | 5           |

### Message Formats

The `format` query parameter on `GET /messages/{id}`:

- `full` -- headers + parsed MIME payload (default, used for sync)
- `metadata` -- headers only (use `metadataHeaders` param to filter)
- `minimal` -- ID + labels only
- `raw` -- base64url-encoded RFC 2822 (for archival)

### Rate Limits

- Per user: 15,000 quota units/minute
- Per project: 1,200,000 quota units/minute
- Batch requests: max 100 calls per batch, max 1,000 IDs for batchModify/batchDelete
- Sending: 2,000 emails/day (Google Workspace)

## Pub/Sub Notifications

### Setup

Single shared topic and subscription across all accounts. All accounts publish to the same topic; the subscriber identifies the account from the `emailAddress` field in each notification.

```
Topic:        projects/{project_id}/topics/{google_pubsub_topic}
Subscription: projects/{project_id}/subscriptions/{google_pubsub_subscription}
```

Infrastructure created idempotently by `setup_pubsub` in `src/mailpilot/pubsub.py`:

1. Create topic (catch `AlreadyExists`)
2. Set IAM policy on topic: grant `gmail-api-push@system.gserviceaccount.com` the `pubsub.publisher` role (skipped when already bound)
3. Create pull subscription with `ack_deadline_seconds=60` (catch `AlreadyExists`)

Each RPC runs inside its own `logfire.span` so a hung call surfaces directly in traces -- wrapping the whole setup in one span hid which step was stuck until everything finished.

### Watch Registration

Per account, call `users.watch()`:

```json
{
  "topicName": "projects/{project}/topics/{topic}",
  "labelIds": ["INBOX"]
}
```

Response:

```json
{
  "historyId": "12345",
  "expiration": "1718000000000"
}
```

- Expiration: 7 days from registration
- Renewal: `_renew_watches_if_due` runs once per `_WATCH_RENEWAL_INTERVAL` (1 hour). `pubsub.renew_watches` re-registers any account whose `watch_expiration` is missing or within 24 hours (`_WATCH_RENEWAL_THRESHOLD`)
- Store `watch_expiration` on the account row; if `watch()` returns a `historyId`, also write it to `gmail_history_id`

### Notification Format

Pub/Sub message `data` is base64-encoded JSON:

```json
{
  "emailAddress": "user@example.com",
  "historyId": "9876543210"
}
```

The `historyId` indicates what changed. The subscriber must call the History API to learn what specifically changed.

### Streaming Pull

`google-cloud-pubsub` `SubscriberClient.subscribe()` opens a persistent gRPC stream. The callback (`make_notification_callback` in `pubsub.py`) is intentionally minimal so the gRPC threads never block on Gmail or DB work:

1. Decode `message.data` (already-decoded raw bytes from the pubsub client) as JSON and read `emailAddress`
2. `sync_queue.put(emailAddress)`
3. `wakeup_event.set()` so the main loop drains the queue immediately instead of waiting for the next periodic tick
4. `message.ack()`

The `historyId` from the notification payload is intentionally ignored -- `sync_account` snapshots the mailbox's current historyId itself via `users.getProfile`. Look-up of the account row and the actual sync run on the main loop thread inside `_drain_sync_queue`, which is also where unknown email addresses are filtered out.

**Decode errors always ack, never nack.** Real Gmail watch payloads have triggered `binascii.Error` from a stale double-decode and `json.JSONDecodeError` / `KeyError` / `UnicodeDecodeError` for malformed messages (see #82). Nacking would cause infinite redelivery of the same poison message, so the callback logs a `pubsub.notification.decode_error` exception with the raw payload excerpt and acks anyway.

## Sync Pipeline

`sync_account` in `sync.py` is the per-account entry point. It uses a **checkpoint-first** pattern: snapshot `users.getProfile().historyId` *before* listing messages, then write that checkpoint as the account's new `gmail_history_id` at the end. Anything that arrives during the run still has a higher historyId and will be picked up on the next incremental sync.

### Initial Sync (no history ID)

When an account has no `gmail_history_id`, `_collect_new_message_ids` returns a full INBOX listing:

1. `list_messages(max_results=_FULL_SYNC_MAX_RESULTS, label_ids=["INBOX"])` -- 100 most recent INBOX message IDs
2. Filter out IDs already present in the `email` table (`get_email_by_gmail_message_id`) -- duplicates are harmless but skipping them avoids needless fetches
3. Batch-fetch the remaining IDs via `get_messages_batch` (one HTTP round-trip per 100 messages, 404s silently skipped)
4. Extract text and headers, store in `email` table; store the snapshot `historyId` and `last_synced_at` on the account

### Incremental Sync (has history ID)

When `gmail_history_id` is set:

1. `get_history(start_history_id=id, history_types=["messageAdded"], label_id="INBOX")` -- pages through `nextPageToken` automatically
2. Extract unique message IDs from `messagesAdded`
3. Same dedupe + batch fetch + store path as initial sync
4. Update `gmail_history_id` to the snapshot taken before the run, plus `last_synced_at`

### History 404 Fallback

If `get_history` returns 404 (history ID too old -- typically >7 days), `_collect_new_message_ids` logs `sync.account.history_fallback` and falls back to the full INBOX listing for this run. The fresh checkpoint from `getProfile` is then written as the new `gmail_history_id` at the end of `sync_account`, so the next sync resumes incrementally without an explicit "clear" step.

### Watch Renewal

`_renew_watches_if_due` runs once per `_WATCH_RENEWAL_INTERVAL` (1 hour) at the bottom of the main loop. `pubsub.renew_watches`:

1. Iterates accounts (`list_accounts(limit=1000)`)
2. Skips any whose `watch_expiration` is more than 24 hours away
3. For each remaining account: call `GmailClient.watch(topic)` and `update_account` with the new `watch_expiration` (and `gmail_history_id` when the response carries one)
4. Each per-account renewal is wrapped in its own `pubsub.watch_account` span; failures are logged via `logfire.exception` and never abort the sweep

## Error Handling

### Retries

`_retry_on_transient` in `gmail.py` decorates every Gmail API call. Each invocation runs inside a `gmail.<method>` span with `attempts` and final `status` attributes; transient retries are emitted as `gmail api transient error, retrying` warnings, and exhausting all retries logs `gmail.retry.exhausted` for alerting.

- Retry on: `_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504, 529}`
- Max attempts: `_MAX_RETRIES = 5`
- Backoff: `min(2**attempt, _MAX_BACKOFF)` with `_MAX_BACKOFF = 30.0` seconds
- Non-transient HTTP errors: raise immediately (status set as span attribute first)

### Per-Account Isolation

Account sync runs serially on the main loop thread (`_drain_sync_queue` and `_sync_all_accounts` iterate accounts one at a time). Each per-account call is wrapped in `try/except`: errors are recorded as `sync.notification.sync_failed` / `sync.account.run failed` exceptions and an `error` operator event, but never propagate to the next account or to the loop itself.

### Specific Cases

- 404 on `get_message`: returns `None` (message deleted between list and fetch)
- 404 inside `get_messages_batch`: silently skipped per message
- 404 on `get_history`: fall back to full INBOX listing (see History 404 Fallback)
- 401/403 on delegated access: surfaced as a sync error and logged; the next account proceeds
- Pub/Sub decode errors: log as `pubsub.notification.decode_error` and ack (#82). Never nack -- nacking poison messages causes infinite redelivery

## Email Sending

`GmailClient.send_message` builds a `multipart/alternative` envelope, base64url-encodes it, and POSTs to `users.messages.send`. The plain-text part is the agent's Markdown source; the HTML part is rendered by `email_renderer.render_email_html` (see ADR-02). Only the plain text is persisted on the `email` row.

Custom headers stamped inside `send_message` (always set):

- `X-MailPilot-Version` -- application version (read from package metadata)
- `X-MailPilot-Account-Id` -- the MailPilot account ID, when supplied by the caller

Threading inside Gmail: set `threadId` in the send body to continue an existing thread on the sender's side.

Threading across mail clients: set the RFC 5322 `In-Reply-To` and `References` headers on the MIME message. `sync._resolve_threading_headers` derives them automatically when the caller supplies a `thread_id`, or honours an explicit `in_reply_to` from the agent's `reply_email` tool. After the send, `sync.send_email` fetches the assigned `Message-ID` from Gmail (with a logged-and-tolerated failure path) and records it on the `email` row so future inbound replies can be matched via RFC 2822 message-id lookup. See ADR-04 for the matching counterpart.

## Consequences

### Positive

- Single-threaded sync loop -- no shared mutable state between accounts, no async/await coloring
- `google-api-python-client` handles auth, discovery, serialization -- less code to maintain
- `google-cloud-pubsub` handles gRPC reconnection, flow control, lease management
- Per-account try/except isolation -- one account's failure cannot cascade
- Incremental sync via history API + checkpoint pattern -- efficient and self-healing on 404
- Event-driven main loop (Pub/Sub + PG `LISTEN`) keeps real-time latency bounded by `wakeup_event` rather than `run_interval`
- No public endpoints needed -- pull-based Pub/Sub works behind firewalls

### Negative

- Google Workspace only (no consumer Gmail)
- Admin must approve domain-wide delegation
- 7-day history limit -- stale accounts require full re-sync
- `google-cloud-pubsub` pulls in gRPC (heavier dependency tree)
- Serial per-account sync inside one tick -- if account count grows past ~50, full sweeps may take longer than `run_interval`. Pub/Sub-driven syncs stay fast because they only sync the notified account.

## References

- [Gmail API Reference](https://developers.google.com/gmail/api/reference/rest)
- [Gmail Push Notifications](https://developers.google.com/gmail/api/guides/push)
- [Gmail Quota](https://developers.google.com/gmail/api/reference/quota)
- [Service Account Auth](https://developers.google.com/identity/protocols/oauth2/service-account)
- [Pub/Sub Streaming Pull](https://cloud.google.com/pubsub/docs/pull)
- `src/mailpilot/gmail.py` -- `GmailClient`, `_retry_on_transient`, `extract_text_from_message`
- `src/mailpilot/pubsub.py` -- `setup_pubsub`, `make_notification_callback`, `renew_watches`
- `src/mailpilot/sync.py` -- `start_sync_loop`, `sync_account`, `_resolve_threading_headers`, `send_email`
- `docs/adr-02-email-body-storage-strategy.md` -- inbound text extraction, outbound rendering
- `docs/adr-04-email-routing.md` -- routing pipeline (consumes the rows this sync writes) and RFC 2822 message-id matching for replies
- `docs/email-flow.md` -- end-to-end inbound and outbound flows
