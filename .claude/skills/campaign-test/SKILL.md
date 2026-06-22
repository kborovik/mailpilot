---
name: campaign-test
description: >-
  Test an outbound cold-email campaign message against discovered contacts
  before any real send. Reads a campaign Markdown file (subject plus body with
  {first_name} / {company} / {title} placeholders), personalizes it for each
  discovered contact seeded by /lead-contacts, runs the live outbound body lint
  (the §V.42 pipe-table check the send path enforces), then live-sends each
  rendered message only to the safe lab5.ca sink mailbox -- never to the real
  contact addresses -- and confirms delivery from Gmail. Use this whenever the
  user wants to test, smoke-test, dry-run, preview, or validate an outbound
  campaign, cold-email blast, or outreach template against discovered or seeded
  leads -- even when they only say "test the campaign", "test my cold email", or
  "check the outreach message before I send it". This sends LIVE Gmail traffic
  to the test mailbox; it never emails the discovered contacts.
argument-hint: <campaign-file> [--limit N] [--company-domain <domain>] [--min-confidence N]
allowed-tools: Bash(uv run *), Read, AskUserQuestion
---

# campaign-test

Test an outbound campaign message against the contacts that `/lead-contacts`
discovered. The skill personalizes the message per discovered contact, lints the
body, sends each rendered copy to the safe `lab5.ca` sink mailbox, and confirms
the copy arrived. The discovered contacts supply personalization data only. The
real send recipient is always the sink, so no real person is ever emailed.

Deterministic work is done by the Python scripts in `scripts/`. They shell out
to the `mailpilot` CLI and emit compact JSON, so the orchestrator spends no
tokens on data plumbing. Run every command from the repo root with `uv run
python` so the project venv, the `mailpilot` console script, and the package are
importable. Scripts live in `.claude/skills/campaign-test/scripts/`.

## What it does

- **Personalize.** Substitutes `{first_name}`, `{last_name}`, `{full_name}`,
  `{title}`, `{company}` (the contact's company domain), and `{email}` per
  discovered contact. A NULL field uses a neutral fallback and is recorded as a
  gap so thin contact data is visible.
- **Lint.** Runs the app's own §V.42 outbound body check
  (`_check_spec_table`, imported from `mailpilot.agent.tools` so it stays
  identical to the send path). The check rejects a body with at least 3
  consecutive spec-shape lines and no `|---|` pipe-table separator.
- **Send.** Sends each rendered copy from `outbound@lab5.ca` to the sink
  `hello@lab5.ca` and records whether Gmail accepted it. Lint failures are
  skipped by default.
- **Verify.** Syncs the sink mailbox and confirms each sent copy arrived,
  matched by its subject tag.
- **Report.** Writes a per-contact table and a PASS or FAIL verdict.

## Safety -- read before running

- The discovered contacts are the personalization **data source only**. Every
  message is sent to `hello@lab5.ca`. A real contact address is never the
  recipient.
- This sends **real Gmail** from `outbound@lab5.ca` to `hello@lab5.ca` (one
  message per selected contact, default 3).
- The skill never starts `mailpilot run`, so no auto-reply or sync loop fires
  during the test. The sink carries no active workflow, so even a running loop
  would not reply. There is no background process to tear down.
- The default sample is 3 contacts. Raise it with `--limit N` only when you
  intend that many test sends.

## Campaign file format

Plain Markdown. The first non-blank line is the subject, the rest is the body.
Both may carry `{placeholder}` tokens. See `assets/campaign.example.md`.

```
Subject: Quick question about {company}'s water treatment

Hi {first_name},

...body in Markdown...
```

Spec rows (model numbers, flow rates, dimensions) in the body **must** use a GFM
pipe table with a `|---|` separator, or the lint rejects the body (§V.42).

## Arguments

- `<campaign-file>` -- path to the campaign Markdown file (required).
- `--limit N` -- number of discovered contacts to test (default 3).
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
uv run python .claude/skills/campaign-test/scripts/new_run_id.py
```
Reuse the printed value (e.g. `746e35cd`) as a **literal** wherever `$RUN_ID`
appears below. Separate tool calls do not share shell state, so do not rely on a
shell variable. Artifacts go to `.campaign-test/<run_id>/` (git-ignored).

### 1. Preflight
```bash
uv run python .claude/skills/campaign-test/scripts/preflight.py --run-id $RUN_ID --campaign <campaign-file>
```
Parses the campaign file, resolves the `outbound@lab5.ca` sender and
`hello@lab5.ca` sink accounts, confirms Google credentials are configured, and
counts the discovered contacts. **Stop the run** if `verdict != "ok"`. Surface
the issues. A `WARNING` line is not blocking.

### 2. Select contacts
```bash
uv run python .claude/skills/campaign-test/scripts/select_contacts.py --run-id $RUN_ID [--limit N] [--company-domain <domain>] [--min-confidence N]
```
Picks the discovered contacts that supply personalization data and writes the
run manifest. **Stop the run** if `selected` is 0 -- run `/lead-contacts` first
to seed contacts.

### 3. Personalize and lint
```bash
uv run python .claude/skills/campaign-test/scripts/personalize.py --run-id $RUN_ID
```
Renders and lints every copy with no network call, and writes one
`preview_NN.md` per contact. Read a preview or two and show the user the
rendered subject and body before sending. Note any `lint_failures` and
`contacts_with_gaps`.

### 4. Send
```bash
uv run python .claude/skills/campaign-test/scripts/send_campaign.py --run-id $RUN_ID
```
Sends each lint-passing copy to the sink. Lint failures are skipped unless you
add `--include-lint-failures`. Reports `sent`, `failed`, and `skipped`.

### 5. Verify delivery
```bash
uv run python .claude/skills/campaign-test/scripts/verify_delivery.py --run-id $RUN_ID
```
Syncs the sink mailbox and confirms each sent copy arrived. Reports `delivered`
and any `missing`.

### 6. Report
```bash
uv run python .claude/skills/campaign-test/scripts/generate_report.py --run-id $RUN_ID
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

1. /campaign-test <campaign-file> --limit 10 -- widen the sample
2. mailpilot email list --account-email hello@lab5.ca --limit 5 -- inspect the delivered copies
3. /campaign-test <campaign-file> --company-domain <domain> -- test one company's contacts
```

After a failing run:

```
## Next

1. open .campaign-test/<run_id>/report.md -- read the per-contact failures
2. edit the campaign file -- fix the lint failure (use a |---| pipe table for spec rows)
3. /campaign-test <campaign-file> -- re-run after the edit
```

## Prerequisites

- `mailpilot` installed locally with a working DB (`mailpilot config get
  database_url`).
- The `outbound@lab5.ca` and `hello@lab5.ca` accounts created (`mailpilot
  account create`). Re-create them after `make clean`.
- `google_application_credentials` set (the live send needs Gmail auth).
- At least one discovered contact (run `/lead-contacts` first).

## Why this skill exists

`/lead-companies` and `/lead-contacts` produce verified contact rows for cold
outreach. Before a campaign goes to those real people, the copy must render
correctly, personalize cleanly, and pass the same body lint the send path
enforces. This skill exercises the real send and render path against real
contact data while keeping every message inside the `lab5.ca` test mailbox, so a
broken template is caught before it reaches a prospect.
