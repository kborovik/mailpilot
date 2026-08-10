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
allowed-tools: run_terminal_command, read_file, spawn_subagent, search_tool, use_tool
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
  uv run mailpilot account create --email outbound@lab5.ca --display-name "MailPilot Outbound"
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
| signature.phone | +1-416-670-0621 |

```bash
uv run mailpilot account update outbound@lab5.ca \
  --display-name "Konstantin Borovik" \
  --signature-full-name "Konstantin Borovik" \
  --signature-title "DevOps Engineer" \
  --signature-website "https://lab5.ca" \
  --signature-phone "+1-416-670-0621"
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

```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/send_touch1.py --run-id $RUN_ID
```

Captures `rfc2822_message_id` per scenario (§V.122). Show the user one sent body.

### 5. Inject replies

```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/inject_replies.py --run-id $RUN_ID
```

Matches received Touch 1 by Message-ID; subject is fallback only.

### 6. Handle replies

```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/handle_replies.py --run-id $RUN_ID
```

Scoped route + agent invoke per scenario; re-enables prospect between scenarios.

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

### 11. Logfire telemetry (optional)

If Logfire MCP is available, query `development` spans for the run window
(`touch1.json` `window_start` minus 1m through handle end + a few minutes).
Write `logfire_report.md`. Skip if unavailable -- never gates verdict.

Useful span names: `agent.invoke` / `invoke_agent %`, `agent.tool_errors`,
`gmail.send_message`, `gmail batch message error` (429 = sync drop, not workflow).

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
