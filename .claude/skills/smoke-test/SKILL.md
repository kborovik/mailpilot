---
name: smoke-test
description: End-to-end MailPilot smoke test against real Gmail across outbound@lab5.ca, inbound@lab5.ca, demo@lab5.ca. One Phase 0 setup → 2 scenarios run sequentially without state reset. Scenario A = outbound workflow + manual operator reply. Scenario B = live KB-grounded inbound auto-reply demo at https://lab5.ca/demo/ (real Drive folder, in-scope grounded reply + out-of-scope polite decline). Outbound workflow stays active across B → verifies concurrent multi-account, multi-workflow operation. Both scenarios mandatory. Use whenever user says "smoke test", "run end-to-end", "verify the system works", or after non-trivial changes to sync, routing, agent execution, KB grounding, or Pub/Sub code -- even without explicit invocation.
---

# Smoke Test

## What this tests

Two scenarios share one Phase 0 setup and one `mailpilot run` loop. Outbound workflow from A stays active through B → exercises real concurrent multi-workflow, multi-account operation. Agent-to-agent reply loop is prevented by two structural properties, not by isolation:

- Distinct subjects per scenario, so each Gmail thread is owned by exactly one workflow type. A's thread → `thread_match` → outbound workflow. B's fresh threads → classification → demo's inbound workflow.
- Enrollments terminate with `record_enrollment_outcome`, so the agent stops replying once a scenario reaches its outcome.

| Scenario | Active workflows                       | Trigger                                  | Verifies                                                                                                                       |
| -------- | -------------------------------------- | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| A        | Outbound only                          | `mailpilot enrollment run`               | Outbound agent send → Gmail delivery → manual operator reply → thread_match routing → agent processes reply                   |
| B        | Outbound (terminal) + Demo (active)    | `mailpilot email send` (operator-driven) | The lab5.ca/demo promise -- KB-grounded reply within 60s for in-scope question, polite decline for out-of-scope                |

Both scenarios are **mandatory**. `make clean` runs **once**, at the very start. Scenario B IS the lab5.ca/demo system under test -- it must run.

## Conventions

- **Unique subject per scenario, freshly randomized.** Format: `[ST-<HHMMSS>] <topic>`. Generate the topic via Bash on every run -- do not invent it in your head, do not reuse topics from prior runs, do not copy any topic shown in this skill. LLMs anchor on examples and have been observed reusing the same topic across runs, which collides traces and defeats the unique-subject point. Generator:

  ```bash
  TOPIC_A=$(sort -R /usr/share/dict/words 2>/dev/null \
    | grep -E '^[A-Za-z]{4,9}$' | head -2 | tr '\n' ' ' | sed 's/ *$//')
  SUBJECT_A="[ST-$(date +%H%M%S)] ${TOPIC_A}"
  ```

  Scenario B sends two trigger emails. Generate `SUBJECT_B1` (in-scope) and `SUBJECT_B2` (out-of-scope) independently the same way. Verify all three (`SUBJECT_A`, `SUBJECT_B1`, `SUBJECT_B2`) are distinct before continuing. If `/usr/share/dict/words` is unavailable, fall back to `head -c 12 /dev/urandom | base32 | tr -d '=' | head -c 10`.

- **Test start ISO timestamp.** Capture before each scenario; reuse for `--since` filters and Logfire windows.
- **Polling.** When waiting for sync, routing, or agent results: poll up to 12 attempts, 5s apart (~60s total). Do not call `mailpilot account sync` directly -- the background `mailpilot run` loop owns sync.
- **CLI parsing.** All commands use `uv run mailpilot`. Parse JSON output of every command, extract IDs for the next step. Do not capture into a shell variable and re-emit with `echo "$VAR" | python3 -c ...` -- zsh's built-in `echo` interprets backslash escapes in the JSON (e.g. converts the literal two-char `\n` inside `body_text` into a real newline) and the resulting stream is no longer valid JSON. Either pipe `mailpilot ... | python3 -c ...` directly, or use `printf '%s' "$VAR"`.
- **ASCII only.** No emojis. Use `->`, `--`, plain pipes.

## Prerequisites

- PostgreSQL running locally.
- `mailpilot config get google_application_credentials` returns a valid path.
- `mailpilot config get anthropic_api_key` returns a non-empty value.
- Network access to Gmail API and Anthropic API.

---

## Phase 0: Shared setup

Run **once** at the start. Both scenarios reuse the same accounts, contacts, and company; do not repeat Phase 0 between scenarios.

1. `make clean` -- drops and re-applies the schema; mailbox contents on Gmail are untouched. Do not run again until the next smoke test.
2. Create accounts:
   ```
   mailpilot account create --email outbound@lab5.ca --display-name "Outbound Smoke"
   mailpilot account create --email inbound@lab5.ca  --display-name "Inbound Smoke"
   mailpilot account create --email demo@lab5.ca     --display-name "Demo (lab5.ca/demo)"
   ```
   Save `OUTBOUND_ACCOUNT_ID`, `INBOUND_ACCOUNT_ID`, `DEMO_ACCOUNT_ID`. All three must be delegated through the service account in `google_application_credentials`. If `demo@lab5.ca` cannot be created (auth/delegation failure), stop -- Scenario B cannot run.
3. Create company:
   ```
   mailpilot company create --domain lab5.ca --name Lab5
   ```
   Save `COMPANY_ID`.
4. Create contacts (stable IDs for resolution and enrollment):
   ```
   mailpilot contact create --email inbound@lab5.ca  --first-name Inbound  --last-name Smoke --company-id <COMPANY_ID>
   mailpilot contact create --email outbound@lab5.ca --first-name Outbound --last-name Smoke --company-id <COMPANY_ID>
   mailpilot contact create --email demo@lab5.ca     --first-name Demo     --last-name Lab5  --company-id <COMPANY_ID>
   ```
   Save `INBOUND_CONTACT_ID` (recipient of A's outbound mail), `OUTBOUND_CONTACT_ID` (sender as seen by the demo mailbox in B), `DEMO_CONTACT_ID` (kept for completeness; not enrolled in any workflow).

### Gate 0

- `mailpilot account list` returns **3** accounts (outbound, inbound, demo).
- `mailpilot contact list` returns **3** contacts.
- `mailpilot company list` returns 1 company.
- `mailpilot workflow list` returns **0** workflows. Workflows are created per-scenario.

**KB visibility gate (Scenario B prerequisite).** The demo KB lives in the `MailPilot` Shared Drive (ID `0AJIvyECg210LUk9PVA`), folder `MailPilot Demo` (ID `1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat`). `demo@lab5.ca` is a Reader on the Shared Drive; that membership -- not per-file ACL -- is what makes the files visible to the impersonated user. Verify before scenarios start, impersonating the actual subject the agent will use:

```
uv run python -c "
from mailpilot.drive import DriveClient
files = DriveClient('demo@lab5.ca').list_markdown('1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat')
print(len(files), [f['name'] for f in files])
"
```

Expect exactly 3 markdown files (`pure-aqua-commercial-ro-systems.md`, `pure-aqua-industrial-water-softener.md`, `watts-uv-com-disinfection.md`). If fewer, B will produce false declines -- stop and fix the Drive ACL before continuing. `anyoneWithLink:reader` alone does **not** make files appear here -- it only governs who can open the URL once it's pasted into the reply.

**On failure:** Stop. Report which entity failed and the error JSON.

---

## Scenario A: Outbound workflow

**Hypothesis:** The outbound workflow composes and sends an email; when the operator (Claude Code) replies manually, the outbound agent picks the reply up via `thread_match`, processes it, and reaches a terminal enrollment state without further auto-replies.

Capture `TEST_START_A` (ISO) and `SUBJECT_A` (`[ST-<HHMMSS>] <topic>`) before A1.

### A1. Create the outbound workflow

```
mailpilot workflow create \
  --name "Outbound Smoke A" \
  --type outbound \
  --account-id <OUTBOUND_ACCOUNT_ID> \
  --objective "Send a single email about <TOPIC_A> and mark the enrollment completed or failed based on the reply" \
  --instructions "You are a sales rep for Lab5. Send ONE email to the contact about <TOPIC_A>. Subject MUST be exactly '<SUBJECT_A>'. Body MUST use Markdown (greeting, 2-3 sentence paragraph, a 3-row 2-column table). When you receive a reply, do not send another email -- read the reply and call record_enrollment_outcome with status='completed' if the reply expresses interest or status='failed' if it declines, then stop. Do not call disable_contact -- this is per-workflow outcome tracking, not a global contact block. Do not create follow-up tasks."
```

Activate if create did not auto-activate:

```
mailpilot workflow start <OUTBOUND_WORKFLOW_ID>
```

Save `OUTBOUND_WORKFLOW_ID`.

### A2. Start the sync loop

Start `mailpilot run` in the background via `Bash` with `run_in_background: true`. Capture the bash_id so you can read its output later. The loop runs **once for the whole test** -- it stays up through B and is only stopped at the very end (B9).

The loop emits curated `event=...` lifecycle lines on stderr regardless of `--debug` (`loop.tick`, `sync.account`, `route.match`, `agent.run`, `task.drain`, `error`). The `Bash` background capture merges stdout and stderr, so the captured output you read still contains them. Use `--debug` only when you also need Logfire's full span output for deep diagnosis.

```
uv run mailpilot --debug run
```

Wait ~3s, read the captured stdout, confirm:

- `Sync loop started (pid <pid>)` printed.
- `Pub/Sub subscriber started` printed (a `Warning: Pub/Sub setup failed` is acceptable -- periodic sync still works).
- At least one `event=loop.tick` line has appeared (proves the loop is ticking, not just started).

**Gate A2:** background process alive; `sync_status` row present.

### A3. Trigger the outbound agent

```
mailpilot enrollment add --workflow-id <OUTBOUND_WORKFLOW_ID> --contact-id <INBOUND_CONTACT_ID>
mailpilot enrollment run --workflow-id <OUTBOUND_WORKFLOW_ID> --contact-id <INBOUND_CONTACT_ID>
```

`mailpilot enrollment run` MUST be invoked exactly once per `(workflow_id, contact_id)`. If the outbound email is not visible in the next gate's `email list` poll, keep polling — do NOT re-invoke `enrollment run`. A second invocation against the same enrollment produces a redundant `agent.invoke` (the agent searches for the prior send and noops correctly, but burns an LLM round-trip and inflates the trace). See SPEC §V12 / §T18 / §B2.

**Gate A3:**

- `enrollment run` output: `"status": "completed"` and `"tool_calls" >= 1`.
- `mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction outbound` shows the outbound email with `subject == SUBJECT_A`.
- The email's `body_text` contains `|` (table) and either `**` or `#` (Markdown).
- `mailpilot enrollment list --workflow-id <OUTBOUND_WORKFLOW_ID>` shows enrollment status `active`. Per ADR-08 `enrollment.status` is operational only (`active` or `paused`); the agent never mutates it directly. The send-completion outcome lives in the activity timeline (verified in A8), not on the enrollment row.

Save `OUTBOUND_EMAIL_ID`.

**On failure:** Stop. `mailpilot task list --workflow-id <OUTBOUND_WORKFLOW_ID>` for task details. Common cause: missing `anthropic_api_key`.

### A4. Wait for Gmail delivery to the inbound mailbox

Poll the inbound account:

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

Claude Code sends the reply directly via CLI -- no inbound agent is involved (no inbound workflow exists yet). The reply lands in the outbound mailbox, where it is picked up by `thread_match` and handed to the outbound agent for terminal processing.

Choose reply content that gives the outbound agent a clear terminal signal so it marks the enrollment outcome and stops. Phrase the decline as "this opportunity is not a fit for our current priorities" -- not "remove us from your list". The latter steers the agent toward `disable_contact` (a global contact block) when we want `record_enrollment_outcome` (the per-workflow outcome).

```
mailpilot email reply \
  --account-id <INBOUND_ACCOUNT_ID> \
  --email-id <INBOUND_SIDE_EMAIL_ID> \
  --body "Thanks for the email. After reviewing internally we have decided this opportunity is not a fit for our current priorities. Please consider this declined."
```

**Gate A5:** Command exits 0 and returns a JSON envelope with the new email's `id`. Save `REPLY_EMAIL_ID`.

### A6. Wait for the reply to route back via thread_match

Poll the outbound account:

```
mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction inbound --since <TEST_START_A>
```

Match by `SUBJECT_A` (Gmail typically preserves the subject with a `Re:` prefix; match on the `[ST-<HHMMSS>]` portion). Fetch detail.

**Gate A6:**

- Email present in outbound account's inbound emails.
- `workflow_id == OUTBOUND_WORKFLOW_ID` (`thread_match` succeeded -- the prior outbound email in this thread is owned by this workflow).
- `is_routed == true`.

**On failure:** If the email arrived but `workflow_id` is null, `thread_match` did not connect the reply to the original send -- check that the original outbound email has `workflow_id` and `gmail_thread_id` set in the DB.

### A7. Wait for the outbound agent to process the reply

The run loop calls `create_tasks_for_routed_emails` once A6's email has `workflow_id` set, inserts a task, and the LISTEN/NOTIFY listener drains it.

Poll for task completion:

```
mailpilot task list --workflow-id <OUTBOUND_WORKFLOW_ID>
```

Wait for a task with `email_id` set to the routed reply and `status == "completed"`.

**Gate A7:**

- Task exists with `email_id == <routed reply id>` and `status == "completed"`.
- `mailpilot enrollment list --workflow-id <OUTBOUND_WORKFLOW_ID>` still shows status `active` -- by design (ADR-08, `enrollment.status` is operational only). The terminal outcome is recorded as an `enrollment_completed` or `enrollment_failed` activity row, verified in A8.
- **No additional outbound emails were sent.** Re-run `mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction outbound --since <TEST_START_A>` and confirm only the original outbound from A3 is present. If the count > 1, the agent kept replying despite the decline signal -- record as a defect.

**On failure:** Task never created → check that A6's email has `workflow_id` set and the run loop is alive. Task `failed` → `mailpilot task view <TASK_ID>` for the reason.

### A8. Verify the CRM activity timeline

Runtime paths emit `activity` rows automatically (no manual `activity create`). Read the inbound contact's timeline:

```
mailpilot activity list --contact-id <INBOUND_CONTACT_ID> --since <TEST_START_A>
```

**Gate A8 (activity wiring):** activity types follow the `enrollment_*` vocabulary in ADR-08.

- `enrollment_added` with `detail.workflow_id == OUTBOUND_WORKFLOW_ID` (emitted by `enrollment add`).
- `email_sent` with `summary == SUBJECT_A` (emitted by `email_ops.send_email` when the outbound agent sent in A3).
- `email_received` with the operator-reply subject (emitted by sync's `_store_inbound_message` when the reply landed in the outbound mailbox in A6).
- Exactly one of `enrollment_completed` or `enrollment_failed` (emitted by `agent.tools.record_enrollment_outcome` in A7); summary equals the agent's `reason`.
- No `tag_added` or `note_added` rows from this scenario (we did not run those CLI commands).

If any expected type is missing, the runtime activity wiring regressed for that path.

### Logfire review for Scenario A

Do this review now, before B, so the window cleanly bounds A's spans. Use `/logfire:debug` with project=`mailpilot` and window `[TEST_START_A, now]`. Spans to verify:

- `agent.invoke` -- count by `trigger` attribute, not by total. Per SPEC §V12 / §T18, the span carries an explicit `trigger` label set by the caller path:
  - `trigger="task"` -- expect exactly **1** (A7 reply handling, drained by background `mailpilot run`). More than 1 → agent kept replying (loop regression). This is the regression signal for Scenario A.
  - `trigger="enrollment_run"` -- expect at least **1** (A3 send via foreground `enrollment run`). Tolerated regardless of count: an operator double-fire produces extra `enrollment_run` spans that correctly noop, so they cost an LLM round-trip but do not signal regression. T19 / B2 prefer single-invocation discipline (see A3) but the trace contract here permits more.
  - `trigger="email"` / `trigger="manual"` -- not expected in Scenario A; flag if present.
- `running tool` -- A3: expect `send_email` plus optional context-gathering reads (`read_contact`, `read_company`); `record_enrollment_outcome` is **not** expected here (it fires after a reply, not on initial send). A7: expect `record_enrollment_outcome` and **no** `send_email` or `reply_email`.
- `routing.route_email` -- the reply (A6) → `route_method=thread_match` and `workflow_id == OUTBOUND_WORKFLOW_ID`. The inbound-side email from A4 → `route_method=skipped_no_workflows` (no inbound workflow at the time).
- `gmail.send_message` -- 2 calls total (A3 by agent + A5 by operator).
- Any `is_exception=true` or `level=warn` spans -- record them.

---

## Transition to Scenario B

Do not stop the sync loop. Do not run `make clean`. Do not recreate accounts or contacts. The outbound workflow stays active with its enrollment in a terminal state, and the run loop keeps syncing all three accounts. Scenario B layers a KB-grounded inbound workflow on `demo@lab5.ca` on top of this live state -- the explicit multi-workflow / multi-account checkpoint of the test.

---

## Scenario B: KB-grounded demo (lab5.ca/demo)

**Hypothesis:** The lab5.ca/demo system delivers on its public promise -- "a professional response grounded in real data" within ~60 seconds for in-scope questions, and a polite explanatory reply (no fabricated specs) for questions outside the KB. With the outbound workflow from A still active, the demo workflow on `demo@lab5.ca` correctly classifies an operator-sent question on a fresh thread, the agent grounds its answer in the real Drive KB via `list_drive_markdown` + `read_drive_markdown`, and the reply round-trips to the outbound mailbox.

**Real KB used.** This scenario uses the production KB folder, not a fixture:

- Shared Drive: `MailPilot` (ID `0AJIvyECg210LUk9PVA`). Members: `kb@lab5.ca` Manager, `demo@lab5.ca` Reader.
- Folder name: `MailPilot Demo`
- Folder ID: `1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat`
- Markdown files (as of writing -- the Phase 0 KB visibility gate also enumerates them; re-confirm via that gate before each run):
  - `pure-aqua-commercial-ro-systems.md` -- TW-series RO systems (e.g., TW-18.0K-1240).
  - `pure-aqua-industrial-water-softener.md` -- SF-series softeners (e.g., SF-100S).
  - `watts-uv-com-disinfection.md` -- UV-COM disinfection units.

  PDFs sit alongside the `.md` files; `list_drive_markdown`'s `mimeType='text/markdown'` filter must skip them. If it does not, that is a defect.

- Access model: because the KB lives in a Shared Drive, listing depends on the impersonated user being a Shared Drive member, not on per-file ACL. `anyoneWithLink:reader` is set on every file so the `web_view_link` returned by `read_drive_markdown` opens for strangers reading the agent's reply. If `list_drive_markdown` returns an empty list or `not_found`, the failure mode is almost always Shared Drive membership of `demo@lab5.ca`, not file-level sharing -- fix that first, do not patch around it.

Capture `TEST_START_B` (ISO, must be later than A's last activity) and two distinct subjects -- `SUBJECT_B1` (in-scope) and `SUBJECT_B2` (out-of-scope) -- per the Conventions section. Both must differ from `SUBJECT_A`.

### B1. Create the demo inbound workflow

Operator-style instructions citing the real folder ID. The agent's behaviour comes from this prompt -- changing the wording changes what we test.

```
mailpilot workflow create \
  --name "Demo (lab5.ca/demo)" \
  --type inbound \
  --account-id <DEMO_ACCOUNT_ID> \
  --objective "Answer water-treatment product questions grounded in the MailPilot Demo Drive folder; politely decline questions about products not in the KB." \
  --instructions "You are the lab5.ca/demo agent. The Markdown product knowledge base lives in Google Drive folder 1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat. For every reply: call list_drive_markdown with that folder ID, pick the most relevant file by name, call read_drive_markdown on it, then compose the reply grounded in that file's content. Cite the source file name in the body. If no listed file is relevant to the question (e.g., the asker is asking about Pentair, Evoqua, or Grundfos products that are not in the folder), reply with a short polite decline that explains the KB does not cover that product and do NOT fabricate specifications. Body MUST use plain Markdown. Subject MUST preserve the incoming thread subject. After replying, call record_enrollment_outcome with outcome='completed'. Do not create follow-up tasks."
```

Activate and pre-enroll the sender:

```
mailpilot workflow start <DEMO_WORKFLOW_ID>
mailpilot enrollment add --workflow-id <DEMO_WORKFLOW_ID> --contact-id <OUTBOUND_CONTACT_ID>
```

**Gate B1 (multi-workflow checkpoint):** `mailpilot workflow list` returns **2** workflows -- the outbound from A (terminal but still active) and the demo workflow just created -- both `active`.

### B2. Confirm the sync loop is still alive

The `mailpilot run` process started in A2 has been syncing all three accounts continuously. Read its captured stdout, confirm no fatal errors since the A-window Logfire review. If the process died, restart it the same way as A2 and note the restart in the report.

### B3. Send the in-scope question

Pick one in-scope question from the lab5.ca/demo page. Examples (rotate freely; do not memorize a single phrasing):

- "What are the dimensions and weight of the TW-18.0K-1240 reverse osmosis system?"
- "Which SF-100S softener would you recommend for a hospital needing at least 200 GPM continuous flow?"
- "Which UV-COM model supports the highest flow rate, and what certifications does it have?"

```
mailpilot email send \
  --account-id <OUTBOUND_ACCOUNT_ID> \
  --to demo@lab5.ca \
  --subject "<SUBJECT_B1>" \
  --body "<your in-scope question>"
```

Save `TRIGGER_EMAIL_ID_B1` and `TRIGGER_THREAD_ID_B1`. Capture wall-clock send time as `T_SEND_B1`.

**Gate B3:** Command exits 0, returns a JSON envelope with the new email's `id`.

### B4. Wait for the demo agent to reply (60-second SLA)

Critical gate. The lab5.ca/demo page promises delivery within ~60 seconds. Poll the outbound mailbox:

```
mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction inbound --since <TEST_START_B>
```

Match by `SUBJECT_B1` (likely with `Re:` prefix). Record the wall-clock time the reply first appears as `T_REPLY_B1`, compute `LATENCY_B1 = T_REPLY_B1 - T_SEND_B1`.

**Gate B4 (the demo promise):**

- Reply present, threaded under `SUBJECT_B1`.
- `LATENCY_B1 <= 60s`. **If the reply takes longer, that is a regression of the lab5.ca/demo promise -- record as a Critical defect.** (Polling cadence is 5s, so granularity is coarse; if the first observation lands at 65s and it was the first reply on the thread, treat the run as borderline and re-test.)
- Reply on the demo side (`mailpilot email list --account-id <DEMO_ACCOUNT_ID> --direction outbound --since <TEST_START_B>`) → `is_routed == true`, `workflow_id == DEMO_WORKFLOW_ID`, `route_method == classified`. The classifier ran -- not `thread_match`, since this is a fresh thread.
- Reply body **grounded in the KB**: mentions the model number from the question verbatim (e.g., `TW-18.0K-1240`, `SF-100S`, `UV-COM`) and includes at least one numeric fact (regex `\d`) consistent with a spec answer. A reply without a model number or numeric fact is a grounding regression.
- Reply body cites the source file name (or its product family name) -- the workflow instructions require this. Missing citation is a prompt-fidelity regression.

### B5. Verify the agent actually used the Drive tools

Run a Logfire query for the `agent.invoke` span produced by B4's reply. Within that invocation, the `running tool` child spans must include, in order:

1. `list_drive_markdown` (with `folder_id=1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat`)
2. `read_drive_markdown` (with a `file_id` returned by step 1)
3. `reply_email`
4. `record_enrollment_outcome` (outcome=`completed`)

**Gate B5:**

- All four tool calls present in this order.
- `list_drive_markdown` returned a non-error list (no `error` key in the tool return).
- `read_drive_markdown` returned a dict with non-empty `content`.
- An agent that skips `list_drive_markdown` or invents a `file_id` without listing first is a prompt-fidelity regression -- record as a defect even if the reply happens to be plausible.

### B6. Send the out-of-scope question

Same demo workflow, fresh subject:

```
mailpilot email send \
  --account-id <OUTBOUND_ACCOUNT_ID> \
  --to demo@lab5.ca \
  --subject "<SUBJECT_B2>" \
  --body "Which Pentair Evoqua reverse osmosis system would you recommend for a 500 GPM industrial laundry?"
```

(Pentair, Evoqua, Grundfos are explicitly named on lab5.ca/demo as out-of-scope vendors. Pick whichever; vary across runs.)

Save `TRIGGER_EMAIL_ID_B2`, capture `T_SEND_B2`, poll the outbound mailbox for `SUBJECT_B2` the same way as B4. Capture `T_REPLY_B2`.

**Gate B6 (polite decline, no fabrication):**

- Reply present within 60s.
- Reply body does **not** contain any Pentair, Evoqua, or Grundfos model number or specification -- a regex over the body must not match `Pentair|Evoqua|Grundfos` followed by what looks like a spec figure. The agent must not fabricate.
- Reply body reads as a polite decline -- acknowledges the asker, states the KB does not cover that product, and (per the workflow instructions) does not invent.
- The `agent.invoke` for B6 still shows `list_drive_markdown` followed by `reply_email` (the decline path satisfies the "must call >=1 tool per run" invariant via the listing). Missing `list_drive_markdown` here means the agent declined without consulting the KB -- it might have got lucky on this question, but the prompt contract was not honoured. Record as a defect.

### B7. Verify the CRM activity timeline

```
mailpilot activity list --contact-id <OUTBOUND_CONTACT_ID> --since <TEST_START_B>
```

**Gate B7 (activity wiring):** activity types follow the `enrollment_*` vocabulary in ADR-08.

- `enrollment_added` with `detail.workflow_id == DEMO_WORKFLOW_ID` (from B1).
- 2 `email_received` activities -- the demo mailbox received the trigger emails for B1 and B2.
- 2 `email_sent` activities from the agent replies (subjects begin with `Re:`).
- 2 `enrollment_completed` activities (one per question, both emitted by `record_enrollment_outcome`).

### B8. Concurrent-workflow quiet check

The Scenario A outbound workflow is still active throughout B. It must not have reacted to B's traffic.

The outbound *account* legitimately sends mail in B (the operator's two trigger emails in B3 and B6 leave from `outbound@`); those are not the signal we care about. The signal is whether the outbound *workflow* generated any agent-driven sends. Filter by `workflow_id`:

```
mailpilot email list \
  --account-id <OUTBOUND_ACCOUNT_ID> \
  --direction outbound \
  --workflow-id <OUTBOUND_WORKFLOW_ID> \
  --since <TEST_START_B>
```

**Gate B8:** Zero rows. Any non-zero count means the still-active outbound workflow reacted to B's traffic -- record as a defect.

Sanity check the operator triggers are still there (B3 and B6 are agent-driven from B's perspective but operator-driven from A's perspective, so they carry `workflow_id == null` on the outbound mailbox):

```
mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction outbound --since <TEST_START_B>
```

Expect exactly 2 rows (the B3 and B6 triggers), each with `workflow_id == null`. Any deviation is a separate signal -- either an unexpected outbound send (record as a defect) or a missing trigger (re-run B3/B6).

### B9. Stop the sync loop

Send SIGTERM to the background `mailpilot run` (e.g. `kill <pid>`). Wait for `Sync loop stopped` in the captured output. Confirm the `sync_status` table is empty. If the process does not exit within 10s, send SIGKILL and record this in the report.

### Logfire review for Scenario B

Window `[TEST_START_B, now]`. Spans to verify:

- `agent.invoke` -- exactly **2** invocations (B4 and B6). More than 2 → demo agent re-fired or outbound workflow reacted to B's traffic.
- `routing.route_email` -- both demo-side trigger emails → `route_method=classified`. Outbound-side replies → `route_method=skipped_no_inbound_workflows`.
- `classify_email` -- 2 invocations. Both `result` values match `DEMO_WORKFLOW_ID`.
- `running tool` per invocation -- B4 (in-scope): `list_drive_markdown` + `read_drive_markdown` + `reply_email` + `record_enrollment_outcome`. B6 (out-of-scope decline): `list_drive_markdown` + `reply_email` + `record_enrollment_outcome` (`read_drive_markdown` is not required here since no listed file is relevant). Either pattern is acceptable, but `list_drive_markdown` is mandatory in both.
- Any `is_exception=true` or `level=warn` spans -- record. Drive 4xx/5xx surfacing as `drive_unavailable` from the tool is acceptable in the agent's tool-return ledger but should not be `is_exception=true` on the span.

---

## Phase 5: Final report

Produce a report covering both scenarios. Both are mandatory; a missing scenario is a test failure, not a permitted skip.

### Part A: Phase results

```
Smoke Test Results
==================

Phase 0 (one-time setup) ..... PASS  (3 accounts, 3 contacts, 1 company)

Scenario A: Outbound workflow (sole workflow active)
  A1 Create workflow ......... PASS
  A2 Start sync loop ......... PASS
  A3 Outbound agent send ..... PASS
  A4 Gmail delivery (in) ..... PASS
  A5 Operator reply .......... PASS
  A6 thread_match routing .... PASS
  A7 Agent processes reply ... PASS
  A8 Activity timeline ....... PASS

Scenario B: KB-grounded demo (lab5.ca/demo, outbound workflow still active)
  B1 Create demo workflow .... PASS  (workflow list shows 2 active)
  B2 Sync loop still alive ... PASS
  B3 In-scope trigger send ... PASS
  B4 60s grounded reply ...... PASS  (LATENCY_B1 = <Ns>; cited model: <e.g., TW-18.0K-1240>)
  B5 Drive tools used ........ PASS  (list_drive_markdown -> read_drive_markdown -> reply_email -> record_enrollment_outcome)
  B6 Out-of-scope decline .... PASS  (LATENCY_B2 = <Ns>; no fabricated specs)
  B7 Activity timeline ....... PASS
  B8 Outbound stayed quiet ... PASS  (0 new outbound sends during B)
  B9 Stop sync loop .......... PASS

Entity IDs (shared by both scenarios):
  Outbound account: <id>   Inbound account: <id>   Demo account: <id>   Company: <id>
  Outbound contact: <id>   Inbound contact: <id>   Demo contact: <id>
  Outbound workflow: <id>  Demo workflow: <id>
  KB folder ID: 1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat

Email summary (Scenario A):
  Outbound send:    <id>  subject: <SUBJECT_A>
  Inbound delivery: <id>  skipped_no_workflows (expected -- no inbound workflow yet)
  Operator reply:   <id>  email_id: <INBOUND_SIDE_EMAIL_ID>
  Reply round-trip: <id>  workflow_id: <OUTBOUND_WORKFLOW_ID> via thread_match

Email summary (Scenario B):
  In-scope trigger:    <id>  subject: <SUBJECT_B1>
  In-scope delivery:   <id>  workflow_id: <DEMO_WORKFLOW_ID> via classified
  In-scope reply:      <id>  latency: <Ns>  cited file: <name>  body grounded: yes
  Out-of-scope trigger:<id>  subject: <SUBJECT_B2>
  Out-of-scope reply:  <id>  latency: <Ns>  fabricated specs: NO  declined politely: yes

Loop sentinels:
  Scenario A: agent.invoke count == 2 (expected 2)
  Scenario B: agent.invoke count == 2 (expected 2)
  Outbound workflow during B: 0 new outbound sends (expected 0)
  Drive tool calls in B: list_drive_markdown >= 2, read_drive_markdown >= 1
```

If a phase failed, stop Part A at the failing phase with the failure JSON and any captured stdout from the background `mailpilot run`.

### Part B: Cross-cutting Logfire pass

Use `/logfire:debug` with the test window. Run once across both scenarios:

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

1. **CLI usability** -- commands needing awkward sequencing or workarounds; missing fields in JSON output; error messages that did not point at the cause.
2. **Logfire observability** -- missing spans, missing attributes on existing spans, noisy span families (quantify with the volume query), broken parent-child causality, agent token/cost visibility.
3. **Agent behavior** -- did agents follow instructions (subject, brevity, no extra tool calls)? Was `agent_reasoning` useful? Did `record_enrollment_outcome` get called when expected? In A, did the agent hold the line on "do not reply again"?
4. **Demo promise (lab5.ca/demo)** -- did the in-scope reply in B4 land within 60s and cite a model number from the KB? Did B5 confirm `list_drive_markdown` + `read_drive_markdown` ran in order? Did the out-of-scope reply in B6 decline without fabricating any Pentair / Evoqua / Grundfos specs? Any deviation is a regression of a public, customer-facing promise -- mark Critical.
5. **Concurrent workflow safety** -- with both workflows active during B, did the outbound workflow stay quiet (zero new sends, no `agent.invoke` outside A's window)? Did the demo workflow correctly leave A's lingering thread alone? Excess `agent.invoke` spans here are the high-priority signal -- they would indicate two simultaneously active workflows can interfere with each other.
6. **Drive integration** -- did the `mimeType='text/markdown'` filter correctly skip the PDFs in the KB folder? Any Drive errors observed (`drive_unavailable`, `not_found`)? Are `list_drive_markdown` / `read_drive_markdown` tool spans surfacing useful attributes (folder_id, file_id, file count)?
7. **Other deficiencies** -- timing, race conditions, data integrity, performance.

---

## Timing

Expected total: ~7 minutes. Phase 0 once, run loop once, no reset between scenarios.

| Phase / scenario               | Duration |
| ------------------------------ | -------- |
| Phase 0 (once, 3 accounts)     | ~15s     |
| A1 / B1 workflow setup         | ~5s      |
| A2 start run loop              | ~5s      |
| A3 outbound agent              | ~10s     |
| A4 sync + route                | ~10-60s  |
| A5 / B3 / B6 operator send     | ~3s each |
| A6 reply round-trip            | ~10-60s  |
| A7 task drain                  | ~10-60s  |
| B4 in-scope reply (60s SLA)    | ~10-60s  |
| B6 out-of-scope reply (60s SLA)| ~10-60s  |
| A8 / B7 activity check         | ~3s      |
| B9 stop run loop               | ~3s      |
| Report                         | ~10s     |
