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

**Google API Python Client (synchronous) + ThreadPoolExecutor for per-account concurrency.** The application manages a small number of accounts (not thousands of connections), so asyncio adds complexity without proportional benefit. `google-api-python-client` is Google's official library -- battle-tested, handles auth refresh, discovery, and serialization. ThreadPoolExecutor gives natural per-account parallelism for I/O-bound work.

**Pub/Sub streaming pull via `google-cloud-pubsub`.** The official library handles gRPC reconnection, flow control, and message leasing. Its callback-based subscriber runs in background threads -- a natural fit for the threading model.

### Dependencies

| Package                    | Purpose                                       |
| -------------------------- | --------------------------------------------- |
| `google-api-python-client` | Gmail API client (service objects, discovery) |
| `google-auth`              | Service account credentials + token refresh   |
| `google-cloud-pubsub`      | Pub/Sub streaming pull subscriber             |
| `html2text`                | HTML-to-plain-text conversion for email body  |

### Concurrency Model

```
Main Thread
  |
  +-- PubSub SubscriberClient (background gRPC threads, managed by library)
  |     |
  |     +-- callback: decode notification, submit sync task to executor
  |
  +-- ThreadPoolExecutor (per-account sync)
  |     |
  |     +-- Thread: sync account A (history API -> fetch messages -> store)
  |     +-- Thread: sync account B (independent, isolated)
  |     +-- ...
  |
  +-- Periodic tasks (watch renewal, stale account check)
```

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

Infrastructure created idempotently:

1. Create topic (catch `AlreadyExists`)
2. Create pull subscription (ack deadline 10s, retention 7 days)
3. Set IAM policy on topic: grant `gmail-api-push@system.gserviceaccount.com` the `pubsub.publisher` role

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
- Renewal: call `watch()` again daily (idempotent)
- Store `watch_expiration` on the account row

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

`google-cloud-pubsub` `SubscriberClient.subscribe()` opens a persistent gRPC stream. The callback:

1. Decode the base64 `data` field
2. Look up the account by `emailAddress`
3. Submit a sync task to the ThreadPoolExecutor
4. `message.ack()`

On decode errors: `message.nack()` for Pub/Sub retry.

## Sync Pipeline

### Initial Sync (no history ID)

When an account has no `gmail_history_id`:

1. `list_messages(query="", max_results=500)` -- list recent message IDs
2. Fetch each message with `format=full`
3. Extract text, headers, store in `email` table
4. Record the `historyId` from the list response on the account

### Incremental Sync (has history ID)

On Pub/Sub notification for an account:

1. `get_history(start_history_id=id)` -- get changes since last sync
2. Page through results (follow `nextPageToken`)
3. For each `messageAdded`: fetch full message, store in `email` table
4. Update `gmail_history_id` on the account to the latest history ID
5. Update `last_synced_at`

### History 404 Fallback

If history API returns 404 (history ID too old -- typically >7 days):

1. Clear `gmail_history_id` on the account
2. Trigger initial sync (full re-sync)
3. Log the event for observability

### Watch Renewal

Periodic check (hourly):

1. Query accounts where `watch_expiration` is within 24 hours
2. For each: call `watch()` to re-register
3. Update `watch_expiration` and `gmail_history_id` on the account

## Error Handling

### Retries

Exponential backoff with `time.sleep` for transient errors:

- Retry on: 429 (rate limit), 500, 502, 503, 504, 529
- Max attempts: 5
- Max backoff: 30 seconds
- Non-transient 4xx: raise immediately

### Per-Account Isolation

Each account syncs in its own thread via ThreadPoolExecutor. Errors are caught and logged per account -- they do not propagate to other accounts or crash the sync loop.

### Specific Cases

- 404 on message fetch: skip silently (message deleted between list and fetch)
- 404 on history: fallback to initial sync
- 401/403 on delegated access: log error, skip account (delegation may have been revoked)
- Pub/Sub callback errors: nack message for redelivery

## Email Sending

Outgoing emails use `send_message()` with a base64url-encoded RFC 2822 message.

Custom headers on all outgoing emails for traceability:

- `X-MailPilot-Version` -- application version
- `X-MailPilot-Account-Id` -- sending account ID

Threading: set `threadId` in the request body to continue an existing Gmail thread.

## Consequences

### Positive

- Simple concurrency model -- threads for parallelism, no async/await coloring
- `google-api-python-client` handles auth, discovery, serialization -- less code to maintain
- `google-cloud-pubsub` handles gRPC reconnection, flow control, lease management
- Per-account thread isolation -- one account's failure cannot cascade
- Incremental sync via history API -- efficient, minimal API quota usage
- No public endpoints needed -- pull-based Pub/Sub works behind firewalls

### Negative

- Google Workspace only (no consumer Gmail)
- Admin must approve domain-wide delegation
- 7-day history limit -- stale accounts require full re-sync
- `google-cloud-pubsub` pulls in gRPC (heavier dependency tree)
- Thread overhead per account (negligible at expected scale of 5-50 accounts)

## References

- [Gmail API Reference](https://developers.google.com/gmail/api/reference/rest)
- [Gmail Push Notifications](https://developers.google.com/gmail/api/guides/push)
- [Gmail Quota](https://developers.google.com/gmail/api/reference/quota)
- [Service Account Auth](https://developers.google.com/identity/protocols/oauth2/service-account)
- [Pub/Sub Streaming Pull](https://cloud.google.com/pubsub/docs/pull)
