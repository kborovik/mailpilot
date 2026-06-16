---
name: mailpilot-reply-test
description: >-
  Run the live end-to-end reply test for the lab5.ca/mailpilot demo agent: send
  water-treatment product questions from outbound@lab5.ca to inbound@lab5.ca,
  let the inbound-google-drive workflow auto-reply, grade replies against
  QA-Pairs.json, measure token use + reply latency from Logfire, and emit a
  brief report — auto-escalating to an Opus root-cause investigation and
  solution analysis if anything fails. Use this whenever the user wants to test,
  smoke-test, or validate the MailPilot inbound reply agent or demo workflow,
  run a Run-A / Run-B email-delivery test, check reply grounding or polite-
  decline behavior, or measure reply latency and token cost — even when they
  only say "test the mailpilot reply", "run the demo test", or "smoke-test the
  agent". This is LIVE Gmail traffic; do not invoke it for unit tests.
argument-hint: (no arguments)
allowed-tools: Bash(uv run *), Bash(pkill *), Read, Agent, AskUserQuestion
---

# MailPilot reply test

Drives a real send → auto-reply → grade → measure loop against the
`lab5.ca/mailpilot/` demo. Deterministic work is done by the Python scripts in
`scripts/` (they shell out to the `mailpilot` CLI and emit compact JSON) so the
LLM spends no tokens on data plumbing. The orchestrator coordinates and keeps
only the distilled per-phase summaries — bulky inputs (email bodies, Logfire
rows, code) stay inside sub-agents.

Run all commands from the repo root with `uv run python` so the project venv,
the `mailpilot` console script, and the package are importable. Scripts live in
`.claude/skills/mailpilot-reply-test/scripts/`.

## What it does

- **Run-A**: 1 in-scope question — a clean deterministic smoke.
- **Run-B**: 2 in-scope + 1 compare + 1 out-of-scope, sent simultaneously —
  exercises grounded answers, cross-datasheet compare, and polite decline under
  concurrency, and measures the parallel task drain.
- Grades each reply (see `references/grading.md`): in-scope deterministically,
  out-scope + compare by a Sonnet judge sub-agent (§V.105). Pulls per-reply
  tokens + latency from Logfire, writes `report.md`, and investigates failures.

## Safety — read before running

- This sends **real Gmail** between `outbound@lab5.ca` and `inbound@lab5.ca`
  (1 message for Run-A, 4 for Run-B).
- Step 0b runs `make clean`, which **drops and re-creates the local `mailpilot`
  and `mailpilot_test` databases** (companies/contacts are exported first). This
  is intentional — a clean DB is what makes each run deterministic — but it wipes
  all local CRM state, so only run this skill against a local/dev database, never
  a shared one.
- `mailpilot run` has no per-account scope: while up it syncs **all** accounts
  and could auto-reply to any genuine inbound mail. The skill keeps it up only
  for the test window and stops it at teardown. **Teardown is mandatory** — run
  it even if a phase failed (manual fallback: `pkill -f "mailpilot run"`).
- No outbound workflow exists (and must not): the reply lands in
  `outbound@lab5.ca`, which has no active workflow, so routing skips it — that
  is what prevents a reply-loop.

## Model orchestration

| Phase | Model | Why |
|---|---|---|
| Setup checks, Run-A, Run-B, Analysis | **Sonnet** sub-agents | Mechanical: run scripts / one SQL query, return a short summary. |
| Reply judging (out-scope + compare) | **Sonnet** sub-agent | NL-shaped grading a deterministic script cannot do reliably (§V.105): reads the reply, rubric, advisory signals, and source datasheet, returns PASS/FAIL + rationale. |
| Failure investigation, Solution analysis | **Opus** sub-agents | Hard reasoning; isolated in sub-agents so the heavy Logfire/code reading never enters the orchestrator's window. |
| Baseline reset (`make clean` + account re-create), run-loop start + stop, report generation | Orchestrator, directly | The reset must precede every sub-agent phase; the loop must outlive every phase, so the one process alive across all of them owns it; pairing start+stop there guarantees teardown always runs. Trivial deterministic commands. |

Spawn each sub-agent with the Agent tool and the stated `model`. Pass it `RUN_ID`
and the exact commands; require it to return **only** the small JSON/summary
described — never the raw bodies or query rows.

## Procedure

### 0. Mint a run id (orchestrator)
```bash
uv run python .claude/skills/mailpilot-reply-test/scripts/new_run_id.py
```
Reuse the printed value (e.g. `746e35cd`) as a **literal** wherever `$RUN_ID`
appears below — substitute the actual string into each command. Do not rely on a
shell variable: separate tool calls do not share shell state. Artifacts go to
`.mptest/<run_id>/`.

### 0b. Reset to a deterministic baseline — orchestrator, directly
Every run must start from a known-empty database so results are reproducible and
not contaminated by prior runs. Run `make clean` (exports companies/contacts,
then drops + re-creates the `mailpilot` and `mailpilot_test` databases; the
schema re-applies on first connection), then re-create the test accounts that the
clean wiped:
```bash
make clean
uv run mailpilot account create --email outbound@lab5.ca --display-name "MailPilot Outbound"
uv run mailpilot account create --email inbound@lab5.ca  --display-name "MailPilot Inbound"
uv run mailpilot account create --email hello@lab5.ca     --display-name "MailPilot Hello"
```
Do **not** re-import the demo workflow here — preflight (step 1) imports it
idempotently onto the inbound account. The orchestrator owns this step because it
is a trivial deterministic command that must run before any sub-agent phase. If
`make clean` or an `account create` fails, surface the error and abort (the loop
is not up yet, so there is nothing to tear down).

### 1. Setup checks — Sonnet sub-agent
Have it run, in order, and report the result of each:
```bash
uv run python .claude/skills/mailpilot-reply-test/scripts/preflight.py    --run-id $RUN_ID
uv run python .claude/skills/mailpilot-reply-test/scripts/validate_qa.py  --run-id $RUN_ID
uv run python .claude/skills/mailpilot-reply-test/scripts/select_cases.py --run-id $RUN_ID
```
Returns: `preflight.verdict` (+ any issues), `validate_qa.verdict` (+ missing),
and the selected Run-A / Run-B case ids.
**Stop the whole run** if `preflight.verdict != "ok"` or `validate_qa.verdict`
is not `"grounded"` — surface the issues and skip to teardown (the loop is not
up yet, so there is nothing to stop).

### 1b. Start the run loop — orchestrator, directly
Only after setup checks pass:
```bash
uv run python .claude/skills/mailpilot-reply-test/scripts/run_loop_start.py --run-id $RUN_ID
```
The orchestrator owns this process so it outlives every sub-agent phase. If it
reports `"started": false` with `"process exited"`, read the `log_tail` (usually
missing Google credentials) and abort to teardown.

### 2. Run-A — Sonnet sub-agent
```bash
uv run python .claude/skills/mailpilot-reply-test/scripts/send_emails.py    --run-id $RUN_ID --run A
uv run python .claude/skills/mailpilot-reply-test/scripts/collect_replies.py --run-id $RUN_ID --run A
uv run python .claude/skills/mailpilot-reply-test/scripts/score_replies.py   --run-id $RUN_ID --run A
```
Returns: the Run-A verdict + elapsed seconds. Run-A is in-scope only, so
`score_replies` grades it deterministically — no judge step. `collect_replies`
polls until the reply arrives or it times out (~5 min); a missing reply is
recorded as `NO_REPLY`, not an error.

### 3. Run-B — Sonnet sub-agent
Same three commands with `--run B`. `send_emails` fires the 4 emails
concurrently; `collect_replies` waits up to ~8 min for all four. `score_replies`
grades the 2 in-scope cases deterministically and writes a `"JUDGE"` sentinel for
the compare + out-of-scope cases (§V.105) — step 3b resolves those. Returns the 2
in-scope verdicts, the 2 pending-judge case ids, and elapsed.

### 3b. Reply judging — Sonnet sub-agent
Resolves the compare + out-of-scope verdicts that `score_replies` deferred. The
sub-agent:
1. Runs `judge_prep.py` to bundle each pending case's question, reply body,
   rubric, advisory signals, and (for compare) the source datasheets:
   ```bash
   uv run python .claude/skills/mailpilot-reply-test/scripts/judge_prep.py --run-id $RUN_ID --run B
   ```
2. Reads `.mptest/$RUN_ID/judge_B.json`. For each case it decides a verdict:
   - **out-scope** — PASS iff the reply clearly declines and invents no spec for
     the absent product; the `fabrication_candidates` signal is only a hint —
     question-echoed figures and referral links are not fabrication.
   - **compare** — PASS iff the reply compares the named products grounded in the
     supplied datasheets (numbers match the source), cites both sources, and uses
     a GFM pipe table.
3. Writes `.mptest/$RUN_ID/judgments_B.json` as
   `{"<case_id>": {"verdict": "PASS"|"FAIL", "rationale": "<one line>"}}`, then
   folds the verdicts in (recomputes `summary` + `failed`):
   ```bash
   uv run python .claude/skills/mailpilot-reply-test/scripts/apply_judgments.py --run-id $RUN_ID --run B
   ```
Returns: the finalized compare + out-of-scope verdicts with one-line rationales.
If `judge_prep` reports a `datasheet_error`, judge grounding from the reply's own
citations and note the degraded check in the rationale.

### 4. Analysis — Sonnet sub-agent (Logfire MCP)
Skip if `preflight.logfire_ok` is false (note it in the report). Otherwise the
sub-agent:
1. Reads `.mptest/$RUN_ID/replies_A.json` and `replies_B.json` and collects each
   case's `trigger_email_id` (the inbound email id = `agent.invoke.email_id`),
   and `sends_*.json` for `window_start` and `preflight.json` for
   `logfire_environment`.
2. Runs ONE `query_run` (project `mailpilot`) — adjust column names to the live
   schema if needed (`query_schema_reference`); latency is the `agent.invoke`
   span duration:
   ```sql
   SELECT attributes->>'email_id'                       AS email_id,
          attributes->>'model'                          AS model,
          (attributes->>'input_tokens')::bigint         AS input_tokens,
          (attributes->>'output_tokens')::bigint        AS output_tokens,
          (attributes->>'total_tokens')::bigint         AS total_tokens,
          (attributes->>'cache_read_input_tokens')::bigint AS cache_read_input_tokens,
          attributes->>'status'                         AS status,
          EXTRACT(EPOCH FROM (end_timestamp - start_timestamp)) * 1000 AS latency_ms
   FROM records
   WHERE span_name = 'agent.invoke'
     AND service_name = 'mailpilot'
     AND deployment_environment = '<logfire_environment>'
     AND start_timestamp >= '<window_start of Run-A>'
     AND attributes->>'email_id' IN (<all trigger_email_ids>);
   ```
3. Writes `.mptest/$RUN_ID/logfire_metrics.json`, mapping each row back to its
   `case_id` via `trigger_email_id`:
   ```json
   {"environment": "...", "window_start": "...",
    "cases": {"qa-in-024": {"email_id": "...", "model": "...",
      "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
      "cache_read_input_tokens": 0, "latency_ms": 0, "status": "completed"}}}
   ```
Returns: total tokens, avg/max latency, model(s). Keep raw rows out of the reply.

### 5. Teardown — orchestrator, directly (ALWAYS)
```bash
uv run python .claude/skills/mailpilot-reply-test/scripts/run_loop_stop.py --run-id $RUN_ID
```

### 6. Report — orchestrator, directly
```bash
uv run python .claude/skills/mailpilot-reply-test/scripts/generate_report.py --run-id $RUN_ID
```
Reads `.mptest/$RUN_ID/report.md` and present its summary to the user.

### 7. Failure escalation — only if a run scored FAIL or NO_REPLY
Check `scoring_A.json` / `scoring_B.json` `failed` flags.

**7a. Investigation — Opus sub-agent.** Give it the failed `case_id`s, their
`trigger_email_id`s, `window_start`, `logfire_environment`, and the paths to
`scoring_*.json`, `replies_*.json`, `judgments_*.json` (the judge's rationale for
any judged FAIL), and `run.log`. It uses the Logfire MCP to
inspect, for those email ids: `agent.invoke` `status` / `result` /
`agent_reasoning` / `tool_error_count`; any `is_exception = true` spans in the
window; `run.task.transient_retry` events; and whether classification routed the
email at all (no `agent.invoke` for an email id ⇒ the classifier didn't route
it). It also greps `run.log` for `event=error`. It writes a concise
`.mptest/$RUN_ID/investigation.md` (root cause per failed case) and returns a
one-paragraph summary.

**7b. Solutions — Opus sub-agent.** Give it `investigation.md`. It maps each root
cause to a concrete fix (a workflow-instruction tweak in
`workflows/demo-lab5-mailpilot.toml`, a code/spec change with `file:line` /
`§V.N`, or an environment fix), notes confidence, and writes
`.mptest/$RUN_ID/solutions.md`. Then re-run `generate_report.py` (step 6) so the
report folds in both sections.

## Artifacts

Everything for a run is under `.mptest/$RUN_ID/` (git-ignored): `preflight.json`,
`validate_qa.json`, `run_manifest.json`, `sends_*.json`, `replies_*.json`,
`scoring_*.json`, `judge_*.json`, `judgments_*.json`, `logfire_metrics.json`,
`run.log`, `run.pid`, `report.md`, and (on failure) `investigation.md` /
`solutions.md`.
