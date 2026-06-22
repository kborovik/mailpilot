---
name: mailpilot-campaign-test
description: >-
  Simulate an outbound cold-email campaign against real CRM contacts before any
  real send. Reads a campaign Markdown file (subject plus body with {first_name}
  / {company} / {title} placeholders), personalizes it for up to nine real
  contacts from the database, runs the live outbound body lint (the §V.42
  pipe-table check the send path enforces), then live-sends each rendered message
  from outbound@lab5.ca -- but rewrites every recipient to a controlled inbound
  alias (inbound1@lab5.ca through inbound9@lab5.ca) so the real contact address
  is never emailed -- and confirms delivery from Gmail. Use this whenever the
  user wants to test, smoke-test, dry-run, preview, simulate, or validate an
  outbound campaign, cold-email blast, or outreach template against real or
  discovered leads -- even when they only say "test the campaign", "test my cold
  email", or "check the outreach message before I send it". This sends LIVE
  Gmail traffic to the alias mailbox; it never emails the real contacts.
argument-hint: <campaign-file> [--limit N] [--company-domain <domain>] [--min-confidence N]
allowed-tools: Bash(uv run *), Read, AskUserQuestion
---

# mailpilot-campaign-test

Simulate an outbound campaign against real CRM contacts, as close to real data
as possible, without emailing anyone real. The skill personalizes the campaign
message per real contact, lints the body, sends each rendered copy from
`outbound@lab5.ca`, and confirms it arrived. The real contacts supply the
personalization data (name, title, company, even their real email). The
recipient address is rewritten to one of nine controlled aliases, so no real
person is ever emailed.

Deterministic work is done by the Python scripts in `scripts/`. They shell out
to the `mailpilot` CLI and emit compact JSON, so the orchestrator spends no
tokens on data plumbing. Run every command from the repo root with `uv run
python` so the project venv, the `mailpilot` console script, and the package are
importable. Scripts live in `.claude/skills/mailpilot-campaign-test/scripts/`.

## Accounts and aliases

- **Source:** `outbound@lab5.ca` sends every message.
- **Targets:** `inbound1@lab5.ca` through `inbound9@lab5.ca` -- nine aliases that
  all deliver into the `inbound@lab5.ca` mailbox. Contact 1 maps to
  `inbound1@lab5.ca`, contact 2 to `inbound2@lab5.ca`, and so on.
- **Delivery mailbox:** `inbound@lab5.ca` receives all nine aliases and is the
  account synced to confirm delivery.

This is why the run is capped at nine messages: one per alias.

## What it does

- **Personalize.** Substitutes `{first_name}`, `{last_name}`, `{full_name}`,
  `{title}`, `{company}` (the contact's company domain), and `{email}` per real
  contact. A NULL field uses a neutral fallback and is recorded as a gap so thin
  contact data is visible.
- **Lint.** Runs the app's own §V.42 outbound body check
  (`_check_spec_table`, imported from `mailpilot.agent.tools` so it stays
  identical to the send path). The check rejects a body with at least 3
  consecutive spec-shape lines and no `|---|` pipe-table separator.
- **Send.** Sends each rendered copy from `outbound@lab5.ca` to the contact's
  assigned alias and records whether Gmail accepted it. Lint failures are skipped
  by default.
- **Verify.** Syncs `inbound@lab5.ca` and confirms each sent copy arrived,
  matched by its subject tag.
- **Report.** Writes a per-contact table and a PASS or FAIL verdict.

## Safety -- read before running

- The real contacts are the personalization **data source only**. Every message
  is sent to an `inbound{1-9}@lab5.ca` alias. A real contact address is never the
  recipient.
- This sends **real Gmail** from `outbound@lab5.ca` to the alias mailbox (one
  message per selected contact, at most nine).
- The skill never starts `mailpilot run`, so no auto-reply or sync loop fires
  during the test. The `inbound@lab5.ca` mailbox normally carries the inbound
  demo workflow, but no auto-reply happens unless the run loop is up -- do not
  start it during a test.
- The run is capped at nine contacts (one per alias). `--limit N` only lowers the
  count; values above nine are clamped to nine.

## Campaign file format

Plain Markdown. The first non-blank line is the subject, the rest is the body.
Both may carry `{placeholder}` tokens. See `assets/campaign.example.md`, which
mirrors the lab5.ca outreach message.

```
Subject: Cutting {company}'s repetitive lookup work to seconds

Hi {first_name},

...body in Markdown...
```

Spec rows (model numbers, costs, dimensions) in the body **must** use a GFM pipe
table with a `|---|` separator, or the lint rejects the body (§V.42).

## Arguments

- `<campaign-file>` -- path to the campaign Markdown file (required).
- `--limit N` -- number of contacts to test (default 9, clamped to at most 9).
- `--company-domain <domain>` -- restrict to one company's contacts.
- `--min-confidence N` -- restrict to contacts with `email_confidence` at least
  N.

If the user gives no campaign file, ask for the path with `AskUserQuestion`
before running. Do not invent one.

## Procedure

The orchestrator runs every step directly. The work is deterministic, so no
sub-agents are needed.

### 0. Mint a run id
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/new_run_id.py
```
Reuse the printed value (e.g. `746e35cd`) as a **literal** wherever `$RUN_ID`
appears below. Separate tool calls do not share shell state, so do not rely on a
shell variable. Artifacts go to `.campaign-test/<run_id>/` (git-ignored).

### 1. Preflight
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/preflight.py --run-id $RUN_ID --campaign <campaign-file>
```
Parses the campaign file, resolves the `outbound@lab5.ca` sender and the
`inbound@lab5.ca` alias mailbox, confirms Google credentials are configured, and
counts the contacts. **Stop the run** if `verdict != "ok"`. Surface the issues. A
`WARNING` line is not blocking.

### 2. Select contacts
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/select_contacts.py --run-id $RUN_ID [--limit N] [--company-domain <domain>] [--min-confidence N]
```
Picks up to nine real contacts, assigns each its alias, and writes the run
manifest. **Stop the run** if `selected` is 0 -- run `/lead-contacts` first to
seed contacts.

### 3. Personalize and lint
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/personalize.py --run-id $RUN_ID
```
Renders and lints every copy with no network call, and writes one
`preview_NN.md` per contact. Read a preview or two and show the user the
rendered subject and body before sending. Note any `lint_failures` and
`contacts_with_gaps`.

### 4. Send
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/send_campaign.py --run-id $RUN_ID
```
Sends each lint-passing copy to its alias. Lint failures are skipped unless you
add `--include-lint-failures`. Reports `sent`, `failed`, and `skipped`.

### 5. Verify delivery
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/verify_delivery.py --run-id $RUN_ID
```
Syncs `inbound@lab5.ca` and confirms each sent copy arrived. Reports `delivered`
and any `missing`.

### 6. Report
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/generate_report.py --run-id $RUN_ID
```
Reads `.campaign-test/$RUN_ID/report.md` and presents its summary. The verdict
is PASS only when there are zero lint failures, zero send failures, and zero
missing deliveries.

## Artifacts

Everything for a run is under `.campaign-test/$RUN_ID/` (git-ignored):
`preflight.json`, `run_manifest.json`, `personalized.json`, `preview_NN.md`,
`sends.json`, `delivery.json`, and `report.md`.

## OUTPUT -- "Next" block

End with a short "Next" block of atomic follow-up commands. Example after a
passing run:

```
## Next

1. mailpilot email list --account-email inbound@lab5.ca --limit 9 -- inspect the delivered copies
2. /mailpilot-campaign-test <campaign-file> --company-domain <domain> -- test one company's contacts
3. open .campaign-test/<run_id>/report.md -- re-read the per-contact table
```

After a failing run:

```
## Next

1. open .campaign-test/<run_id>/report.md -- read the per-contact failures
2. edit the campaign file -- fix the lint failure (use a |---| pipe table for spec rows)
3. /mailpilot-campaign-test <campaign-file> -- re-run after the edit
```

## Prerequisites

- `mailpilot` installed locally with a working DB (`mailpilot config get
  database_url`).
- The `outbound@lab5.ca` and `inbound@lab5.ca` accounts created (`mailpilot
  account create`). Re-create them after `make clean`.
- The nine aliases `inbound1@lab5.ca` through `inbound9@lab5.ca` configured on
  the `inbound@lab5.ca` mailbox in Google Workspace.
- `google_application_credentials` set (the live send needs Gmail auth).
- At least one contact in the database (run `/lead-contacts` first).

## Why this skill exists

`/lead-companies` and `/lead-contacts` produce real contact rows for cold
outreach. Before a campaign goes to those real people, the copy must render
correctly, personalize cleanly, and pass the same body lint the send path
enforces. This skill simulates the campaign against real contact data while
rewriting every recipient to a controlled alias, so a broken template is caught
before it reaches a prospect. The companion outbound workflow definition lives at
`workflows/outbound-lab5-llm-lookup-work.toml`.
