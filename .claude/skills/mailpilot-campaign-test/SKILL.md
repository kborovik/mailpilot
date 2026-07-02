---
name: mailpilot-campaign-test
description: >-
  Test the full multi-step flow of the real outbound cold-email workflow agent
  (default workflows/ai-engineering.toml) before any real send. The live agent
  sends a personalized cold Touch 1 to a controlled prospect mailbox
  (inbound@lab5.ca), then the test replies to that email with content crafted to
  drive each branch of the workflow's reply handling -- positive/booked,
  question, not-now, opt-out, auto-reply/out-of-office, and wrong-person -- and
  the live agent handles each reply. The test then verifies the agent took the
  right branch (booked the call, declined, disabled the contact, or did nothing)
  and runs an Opus sub-agent that critiques the workflow's reply-handling wording
  and suggests edits. Use this whenever the user wants to test, smoke-test,
  dry-run, simulate, critique, or validate an outbound campaign, cold-email
  blast, outreach workflow, or its reply handling and branch outcomes -- even
  when they only say "test the campaign", "test my cold email", "test the full
  flow", or "check the outreach workflow before I send it". This sends LIVE Gmail
  traffic between outbound@lab5.ca and inbound@lab5.ca; it never emails a real
  contact.
argument-hint: "[--workflow-file <path>] [--company-domain <domain>] [--min-confidence N]"
allowed-tools: Bash(uv run *), Read, Agent, mcp__claude_ai_logfire__query_run
---

# mailpilot-campaign-test

Test the real outbound workflow agent across its **full multi-step flow**, as
close to production as possible, without emailing anyone real. The skill runs the
live agent defined by an outbound workflow TOML through two agent turns:

1. **Touch 1** -- the agent reads the prospect contact and the real grounding
   company, drafts a personalized cold email, and sends it.
2. **Reply handling** -- the test plays the prospect: it replies to that Touch 1
   with content crafted to drive one branch of the workflow's "Handling replies"
   section, and the live agent handles the reply and takes a branch.

The recipient is always the controlled `inbound@lab5.ca` mailbox, so no real
person is ever emailed. Deterministic work is done by the Python scripts in
`scripts/`; they shell out to the `mailpilot` CLI (and, for the scoped
reply-handling trigger, import a few `mailpilot` functions) and emit compact
JSON, so the orchestrator spends no tokens on data plumbing. Run every command
from the repo root with `uv run python`.

## How the safety + isolation works

- **Sender:** `outbound@lab5.ca` sends every Touch 1 and every agent reply.
- **Prospect:** one persistent contact whose own email IS `inbound@lab5.ca`. The
  agent sends Touch 1 to the contact's email, so it can only ever reach the
  controlled mailbox. The test replies from that same mailbox, so the reply's
  From maps the inbound reply back to this one prospect contact.
- **Grounding:** a real contact's first name, last name, title, and company are
  mirrored onto the prospect contact, and it is linked to the real company for
  the run, so the agent's `read_contact` / `read_company` steps have real
  grounding. The real contact row is never modified and its address is never a
  recipient.
- **Scenario isolation:** every reply branch gets its **own ephemeral workflow**.
  An enrollment is per (workflow, contact), so N ephemeral workflows give N
  independent enrollments for the one prospect contact -- one per scenario -- and
  a fresh workflow id also dodges the 30-day cold-send cooldown (§V.79).
- **No run loop:** the skill never starts `mailpilot run`. Reply handling is
  driven scoped (see step 6): sync the sender, route each reply, bridge it to a
  task, and invoke the agent on it -- the same path the loop uses, but only for
  this run's workflows, so no auto-reply ever fires for genuine mail.

## What it does

- **Set up.** Ensures the prospect contact and a neutral disabled test company
  exist, mirrors a real contact onto the prospect for grounding, then imports one
  ephemeral workflow per scenario and enrolls the prospect in each.
- **Send Touch 1.** Runs each enrollment so the live agent drafts and sends a
  cold Touch 1 to `inbound@lab5.ca` through the production send path (including
  the §V.42 body lint), and captures each Touch 1's RFC 2822 Message-ID.
- **Inject replies.** Syncs the prospect mailbox, matches each received Touch 1 by
  Message-ID, and replies with the scenario's crafted body -- so the agent sees a
  real, in-thread prospect reply.
- **Handle replies.** Syncs the sender so each reply routes back to its ephemeral
  workflow, bridges it to a task, and invokes the live agent on it. Scenarios are
  handled one at a time, re-enabling the shared prospect contact between each, so
  an opt-out / wrong-person disable never blocks a later scenario.
- **Verify branches.** For each scenario, reads the observable state the agent
  left -- terminal outcome, contact disabled or not, whether the agent replied,
  any follow-up task -- and checks it against the branch the scenario should have
  driven. PASS only when every scenario matched.
- **Critique.** An Opus sub-agent reads the workflow's reply-handling wording and
  the branch evidence, scores it against the rubric
  (`references/marketing-rubric.md`), and suggests concrete edits. Advisory only;
  it never changes the verdict.
- **Report.** Writes a per-scenario table (expected branch | observed | PASS or
  FAIL) and an overall verdict, plus the critique.
- **Clean up.** Re-enables and re-parks the prospect contact and stops every
  ephemeral workflow.
- **Analyze telemetry.** Queries the run's Logfire spans: which model the agent
  ran on, token use and latency per turn, agent tool errors, send results, and
  any Gmail 429 rate-limit errors during sync.

## Safety -- read before running

- The real contact is the personalization **data source only**. The agent only
  ever reads the prospect contact and sends to `inbound@lab5.ca`. A real contact
  address is never a recipient, and the real contact row is never modified.
- This sends **real Gmail**: one Touch 1 per scenario from `outbound@lab5.ca` to
  `inbound@lab5.ca`, one crafted reply per scenario back the other way, and the
  agent's own handling replies. With the default six scenarios that is up to
  about a dozen messages, all between the two test mailboxes.
- The skill creates persistent test scaffolding in the live database: one prospect
  contact (`inbound@lab5.ca`) and one neutral disabled test company
  (`campaign-test.invalid`, hidden from `company list` and lead discovery). These
  are reused across runs.
- During a run the prospect contact is linked to a real company for grounding,
  which adds one to that company's contact_count transiently. Cleanup re-parks it
  on the neutral company, so real companies are untouched at rest.
- An opt-out / wrong-person branch disables the prospect contact globally. The
  handling step re-enables it between scenarios, and cleanup re-enables it at the
  end; the next run's setup also re-enables defensively. This is test scaffolding,
  not a real unsubscribe.
- Each run leaves several stopped `[campaign-test <run_id> <scenario>]` workflows
  on `outbound@lab5.ca`. Workflows cannot be deleted, so this is expected.
- The skill never starts `mailpilot run`, so no sync loop or auto-reply fires for
  any other account.

## Arguments

- `--workflow-file <path>` -- the outbound workflow TOML whose agent to test.
  Defaults to `workflows/ai-engineering.toml`.
- `--company-domain <domain>` -- restrict the grounding contact to one company.
- `--min-confidence N` -- restrict the grounding contact to `email_confidence`
  at least N.

The workflow file must exist and be valid TOML with `name`, `template`,
`goal`, and `instructions`. The default lives under `workflows/`, a
gitignored symlink to the independent repo at `/Users/kb/github/workflows`. If
that path is absent, restore the symlink (`ln -s ../workflows workflows`) or pass
`--workflow-file` with an explicit path. The reply branches are fixed by
`references/reply-scenarios.json`.

## Procedure

The orchestrator runs every step directly. Steps 0 through 7, 9, and 10 are
deterministic scripts. Step 8 (critique) is a sub-agent phase: spawn it with the
Agent tool and `model: opus`. Step 11 (telemetry) queries Logfire through the
MCP. The heavy reading -- the workflow wording, the reply bodies, the rubric --
stays inside the critique sub-agent; it returns only a short summary.

**Always run cleanup (step 10) before you finish, even if a later step fails**,
so the prospect contact is re-enabled and re-parked and the ephemeral workflows
are stopped.

### 0. Mint a run id
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/new_run_id.py
```
Reuse the printed value (e.g. `2026-06-26-142305_746e35cd`) as a **literal**
wherever `$RUN_ID` appears below. Separate tool calls do not share shell state. Artifacts go to
`reports/campaign-test/<run_id>/` (git-ignored).

### 0b. Ensure the test accounts exist -- create if missing
```bash
uv run mailpilot account view outbound@lab5.ca >/dev/null 2>&1 || uv run mailpilot account create --email outbound@lab5.ca --display-name "MailPilot Outbound"
uv run mailpilot account view inbound@lab5.ca  >/dev/null 2>&1 || uv run mailpilot account create --email inbound@lab5.ca  --display-name "MailPilot Inbound"
```
The `account view` guard makes this idempotent. Never run `make clean` here --
this skill tests against the live CRM database, and `make clean` would drop real
company and contact rows (§V.119). The guard cannot re-enable a disabled account;
if preflight reports an account disabled, re-enable it with `mailpilot account
enable`.

### 1. Preflight
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/preflight.py --run-id $RUN_ID [--workflow-file <path>]
```
Validates the workflow TOML, resolves the `outbound@lab5.ca` sender and the
`inbound@lab5.ca` prospect mailbox, confirms neither account is disabled,
confirms Google credentials, and counts the real contacts available for
grounding. **Stop the run** if `verdict != "ok"`. A `WARNING` line is not
blocking.

### 2. Select the grounding contact
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/select_contacts.py --run-id $RUN_ID [--company-domain <domain>] [--min-confidence N]
```
Picks one real contact as the Touch 1 personalization grounding and writes the
run manifest. **Stop the run** if no grounding contact is selected -- run
`/lead-contacts` first to seed contacts.

### 3. Set up the scenarios
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/setup_scenarios.py --run-id $RUN_ID
```
Ensures the prospect contact and neutral company exist, mirrors the grounding
contact onto the prospect (linked to the real company), imports one ephemeral
workflow per scenario, and enrolls the prospect in each. Reports how many
scenarios were enrolled. **From here on, always run cleanup (step 10) before you
finish.**

### 4. Send Touch 1
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/send_touch1.py --run-id $RUN_ID
```
Runs each enrollment so the live agent sends a cold Touch 1 to `inbound@lab5.ca`,
then reads back each Touch 1 row (subject, thread, RFC Message-ID). Reports `sent`
and any `missing_message_id`. Read a sent body from `reports/campaign-test/$RUN_ID/`
(`mailpilot email view <id>`) and show the user what the agent produced. A
`missing_message_id` entry is a risk: routing the reply later depends on it (see
step 6).

### 5. Inject replies
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/inject_replies.py --run-id $RUN_ID
```
Syncs the prospect mailbox, matches each received Touch 1 by Message-ID, and
replies with each scenario's crafted body. Polls until every Touch 1 has arrived
or it times out (~5 min). Reports `replies_sent` and any `not_received`.

### 6. Handle replies
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/handle_replies.py --run-id $RUN_ID
```
Syncs the sender so each reply routes back to its ephemeral workflow (by RFC
Message-ID), bridges each to a task, and invokes the live agent on it -- handling
scenarios one at a time and re-enabling the prospect contact between each.
Reports `handled` and any `unrouted`. **An `unrouted` scenario means routing did
not match the reply to its workflow** (most often a Touch 1 that never captured
its Message-ID); that scenario cannot be verified and the report will show it.

### 7. Verify branches
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/verify_branches.py --run-id $RUN_ID
```
For each scenario, reads the observable state (terminal outcome, contact disabled
or not, whether the agent replied, any follow-up task) and checks it against the
expected branch in `references/reply-scenarios.json`. Reports the verdict and any
`failed` scenarios. The expectations are tolerant of the wording-vs-tool gap (the
TOML says "completed/opt-out" but the disable branches record `failed`): the
gating keys on the branch-defining signal, and a divergent outcome type is
reported, not gated.

### 8. Critique -- Opus sub-agent
First bundle the workflow wording with the branch evidence:
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/critique_prep.py --run-id $RUN_ID
```
This writes `reports/campaign-test/$RUN_ID/critique_input.json` with a `workflow` block
and a `scenarios` list (each with the inbound reply, the agent's reply, the
expected branch, and the observed outcome). Then spawn ONE sub-agent with the
Agent tool and `model: opus`. Give it only the two paths and the output contract:

> You are a reply-handling workflow critic. The unit of critique is the workflow
> wording, not the individual replies. Read
> `reports/campaign-test/<RUN_ID>/critique_input.json` -- its `workflow` block holds the
> `goal` and `instructions` that drove the agent, and its `scenarios` list
> is evidence of how that wording handled each reply branch (each with the inbound
> reply, the agent's reply, the expected branch, and the observed outcome). Also
> read `.claude/skills/mailpilot-campaign-test/references/marketing-rubric.md`.
> Critique the workflow's reply-handling wording against the rubric, using the
> scenarios as evidence, and suggest concrete edits to the `goal` and
> `instructions`. Write `reports/campaign-test/<RUN_ID>/critiques.json` as
> `{"workflow_name": <str>, "overall_score": <1-5>, "dimension_scores": {...},
> "strengths": [...], "patterns": [...], "weaknesses": [...], "edits": [...],
> "summary": "<one paragraph>"}` -- each `edits` entry names the line to change
> and gives the replacement wording, and the first `edits` entry is the single
> highest-impact change. Also write a readable
> `reports/campaign-test/<RUN_ID>/critiques.md`. Return only a two-line summary: the
> reply-handling score and the highest-impact edit. Do not return the reply
> bodies.

Substitute the literal run id for `<RUN_ID>`. The score is advisory; it never
gates the verdict.

### 9. Report
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/generate_report.py --run-id $RUN_ID
```
Reads `reports/campaign-test/$RUN_ID/report.md` and presents its summary. The report
folds in the critique. The verdict is PASS only when every scenario's observed
branch matched its expectation.

### 10. Clean up
```bash
uv run python .claude/skills/mailpilot-campaign-test/scripts/cleanup.py --run-id $RUN_ID
```
Re-enables and re-parks the prospect contact and stops every ephemeral workflow.
**Always run this**, even if an earlier step failed. It is idempotent and safe to
re-run.

### 11. Analyze Logfire telemetry
Query the run's spans through the Logfire MCP (`mcp__claude_ai_logfire__query_run`,
`project: mailpilot`) and save `reports/campaign-test/$RUN_ID/logfire_report.md`. The
run's spans are in the `development` environment. Scope the time range to the run
window: `touch1.json` holds `window_start`; query from a minute before it to a few
minutes after the last handle step. Pull these facts and write them up:

- **Model and token use** -- `span_name = 'agent.invoke'` (rollup: bare
  `input_tokens` / `output_tokens` attributes) or `span_name LIKE 'invoke_agent %'`
  (pydantic-ai v2 run span: `gen_ai.aggregated_usage.input_tokens` /
  `output_tokens`) gives per-turn latency and token use. Report which model the
  workflow actually ran on; it is not always a Claude model.
- **Agent tool errors** -- `span_name = 'agent.tool_errors'`. The `tool_errors`
  attribute names the tool and error.
- **Send results** -- `span_name = 'gmail.send_message'`. Confirm sends and no
  failures.
- **Inbound-sync rate limiting** -- `span_name = 'gmail batch message error'`.
  HTTP 429 in a sync window means the sync was rate limited and dropped fetched
  messages. **A missing Touch 1 arrival or a missing reply route plus a 429 burst
  in the same minute means the message reached the mailbox but the sync lost it --
  not a workflow failure.**
- **Exceptions** -- any row with `is_exception = true` in the window.

Present a three-line summary: model and total tokens, any tool-error pattern, and
whether any 429s hit a sync. If the Logfire MCP or token is unavailable, note that
and skip this step -- it is read-only and never gates the verdict.

## Artifacts

Everything for a run is under `reports/campaign-test/$RUN_ID/` (git-ignored):
`preflight.json`, `run_manifest.json`, `scaffold.json`, `ephemeral_<scenario>.toml`,
`touch1.json`, `replies.json`, `handled.json`, `verify.json`, `critique_input.json`,
`critiques.json`, `critiques.md`, `report.md`, `cleanup.json`, and
`logfire_report.md`.

## OUTPUT -- "Next" block

End with a short "Next" block of atomic follow-up commands. Example after a
passing run:

```
## Next

1. mailpilot email list --account-email inbound@lab5.ca --limit 20 -- inspect the Touch 1s, replies, and agent handling
2. open reports/campaign-test/<run_id>/report.md -- re-read the per-scenario table
3. /mailpilot-campaign-test --company-domain <domain> -- re-run grounded on one company
```

After a failing run:

```
## Next

1. open reports/campaign-test/<run_id>/verify.json -- read which branch the agent missed and why
2. open reports/campaign-test/<run_id>/logfire_report.md -- separate a routing miss (unrouted / 429) from a wrong-branch decision
3. edit the workflow's "Handling replies" wording -- apply the critique's highest-impact edit
4. /mailpilot-campaign-test -- re-run after the edit
```

## Prerequisites

- `mailpilot` installed locally with a working DB (`mailpilot config get
  database_url`).
- The `outbound@lab5.ca` and `inbound@lab5.ca` accounts present and neither
  disabled. Step 0b creates either if missing but cannot re-enable a disabled one
  -- use `mailpilot account enable`.
- `google_application_credentials` set (the live send and replies need Gmail
  auth).
- The workflow file present (the `workflows/` symlink points at
  `/Users/kb/github/workflows`; restore it with `ln -s ../workflows workflows` if
  absent).
- At least one real contact in the database for grounding (run `/lead-contacts`
  first).

## Why this skill exists

Before a workflow goes to real prospects, its agent must do the whole job, not
just the first email: read the contact and company, draft a Touch 1 that renders
and clears the body lint, and then handle whatever the prospect replies -- book
the call, answer a question, defer gracefully, honor an unsubscribe, ignore an
auto-reply, and drop a wrong contact. This skill runs that real agent through the
full send-reply-handle loop against real contact data while keeping every
recipient on the controlled `inbound@lab5.ca` mailbox, so a broken branch is
caught before it reaches a prospect. The critique then reads the branches as a set
and points back at the workflow's reply-handling wording -- the text the operator
actually edits. The default workflow definition lives at
`workflows/ai-engineering.toml`.
