# ADR-04: Gmail Watch Implementation with Pub/Sub Pull

## Status

Needs review

## Context

The Pilot application needs to receive near real-time notifications when new emails arrive in Gmail accounts. Gmail provides a push notification system through their Watch API, which publishes messages to Google Cloud Pub/Sub when mailbox changes occur.

Three approaches considered:

1. **Webhook (Push) Delivery**: Pub/Sub pushes messages to an HTTPS endpoint
2. **Pub/Sub Pull**: Application pulls messages from Pub/Sub subscription
3. **Polling Only**: Periodically call Gmail API to check for changes

## Decision

**Pub/Sub streaming pull** for production email watching.

### Architecture

1. **Automatic Infrastructure Creation**: `ensure_pubsub_resources()` creates Google Cloud resources idempotently:
   - Create Pub/Sub topic (catches `AlreadyExists`)
   - Create pull subscription (10-min ack deadline, 7-day message retention)
   - Grant `gmail-api-push@system.gserviceaccount.com` publisher role on topic

2. **Streaming Pull Processing** (`PubSubSubscriber`):
   - `subscriber_client.subscribe()` opens persistent streaming connection with callback
   - Messages decoded: `{"emailAddress": "user@example.com", "historyId": "12345"}`
   - Accounts queued to thread-safe `pending_sync_accounts` set
   - Messages ACKed immediately on decode; NACKed on errors for Pub/Sub retry

3. **Batched Sync** (every 5 seconds):
   - `_process_pending_syncs()` drains the pending set and syncs each account
   - `GmailSyncEngine.sync_account()` performs history-based incremental sync
   - Synced emails routed through `process_incoming_emails()` for classification

4. **Watch Auto-Renewal** (hourly check):
   - `_check_watch_renewals()` queries accounts expiring within 24 hours
   - Calls `watch_manager.setup_watch()` to re-register with Gmail
   - Updates `gmail_history_id` and `watch_expiration` in database

5. **Complementary Sync**:
   - Initial sync: full message list (latest 500) when no history ID exists
   - History 404 fallback: re-syncs from scratch if history ID is stale
   - CLI `pilot dev poll`: manual sync for development/testing

### Server Lifecycle

`ServerManager` handles process management:

- Foreground mode: blocking with stderr logging (development)
- Daemon mode: double-fork with file logging (production)
- PID file tracking at `{project_root}/.pilot/server.pid`
- Graceful shutdown via SIGTERM (30s timeout, then SIGKILL)
- `ThreadPoolExecutor` with 2 workers for periodic tasks (sync + watch renewal)

## Consequences

### Positive

- **Simplified Deployment**: No public endpoints, SSL certificates, or domain verification needed
- **Better Security**: No exposed webhook endpoints; all communication is outbound
- **Flexibility**: Works in any environment (local development, behind firewalls, cloud)
- **Reliability**: Pub/Sub provides message persistence and retry; NACK returns messages for redelivery
- **Cost Effective**: Pull subscriptions have no additional costs beyond message volume
- **Development Friendly**: Easier to test and debug locally

### Negative

- **Complexity**: Requires Google Cloud SDK and Pub/Sub permissions
- **Resource Usage**: Streaming connection consumes network resources
- **Dependency**: Adds dependency on Google Cloud Pub/Sub service

### Neutral

- **API Quota**: Both approaches consume similar Gmail API quota for actual email fetching
- **Message Format**: Same notification format regardless of delivery method

## Implementation Notes

### Required Permissions

The service account needs:

- `pubsub.topics.create`
- `pubsub.subscriptions.create`
- `pubsub.subscriber` role for pulling messages
- Gmail API `gmail.modify` scope for watch registration

### Resource Naming

Single shared topic and subscription across all accounts (configurable via env vars):

```
Topic: projects/{project}/topics/{GOOGLE_PUBSUB_TOPIC}         (default: "gmail-watch")
Subscription: projects/{project}/subscriptions/{GOOGLE_PUBSUB_SUBSCRIPTION}  (default: "pilot-watch")
```

All accounts publish to the same topic. The subscriber identifies the account from the `emailAddress` field in each notification message.

### Error Handling

- **Pub/Sub messages**: ACK on successful decode, NACK on errors (Pub/Sub retries)
- **Gmail API**: Retry with exponential backoff for transient errors (429, 5xx)
- **History sync**: Falls back to initial sync on 404 or stale history ID
- **Watch renewal**: Per-account try/except -- failures logged but don't block other accounts
- **Periodic tasks**: Wrapped in try/except -- failures logged, next cycle continues

### Key Files

- `src/pilot/gmail/watch.py` -- `GmailWatchManager`, `ensure_pubsub_resources()`
- `src/pilot/server/pubsub_subscriber.py` -- `PubSubSubscriber`, streaming pull, periodic tasks
- `src/pilot/server/manager.py` -- `ServerManager`, daemon lifecycle
- `src/pilot/gmail/sync.py` -- `GmailSyncEngine`, history-based sync

## References

- [Gmail Push Notifications Guide](https://developers.google.com/gmail/api/guides/push)
- [Google Cloud Pub/Sub Pull Documentation](https://cloud.google.com/pubsub/docs/pull)
- [Gmail API Quotas and Limits](https://developers.google.com/gmail/api/reference/quota)
