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

**Goal:** Deterministic starting state.

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

### Gate 0

- `mailpilot account list` returns exactly 2 accounts.
- `mailpilot contact list` returns exactly 2 contacts.
- `mailpilot company list` returns exactly 1 company.

**On failure:** Stop. Report which entity failed to create and the error message.

### Logfire: Phase 0

Query spans for `db.status.counts` and any errors. Note whether entity creation produced clean traces or unexpected warnings.

---

## Phase 1: Outbound Email

**Goal:** The outbound agent composes and sends an email from `outbound@lab5.ca` to `inbound@lab5.ca`.

1. Create outbound workflow (creates in `active` status automatically -- no separate `start` needed):
   ```
   mailpilot workflow create \
     --name "Outbound Smoke Test" \
     --type outbound \
     --account-id <OUTBOUND_ACCOUNT_ID> \
     --objective "Send an email to the contact about <RANDOM_TOPIC>" \
     --instructions "You are a sales representative for Lab5. Send a brief email to the contact about <RANDOM_TOPIC>. Keep it under 3 sentences. Subject line MUST be exactly '<SUBJECT>' (the unique subject you generated for this run). Do not create follow-up tasks."
   ```
   Replace `<SUBJECT>` and `<RANDOM_TOPIC>` with the unique subject and topic you invented for this run. Save the workflow ID.
2. Enroll the inbound contact:
   ```
   mailpilot workflow contact add --workflow-id <OUTBOUND_WORKFLOW_ID> --contact-id <INBOUND_CONTACT_ID>
   ```
3. Run the agent:
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

## Phase 2: Inbound Workflow Setup + Sync + Routing

**Goal:** Create the inbound workflow before syncing so the LLM classifier can route the email, then sync and verify routing.

**Why this ordering matters:** `route_email` during sync classifies emails against active workflows only. If no active inbound workflow exists at sync time, the email is stored as unrouted (`workflow_id=NULL`) and will never be bridged to a task.

1. Create inbound workflow (creates in `active` status automatically -- no separate `start` needed):
   ```
   mailpilot workflow create \
     --name "Inbound Smoke Test" \
     --type inbound \
     --account-id <INBOUND_ACCOUNT_ID> \
     --objective "Respond to incoming emails professionally" \
     --instructions "You are a professional assistant for Lab5. Reply to the incoming email about the same topic they raised, acknowledging receipt and expressing interest. Keep it under 3 sentences. Subject line must preserve the existing thread subject exactly. Do not create follow-up tasks."
   ```
   Save the workflow ID.
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

**Goal:** The execution loop bridges the routed email to a task and the inbound agent sends a reply.

1. Set a short run interval:
   ```
   mailpilot config set run_interval 5
   ```
2. Run the execution loop with a timeout (30 seconds is enough for multiple iterations):
   ```
   timeout 30 mailpilot run
   ```
   This command will exit with code 124 (timeout) -- that is expected and not an error.
3. Restore the default run interval:
   ```
   mailpilot config set run_interval 30
   ```

**Note:** Unlike outbound (`workflow run`), inbound agent invocation only happens through the execution loop (`mailpilot run`). The loop syncs accounts, bridges routed emails to tasks via `create_tasks_for_routed_emails`, and executes pending tasks.

### Gate 3

- `mailpilot task list --workflow-id <INBOUND_WORKFLOW_ID>` shows at least 1 task with `"status": "completed"`.
- `mailpilot email list --account-id <INBOUND_ACCOUNT_ID> --direction outbound` shows at least 1 reply email.
- Note the reply email's `thread_id`. **Known limitation:** the `send_email` agent tool currently cannot reply in-thread (no `thread_id` / `In-Reply-To` / `References` support), so the reply will be sent as a new Gmail thread. Record the mismatch but do not fail the gate on this.
- Note how many total tasks were created. **Known issue:** `create_tasks_for_routed_emails` may bridge old historical emails to tasks (not just the smoke test email). Record the count of stale pending tasks.

**On failure:** Stop. Check `mailpilot task list --workflow-id <INBOUND_WORKFLOW_ID> --status failed` for agent errors. If no tasks exist at all, the email-to-task bridge did not fire -- verify the email has `workflow_id` set via `mailpilot email view <EMAIL_ID>`.

### Logfire: Phase 3

Query for these span families and review:

- `run.loop.iteration` -- How many iterations ran? Duration of each.
- `run.execute_task` -- Task execution: `task_id`, `workflow_id`, `contact_id`. Did it complete or fail?
- `agent.invoke` -- Inbound agent invocation: `trigger` should be `email`. Check `tool_call_count`, `agent_reasoning`, token usage.
- `agent.tool.send_email` -- Reply sent: check `to`. Note: `thread_id` will be missing (known limitation -- `send_email` does not support in-thread replies yet).
- `agent.tool.update_contact_status` -- Did the agent update the contact's workflow status?
- `agent.tool.noop` -- If called, why? The agent should have replied, not nooped.
- `run.sync.account_failed` -- Any sync errors during the loop?

Note: Is the task lifecycle (bridge -> execute -> complete) fully traceable from spans? Are error paths well-instrumented?

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
- Note whether the reply `thread_id` matches the original outbound email's `thread_id`. **Expected mismatch** due to the same `send_email` threading limitation noted in Gate 3.

**On failure:** Report "reply not received after 3 sync attempts." Phases 1-3 passed, so this is a delivery or sync issue on the return path.

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
  Inbound reply:     <id> | thread: <thread_id> (MISMATCH expected)
  Outbound received: <id> | thread: <thread_id> (MISMATCH expected)

Tasks:
  Outbound task: <id> (completed)
  Inbound task:  <id> (completed)

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

- Phase 0: ~5 seconds
- Phase 1: ~10 seconds
- Phase 2: ~15-60 seconds (Gmail delivery + sync retries)
- Phase 3: ~30 seconds (execution loop timeout)
- Phase 4: ~15-60 seconds (reply delivery + sync retries)
- Phase 5: ~1 second
