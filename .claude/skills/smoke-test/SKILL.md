---
name: smoke-test
description: |
  End-to-end MailPilot smoke test against real Gmail across outbound@lab5.ca and inbound@lab5.ca. One Phase 0 setup → 3 scenarios run sequentially without state reset. Scenario A = outbound workflow + manual operator reply. Scenario B = live KB-grounded inbound auto-reply demo at https://lab5.ca/mailpilot// (real Drive folder, in-scope single-source grounded reply + out-of-scope polite decline + multi-source compare-and-contrast across manufacturers e.g. Dow FilmTec vs Hydranautics vs LG Chem vs Toray RO membranes). Scenario C = burst-load oracle (8 emails fired at P=8 concurrency, mix 4 in-scope / 2 out-of-scope / 2 compare; aggregate Logfire + CLI verdicts only -- no per-message grading). Outbound workflow stays active across B and C → verifies concurrent multi-account, multi-workflow operation under sustained load. All three scenarios mandatory. Use whenever user says "smoke test", "run end-to-end", "verify the system works", or after non-trivial changes to sync, routing, agent execution, KB grounding, or Pub/Sub code -- even without explicit invocation.
model: sonnet
---

# Smoke Test

## What this tests

Two scenarios share one Phase 0 setup and one `mailpilot run` loop. Outbound workflow from A stays active through B → exercises real concurrent multi-workflow, multi-account operation. Agent-to-agent reply loop is prevented by two structural properties, not by isolation:

- Distinct subjects per scenario, so each Gmail thread is owned by exactly one workflow type. A's thread → `thread_match` → outbound workflow. B's fresh threads → classification → demo's inbound workflow.
- Enrollments terminate with `record_enrollment_outcome`, so the agent stops replying once a scenario reaches its outcome.

| Scenario | Active workflows                    | Trigger                                  | Verifies                                                                                                        |
| -------- | ----------------------------------- | ---------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| A        | Outbound only                       | `mailpilot enrollment run`               | Outbound agent send → Gmail delivery → manual operator reply → thread_match routing → agent processes reply     |
| B        | Outbound (terminal) + Demo (active) | `mailpilot email send` (operator-driven) | The lab5.ca/mailpilot/ promise -- KB-grounded reply within 90s for an in-scope question, polite decline for out-of-scope, AND a multi-source compare-and-contrast across manufacturer datasheets |
| C        | Outbound (terminal) + Demo (active) | `mailpilot email send` x8 @ P=8          | Burst-load oracle -- 8 emails (4 in-scope / 2 out-of-scope / 2 compare) fired at P=8 concurrency; aggregate Logfire SLA + CLI state verdicts; no per-message grading |

All three scenarios are **mandatory**. `make clean` runs **once**, at the very start. Scenario B IS the lab5.ca/mailpilot/ system under test -- it must run. Scenario C is the load oracle for that same system under sustained burst.

## Conventions

- **Unique subject per scenario, freshly randomized.** Format: `[ST-<HHMMSS>] <topic>`. Generate the topic via Bash on every run -- do not invent it in your head, do not reuse topics from prior runs, do not copy any topic shown in this skill. LLMs anchor on examples and have been observed reusing the same topic across runs, which collides traces and defeats the unique-subject point. Generator:

  ```bash
  TOPIC_A=$(sort -R /usr/share/dict/words 2>/dev/null \
    | grep -E '^[A-Za-z]{4,9}$' | head -2 | tr '\n' ' ' | sed 's/ *$//')
  SUBJECT_A="[ST-$(date +%H%M%S)] ${TOPIC_A}"
  ```

  Scenario B sends five trigger emails. Generate `SUBJECT_B1` (in-scope), `SUBJECT_B2` (out-of-scope), `SUBJECT_B3` (compare-and-contrast), `SUBJECT_B75a` and `SUBJECT_B75b` (concurrent in-scope pair, gate B7.5) independently the same way. Verify all six (`SUBJECT_A`, `SUBJECT_B1`, `SUBJECT_B2`, `SUBJECT_B3`, `SUBJECT_B75a`, `SUBJECT_B75b`) are distinct before continuing. Scenario C generates 8 subjects in bulk inside C1 using the format `[ST-<HHMMSS>-<i>] <topic>` -- the `-<i>` index suffix means C subjects cannot collide with A/B subjects (which use `[ST-<HHMMSS>]` without the suffix); see C1. If `/usr/share/dict/words` is unavailable, fall back to `head -c 12 /dev/urandom | base32 | tr -d '=' | head -c 10`.

- **Test start ISO timestamp.** Capture before each scenario; reuse for `--since` filters and Logfire windows.
- **Polling.** When waiting for sync, routing, or agent results: poll up to 12 attempts, 5s apart (~60s total). Do not call `mailpilot account sync` directly -- the background `mailpilot run` loop owns sync.
- **CLI parsing.** All commands use `uv run mailpilot`. Parse JSON output of every command, extract IDs for the next step. Do not capture into a shell variable and re-emit with `echo "$VAR" | python3 -c ...` -- zsh's built-in `echo` interprets backslash escapes in the JSON (e.g. converts the literal two-char `\n` inside `body_text` into a real newline) and the resulting stream is no longer valid JSON. Either pipe `mailpilot ... | python3 -c ...` directly, or use `printf '%s' "$VAR"`.
- **Envelope shape (SPEC §V.4).** `<entity> view`/`create`/`update` returns `{"<singular>": {...}, "ok": true}`; `<entity> list`/`search` returns `{"<plural>": [...], "ok": true}`. Always extract through the wrap: `json.load(sys.stdin)["email"]["workflow_id"]`, not `json.load(sys.stdin)["workflow_id"]`. Operational commands (`enrollment run`, `tag remove`, `enrollment remove`, `*_export`/`*_import`, `config get/set`, `status`) keep their bespoke shapes. `account sync` returns `{"accounts": [...], "ok": true}` per §V.4 plural envelope.
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
- `qa.py check --id ID --reply-text "<body>" | --reply-file PATH` -- **out-of-scope only** post-§V.57. Validates a decline reply against `forbidden_token_pairs` and `decline_signals`. Exit 0 = pass, 1 = fail, 2 = caller passed a non-outscope id (in-scope grading is operator-judged in gate B4; compare grading is operator-judged in gate B7). JSON on stdout lists fabrications / decline-signal absence.
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

Add the notes (XOR per §V.13 -- one of `--contact-id` / `--company-id`, never both):

```
mailpilot note add --contact-id <INBOUND_CONTACT_ID> \
  --body "Reference: $CONTACT_NOTE_TOKEN. Inbound is VP of Lab Operations; their procurement workflow requires this contact-specific tracking code in every outbound email."

mailpilot note add --company-id <COMPANY_ID> \
  --body "Reference: $COMPANY_NOTE_TOKEN. Lab5 standardizes account-level correlation codes; this code MUST appear in customer correspondence per their procurement policy."
```

**Gate A1a:**

- `mailpilot note list --contact-id <INBOUND_CONTACT_ID>` returns 1 note; its full body (via `mailpilot note view <id>`) contains `Reference: $CONTACT_NOTE_TOKEN`.
- `mailpilot note list --company-id <COMPANY_ID>` returns 1 note; its body contains `Reference: $COMPANY_NOTE_TOKEN`.
- `mailpilot activity list --contact-id <INBOUND_CONTACT_ID> --since <TEST_START_A>` shows 1 `note_added` row (the contact-side note; per §V.17 it carries both `contact_id` and `company_id`).
- `mailpilot activity list --company-id <COMPANY_ID> --since <TEST_START_A>` shows 2 `note_added` rows (contact-side note via multi-target + company-side note).

**Carries forward to:** A3 (body must contain both tokens; tool sequence must include both reads), A8 (note_added activity expectations).

**Prerequisite (separate code change).** This step assumes the agent tools `read_contact` and `read_company` inline recent notes in their return shape (operator choice 2026-05-15 -- "Inline notes in read_contact/read_company"). That tool-surface change is a §V invariant edit and must land via `/sdd:spec` → `/sdd:build` before A3's personalization gate can pass; until it does, the agent has no way to see the tokens and A3's body-token assertion will fail. If the agent surface has not yet shipped, run A1a anyway (it exercises the CLI + activity wiring), and expect the A3 body-token gate to fail -- record as a Critical Bug for tracking, not a regression.

### A1. Import the outbound workflow

The workflow definition is declarative -- it lives in `tests/fixtures/workflows-outbound.json` and is round-tripped via `workflow import` (SPEC §V.63). The fixture contains `${TOPIC_A}` and `${SUBJECT_A}` placeholders that resolve from the shell variables set per the Conventions section; substitute them with `envsubst` before piping the payload to `workflow import` on stdin:

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

Start `mailpilot run` in the background via `Bash` with `run_in_background: true`. Capture the bash_id so you can read its output later. The loop runs **once for the whole test** -- it stays up through B and C and is only stopped at the very end (C7).

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

`mailpilot enrollment run` MUST be invoked exactly once per `(workflow_id, contact_id)`. If the outbound email is not visible in the next gate's `email list` poll, keep polling — do NOT re-invoke `enrollment run`. A second invocation against the same enrollment produces a redundant `agent.invoke` (the agent searches for the prior send and noops correctly, but burns an LLM round-trip and inflates the trace). See SPEC §V.27 / §T.18 / §B.2.

**Gate A3:**

- `enrollment run` output: `"status": "completed"` and `"tool_calls" >= 3` (`read_contact` + `read_company` + `send_email` at minimum).
- `mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction outbound` shows the outbound email with `subject == SUBJECT_A`.
- The email's `body_text` contains `|` (table) and either `**` or `#` (Markdown).
- **Personalization gate (A1a payoff).** The email's `body_text` contains BOTH `$CONTACT_NOTE_TOKEN` AND `$COMPANY_NOTE_TOKEN` verbatim. Either missing → either the agent skipped a `read_*` call or it ignored the note content; treat as a Bug (missing tool call = prompt-fidelity regression; tool call made but token missing = personalization regression). If A1a's prerequisite tool-surface change has NOT shipped (notes not inlined in `read_contact` / `read_company` returns), this gate WILL fail -- record as a Critical Bug to drive the fix, do not skip.
- `mailpilot enrollment list --workflow-id <OUTBOUND_WORKFLOW_ID>` shows enrollment status `active`. Per SPEC §V.15, `enrollment.status` is operational only (`active` or `paused`); the agent never mutates it directly. The send-completion outcome lives in the activity timeline (verified in A8), not on the enrollment row.

Save `OUTBOUND_EMAIL_ID`.

**On failure:** Stop. `mailpilot task list --workflow-id <OUTBOUND_WORKFLOW_ID>` for task details. Common cause: missing `anthropic_api_key`.

### A4. Wait for Gmail delivery to the inbound mailbox

Poll the inbound account:

```
mailpilot email list --account-id <INBOUND_ACCOUNT_ID> --direction inbound --since <TEST_START_A>
```

Match by `SUBJECT_A`. The `email list` row already projects `gmail_thread_id` (§V.7(+) / §T.65), so the thread-presence check below reads from the list directly -- no per-row `email view` round-trip needed.

**Gate A4:**

- The email exists in the inbound account's inbound emails.
- `is_routed == true`.
- `workflow_id == null` (no inbound workflow exists yet -- the `routing.route_email` span emits `route_method=skipped_no_workflows`).
- `gmail_thread_id` is set on the list row (read directly from `email list` JSON; do not round-trip through `email view`). Save the inbound-side email ID as `INBOUND_SIDE_EMAIL_ID` for the reply.

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
- `mailpilot enrollment list --workflow-id <OUTBOUND_WORKFLOW_ID>` still shows status `active` -- by design (SPEC §V.15, `enrollment.status` is operational only). The terminal outcome is recorded as an `enrollment_completed` or `enrollment_failed` activity row, verified in A8.
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
- 1 `note_added` row from A1a's contact-side `note add` (the row carries `contact_id == INBOUND_CONTACT_ID` and `company_id == COMPANY_ID` per §V.17 multi-target; the company-side `note add` does NOT appear here because it has no `contact_id`).
- No `tag_added` rows from this scenario (we did not run `tag add`).

Also assert company-side timeline:

```
mailpilot activity list --company-id <COMPANY_ID> --since <TEST_START_A>
```

Must contain 2 `note_added` rows -- the contact-side note (via multi-target) and the company-side note.

If any expected type is missing, the runtime activity wiring regressed for that path.

### Logfire review for Scenario A

Do this review now, before B, so the window cleanly bounds A's spans. Use `/logfire:debug` with project=`mailpilot` and window `[TEST_START_A, now]`. Spans to verify:

- `agent.invoke` -- count by `trigger` attribute, not by total. Per SPEC §V.27 / §T.18, the span carries an explicit `trigger` label set by the caller path:
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

**Hypothesis:** The lab5.ca/mailpilot/ system delivers on its public promise -- "a professional response grounded in real data" within ~90 seconds for in-scope questions, a polite explanatory reply (no fabricated specs) for questions outside the KB, and a structurally-sound multi-source synthesis when the question forces the agent to compare specs across vendor datasheets. With the outbound workflow from A still active, the demo workflow on `inbound@lab5.ca` correctly classifies each operator-sent question on a fresh thread, the agent grounds its answer in the real Drive KB via `list_drive_markdown` + `read_drive_markdown`, issues one `read_drive_markdown` per source document the compare question targets, and the reply round-trips to the outbound mailbox.

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

The workflow definition is declarative -- the operator-style instructions citing the real folder ID live in `tests/fixtures/workflows-inbound.json` and are round-tripped via `workflow import` (SPEC §V.63). The agent's behaviour comes from that prompt -- changing the wording changes what we test, so edit the fixture, do not type a different command.

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

### B4. Wait for the demo agent to reply (90-second SLA)

Critical gate. The lab5.ca/mailpilot/ page promises delivery within ~90 seconds. Per SPEC `§V.61`, the latency verdict is derived from the agent.invoke span in Logfire, not from CLI poll cadence; the CLI poll is a `did-round-trip?` side-effect check only and uses the wider 120s cap (24 attempts × 5s) so a borderline run does not false-fail on the CLI loop alone.

Poll the outbound mailbox (round-trip check only, cap 120s):

```
mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction inbound --since <TEST_START_B>
```

Match by `SUBJECT_B1` (likely with `Re:` prefix). Note the reply's arrival as confirmation of round-trip; do not derive latency from this observation.

**Gate B4 (the demo promise):**

- Reply present, threaded under `SUBJECT_B1` (CLI round-trip check; cap 120s).
- **Latency verdict via Logfire per §V.61(+) (two-budget split).** Window `[T_SEND_B1, now]`, scope to the demo workflow:

  ```sql
  SELECT end_timestamp,
         EXTRACT(EPOCH FROM (end_timestamp - start_timestamp)) AS sla_agent_seconds,
         EXTRACT(EPOCH FROM (start_timestamp - TIMESTAMPTZ '<T_SEND_B1>')) AS sla_delivery_seconds,
         EXTRACT(EPOCH FROM (end_timestamp - TIMESTAMPTZ '<T_SEND_B1>')) AS total_latency_s
  FROM records
  WHERE deployment_environment = 'development'
    AND span_name = 'agent.invoke'
    AND start_timestamp >= '<T_SEND_B1>'
    AND attributes->>'workflow_id' = '<DEMO_WORKFLOW_ID>'
    AND attributes->>'trigger' = 'task'
  ORDER BY start_timestamp
  LIMIT 1
  ```

  Capture `LATENCY_B1 = total_latency_s`, `SLA_AGENT_B1 = sla_agent_seconds`, `SLA_DELIVERY_B1 = sla_delivery_seconds` for the §1 report.

  **Steady-state primary verdict (§V.61(+)):** `sla_agent_seconds > 50` is an our-side regression of agent execution -- record as a Critical Bug. `sla_delivery_seconds` is advisory: Gmail-side delivery lag is jointly uncontrolled, so a single-send breach is reportable but not gating.

  Zero rows in the window means the demo workflow never fired -- separate Critical Bug (run-loop / Pub/Sub regression).
- Reply on the demo side (`mailpilot email list --account-id <INBOUND_ACCOUNT_ID> --direction outbound --since <TEST_START_B>`) → `is_routed == true`, `workflow_id == DEMO_WORKFLOW_ID`, `route_method == classified`. The classifier ran -- not `thread_match`, since this is a fresh thread.
- Reply body **grounded in the KB** -- operator-judged per SPEC §V.57. Substring match against curated `expected_tokens` was retired (false negatives on phrasing variation like `0.48 mm` vs `0.48mm`); the operator now grades the reply against the live source doc. Procedure:
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
       "source_file_alts": [],
       "answers_question": true,
       "every_factual_claim_supported_by_source": true,
       "cites_source_file": true,
       "unsupported_claims": [],
       "verdict": "pass"
     }
     ```

     Each unsupported factual claim in the reply MUST appear verbatim in `unsupported_claims` (structural defence against LLM-judge sycophancy -- the field forces the grader to enumerate concrete misses rather than hand-wave a passing rating). `source_file_alts` is the verbatim list from the pair (default `[]`); per amended §V.57, `cites_source_file == true` iff the agent's citation is in `{source_file} ∪ source_file_alts` (set union -- admits cross-source identifier collisions like model `WS36-600-2` appearing in two divergent datasheets per §B.40). `verdict` MUST be `"pass"` if and only if all three booleans are true AND `unsupported_claims` is empty; otherwise `"fail"`.

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
- The `reply_email` span returned no `error` key. A return with `error == "format"` means the spec-table lint (§V.42) rejected the body -- the agent rendered specs as space-aligned text instead of a Markdown pipe-table. Record as a prompt-fidelity Bug for B4.

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

Save `TRIGGER_EMAIL_ID_B2`, capture `T_SEND_B2`, poll the outbound mailbox for `SUBJECT_B2` the same way as B4 (round-trip check only, cap 120s — latency verdict is Logfire-derived per §V.61). Carry `QA_ID_B2` forward to the gate.

**Gate B6 (polite decline, no fabrication):**

Out-of-scope decline keeps the script verifier (per SPEC §V.57): regex appropriately fits shape detection (vendor name near a digit-shaped fabrication, decline-phrase presence) and the surface area is small. The operator-judged path applies only to in-scope grounding (B4).

- Reply present (CLI round-trip check; cap 120s).
- **Latency verdict via Logfire per §V.61(+).** Same query shape as B4, swap `<T_SEND_B1>` for `<T_SEND_B2>`. Capture `LATENCY_B2`, `SLA_AGENT_B2`, `SLA_DELIVERY_B2`. `sla_agent_seconds > 50` is a Critical Bug; `sla_delivery_seconds` is advisory.
- Reply body validated by the QA verifier:

  ```
  python3 .claude/skills/smoke-test/scripts/qa.py check \
    --id "$QA_ID_B2" \
    --reply-text "$(mailpilot email view <REPLY_EMAIL_ID> | python3 -c 'import json,sys; print(json.load(sys.stdin)["email"]["body_text"])')"
  ```

  Exit 0 = pass. Exit 1 = fabrication regression OR missing decline-signal language. Exit 2 = caller passed an in-scope id by mistake (use B4's operator-judged flow instead). The JSON output names which `forbidden_token_pairs` matched (vendor name within 60 chars of a digit -- the fabrication signature) and whether at least one `decline_signals` phrase was found.

- The `agent.invoke` for B6 still shows a KB-consulting tool call followed by `reply_email`. `search_drive_markdown` (returning `[]`) is the expected path -- the agent searches with terms from the question, gets no hits, and declines. `list_drive_markdown` is also acceptable for this decline path. Missing both means the agent declined without consulting the KB -- it might have got lucky on this question, but the prompt contract was not honoured. Record as a Bug.

### B6.5. Concurrent-fanout race regression check

This is a Logfire-only gate that piggybacks on B7's compare invocation (the only Scenario B turn that emits multiple `read_drive_markdown` calls). It exists to catch §B.34 -- the `httplib2.Http` thread-safety race where one parallel `read_drive_markdown` returned in ~1s while its sibling hung 60.83s at the socket timeout, killing the agent run. The structural fix is `sequential=True` on every Drive `Tool(...)` registration in `src/mailpilot/agent/templates.py` (§V.38); this gate verifies the dispatcher actually serializes parallel emissions in production.

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
- **No span duration >=60s.** A `read_drive_markdown` span at or past the 60s `_DRIVE_HTTP_TIMEOUT_SECONDS` cap is the §B.34 hang signature; it means a sibling read raced the shared `httplib2.Http` and stalled at the socket timeout. Record as a Critical Bug -- the race fix has regressed and the next failure will burn the §V.49 retry budget.
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

Poll the outbound mailbox for `SUBJECT_B3` (likely `Re:` prefixed) the same way as B4 (round-trip check only, cap 120s — latency verdict is Logfire-derived per §V.61).

**Gate B7 (multi-source grounding, no single-source synthesis):**

- Reply present (CLI round-trip check; cap 120s). Compare questions force a longer agent loop (>=2 `read_drive_markdown` calls) and 2-datasheet synthesis, so the compare-type steady ceiling is 90s (vs 50s for single-source B4 and decline B6) per §V.61(+).
- **Latency verdict via Logfire per §V.61(+).** Same query shape as B4, swap `<T_SEND_B1>` for `<T_SEND_B3>`. Capture `LATENCY_B3`, `SLA_AGENT_B3`, `SLA_DELIVERY_B3`. `sla_agent_seconds > 90` is a Critical regression of agent execution for compare-type; `50 < sla_agent_seconds <= 90` is advisory (compare-type intrinsic cost band per §B.61); `sla_delivery_seconds` is advisory.
- Reply on the demo side has `is_routed == true`, `workflow_id == DEMO_WORKFLOW_ID`, `route_method == classified`. Fresh thread, so not `thread_match`.
- Reply body **grounded in EVERY source listed in `source_files`** -- operator-judged per the same §V.57 contract that governs B4. Procedure:
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
  3. `reply_email` (no `error` key in the tool return; format errors mean the spec-table lint rejected the body -- record as a §V.42 prompt-fidelity Bug).
  4. `record_enrollment_outcome` (`outcome=completed`).

  Fewer `read_drive_markdown` calls than `EXPECTED_READ_COUNT` is the headline regression this gate catches -- the agent guessed at one of the products' specs instead of reading the doc. Record as a Bug even if the reply happens to be factually correct. More reads than expected (e.g., the agent followed a distractor) is acceptable as long as the four required calls above are present.

### B7.5. Concurrent in-scope dual-send (multi-request / multi-tool-call stress)

Two distinct in-scope questions arrive on the demo workflow at nearly the same wall-clock instant, on two fresh threads. Each must trigger its own `agent.invoke`, each must ground in its own KB source, and both replies must meet the steady-state `sla_agent_seconds <= 50` budget per §V.61(+). Catches three failure classes the single-send tests (B3, B6, B7) cannot:

- Classifier serializes when it should parallelize (one classification blocking the next inbound).
- Two concurrent agent invocations share mutable state (e.g., one workflow row updated mid-flight by the other, or a shared httplib2 transport reused across agent.invoke boundaries).
- Drive `Tool(... sequential=True)` (§V.38) inadvertently serializes across agent invocations, not just within one -- defeating the stress test at the dispatcher layer.

Pick two distinct in-scope pairs whose `source_file` differs (avoid same-doc collisions so groundedness is independently judgeable):

```bash
QA_B75a=$(python3 .claude/skills/smoke-test/scripts/qa.py pick --type inscope)
QA_ID_B75a=$(printf '%s' "$QA_B75a" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
QUESTION_B75a=$(printf '%s' "$QA_B75a" | python3 -c 'import json,sys; print(json.load(sys.stdin)["question"])')
SOURCE_FILE_B75a=$(printf '%s' "$QA_B75a" | python3 -c 'import json,sys; print(json.load(sys.stdin)["source_file"])')

while :; do
  QA_B75b=$(python3 .claude/skills/smoke-test/scripts/qa.py pick --type inscope)
  SOURCE_FILE_B75b=$(printf '%s' "$QA_B75b" | python3 -c 'import json,sys; print(json.load(sys.stdin)["source_file"])')
  [ "$SOURCE_FILE_B75b" != "$SOURCE_FILE_B75a" ] && break
done
QA_ID_B75b=$(printf '%s' "$QA_B75b" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
QUESTION_B75b=$(printf '%s' "$QA_B75b" | python3 -c 'import json,sys; print(json.load(sys.stdin)["question"])')
```

Generate two more distinct subjects per the Conventions section and verify all six (`SUBJECT_A`, `SUBJECT_B1`, `SUBJECT_B2`, `SUBJECT_B3`, `SUBJECT_B75a`, `SUBJECT_B75b`) are distinct before continuing.

Fire both sends in parallel via Bash background jobs so they hit Gmail within milliseconds of each other:

```bash
T_SEND_B75=$(date -u +%Y-%m-%dT%H:%M:%SZ)

mailpilot email send \
  --account-id <OUTBOUND_ACCOUNT_ID> \
  --to inbound@lab5.ca \
  --subject "$SUBJECT_B75a" \
  --body "$QUESTION_B75a" &
PID_B75a=$!

mailpilot email send \
  --account-id <OUTBOUND_ACCOUNT_ID> \
  --to inbound@lab5.ca \
  --subject "$SUBJECT_B75b" \
  --body "$QUESTION_B75b" &
PID_B75b=$!

wait $PID_B75a $PID_B75b
```

Poll the outbound mailbox for BOTH replies (in either order), capping each at 120s wall-clock from `T_SEND_B75` (round-trip check only; latency verdicts are Logfire-derived per §V.61). Capture `REPLY_EMAIL_ID_B75a` / `REPLY_EMAIL_ID_B75b` from the matches.

**Gate B7.5 (multi-request, multi-tool concurrency):**

- BOTH replies present (CLI round-trip check; cap 120s each). Either missing means the system stalled one trigger while serving the other -- record as a Critical Bug (regression of the lab5.ca/mailpilot/ SLA under concurrent load).
- **Per-span latency verdict via Logfire per §V.61.** Query the two agent.invoke spans by `email_id` (post §T.63; each span carries its own inbound trigger's id). Window `[T_SEND_B75, now]`, scope to the demo workflow:

  ```sql
  SELECT attributes->>'email_id' AS email_id,
         end_timestamp,
         EXTRACT(EPOCH FROM (end_timestamp - start_timestamp)) AS sla_agent_seconds,
         EXTRACT(EPOCH FROM (start_timestamp - TIMESTAMPTZ '<T_SEND_B75>')) AS sla_delivery_seconds,
         EXTRACT(EPOCH FROM (end_timestamp - TIMESTAMPTZ '<T_SEND_B75>')) AS total_latency_s
  FROM records
  WHERE deployment_environment = 'development'
    AND span_name = 'agent.invoke'
    AND start_timestamp >= '<T_SEND_B75>'
    AND attributes->>'workflow_id' = '<DEMO_WORKFLOW_ID>'
    AND attributes->>'trigger' = 'task'
  ORDER BY start_timestamp
  LIMIT 5
  ```

  Expect exactly 2 rows, distinct `email_id` values. Capture `LATENCY_B75a` / `LATENCY_B75b` (= `total_latency_s`) plus `SLA_AGENT_B75a` / `SLA_AGENT_B75b` and the matching `sla_delivery_seconds` values. **Either `sla_agent_seconds > 50` is a Critical SLA breach per §V.61(+) steady-state.** `sla_delivery_seconds` is advisory. Fewer than 2 rows means the drain-layer concurrent worker pool (§V.23) regressed and one trigger was dropped or merged.
- BOTH replies routed on the demo side with `workflow_id == DEMO_WORKFLOW_ID` and `route_method == classified` (each is a fresh thread). Either routed differently means the classifier serialized or misfired under load.
- BOTH replies grounded in their own `source_file` per §V.57, operator-judged with the same verdict-JSON schema as B4. Run `qa.py source --id "$QA_ID_B75a"` and `qa.py source --id "$QA_ID_B75b"` separately, then grade each reply against its own source. A cross-grounded reply (B75a's body cites B75b's source, or vice versa) is a state-leak Bug -- shared mutable state across concurrent agent invocations.
- Logfire window `[T_SEND_B75, T_SEND_B75 + 90s]`, scope to `workflow_id == DEMO_WORKFLOW_ID`:
  - Exactly **2** `agent.invoke` spans, each with `workflow_id` matching `DEMO_WORKFLOW_ID`, `trigger='task'`, and a populated `email_id` attribute (per §V.26(+) / §T.63). Attribute B75a vs B75b by the inbound-side `email_id` recorded on each span — distinct values disambiguate the pair without `agent_reasoning` substring inspection.
  - The two `agent.invoke` spans' wall-clock intervals MUST overlap (`start(later) < end(earlier)`). Strictly sequential execution defeats the stress test -- record as a Bug ("agent invocations serialized; expected concurrent") and investigate the run-loop / task-drain path. If a recent pydantic-ai or sync-loop change serialized at this layer, pin the working version while investigating.
  - Each `agent.invoke` carries its own `search_drive_markdown` + `read_drive_markdown` + `reply_email` + `record_enrollment_outcome` chain. Tool spans across the two invocations MAY overlap (different threads, different `DriveClient` instances per `read_drive_markdown` call); a 60s+ Drive tool span anywhere in the window is the §B.34 race signature.
  - Zero `is_exception=true` and zero `level=warn` spans on either invocation. A `drive_unavailable` tool return surfaced in the tool's structured response is acceptable (the broadened catch envelope from §T.60 step (b)); an unhandled exception escaping the tool wrapper is not.

### B8. Verify the CRM activity timeline

```
mailpilot activity list --contact-id <OUTBOUND_CONTACT_ID> --since <TEST_START_B>
```

**Gate B8 (activity wiring):** activity types follow the `enrollment_*` / `email_*` vocabulary enforced by `activity.type` CHECK constraint in `src/mailpilot/schema.sql`.

- `enrollment_added` with `workflow_id == DEMO_WORKFLOW_ID` on the activity row itself (FK column, not `detail` JSONB). From B1.
- 5 `email_received` activities -- the demo mailbox received the trigger emails for B3 (in-scope), B6 (out-of-scope), B7 (compare), and the two B7.5 concurrent in-scope sends (B75a, B75b).
- 5 `email_sent` activities from the agent replies (subjects begin with `Re:`).
- 5 `enrollment_completed` activities (one per question, all emitted by `record_enrollment_outcome`).

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

Expect exactly 5 rows (the B3, B6, B7, B75a, and B75b triggers), each with `workflow_id == null`. Any deviation is a separate signal -- either an unexpected outbound send (record as a Bug) or a missing trigger (re-run the missing step).

### Logfire review for Scenario B

Window `[TEST_START_B, now]`. Spans to verify:

- `agent.invoke` -- exactly **5** invocations (B4 in-scope, B6 out-of-scope, B7 compare, B75a, B75b). More than 5 → demo agent re-fired or outbound workflow reacted to B's traffic. The B75a / B75b spans MUST have overlapping wall-clock intervals (concurrent execution) -- strict serialization at the agent layer fails gate B7.5.
- `routing.route_email` -- all five demo-side trigger emails → `route_method=classified`. Outbound-side replies → `route_method=skipped_no_inbound_workflows`.
- `agent.classify_email` -- 5 invocations. All five `result` values match `DEMO_WORKFLOW_ID`.
- `running tool` per invocation:
  - B4 (in-scope): `search_drive_markdown` + `read_drive_markdown` + `reply_email` + `record_enrollment_outcome`.
  - B6 (out-of-scope decline): `search_drive_markdown` (returning `[]`) + `reply_email` + `record_enrollment_outcome` (`read_drive_markdown` not required since no document matches).
  - B7 (compare-and-contrast): `search_drive_markdown` >=1 + `read_drive_markdown` exactly `EXPECTED_READ_COUNT` times (one per file in the pair's `source_files`) + `reply_email` + `record_enrollment_outcome`. Fewer reads than `EXPECTED_READ_COUNT` is the headline regression: agent guessed a doc instead of reading it. The set of `file_id` arguments to `read_drive_markdown` must equal the set Drive returned for the `source_files` names -- mismatch means the agent read the wrong doc.
  - B75a / B75b (concurrent in-scope): each carries `search_drive_markdown` + `read_drive_markdown` + `reply_email` + `record_enrollment_outcome`. The two `read_drive_markdown` spans (one per invocation) MAY overlap (different threads against different `DriveClient` instances). Any single Drive span at the 60s ceiling is the §B.34 race signature.
  - At least one KB-consulting tool call (`search_drive_markdown` or `list_drive_markdown`) is mandatory in all five. With ≥30 docs in the folder, `list_drive_markdown` instead of `search_drive_markdown` on B4 / B7 / B75a / B75b is a regression. On B6 (decline), `list_drive_markdown` is acceptable.
- `agent.invoke` (B7) `input_tokens` -- should be noticeably higher than B4 because the compare invocation pulls 2-4 full datasheets into the agent's context. A B7 token count at or below B4's signals the agent skipped one or more reads -- cross-check against the read-count gate.
- Any `is_exception=true` or `level=warn` spans -- record. Drive 4xx/5xx surfacing as `drive_unavailable` from the tool is acceptable in the agent's tool-return ledger but should not be `is_exception=true` on the span.

---

## Transition to Scenario C

Do not stop the sync loop. Do not run `make clean`. Do not recreate accounts, contacts, or workflows. Scenario C reuses the same demo workflow (`DEMO_WORKFLOW_ID`) and outbound contact (`OUTBOUND_CONTACT_ID`) from B; the outbound workflow from A remains active and must stay quiet through C as well. Scenario C is the load oracle for the lab5.ca/mailpilot/ system that B verified at single-send fidelity -- B trusts content quality, C trusts sustained-burst structural health.

---

## Scenario C: Burst-load oracle

**Hypothesis:** The demo workflow handles a sustained burst (8 emails fired at P=8 concurrency, mixing the same three classifier branches Scenario B exercises one-at-a-time) without dropping triggers, serializing classification, leaking state across invocations, or breaching the lab5.ca/mailpilot/ promise on the bulk of replies. This is the **load oracle**; B3-B7.5 remain the **correctness oracle** -- per-message groundedness is graded there, and C trusts those gates for content quality. C's verdicts are aggregate Logfire + CLI state only; there is no per-message body inspection.

**Concurrency.** P=8 -- high enough to interleave classification + `agent.invoke` under real concurrency, low enough that Gmail per-user send rate stays comfortably under quota. Do **not** raise P without first confirming Gmail rate limits for the impersonated user; tripping a Gmail-side 429 invalidates the run, not the system under test.

**Two-budget SLA per §V.61(+).** Primary verdict is `sla_agent_seconds` (our-side agent execution); `sla_delivery_seconds` (Gmail Pub/Sub notify dwell + classification-pipeline lag) is advisory because it is jointly uncontrolled by Gmail-side batching. Steady-state single-source / decline sends (B4 / B6 / B7.5) gate `sla_agent_seconds <= 50s`; compare-type sends (B7) gate `sla_agent_seconds <= 90s` (compare-type 2-datasheet synthesis structurally exceeds the 50s single-source band per §B.61). Burst C gates non-compare `p95(sla_agent_seconds) <= 75s`; compare-type invocations are reported separately with an advisory ceiling of 120s (compare cost dominates the tail and would flap a blended p95 per §B.62). The public lab5.ca/mailpilot/ 90s promise remains the demo-test end-to-end gate (`/demo-test` G1), not a smoke-test gate.

Capture `TEST_START_C` (ISO, must be later than B's last activity).

### C1. Generate burst payload (subjects + Q/A pair ids + questions)

Pre-generate the entire burst inputs before any send: arrays of 8 subjects + 8 Q/A pair ids + 8 questions, mixed deterministically 4 in-scope / 2 out-of-scope / 2 compare. The out-of-scope pool has 5 pairs total -- sampling 2 leaves headroom; in-scope and compare pools are large enough that replacement is unlikely to collide.

```bash
TEST_START_C=$(date -u +%Y-%m-%dT%H:%M:%SZ)

SUBJECTS_BURST=()
for i in $(seq 1 8); do
  TOPIC=$(sort -R /usr/share/dict/words 2>/dev/null \
    | grep -E '^[A-Za-z]{4,9}$' | head -2 | tr '\n' ' ' | sed 's/ *$//')
  SUBJECTS_BURST+=("[ST-$(date +%H%M%S)-${i}] ${TOPIC}")
done
[ "$(printf '%s\n' "${SUBJECTS_BURST[@]}" | sort -u | wc -l)" -eq 8 ] \
  || { echo "FAIL: subject collision in burst"; exit 1; }

QA_IDS_BURST=()
for i in $(seq 1 4); do
  QA_IDS_BURST+=("$(python3 .claude/skills/smoke-test/scripts/qa.py pick --type inscope \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')")
done
for i in $(seq 1 2); do
  QA_IDS_BURST+=("$(python3 .claude/skills/smoke-test/scripts/qa.py pick --type outscope \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')")
done
for i in $(seq 1 2); do
  QA_IDS_BURST+=("$(python3 .claude/skills/smoke-test/scripts/qa.py pick --type compare \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')")
done

QUESTIONS_BURST=()
for qa_id in "${QA_IDS_BURST[@]}"; do
  QUESTIONS_BURST+=("$(python3 .claude/skills/smoke-test/scripts/qa.py pick --id "$qa_id" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["question"])')")
done
```

**Gate C1:**

- `SUBJECTS_BURST` has 8 distinct entries (the explicit `sort -u | wc -l` check above must pass).
- `QA_IDS_BURST` has 8 entries: exactly 4 starting `qa-in-`, 2 starting `qa-out-`, 2 starting `qa-cmp-`. A different split means `qa.py pick --type <T>` returned the wrong type -- record as a Bug and stop (the mix is what makes C's classifier-under-load verdict meaningful).
- `QUESTIONS_BURST` has 8 entries, none empty.
- The `[ST-<HHMMSS>-<i>] <topic>` subject format trivially excludes collisions with A/B subjects (which use `[ST-<HHMMSS>]` without the `-<i>` suffix).

### C2. Fire 8 sends at P=8

Bounded-concurrency loop using bash job control. Capture `T_SEND_C` as a single wall-clock anchor immediately before the loop -- all 8 sends complete within ~5-15s, so a single anchor is precise enough for the per-span latency derivation in C4.

```bash
T_SEND_C=$(date -u +%Y-%m-%dT%H:%M:%SZ)
for i in $(seq 0 7); do
  while [ "$(jobs -r | wc -l)" -ge 8 ]; do sleep 0.1; done
  mailpilot email send \
    --account-id <OUTBOUND_ACCOUNT_ID> \
    --to inbound@lab5.ca \
    --subject "${SUBJECTS_BURST[$i]}" \
    --body "${QUESTIONS_BURST[$i]}" >/dev/null &
done
wait
```

**Gate C2:**

- `wait` returns 0 (all 8 background sends exited cleanly). A non-zero return means one or more Gmail-side or CLI-side failures occurred during the burst -- record which subject(s) failed and stop; the run is invalidated by the failed send(s), not by the system under test.
- `mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction outbound --since $TEST_START_C` returns 8 rows, each with `workflow_id == null` (these are operator-driven outbound, analogous to B3/B6/B7/B75a/B75b). Any deviation here -- extra rows, missing rows, or non-null `workflow_id` -- is a separate Bug.

### C3. Poll for 8 replies (cap 240s)

The lab5.ca/mailpilot/ public SLA is 90s for a single reply. At N=8/P=8 the burst is a single wave, so expected wall-clock to receive all replies is ~one `sla_agent` window plus delivery dwell. 240s is a generous ceiling so the CLI poll does not false-fail; C4 still enforces the strict per-span verdict per reply.

```bash
mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction inbound --since "$TEST_START_C"
```

Match each row's `subject` against `Re: <one of SUBJECTS_BURST>` (Gmail typically preserves with `Re:` prefix; match on the bracket-and-topic substring). Capture the 8 reply ids as `REPLY_IDS_BURST`.

**Gate C3 (wire counts):**

- 8 replies present in the outbound mailbox, each matching a unique entry in `SUBJECTS_BURST` (no duplicates, no missing pairings). Fewer than 8 within 240s is a Critical regression -- the system dropped or queued a trigger.
- `mailpilot email list --account-id <INBOUND_ACCOUNT_ID> --direction inbound --since $TEST_START_C` returns 8 rows, all `is_routed == true`, all `workflow_id == DEMO_WORKFLOW_ID`, all `route_method == classified`. Any `route_method == thread_match` is a Bug -- fresh threads must classify, not match. Any `is_routed == false` row is a Critical routing regression under load.
- `mailpilot email list --account-id <INBOUND_ACCOUNT_ID> --direction outbound --since $TEST_START_C` returns 8 rows (the agent replies).
- `mailpilot task list --workflow-id <DEMO_WORKFLOW_ID>` filtered to the burst window returns 8 tasks, all `status == "completed"`. Any `failed` row -- record with `mailpilot task view <id>` reason and treat as a Bug.

### C4. Logfire aggregate gates

Single window `[T_SEND_C, T_SEND_C + 300s]`, scope to `workflow_id == DEMO_WORKFLOW_ID` and `trigger == 'task'`.

**Gate C4.a -- per-span SLA + token economics (two-budget split per §V.59(+) / §V.61(+); compare-type vs non-compare split per §V.61(+) / §B.62):**

```sql
WITH read_counts AS (
  -- Count read_drive_markdown calls per agent.invoke trace.
  -- compare-type invocation per §V.61(+) ≡ >=2 read_drive_markdown spans in the same trace.
  SELECT trace_id, COUNT(*) AS read_count
  FROM records
  WHERE deployment_environment = 'development'
    AND attributes->>'gen_ai.tool.name' = 'read_drive_markdown'
    AND start_timestamp >= '<T_SEND_C>'
    AND start_timestamp <= '<T_SEND_C>'::timestamptz + INTERVAL '300 seconds'
  GROUP BY trace_id
),
burst AS (
  SELECT
    r.attributes->>'email_id' AS email_id,
    r.start_timestamp,
    r.end_timestamp,
    EXTRACT(EPOCH FROM (r.end_timestamp - r.start_timestamp)) AS sla_agent_seconds,
    EXTRACT(EPOCH FROM (r.start_timestamp - TIMESTAMPTZ '<T_SEND_C>')) AS sla_delivery_seconds,
    EXTRACT(EPOCH FROM (r.end_timestamp - TIMESTAMPTZ '<T_SEND_C>')) AS total_latency_s,
    r.is_exception,
    r.level,
    (r.attributes->>'input_tokens')::int AS in_tok,
    (r.attributes->>'output_tokens')::int AS out_tok,
    (r.attributes->>'cache_read_input_tokens')::int AS cache_read,
    COALESCE(rc.read_count, 0) >= 2 AS is_compare
  FROM records r
  LEFT JOIN read_counts rc ON rc.trace_id = r.trace_id
  WHERE r.deployment_environment = 'development'
    AND r.span_name = 'agent.invoke'
    AND r.start_timestamp >= '<T_SEND_C>'
    AND r.start_timestamp <= '<T_SEND_C>'::timestamptz + INTERVAL '300 seconds'
    AND r.attributes->>'workflow_id' = '<DEMO_WORKFLOW_ID>'
    AND r.attributes->>'trigger' = 'task'
)
SELECT
  COUNT(*) AS n_invokes,
  COUNT(DISTINCT email_id) AS n_distinct_email_ids,
  SUM(CASE WHEN is_compare THEN 1 ELSE 0 END) AS n_compare,
  SUM(CASE WHEN NOT is_compare THEN 1 ELSE 0 END) AS n_noncompare,
  MAX(sla_agent_seconds) FILTER (WHERE NOT is_compare) AS max_sla_agent_noncompare_s,
  approx_percentile_cont(sla_agent_seconds, 0.95) FILTER (WHERE NOT is_compare) AS p95_sla_agent_noncompare_s,
  MAX(sla_agent_seconds) FILTER (WHERE is_compare) AS max_sla_agent_compare_s,
  approx_percentile_cont(sla_agent_seconds, 0.95) FILTER (WHERE is_compare) AS p95_sla_agent_compare_s,
  MAX(sla_delivery_seconds) AS max_sla_delivery_s,
  approx_percentile_cont(sla_delivery_seconds, 0.95) AS p95_sla_delivery_s,
  approx_percentile_cont(sla_delivery_seconds, 0.50) AS p50_sla_delivery_s,
  MAX(total_latency_s) AS max_total_s,
  approx_percentile_cont(total_latency_s, 0.95) AS p95_total_s,
  SUM(CASE WHEN is_exception THEN 1 ELSE 0 END) AS n_exceptions,
  SUM(CASE WHEN level = 'warn' THEN 1 ELSE 0 END) AS n_warns,
  AVG(cache_read::float / NULLIF(in_tok::float, 0)) AS avg_cache_hit_ratio,
  SUM(in_tok) AS total_in_tok,
  SUM(out_tok) AS total_out_tok
FROM burst;
```

Assertions (primary verdict = `sla_agent_seconds` per §V.61(+); `sla_delivery_seconds` is advisory only because Gmail-side Pub/Sub batching is jointly uncontrolled):

- `n_invokes == 8` AND `n_distinct_email_ids == 8` -- no merged or dropped triggers (§V.26 / §T.63 contract: one span per inbound email).
- `n_compare == 2` AND `n_noncompare == 6` -- matches the C1 mix (2 qa-cmp + 4 qa-in + 2 qa-out). Any mismatch means a compare invocation skipped one of its required reads OR a non-compare invocation issued a stray second read; cross-check against C4.c and the B7 tool-use gate before flagging.
- `p95_sla_agent_noncompare_s <= 75` -- burst gate over non-compare invocations per §V.61(+). Matches the §V.23 burst-load formula `ceil(N * avg_invoke_s / sla_s)` sized for the 50s steady single-source ceiling. A breach here is an our-side regression of agent execution under load on single-source / decline traffic.
- `n_exceptions == 0` AND `n_warns == 0`.
- `avg_cache_hit_ratio >= 0.5` -- prompt cache stays warm across the burst (catches cache-key churn regressions where each agent invocation re-pays the full system-prompt token cost; the dominant cost driver at this scale).

Report (NOT gated):

- `p95_sla_agent_compare_s`, `max_sla_agent_compare_s` -- compare-type advisory ceiling 120s per §V.61(+) / §B.62. Compare invocations load 2 datasheets (~60k input tokens vs ~22k single-source) and synthesize across them; the cost is structural, not a regression class. Trend-escalate on run-over-run drift past 120s; do NOT fail the run on a single breach unless the cause is the same root as §B.61 / §B.62 (intrinsic compare cost growing) rather than a prompt-loop regression (§V.71 cap firing).
- `max_sla_delivery_s`, `p95_sla_delivery_s`, `p50_sla_delivery_s` -- Gmail Pub/Sub notify dwell + classification-pipeline lag dominate the tail even at N=8 single-wave (§B.53 / §B.54). Trend-escalate on run-over-run drift but do NOT fail the run on a single breach: our side cannot accelerate Gmail-side notify emission.
- `max_total_s`, `p95_total_s` -- end-to-end (delivery + agent), retained for run-over-run trend continuity with the pre-amend bundled metric.

**Gate C4.b -- concurrency proof (no serialization regression):**

```sql
WITH burst AS ( /* same CTE as C4.a */ )
SELECT COUNT(*) AS overlap_pairs
FROM burst a, burst b
WHERE a.email_id < b.email_id
  AND a.start_timestamp < b.end_timestamp
  AND b.start_timestamp < a.end_timestamp;
```

Assert `overlap_pairs >= 10`. With N=8 sends fired in a single P=8 wave and ~10-40s per `agent.invoke`, max possible overlap pairs is C(8,2)=28 and expected is close to that; this floor is generous enough that only strict serialization (drain-layer pool regression, §V.23 / §V.23(+)) can fail it. A failure here means the dispatcher serialized invocations -- record as a Critical Bug, since it defeats the burst-load oracle entirely.

**Gate C4.c -- Drive race signatures absent (§B.34):**

```sql
SELECT MAX(EXTRACT(EPOCH FROM (end_timestamp - start_timestamp))) AS max_dur_s,
       SUM(CASE WHEN is_exception THEN 1 ELSE 0 END) AS n_exc
FROM records
WHERE deployment_environment = 'development'
  AND attributes->>'gen_ai.tool.name' = 'read_drive_markdown'
  AND start_timestamp >= '<T_SEND_C>'
  AND start_timestamp <= '<T_SEND_C>'::timestamptz + INTERVAL '300 seconds';
```

Assert:

- `max_dur_s < 60` -- §B.34 60s socket-timeout signature absent across the burst's Drive tool calls.
- `n_exc == 0` -- no unhandled exceptions escape the Drive tool wrappers.

A 60s+ `read_drive_markdown` span under burst is a stronger signal than the same span in B6.5 (B7's single-invocation pair): it means the structural `sequential=True` registration (§V.38) regressed across concurrent agent invocations, not just within one. Record as a Critical Bug.

### C5. Activity timeline check (incremental)

```
mailpilot activity list --contact-id <OUTBOUND_CONTACT_ID> --since <TEST_START_C>
```

**Gate C5 (incremental from `TEST_START_C`, not cumulative):**

- 8 `email_received` activities (one per burst trigger landing in the demo mailbox).
- 8 `email_sent` activities (one per agent reply).
- 8 `enrollment_completed` activities (one per `record_enrollment_outcome`).
- 0 `enrollment_failed` -- any failed enrollment in the burst window is a Bug (the agent should reach `completed` even on out-of-scope replies, which terminate via decline).

### C6. Concurrent-workflow quiet check (Scenario A outbound still silent)

The Scenario A outbound workflow remained active through B and continues through C. It must not have reacted to C's burst traffic.

```
mailpilot email list \
  --account-id <OUTBOUND_ACCOUNT_ID> \
  --direction outbound \
  --workflow-id <OUTBOUND_WORKFLOW_ID> \
  --since <TEST_START_C>
```

**Gate C6:** Zero rows. Any non-zero count means the outbound workflow reacted to C's traffic -- record as a Critical Bug. Cross-workflow leak under burst is a substantially worse signal than the same leak under single-send, because it implies the leak scales with traffic.

### C7. Stop the sync loop

End of test. Send SIGTERM to the background `mailpilot run` (e.g. `kill <pid>`). Wait for `Sync loop stopped` in the captured output. Confirm the `sync_status` table is empty. If the process does not exit within 10s, send SIGKILL and record this in the report.

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
  B4  Grounded reply (sla_agent<=50) PASS  (SLA_AGENT_B1=<Ns> / SLA_DELIVERY_B1=<Ns> / total=<Ns>; cited model: <e.g., TW-18.0K-1240>)
  B5  Drive tools used ............. PASS  (search_drive_markdown -> read_drive_markdown -> reply_email -> record_enrollment_outcome)
  B6  Out-of-scope decline ......... PASS  (SLA_AGENT_B2=<Ns> / SLA_DELIVERY_B2=<Ns> / total=<Ns>; no fabricated specs)
  B7  Compare-and-contrast reply ... PASS  (SLA_AGENT_B3=<Ns> / SLA_DELIVERY_B3=<Ns> / total=<Ns>; <N> sources, <N> read_drive_markdown calls; no single-sourced specs)
  B7.5 Concurrent dual in-scope ..... PASS  (SLA_AGENT_B75a=<Ns>/SLA_DELIVERY_B75a=<Ns>, SLA_AGENT_B75b=<Ns>/SLA_DELIVERY_B75b=<Ns>; 2 overlapping agent.invoke spans; both grounded in own source)
  B8  Activity timeline ............ PASS  (5 received / 5 sent / 5 enrollment_completed)
  B9  Outbound stayed quiet ........ PASS  (0 new outbound sends during B)

Scenario C: Burst-load oracle (8 emails @ P=8, outbound workflow still active)
  C1  Burst payload generated ...... PASS  (8 distinct subjects; mix 4 qa-in / 2 qa-out / 2 qa-cmp)
  C2  8 sends @ P=8 ................ PASS  (wait exit 0; 8 outbound rows with workflow_id=null)
  C3  8 replies received ........... PASS  (wall-clock <Ns>; all classified to demo workflow)
  C4  Logfire aggregate gates ...... PASS  (p95_sla_agent_noncompare=<Ns> <=75, 0 exc, 0 warn, cache>=0.5, overlap_pairs=<N> >=10, drive max_dur<60s; advisory: p95_sla_agent_compare=<Ns> (~<=120), p50/p95/max_sla_delivery=<Ns>/<Ns>/<Ns>, p95_total=<Ns>)
  C5  Activity timeline (delta) .... PASS  (+8 received / +8 sent / +8 enrollment_completed)
  C6  Outbound stayed quiet ........ PASS  (0 new outbound-workflow sends during C)
  C7  Stop sync loop ............... PASS

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

"Auto-invoke" = call `/sdd:spec` with the exact `Spec action:` invocation. Run them sequentially so each `## Next` reply token (`ok` / `revise` / `cancel`) applies to a single Bug -- never batch. Record outcome (`filed as §B.7 with §V.22`, `cancelled by operator`, etc.) in the hand-off block. "Print only" means the line goes into the hand-off "Operator review" list, ready for the operator to paste.

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
  - Bug <N> -- <title>: <result, e.g., "filed as §B.7 with §V.22", "cancelled by operator">
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

Expected total: ~10-11 minutes. Phase 0 once, run loop once, no reset between scenarios. The added compare-and-contrast question (B7) costs roughly one extra ~60-90s `sla_agent` window over the prior baseline because it forces 2-4 `read_drive_markdown` calls and a multi-doc synthesis (compare-type steady ceiling is 90s per §V.61(+) / §B.61, vs 50s for single-source). The concurrent dual-send (B7.5) adds another ~50s window for the second of the two parallel replies (the two windows overlap, so the marginal cost is one reply window, not two). Scenario C (burst-load oracle) adds ~1-2 minutes for the 8-send burst plus aggregate verification; per-span `sla_agent` p95 must stay <=75s under burst on non-compare invocations (§V.61(+)), compare-type p95 is reported separately with an advisory 120s ceiling per §B.62, while `sla_delivery` is advisory because Gmail-side Pub/Sub batching dominates the tail.

| Phase / scenario                          | Duration |
| ----------------------------------------- | -------- |
| Phase 0 (once, 2 accounts)                | ~15s     |
| A1 / B1 workflow setup                    | ~5s      |
| A2 start run loop                         | ~5s      |
| A3 outbound agent                         | ~10s     |
| A4 sync + route                           | ~10-60s  |
| A5 / B3 / B6 / B7 operator send           | ~3s each |
| A6 reply round-trip                       | ~10-60s  |
| A7 task drain                             | ~10-60s  |
| B4 in-scope reply (sla_agent<=50s)        | ~10-50s  |
| B6 out-of-scope reply (sla_agent<=50s)    | ~10-50s  |
| B7 compare reply (sla_agent<=90s)         | ~20-90s  |
| B7.5 concurrent dual reply (sla_agent<=50s each, overlapping) | ~20-50s |
| A8 / B8 activity check                    | ~3s      |
| C1 burst payload generation               | ~3s      |
| C2 8 sends @ P=8                          | ~5-15s   |
| C3 poll for 8 replies (240s CLI cap)      | ~60-120s |
| C4 Logfire aggregate gates (4 queries)    | ~10s     |
| C5 / C6 activity + quiet check            | ~5s      |
| C7 stop run loop                          | ~3s      |
| Report                                    | ~10s     |
