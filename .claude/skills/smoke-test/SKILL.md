---
name: smoke-test
description: End-to-end MailPilot smoke test against real Gmail (outbound@lab5.ca <-> inbound@lab5.ca). Drives the full production loop: entity setup, outbound agent send, background `mailpilot run` for sync/routing/task-drain, inbound agent reply, round-trip verification. Use whenever the user says "smoke test", "run end-to-end", "verify the system works", or after non-trivial changes to sync, routing, agent execution, or Pub/Sub code -- even if they don't explicitly invoke the skill by name.
---

# Smoke Test

Run a phased end-to-end smoke test using only `mailpilot` CLI commands. Each phase has a verification gate -- if a gate fails, stop immediately and report what broke.

Record the **test start time** as an ISO timestamp before Phase 0. You will use it for `--since` filters and Logfire time ranges later.

**Unique subject per run.** Before Phase 1, invent a random, creative email subject for this test run. The subject must be clearly distinguishable from any previous smoke test. Use this pattern: `[ST-<HHMMSS>] <random topic>` where `HHMMSS` comes from the test start time and `<random topic>` is a short, inventive phrase you make up (e.g., `[ST-104316] Quantum Llama Migration Strategy`, `[ST-153042] Artisanal Pickle Logistics Update`). This subject is used in both the outbound workflow instructions and all gate matching throughout the test. The inbound agent should reply about the same topic.

All commands below use `uv run mailpilot`. Parse JSON output from every command to extract IDs for subsequent steps.

**Sync mechanism: `mailpilot run`.** This skill drives sync via the unified background loop (`mailpilot run`), not ad-hoc `mailpilot account sync` calls. `mailpilot run` is started once after Phase 0 and runs in the background for the rest of the test. It handles:

- Pub/Sub real-time Gmail notifications (inbound and outbound mailboxes).
- Periodic sync at `run_interval` seconds (default 30s) as a fallback.
- PG `LISTEN/NOTIFY` for instant task execution after `INSERT INTO task`.
- Bridging routed inbound emails to tasks via `create_tasks_for_routed_emails`.
- Draining pending tasks by invoking the workflow agent.

Consequences for test flow: inbound sync, routing, task creation, and the inbound agent reply all happen automatically once an email lands in Gmail. The test waits and polls the DB instead of triggering each step. Outbound still requires an explicit `mailpilot workflow run` to create the initial task -- enrollment alone does not produce outbound tasks.

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
     --instructions "You are a sales representative for Lab5. Send a brief email to the contact about <RANDOM_TOPIC>. Subject line MUST be exactly '<SUBJECT>' (the unique subject you generated for this run). The email body MUST use Markdown formatting to test HTML rendering. Include: a greeting line, a short paragraph (2-3 sentences), and a Markdown table with 3 rows and 2 columns showing made-up data related to the topic (e.g., product names and prices, routes and ETAs, features and availability). End with a call-to-action sentence. Do not create follow-up tasks. After sending, mark the contact as completed."
   ```
   Replace `<SUBJECT>` and `<RANDOM_TOPIC>` with the unique subject and topic you invented for this run. Save the workflow ID.
6. Create inbound workflow:
   ```
   mailpilot workflow create \
     --name "Inbound Smoke Test" \
     --type inbound \
     --account-id <INBOUND_ACCOUNT_ID> \
     --objective "Respond to incoming emails professionally" \
     --instructions "You are a professional assistant for Lab5. Reply to the incoming email about the same topic they raised, acknowledging receipt and expressing interest. The reply body MUST use Markdown formatting. Include: a greeting, a short response paragraph (2-3 sentences), and a Markdown table with 2 rows and 2 columns showing your availability or next steps. Subject line must preserve the existing thread subject exactly. Do not create follow-up tasks. After replying, mark the contact as completed."
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

## Phase 0.5: Start Sync Loop

**Goal:** Start `mailpilot run` in the background so subsequent phases get sync, routing, and task execution for free.

**Do not modify `run_interval`.** The smoke test must run against the user's configured interval (default 30s) to emulate production behavior. Sync arrives in real time via Pub/Sub; the periodic fallback is only used if Pub/Sub fails.

1. Start the sync loop in the background. Use `Bash` with `run_in_background: true` and capture the bash_id:
   ```
   uv run mailpilot run
   ```
2. Wait briefly (~3 seconds) for startup, then read the background output and confirm:
   - `Sync loop started (pid <pid>)` is printed.
   - `Pub/Sub subscriber started` is printed (or a `Warning: Pub/Sub setup failed` if credentials are missing -- in that case, periodic sync alone will be slower but still functional).

### Gate 0.5

- `mailpilot status` (or DB query) shows a non-stale `sync_status` row with the loop's PID.
- The background `mailpilot run` process is still running.

**On failure:** Stop. If the loop exited immediately, read the captured stdout/stderr from the background bash. Common causes: stale `sync_status` row from a prior run, missing service account credentials, or invalid `database_url`.

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
- The outbound email `body_text` contains Markdown formatting: at least a `|` character (table) and `**` or `#` (formatting).
- `mailpilot workflow contact list --workflow-id <OUTBOUND_WORKFLOW_ID>` shows contact status is `completed`.

**On failure:** Stop. Report the `workflow run` output. Run `mailpilot task list --workflow-id <OUTBOUND_WORKFLOW_ID>` for task details. Common cause: missing `anthropic_api_key` -- check with `mailpilot config get anthropic_api_key`.

### Logfire: Phase 1

Query for these span families and review:

- `agent.invoke` -- Did it complete? Check `trigger`, `tool_call_count`, `input_tokens`, `output_tokens`, `agent_reasoning` attributes.
- `running tool` -- Were the expected tools called? Check `gen_ai.tool.name`, `tool_arguments`, `tool_response` attributes. (Tool spans are auto-generated by `instrument_pydantic_ai()` -- no custom `agent.tool.*` spans.)
- `gmail.send_message` -- Did the Gmail API call succeed? Check duration.
- `run.execute_task` -- Task lifecycle: was the task created and completed cleanly?
- Any `logfire.warn` or `logfire.exception` level spans -- unexpected warnings or errors.

Note: Are span attributes sufficient to debug a failure without reading code? Is anything noisy or redundant?

---

## Phase 2: Inbound Sync + Routing

**Goal:** Wait for the background `mailpilot run` loop to sync the inbound account and route the email to the inbound workflow.

**Why this works:** The inbound workflow was created in Phase 0 before the outbound agent sent the email in Phase 1. The email's `received_at` timestamp is after the workflow's `created_at`, so the `predates_workflows` check passes and LLM classification runs. Sync arrives via Pub/Sub (real-time) or the periodic fallback at `run_interval` seconds.

**Old messages from prior tests.** Email messages are never deleted from Gmail -- `make clean` only resets the database, not the mailboxes. Sync will store messages from prior smoke test runs alongside the current run's email. This is expected and intentional: old messages exercise the routing flow (they should route as `unrouted` or `thread_match` to prior workflows that no longer exist in the DB). Use the `--since` filter and unique `[ST-HHMMSS]` subject prefix to isolate the current run's email from prior test messages.

1. Enroll the outbound contact in the inbound workflow (so the inbound agent can mark them completed):
   ```
   mailpilot workflow contact add --workflow-id <INBOUND_WORKFLOW_ID> --contact-id <OUTBOUND_CONTACT_ID>
   ```
2. **Poll for the routed inbound email** (up to 12 attempts, 5 seconds apart -- ~60 seconds total). Do NOT call `mailpilot account sync`; rely on the background `mailpilot run` loop:
   ```
   mailpilot email list --account-id <INBOUND_ACCOUNT_ID> --direction inbound --since <TEST_START_ISO>
   ```
   Match the smoke test email by its unique subject (`[ST-<HHMMSS>]` prefix). Once found, fetch its detail:
   ```
   mailpilot email view <EMAIL_ID>
   ```
   Continue polling until `workflow_id` is set (routing complete). If `workflow_id` remains null after 60 seconds, the LLM classifier did not match -- stop and diagnose.

### Gate 2

- The smoke test email appears in `email list` for the inbound account.
- The email has `contact_id` set (auto-contact resolution worked).
- `mailpilot email view <EMAIL_ID>` shows:
  - `body_text` is non-empty.
  - `workflow_id` is set to the inbound workflow ID (routing succeeded).
  - `is_routed` is `true`.

**On failure:** Stop. If the email arrived but `workflow_id` is null after the polling window, the LLM classifier did not match it -- check the inbound workflow is active and has a clear objective. If the email never arrived, the background `mailpilot run` loop is not syncing -- read the captured stdout/stderr of the background bash process for Pub/Sub or sync errors. Report "email not delivered after 60 seconds" and include the outbound email ID for cross-reference.

### Logfire: Phase 2

Query for these span families and review:

- `cli.account.sync` -- Did sync complete? Check `total_stored`, `account_succeeded`, `account_failed` attributes.
- `sync.account` -- Per-account sync details: `messages_stored`, `sync_type` (history vs full).
- `gmail.list_messages` or `gmail.get_history` -- Gmail API calls: count, duration, any retries.
- `gmail.get_message` -- Per-message fetch: how many, duration per call.
- `routing.route_email` -- Was the email routed? Check `workflow_id`, `route_method` (`thread_match`, `classified`, `unrouted`).
- `classify_email` -- LLM classification: check `input_tokens`, `output_tokens`, `result` attributes. Was the classification correct?
- Any DB-level spans -- Are they useful or just noise? Note microsecond spans that clutter traces.

Note: Can you reconstruct the full sync-to-route pipeline from spans alone? What is missing?

---

## Phase 3: Inbound Agent Response

**Goal:** Wait for the background `mailpilot run` loop to bridge the routed email into a task, drain the task queue, and have the inbound agent reply.

**Why this works:** Once Phase 2 records the inbound email with `workflow_id` set, the run loop's next iteration calls `create_tasks_for_routed_emails`, which inserts a `task` row with the email attached. The PG `LISTEN/NOTIFY` listener fires immediately on insert, the task is drained, and `execute_task` invokes the inbound agent. No manual `workflow run` is required.

1. **Poll for task creation and completion** (up to 12 attempts, 5 seconds apart -- ~60 seconds total):
   ```
   mailpilot task list --workflow-id <INBOUND_WORKFLOW_ID>
   ```
   Look for a task with `email_id` set (the inbound smoke test email's ID) and `status = "completed"`. If still `pending`, wait and retry.
2. Once the task is completed, fetch its detail:
   ```
   mailpilot task view <TASK_ID>
   ```
3. List the inbound account's outbound emails to confirm the reply was sent:
   ```
   mailpilot email list --account-id <INBOUND_ACCOUNT_ID> --direction outbound --since <TEST_START_ISO>
   ```

### Gate 3

- A task exists for the inbound workflow with `email_id` set to the routed email's ID and `status = "completed"`.
- `mailpilot email list --account-id <INBOUND_ACCOUNT_ID> --direction outbound` shows at least 1 reply email since `<TEST_START_ISO>`.
- The reply email's `thread_id` matches the inbound email's `thread_id` (agent used `reply_email` to reply in-thread).
- The reply email `body_text` contains Markdown formatting: at least a `|` character (table).
- `mailpilot workflow contact list --workflow-id <INBOUND_WORKFLOW_ID>` shows contact status is `completed`.

**On failure:** Stop. If the task was never created, the run loop did not bridge the routed email -- check that Phase 2's email has `workflow_id` set and that the background `mailpilot run` is still alive. If the task exists but stayed `pending`, the task drain or PG `LISTEN/NOTIFY` is not firing -- read the captured stdout/stderr of the background bash. If the task is `failed`, run `mailpilot task view <TASK_ID>` for the failure reason.

### Logfire: Phase 3

Query for these span families and review:

- `run.execute_task` -- Task execution: `task_id`, `workflow_id`, `contact_id`. Did it complete or fail?
- `agent.invoke` -- Inbound agent invocation: `trigger` should be `email`. Check `tool_call_count`, `agent_reasoning`, token usage.
- `running tool` -- Check `gen_ai.tool.name` for `reply_email` (in-thread) rather than `send_email` (new thread). Verify `update_contact_status` was called.
- Any `noop` tool call -- If called, why? The agent should have replied, not nooped.

Note: Is the task lifecycle (create -> execute -> complete) fully traceable from spans? Are error paths well-instrumented?

---

## Phase 4: Round-Trip Verification

**Goal:** Wait for the background `mailpilot run` loop to sync the outbound account and confirm the reply arrived, completing the loop.

1. **Poll for the inbound reply on the outbound account** (up to 12 attempts, 5 seconds apart -- ~60 seconds total). Do NOT call `mailpilot account sync`; rely on the background `mailpilot run` loop:
   ```
   mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction inbound --since <TEST_START_ISO>
   ```
   Match by the unique subject (`[ST-<HHMMSS>]` prefix). If not found, wait 5 seconds and retry.

### Gate 4

- The reply email appears in `email list` for the outbound account matching the unique subject.
- Note whether the reply `thread_id` matches the original outbound email's `thread_id`. Gmail assigns different thread IDs per account, so a mismatch here is expected Gmail behavior, not a bug.

**On failure:** Report "reply not received after 60 seconds." Phases 1-3 passed, so this is a delivery or sync issue on the return path -- check the captured stdout/stderr of the background `mailpilot run` for Pub/Sub errors on the outbound account or watch-renewal failures.

**Expected: old messages route as `unrouted`.** The outbound account sync will store messages from prior smoke test runs. These old messages will appear as `unrouted` in routing spans because their original workflows no longer exist in the clean database. This is correct behavior -- the routing flow correctly identifies them as unroutable. Do not flag these as deficiencies in the report.

### Logfire: Phase 4

Query for sync spans during this phase. Same review as Phase 2 sync analysis but for the outbound account.

---

## Phase 4.5: Stop Sync Loop

**Goal:** Cleanly shut down the background `mailpilot run` loop before reporting.

1. Send `SIGTERM` (or `SIGINT`) to the background bash process running `mailpilot run` (e.g., `kill <pid>`). The loop will print `Sync loop stopped` and clean up its `sync_status` row.
2. Wait for the process to exit (~2-3 seconds).
3. Read the final stdout/stderr from the captured background bash output -- include any unexpected warnings or errors in the Phase 5 report.

### Gate 4.5

- The background `mailpilot run` process has exited cleanly (status 0, `Sync loop stopped` in output).
- `sync_status` table is empty (the loop deleted its row on shutdown).

**On failure:** If the process did not exit within 10 seconds of SIGTERM, send SIGKILL and note this in the report -- shutdown is supposed to be graceful.

---

## Phase 5: Report

### Part A: Phase Results

Print the pass/fail summary:

```
Smoke Test Results
==================

Phase 0:   Clean Slate ............. PASS
Phase 0.5: Start Sync Loop ......... PASS
Phase 1:   Outbound Email .......... PASS
Phase 2:   Inbound Sync + Routing .. PASS
Phase 3:   Inbound Agent Response .. PASS
Phase 4:   Round-Trip Verification . PASS
Phase 4.5: Stop Sync Loop .......... PASS

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

Expected total duration: 2-4 minutes.

- Phase 0: ~10 seconds (entities + workflows)
- Phase 0.5: ~5 seconds (start `mailpilot run`)
- Phase 1: ~10 seconds (outbound agent via `workflow run`)
- Phase 2: ~10-60 seconds (Gmail delivery + Pub/Sub-driven sync + LLM routing)
- Phase 3: ~10-60 seconds (run loop bridges email -> task -> drains -> agent replies)
- Phase 4: ~10-60 seconds (reply delivery + Pub/Sub-driven sync on outbound account)
- Phase 4.5: ~3 seconds (graceful shutdown of `mailpilot run`)
- Phase 5: ~1 second
