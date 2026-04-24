---
name: smoke-test
description: Run a full end-to-end smoke test of the MailPilot system between outbound@lab5.ca and inbound@lab5.ca. Exercises the complete agent loop -- entity setup, outbound email send, inbound sync, email routing, inbound agent reply, and round-trip verification. Use when you need to verify the system works end-to-end after changes.
---

# Smoke Test

Run a phased end-to-end smoke test using only `mailpilot` CLI commands. Each phase has a verification gate -- if a gate fails, stop immediately and report what broke.

Record the **test start time** as an ISO timestamp before Phase 0. You will use it for `--since` filters and Logfire time ranges later.

**Unique subject per run.** Before Phase 1, invent a random, creative email subject for this test run. The subject must be clearly distinguishable from any previous smoke test. Use this pattern: `[ST-<HHMMSS>] <random topic>` where `HHMMSS` comes from the test start time and `<random topic>` is a short, inventive phrase you make up (e.g., `[ST-104316] Quantum Llama Migration Strategy`, `[ST-153042] Artisanal Pickle Logistics Update`). This subject is used in both the outbound workflow instructions and all gate matching throughout the test. The inbound agent should reply about the same topic.

All commands below use `uv run mailpilot`. Parse JSON output from every command to extract IDs for subsequent steps.

**Logfire analysis:** After each phase's verification gate passes, use `/logfire:debug` to query Logfire (project `mailpilot`) for all spans generated during that phase. Use `start_timestamp` and `end_timestamp` to scope queries to the phase's time window. Note anomalies: errors, missing spans, excessive noise, unhelpful attributes, slow operations. Collect these observations for the final report in Phase 5.

---

## Phase 0: Clean Slate

**Goal:** Deterministic starting state with all entities and both workflows created upfront.

**Why workflows are created here:** The `predates_workflows` optimization in sync skips LLM classification for emails whose `received_at` predates the inbound workflow's `created_at`. Creating the inbound workflow before the outbound agent sends the email guarantees the email's `received_at` will be after the workflow's `created_at`, so routing works correctly.

1. Run `make clean` to drop and re-apply the schema.
2. Create accounts:
   ```
   mailpilot account create --email outbound@lab5.ca --display-name "Outbound Smoke"
   mailpilot account create --email inbound@lab5.ca --display-name "Inbound Smoke"
   ```
   Save both account IDs.
3. Create company:
   ```
   mailpilot company create --domain lab5.ca --name Lab5
   ```
   Save the company ID.
4. Create contacts:
   ```
   mailpilot contact create --email inbound@lab5.ca --first-name Inbound --last-name Smoke --company-id <COMPANY_ID>
   mailpilot contact create --email outbound@lab5.ca --first-name Outbound --last-name Smoke --company-id <COMPANY_ID>
   ```
   Save both contact IDs. The "inbound contact" is the recipient of outbound email. The "outbound contact" lets the inbound workflow track the sender.
5. Create outbound workflow (creates in `active` status automatically -- no separate `start` needed):
   ```
   mailpilot workflow create \
     --name "Outbound Smoke Test" \
     --type outbound \
     --account-id <OUTBOUND_ACCOUNT_ID> \
     --objective "Send an email to the contact about <RANDOM_TOPIC>" \
     --instructions "You are a sales representative for Lab5. Send a brief email to the contact about <RANDOM_TOPIC>. Keep it under 3 sentences. Subject line MUST be exactly '<SUBJECT>' (the unique subject you generated for this run). Do not create follow-up tasks."
   ```
   Replace `<SUBJECT>` and `<RANDOM_TOPIC>` with the unique subject and topic you invented for this run. Save the workflow ID.
6. Create inbound workflow:
   ```
   mailpilot workflow create \
     --name "Inbound Smoke Test" \
     --type inbound \
     --account-id <INBOUND_ACCOUNT_ID> \
     --objective "Respond to incoming emails professionally" \
     --instructions "You are a professional assistant for Lab5. Reply to the incoming email about the same topic they raised, acknowledging receipt and expressing interest. Keep it under 3 sentences. Subject line must preserve the existing thread subject exactly. Do not create follow-up tasks."
   ```
   Save the workflow ID.

### Gate 0

- `mailpilot account list` returns exactly 2 accounts.
- `mailpilot contact list` returns exactly 2 contacts.
- `mailpilot company list` returns exactly 1 company.
- `mailpilot workflow list` returns exactly 2 workflows (both `active`).

**On failure:** Stop. Report which entity failed to create and the error message.

### Logfire: Phase 0

Query spans for `db.status.counts` and any errors. Note whether entity creation produced clean traces or unexpected warnings.

---

## Phase 1: Outbound Email

**Goal:** The outbound agent composes and sends an email from `outbound@lab5.ca` to `inbound@lab5.ca`.

1. Enroll the inbound contact in the outbound workflow:
   ```
   mailpilot workflow contact add --workflow-id <OUTBOUND_WORKFLOW_ID> --contact-id <INBOUND_CONTACT_ID>
   ```
2. Run the agent:
   ```
   mailpilot workflow run --workflow-id <OUTBOUND_WORKFLOW_ID> --contact-id <INBOUND_CONTACT_ID>
   ```

### Gate 1

- The `workflow run` output shows `"status": "completed"` and `"tool_calls"` >= 1.
- `mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction outbound` returns at least 1 email.
- The outbound email subject matches the unique subject you generated for this run.
- `mailpilot workflow contact list --workflow-id <OUTBOUND_WORKFLOW_ID>` -- note whether the contact status changed from `pending` (agent behavior is non-deterministic here; status update is desirable but not a hard gate).

**On failure:** Stop. Report the `workflow run` output. Run `mailpilot task list --workflow-id <OUTBOUND_WORKFLOW_ID>` for task details. Common cause: missing `anthropic_api_key` -- check with `mailpilot config get anthropic_api_key`.

### Logfire: Phase 1

Query for these span families and review:

- `agent.invoke` -- Did it complete? Check `trigger`, `tool_call_count`, `input_tokens`, `output_tokens`, `agent_reasoning` attributes.
- `agent.tool.send_email` -- Was the email tool called? Check `to`, `workflow_id` attributes.
- `gmail.send` (or `gmail.*`) -- Did the Gmail API call succeed? Check duration.
- `run.execute_task` -- Task lifecycle: was the task created and completed cleanly?
- Any `logfire.warn` or `logfire.exception` level spans -- unexpected warnings or errors.

Note: Are span attributes sufficient to debug a failure without reading code? Is anything noisy or redundant?

---

## Phase 2: Inbound Sync + Routing

**Goal:** Sync the inbound account and verify the email is routed to the inbound workflow.

**Why this works:** The inbound workflow was created in Phase 0 before the outbound agent sent the email in Phase 1. The email's `received_at` timestamp is after the workflow's `created_at`, so the `predates_workflows` check passes and LLM classification runs.

**Old messages from prior tests.** Email messages are never deleted from Gmail -- `make clean` only resets the database, not the mailboxes. Sync will store messages from prior smoke test runs alongside the current run's email. This is expected and intentional: old messages exercise the routing flow (they should route as `unrouted` or `thread_match` to prior workflows that no longer exist in the DB). Use the `--since` filter and unique `[ST-HHMMSS]` subject prefix to isolate the current run's email from prior test messages.

1. Enroll the outbound contact in the inbound workflow:
   ```
   mailpilot workflow contact add --workflow-id <INBOUND_WORKFLOW_ID> --contact-id <OUTBOUND_CONTACT_ID>
   ```
2. **Time-boxed sync retry loop** (up to 3 attempts, 15 seconds apart):
   ```
   mailpilot account sync --account-id <INBOUND_ACCOUNT_ID>
   mailpilot email list --account-id <INBOUND_ACCOUNT_ID> --direction inbound --since <TEST_START_ISO>
   ```
   Look for the smoke test email by matching the unique subject (`[ST-<HHMMSS>]` prefix). If not found, wait 15 seconds and retry. Max 3 attempts (~45 seconds total).

### Gate 2

- The smoke test email appears in `email list` for the inbound account.
- The email has `contact_id` set (auto-contact resolution worked).
- `mailpilot email view <EMAIL_ID>` shows:
  - `body_text` is non-empty.
  - `workflow_id` is set to the inbound workflow ID (routing succeeded).
  - `is_routed` is `true`.

**On failure:** Stop. If the email arrived but `workflow_id` is null, the LLM classifier did not match it -- check the inbound workflow is active and has a clear objective. If the email did not arrive at all, report "email not delivered after 3 sync attempts" and include the outbound email ID for cross-reference.

### Logfire: Phase 2

Query for these span families and review:

- `cli.account.sync` -- Did sync complete? Check `total_stored`, `account_succeeded`, `account_failed` attributes.
- `sync.account` -- Per-account sync details: `messages_stored`, `sync_type` (history vs full).
- `gmail.list_messages` or `gmail.get_history` -- Gmail API calls: count, duration, any retries.
- `gmail.get_message` -- Per-message fetch: how many, duration per call.
- `routing.route_email` -- Was the email routed? Check `workflow_id`, `route_method` (thread_match vs classification).
- `classify_email` -- LLM classification: check `input_tokens`, `output_tokens`, `result` attributes. Was the classification correct?
- Any DB-level spans -- Are they useful or just noise? Note microsecond spans that clutter traces.

Note: Can you reconstruct the full sync-to-route pipeline from spans alone? What is missing?

---

## Phase 3: Inbound Agent Response

**Goal:** The inbound agent replies to the routed email.

1. Run the inbound agent directly:
   ```
   mailpilot workflow run --workflow-id <INBOUND_WORKFLOW_ID> --contact-id <OUTBOUND_CONTACT_ID>
   ```
   This uses the `workflow run` inbound support to find the unprocessed inbound email via `get_unprocessed_inbound_email`, create a task with the email attached, and execute the agent.

### Gate 3

- The `workflow run` output shows `"status": "completed"` and `"tool_calls"` >= 1.
- The task has `email_id` set (the unprocessed email was found and attached).
- `mailpilot email list --account-id <INBOUND_ACCOUNT_ID> --direction outbound` shows at least 1 reply email.
- The reply email's `thread_id` matches the inbound email's `thread_id` (agent used `reply_email` to reply in-thread).

**On failure:** Stop. Check the `workflow run` output for errors. If `email_id` is null, the email was not found by `get_unprocessed_inbound_email` -- verify the email has `workflow_id` set via `mailpilot email view <EMAIL_ID>` and that the contact is enrolled in the workflow.

### Logfire: Phase 3

Query for these span families and review:

- `run.execute_task` -- Task execution: `task_id`, `workflow_id`, `contact_id`. Did it complete or fail?
- `agent.invoke` -- Inbound agent invocation: `trigger` should be `email`. Check `tool_call_count`, `agent_reasoning`, token usage.
- `agent.tool.reply_email` -- Reply sent: check `email_id`, `workflow_id`. Verify the agent used `reply_email` (in-thread) rather than `send_email` (new thread).
- `agent.tool.update_contact_status` -- Did the agent update the contact's workflow status?
- `agent.tool.noop` -- If called, why? The agent should have replied, not nooped.

Note: Is the task lifecycle (create -> execute -> complete) fully traceable from spans? Are error paths well-instrumented?

---

## Phase 4: Round-Trip Verification

**Goal:** The reply arrives back at the outbound account, confirming the full loop.

1. **Time-boxed sync retry loop** (up to 3 attempts, 15 seconds apart):
   ```
   mailpilot account sync --account-id <OUTBOUND_ACCOUNT_ID>
   mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction inbound --since <TEST_START_ISO>
   ```
   Match by the unique subject (`[ST-<HHMMSS>]` prefix). If not found, wait 15 seconds and retry.

### Gate 4

- The reply email appears in `email list` for the outbound account matching the unique subject.
- Note whether the reply `thread_id` matches the original outbound email's `thread_id`. Gmail assigns different thread IDs per account, so a mismatch here is expected Gmail behavior, not a bug.

**On failure:** Report "reply not received after 3 sync attempts." Phases 1-3 passed, so this is a delivery or sync issue on the return path.

**Expected: old messages route as `unrouted`.** The outbound account sync will store messages from prior smoke test runs. These old messages will appear as `unrouted` in routing spans because their original workflows no longer exist in the clean database. This is correct behavior -- the routing flow correctly identifies them as unroutable. Do not flag these as deficiencies in the report.

### Logfire: Phase 4

Query for sync spans during this phase. Same review as Phase 2 sync analysis but for the outbound account.

---

## Phase 5: Report

### Part A: Phase Results

Print the pass/fail summary:

```
Smoke Test Results
==================

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
  Outbound sent:     <id> | subject: <unique subject>
  Inbound received:  <id> | routed to: <inbound_workflow_id>
  Inbound reply:     <id> | thread: <thread_id> (in-thread via reply_email)
  Outbound received: <id> | thread: <thread_id>

Tasks:
  Outbound task: <id> (completed)
  Inbound task:  <id> (completed, email_id set)

All data left in database for inspection.
```

If any phase failed, stop Part A at the failing phase with the failure reason and diagnostics.

### Part B: Execution Analysis

Use `/logfire:debug` to run a comprehensive analysis of all spans generated during the smoke test (from test start to now). Query Logfire with `project='mailpilot'` scoped to the test time window.

Run these queries:

1. **Full trace overview**: All top-level spans ordered by time. Get the big picture of what happened.
   ```sql
   SELECT start_timestamp, span_name, duration, is_exception, attributes
   FROM records
   WHERE start_timestamp >= '<TEST_START_ISO>'
     AND is_exception = false
   ORDER BY start_timestamp
   LIMIT 200
   ```

2. **Errors and warnings**: All exception or warning-level spans.
   ```sql
   SELECT start_timestamp, span_name, message, attributes
   FROM records
   WHERE start_timestamp >= '<TEST_START_ISO>'
     AND (is_exception = true OR level = 'warn')
   ORDER BY start_timestamp
   LIMIT 50
   ```

3. **Agent invocations**: Both outbound and inbound agent runs with token usage and tool calls.
   ```sql
   SELECT start_timestamp, span_name, duration, attributes
   FROM records
   WHERE start_timestamp >= '<TEST_START_ISO>'
     AND span_name LIKE 'agent.%'
   ORDER BY start_timestamp
   LIMIT 50
   ```

4. **Gmail API calls**: All external API calls with durations.
   ```sql
   SELECT start_timestamp, span_name, duration, attributes
   FROM records
   WHERE start_timestamp >= '<TEST_START_ISO>'
     AND span_name LIKE 'gmail.%'
   ORDER BY start_timestamp
   LIMIT 50
   ```

5. **Span volume by name**: Identify noisy span families.
   ```sql
   SELECT span_name, COUNT(*) as count, AVG(duration) as avg_duration_ms
   FROM records
   WHERE start_timestamp >= '<TEST_START_ISO>'
   GROUP BY span_name
   ORDER BY count DESC
   LIMIT 30
   ```

### Part C: Suggestions

Based on all observations from per-phase Logfire analysis and the Part B queries, write a structured suggestions section. Present this directly in the report output -- do NOT create a GitHub issue automatically. The user will decide whether to file issues from the suggestions.

Organize findings into these sections:

**1. CLI API Usability**

Evaluate the CLI from the perspective of an LLM operator that just ran 20+ commands in sequence. Look for:
- Commands that required awkward workarounds or extra steps.
- Missing commands that would have simplified the test (e.g., a `task run` for inbound, a `workflow run` that works for both types).
- Output format problems: missing fields, fields that should be present but are null, IDs that are hard to cross-reference.
- Validation gaps: commands that silently succeed when they should warn.
- Any command that produced confusing or unhelpful error output.

**2. Logfire Observability**

Evaluate the traces from the perspective of someone debugging a production issue. Look for:
- **Missing spans**: operations that happened but produced no trace (gaps in the story).
- **Missing attributes**: spans that exist but lack the context needed to debug (e.g., an error span without the input that caused it).
- **Noise**: span families that fire too often or carry too little information (e.g., sub-millisecond DB spans that clutter traces). Quantify with the span volume query.
- **Broken causality**: parent-child span relationships that are missing or wrong (operations that should be nested but appear as siblings).
- **Error instrumentation**: are error paths well-traced? Can you tell *why* something failed from the span alone?
- **LLM cost visibility**: are token counts, model name, and request duration on agent spans? Enough to estimate cost per workflow run?

**3. Agent Behavior**

Evaluate what the Pydantic AI agents actually did. Look for:
- Did the outbound agent follow instructions (subject prefix, brevity, no follow-up tasks)?
- Did the inbound agent follow instructions (reply in thread, brevity, no follow-up tasks)?
- Did agents call unexpected tools or make unnecessary tool calls?
- Was the `agent_reasoning` output useful or generic?
- Were contact statuses updated appropriately?

**4. System Deficiencies**

Anything else the smoke test revealed:
- Race conditions, timing issues, or flaky behavior.
- Data integrity problems (missing FKs, null fields that should be set).
- Performance concerns (slow operations, excessive API calls).
- Missing functionality that the test exposed as a gap.

---

## Prerequisites

Before running this smoke test, verify:

- PostgreSQL is running locally.
- Service account credentials: `mailpilot config get google_application_credentials` returns a valid path.
- Anthropic API key: `mailpilot config get anthropic_api_key` returns a non-empty value.
- Network access to Gmail API and Anthropic API.

## Timing

Expected total duration: 2-3 minutes.

- Phase 0: ~10 seconds (entities + workflows)
- Phase 1: ~10 seconds (outbound agent)
- Phase 2: ~15-60 seconds (Gmail delivery + sync retries)
- Phase 3: ~10 seconds (inbound agent via workflow run)
- Phase 4: ~15-60 seconds (reply delivery + sync retries)
- Phase 5: ~1 second
