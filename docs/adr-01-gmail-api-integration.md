# ADR-02: Gmail API Integration Approach

## Status

Needs review

## Context

Need Gmail sync. Options: IMAP (limited), OAuth2 (complex auth), Service Account (server-to-server).

Requirements: No user interaction, full email access, batch operations, label management, send on behalf of users.

## Decision

Gmail API with Service Account + domain-wide delegation for Google Workspace.

## Implementation

### Authentication

- Service account with domain-wide delegation via `google.oauth2.service_account`
- Credential resolution: `GOOGLE_APPLICATION_CREDENTIALS` env var, falls back to Application Default Credentials (ADC)
- Per-account impersonation via `.with_subject(email)` for delegated access
- Single scope: `https://www.googleapis.com/auth/gmail.modify`

### Gmail API Operations

| Operation     | Method                         | Purpose                                            |
| ------------- | ------------------------------ | -------------------------------------------------- |
| Get Profile   | `get_profile()`                | Fetch user profile                                 |
| List Messages | `list_messages()`              | Query inbox with Gmail search syntax               |
| Get Message   | `get_message()`                | Fetch full message (headers, payload, attachments) |
| Send Message  | `send_message()`               | Send email, optionally in thread                   |
| Modify Labels | `modify_message()`             | Add/remove labels on messages                      |
| Batch Modify  | `batchModify()`                | Apply labels to multiple messages                  |
| Batch Delete  | `batch_delete_messages()`      | Permanently delete (max 1000/request)              |
| Get History   | `get_history()`                | Incremental sync since history ID                  |
| Watch         | `watch()`                      | Setup Pub/Sub push notifications                   |
| Create Label  | `create_label_if_not_exists()` | Create or get label ID (with cache)                |

### Push Notifications (Cloud Pub/Sub)

- Gmail `watch()` on INBOX label with Pub/Sub topic (7-day expiration, auto-renewal)
- Grant `gmail-api-push@system.gserviceaccount.com` publisher role on topic
- Shared topic/subscription across all accounts (see ADR-04 for details)
- Fallback: history-based sync on 404 or missing history ID

### API Error Handling

- Retry decorator with exponential backoff (max 5 attempts, max 30s backoff)
- Transient errors retried: 429 (rate limit), 500, 502, 503, 504, 529
- Non-transient errors (other 4xx): raised immediately
- 404 on message fetch: silently skipped (message deleted between list/fetch)
- Label operations: best-effort (errors logged, not raised)

### Account Setup Flow

1. `account create` CLI command creates `DbAccount`
2. `setup_gmail_watch_for_account()` called automatically
3. Pub/Sub resources created if needed (topic, subscription, IAM binding)
4. `gmail_client.watch()` registers INBOX notifications
5. Account updated with `gmail_history_id` and `watch_expiration`

## Consequences

**Positive**: No OAuth complexity, full API access, batch support, unified Google auth, real-time processing via Pub/Sub, efficient incremental sync via history API

**Negative**: Google Workspace only, admin approval needed for domain-wide delegation, 250 quota units/user/second, Pub/Sub costs, 7-day Gmail history limit

## Security

- TLS everywhere
- Delegated access scoped to specific accounts
- Custom headers on sent emails (`X-MailPilot-Version`, `X-MailPilot-Account-Id`) for traceability
- Label cache with thread-safe locking

## References

- [Gmail API](https://developers.google.com/gmail/api)
- [Service Accounts](https://developers.google.com/identity/protocols/oauth2/service-account)
- [Push Notifications](https://developers.google.com/gmail/api/guides/push)
