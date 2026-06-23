---
name: mailpilot-campaign-test
description: >-
  Test the real outbound cold-email workflow agent against real CRM contacts
  before any real send. Runs the live workflow agent (default
  workflows/outbound-lab5-llm-lookup-work.toml) so it drafts and sends a
  genuinely personalized email per contact, but mirrors each real contact onto a
  controlled inbound alias (inbound1@lab5.ca through inbound9@lab5.ca) so the
  real contact address is never emailed, confirms delivery from Gmail, and runs
  an Opus sub-agent that critiques each sent email against cold-email marketing
  best practices. Use this whenever the user wants to test, smoke-test, dry-run,
  preview, simulate, critique, or validate an outbound campaign, cold-email
  blast, or outreach workflow against real or discovered leads -- even when they
  only say "test the campaign", "test my cold email", or "check the outreach
  workflow before I send it". This sends LIVE Gmail traffic to the alias mailbox;
  it never emails the real contacts.
argument-hint: "[--workflow-file <path>] [--limit N] [--company-domain <domain>] [--min-confidence N]"
allowed-tools: Bash(uv run *), Read, Agent, AskUserQuestion
---

# mailpilot-campaign-test

Test the real outbound workflow agent against real CRM contacts, as close to
production as possible, without emailing anyone real. The skill runs the live
agent defined by an outbound workflow TOML, so the agent reads each contact and
company, drafts a personalized email, and sends it through the same path the
production agent uses. Real contacts supply the personalization data (name,
title, company); the recipient is always a controlled inbound alias, so no real
person is ever emailed.

Deterministic work is done by the Python scripts in `scripts/`. They shell out
to the `mailpilot` CLI and emit compact JSON, so the orchestrator spends no
tokens on data plumbing. Run every command from the repo root with `uv run
python` so the project venv, the `mailpilot` console script, and the package are
importable. Scripts live in `.claude/skills/mailpilot-campaign-test/scripts/`.

## How the alias safety works

The agent sends to the contact's stored email. The safety guarantee is that the
contact the agent reads is never a real prospect: it is a persistent
**alias-contact** whose own email is one of nine inbound aliases.

- **Source:** `outbound@lab5.ca` sends every message.
- **Alias-contacts:** nine persistent contacts whose email is `inbound1@lab5.ca`
  through `inbound9@lab5.ca`. A run mirrors a selected real contact's name,
  title, and company onto one alias-contact, then runs the agent against it. The
  agent reads the alias-contact and sends to its email -- the alias. The real
  contact row is never touched and its address is never a recipient.
- **Delivery mailbox:** `inbound@lab5.ca` receives all nine aliases and is the
  account synced to confirm delivery.

`contact.email` is globally unique, so there can be exactly one contact per
alias address. That is why the alias-contacts are persistent and reused, and why
the run is capped at nine messages (one per alias).

## What it does

- **Select.** Picks up to nine real contacts as the personalization source,
  skipping the alias-contact scaffolding itself.
- **Scaffold.** Ensures the nine alias-contacts and a neutral, disabled test
  company exist, then mirrors each selected real contact's first name, last name,
  title, and company onto its alias-contact. The alias-contact is linked to the
  REAL company for the duration of the run so the agent's `read_company` step has
  real grounding.
- **Enroll.** Imports an ephemeral, per-run copy of the outbound workflow into
  `outbound@lab5.ca` and enrolls the alias-contacts. A fresh workflow each run
  means the 30-day cold-send cooldown never blocks a re-run.
- **Run the agent.** Runs each enrollment synchronously. The live agent drafts a
  personalized email and sends it to the alias through the production send path,
  including the outbound body lint. The skill then reads back what the agent
  actually sent (subject and body).
- **Verify.** Syncs `inbound@lab5.ca` and confirms each sent email arrived,
  matched by the agent-written subject.
- **Critique.** An Opus sub-agent reads each sent email with its contact and
  company context and scores it against the cold-email marketing rubric
  (`references/marketing-rubric.md`): relevance, subject, value proposition,
  credibility, call to action, concision, tone, and spam risk. It returns
  per-email strengths, weaknesses, and the single highest-impact fix. Advisory
  only -- it never changes the PASS or FAIL verdict.
- **Report.** Writes a per-contact table (including the critique score) and a
  PASS or FAIL verdict.
- **Clean up.** Re-parks the alias-contacts off the real companies and stops the
  ephemeral workflow.

## Safety -- read before running

- The real contacts are the personalization **data source only**. The agent only
  ever reads an alias-contact and sends to its alias address
  (`inbound{1-9}@lab5.ca`). A real contact address is never the recipient, and
  the real contact row is never modified.
- This sends **real Gmail** from `outbound@lab5.ca` to the alias mailbox (one
  message per selected contact, at most nine), drafted live by the agent.
- The skill creates persistent test scaffolding in the live database: nine
  alias-contacts and one neutral disabled test company (`campaign-test.invalid`,
  hidden from `company list` and lead discovery). These are reused across runs.
- During a run, each alias-contact is linked to a real company for grounding,
  which adds one to that company's contact_count transiently. Cleanup re-parks
  the alias-contacts on the neutral company, so real companies are untouched at
  rest. Cleanup is best-effort and idempotent; the next run's setup also re-parks
  defensively.
- Each run leaves one stopped `[campaign-test <run_id>]` workflow on
  `outbound@lab5.ca`. Workflows cannot be deleted, so this is expected; an
  operator may ignore or `workflow stop` leftover rows.
- The skill never starts `mailpilot run`, so no sync loop or auto-reply fires
  during the test.
- The run is capped at nine contacts (one per alias). `--limit N` only lowers the
  count; values above nine are clamped to nine.

## Arguments

- `--workflow-file <path>` -- the outbound workflow TOML whose agent to test.
  Defaults to `workflows/outbound-lab5-llm-lookup-work.toml`.
- `--limit N` -- number of contacts to test (default 9, clamped to at most 9).
- `--company-domain <domain>` -- restrict to one company's contacts.
- `--min-confidence N` -- restrict to contacts with `email_confidence` at least
  N.

The workflow file must exist and be valid TOML with `name`, `template`,
`objective`, and `instructions`. If `--workflow-file` points at a path under the
`workflows/` submodule that is absent, run `git submodule update --init` first.

## Procedure

The orchestrator runs every step directly. Steps 0 through 5, 7, and 8 are
deterministic scripts. Step 6 (critique) is the one sub-agent phase: spawn it
with the Agent tool and `model: opus`. The heavy reading -- the email bodies,
contact context, and rubric -- stays inside that sub-agent; it returns only a
short summary.

### 0. Mint a run id
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/new_run_id.py
```
Reuse the printed value (e.g. `746e35cd`) as a **literal** wherever `$RUN_ID`
appears below. Separate tool calls do not share shell state, so do not rely on a
shell variable. Artifacts go to `.campaign-test/<run_id>/` (git-ignored).

### 1. Preflight
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/preflight.py --run-id $RUN_ID [--workflow-file <path>]
```
Validates the workflow TOML, resolves the `outbound@lab5.ca` sender and the
`inbound@lab5.ca` alias mailbox, confirms neither account is disabled, confirms
Google credentials are configured, and counts the real contacts. **Stop the run**
if `verdict != "ok"`. Surface the issues. A `WARNING` line is not blocking.

### 2. Select contacts
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/select_contacts.py --run-id $RUN_ID [--limit N] [--company-domain <domain>] [--min-confidence N]
```
Picks up to nine real contacts (excluding the alias-contact scaffolding) and
writes the run manifest. **Stop the run** if `selected` is 0 -- run
`/lead-contacts` first to seed contacts.

### 3. Set up the run
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/setup_run.py --run-id $RUN_ID
```
Ensures the alias-contacts and the neutral test company exist, mirrors each
selected real contact onto its alias-contact (linked to the real company),
imports the ephemeral per-run workflow, and enrolls the alias-contacts. Reports
the `ephemeral_workflow_id` and how many alias-contacts were enrolled. Writes
`scaffold.json`. **From here on, always run cleanup (step 8) before you finish,
even if a later step fails**, so the alias-contacts are re-parked and the
ephemeral workflow is stopped.

### 4. Run the live agent
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/run_agents.py --run-id $RUN_ID
```
Runs each enrollment synchronously. The live agent drafts and sends each email to
its alias, then the script reads back what was sent. Reports `sent`, `failed`,
and `skipped`. Read a sent body or two from `.campaign-test/$RUN_ID/sends.json`
and show the user what the agent actually produced (subject and body), plus any
failures.

### 5. Verify delivery
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/verify_delivery.py --run-id $RUN_ID
```
Syncs `inbound@lab5.ca` and confirms each sent email arrived, matched by the
agent-written subject. Reports `delivered` and any `missing`.

### 6. Critique -- Opus sub-agent
First bundle each sent email with its contact and company context:
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/critique_prep.py --run-id $RUN_ID
```
This writes `.campaign-test/$RUN_ID/critique_input.json`. If `prepared` is 0
(nothing was sent), skip the sub-agent and note it. Otherwise spawn ONE sub-agent
with the Agent tool and `model: opus`. Give it only the two paths and the output
contract below -- it does its own reading, so the bodies never enter your window:

> You are a cold-email marketing critic. Read
> `.campaign-test/<RUN_ID>/critique_input.json` (one record per sent email, each
> with the recipient's contact and company context, the subject, and the body)
> and `.claude/skills/mailpilot-campaign-test/references/marketing-rubric.md`.
> Critique each email against the rubric, grounded in that recipient's context.
> Write `.campaign-test/<RUN_ID>/critiques.json` as
> `{"critiques": [{"sequence": <int>, "overall_score": <1-5>, "dimension_scores":
> {...}, "strengths": [...], "weaknesses": [...], "suggestions": [...]}],
> "summary": "<one paragraph across all emails>"}` -- the first suggestion per
> email must be the single highest-impact fix. Also write a readable
> `.campaign-test/<RUN_ID>/critiques.md`. Return only a two-line summary: the
> average score and the most common weakness. Do not return the bodies.

Substitute the literal run id for `<RUN_ID>`. The score does not gate the
verdict; it is advisory marketing feedback for the operator.

### 7. Report
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/generate_report.py --run-id $RUN_ID
```
Reads `.campaign-test/$RUN_ID/report.md` and presents its summary. The report
folds in the critique score per email plus the critique section. The verdict is
PASS only when there are zero send failures and zero missing deliveries; the
critique never changes it.

### 8. Clean up
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/cleanup.py --run-id $RUN_ID
```
Re-parks the alias-contacts on the neutral test company (removing the
real-company link) and stops the ephemeral workflow. **Always run this**, even if
an earlier step failed, so the run leaves no real-company contact_count skew and
no active test workflow. It is idempotent and safe to re-run.

## Artifacts

Everything for a run is under `.campaign-test/$RUN_ID/` (git-ignored):
`preflight.json`, `run_manifest.json`, `scaffold.json`, `ephemeral_workflow.toml`,
`sends.json`, `delivery.json`, `critique_input.json`, `critiques.json`,
`critiques.md`, `report.md`, and `cleanup.json`.

## OUTPUT -- "Next" block

End with a short "Next" block of atomic follow-up commands. Example after a
passing run:

```
## Next

1. mailpilot email list --account-email inbound@lab5.ca --limit 9 -- inspect the delivered copies
2. /mailpilot-campaign-test --company-domain <domain> -- test one company's contacts
3. open .campaign-test/<run_id>/report.md -- re-read the per-contact table
```

After a failing run:

```
## Next

1. open .campaign-test/<run_id>/report.md -- read the per-contact send failures
2. edit the workflow instructions -- fix what made the agent's send fail (e.g. a spec block that must be a |---| pipe table)
3. /mailpilot-campaign-test -- re-run after the edit
```

## Prerequisites

- `mailpilot` installed locally with a working DB (`mailpilot config get
  database_url`).
- The `outbound@lab5.ca` and `inbound@lab5.ca` accounts created (`mailpilot
  account create`), neither disabled. Re-create them after `make clean`.
- The nine aliases `inbound1@lab5.ca` through `inbound9@lab5.ca` configured on
  the `inbound@lab5.ca` mailbox in Google Workspace.
- `google_application_credentials` set (the live send needs Gmail auth).
- The workflow file checked out (`git submodule update --init` if it lives under
  `workflows/`).
- At least one real contact in the database (run `/lead-contacts` first).

## Why this skill exists

`/lead-companies` and `/lead-contacts` produce real contact rows for cold
outreach. Before a workflow goes to those real people, its agent must read the
contact and company, draft a personalized email that renders correctly and clears
the outbound body lint, and stay on message. This skill runs that real agent
against real contact data while keeping every recipient on a controlled alias, so
a broken workflow is caught before it reaches a prospect. The default workflow
definition lives at `workflows/outbound-lab5-llm-lookup-work.toml`.
