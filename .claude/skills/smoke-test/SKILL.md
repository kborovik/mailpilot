---
name: smoke-test
description: End-to-end MailPilot smoke test against real Gmail (outbound@lab5.ca <-> inbound@lab5.ca). Runs two isolated scenarios sequentially -- Scenario A exercises an outbound workflow with a manual operator reply (no inbound workflow active), Scenario B exercises an inbound workflow with a manual trigger email (no outbound workflow active). Isolating one workflow type per run avoids the agent-to-agent reply loop tracked in issue #83. Use whenever the user says "smoke test", "run end-to-end", "verify the system works", or after non-trivial changes to sync, routing, agent execution, or Pub/Sub code -- even if they don't explicitly invoke the skill by name.
---

# Smoke Test

## What this tests

Two isolated scenarios. Each runs the full sync + routing + agent loop with exactly **one** workflow active, so the bug in issue #83 (two MailPilot-managed accounts on the same Gmail thread spawning an unbounded agent ping-pong) cannot fire.

| Scenario | Active workflow            | Trigger                                  | Verifies                                                                                                        |
| -------- | -------------------------- | ---------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| A        | Outbound only (no inbound) | `mailpilot enrollment run`               | Outbound agent send -> Gmail delivery -> manual operator reply -> thread_match routing -> agent processes reply |
| B        | Inbound only (no outbound) | `mailpilot email send` (operator-driven) | Manual trigger email -> sync -> classification routing -> inbound agent reply -> Gmail delivery                 |

Default: run **both** scenarios in sequence with `make clean` between them. Operator can stop after Scenario A if only outbound is in scope, or skip directly to Scenario B if only inbound matters.

## Conventions used throughout

- **Unique subject per scenario.** Generate a fresh `[ST-<HHMMSS>] <random topic>` per scenario (e.g., `[ST-104316] Quantum Llama Migration`). Different subject for A and B so traces don't collide.
- **Test start ISO timestamp.** Capture before each scenario; reuse for `--since` filters and Logfire time windows.
- **Polling.** When waiting for sync, routing, or agent results: poll up to **12 attempts, 5 seconds apart (~60s total)**. Do not call `mailpilot account sync` directly -- the background `mailpilot run` loop owns sync.
- **CLI parsing.** All commands use `uv run mailpilot`. Parse the JSON output of every command and extract IDs for the next step.
- **ASCII only.** No emojis. Use `->`, `--`, plain pipes.

## Prerequisites

Verify before starting:

- PostgreSQL running locally.
- `mailpilot config get google_application_credentials` returns a valid path.
- `mailpilot config get anthropic_api_key` returns a non-empty value.
- Network access to Gmail API and Anthropic API.

---

## Phase 0: Shared setup

Run once at the start of each scenario (Scenario B repeats this after `make clean`).

1. `make clean` -- drops and re-applies the schema; mailbox contents on Gmail are untouched.
2. Create accounts:
   ```
   mailpilot account create --email outbound@lab5.ca --display-name "Outbound Smoke"
   mailpilot account create --email inbound@lab5.ca --display-name "Inbound Smoke"
   ```
   Save `OUTBOUND_ACCOUNT_ID` and `INBOUND_ACCOUNT_ID`.
3. Create company:
   ```
   mailpilot company create --domain lab5.ca --name Lab5
   ```
   Save `COMPANY_ID`.
4. Create contacts (so contact resolution and enrollment have stable IDs to reference):
   ```
   mailpilot contact create --email inbound@lab5.ca --first-name Inbound --last-name Smoke --company-id <COMPANY_ID>
   mailpilot contact create --email outbound@lab5.ca --first-name Outbound --last-name Smoke --company-id <COMPANY_ID>
   ```
   Save `INBOUND_CONTACT_ID` (the recipient of outbound mail) and `OUTBOUND_CONTACT_ID` (the sender as seen by the inbound mailbox).

### Gate 0

- `mailpilot account list` returns 2 accounts.
- `mailpilot contact list` returns 2 contacts.
- `mailpilot company list` returns 1 company.
- `mailpilot workflow list` returns **0** workflows. Workflows are created per-scenario, not here.

**On failure:** Stop. Report which entity failed and the error JSON.

---

## Scenario A: Outbound workflow

**Hypothesis:** The outbound workflow can compose and send an email, and when the operator (Claude Code) replies manually, the outbound agent picks the reply up via thread_match, processes it, and reaches a terminal enrollment state without further auto-replies.

Capture `TEST_START_A` (ISO timestamp) and `SUBJECT_A` (`[ST-<HHMMSS>] <topic>`) before Step A1.

### A1. Create the outbound workflow

```
mailpilot workflow create \
  --name "Outbound Smoke A" \
  --type outbound \
  --account-id <OUTBOUND_ACCOUNT_ID> \
  --objective "Send a single email about <TOPIC> and mark the enrollment completed or failed based on the reply" \
  --instructions "You are a sales rep for Lab5. Send ONE email to the contact about <TOPIC>. Subject MUST be exactly '<SUBJECT_A>'. Body MUST use Markdown (greeting, 2-3 sentence paragraph, a 3-row 2-column table). When you receive a reply, do not send another email -- read the reply and call update_enrollment_status with status='completed' if the reply expresses interest or status='failed' if it declines, then stop. Do not call disable_contact -- this is per-workflow outcome tracking, not a global contact block. Do not create follow-up tasks."
```

Activate it if creation does not auto-activate:

```
mailpilot workflow start <OUTBOUND_WORKFLOW_ID>
```

Save `OUTBOUND_WORKFLOW_ID`.

### A2. Start the sync loop

Start `mailpilot run` in the background using `Bash` with `run_in_background: true`. Capture the bash_id so you can read its output later:

```
uv run mailpilot run
```

Wait ~3s, read the captured stdout, confirm:

- `Sync loop started (pid <pid>)` printed.
- `Pub/Sub subscriber started` printed (a `Warning: Pub/Sub setup failed` is acceptable -- periodic sync still works).

**Gate A2:** background process alive; `sync_status` row present.

### A3. Trigger the outbound agent

```
mailpilot enrollment add --workflow-id <OUTBOUND_WORKFLOW_ID> --contact-id <INBOUND_CONTACT_ID>
mailpilot enrollment run --workflow-id <OUTBOUND_WORKFLOW_ID> --contact-id <INBOUND_CONTACT_ID>
```

**Gate A3:**

- `enrollment run` output: `"status": "completed"` and `"tool_calls" >= 1`.
- `mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction outbound` shows the outbound email with `subject == SUBJECT_A`.
- The email's `body_text` contains `|` (table) and either `**` or `#` (Markdown).
- `mailpilot enrollment list --workflow-id <OUTBOUND_WORKFLOW_ID>` shows enrollment status `active` or `completed`.

Save `OUTBOUND_EMAIL_ID`.

**On failure:** Stop. `mailpilot task list --workflow-id <OUTBOUND_WORKFLOW_ID>` for task details. Common cause: missing `anthropic_api_key`.

### A4. Wait for Gmail delivery to the inbound mailbox

Poll the inbound account for the smoke-test email:

```
mailpilot email list --account-id <INBOUND_ACCOUNT_ID> --direction inbound --since <TEST_START_A>
```

Match by `SUBJECT_A`. When found, fetch detail:

```
mailpilot email view <INBOUND_SIDE_EMAIL_ID>
```

**Gate A4:**

- The email exists in the inbound account's inbound emails.
- `is_routed == true`.
- `workflow_id == null` (no inbound workflow exists -- routing correctly identifies it as `unrouted`).
- `gmail_thread_id` is set. Save the inbound-side email ID as `INBOUND_SIDE_EMAIL_ID` for the reply.

**On failure:** Email never arrived after 60s -- read the captured `mailpilot run` output for Pub/Sub or sync errors.

### A5. Manual operator reply (the key isolation step)

Claude Code sends the reply directly via CLI -- no inbound agent involved. This breaks the agent-to-agent loop because there is no inbound workflow to react to anything.

Choose reply content that gives the outbound agent a clear terminal signal so it marks the enrollment outcome and stops. Phrase the decline as "this opportunity is not a fit for our current priorities" rather than "remove us from your list" -- the latter steers the agent toward `disable_contact` (a global contact block) when we want it to call `update_enrollment_status` (the per-workflow outcome). Recommended template:

```
mailpilot email reply \
  --account-id <INBOUND_ACCOUNT_ID> \
  --email-id <INBOUND_SIDE_EMAIL_ID> \
  --body "Thanks for the email. After reviewing internally we have decided this opportunity is not a fit for our current priorities. Please consider this declined."
```

**Gate A5:** Command exits 0 and returns a JSON envelope with the new email's `id`. Save `REPLY_EMAIL_ID`.

### A6. Wait for the reply to route back via thread_match

Poll the outbound account for the inbound reply:

```
mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction inbound --since <TEST_START_A>
```

Match by `SUBJECT_A` (Gmail typically preserves the subject with a `Re:` prefix; match on the `[ST-<HHMMSS>]` portion). Fetch detail.

**Gate A6:**

- Email present in outbound account's inbound emails.
- `workflow_id == OUTBOUND_WORKFLOW_ID` (thread_match succeeded -- the prior outbound email in this thread is owned by this workflow).
- `is_routed == true`.

**On failure:** If the email arrived but `workflow_id` is null, thread_match did not connect the reply to the original send -- check that the original outbound email has `workflow_id` and `gmail_thread_id` set in the DB.

### A7. Wait for the outbound agent to process the reply

The run loop calls `create_tasks_for_routed_emails` once Phase A6's email has `workflow_id` set, inserts a task, and the LISTEN/NOTIFY listener drains it.

Poll for task completion:

```
mailpilot task list --workflow-id <OUTBOUND_WORKFLOW_ID>
```

Wait for a task with `email_id` set to the routed reply and `status == "completed"`.

**Gate A7:**

- Task exists with `email_id == <routed reply id>` and `status == "completed"`.
- `mailpilot enrollment list --workflow-id <OUTBOUND_WORKFLOW_ID>` shows enrollment status is `completed` or `failed` (terminal, agent-driven).
- **No additional outbound emails were sent.** Re-run `mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction outbound --since <TEST_START_A>` and confirm only the original outbound from A3 is present. If the count > 1, the agent kept replying despite the decline signal -- record this as a defect for the report.

**On failure:** If the task was never created, check that A6's email has `workflow_id` set and the run loop is alive. If the task is `failed`, `mailpilot task view <TASK_ID>` for the reason.

### A8. Stop the sync loop

Send SIGTERM to the background `mailpilot run` (e.g. `kill <pid>`). Wait for `Sync loop stopped` in the captured output. Confirm `sync_status` table is empty.

If the process does not exit within 10s, send SIGKILL and record this in the report.

### Logfire review for Scenario A

Use `/logfire:debug` with project=`mailpilot` and time window `[TEST_START_A, now]`. Spans to verify:

- `agent.invoke` -- exactly **2** invocations (A3 send + A7 reply handling). More than 2 means the agent kept replying (loop bug regression).
- `running tool` -- in A3 expect `send_email` and `update_enrollment_status` (or one of them). In A7 expect `update_enrollment_status` and **no** `send_email` or `reply_email`.
- `routing.route_email` -- the reply (A6) should show `route_method=thread_match` and `workflow_id == OUTBOUND_WORKFLOW_ID`. The inbound-side email from A4 should show `route_method=unrouted` (no inbound workflow).
- `gmail.send_message` -- 2 calls total (A3 by agent + A5 by operator).
- Any `is_exception=true` or `level=warn` spans -- record them.

---

## Reset between scenarios

If running both scenarios in one session, reset between them so Scenario B starts clean and no outbound workflow remnants can react to B's traffic:

1. Re-run `make clean` (drops the DB).
2. Re-run **Phase 0** (entities only). Save fresh account IDs and contact IDs -- they will differ from Scenario A.

---

## Scenario B: Inbound workflow

**Hypothesis:** An inbound workflow correctly classifies an operator-sent trigger email, the agent generates a reply via the run loop, and the reply round-trips to the outbound mailbox. No outbound workflow exists, so the inbound agent's reply lands in the outbound mailbox as `unrouted` and the loop terminates.

Capture `TEST_START_B` (ISO timestamp) and `SUBJECT_B` (different topic from A) before Step B1.

### B1. Create the inbound workflow

Choose an objective and trigger-email body that pair cleanly so classification is unambiguous. Recommended (product question):

```
mailpilot workflow create \
  --name "Inbound Smoke B" \
  --type inbound \
  --account-id <INBOUND_ACCOUNT_ID> \
  --objective "Answer product questions about Lab5 services" \
  --instructions "You are a customer service rep for Lab5. Reply briefly to product questions about Lab5's services. Body MUST use Markdown (greeting, 2-3 sentence response, a 2-row 2-column table of services or next steps). Subject MUST preserve the incoming thread subject. After replying, call update_enrollment_status with status='completed'. Do not create follow-up tasks."
```

Activate if needed:

```
mailpilot workflow start <INBOUND_WORKFLOW_ID>
```

Save `INBOUND_WORKFLOW_ID`. Pre-enroll the sender so the agent can update the enrollment cleanly:

```
mailpilot enrollment add --workflow-id <INBOUND_WORKFLOW_ID> --contact-id <OUTBOUND_CONTACT_ID>
```

### B2. Start the sync loop

Same as A2: `uv run mailpilot run` in the background, confirm startup, save bash_id.

### B3. Operator sends the trigger email

```
mailpilot email send \
  --account-id <OUTBOUND_ACCOUNT_ID> \
  --to inbound@lab5.ca \
  --subject "<SUBJECT_B>" \
  --body "Hi Lab5 team -- I am evaluating Lab5 for our procurement team. Can you describe what services you offer and how onboarding works? Looking forward to your response."
```

Save `TRIGGER_EMAIL_ID` (from the JSON output) and `TRIGGER_THREAD_ID` (`gmail_thread_id` -- this is the outbound-account-side thread).

**Gate B3:** Command exits 0 and returns a JSON envelope with the new email's `id`.

### B4. Wait for the inbound side to sync, classify, and route

Poll the inbound account:

```
mailpilot email list --account-id <INBOUND_ACCOUNT_ID> --direction inbound --since <TEST_START_B>
```

Match by `SUBJECT_B`. When found, fetch detail and wait until `workflow_id` is set.

**Gate B4:**

- Email present in inbound account's inbound emails.
- `is_routed == true`.
- `workflow_id == INBOUND_WORKFLOW_ID`.
- `route_method == classified` (verifiable from the `routing.route_email` Logfire span; classification, not thread_match, since this is a fresh thread on the inbound side).

Save `ROUTED_EMAIL_ID`.

**On failure:** If the email arrived but `workflow_id` is null after 60s, the LLM classifier did not match. Confirm the workflow is `active` and the objective phrasing matches the trigger body. Re-read inbound workflow with `mailpilot workflow view <INBOUND_WORKFLOW_ID>`.

### B5. Wait for the inbound agent to reply

The run loop bridges B4's routed email into a task and drains it.

Poll:

```
mailpilot task list --workflow-id <INBOUND_WORKFLOW_ID>
```

Wait for a task with `email_id == ROUTED_EMAIL_ID` and `status == "completed"`.

**Gate B5:**

- Task completed.
- `mailpilot email list --account-id <INBOUND_ACCOUNT_ID> --direction outbound --since <TEST_START_B>` returns at least 1 reply.
- The reply's `gmail_thread_id` matches the inbound side's thread of the routed email (in-thread via `reply_email`).
- The reply's `body_text` contains `|` (Markdown table preserved).
- `mailpilot enrollment list --workflow-id <INBOUND_WORKFLOW_ID>` shows enrollment status `completed`.

Save `INBOUND_REPLY_EMAIL_ID`.

**On failure:** No task -- check the run loop is alive and B4's email has `workflow_id` set. Task `failed` -- `mailpilot task view <TASK_ID>` for reason.

### B6. Wait for the reply to land in the outbound mailbox

Poll the outbound account:

```
mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction inbound --since <TEST_START_B>
```

Match by `SUBJECT_B` (likely with a `Re:` prefix on Gmail's side).

**Gate B6:**

- Email present in outbound account's inbound emails.
- `is_routed == true`.
- `workflow_id == null` (no outbound workflow exists -- routes as `unrouted`, which is correct).
- **No additional inbound replies.** Re-run `mailpilot email list --account-id <INBOUND_ACCOUNT_ID> --direction outbound --since <TEST_START_B>` and confirm only the single reply from B5 exists. More than one means the agent kept replying despite the terminal `update_enrollment_status` call -- record as a defect.

### B7. Stop the sync loop

Same as A8.

### Logfire review for Scenario B

Time window `[TEST_START_B, now]`. Spans to verify:

- `agent.invoke` -- exactly **1** invocation (B5). More than 1 means the run loop is creating tasks for emails it should have ignored.
- `routing.route_email` -- the inbound-side email (B4) should be `route_method=classified`. The outbound-side reply (B6) should be `route_method=unrouted`.
- `classify_email` -- 1 invocation (B4). Check `result` matches `INBOUND_WORKFLOW_ID`.
- `running tool` (B5) -- expect `reply_email` and `update_enrollment_status`.
- Any `is_exception=true` or `level=warn` spans -- record them.

---

## Phase 5: Final report

Produce a report covering both scenarios (or just the one that was run).

### Part A: Phase results

```
Smoke Test Results
==================

Scenario A: Outbound workflow
  Phase 0 (setup) ............ PASS
  A1 Create workflow ......... PASS
  A2 Start sync loop ......... PASS
  A3 Outbound agent send ..... PASS
  A4 Gmail delivery (in) ..... PASS
  A5 Operator reply .......... PASS
  A6 thread_match routing .... PASS
  A7 Agent processes reply ... PASS
  A8 Stop sync loop .......... PASS

Scenario B: Inbound workflow
  Phase 0 (setup) ............ PASS
  B1 Create workflow ......... PASS
  B2 Start sync loop ......... PASS
  B3 Operator trigger send ... PASS
  B4 classified routing ...... PASS
  B5 Agent reply ............. PASS
  B6 Gmail delivery (out) .... PASS
  B7 Stop sync loop .......... PASS

Entity IDs (Scenario A):
  Outbound account: <id>   Inbound account: <id>   Company: <id>
  Outbound workflow: <id>  Outbound contact: <id>  Inbound contact: <id>

Entity IDs (Scenario B):
  Outbound account: <id>   Inbound account: <id>   Company: <id>
  Inbound workflow: <id>   Outbound contact: <id>  Inbound contact: <id>

Email summary (Scenario A):
  Outbound send:    <id>  subject: <SUBJECT_A>
  Inbound delivery: <id>  unrouted (expected -- no inbound workflow)
  Operator reply:   <id>  email_id: <INBOUND_SIDE_EMAIL_ID>
  Reply round-trip: <id>  workflow_id: <OUTBOUND_WORKFLOW_ID> via thread_match

Email summary (Scenario B):
  Operator trigger: <id>  subject: <SUBJECT_B>
  Inbound delivery: <id>  workflow_id: <INBOUND_WORKFLOW_ID> via classified
  Agent reply:      <id>  thread: <inbound thread>
  Reply round-trip: <id>  unrouted (expected -- no outbound workflow)

Loop sentinel:
  Scenario A: agent.invoke count == 2 (expected 2)
  Scenario B: agent.invoke count == 1 (expected 1)
```

If a phase failed, stop Part A at the failing phase with the failure JSON and any captured stdout from the background `mailpilot run`.

### Part B: Cross-cutting Logfire pass

Use `/logfire:debug` with the test time window. Run once across both scenarios:

```sql
-- Volume by span name (find noise)
SELECT span_name, COUNT(*) AS count, AVG(duration) AS avg_ms
FROM records
WHERE start_timestamp >= '<EARLIEST_TEST_START>'
GROUP BY span_name
ORDER BY count DESC
LIMIT 30
```

```sql
-- Errors and warnings
SELECT start_timestamp, span_name, message, attributes
FROM records
WHERE start_timestamp >= '<EARLIEST_TEST_START>'
  AND (is_exception = true OR level = 'warn')
ORDER BY start_timestamp
LIMIT 50
```

```sql
-- Agent invocations (one row per agent.invoke)
SELECT start_timestamp, attributes->>'workflow_type' AS type,
       attributes->>'trigger' AS trigger,
       attributes->>'tool_call_count' AS tools,
       attributes->>'input_tokens' AS in_tok,
       attributes->>'output_tokens' AS out_tok
FROM records
WHERE start_timestamp >= '<EARLIEST_TEST_START>'
  AND span_name = 'agent.invoke'
ORDER BY start_timestamp
LIMIT 50
```

### Part C: Suggestions

Write findings directly into the report -- do not file GitHub issues unless the user asks.

1. **CLI usability** -- commands that needed awkward sequencing or workarounds; missing fields in JSON output; error messages that did not point at the cause.
2. **Logfire observability** -- missing spans, missing attributes on existing spans, noisy span families (quantify with the volume query), broken parent-child causality, agent token/cost visibility.
3. **Agent behavior** -- did the agents follow instructions (subject, brevity, no extra tool calls)? Was `agent_reasoning` useful? Did `update_enrollment_status` get called when expected? In Scenario A, did the agent hold the line on "do not reply again"?
4. **Loop guardrails (issue #83)** -- did either scenario produce more `agent.invoke` spans than expected? If so, the isolation strategy worked but the underlying loop bug surfaced anyway, and that is the high-priority signal from this run.
5. **Other deficiencies** -- timing, race conditions, data integrity, performance.

---

## Timing

Expected total: ~7 minutes when running both scenarios.

| Phase / scenario       | Duration |
| ---------------------- | -------- |
| Phase 0 (each)         | ~10s     |
| A1 / B1 workflow setup | ~5s      |
| A2 / B2 start run loop | ~5s      |
| A3 outbound agent      | ~10s     |
| A4 / B4 sync + route   | ~10-60s  |
| A5 / B3 operator send  | ~3s      |
| A6 reply round-trip    | ~10-60s  |
| A7 / B5 task drain     | ~10-60s  |
| B6 reply round-trip    | ~10-60s  |
| Stop loop (each)       | ~3s      |
| Reset between          | ~10s     |
| Report                 | ~10s     |
