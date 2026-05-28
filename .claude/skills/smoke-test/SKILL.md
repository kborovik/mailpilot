---
name: smoke-test
description: End-to-end MailPilot smoke test against real Gmail across outbound@lab5.ca and inbound@lab5.ca. One Phase 0 setup → 2 scenarios run sequentially without state reset. Scenario A = outbound workflow + manual operator reply. Scenario B = live KB-grounded inbound auto-reply demo at https://lab5.ca/mailpilot// (real Drive folder, in-scope single-source grounded reply + out-of-scope polite decline + multi-source compare-and-contrast across manufacturers e.g. Dow FilmTec vs Hydranautics vs LG Chem vs Toray RO membranes). Outbound workflow stays active across B → verifies concurrent multi-account, multi-workflow operation. Both scenarios mandatory. Use whenever user says "smoke test", "run end-to-end", "verify the system works", or after non-trivial changes to sync, routing, agent execution, KB grounding, or Pub/Sub code -- even without explicit invocation.
---

# Smoke Test

## What this tests

Two scenarios share one Phase 0 setup and one `mailpilot run` loop. Outbound workflow from A stays active through B → exercises real concurrent multi-workflow, multi-account operation. Agent-to-agent reply loop is prevented by two structural properties, not by isolation:

- Distinct subjects per scenario, so each Gmail thread is owned by exactly one workflow type. A's thread → `thread_match` → outbound workflow. B's fresh threads → classification → demo's inbound workflow.
- Enrollments terminate with `record_enrollment_outcome`, so the agent stops replying once a scenario reaches its outcome.

| Scenario | Active workflows                    | Trigger                                  | Verifies                                                                                                        |
| -------- | ----------------------------------- | ---------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| A        | Outbound only                       | `mailpilot enrollment run`               | Outbound agent send → Gmail delivery → manual operator reply → thread_match routing → agent processes reply     |
| B        | Outbound (terminal) + Demo (active) | `mailpilot email send` (operator-driven) | The lab5.ca/mailpilot/ promise -- KB-grounded reply within 60s for an in-scope question, polite decline for out-of-scope, AND a multi-source compare-and-contrast across manufacturer datasheets |

Both scenarios are **mandatory**. `make clean` runs **once**, at the very start. Scenario B IS the lab5.ca/mailpilot/ system under test -- it must run.

## Conventions

- **Unique subject per scenario, freshly randomized.** Format: `[ST-<HHMMSS>] <topic>`. Generate the topic via Bash on every run -- do not invent it in your head, do not reuse topics from prior runs, do not copy any topic shown in this skill. LLMs anchor on examples and have been observed reusing the same topic across runs, which collides traces and defeats the unique-subject point. Generator:

  ```bash
  TOPIC_A=$(sort -R /usr/share/dict/words 2>/dev/null \
    | grep -E '^[A-Za-z]{4,9}$' | head -2 | tr '\n' ' ' | sed 's/ *$//')
  SUBJECT_A="[ST-$(date +%H%M%S)] ${TOPIC_A}"
  ```

  Scenario B sends three trigger emails. Generate `SUBJECT_B1` (in-scope), `SUBJECT_B2` (out-of-scope), and `SUBJECT_B3` (compare-and-contrast) independently the same way. Verify all four (`SUBJECT_A`, `SUBJECT_B1`, `SUBJECT_B2`, `SUBJECT_B3`) are distinct before continuing. If `/usr/share/dict/words` is unavailable, fall back to `head -c 12 /dev/urandom | base32 | tr -d '=' | head -c 10`.

- **Test start ISO timestamp.** Capture before each scenario; reuse for `--since` filters and Logfire windows.
- **Polling.** When waiting for sync, routing, or agent results: poll up to 12 attempts, 5s apart (~60s total). Do not call `mailpilot account sync` directly -- the background `mailpilot run` loop owns sync.
- **CLI parsing.** All commands use `uv run mailpilot`. Parse JSON output of every command, extract IDs for the next step. Do not capture into a shell variable and re-emit with `echo "$VAR" | python3 -c ...` -- zsh's built-in `echo` interprets backslash escapes in the JSON (e.g. converts the literal two-char `\n` inside `body_text` into a real newline) and the resulting stream is no longer valid JSON. Either pipe `mailpilot ... | python3 -c ...` directly, or use `printf '%s' "$VAR"`.
- **Envelope shape (SPEC §V.5).** `<entity> view`/`create`/`update` returns `{"<singular>": {...}, "ok": true}`; `<entity> list`/`search` returns `{"<plural>": [...], "ok": true}`. Always extract through the wrap: `json.load(sys.stdin)["email"]["workflow_id"]`, not `json.load(sys.stdin)["workflow_id"]`. Operational commands (`enrollment run`, `tag remove`, `enrollment remove`, `*_export`/`*_import`, `config get/set`, `status`) keep their bespoke shapes. `account sync` returns `{"accounts": [...], "ok": true}` per §V.5 plural envelope.
- **ASCII only.** No emojis. Use `->`, `--`, plain pipes.

## Prerequisites

- PostgreSQL running locally.
- `mailpilot config get google_application_credentials` returns a valid path.
- `mailpilot config get anthropic_api_key` returns a non-empty value.
- Network access to Gmail API and Anthropic API.

## Scripts

Located at `.claude/skills/smoke-test/scripts/`. All QA-only -- KB-content maintenance (PDF conversion, verification, Drive push) lives outside the smoke test except for the Drive sync helper below.

**Runtime (used during the test, in B3/B4/B6/B7):**

- `qa.py pick [--type inscope|outscope|compare] [--id ID]` -- emit one Q/A pair as JSON. Random unless `--id` given. Default type is `inscope`. The pair includes the question to send and either the single source `.md` file the agent must cite (in-scope), the list of source files it must synthesize across (compare), or the decline contract (out-of-scope).
- `qa.py source --id ID` -- impersonate `inbound@lab5.ca`, load the pair's source markdown from the demo Drive folder (`1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat`), print to stdout. For in-scope pairs that is one file; for compare pairs each file is preceded by a `=== SOURCE: <name> ===` separator so the operator can grade the reply against every source. Exit non-zero when ANY source file is absent (KB-drift signal -- the pair points at a doc the agent could not have grounded in either). Used by gate B4 (in-scope) and gate B7 (compare).
- `qa.py check --id ID --reply-text "<body>" | --reply-file PATH` -- **out-of-scope only** post-§V.31. Validates a decline reply against `forbidden_token_pairs` and `decline_signals`. Exit 0 = pass, 1 = fail, 2 = caller passed a non-outscope id (in-scope grading is operator-judged in gate B4; compare grading is operator-judged in gate B7). JSON on stdout lists fabrications / decline-signal absence.
- `qa_pairs.json` -- 29 in-scope + 11 compare + 5 out-of-scope pairs. In-scope pairs retain `expected_tokens` for historical-run repro but the field is no longer consumed by any gate; the live source loaded via `qa.py source` is the grounding evidence. Compare pairs carry `source_files: list[str]` (>=2 files) and force the agent to issue >=2 `read_drive_markdown` calls and synthesize across manufacturers (Dow FilmTec vs Hydranautics vs LG Chem vs Toray RO membranes, Pulsafeeder vs ProMinent dosing pumps, Watts UV-COM vs Trojan UVMax UV, Pure Aqua PAPV vs ROPV pressure vessels, etc.). Out-of-scope pairs name (vendor, spec-shape) regex pairs the reply MUST NOT match, plus decline-signal phrases the reply MUST contain.

**Maintenance (run only after the demo Drive folder content changes):**

- `kb-docs/` -- repo source-of-truth markdown for the demo KB. The agent reads from Drive, not from this directory; this is the file set that gets pushed to Drive by `sync_kb_to_drive.py`. Edit files here, then sync.
- `sync_kb_to_drive.py` -- push every `*.md` from `kb-docs/` into the demo Drive folder. Impersonates `kb@lab5.ca` (Shared Drive Manager); requires the service account to be authorized for `https://www.googleapis.com/auth/drive` (the full RW scope, not the read-only scope the test loop uses). Idempotent: existing files are content-diffed and updated in place so `web_view_link` stays stable; new files get `anyoneWithLink:reader` so the link the agent quotes opens for external recipients. `--dry-run` reports planned actions without writing. Run after adding manufacturer datasheets to `kb-docs/` and before re-running the smoke test.
- `generate_qa_pairs.py` -- regenerate `qa_pairs.json`. Reads each `.md` from the live Drive folder via the impersonated DriveClient, asks Haiku 4.5 to draft one in-scope question per file with verifiable expected_tokens. Out-of-scope pairs are hand-curated inside the script; compare pairs are hand-curated directly in `qa_pairs.json` (one good compare pair takes more author judgement than one in-scope pair, so the model isn't asked to draft them).

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

Expect **at least 30 markdown files** (the original water-treatment catalog, plus the five compare-and-contrast targets pushed by `sync_kb_to_drive.py`: `lg-chem-bw-440-r-g2-ro-membrane.md`, `toray-tm720-440-ro-membrane.md`, `prominent-gamma-l-metering-pump.md`, `trojan-uvmax-pro-uv-disinfection.md`, `ropv-r80300b-pressure-vessel-fiberglass.md`). The size matters for two reasons: the agent can no longer succeed by listing every file (regression-masking risk for `search` vs `list`), and the compare-target docs MUST be present or gate B7 cannot grade groundedness against the live sources. If any of the five compare-target files is missing, run `uv run python .claude/skills/smoke-test/scripts/sync_kb_to_drive.py` to push them from the repo source-of-truth before continuing.

If the count is zero or `not_found`, the failure is Drive ACL, not KB content -- `inbound@lab5.ca`'s Shared Drive Reader membership is what makes files visible to the impersonated user. `anyoneWithLink:reader` alone does **not** make files appear here -- it only governs who can open the URL once it's pasted into the reply.

**On failure:** Stop. Report which entity failed and the error JSON.

---

## Scenario A: Outbound workflow

**Hypothesis:** The outbound workflow composes and sends an email; when the operator (Claude Code) replies manually, the outbound agent picks the reply up via `thread_match`, processes it, and reaches a terminal enrollment state without further auto-replies. **Additionally** -- before composing, the agent reads the contact and company entities via `read_contact` / `read_company` (loading attached notes inlined in those tool returns) and personalizes the email by echoing each note's `Reference: <token>` verbatim into the body -- exercising the LLM's context window and validating the personalization path.

Capture `TEST_START_A` (ISO) and `SUBJECT_A` (`[ST-<HHMMSS>] <topic>`) before A1.

### A1a. Seed contact + company notes for personalization

Tests two things: (1) the agent uses its context window -- reading both `read_contact` AND `read_company` before composing; and (2) the agent personalizes by echoing note content. The signal is a deterministic nonce: each note carries a `Reference: <token>` line that the workflow prompt requires the agent to copy verbatim into the email body. Token in body → agent read the note. Both tokens in body → agent read both entities.

Generate two distinct hex nonces per run (do not reuse, do not invent in your head):

```bash
CONTACT_NOTE_TOKEN=$(head -c 6 /dev/urandom | xxd -p)
COMPANY_NOTE_TOKEN=$(head -c 6 /dev/urandom | xxd -p)
[ "$CONTACT_NOTE_TOKEN" != "$COMPANY_NOTE_TOKEN" ] || { echo "FAIL: token collision"; exit 1; }
```

Add the notes (XOR per §V.8 -- one of `--contact-id` / `--company-id`, never both):

```
mailpilot note add --contact-id <INBOUND_CONTACT_ID> \
  --body "Reference: $CONTACT_NOTE_TOKEN. Inbound is VP of Lab Operations; their procurement workflow requires this contact-specific tracking code in every outbound email."

mailpilot note add --company-id <COMPANY_ID> \
  --body "Reference: $COMPANY_NOTE_TOKEN. Lab5 standardizes account-level correlation codes; this code MUST appear in customer correspondence per their procurement policy."
```

**Gate A1a:**

- `mailpilot note list --contact-id <INBOUND_CONTACT_ID>` returns 1 note; its full body (via `mailpilot note view <id>`) contains `Reference: $CONTACT_NOTE_TOKEN`.
- `mailpilot note list --company-id <COMPANY_ID>` returns 1 note; its body contains `Reference: $COMPANY_NOTE_TOKEN`.
- `mailpilot activity list --contact-id <INBOUND_CONTACT_ID> --since <TEST_START_A>` shows 1 `note_added` row (the contact-side note; per §V.23 it carries both `contact_id` and `company_id`).
- `mailpilot activity list --company-id <COMPANY_ID> --since <TEST_START_A>` shows 2 `note_added` rows (contact-side note via multi-target + company-side note).

**Carries forward to:** A3 (body must contain both tokens; tool sequence must include both reads), A8 (note_added activity expectations).

**Prerequisite (separate code change).** This step assumes the agent tools `read_contact` and `read_company` inline recent notes in their return shape (operator choice 2026-05-15 -- "Inline notes in read_contact/read_company"). That tool-surface change is a §V invariant edit and must land via `/sdd:spec` → `/sdd:build` before A3's personalization gate can pass; until it does, the agent has no way to see the tokens and A3's body-token assertion will fail. If the agent surface has not yet shipped, run A1a anyway (it exercises the CLI + activity wiring), and expect the A3 body-token gate to fail -- record as a Critical Bug for tracking, not a regression.

### A1. Import the outbound workflow

The workflow definition is declarative -- it lives in `tests/fixtures/workflows-outbound.json` and is round-tripped via `workflow import` (SPEC §V.39). The fixture contains `${TOPIC_A}` and `${SUBJECT_A}` placeholders that resolve from the shell variables set per the Conventions section; substitute them with `envsubst` before piping the payload to `workflow import` on stdin:

```
TOPIC_A="$TOPIC_A" SUBJECT_A="$SUBJECT_A" envsubst < tests/fixtures/workflows-outbound.json \
  | mailpilot workflow import --account-id <OUTBOUND_ACCOUNT_ID>
```

`workflow import` upserts the row keyed on `(account_id, name)` and auto-activates when both objective and instructions are non-empty -- no separate `workflow start` call needed. Capture the workflow ID for later steps:

```
OUTBOUND_WORKFLOW_ID=$(mailpilot workflow list --account-id <OUTBOUND_ACCOUNT_ID> \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["workflows"][0]["id"])')
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

`mailpilot enrollment run` MUST be invoked exactly once per `(workflow_id, contact_id)`. If the outbound email is not visible in the next gate's `email list` poll, keep polling — do NOT re-invoke `enrollment run`. A second invocation against the same enrollment produces a redundant `agent.invoke` (the agent searches for the prior send and noops correctly, but burns an LLM round-trip and inflates the trace). See SPEC §V.12 / §T.18 / §B.2.

**Gate A3:**

- `enrollment run` output: `"status": "completed"` and `"tool_calls" >= 3` (`read_contact` + `read_company` + `send_email` at minimum).
- `mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction outbound` shows the outbound email with `subject == SUBJECT_A`.
- The email's `body_text` contains `|` (table) and either `**` or `#` (Markdown).
- **Personalization gate (A1a payoff).** The email's `body_text` contains BOTH `$CONTACT_NOTE_TOKEN` AND `$COMPANY_NOTE_TOKEN` verbatim. Either missing → either the agent skipped a `read_*` call or it ignored the note content; treat as a Bug (missing tool call = prompt-fidelity regression; tool call made but token missing = personalization regression). If A1a's prerequisite tool-surface change has NOT shipped (notes not inlined in `read_contact` / `read_company` returns), this gate WILL fail -- record as a Critical Bug to drive the fix, do not skip.
- `mailpilot enrollment list --workflow-id <OUTBOUND_WORKFLOW_ID>` shows enrollment status `active`. Per SPEC §V.10, `enrollment.status` is operational only (`active` or `paused`); the agent never mutates it directly. The send-completion outcome lives in the activity timeline (verified in A8), not on the enrollment row.

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
- `mailpilot enrollment list --workflow-id <OUTBOUND_WORKFLOW_ID>` still shows status `active` -- by design (SPEC §V.10, `enrollment.status` is operational only). The terminal outcome is recorded as an `enrollment_completed` or `enrollment_failed` activity row, verified in A8.
- **No additional outbound emails were sent.** Re-run `mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction outbound --since <TEST_START_A>` and confirm only the original outbound from A3 is present. If the count > 1, the agent kept replying despite the decline signal -- record as a Bug.

**On failure:** Task never created → check that A6's email has `workflow_id` set and the run loop is alive. Task `failed` → `mailpilot task view <TASK_ID>` for the reason.

### A8. Verify the CRM activity timeline

Runtime paths emit `activity` rows automatically (no manual `activity create`). Read the inbound contact's timeline:

```
mailpilot activity list --contact-id <INBOUND_CONTACT_ID> --since <TEST_START_A>
```

**Gate A8 (activity wiring):** activity types follow the `enrollment_*` / `email_*` vocabulary enforced by `activity.type` CHECK constraint in `src/mailpilot/schema.sql`.

- `enrollment_added` with `workflow_id == OUTBOUND_WORKFLOW_ID` on the activity row itself (FK column on `activity`, not inside the `detail` JSONB; `detail` carries `{"workflow_name": ...}` as a display label). Emitted by `enrollment add`.
- `email_sent` with `summary == SUBJECT_A` (emitted by `email_ops.send_email` when the outbound agent sent in A3).
- `email_received` with the operator-reply subject (emitted by sync's `_store_inbound_message` when the reply landed in the outbound mailbox in A6).
- Exactly one of `enrollment_completed` or `enrollment_failed` (emitted by `agent.tools.record_enrollment_outcome` in A7); summary equals the agent's `reason`.
- 1 `note_added` row from A1a's contact-side `note add` (the row carries `contact_id == INBOUND_CONTACT_ID` and `company_id == COMPANY_ID` per §V.23 multi-target; the company-side `note add` does NOT appear here because it has no `contact_id`).
- No `tag_added` rows from this scenario (we did not run `tag add`).

Also assert company-side timeline:

```
mailpilot activity list --company-id <COMPANY_ID> --since <TEST_START_A>
```

Must contain 2 `note_added` rows -- the contact-side note (via multi-target) and the company-side note.

If any expected type is missing, the runtime activity wiring regressed for that path.

### Logfire review for Scenario A

Do this review now, before B, so the window cleanly bounds A's spans. Use `/logfire:debug` with project=`mailpilot` and window `[TEST_START_A, now]`. Spans to verify:

- `agent.invoke` -- count by `trigger` attribute, not by total. Per SPEC §V.12 / §T.18, the span carries an explicit `trigger` label set by the caller path:
  - `trigger="task"` -- expect exactly **1** (A7 reply handling, drained by background `mailpilot run`). More than 1 → agent kept replying (loop regression). This is the regression signal for Scenario A.
  - `trigger="enrollment_run"` -- expect at least **1** (A3 send via foreground `enrollment run`). Tolerated regardless of count: an operator double-fire produces extra `enrollment_run` spans that correctly noop, so they cost an LLM round-trip but do not signal regression. §T.19 / §B.2 prefer single-invocation discipline (see A3) but the trace contract here permits more.
  - `trigger="email"` / `trigger="manual"` -- not expected in Scenario A; flag if present.
- `running tool` -- A3: expect `read_contact`, `read_company`, and `send_email` (both reads MUST appear per the A1a personalization contract; order may be interleaved). `record_enrollment_outcome` is **not** expected here (it fires after a reply, not on initial send). A7: expect `record_enrollment_outcome` and **no** `send_email` or `reply_email`.
- `agent.invoke` (A3) `input_tokens` -- should noticeably exceed the no-notes baseline (~+200-400 tokens) because both note bodies are inlined into the `read_contact` / `read_company` tool returns. A baseline-equivalent count signals either the tool-surface change hasn't shipped (notes not inlined) or the agent skipped the reads.
- `routing.route_email` -- the reply (A6) → `route_method=thread_match` and `workflow_id == OUTBOUND_WORKFLOW_ID`. The inbound-side email from A4 → `route_method=skipped_no_workflows` (no inbound workflow at the time).
- `gmail.send_message` -- 2 calls total (A3 by agent + A5 by operator).
- Any `is_exception=true` or `level=warn` spans -- record them.

---

## Transition to Scenario B

Do not stop the sync loop. Do not run `make clean`. Do not recreate accounts or contacts. The outbound workflow stays active with its enrollment in a terminal state, and the run loop keeps syncing both accounts. Scenario B layers a KB-grounded inbound workflow on `inbound@lab5.ca` on top of this live state -- the explicit multi-workflow / multi-account checkpoint of the test.

---

## Scenario B: KB-grounded demo (lab5.ca/mailpilot/)

**Hypothesis:** The lab5.ca/mailpilot/ system delivers on its public promise -- "a professional response grounded in real data" within ~60 seconds for in-scope questions, a polite explanatory reply (no fabricated specs) for questions outside the KB, and a structurally-sound multi-source synthesis when the question forces the agent to compare specs across vendor datasheets. With the outbound workflow from A still active, the demo workflow on `inbound@lab5.ca` correctly classifies each operator-sent question on a fresh thread, the agent grounds its answer in the real Drive KB via `list_drive_markdown` + `read_drive_markdown`, issues one `read_drive_markdown` per source document the compare question targets, and the reply round-trips to the outbound mailbox.

**Real KB used.** This scenario uses the production KB folder, not a fixture:

- Shared Drive: `MailPilot` (ID `0AJIvyECg210LUk9PVA`). Members: `kb@lab5.ca` Manager, `inbound@lab5.ca` Reader.
- Folder name: `MailPilot Demo`
- Folder ID: `1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat`
- Markdown files (as of writing -- the Phase 0 KB visibility gate enumerates them and asserts the ≥30 floor; re-confirm via that gate before each run). In-scope single-source seeds:
  - `pure-aqua-commercial-ro-systems.md` -- TW-series RO systems (e.g., TW-18.0K-1240).
  - `pure-aqua-industrial-water-softener.md` -- SF-series softeners (e.g., SF-100S).
  - `watts-uv-com-disinfection.md` -- UV-COM disinfection units.

  Compare-and-contrast targets (pushed by `sync_kb_to_drive.py` from `kb-docs/`):
  - 8" brackish RO membranes (4-way bake-off): `dow-filmtec-eco-440i-ro-membrane.md`, `hydranautics-cpa4-ro-membrane.md`, `lg-chem-bw-440-r-g2-ro-membrane.md`, `toray-tm720-440-ro-membrane.md`.
  - Chemical dosing pumps: `pulsafeeder-chem-tech-100-150-chemical-dosing-pump.md` vs `prominent-gamma-l-metering-pump.md`.
  - UV disinfection: `watts-uv-com-disinfection.md` vs `trojan-uvmax-pro-uv-disinfection.md` (and `watts-smartstream-uv-disinfection.md` for an intra-vendor pair).
  - FRP pressure vessels: `pure-aqua-papv-ro-pressure-vessels.md` vs `ropv-r80300b-pressure-vessel-fiberglass.md`.
  - Iron-removal filter media (intra-vendor): `clack-birm-iron-removal-media.md` vs `clack-pyrolox-iron-manganese-media.md`.

  Plus distractors on adjacent in-scope water-treatment topics so the search-vs-list discriminator (gate B5) is meaningful. The single-source seeds are what the in-scope B3 question targets; the agent must locate one of them via `search_drive_markdown` rather than by listing the whole folder. The compare targets are what B7 forces the agent to multi-read.

  PDFs sit alongside the `.md` files; the `mimeType='text/markdown'` filter on both `list_drive_markdown` and `search_drive_markdown` must skip them. If it does not, that is a defect.

- Access model: because the KB lives in a Shared Drive, listing depends on the impersonated user being a Shared Drive member, not on per-file ACL. `anyoneWithLink:reader` is set on every file so the `web_view_link` returned by `read_drive_markdown` opens for strangers reading the agent's reply. If `list_drive_markdown` returns an empty list or `not_found`, the failure mode is almost always Shared Drive membership of `inbound@lab5.ca`, not file-level sharing -- fix that first, do not patch around it.

Capture `TEST_START_B` (ISO, must be later than A's last activity) and three distinct subjects -- `SUBJECT_B1` (in-scope), `SUBJECT_B2` (out-of-scope), `SUBJECT_B3` (compare-and-contrast) -- per the Conventions section. All three must differ from each other and from `SUBJECT_A`.

### B1. Import the demo inbound workflow

The workflow definition is declarative -- the operator-style instructions citing the real folder ID live in `tests/fixtures/workflows-inbound.json` and are round-tripped via `workflow import` (SPEC §V.39). The agent's behaviour comes from that prompt -- changing the wording changes what we test, so edit the fixture, do not type a different command.

```
mailpilot workflow import \
  --account-id <INBOUND_ACCOUNT_ID> \
  --file tests/fixtures/workflows-inbound.json
```

`workflow import` upserts the row keyed on `(account_id, name)` and auto-activates when both objective and instructions are non-empty -- no separate `workflow start` call needed. Capture the workflow ID and pre-enroll the sender:

```
DEMO_WORKFLOW_ID=$(mailpilot workflow list --account-id <INBOUND_ACCOUNT_ID> \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["workflows"][0]["id"])')
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

Critical gate. The lab5.ca/mailpilot/ page promises delivery within ~60 seconds. Poll the outbound mailbox:

```
mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction inbound --since <TEST_START_B>
```

Match by `SUBJECT_B1` (likely with `Re:` prefix). Record the wall-clock time the reply first appears as `T_REPLY_B1`, compute `LATENCY_B1 = T_REPLY_B1 - T_SEND_B1`.

**Gate B4 (the demo promise):**

- Reply present, threaded under `SUBJECT_B1`.
- `LATENCY_B1 <= 60s`. **If the reply takes longer, that is a regression of the lab5.ca/mailpilot/ promise -- record as a Critical Bug.** (Polling cadence is 5s, so granularity is coarse; if the first observation lands at 65s and it was the first reply on the thread, treat the run as borderline and re-test.)
- Reply on the demo side (`mailpilot email list --account-id <INBOUND_ACCOUNT_ID> --direction outbound --since <TEST_START_B>`) → `is_routed == true`, `workflow_id == DEMO_WORKFLOW_ID`, `route_method == classified`. The classifier ran -- not `thread_match`, since this is a fresh thread.
- Reply body **grounded in the KB** -- operator-judged per SPEC §V.31. Substring match against curated `expected_tokens` was retired (false negatives on phrasing variation like `0.48 mm` vs `0.48mm`); the operator now grades the reply against the live source doc. Procedure:
  1. Load the source doc the pair points at:

     ```
     python3 .claude/skills/smoke-test/scripts/qa.py source --id "$QA_ID_B1"
     ```

     Exit non-zero = `source_file` is no longer in the demo Drive folder (KB drift) -- record as a Bug and stop B4. The script impersonates `inbound@lab5.ca` and looks the file up in folder `1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat`, the same folder the agent grounded against.

  2. Read the agent's reply body:

     ```
     mailpilot email view <REPLY_EMAIL_ID> | python3 -c 'import json,sys; print(json.load(sys.stdin)["email"]["body_text"])'
     ```

  3. As operator, emit a structured JSON verdict on stdout (no free-form rating). Schema, exact field names:

     ```json
     {
       "qa_id": "<QA_ID_B1>",
       "question": "<question text>",
       "source_file": "<source_file from pair>",
       "answers_question": true,
       "every_factual_claim_supported_by_source": true,
       "cites_source_file": true,
       "unsupported_claims": [],
       "verdict": "pass"
     }
     ```

     Each unsupported factual claim in the reply MUST appear verbatim in `unsupported_claims` (structural defence against LLM-judge sycophancy -- the field forces the grader to enumerate concrete misses rather than hand-wave a passing rating). `verdict` MUST be `"pass"` if and only if all three booleans are true AND `unsupported_claims` is empty; otherwise `"fail"`.

  Anything other than `verdict == "pass"` is a grounding regression -- record the verdict JSON in §2 Bugs. `qa_pairs.json.expected_tokens` is retained for historical-run repro only and is no longer consumed by any gate.

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
- An agent that uses `list_drive_markdown` instead of `search_drive_markdown` for the in-scope question is a regression: with ≥10 docs in the folder, full enumeration is the failure mode the new tool exists to prevent. Record as a Bug even if the reply is otherwise correct. Inventing a `file_id` without searching first is also a prompt-fidelity regression.
- The `reply_email` span returned no `error` key. A return with `error == "format"` means the spec-table lint (§V.29) rejected the body -- the agent rendered specs as space-aligned text instead of a Markdown pipe-table. Record as a prompt-fidelity Bug for B4.

### B6. Send the out-of-scope question

Pick a random out-of-scope Q/A pair (Pentair, Evoqua, Grundfos, Suez, Veolia -- vendors explicitly named on lab5.ca/mailpilot/ as out-of-scope):

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

Out-of-scope decline keeps the script verifier (per SPEC §V.31): regex appropriately fits shape detection (vendor name near a digit-shaped fabrication, decline-phrase presence) and the surface area is small. The operator-judged path applies only to in-scope grounding (B4).

- Reply present within 60s.
- Reply body validated by the QA verifier:

  ```
  python3 .claude/skills/smoke-test/scripts/qa.py check \
    --id "$QA_ID_B2" \
    --reply-text "$(mailpilot email view <REPLY_EMAIL_ID> | python3 -c 'import json,sys; print(json.load(sys.stdin)["email"]["body_text"])')"
  ```

  Exit 0 = pass. Exit 1 = fabrication regression OR missing decline-signal language. Exit 2 = caller passed an in-scope id by mistake (use B4's operator-judged flow instead). The JSON output names which `forbidden_token_pairs` matched (vendor name within 60 chars of a digit -- the fabrication signature) and whether at least one `decline_signals` phrase was found.

- The `agent.invoke` for B6 still shows a KB-consulting tool call followed by `reply_email`. `search_drive_markdown` (returning `[]`) is the expected path -- the agent searches with terms from the question, gets no hits, and declines. `list_drive_markdown` is also acceptable for this decline path. Missing both means the agent declined without consulting the KB -- it might have got lucky on this question, but the prompt contract was not honoured. Record as a Bug.

### B6.5. Concurrent-fanout race regression check

This is a Logfire-only gate that piggybacks on B7's compare invocation (the only Scenario B turn that emits multiple `read_drive_markdown` calls). It exists to catch §B.34 -- the `httplib2.Http` thread-safety race where one parallel `read_drive_markdown` returned in ~1s while its sibling hung 60.83s at the socket timeout, killing the agent run. The structural fix is `sequential=True` on every Drive `Tool(...)` registration in `src/mailpilot/agent/templates.py` (§V.61); this gate verifies the dispatcher actually serializes parallel emissions in production.

Run **after** B7's tool-use gate (the assertion needs B7's `agent.invoke` to be in Logfire). Window `[T_SEND_B3, T_REPLY_B3 + 10s]`, scope to B7's invocation:

```sql
WITH compare AS (
  SELECT trace_id, span_id
  FROM records
  WHERE deployment_environment = 'development'
    AND span_name = 'agent.invoke'
    AND start_timestamp >= '<T_SEND_B3>'
    AND start_timestamp <= '<T_REPLY_B3>'
    AND attributes->>'workflow_id' = '<DEMO_WORKFLOW_ID>'
  LIMIT 1
)
SELECT span_name, start_timestamp, end_timestamp,
       attributes->>'gen_ai.tool.name' AS tool_name,
       is_exception, level
FROM records
WHERE trace_id IN (SELECT trace_id FROM compare)
  AND attributes->>'gen_ai.tool.name' = 'read_drive_markdown'
ORDER BY start_timestamp
LIMIT 20;
```

**Gate B6.5 (race signature absent):**

- At least 2 `read_drive_markdown` spans inside the B7 `agent.invoke` (the compare-and-contrast question forces multi-doc fanout; fewer is a separate regression already caught by B7's `EXPECTED_READ_COUNT` check).
- **No span duration >=60s.** A `read_drive_markdown` span at or past the 60s `_DRIVE_HTTP_TIMEOUT_SECONDS` cap is the §B.34 hang signature; it means a sibling read raced the shared `httplib2.Http` and stalled at the socket timeout. Record as a Critical Bug -- the race fix has regressed and the next failure will burn the §V.44 retry budget.
- **No `is_exception=true` and no `level=warn` on any of these spans.** A bare `TimeoutError` / `socket.timeout` / `OSError` that escapes the tool wrapper means the broadened catch envelope in `src/mailpilot/agent/tools.py` was reverted; the structured `drive_unavailable` tool return (which lets the surviving sibling carry the agent run) is gone.
- Sibling spans must not overlap. `sequential=True` in the dispatcher serializes parallel emissions, so for any two `read_drive_markdown` spans, the later one's `start_timestamp` >= the earlier one's `end_timestamp`. An overlap means a future Pydantic AI bump dropped the `sequential` honor on parallel tool dispatch; record as a Bug and pin the working pydantic-ai version while investigating.

### B7. Send the compare-and-contrast question

Pick a random compare pair from the manifest. Compare pairs force the agent to read >=2 manufacturer datasheets and synthesize across them -- the failure mode this gate catches is a reply that names all the products in question but pulls specs from only one source (or, worse, fabricates specs for products it never read):

```
QA_B3=$(python3 .claude/skills/smoke-test/scripts/qa.py pick --type compare)
QA_ID_B3=$(printf '%s' "$QA_B3" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
QUESTION_B3=$(printf '%s' "$QA_B3" | python3 -c 'import json,sys; print(json.load(sys.stdin)["question"])')
SOURCE_FILES_B3=$(printf '%s' "$QA_B3" | python3 -c 'import json,sys; print(" ".join(json.load(sys.stdin)["source_files"]))')
EXPECTED_READ_COUNT=$(printf '%s' "$QA_B3" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["source_files"]))')
```

Send the question on a fresh subject:

```
mailpilot email send \
  --account-id <OUTBOUND_ACCOUNT_ID> \
  --to inbound@lab5.ca \
  --subject "<SUBJECT_B3>" \
  --body "<QUESTION_B3>"
```

Save `TRIGGER_EMAIL_ID_B3`, capture wall-clock send time as `T_SEND_B3`. Carry `QA_ID_B3`, `SOURCE_FILES_B3`, and `EXPECTED_READ_COUNT` forward.

Poll the outbound mailbox for `SUBJECT_B3` (likely `Re:` prefixed) the same way as B4. Record `T_REPLY_B3` and compute `LATENCY_B3 = T_REPLY_B3 - T_SEND_B3`.

**Gate B7 (multi-source grounding, no single-source synthesis):**

- Reply present within 60s. Compare questions force a longer agent loop (>=2 `read_drive_markdown` calls), so latency near the 60s ceiling is more likely here than in B4 -- but a reply over 60s is still a Critical regression of the lab5.ca/mailpilot/ promise.
- Reply on the demo side has `is_routed == true`, `workflow_id == DEMO_WORKFLOW_ID`, `route_method == classified`. Fresh thread, so not `thread_match`.
- Reply body **grounded in EVERY source listed in `source_files`** -- operator-judged per the same §V.31 contract that governs B4. Procedure:
  1. Load all source docs in one bundle:

     ```
     python3 .claude/skills/smoke-test/scripts/qa.py source --id "$QA_ID_B3"
     ```

     Non-zero exit = at least one source is missing from Drive (KB drift) -- record as a Bug and stop B7 (the agent could not have grounded against the missing doc, so a regression here would be false). `qa.py source` prints each source preceded by a `=== SOURCE: <name> ===` separator so the operator can attribute claims to specific files.

  2. Read the agent's reply body:

     ```
     mailpilot email view <REPLY_EMAIL_ID_B3> | python3 -c 'import json,sys; print(json.load(sys.stdin)["email"]["body_text"])'
     ```

  3. As operator, emit a structured JSON verdict on stdout. Schema, exact field names:

     ```json
     {
       "qa_id": "<QA_ID_B3>",
       "question": "<question text>",
       "source_files": ["<file 1>", "<file 2>", "..."],
       "answers_question": true,
       "every_factual_claim_supported_by_source": true,
       "every_source_contributes_at_least_one_claim": true,
       "cites_all_source_files": true,
       "unsupported_claims": [],
       "single_sourced_compare": false,
       "verdict": "pass"
     }
     ```

     New fields versus B4:
     - `every_source_contributes_at_least_one_claim` -- false if the reply names a product but pulls zero specs from its datasheet (the agent guessed or skipped that read).
     - `single_sourced_compare` -- true if every concrete number in the reply could plausibly have come from just one of the source docs (failure to actually compare). Flip of the structural property the compare-test is checking.
     - `unsupported_claims` -- list each factual claim verbatim that no source supports. Forces concrete enumeration to defend against LLM-judge sycophancy.

     `verdict` MUST be `"pass"` if and only if `answers_question`, `every_factual_claim_supported_by_source`, `every_source_contributes_at_least_one_claim`, and `cites_all_source_files` are all true, AND `unsupported_claims` is empty, AND `single_sourced_compare` is false. Otherwise `"fail"`.

- **Tool-use gate.** The `agent.invoke` for B7 must include:
  1. `search_drive_markdown` >=1 (with a non-empty query and `folder_id=1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat`).
  2. `read_drive_markdown` exactly `EXPECTED_READ_COUNT` times (one per file in `source_files`), each returning non-empty `content`.
  3. `reply_email` (no `error` key in the tool return; format errors mean the spec-table lint rejected the body -- record as a §V.29 prompt-fidelity Bug).
  4. `record_enrollment_outcome` (`outcome=completed`).

  Fewer `read_drive_markdown` calls than `EXPECTED_READ_COUNT` is the headline regression this gate catches -- the agent guessed at one of the products' specs instead of reading the doc. Record as a Bug even if the reply happens to be factually correct. More reads than expected (e.g., the agent followed a distractor) is acceptable as long as the four required calls above are present.

### B8. Verify the CRM activity timeline

```
mailpilot activity list --contact-id <OUTBOUND_CONTACT_ID> --since <TEST_START_B>
```

**Gate B8 (activity wiring):** activity types follow the `enrollment_*` / `email_*` vocabulary enforced by `activity.type` CHECK constraint in `src/mailpilot/schema.sql`.

- `enrollment_added` with `workflow_id == DEMO_WORKFLOW_ID` on the activity row itself (FK column, not `detail` JSONB). From B1.
- 3 `email_received` activities -- the demo mailbox received the trigger emails for B3 (in-scope), B6 (out-of-scope), and B7 (compare).
- 3 `email_sent` activities from the agent replies (subjects begin with `Re:`).
- 3 `enrollment_completed` activities (one per question, all emitted by `record_enrollment_outcome`).

### B9. Concurrent-workflow quiet check

The Scenario A outbound workflow is still active throughout B. It must not have reacted to B's traffic.

The outbound _account_ legitimately sends mail in B (the operator's three trigger emails in B3, B6, and B7 leave from `outbound@`); those are not the signal we care about. The signal is whether the outbound _workflow_ generated any agent-driven sends. Filter by `workflow_id`:

```
mailpilot email list \
  --account-id <OUTBOUND_ACCOUNT_ID> \
  --direction outbound \
  --workflow-id <OUTBOUND_WORKFLOW_ID> \
  --since <TEST_START_B>
```

**Gate B9:** Zero rows. Any non-zero count means the still-active outbound workflow reacted to B's traffic -- record as a Bug.

Sanity check the operator triggers are still there (B3, B6, and B7 are agent-driven from B's perspective but operator-driven from A's perspective, so they carry `workflow_id == null` on the outbound mailbox):

```
mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction outbound --since <TEST_START_B>
```

Expect exactly 3 rows (the B3, B6, and B7 triggers), each with `workflow_id == null`. Any deviation is a separate signal -- either an unexpected outbound send (record as a Bug) or a missing trigger (re-run B3/B6/B7).

### B10. Stop the sync loop

Send SIGTERM to the background `mailpilot run` (e.g. `kill <pid>`). Wait for `Sync loop stopped` in the captured output. Confirm the `sync_status` table is empty. If the process does not exit within 10s, send SIGKILL and record this in the report.

### Logfire review for Scenario B

Window `[TEST_START_B, now]`. Spans to verify:

- `agent.invoke` -- exactly **3** invocations (B4 in-scope, B6 out-of-scope, B7 compare). More than 3 → demo agent re-fired or outbound workflow reacted to B's traffic.
- `routing.route_email` -- all three demo-side trigger emails → `route_method=classified`. Outbound-side replies → `route_method=skipped_no_inbound_workflows`.
- `classify_email` -- 3 invocations. All three `result` values match `DEMO_WORKFLOW_ID`.
- `running tool` per invocation:
  - B4 (in-scope): `search_drive_markdown` + `read_drive_markdown` + `reply_email` + `record_enrollment_outcome`.
  - B6 (out-of-scope decline): `search_drive_markdown` (returning `[]`) + `reply_email` + `record_enrollment_outcome` (`read_drive_markdown` not required since no document matches).
  - B7 (compare-and-contrast): `search_drive_markdown` >=1 + `read_drive_markdown` exactly `EXPECTED_READ_COUNT` times (one per file in the pair's `source_files`) + `reply_email` + `record_enrollment_outcome`. Fewer reads than `EXPECTED_READ_COUNT` is the headline regression: agent guessed a doc instead of reading it. The set of `file_id` arguments to `read_drive_markdown` must equal the set Drive returned for the `source_files` names -- mismatch means the agent read the wrong doc.
  - At least one KB-consulting tool call (`search_drive_markdown` or `list_drive_markdown`) is mandatory in all three. With ≥30 docs in the folder, `list_drive_markdown` instead of `search_drive_markdown` on B4 or B7 is a regression. On B6 (decline), `list_drive_markdown` is acceptable.
- `agent.invoke` (B7) `input_tokens` -- should be noticeably higher than B4 because the compare invocation pulls 2-4 full datasheets into the agent's context. A B7 token count at or below B4's signals the agent skipped one or more reads -- cross-check against the read-count gate.
- Any `is_exception=true` or `level=warn` spans -- record. Drive 4xx/5xx surfacing as `drive_unavailable` from the tool is acceptable in the agent's tool-return ledger but should not be `is_exception=true` on the span.

---

## Phase 5: Final report

Three sections, actionable-only: §1 Execution, §2 Bugs, §3 Invariants. Plus the Spec hand-off block. Both scenarios mandatory; a missing scenario is a test failure, not a permitted skip. The report is a queue of `/sdd:spec` inputs, not a narrative -- if an item is not actionable, it does not appear.

The report is chat-only -- do NOT write it to disk. The operator reads it inline and pastes any "Operator review" lines they want to file. If they later want a durable copy, they will ask.

### Derivation aids (NOT rendered to the report)

These are scan inputs that feed §2 (concrete failures) and §3 (under-specified contracts). Do NOT render the SQL queries, the 13-cat checklist, or per-category narrative as sections in the output.

**Logfire pass.** Run once across both scenarios via `/logfire:debug` with the test window:

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
       attributes->>'cache_read_input_tokens' AS cache_read,
       attributes->>'input_tokens' AS in_tok,
       attributes->>'output_tokens' AS out_tok
FROM records
WHERE start_timestamp >= '<EARLIEST_TEST_START>'
  AND span_name = 'agent.invoke'
ORDER BY start_timestamp
LIMIT 50
```

**Scan checklist** (categories, scenario-agnostic; skip cleanly if the run did not exercise one). Use these to find anything that should escalate to §2 Bugs or §3 Invariants:

1. CLI usability -- awkward sequencing, missing fields, unhelpful error messages.
2. Logfire observability -- missing spans/attributes, noisy span families, broken parent-child causality.
3. Agent prompt fidelity -- DOs/DON'Ts honoured, format constraints, stop conditions, decline vs fabricate, reasoning matches tool calls.
4. Agent context window -- prompt cache active, tool inventory appropriate, redundant context.
5. Latency -- end-to-end per stage, scenario SLAs, cold vs warm.
6. Cost / token economics -- per-invoke tokens, cache hit ratio, classifier cost share.
7. Tool-call efficiency -- calls per outcome, redundant calls, refusal-round waste.
8. Routing & classification quality -- `route_method` distribution, classifier reasoning sanity, false-skip cases.
9. Concurrent workflow safety -- cross-workflow / cross-account leakage.
10. Tool integration health -- per-integration error rate, informative tool errors.
11. Data integrity -- agent claims vs DB rows, no orphans, no silent drops.
12. Determinism / variance -- run-over-run drift on critical paths.
13. Other -- timing, races, performance, anything off-grid.

### §1 Execution

Phase-results matrix + 3-bullet Logfire signal summary. One screen.

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

Scenario B: KB-grounded demo (lab5.ca/mailpilot/, outbound workflow still active)
  B1  Create demo workflow ......... PASS  (workflow list shows 2 active)
  B2  Sync loop still alive ........ PASS
  B3  In-scope trigger send ........ PASS
  B4  60s grounded reply ........... PASS  (LATENCY_B1 = <Ns>; cited model: <e.g., TW-18.0K-1240>)
  B5  Drive tools used ............. PASS  (search_drive_markdown -> read_drive_markdown -> reply_email -> record_enrollment_outcome)
  B6  Out-of-scope decline ......... PASS  (LATENCY_B2 = <Ns>; no fabricated specs)
  B7  Compare-and-contrast reply ... PASS  (LATENCY_B3 = <Ns>; <N> sources, <N> read_drive_markdown calls; no single-sourced specs)
  B8  Activity timeline ............ PASS  (3 received / 3 sent / 3 enrollment_completed)
  B9  Outbound stayed quiet ........ PASS  (0 new outbound sends during B)
  B10 Stop sync loop ............... PASS

Entity IDs:
  Outbound account: <id>   Inbound account: <id>   Company: <id>
  Outbound contact: <id>   Inbound contact: <id>
  Outbound workflow: <id>  Demo workflow: <id>

Logfire signals:
  - Top span by volume: <span_name> (<N> calls)
  - Errors / warnings: <N> (<one-line summary or "none">)
  - Cache-hit ratio (multi-turn invokes): <pct> (<one-line note if below ~0.5>)
```

If a phase failed, stop §1 at the failing phase with the failure JSON and any captured stdout from the background `mailpilot run`.

### §2 Bugs

Observable failures, one paragraph each. Mandatory section -- write `Bugs: none.` if nothing fired.

A Bug = an observable failure: a gate that did not pass, a regression of a public promise, a wrong functional output, or a broken presentation. Things that "worked as designed" do not appear here. Latent risks surfaced by inspection (no concrete failure this run) belong in §3 Invariants.

**Bug block format.** One paragraph per entry, this exact shape:

> **Bug N -- <one-line title> (<severity>).** <observable failure: what the test saw vs expected.> Caught at gate <gate id>. <entity / span / file involved.> Suspected cause: <one clause>. <SPEC §V / §T cross-reference if contradicting an existing invariant.> <Logfire signal if the trace proves cause.>
>
> **Spec action:** `<routing tag>` -- `<exact /sdd:spec invocation>`

Routing tag:

- `bug` -- file an incident in §B. Use when a concrete failure occurred. BACKPROP decides whether a new §V accompanies. Invocation: `/sdd:spec bug: <body sentence(s)>`
- `bug+invariant` -- file in §B AND propose a specific new §V in the body so BACKPROP appends both. Use when the failure has a clear recurrence class and the invariant can be stated in one sentence. Invocation: `/sdd:spec bug: <body>. Propose §V: <invariant text>`
- `code-only` -- record only; no spec entry. Use only for mechanical / harness-flake failures with zero recurrence class.

Severity (number sequentially `Bug 1`, `Bug 2`, ...):

- `Critical` -- regression of a public promise (lab5.ca/mailpilot/ SLA, KB grounding, fabrication-free decline). Always at least `bug` routing.
- `High` -- wrong functional output a real user would see. Always at least `bug` routing.
- `Medium` -- correct output, broken presentation. Routing typically `bug` or `code-only`.
- `Low` -- harness-only issues. Routing typically `code-only`.

**Auto-file matrix.** After the chat-rendered report, invoke the `Spec action:` line for each Bug per this matrix:

| Severity | `bug` / `bug+invariant`      | `code-only` |
| -------- | ---------------------------- | ----------- |
| Critical | auto-invoke                  | print only  |
| High     | auto-invoke                  | print only  |
| Medium   | print only (operator review) | print only  |
| Low      | print only                   | print only  |

"Auto-invoke" = call `/sdd:spec` with the exact `Spec action:` invocation. Run them sequentially so each `## Next` reply token (`ok` / `revise` / `cancel`) applies to a single Bug -- never batch. Record outcome (`filed as §B.7 with §V.27`, `cancelled by operator`, etc.) in the hand-off block. "Print only" means the line goes into the hand-off "Operator review" list, ready for the operator to paste.

### §3 Invariants

Under-specified contracts surfaced by inspection -- no concrete failure this run, but the run revealed a gap in §V. Mandatory section -- write `Invariants: none.` if nothing fired. Always print-only; the operator pastes if they choose to file.

**Invariant block format.**

> **Invariant N -- <title>.** <one paragraph: observation, proposed change, why it helps. Plain prose, no bullets.>
>
> **Spec action:** `propose-invariant` -- `/sdd:spec amend §V add: <invariant text>`

Number sequentially across the run so each item has a unique number (`Bug 1`, `Bug 2`, `Invariant 3`, ...).

End §3 with the runtime line:

```
Total runtime: ~<N> minutes. <one-sentence verdict>.
```

### Spec hand-off block

After §3, after auto-invocations have run, print:

```
Spec hand-off
=============
Auto-filed (Critical/High Bugs):
  - Bug <N> -- <title>: <result, e.g., "filed as §B.7 with §V.27", "cancelled by operator">
  - ...

Operator review (print-only Bugs + all Invariants):
  /sdd:spec bug: <Bug N body...>                          # Bug N (<severity>, <routing>)
  /sdd:spec amend §V add: <invariant text>                # Invariant N
  ...
```

Each line under "Operator review" MUST be the exact `Spec action:` invocation -- ready to paste. The trailing `# comment` names the originator so the operator can find it in the body.

If a `/sdd:spec` invocation is cancelled or revised by the user mid-run, record the outcome in the auto-filed list and continue with the next Bug.

---

## Timing

Expected total: ~8 minutes. Phase 0 once, run loop once, no reset between scenarios. The added compare-and-contrast question (B7) costs roughly one extra 60s reply window over the prior baseline because it forces 2-4 `read_drive_markdown` calls and a multi-doc synthesis.

| Phase / scenario                     | Duration |
| ------------------------------------ | -------- |
| Phase 0 (once, 2 accounts)           | ~15s     |
| A1 / B1 workflow setup               | ~5s      |
| A2 start run loop                    | ~5s      |
| A3 outbound agent                    | ~10s     |
| A4 sync + route                      | ~10-60s  |
| A5 / B3 / B6 / B7 operator send      | ~3s each |
| A6 reply round-trip                  | ~10-60s  |
| A7 task drain                        | ~10-60s  |
| B4 in-scope reply (60s SLA)          | ~10-60s  |
| B6 out-of-scope reply (60s SLA)      | ~10-60s  |
| B7 compare reply (60s SLA)           | ~20-60s  |
| A8 / B8 activity check               | ~3s      |
| B10 stop run loop                    | ~3s      |
| Report                               | ~10s     |
