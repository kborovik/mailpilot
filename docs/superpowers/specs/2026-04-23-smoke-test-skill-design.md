# Smoke Test Skill Design

## Problem

The existing e2e tests (`tests/e2e/`) cover basic connectivity: service account auth, sync pipeline, and outbound email delivery. They do not exercise the full agent loop -- workflow creation, agent invocation, email routing, inbound processing, and round-trip verification. There is no way for Claude Code to run a comprehensive smoke test of the system via CLI commands.

## Solution

A local project skill (`.claude/skills/smoke-test/SKILL.md`) that instructs Claude Code to execute a phased smoke test using only `mailpilot` CLI commands. The skill exercises the full agent loop between `outbound@lab5.ca` and `inbound@lab5.ca`, with verification gates between phases.

## Design

### Skill Location

```
.claude/skills/smoke-test/SKILL.md
```

Local project skill, committed to the repo. Triggered manually via `/smoke-test` or when Claude Code identifies a need to verify the system end-to-end.

### Database

Uses the default `mailpilot` database (not `mailpilot_test`). Starts with `make clean` for deterministic results. Leaves all data intact after completion for inspection.

### Test Entities

| Entity           | Value                         | Purpose                                                                 |
| ---------------- | ----------------------------- | ----------------------------------------------------------------------- |
| Outbound account | `outbound@lab5.ca`            | Sends cold outbound email                                               |
| Inbound account  | `inbound@lab5.ca`             | Receives email, auto-replies                                            |
| Company          | domain `lab5.ca`, name "Lab5" | Shared company for both contacts                                        |
| Outbound contact | `outbound@lab5.ca`            | Contact record for the outbound sender (enrolled in inbound workflow)   |
| Inbound contact  | `inbound@lab5.ca`             | Contact record for the inbound receiver (enrolled in outbound workflow) |

### Workflow Instructions

**Outbound workflow** ("Outbound Smoke Test"):

- Type: `outbound`
- Account: outbound account
- Objective: "Send an introductory email to the contact"
- Instructions: "You are a sales representative for Lab5. Send a brief introductory email to the contact. Keep it under 3 sentences. Subject line should include '[SMOKE TEST]' prefix. Do not create follow-up tasks."

**Inbound workflow** ("Inbound Smoke Test"):

- Type: `inbound`
- Account: inbound account
- Objective: "Respond to incoming emails professionally"
- Instructions: "You are a professional assistant for Lab5. Reply to the incoming email acknowledging receipt and expressing interest. Keep it under 3 sentences. Subject line must preserve the existing thread subject. Do not create follow-up tasks."

The "[SMOKE TEST]" prefix in the outbound subject makes it easy to identify test emails in Gmail and Logfire. The "do not create follow-up tasks" instruction keeps the smoke test bounded -- one send, one reply, done.

### Phases

#### Phase 0: Clean Slate

**Goal:** Deterministic starting state.

**Steps:**

1. Run `make clean` to drop and re-apply schema
2. Create outbound account: `mailpilot account create --email outbound@lab5.ca --display-name "Outbound Smoke"`
3. Create inbound account: `mailpilot account create --email inbound@lab5.ca --display-name "Inbound Smoke"`
4. Create company: `mailpilot company create --domain lab5.ca --name Lab5`
5. Create inbound contact (the recipient of outbound email): `mailpilot contact create --email inbound@lab5.ca --first-name Inbound --last-name Smoke --company-id <company_id>`
6. Create outbound contact (so inbound workflow can track the sender): `mailpilot contact create --email outbound@lab5.ca --first-name Outbound --last-name Smoke --company-id <company_id>`

**Verification gate:**

- `mailpilot account list` returns 2 accounts
- `mailpilot contact list` returns 2 contacts
- `mailpilot company list` returns 1 company

**On failure:** Stop. Report which entity failed to create and the error message.

#### Phase 1: Outbound Email

**Goal:** Agent composes and sends an email from `outbound@lab5.ca` to `inbound@lab5.ca`.

**Steps:**

1. Create outbound workflow: `mailpilot workflow create --name "Outbound Smoke Test" --type outbound --account-id <outbound_account_id> --objective "Send an introductory email to the contact" --instructions <outbound_instructions>`
2. Start workflow: `mailpilot workflow start <workflow_id>`
3. Enroll inbound contact: `mailpilot workflow contact add --workflow-id <workflow_id> --contact-id <inbound_contact_id>`
4. Run agent: `mailpilot workflow run --workflow-id <workflow_id> --contact-id <inbound_contact_id>`

**Verification gate:**

- `workflow run` output shows `status: completed` and `tool_calls >= 1`
- `mailpilot email list --account-id <outbound_account_id> --direction outbound` returns at least 1 email
- The outbound email subject contains "[SMOKE TEST]"
- `mailpilot workflow contact list --workflow-id <workflow_id>` shows the contact status changed from `pending`

**On failure:** Stop. Report the `workflow run` output, task status, and any error. Check `mailpilot task list --workflow-id <workflow_id>` for task details.

#### Phase 2: Inbound Workflow Setup + Sync + Routing

**Goal:** Create the inbound workflow _before_ syncing, so the LLM classifier can route the email. Then sync the inbound account and verify routing.

**Why this ordering matters:** `route_email` during sync classifies emails against active workflows. If no active inbound workflow exists at sync time, the email is stored as unrouted (`workflow_id=NULL`) and `create_tasks_for_routed_emails` will never bridge it. The inbound workflow must be active before the first sync.

**Steps:**

1. Create inbound workflow: `mailpilot workflow create --name "Inbound Smoke Test" --type inbound --account-id <inbound_account_id> --objective "Respond to incoming emails professionally" --instructions <inbound_instructions>`
2. Start workflow: `mailpilot workflow start <inbound_workflow_id>`
3. Sync inbound account: `mailpilot account sync --account-id <inbound_account_id>`
4. Check for the email: `mailpilot email list --account-id <inbound_account_id> --direction inbound --since <test_start_iso>`
5. If the smoke test email is not found, wait 15 seconds and repeat from step 3
6. Repeat up to 3 times (max ~45 seconds wait)

**Verification gate:**

- The smoke test email appears in `email list` for the inbound account
- The email has `contact_id` set (auto-contact resolution worked)
- `mailpilot email view <email_id>` shows:
  - `body_text` is non-empty
  - `workflow_id` is set to the inbound workflow ID (routing succeeded)
  - `is_routed` is `true`

**On failure:** Stop. If the email arrived but `workflow_id` is null, the LLM classifier did not match it to the inbound workflow -- check the workflow is active and the objective/instructions are clear. If the email did not arrive at all, report "email not delivered after 3 sync attempts" and include the outbound email ID for cross-reference.

#### Phase 3: Inbound Agent Response

**Goal:** The inbound workflow agent processes the routed email and sends a reply.

**Steps:**

1. Set a short run interval: `mailpilot config set run_interval 5`
2. Run the execution loop with a timeout: `timeout 30 mailpilot run` (runs for up to 30 seconds -- enough for multiple iterations: sync + bridge routed emails to tasks + execute tasks)
3. Restore run interval: `mailpilot config set run_interval 30`

**Note on inbound processing:** Unlike outbound (`workflow run`), inbound agent invocation only happens through the execution loop (`mailpilot run`). The loop syncs accounts, bridges routed emails to tasks, and executes pending tasks. Running it for 30 seconds with a 5-second interval gives ~5 iterations.

**Verification gate:**

- `mailpilot task list --workflow-id <inbound_workflow_id>` shows at least 1 task with `status: completed`
- `mailpilot email list --account-id <inbound_account_id> --direction outbound` shows at least 1 reply email
- The reply email has a `thread_id` matching the original conversation (it is a reply, not a new email)

**On failure:** Stop. Report task status and any agent errors. Check `mailpilot task list --workflow-id <inbound_workflow_id> --status failed` for failures. If no tasks exist, the email-to-task bridge did not fire -- check if the email was routed (`email view` should show `workflow_id` set).

#### Phase 4: Round-Trip Verification

**Goal:** The reply arrives back at the outbound account, confirming the full loop.

**Steps:**

1. Sync outbound account: `mailpilot account sync --account-id <outbound_account_id>`
2. Check for the reply: `mailpilot email list --account-id <outbound_account_id> --direction inbound --since <test_start_iso>`
3. If not found, wait 15 seconds and retry (up to 3 times, same pattern as Phase 2)

**Verification gate:**

- The reply email appears in `email list` for the outbound account
- The reply `thread_id` matches the original outbound email's `thread_id` (same Gmail thread)
- The full email chain has 3+ emails in the same thread: outbound send, inbound receive, inbound reply (outbound receive of reply)

**On failure:** Report "reply not received after 3 sync attempts." The outbound send and inbound processing succeeded (Phases 1-3 passed), so this is a delivery or sync issue on the return path.

#### Phase 5: Report

**Goal:** Summarize results for the operator.

**Output format:**

```
Smoke Test Report
=================

Phase 0: Clean Slate ............. PASS
Phase 1: Outbound Email .......... PASS
Phase 2: Inbound Sync + Routing .. PASS
Phase 3: Inbound Agent Response .. PASS
Phase 4: Round-Trip Verification . PASS

Entities:
  Outbound account: <id>
  Inbound account:  <id>
  Company:          <id>
  Outbound workflow: <id> (status: active)
  Inbound workflow:  <id> (status: active)

Emails:
  Outbound sent:     <id> | subject: [SMOKE TEST] ...
  Inbound received:  <id> | routed to: <inbound_workflow_id>
  Inbound reply:     <id> | thread: <thread_id>
  Outbound received: <id> | thread: <thread_id>

Tasks:
  Outbound task: <id> (completed)
  Inbound task:  <id> (completed)

All data left in database for inspection.
```

If any phase failed, the report stops at the failing phase with the failure reason and diagnostic commands to run.

### Error Handling

Each phase is a gate. If a gate fails, execution stops immediately. The report shows which phases passed and which failed, with actionable diagnostics.

Common failure modes and their diagnostics:

| Failure                | Phase | Likely cause                               | Diagnostic                                                      |
| ---------------------- | ----- | ------------------------------------------ | --------------------------------------------------------------- |
| Account create fails   | 0     | DB not running, schema issue               | Check `mailpilot status`                                        |
| `workflow run` fails   | 1     | Missing `anthropic_api_key`                | `mailpilot config get anthropic_api_key`                        |
| Agent sends no email   | 1     | Instructions unclear, model issue          | Check task result in `task view`                                |
| Email not synced       | 2     | Gmail delivery delay, auth issue           | Retry manually: `account sync`                                  |
| Email not routed       | 2     | Inbound workflow not active at sync time   | Check `email view` for `workflow_id`, verify workflow is active |
| Task not created       | 3     | `create_tasks_for_routed_emails` missed it | Check `task list`, verify email has `workflow_id`               |
| Agent fails on inbound | 3     | API key, model, instructions               | Check `task list --status failed`                               |
| Reply not received     | 4     | Gmail delivery delay                       | Retry sync, check Gmail directly                                |

### Timing

Expected total duration: 2-3 minutes.

- Phase 0: ~5 seconds (DB reset + entity creation)
- Phase 1: ~10 seconds (agent invocation + email send)
- Phase 2: ~15-60 seconds (Gmail delivery + sync retries)
- Phase 3: ~30 seconds (execution loop timeout)
- Phase 4: ~15-60 seconds (reply delivery + sync retries)
- Phase 5: ~1 second (report generation)

### Prerequisites

- PostgreSQL running locally
- Service account credentials configured: `mailpilot config get google_application_credentials`
- Anthropic API key configured: `mailpilot config get anthropic_api_key`
- Network access to Gmail API and Anthropic API
