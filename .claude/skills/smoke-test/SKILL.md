---
name: smoke-test
description: End-to-end MailPilot smoke test against real Gmail (outbound@lab5.ca <-> inbound@lab5.ca). One Phase 0 setup, then two scenarios run sequentially without resetting state between them -- Scenario A exercises an outbound workflow with a manual operator reply, Scenario B exercises an inbound workflow with a manual trigger email. The outbound workflow stays active across Scenario B so the test verifies concurrent multi-account, multi-workflow operation. Use whenever the user says "smoke test", "run end-to-end", "verify the system works", or after non-trivial changes to sync, routing, agent execution, or Pub/Sub code -- even if they don't explicitly invoke the skill by name.
---

# Smoke Test

## What this tests

Two scenarios share a single Phase 0 setup and a single `mailpilot run` loop. The outbound workflow created in Scenario A stays active through Scenario B, so the test exercises real concurrent multi-workflow, multi-account operation. The agent-to-agent reply loop is prevented by two structural properties, not by isolation:

- Distinct subjects per scenario, so each Gmail thread is owned by exactly one workflow type (Scenario A's thread routes via `thread_match` to the outbound workflow; Scenario B's fresh thread routes via classification to the inbound workflow).
- Enrollments terminating with `record_enrollment_outcome`, so the agent stops replying once a scenario reaches its outcome.

| Scenario | Active workflows                       | Trigger                                  | Verifies                                                                                                        |
| -------- | -------------------------------------- | ---------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| A        | Outbound only                          | `mailpilot enrollment run`               | Outbound agent send -> Gmail delivery -> manual operator reply -> thread_match routing -> agent processes reply |
| B        | Outbound (terminal) + Inbound (active) | `mailpilot email send` (operator-driven) | Manual trigger email -> sync -> classification routing -> inbound agent reply -> Gmail delivery                 |

Default: run **both** scenarios in sequence. `make clean` runs **only once**, at the very start of the test. Operator can stop after Scenario A if only outbound is in scope.

## Conventions used throughout

- **Unique subject per scenario, freshly randomized.** Subject format: `[ST-<HHMMSS>] <topic>`. Generate the topic via Bash on every run -- do **not** invent it in your head, do **not** reuse topics from prior runs, and do **not** copy any topic shown in this skill. LLMs anchor on examples and have been observed reusing the same topic across runs, which makes traces collide and defeats the point of unique subjects. Recommended generator:

  ```bash
  TOPIC_A=$(sort -R /usr/share/dict/words 2>/dev/null \
    | grep -E '^[A-Za-z]{4,9}$' | head -2 | tr '\n' ' ' | sed 's/ *$//')
  SUBJECT_A="[ST-$(date +%H%M%S)] ${TOPIC_A}"
  ```

  Generate `SUBJECT_B` independently the same way; verify `SUBJECT_B != SUBJECT_A` before continuing. If `/usr/share/dict/words` is unavailable, fall back to `head -c 12 /dev/urandom | base32 | tr -d '=' | head -c 10` and use that as the topic.

- **Test start ISO timestamp.** Capture before each scenario; reuse for `--since` filters and Logfire time windows.
- **Polling.** When waiting for sync, routing, or agent results: poll up to **12 attempts, 5 seconds apart (~60s total)**. Do not call `mailpilot account sync` directly -- the background `mailpilot run` loop owns sync.
- **CLI parsing.** All commands use `uv run mailpilot`. Parse the JSON output of every command and extract IDs for the next step. Do not capture into a shell variable and re-emit with `echo "$VAR" | python3 -c ...` -- zsh's built-in `echo` interprets backslash escapes in the JSON (e.g. converts the literal two-char `\n` inside `body_text` into a real newline) and the resulting stream is no longer valid strict JSON. Either pipe `mailpilot ... | python3 -c ...` directly, or use `printf '%s' "$VAR"` to feed a captured value verbatim.
- **ASCII only.** No emojis. Use `->`, `--`, plain pipes.

## Prerequisites

Verify before starting:

- PostgreSQL running locally.
- `mailpilot config get google_application_credentials` returns a valid path.
- `mailpilot config get anthropic_api_key` returns a non-empty value.
- Network access to Gmail API and Anthropic API.

---

## Phase 0: Shared setup

Run **once** at the start of the test. Both scenarios reuse the same accounts, contacts, and company; do **not** repeat Phase 0 between scenarios.

1. `make clean` -- drops and re-applies the schema; mailbox contents on Gmail are untouched. Do not run `make clean` again until the next smoke test.
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
- `mailpilot workflow list` returns **0** workflows. Workflows are created per-scenario.

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
  --objective "Send a single email about <TOPIC_A> and mark the enrollment completed or failed based on the reply" \
  --instructions "You are a sales rep for Lab5. Send ONE email to the contact about <TOPIC_A>. Subject MUST be exactly '<SUBJECT_A>'. Body MUST use Markdown (greeting, 2-3 sentence paragraph, a 3-row 2-column table). When you receive a reply, do not send another email -- read the reply and call record_enrollment_outcome with status='completed' if the reply expresses interest or status='failed' if it declines, then stop. Do not call disable_contact -- this is per-workflow outcome tracking, not a global contact block. Do not create follow-up tasks."
```

Activate it if creation does not auto-activate:

```
mailpilot workflow start <OUTBOUND_WORKFLOW_ID>
```

Save `OUTBOUND_WORKFLOW_ID`.

### A2. Start the sync loop

Start `mailpilot run` in the background using `Bash` with `run_in_background: true`. Capture the bash_id so you can read its output later. This loop runs **once for the whole test** -- it stays up through Scenario B and is only stopped at the very end (Step B8).

The loop emits curated `event=...` lifecycle lines on stdout regardless of `--debug` (`loop.tick`, `sync.account`, `route.match`, `agent.run`, `task.drain`, `error`). Use `--debug` only when you also need Logfire's full span output for deep diagnosis.

```
uv run mailpilot --debug run
```

Wait ~3s, read the captured stdout, confirm:

- `Sync loop started (pid <pid>)` printed.
- `Pub/Sub subscriber started` printed (a `Warning: Pub/Sub setup failed` is acceptable -- periodic sync still works).
- At least one `event=loop.tick` line has appeared (proves the loop is actually ticking, not just started).

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
- `mailpilot enrollment list --workflow-id <OUTBOUND_WORKFLOW_ID>` shows enrollment status `active`. Per ADR-08 `enrollment.status` is operational only (`active` / `paused`); the agent never mutates it directly. The send-completion outcome lives in the activity timeline (verified in A8), not on the enrollment row.

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
- `workflow_id == null` (no inbound workflow exists yet -- the `routing.route_email` span emits `route_method=skipped_no_workflows`).
- `gmail_thread_id` is set. Save the inbound-side email ID as `INBOUND_SIDE_EMAIL_ID` for the reply.

**On failure:** Email never arrived after 60s -- read the captured `mailpilot run` output for Pub/Sub or sync errors.

### A5. Manual operator reply

Claude Code sends the reply directly via CLI -- no inbound agent is involved at this point because no inbound workflow has been created yet. The reply lands in the outbound mailbox, where it will be picked up by `thread_match` and handed to the outbound agent for terminal processing.

Choose reply content that gives the outbound agent a clear terminal signal so it marks the enrollment outcome and stops. Phrase the decline as "this opportunity is not a fit for our current priorities" rather than "remove us from your list" -- the latter steers the agent toward `disable_contact` (a global contact block) when we want it to call `record_enrollment_outcome` (the per-workflow outcome). Recommended template:

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

The run loop calls `create_tasks_for_routed_emails` once Step A6's email has `workflow_id` set, inserts a task, and the LISTEN/NOTIFY listener drains it.

Poll for task completion:

```
mailpilot task list --workflow-id <OUTBOUND_WORKFLOW_ID>
```

Wait for a task with `email_id` set to the routed reply and `status == "completed"`.

**Gate A7:**

- Task exists with `email_id == <routed reply id>` and `status == "completed"`.
- `mailpilot enrollment list --workflow-id <OUTBOUND_WORKFLOW_ID>` still shows status `active` -- by design (ADR-08, `enrollment.status` is operational only). The terminal outcome is recorded as an `enrollment_completed` or `enrollment_failed` activity row, verified in A8.
- **No additional outbound emails were sent.** Re-run `mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction outbound --since <TEST_START_A>` and confirm only the original outbound from A3 is present. If the count > 1, the agent kept replying despite the decline signal -- record this as a defect for the report.

**On failure:** If the task was never created, check that A6's email has `workflow_id` set and the run loop is alive. If the task is `failed`, `mailpilot task view <TASK_ID>` for the reason.

### A8. Verify the CRM activity timeline

The runtime paths emit `activity` rows automatically (no manual `activity create`). Read the inbound contact's timeline:

```
mailpilot activity list --contact-id <INBOUND_CONTACT_ID> --since <TEST_START_A>
```

**Gate A8 (activity wiring):** activity types follow the `enrollment_*` vocabulary defined in ADR-08.

- `enrollment_added` activity present with `detail.workflow_id == OUTBOUND_WORKFLOW_ID` (emitted by `enrollment add`).
- `email_sent` activity present with `summary == SUBJECT_A` (emitted by `email_ops.send_email` when the outbound agent sent in A3).
- `email_received` activity present with the operator-reply subject (emitted by sync's `_store_inbound_message` when the reply landed in the outbound mailbox in A6).
- Exactly one of `enrollment_completed` or `enrollment_failed` present (emitted by `agent.tools.record_enrollment_outcome` in A7); summary equals the agent's `reason`.
- No `tag_added` / `note_added` rows from this scenario (we did not run those CLI commands).

If any expected type is missing, the runtime activity wiring regressed for that path.

### Logfire review for Scenario A

Do this review now, before starting Scenario B, so the time window cleanly bounds A's spans. Use `/logfire:debug` with project=`mailpilot` and time window `[TEST_START_A, now]`. Spans to verify:

- `agent.invoke` -- exactly **2** invocations across all processes (A3 send invoked by foreground `enrollment run`, A7 reply handling drained by the background `mailpilot run` loop). The bg run-loop's stdout will only show the A7 invocation -- the A3 invocation lives in the fg `enrollment run` process. Logfire (or any process-agnostic trace store) shows both. More than 2 total means the agent kept replying (loop regression).
- `running tool` -- in A3 expect `send_email` plus optional context-gathering reads (`read_contact`, `read_company`); `record_enrollment_outcome` is **not** expected here (it fires after a reply, not on initial send). In A7 expect `record_enrollment_outcome` and **no** `send_email` or `reply_email`.
- `routing.route_email` -- the reply (A6) should show `route_method=thread_match` and `workflow_id == OUTBOUND_WORKFLOW_ID`. The inbound-side email from A4 should show `route_method=skipped_no_workflows` (no inbound workflow at the time).
- `gmail.send_message` -- 2 calls total (A3 by agent + A5 by operator).
- Any `is_exception=true` or `level=warn` spans -- record them.

---

## Transition to Scenario B

Do **not** stop the sync loop. Do **not** run `make clean`. Do **not** recreate accounts or contacts. The outbound workflow stays active with its enrollment in a terminal state, and the run loop keeps syncing both accounts. Scenario B layers an inbound workflow on top of this live state, which is the explicit multi-workflow / multi-account checkpoint of this test.

---

## Scenario B: Inbound workflow

**Hypothesis:** With the outbound workflow from Scenario A still active, an inbound workflow correctly classifies an operator-sent trigger email on a fresh thread, the agent generates a reply via the same run loop, and the reply round-trips to the outbound mailbox. The outbound workflow does not react to B's traffic because B's thread is not owned by it (no `thread_match` hit), and outbound workflows do not classify.

Capture `TEST_START_B` (ISO timestamp, must be later than Scenario A's last activity) and `SUBJECT_B` -- generate a fresh random topic per the Conventions section, distinct from `SUBJECT_A`.

### B1. Create the inbound workflow

Choose an objective and trigger-email body that pair cleanly so classification is unambiguous. Recommended (product question):

```
mailpilot workflow create \
  --name "Inbound Smoke B" \
  --type inbound \
  --account-id <INBOUND_ACCOUNT_ID> \
  --objective "Answer product questions about Lab5 services" \
  --instructions "You are a customer service rep for Lab5. Reply briefly to product questions about Lab5's services. Body MUST use Markdown (greeting, 2-3 sentence response, a 2-row 2-column table of services or next steps). Subject MUST preserve the incoming thread subject. After replying, call record_enrollment_outcome with status='completed'. Do not create follow-up tasks."
```

Activate if needed:

```
mailpilot workflow start <INBOUND_WORKFLOW_ID>
```

Save `INBOUND_WORKFLOW_ID`. Pre-enroll the sender so the agent can update the enrollment cleanly:

```
mailpilot enrollment add --workflow-id <INBOUND_WORKFLOW_ID> --contact-id <OUTBOUND_CONTACT_ID>
```

**Gate B1 (multi-workflow checkpoint):** `mailpilot workflow list` returns **2** workflows -- the outbound from Scenario A and the inbound just created -- both with status `active`. This is the explicit verification that MailPilot can run multiple workflows on multiple accounts simultaneously.

### B2. Confirm the sync loop is still alive

The `mailpilot run` process started in Step A2 has been syncing both accounts continuously. Read its captured stdout and confirm no fatal errors since the A-window Logfire review. If the process has died, restart it the same way as A2 and note the restart in the report.

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
- `mailpilot enrollment list --workflow-id <INBOUND_WORKFLOW_ID>` still shows status `active` -- by design (ADR-08, `enrollment.status` is operational only). The terminal outcome is recorded as an `enrollment_completed` activity row, verified in B7.

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
- `workflow_id == null` -- the outbound workflow exists and is active, but it does not own B's thread (no `thread_match`) and outbound workflows do not classify, so the reply correctly lands with `route_method=skipped_no_inbound_workflows` (account has active workflows, but none are inbound, so the classifier never runs).
- **No additional inbound replies.** Re-run `mailpilot email list --account-id <INBOUND_ACCOUNT_ID> --direction outbound --since <TEST_START_B>` and confirm only the single reply from B5 exists. More than one means the inbound agent kept replying despite the terminal `record_enrollment_outcome` call -- record as a defect.
- **Outbound workflow stayed quiet (concurrent-workflow check).** Re-run `mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction outbound --since <TEST_START_B>` and confirm zero new outbound sends. Any non-zero count means the still-active outbound workflow reacted to B's traffic -- record as a defect.

### B7. Verify the CRM activity timeline

Read the outbound contact's timeline (this is the contact whose enrollment was driven by the inbound workflow in B):

```
mailpilot activity list --contact-id <OUTBOUND_CONTACT_ID> --since <TEST_START_B>
```

**Gate B7 (activity wiring):** activity types follow the `enrollment_*` vocabulary defined in ADR-08.

- `enrollment_added` activity present with `detail.workflow_id == INBOUND_WORKFLOW_ID` (emitted by `enrollment add` in B1).
- `email_received` activity present with `summary == SUBJECT_B` (emitted by sync's `_store_inbound_message` when the trigger arrived in B4 on the inbound mailbox).
- `email_sent` activity present from the agent reply in B5 (subject begins with `Re:`).
- `enrollment_completed` activity present (emitted by `agent.tools.record_enrollment_outcome` after B5).

Note: re-reading the inbound contact's timeline from Scenario A is **not** a useful concurrent-quiet check. The inbound contact (recipient of A's outbound mail and counterparty for B's traffic on the outbound mailbox) will naturally accumulate `email_sent` / `email_received` rows during B because B's trigger and the agent's round-tripped reply both involve that contact. The authoritative concurrent-quiet check is Gate B6's "0 new outbound sends from the outbound account" assertion -- that is what proves the outbound workflow did not act.

### B8. Stop the sync loop

Send SIGTERM to the background `mailpilot run` (e.g. `kill <pid>`). Wait for `Sync loop stopped` in the captured output. Confirm the `sync_status` table is empty.

If the process does not exit within 10s, send SIGKILL and record this in the report.

### Logfire review for Scenario B

Time window `[TEST_START_B, now]`. Spans to verify:

- `agent.invoke` -- exactly **1** invocation (B5). More than 1 means either the inbound agent re-fired or the outbound workflow reacted to B's traffic.
- `routing.route_email` -- the inbound-side email (B4) should be `route_method=classified`. The outbound-side reply (B6) should be `route_method=skipped_no_inbound_workflows` (the outbound workflow is active but not an inbound classification candidate).
- `classify_email` -- 1 invocation (B4). Check `result` matches `INBOUND_WORKFLOW_ID`.
- `running tool` (B5) -- expect `reply_email` and `record_enrollment_outcome`.
- Any `is_exception=true` or `level=warn` spans -- record them.

---

## Scenario C: KB-grounded inbound auto-reply (optional)

Exercises the `list_drive_markdown` / `read_drive_markdown` agent tools by routing an inbound trigger through a workflow that cites a Google Drive folder of Markdown notes. Run only when changes touch `src/mailpilot/drive.py`, the two Drive tools, or the system prompt's KB grounding paragraph in `src/mailpilot/agent/invoke.py`. Run after Scenario B; reuses Phase 0 entities and the `mailpilot run` loop.

### Prerequisites (operator action, one-time)

Outside the test runner, the operator must:

1. Create a Google Drive folder named `mailpilot-kb-smoke` and grant the inbound service-account-impersonated user (`inbound@lab5.ca`) at least Viewer access.
2. Drop one Markdown file `lab5-faq.md` in that folder containing exactly one verifiable fact, e.g.:
   ```
   # Lab5 FAQ
   Lab5's office is at 1234 Industrial Way, Vancouver BC.
   ```
3. Capture the folder ID from the URL and export it as `KB_FOLDER_ID`.

If `KB_FOLDER_ID` is unset, skip Scenario C and record `SKIPPED (no KB folder configured)` in the report.

### C1. Create KB-grounded inbound workflow

```
mailpilot workflow create --name "KB Inbound" --type inbound --account-id <INBOUND_ACCOUNT_ID> \
  --objective "Answer questions using the KB folder; decline politely when the answer is not in the KB." \
  --instructions "You are an inbound assistant. The Drive folder ID is $KB_FOLDER_ID. Always ground replies in the Markdown files in that folder. If no listed file is relevant, decline politely and do not invent facts."
mailpilot workflow start <KB_WORKFLOW_ID>
mailpilot enrollment add --workflow-id <KB_WORKFLOW_ID> --contact-id <OUTBOUND_CONTACT_ID>
```

Disable the Scenario B inbound workflow (`workflow stop <INBOUND_WORKFLOW_ID>`) so the classifier picks the KB workflow unambiguously.

### C2. In-scope question -> grounded reply

Capture `TEST_START_C`. Generate `SUBJECT_C` per the conventions section. From the operator side:

```
mailpilot email send --account-id <OUTBOUND_ACCOUNT_ID> --to inbound@lab5.ca \
  --subject "<SUBJECT_C>" --body "Where is the Lab5 office located?"
```

Poll for the reply (12 attempts, 5s apart) on the outbound mailbox `--direction inbound --since <TEST_START_C>`.

**Gate C2:**
- Reply present, threaded under `SUBJECT_C`.
- Reply body mentions the verifiable fact from `lab5-faq.md` (`1234 Industrial Way`).
- `agent.invoke` Logfire span for the inbound run shows tool calls including `list_drive_markdown` and `read_drive_markdown` followed by `reply_email`.

### C3. Out-of-scope question -> polite decline

Generate a fresh `SUBJECT_C2`. Send a question whose answer is not in the KB:

```
mailpilot email send --account-id <OUTBOUND_ACCOUNT_ID> --to inbound@lab5.ca \
  --subject "<SUBJECT_C2>" --body "What was Lab5's revenue last quarter?"
```

**Gate C3:**
- Reply present.
- Reply body does **not** assert a revenue figure; phrasing reads as a polite decline (e.g. "I do not have that information").
- `agent.invoke` span shows `list_drive_markdown` was called (the agent satisfied the "must call at least one tool" invariant via the listing + `reply_email`, even on the decline path).

### C4. Stop the KB workflow

```
mailpilot workflow stop <KB_WORKFLOW_ID>
```

---

## Phase 5: Final report

Produce a report covering both scenarios (or just the one that was run).

### Part A: Phase results

```
Smoke Test Results
==================

Phase 0 (one-time setup) ..... PASS

Scenario A: Outbound workflow (sole workflow active)
  A1 Create workflow ......... PASS
  A2 Start sync loop ......... PASS
  A3 Outbound agent send ..... PASS
  A4 Gmail delivery (in) ..... PASS
  A5 Operator reply .......... PASS
  A6 thread_match routing .... PASS
  A7 Agent processes reply ... PASS
  A8 Activity timeline ....... PASS

Scenario B: Inbound workflow (outbound workflow still active)
  B1 Create workflow ......... PASS  (workflow list shows 2 active)
  B2 Sync loop still alive ... PASS
  B3 Operator trigger send ... PASS
  B4 classified routing ...... PASS
  B5 Agent reply ............. PASS
  B6 Gmail delivery (out) .... PASS
  B7 Activity timeline ....... PASS
  B8 Stop sync loop .......... PASS

Entity IDs (shared by both scenarios):
  Outbound account: <id>   Inbound account: <id>   Company: <id>
  Outbound contact: <id>   Inbound contact: <id>
  Outbound workflow: <id>  Inbound workflow: <id>

Email summary (Scenario A):
  Outbound send:    <id>  subject: <SUBJECT_A>
  Inbound delivery: <id>  skipped_no_workflows (expected -- no inbound workflow yet)
  Operator reply:   <id>  email_id: <INBOUND_SIDE_EMAIL_ID>
  Reply round-trip: <id>  workflow_id: <OUTBOUND_WORKFLOW_ID> via thread_match

Email summary (Scenario B):
  Operator trigger: <id>  subject: <SUBJECT_B>
  Inbound delivery: <id>  workflow_id: <INBOUND_WORKFLOW_ID> via classified
  Agent reply:      <id>  thread: <inbound thread>
  Reply round-trip: <id>  skipped_no_inbound_workflows (outbound workflow active but not an inbound classification candidate)

Loop sentinels:
  Scenario A: agent.invoke count == 2 (expected 2)
  Scenario B: agent.invoke count == 1 (expected 1)
  Outbound workflow during B: 0 new outbound sends (expected 0)
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

Write findings directly into the report. Do not file external tickets unless the user asks.

1. **CLI usability** -- commands that needed awkward sequencing or workarounds; missing fields in JSON output; error messages that did not point at the cause.
2. **Logfire observability** -- missing spans, missing attributes on existing spans, noisy span families (quantify with the volume query), broken parent-child causality, agent token/cost visibility.
3. **Agent behavior** -- did the agents follow instructions (subject, brevity, no extra tool calls)? Was `agent_reasoning` useful? Did `record_enrollment_outcome` get called when expected? In Scenario A, did the agent hold the line on "do not reply again"?
4. **Concurrent workflow safety** -- with both workflows active during Scenario B, did the outbound workflow stay quiet (zero new sends, no `agent.invoke` outside Scenario A's window)? Did the inbound workflow correctly leave A's lingering thread alone? Excess `agent.invoke` spans here are the high-priority signal from this run, since they would indicate that two simultaneously active workflows can interfere with each other.
5. **Other deficiencies** -- timing, race conditions, data integrity, performance.

---

## Timing

Expected total: ~6 minutes. Phase 0 runs once, the run loop runs once, and there is no reset between scenarios.

| Phase / scenario       | Duration |
| ---------------------- | -------- |
| Phase 0 (once)         | ~10s     |
| A1 / B1 workflow setup | ~5s      |
| A2 start run loop      | ~5s      |
| A3 outbound agent      | ~10s     |
| A4 / B4 sync + route   | ~10-60s  |
| A5 / B3 operator send  | ~3s      |
| A6 reply round-trip    | ~10-60s  |
| A7 / B5 task drain     | ~10-60s  |
| B6 reply round-trip    | ~10-60s  |
| A8 / B7 activity check | ~3s      |
| B8 stop run loop       | ~3s      |
| Report                 | ~10s     |
