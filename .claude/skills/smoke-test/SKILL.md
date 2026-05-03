---
name: smoke-test
description: End-to-end MailPilot smoke test against real Gmail across outbound@lab5.ca and inbound@lab5.ca. One Phase 0 setup → 2 scenarios run sequentially without state reset. Scenario A = outbound workflow + manual operator reply. Scenario B = live KB-grounded inbound auto-reply demo at https://lab5.ca/demo/ (real Drive folder, in-scope grounded reply + out-of-scope polite decline). Outbound workflow stays active across B → verifies concurrent multi-account, multi-workflow operation. Both scenarios mandatory. Use whenever user says "smoke test", "run end-to-end", "verify the system works", or after non-trivial changes to sync, routing, agent execution, KB grounding, or Pub/Sub code -- even without explicit invocation.
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
- **Envelope shape (SPEC §V13).** `<entity> view`/`create`/`update` returns `{"<singular>": {...}, "ok": true}`; `<entity> list`/`search` returns `{"<plural>": [...], "ok": true}`. Always extract through the wrap: `json.load(sys.stdin)["email"]["workflow_id"]`, not `json.load(sys.stdin)["workflow_id"]`. Operational commands (`enrollment run`, `account sync`, `tag remove`, `enrollment remove`, `*_export`/`*_import`, `config get/set`, `status`) keep their bespoke shapes.
- **ASCII only.** No emojis. Use `->`, `--`, plain pipes.

## Prerequisites

- PostgreSQL running locally.
- `mailpilot config get google_application_credentials` returns a valid path.
- `mailpilot config get anthropic_api_key` returns a non-empty value.
- Network access to Gmail API and Anthropic API.

## Scripts

Located at `.claude/skills/smoke-test/scripts/`. All QA-only -- KB-content maintenance (PDF conversion, verification, Drive push) lives outside the smoke test.

**Runtime (used during the test, in B3/B4/B6):**

- `qa.py pick [--type inscope|outscope] [--id ID]` -- emit one Q/A pair as JSON. Random unless `--id` given. Default type is `inscope`. The pair includes the question to send and the verifiable evidence the agent's reply must contain.
- `qa.py check --id ID --reply-text "<body>" | --reply-file PATH` -- validate the agent's reply against that pair. Exit 0 = pass, 1 = fail. JSON on stdout lists missing tokens / fabrications / decline-signal absence.
- `qa_pairs.json` -- 29 in-scope + 5 out-of-scope pairs. In-scope pairs name a model number + 1-3 numeric specs that MUST appear in the agent's reply, plus the source `.md` file the agent must cite. Out-of-scope pairs name (vendor, spec-shape) regex pairs the reply MUST NOT match, plus decline-signal phrases the reply MUST contain.

**Maintenance (run only after the demo Drive folder content changes):**

- `generate_qa_pairs.py` -- regenerate `qa_pairs.json`. Reads each `.md` from the live Drive folder via the impersonated DriveClient, asks Haiku 4.5 to draft one in-scope question per file with verifiable expected_tokens. Out-of-scope pairs are hand-curated inside the script; edit them there to rotate vendors.

---

## Phase 0: Shared setup

Run **once** at the start. Both scenarios reuse the same accounts, contacts, and company; do not repeat Phase 0 between scenarios.

1. `make clean` -- drops and re-applies the schema; mailbox contents on Gmail are untouched. Do not run again until the next smoke test.
2. Create accounts:
   ```
   mailpilot account create --email outbound@lab5.ca --display-name "Outbound Smoke"
   mailpilot account create --email inbound@lab5.ca  --display-name "Inbound Smoke (also hosts Demo workflow in B)"
   ```
   Save `OUTBOUND_ACCOUNT_ID`, `INBOUND_ACCOUNT_ID`. Both must be delegated through the service account in `google_application_credentials`. If `inbound@lab5.ca` cannot be created (auth/delegation failure), stop -- Scenario B cannot run.
3. Create company:
   ```
   mailpilot company create --domain lab5.ca --name Lab5
   ```
   Save `COMPANY_ID`.
4. Create contacts (stable IDs for resolution and enrollment):
   ```
   mailpilot contact create --email inbound@lab5.ca  --first-name Inbound  --last-name Smoke --company-id <COMPANY_ID>
   mailpilot contact create --email outbound@lab5.ca --first-name Outbound --last-name Smoke --company-id <COMPANY_ID>
   ```
   Save `INBOUND_CONTACT_ID` (recipient of A's outbound mail; also the demo-workflow mailbox's contact in B), `OUTBOUND_CONTACT_ID` (sender as seen by the demo mailbox in B).

### Gate 0

- `mailpilot account list` returns **2** accounts (outbound, inbound).
- `mailpilot contact list` returns **2** contacts.
- `mailpilot company list` returns 1 company.
- `mailpilot workflow list` returns **0** workflows. Workflows are created per-scenario.

**KB visibility gate (Scenario B prerequisite).** The demo KB lives in the `MailPilot` Shared Drive (ID `0AJIvyECg210LUk9PVA`), folder `MailPilot Demo` (ID `1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat`). `inbound@lab5.ca` is a Reader on the Shared Drive; that membership -- not per-file ACL -- is what makes the files visible to the impersonated user. Verify before scenarios start, impersonating the actual subject the agent will use:

```
uv run python -c "
from mailpilot.drive import DriveClient
files = DriveClient('inbound@lab5.ca').list_markdown('1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat')
print(len(files), [f['name'] for f in files])
"
```

Expect **at least 10 markdown files** (the original three -- `pure-aqua-commercial-ro-systems.md`, `pure-aqua-industrial-water-softener.md`, `watts-uv-com-disinfection.md` -- plus distractors covering adjacent water-treatment products that are still in-scope but irrelevant to the B1 question). The size matters: with only 3 docs the agent can succeed by listing every file, which masks a regression where it forgets to use `search_drive_markdown` as the targeted entry point. If fewer than 10, B's `search` vs `list` discriminator (gate B5) is meaningless -- stop and add more KB docs before continuing.

If the count is zero or `not_found`, the failure is Drive ACL, not KB content -- `inbound@lab5.ca`'s Shared Drive Reader membership is what makes files visible to the impersonated user. `anyoneWithLink:reader` alone does **not** make files appear here -- it only governs who can open the URL once it's pasted into the reply.

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

Do not stop the sync loop. Do not run `make clean`. Do not recreate accounts or contacts. The outbound workflow stays active with its enrollment in a terminal state, and the run loop keeps syncing both accounts. Scenario B layers a KB-grounded inbound workflow on `inbound@lab5.ca` on top of this live state -- the explicit multi-workflow / multi-account checkpoint of the test.

---

## Scenario B: KB-grounded demo (lab5.ca/demo)

**Hypothesis:** The lab5.ca/demo system delivers on its public promise -- "a professional response grounded in real data" within ~60 seconds for in-scope questions, and a polite explanatory reply (no fabricated specs) for questions outside the KB. With the outbound workflow from A still active, the demo workflow on `inbound@lab5.ca` correctly classifies an operator-sent question on a fresh thread, the agent grounds its answer in the real Drive KB via `list_drive_markdown` + `read_drive_markdown`, and the reply round-trips to the outbound mailbox.

**Real KB used.** This scenario uses the production KB folder, not a fixture:

- Shared Drive: `MailPilot` (ID `0AJIvyECg210LUk9PVA`). Members: `kb@lab5.ca` Manager, `inbound@lab5.ca` Reader.
- Folder name: `MailPilot Demo`
- Folder ID: `1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat`
- Markdown files (as of writing -- the Phase 0 KB visibility gate enumerates them and asserts the ≥10 floor; re-confirm via that gate before each run). Three answer-bearing seeds:
  - `pure-aqua-commercial-ro-systems.md` -- TW-series RO systems (e.g., TW-18.0K-1240).
  - `pure-aqua-industrial-water-softener.md` -- SF-series softeners (e.g., SF-100S).
  - `watts-uv-com-disinfection.md` -- UV-COM disinfection units.

  Plus ≥7 distractors on adjacent in-scope water-treatment topics so the search-vs-list discriminator (gate B5) is meaningful. The seeds are what the in-scope B1 question targets; the agent must locate one of them via `search_drive_markdown` rather than by listing the whole folder.

  PDFs sit alongside the `.md` files; the `mimeType='text/markdown'` filter on both `list_drive_markdown` and `search_drive_markdown` must skip them. If it does not, that is a defect.

- Access model: because the KB lives in a Shared Drive, listing depends on the impersonated user being a Shared Drive member, not on per-file ACL. `anyoneWithLink:reader` is set on every file so the `web_view_link` returned by `read_drive_markdown` opens for strangers reading the agent's reply. If `list_drive_markdown` returns an empty list or `not_found`, the failure mode is almost always Shared Drive membership of `inbound@lab5.ca`, not file-level sharing -- fix that first, do not patch around it.

Capture `TEST_START_B` (ISO, must be later than A's last activity) and two distinct subjects -- `SUBJECT_B1` (in-scope) and `SUBJECT_B2` (out-of-scope) -- per the Conventions section. Both must differ from `SUBJECT_A`.

### B1. Create the demo inbound workflow

Operator-style instructions citing the real folder ID. The agent's behaviour comes from this prompt -- changing the wording changes what we test.

```
mailpilot workflow create \
  --name "Demo (lab5.ca/demo)" \
  --type inbound \
  --account-id <INBOUND_ACCOUNT_ID> \
  --objective "Answer water-treatment product questions grounded in the MailPilot Demo Drive folder; politely decline questions about products not in the KB." \
  --instructions "You are the lab5.ca/demo agent. The Markdown product knowledge base lives in Google Drive folder 1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat. For every reply: call search_drive_markdown with that folder ID and a query derived from the incoming question (key product terms, model numbers, application). Pick the top relevant hit and call read_drive_markdown on it before composing the reply grounded in that file's content. Cite the source file name in the body. If search_drive_markdown returns no hits for the question's terms (e.g., the asker is asking about Pentair, Evoqua, or Grundfos products that are not in the folder), reply with a short polite decline that explains the KB does not cover that product and do NOT fabricate specifications. Body MUST use plain Markdown. When the reply contains product specifications (model numbers, flow rates, dimensions, capacities), present them as a GitHub-flavored Markdown pipe table with a header row -- e.g., `| Specification | Value |` followed by `|---|---|` and one row per spec. Do NOT use asterisks, colons, or single-spaced lines as a substitute for a table. Subject MUST preserve the incoming thread subject. After replying, call record_enrollment_outcome with outcome='completed'. Do not create follow-up tasks."
```

Activate and pre-enroll the sender:

```
mailpilot workflow start <DEMO_WORKFLOW_ID>
mailpilot enrollment add --workflow-id <DEMO_WORKFLOW_ID> --contact-id <OUTBOUND_CONTACT_ID>
```

**Gate B1 (multi-workflow checkpoint):** `mailpilot workflow list` returns **2** workflows -- the outbound from A (terminal but still active) and the demo workflow just created -- both `active`.

### B2. Confirm the sync loop is still alive

The `mailpilot run` process started in A2 has been syncing both accounts continuously. Read its captured stdout, confirm no fatal errors since the A-window Logfire review. If the process died, restart it the same way as A2 and note the restart in the report.

### B3. Send the in-scope question

Pick a random in-scope Q/A pair from the manifest, capture both its id (for the B4 verifier) and its question (for the email body):

```
QA_B1=$(python3 .claude/skills/smoke-test/scripts/qa.py pick --type inscope)
QA_ID_B1=$(printf '%s' "$QA_B1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
QUESTION_B1=$(printf '%s' "$QA_B1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["question"])')
SOURCE_FILE_B1=$(printf '%s' "$QA_B1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["source_file"])')
```

Send the question:

```
mailpilot email send \
  --account-id <OUTBOUND_ACCOUNT_ID> \
  --to inbound@lab5.ca \
  --subject "<SUBJECT_B1>" \
  --body "<QUESTION_B1>"
```

Save `TRIGGER_EMAIL_ID_B1`, `TRIGGER_THREAD_ID_B1`, capture wall-clock send time as `T_SEND_B1`. Carry `QA_ID_B1` and `SOURCE_FILE_B1` forward to B4.

**Gate B3:** Command exits 0, returns a JSON envelope with the new email's `id`. `QA_ID_B1` matches `qa-in-NNN`. (`qa.py pick` is deterministic given `--id` -- if a run needs to repro a failing question, pin the id from the prior run's report.)

### B4. Wait for the demo agent to reply (60-second SLA)

Critical gate. The lab5.ca/demo page promises delivery within ~60 seconds. Poll the outbound mailbox:

```
mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction inbound --since <TEST_START_B>
```

Match by `SUBJECT_B1` (likely with `Re:` prefix). Record the wall-clock time the reply first appears as `T_REPLY_B1`, compute `LATENCY_B1 = T_REPLY_B1 - T_SEND_B1`.

**Gate B4 (the demo promise):**

- Reply present, threaded under `SUBJECT_B1`.
- `LATENCY_B1 <= 60s`. **If the reply takes longer, that is a regression of the lab5.ca/demo promise -- record as a Critical defect.** (Polling cadence is 5s, so granularity is coarse; if the first observation lands at 65s and it was the first reply on the thread, treat the run as borderline and re-test.)
- Reply on the demo side (`mailpilot email list --account-id <INBOUND_ACCOUNT_ID> --direction outbound --since <TEST_START_B>`) → `is_routed == true`, `workflow_id == DEMO_WORKFLOW_ID`, `route_method == classified`. The classifier ran -- not `thread_match`, since this is a fresh thread.
- Reply body **grounded in the KB** -- run the QA verifier against the reply's `body_text`:

  ```
  python3 .claude/skills/smoke-test/scripts/qa.py check \
    --id "$QA_ID_B1" \
    --reply-text "$(mailpilot email view <REPLY_EMAIL_ID> | python3 -c 'import json,sys; print(json.load(sys.stdin)["email"]["body_text"])')"
  ```

  Exit 0 = pass. Exit 1 = grounding regression: the JSON output names which `expected_tokens` are missing and whether the source file was cited. Both signals are mandatory; a reply that mentions every spec but skips the source citation still fails the prompt-fidelity contract.

### B5. Verify the agent actually used the Drive tools

Run a Logfire query for the `agent.invoke` span produced by B4's reply. Within that invocation, the `running tool` child spans must include, in order:

1. `search_drive_markdown` (with `folder_id=1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat` and a non-empty `query`)
2. `read_drive_markdown` (with a `file_id` returned by step 1)
3. `reply_email`
4. `record_enrollment_outcome` (outcome=`completed`)

**Gate B5:**

- All four tool calls present in this order.
- `search_drive_markdown` returned a non-error list (no `error` key in the tool return) and the list is non-empty.
- `read_drive_markdown` returned a dict with non-empty `content`.
- An agent that uses `list_drive_markdown` instead of `search_drive_markdown` for the in-scope question is a regression: with ≥10 docs in the folder, full enumeration is the failure mode the new tool exists to prevent. Record as a defect even if the reply is otherwise correct. Inventing a `file_id` without searching first is also a prompt-fidelity regression.

### B6. Send the out-of-scope question

Pick a random out-of-scope Q/A pair (Pentair, Evoqua, Grundfos, Suez, Veolia -- vendors explicitly named on lab5.ca/demo as out-of-scope):

```
QA_B2=$(python3 .claude/skills/smoke-test/scripts/qa.py pick --type outscope)
QA_ID_B2=$(printf '%s' "$QA_B2" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
QUESTION_B2=$(printf '%s' "$QA_B2" | python3 -c 'import json,sys; print(json.load(sys.stdin)["question"])')
```

Send it on a fresh subject:

```
mailpilot email send \
  --account-id <OUTBOUND_ACCOUNT_ID> \
  --to inbound@lab5.ca \
  --subject "<SUBJECT_B2>" \
  --body "<QUESTION_B2>"
```

Save `TRIGGER_EMAIL_ID_B2`, capture `T_SEND_B2`, poll the outbound mailbox for `SUBJECT_B2` the same way as B4. Capture `T_REPLY_B2`. Carry `QA_ID_B2` forward to the gate.

**Gate B6 (polite decline, no fabrication):**

- Reply present within 60s.
- Reply body validated by the QA verifier:

  ```
  python3 .claude/skills/smoke-test/scripts/qa.py check \
    --id "$QA_ID_B2" \
    --reply-text "$(mailpilot email view <REPLY_EMAIL_ID> | python3 -c 'import json,sys; print(json.load(sys.stdin)["email"]["body_text"])')"
  ```

  Exit 0 = pass. Exit 1 = fabrication regression OR missing decline-signal language. The JSON output names which `forbidden_token_pairs` matched (vendor name within 60 chars of a digit -- the fabrication signature) and whether at least one `decline_signals` phrase was found.
- The `agent.invoke` for B6 still shows a KB-consulting tool call followed by `reply_email`. `search_drive_markdown` (returning `[]`) is the expected path -- the agent searches with terms from the question, gets no hits, and declines. `list_drive_markdown` is also acceptable for this decline path. Missing both means the agent declined without consulting the KB -- it might have got lucky on this question, but the prompt contract was not honoured. Record as a defect.

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
- `running tool` per invocation -- B4 (in-scope): `search_drive_markdown` + `read_drive_markdown` + `reply_email` + `record_enrollment_outcome`. B6 (out-of-scope decline): `search_drive_markdown` (returning `[]`) + `reply_email` + `record_enrollment_outcome` (`read_drive_markdown` is not required here since no document matches). At least one KB-consulting tool call (`search_drive_markdown` or `list_drive_markdown`) is mandatory in both. With ≥10 docs in the folder, `list_drive_markdown` instead of `search_drive_markdown` on the in-scope path is a regression.
- Any `is_exception=true` or `level=warn` spans -- record. Drive 4xx/5xx surfacing as `drive_unavailable` from the tool is acceptable in the agent's tool-return ledger but should not be `is_exception=true` on the span.

---

## Phase 5: Final report

Produce a report covering both scenarios. Both are mandatory; a missing scenario is a test failure, not a permitted skip. The report has four parts -- A (phase results), B (cross-cutting Logfire pass), C (suggestions), D (defects and notes). Part D is mandatory even on a clean run and is the input surface for `/sdd:spec bug: ...` BACKPROP -- skipping it strands findings.

### Part A: Phase results

```
Smoke Test Results
==================

Phase 0 (one-time setup) ..... PASS  (2 accounts, 2 contacts, 1 company)

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
  B5 Drive tools used ........ PASS  (search_drive_markdown -> read_drive_markdown -> reply_email -> record_enrollment_outcome)
  B6 Out-of-scope decline .... PASS  (LATENCY_B2 = <Ns>; no fabricated specs)
  B7 Activity timeline ....... PASS
  B8 Outbound stayed quiet ... PASS  (0 new outbound sends during B)
  B9 Stop sync loop .......... PASS

Entity IDs (shared by both scenarios):
  Outbound account: <id>   Inbound account: <id>   Company: <id>
  Outbound contact: <id>   Inbound contact: <id>
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

### Part D: Defects and notes

Mandatory final section, even when the test passes cleanly (write `Defects: none.` and keep Notes / Suggestion / runtime if nothing fired). This is the operator-readable hand-off and the input surface for `/sdd:spec bug: ...` -- each Defect entry MUST be a self-contained one-paragraph bug statement that can be pasted verbatim after `/sdd:spec bug: ` to trigger BACKPROP into `SPEC.md` §B without further editing.

**Layout (exact order):**

1. `Defects and notes` heading.
2. `Defect N -- <one-line title> (<severity>).` blocks. Severity is one of `Critical`, `High`, `Medium`, `Low`. Number sequentially across the run (`Defect 1`, `Defect 2`, ...). Critical = customer-facing regression of a public promise (lab5.ca/demo SLA, KB grounding, fabrication-free decline). High = wrong functional output that would mislead a real user (wrong document grounded, wrong specs returned). Medium = correct output with broken presentation (table rendered as plain lines, missing citation). Low = harness-only issues (verifier heuristics, false-negative checks).
3. `Note N -- <title>.` blocks for things that worked as designed and are worth recording (e.g., a SPEC §V invariant held cleanly, concurrent multi-workflow operation verified). Continue numbering from where Defects left off so each item has a unique number across both lists.
4. Optional `Suggestion -- <title>.` blocks for non-bug improvements (test data tweaks, prompt-fidelity hardening). Suggestions are NOT consumable by `/sdd:spec bug:`.
5. Final `Total runtime: ~<N> minutes. <one-sentence verdict>.` line.

**Defect body shape (so `/sdd:spec bug:` can BACKPROP it):**

- Open with the observable failure (what the test saw, what was expected).
- Cite the smoke-test gate that caught it (`A3`, `B4`, `B5`, ...) and the entity / span / file involved.
- Name the suspected root cause in one clause -- the BACKPROP step needs this to draft §B's `cause` column and decide whether a new §V invariant prevents recurrence.
- Reference SPEC §V / §T identifiers when the defect contradicts an existing invariant or task.
- Reference Logfire signals (span name, attribute) when the trace already proves the cause -- e.g., `tool_call_count=5 on agent.invoke before the refusal`.
- Plain prose, no bullets inside the block. ASCII only.

**Example Defect (illustrative -- regenerate, do not paste verbatim):**

> **Defect 1 -- outbound agent over-applies KB grounding (Critical).** A3 first attempt failed: with no KB-related instructions in the prompt, the outbound agent still called `search_drive_markdown` for the random topic, found nothing, and refused to send. Workflow had to be amended with explicit "do NOT call list_drive_markdown / search_drive_markdown / read_drive_markdown" to send. Logfire shows `tool_call_count=5` on the first `agent.invoke` -- five LLM round-trips wasted before the refusal. Suspected cause: outbound system prompt pulls Drive tools in by default; should be opt-in per workflow. Contradicts the spirit of SPEC §V14 (outbound workflows that do not reference a KB MUST NOT consult one).

**Auto-file Critical and High defects.** After the report is rendered, Claude Code MUST invoke `/sdd:spec bug: <defect body>` once per Critical and High defect, sequentially, before yielding control back to the user. Each invocation goes through `/sdd:spec`'s standard BACKPROP flow (root-cause trace, §B append, optional §V invariant, diff-then-confirm). Run them one at a time so each `## Next` reply token (`ok` / `revise` / `cancel`) applies to a single defect -- never batch.

Rules:

- Critical defect → MUST auto-invoke. Critical = regression of a public promise (lab5.ca/demo SLA, KB grounding, fabrication-free decline) and warrants spec-level capture.
- High defect → MUST auto-invoke. High = wrong functional output a real user would see.
- Medium defect → print the `/sdd:spec bug: ...` line in the report's hand-off block but do NOT auto-invoke. Operator decides whether to file. Medium often reflects presentation-layer fixes that may not need a §V invariant.
- Low defect → harness-only; do NOT print and do NOT invoke.
- Notes / Suggestions → NEVER consumable by `/sdd:spec bug:`. They are FYI only.

**Hand-off block format.** At the very end of the report, after the auto-invocations have run, print:

```
Spec hand-off
=============
Auto-filed (Critical/High):
  - Defect <N> -- <title>: <result, e.g., "filed as §B.7 with §V.27" or "cancelled by operator">
  - ...

Operator review (Medium):
  /sdd:spec bug: Defect <N> -- <title>. <body sentence(s)>
  ...

Skipped (Low / Notes / Suggestions): N items, see Part D above.
```

If a `/sdd:spec` invocation is cancelled or revised by the user mid-run, record the outcome in the auto-filed list and continue with the next defect.

---

## Timing

Expected total: ~7 minutes. Phase 0 once, run loop once, no reset between scenarios.

| Phase / scenario               | Duration |
| ------------------------------ | -------- |
| Phase 0 (once, 2 accounts)     | ~15s     |
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
