---
name: mailpilot-prompt-audit
description: >-
  Audit how the mailpilot system composes its LLM prompts, per system, and
  recommend improvements to the system prompt and workflow instructions. Two
  LLM systems are covered: the classifier (agent.classify_email, routes by each
  workflow's goal) and the workflow agent (agent.invoke, system prompt =
  code-defined template protocol fragments plus the workflow's TOML
  instructions). The skill composes each system's actual prompt deterministically
  from code and the live workflow rows, pulls per-system Logfire telemetry
  (token economics, cache-read share, tool-call and tool-error counts, status
  distribution, latency, sample reasoning), then an Opus sub-agent reviews the
  system prompt plus workflow instructions against the telemetry and writes a
  prioritized list of concrete edits -- each flagged as a workflow TOML change or
  a code change plus PR. Use this whenever the user wants to analyze, audit, or
  review prompt composition, the system prompt, the agent shape, or workflow
  instructions from Logfire data and get improvement suggestions -- even when
  they only say "audit the prompts", "analyze prompt composition", "review the
  system prompt", or "suggest workflow-instruction improvements". This is
  read-only: no Gmail traffic, no `mailpilot run`, no database mutation.
argument-hint: "[--status active|all] [--days N] [--environment development|production]"
allowed-tools: Bash(uv run *), Read, Agent, mcp__claude_ai_logfire__query_run, mcp__claude_ai_logfire__query_schema_reference
---

# mailpilot-prompt-audit

Audit the **prompt / LLM-agent composition** of the running system, per system,
and recommend improvements to the **system prompt** and **workflow
instructions**. Read-only: it composes the prompts the system already runs,
joins them with Logfire telemetry, and critiques the wording. It never sends
Gmail, never starts `mailpilot run`, and never mutates the database (§V.119).

Two LLM systems are composed and graded:

- **Classifier** (`agent.classify_email`) -- one structured-output call, no
  tools. System prompt is `_INSTRUCTIONS` in `agent/classify.py`. It routes an
  inbound email by matching each candidate workflow's `goal`.
- **Workflow agent** (`agent.invoke`) -- the tool-using auto-reply / outbound
  agent. System prompt is `template.build_protocol(trigger) +
  workflow.instructions` (§V.44-45): code-defined protocol fragments from
  `agent/templates.py` followed by the workflow's own TOML `instructions`.

Deterministic work (compose, join, report) is done by the Python scripts in
`scripts/`; they import `mailpilot` and read live workflow rows, emitting compact
JSON, so the orchestrator spends no tokens on data plumbing. The bulky inputs --
full prompt text, telemetry rows -- stay inside the scripts and sub-agents. Run
every command from the repo root with `uv run python`.

## What it does

- **Compose.** Reads every live workflow row (their ids match the Logfire
  `workflow_id`, so telemetry joins back to a system), resolves each workflow's
  template, and composes the full system prompt from the protocol fragments plus
  the TOML `instructions`. Also captures the classifier system prompt and the
  shared fragment inventory. Sizes each layer and tags it by edit target (code
  fragment vs. workflow TOML).
- **Measure.** Pulls per-system Logfire telemetry over a window: model, token
  economics, cache-read share, tool-call and tool-error counts, status
  distribution, trigger mix (which composition ran), prompt length, latency, and
  a small sample of agent reasoning.
- **Join.** Merges telemetry onto each composed system and computes the derived
  rates deterministically (cache-read share, tokens per invocation, tool-error
  rate, failed-run rate).
- **Analyze.** An Opus sub-agent reviews each system prompt plus the workflow
  instructions against the rubric (`references/audit-rubric.md`) and the
  telemetry, and writes a prioritized list of concrete edits -- each flagged as
  a workflow TOML change or a code change plus PR (§V.44).
- **Report.** Writes `report.md`: a systems-overview table, a composition-sizing
  table, the classifier section, and the folded-in analysis.

## Safety -- read before running

- **Read-only.** No Gmail send, no sync loop, no database write. The skill opens
  one database connection to read workflow rows; `initialize_database`
  provisions only an empty DB and never mutates a populated one as a side effect
  (§V.110).
- The composed `composition.json` and `analysis_input.json` carry the full
  prompt text. They live under `reports/prompt-audit/<run_id>/` (git-ignored) and are read
  only by the scripts and the analysis sub-agent, never echoed to the
  orchestrator in bulk.

## Arguments

- `--status active|all` -- which workflow statuses to compose. Default `active`
  (the live systems). `all` includes paused and draft rows -- useful to audit a
  workflow before activating it, but it also pulls in the test-scaffold
  workflows the campaign-test skill leaves paused.
- `--days N` -- telemetry lookback window in days. Default 13. The Logfire MCP
  enforces a strict 14-day maximum on the query time range, measured to the
  second, so a 14-day lookback whose end timestamp is the current time (not
  midnight) is rejected as "exceeds max 14 days". 13 leaves headroom and never
  trips the check.
- `--environment development|production` -- the Logfire `deployment_environment`
  to query. Default `development` (local runs land here; §V.52).

## Model orchestration

| Phase | Model | Why |
|---|---|---|
| Compose, Join, Report | Orchestrator, directly | Deterministic scripts; trivial commands that emit compact JSON. |
| Telemetry pull | **Sonnet** sub-agent | Runs two aggregate SQL queries through the Logfire MCP and shapes the result JSON. Mechanical; keeps the raw rows out of the orchestrator. |
| Prompt analysis | **Opus** sub-agent | Judgment-heavy critique of the system prompt and workflow instructions against the telemetry; isolated so the full prompt text never enters the orchestrator's window. |

Spawn each sub-agent with the Agent tool and the stated `model`. Pass it the
literal `RUN_ID` and the exact commands; require it to return only the small
summary described.

## Procedure

The orchestrator runs the deterministic steps directly. Step 2 (telemetry) is a
Sonnet sub-agent that queries the Logfire MCP. Step 4 (analysis) is an Opus
sub-agent. The heavy reading -- full prompt text, telemetry rows -- stays inside
the scripts and sub-agents.

### 0. Mint a run id (orchestrator)
```bash
uv run python .claude/skills/mailpilot-prompt-audit/scripts/new_run_id.py
```
Reuse the printed value (e.g. `2026-06-26-142305_56f7ec48`) as a **literal**
wherever `$RUN_ID` appears below -- substitute the actual string into each command. Separate tool
calls do not share shell state. Artifacts go to `reports/prompt-audit/<run_id>/`
(git-ignored).

### 1. Compose the prompts (orchestrator, directly)
```bash
uv run python .claude/skills/mailpilot-prompt-audit/scripts/compose_prompts.py --run-id $RUN_ID --status active
```
Writes `composition.json` (full system-prompt text per system, sizes, fragment
inventory) and prints a compact summary: the workflow count, the templates in
use, and per-workflow instruction / system-prompt sizes. Note the
`workflow_id`s it lists -- they are the join key for the telemetry. If the
workflow count is 0, tell the user no workflows match the status filter and
suggest `--status all`.

### 2. Pull Logfire telemetry -- Sonnet sub-agent
Skip this step only if the Logfire token is unavailable (note it in the report;
the analysis then degrades to a static review). Otherwise spawn one Sonnet
sub-agent. Give it `RUN_ID`, the environment (default `development`), the
lookback (default 13 days), and the composed `workflow_id`s from step 1, and
this contract:

> You pull per-system telemetry for the mailpilot prompt audit. Run each query
> through the Logfire MCP with `mcp__claude_ai_logfire__query_run`
> (`project: mailpilot`); call `mcp__claude_ai_logfire__query_schema_reference`
> once if you need the schema. Run the two queries below over the last `<DAYS>` days in
> `deployment_environment = '<ENVIRONMENT>'`. Set the MCP `start_timestamp` /
> `end_timestamp` to span the lookback; the MCP enforces a strict 14-day maximum
> measured to the second, so if a query is rejected for exceeding the max range,
> reduce the lookback by one day and note it. Adjust column names to the live
> schema only if a query errors. Latency is the `agent.invoke` span `duration`
> (seconds) times 1000.
>
> Query A -- per-workflow agent telemetry:
> ```sql
> SELECT
>   attributes->>'workflow_id'                                AS workflow_id,
>   COUNT(*)                                                  AS invocations,
>   MAX(attributes->>'model')                                AS model,
>   SUM((attributes->>'input_tokens')::bigint)               AS input_tokens_sum,
>   SUM((attributes->>'output_tokens')::bigint)              AS output_tokens_sum,
>   SUM((attributes->>'total_tokens')::bigint)               AS total_tokens_sum,
>   SUM((attributes->>'cache_read_input_tokens')::bigint)    AS cache_read_sum,
>   SUM((attributes->>'cache_creation_input_tokens')::bigint) AS cache_creation_sum,
>   SUM((attributes->>'tool_call_count')::bigint)            AS tool_call_count_sum,
>   SUM((attributes->>'tool_error_count')::bigint)           AS tool_error_count_sum,
>   SUM(CASE WHEN attributes->>'status' = 'completed' THEN 1 ELSE 0 END) AS completed,
>   SUM(CASE WHEN attributes->>'status' = 'failed' THEN 1 ELSE 0 END)    AS failed,
>   SUM(CASE WHEN attributes->>'trigger' = 'task' THEN 1 ELSE 0 END)            AS trigger_task,
>   SUM(CASE WHEN attributes->>'trigger' = 'email' THEN 1 ELSE 0 END)           AS trigger_email,
>   SUM(CASE WHEN attributes->>'trigger' = 'enrollment_run' THEN 1 ELSE 0 END)  AS trigger_enrollment_run,
>   AVG((attributes->>'prompt_length')::bigint)              AS avg_prompt_length,
>   AVG(duration) * 1000                                     AS avg_latency_ms
> FROM records
> WHERE service_name = 'mailpilot'
>   AND span_name = 'agent.invoke'
>   AND deployment_environment = '<ENVIRONMENT>'
>   AND start_timestamp >= now() - interval '<DAYS> days'
> GROUP BY workflow_id
> ORDER BY invocations DESC
> LIMIT 100
> ```
>
> Query B -- classifier telemetry. The `agent.classify_email` span carries
> `input_tokens` / `output_tokens` / `total_tokens` and `duration`, but no cache
> attributes (the classifier does not enable prompt caching), so cache-read share
> is not available for it:
> ```sql
> SELECT
>   COUNT(*)                                     AS invocations,
>   MAX(attributes->>'model')                    AS model,
>   SUM((attributes->>'input_tokens')::bigint)   AS input_tokens,
>   SUM((attributes->>'output_tokens')::bigint)  AS output_tokens,
>   SUM((attributes->>'total_tokens')::bigint)   AS total_tokens,
>   AVG(duration) * 1000                         AS avg_latency_ms,
>   SUM(CASE WHEN attributes->>'result' = 'match' THEN 1 ELSE 0 END)         AS match,
>   SUM(CASE WHEN attributes->>'result' = 'no_match' THEN 1 ELSE 0 END)      AS no_match,
>   SUM(CASE WHEN attributes->>'result' = 'no_candidates' THEN 1 ELSE 0 END) AS no_candidates
> FROM records
> WHERE service_name = 'mailpilot'
>   AND span_name = 'agent.classify_email'
>   AND deployment_environment = '<ENVIRONMENT>'
>   AND start_timestamp >= now() - interval '<DAYS> days'
> LIMIT 10
> ```
>
> Then fetch up to 2 sample reasonings each (truncate each to 400 chars) for the
> **audited** workflows -- the composed `workflow_id`s from step 1, not the
> global busiest -- so the analyst has qualitative signal for the systems under
> audit. With `--status active` the audited workflow is often not among the
> busiest ids in the window, so target the composed ids explicitly:
> ```sql
> SELECT attributes->>'workflow_id' AS workflow_id,
>        LEFT(attributes->>'agent_reasoning', 400) AS reasoning
> FROM records
> WHERE service_name = 'mailpilot'
>   AND span_name = 'agent.invoke'
>   AND deployment_environment = '<ENVIRONMENT>'
>   AND start_timestamp >= now() - interval '<DAYS> days'
>   AND attributes->>'workflow_id' IN (<the composed workflow_ids from step 1>)
>   AND attributes->>'agent_reasoning' IS NOT NULL
> LIMIT 20
> ```
> If a composed workflow returns no reasoning rows, record an empty
> `sample_reasonings` for it and note that the window captured no reasoning for
> that system -- an instrumentation gap, not an error.
>
> Write `reports/prompt-audit/<RUN_ID>/logfire_metrics.json` in exactly this shape (map
> Query A's `completed`/`failed` columns into `status_distribution`, its
> `trigger_task`/`trigger_email`/`trigger_enrollment_run` columns into
> `trigger_distribution`, and group the sample reasonings under each workflow's
> `sample_reasonings`):
> ```json
> {
>   "window": {"days": <DAYS>, "since": "<now - DAYS, ISO>", "until": "<now, ISO>"},
>   "environment": "<ENVIRONMENT>",
>   "classifier": {"invocations": 0, "model": "...",
>     "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
>     "avg_latency_ms": 0,
>     "result_distribution": {"match": 0, "no_match": 0, "no_candidates": 0}},
>   "workflows": {
>     "<workflow_id>": {"invocations": 0, "model": "...",
>       "input_tokens_sum": 0, "output_tokens_sum": 0, "total_tokens_sum": 0,
>       "cache_read_sum": 0, "cache_creation_sum": 0,
>       "tool_call_count_sum": 0, "tool_error_count_sum": 0,
>       "status_distribution": {"completed": 0, "failed": 0},
>       "trigger_distribution": {"task": 0, "email": 0, "enrollment_run": 0},
>       "avg_prompt_length": 0, "avg_latency_ms": 0,
>       "sample_reasonings": ["..."]}
>   }
> }
> ```
> Return only: the environment, the window, the count of workflows with
> telemetry, and the classifier match rate. Do not return the raw rows.

If the sub-agent reports the Logfire token or MCP is unavailable, skip to step 3
without `logfire_metrics.json` -- the join still runs and the analysis becomes a
static composition review.

### 3. Join telemetry onto the composition (orchestrator, directly)
```bash
uv run python .claude/skills/mailpilot-prompt-audit/scripts/analyze_prep.py --run-id $RUN_ID
```
Writes `analysis_input.json`: each composed system enriched with a `telemetry`
block and the derived rates, ordered so the busiest systems lead. Prints
whether telemetry was available and the top systems by invocation count.

### 4. Analyze -- Opus sub-agent
Spawn one Opus sub-agent. Give it `RUN_ID` and this contract:

> You are a prompt-composition auditor. The unit of critique is the authored
> prompt text -- the code-defined system prompt (template fragments, classifier
> `_INSTRUCTIONS`) and each workflow's TOML `goal` and `instructions`. Read
> `reports/prompt-audit/<RUN_ID>/analysis_input.json` -- it holds, per system, the composed
> system prompt, the layer sizes tagged by edit target, and a `telemetry` block
> with derived rates (or null when the window had no activity). The telemetry
> block also carries `trigger_distribution` and `composition_run` (`TASK`,
> `INITIAL`, or `mixed`), so you can tell which composed prompt the window
> actually exercised rather than inferring it. The top-level `tool_contract`
> lists the real `conclude_enrollment` dispositions and their outcomes; check
> every tool name and argument a workflow instruction references against it, and
> `derived_formulas` spells out each derived rate so you can verify a number
> against the raw sums. Also read
> `.claude/skills/mailpilot-prompt-audit/references/audit-rubric.md` and the
> project `CLAUDE.md`. For the exact text of a code fragment you want to change,
> read `src/mailpilot/agent/templates.py` or `src/mailpilot/agent/classify.py`;
> for workflow instruction text, the composed prompt is already in the JSON
> (source of truth is `workflows/*.toml`).
>
> Score each system against the rubric dimensions, using the telemetry as
> evidence, and produce a prioritized list of concrete edits. Each edit names a
> `target` (`code:templates.py:<fragment>`, `code:classify.py`,
> `toml:<workflow> goal`, or `toml:<workflow> instructions`), quotes the
> `current` text, gives the `proposed` replacement, cites the motivating
> `evidence` (the metric and value, or the contradicting lines), and states
> `confidence` and `priority`. Flag every code target as a change plus PR
> (§V.44), not a workflow update. The first edit is the single highest-impact
> change.
>
> Write `reports/prompt-audit/<RUN_ID>/analysis.json` as
> `{"systems": [{"name": ..., "dimension_scores": {...}, "strengths": [...],
> "weaknesses": [...]}], "edits": [...], "summary": "<one paragraph>"}` and a
> readable `reports/prompt-audit/<RUN_ID>/analysis.md` (a short per-system section, then a
> prioritized "Suggested edits" list). Return only a three-line summary: the
> highest-impact edit, the most-improvable system, and the strongest empirical
> signal you found. Do not return the full prompt text.

Substitute the literal run id for `<RUN_ID>`.

### 5. Report (orchestrator, directly)
```bash
uv run python .claude/skills/mailpilot-prompt-audit/scripts/generate_report.py --run-id $RUN_ID
```
Reads `reports/prompt-audit/$RUN_ID/report.md` and presents its summary to the user. The
report folds in `analysis.md` automatically.

## Artifacts

Everything for a run is under `reports/prompt-audit/$RUN_ID/` (git-ignored):
`composition.json`, `logfire_metrics.json`, `analysis_input.json`,
`analysis.json`, `analysis.md`, and `report.md`.

## OUTPUT -- "Next" block

End with a short "Next" block of atomic follow-up commands. Example:

```
## Next

1. open reports/prompt-audit/<run_id>/report.md -- read the systems table and the suggested edits
2. open reports/prompt-audit/<run_id>/analysis.md -- read the per-system critique and evidence
3. edit workflows/<file>.toml -- apply the highest-impact instruction edit, then re-run /mailpilot-campaign-test or /mailpilot-reply-test to validate
4. /mailpilot-prompt-audit --status all -- widen the audit to paused/draft workflows
```

## Why this skill exists

Operators tune two things: the workflow `goal` and `instructions` they edit in
TOML, and the code-defined protocol fragments that wrap them. The wording of
both drives every model call, but the cost, cache efficiency, tool errors, and
decline behavior of those calls only show up in Logfire. This skill joins the
two -- the authored prompt text on one side, the empirical telemetry on the
other -- so a wording problem (a redundant rule, an unstable prefix that breaks
caching, a vague goal that misroutes, an instruction that fights the send
contract) is caught with evidence and a concrete edit, separate from any live
email test.
