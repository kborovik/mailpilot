---
name: mailpilot-campaign-test
description: >-
  Grok-native multi-step smoke test for an outbound cold-email workflow agent.
  Sends live Touch 1 from outbound@lab5.ca to inbound@lab5.ca only, injects
  crafted prospect replies per branch, verifies agent branch outcomes, then
  cleans up. Default workflow is acumatica-var-outbound. Use when the user
  wants to test, smoke-test, dry-run, or validate a campaign / outreach
  workflow before real sends, or runs /mailpilot-campaign-test.
argument-hint: "[--workflow-file <path>] [--company-domain <domain>] [--min-confidence N]"
allowed-tools: run_terminal_command, spawn_subagent, search_tool, use_tool
---

# mailpilot-campaign-test

Test the real outbound workflow agent end-to-end without emailing a real
prospect. Deterministic work lives in the shared Claude skill scripts; this
skill is the Grok orchestrator.

**Scripts / references (do not copy):**
`.claude/skills/mailpilot-campaign-test/scripts/` and
`.claude/skills/mailpilot-campaign-test/references/`.

**Default workflow file:**
`/Users/kb/github/lab5-leads/workflows/acumatica-var-outbound.toml`

Every command runs from the **repo root** via `uv run python` / `uv run mailpilot`.

## Safety (non-negotiable)

- **Sender only:** `outbound@lab5.ca`
- **Prospect only:** `inbound@lab5.ca` (mailbox and contact email)
- Never send to any other address. Never start `mailpilot run`.
- Real contacts are grounding data only; their address is never a recipient.
- Always run cleanup (step 10) before finishing, even on failure.

## Logfire real-time error watch (required when MCP available)

Agent-active steps emit live spans to Logfire (`project: mailpilot`,
environment `development`). **Watch for errors while those steps run** — do not
wait until step 11.

### When

| Phase | After which steps | Why |
|---|---|---|
| Start window | Note UTC start just before step 4 | Touch 1 agent.invoke + gmail.send |
| Mid-run poll | After step 4, after step 5, after step 6 | Catch exceptions / tool errors / 429s early |
| Final digest | Step 11 | Full window write-up → `logfire_report.md` |

If a step runs long (scripts often 1–5+ min), poll Logfire once mid-wait when
practical (background command still running), then again when it finishes.

### How (Logfire MCP)

1. `search_tool` for `logfire query` if schemas are not already loaded.
2. Invoke every Logfire MCP tool via `use_tool` (qualified `logfire__*` name +
   `tool_input`). Prefer `logfire__query_run` (call
   `logfire__query_schema_reference` first when writing non-trivial SQL).
   Project: `mailpilot`.
3. Time range: `start_timestamp` = run window start (step-4 start, or
   `touch1.json` `window_start` minus 1m once written); `end_timestamp` = now.
4. Error-focused queries (DataFusion / Postgres-like). Example shapes:

```sql
-- exceptions + error-level records since window start
SELECT start_timestamp, span_name, message, level, is_exception,
       exception_type, exception_message
FROM records
WHERE (is_exception = true OR level IN ('error', 'fatal'))
ORDER BY start_timestamp DESC
LIMIT 50
```

```sql
-- agent tool failures
SELECT start_timestamp, span_name, attributes
FROM records
WHERE span_name = 'agent.tool_errors'
ORDER BY start_timestamp DESC
LIMIT 30
```

```sql
-- Gmail rate limits during sync (routing miss signal, not workflow branch fail)
SELECT start_timestamp, span_name, message, attributes
FROM records
WHERE span_name = 'gmail batch message error'
ORDER BY start_timestamp DESC
LIMIT 20
```

Optional UI handoff via `use_tool`: `logfire__project_logfire_ui_link` with
`query` like `level='error' OR is_exception` and `since` = window start.

### What to surface

On **any** hit, tell the user immediately (one short block):

- timestamp + span_name
- exception type/message or tool error
- classification:
  - **agent/tool failure** — may explain a later branch FAIL
  - **429 / batch message error** — sync drop; missing Touch 1 / unrouted often
    means mailbox had mail but sync lost it — **not** a workflow wording fail
  - **other** — note and continue

Do **not** auto-abort the campaign test for Logfire hits (still finish verify +
cleanup). Do **not** gate the branch verdict on telemetry alone. If MCP or token
is unavailable, say so once and continue — same as step 11.

### Useful span names

`agent.invoke` / `invoke_agent %`, `agent.tool_errors`, `gmail.send_message`,
`gmail batch message error`, `execute_tool %`.

## Arguments

- `--workflow-file <path>` -- defaults to the acumatica-var-outbound path above
- `--company-domain <domain>` -- optional grounding filter
- `--min-confidence N` -- optional `email_confidence` floor

## Procedure

Reuse the printed `$RUN_ID` as a **literal** in later commands (tool calls do not
share shell state). Artifacts: `reports/campaign-test/<run_id>/` (git-ignored).

### 0. Mint run id

```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/new_run_id.py
```

### 0b. Ensure test accounts exist

```bash
uv run mailpilot account view outbound@lab5.ca >/dev/null 2>&1 || \
  uv run mailpilot account create --email outbound@lab5.ca --display-name "Konstantin Borovik"
uv run mailpilot account view inbound@lab5.ca >/dev/null 2>&1 || \
  uv run mailpilot account create --email inbound@lab5.ca --display-name "MailPilot Inbound"
```

Idempotent. Never `make clean` here (live CRM). Disabled accounts need
`mailpilot account enable` -- this step cannot re-enable them.

### 0c. Ensure outbound identity (§V.151)

Required fields (exact match):

| field | value |
|---|---|
| display_name (From) | Konstantin Borovik |
| signature.full_name | Konstantin Borovik |
| signature.title | DevOps Engineer |
| signature.website | https://lab5.ca |
| signature.phone | 416-670-0621 |

```bash
uv run mailpilot account update outbound@lab5.ca \
  --display-name "Konstantin Borovik" \
  --signature-full-name "Konstantin Borovik" \
  --signature-title "DevOps Engineer" \
  --signature-website "https://lab5.ca" \
  --signature-phone "416-670-0621"
```

Idempotent field-selective update. Preflight (step 1) **blocks** if
`display_name` or nested `signature` on `outbound@lab5.ca` is missing or any
field mismatches.

### 1. Preflight

```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/preflight.py \
  --run-id $RUN_ID \
  --workflow-file /Users/kb/github/lab5-leads/workflows/acumatica-var-outbound.toml
```

Stop if `verdict != "ok"`. WARNING lines are non-blocking.

### 2. Select grounding contact

```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/select_contacts.py \
  --run-id $RUN_ID [--company-domain <domain>] [--min-confidence N]
```

Stop if no grounding contact. Seed via `/lead-contacts` (or create a real
company + contact) first.

### 3. Set up scenarios

```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/setup_scenarios.py --run-id $RUN_ID
```

From here on, always run cleanup before finish.

### 4. Send Touch 1

**Start the Logfire error-watch window** (record UTC now). Then:

```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/send_touch1.py --run-id $RUN_ID
```

Captures `rfc2822_message_id` per scenario (§V.122). Show the user one sent body.
**Poll Logfire for errors** (see Logfire real-time error watch) after this step.

### 5. Inject replies

```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/inject_replies.py --run-id $RUN_ID
```

Matches received Touch 1 by Message-ID; subject is fallback only.
**Poll Logfire** after this step (429 / sync drops often show up here).

### 6. Handle replies

```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/handle_replies.py --run-id $RUN_ID
```

Scoped route + agent invoke per scenario; re-enables prospect between scenarios.
**Poll Logfire** after this step (agent.tool_errors + exceptions peak here).

### 7. Verify branches

```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/verify_branches.py --run-id $RUN_ID
```

### 8. Critique (optional advisory)

```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/critique_prep.py --run-id $RUN_ID
```

Spawn one subagent. Give it:

- `reports/campaign-test/<RUN_ID>/critique_input.json`
- `.claude/skills/mailpilot-campaign-test/references/marketing-rubric.md`
- Write `critiques.json` + `critiques.md` under the run dir
- Return only: overall score + highest-impact edit

Score never gates the verdict.

### 9. Report

```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/generate_report.py --run-id $RUN_ID
```

Present `reports/campaign-test/$RUN_ID/report.md`. PASS only when every scenario matched.

### 10. Clean up (always)

```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/cleanup.py --run-id $RUN_ID
```

### 11. Logfire telemetry (digest)

If Logfire MCP is available, query the full run window for a final digest
(`touch1.json` `window_start` minus 1m through handle end + a few minutes).
Write `reports/campaign-test/$RUN_ID/logfire_report.md`. Fold in anything
already surfaced by the real-time error watch. Skip if unavailable -- never
gates verdict.

Include: model + token use (`agent.invoke` / `invoke_agent %`), tool errors,
send results (`gmail.send_message`), 429 sync drops, any `is_exception` rows.
Present a three-line summary: model/tokens, tool-error pattern, 429s yes/no.

## Artifacts

Under `reports/campaign-test/$RUN_ID/`:
`preflight.json`, `run_manifest.json`, `scaffold.json`, ephemeral TOMLs,
`touch1.json`, `replies.json`, `handled.json`, `verify.json`,
`critique_input.json`, `critiques.json`, `critiques.md`, `report.md`,
`cleanup.json`, `logfire_report.md`.

## Next block

End every run with `## Next` (1-5 atomic items). Examples:

After PASS:

```
## Next

1. open reports/campaign-test/<run_id>/report.md
2. mailpilot email list --account-email inbound@lab5.ca --limit 20
3. /mailpilot-campaign-test --company-domain <domain>
```

After FAIL:

```
## Next

1. open reports/campaign-test/<run_id>/verify.json
2. open reports/campaign-test/<run_id>/logfire_report.md
3. edit workflow reply-handling wording from critique highest-impact edit
4. /mailpilot-campaign-test
```

## Prerequisites

- Working DB (`mailpilot config get database_url`)
- `outbound@lab5.ca` + `inbound@lab5.ca` (step 0b creates if missing)
- Outbound identity set (step 0c: display_name + signature; preflight enforces)
- `google_application_credentials` configured
- Workflow file present
- At least one real (non-infra) contact for grounding
- Logfire MCP connected (preferred) for real-time error watch + step 11 digest;
  skill continues without it
