# mailpilot reply test (local skill)

## Problem / Goal

Stand up a **local Claude Code skill** (`.claude/skills/mailpilot-reply-test/`) that drives a
live end-to-end test of the inbound auto-reply agent for the `lab5.ca/mailpilot/` demo:

- send water-treatment product questions from `outbound@lab5.ca` -> `inbound@lab5.ca`,
- let the demo workflow agent (template `inbound-google-drive`, grounded in Drive folder
  `1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat`) auto-reply,
- grade replies against `QA-Pairs.json`,
- measure token use + reply latency from Logfire,
- emit a brief report,
- on failure, auto-escalate to an Opus root-cause investigation, then an Opus
  solution-analysis pass.

This replaces the removed `test-google-drive` skill (commits `57afd14`/`aae7662`); `QA-Pairs.json`
is the surviving fixture. It is a **`.claude/` local artifact, NOT folded into SPEC.md** — no new
§T/§V/§B. Distinct from `.claude/workflows/*.js` Claude Code orchestration scripts (§V.73-74) and
from `workflows/*.toml` workflow definitions (§V.103).

### Non-goals
- Not a CI/pytest unit test — this is live Gmail traffic, operator-run.
- No outbound workflow (per request; also what prevents a reply-loop — see Safety).
- No new SPEC items.

## Confirmed decisions (this session)

| # | Decision |
|---|---|
| Run-loop | **Skill-managed, detached.** Setup launches `mailpilot run` in a new session (no per-account scope exists); teardown SIGTERMs it. |
| Case mix | **Run-A** = 1 in-scope. **Run-B** = 2 in-scope + 1 compare + 1 out-of-scope, sent simultaneously. |
| Live eval | Build, then **one live Run-A smoke**, then iterate. |
| Models | Setup + execution + routine analysis = **Sonnet** sub-agents. Failure investigation + solution analysis = **Opus** sub-agents. Heavy data stays in sub-agent context; only distilled JSON/summaries return to the orchestrator. |

## System facts that constrain the design (verified)

- **Accounts already exist**: `outbound@lab5.ca` = `019ecb12-1c55-722f-953a-f1149f6812f9`,
  `inbound@lab5.ca` = `019ecb12-2a5b-745e-a235-abce830d2d45`. Account ids are UUIDv7 (§V.12).
- **Demo workflow already imported + active** on inbound: `019ecb12-6064-7462-94bd-1fba1bf281fd`,
  template `inbound-google-drive`, status `active`.
- **Drive grounding verified**: all 34 `source_file`s in `QA-Pairs.json` exist in the folder; 0
  missing, 0 extra (folder also holds 34 sibling PDFs, unreferenced).
- **CLI surface** (`cli.py`):
  - `email send --account-id <uuid> --to <addr> --subject <s> --body <s>` — body inline only;
    prints `{"email":{...},"ok":true}` (§V.4).
  - `email list [--account-id --since --direction inbound|outbound --status sent --thread-id --to --from]`
    — JSON; `email view <id>` for full body.
  - `account list` (find uuids by email); `workflow import --account-id <uuid> --file <toml>`
    (idempotent upsert, auto-activates when objective+instructions set).
  - `mailpilot run` — foreground loop, **all accounts, no per-account scope**, SIGTERM/SIGINT to stop.
    `account sync` routes but **does not drain the task queue**, so it never produces replies — the
    full loop is the only path to an auto-reply.
- **Reply path** (`sync.py` -> `routing.py` -> `run.py` -> `agent/invoke.py` -> `agent/tools.py`):
  inbound stored -> `route_email` (thread/RFC-id/LLM classify) -> `_ensure_enrollment` ->
  `create_tasks_for_routed_emails` -> task drain -> `invoke_workflow_agent` (Drive tools, then
  `reply_email` with pre-send `_fact_check_body` rejecting un-grounded numbers) ->
  `record_enrollment_outcome`. Reply is threaded (`In-Reply-To`, same `gmail_thread_id`), subject
  preserved with `Re:` prefix, `multipart/alternative`.
- **Timing**: ~20-50s/reply, 240s agent read-timeout (§V.48). Burst of 4 runs in parallel
  (`max_concurrent_tasks=10`); wall-clock ~25-40s, not 4x. Polling fallback latency = `run_interval`
  (default 60s) when Pub/Sub is unavailable.
- **Logfire** (§V.51-55): `agent.invoke` span carries `input_tokens`, `output_tokens`,
  `total_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `model`, `workflow_id`,
  `contact_id`, `email_id` (the **inbound trigger** email's DB id), `status`, `tool_error_count`,
  `agent_reasoning`. It **roots its own trace** (§V.23 detachment) — 1:1 with a reply.
  `run.execute_task` is parent (`task_id`); `sync.send_email` is a child. Latency =
  `run.execute_task.start` -> `sync.send_email.end`. `deployment_environment` = `development`
  (current) | `production`; `service_name='mailpilot'`. Classifier tokens are on a *separate* trace.

## Architecture

### Directory layout
```
.claude/skills/mailpilot-reply-test/
  SKILL.md                      # orchestration: phases, model assignment, sub-agent boundaries
  scripts/                      # deterministic, no-LLM, compact-JSON I/O (run via `uv run python`)
    new_run_id.py               # print a fresh 8-char run id (portable mint; reuse as a literal)
    preflight.py                # accounts + workflow active + creds/keys present -> preflight.json
    validate_qa.py              # DriveClient(inbound).list_markdown vs QA source_files -> validate_qa.json
    select_cases.py             # pick Run-A/Run-B cases + tagged subjects -> run_manifest.json
    run_loop_start.py           # daemonize `mailpilot run` (start_new_session, MAILPILOT_RUN_INTERVAL=10)
    run_loop_stop.py            # read run.pid, SIGTERM (SIGKILL fallback); idempotent
    send_emails.py              # `email send` per case (Run-B: concurrent) -> sends.json + window_start
    collect_replies.py          # poll `email list/view`, match subject tag, resolve trigger email_id
    score_replies.py            # deterministic grading per QA type -> scoring.json (+ failed flag)
    generate_report.py          # merge all JSON -> report.md (+ short stdout summary)
  references/
    grading.md                  # grading rubric + substring-matching caveats
  assets/QA-Pairs.json          # the test base: 34 in-scope + 11 compare + 5 out-of-scope cases
  evals/evals.json              # skill-creator trigger prompts
.mptest/<run_id>/               # gitignored run artifacts (json, run.log, run.pid, report.md, ...)
```

### Model-orchestration map
```
orchestrator (session model; keeps only distilled summaries)
  Phase 1  SETUP CHECKS -> Sonnet sub-agent: preflight + validate_qa + select_cases
  Phase 1b RUN-LOOP     -> orchestrator, directly: run_loop_start (owns the long-lived process)
  Phase 2  RUN-A        -> Sonnet sub-agent: send(1) + collect + score
  Phase 3  RUN-B        -> Sonnet sub-agent: send(4 concurrent) + collect + score
  Phase 4  ANALYSIS     -> Sonnet sub-agent (logfire MCP): per-trace tokens + latency -> logfire_metrics.json
  Phase 5  TEARDOWN     -> orchestrator, directly: run_loop_stop (ALWAYS, even on phase failure)
  Phase 6  REPORT       -> generate_report.py -> report.md + stdout summary
  Phase 7  ESCALATION   -> only if scoring.failed:
                          Opus sub-agent: logfire root-cause     -> investigation.md
                          Opus sub-agent: solution analysis      -> solutions.md
                          re-run generate_report.py (folds them in)
```
Sub-agents are how we honor "minimize context volume for Opus to analyze": bulky inputs (email
bodies, logfire rows, code) live in the sub-agent's context; each returns a compact verdict so the
orchestrator's window stays small.

### Reply matching (the correlation spine)
- Each test email subject = `[MP-TEST <run_id> <case_id>] <hint>`; body = the QA question.
- Reply subject = `Re: [MP-TEST <run_id> <case_id>] <hint>` (subject preserved) -> regex extracts
  `run_id` + `case_id` -> maps reply to case.
- For Logfire correlation, `collect_replies` resolves the **inbound trigger** email id per case:
  `email list --thread-id <reply.thread_id> --direction inbound` -> that DB id equals
  `agent.invoke.email_id`. Analysis joins Logfire rows on this id for exact per-case tokens/latency.

### Grading (deterministic, in `score_replies.py`; rubric in `references/grading.md`)
Whitespace-normalized, case-insensitive substring matching of the reply body.
- **in-scope**: PASS iff every `expected_tokens` entry is present. Selection prefers cases with
  specific tokens (>=2 tokens of length >=5) so bare short numbers like `"3"` don't yield false
  passes — a known substring-matching limitation, documented in grading.md.
- **out-of-scope**: PASS iff (a) no `forbidden_token_pairs` co-occur — pair `[brand, regex]` fails
  when the brand string AND the regex both hit the body (i.e. a fabricated spec for an absent
  product) — AND (b) >=1 `decline_signal` present.
- **compare**: structural proxy aligned with the workflow's own rules — every `source_file` cited
  (filename basename present), every target's model id mentioned (cross-referenced from the matching
  in-scope entry; selection requires full coverage), and >=1 GFM pipe table present. Deeper
  correctness is left to the analysis note; grading.md states this scope honestly.
- **no reply within timeout** = NO_REPLY (a real failure mode: classifier didn't route, agent
  errored, or loop not draining).

### Logfire analysis (Phase 4, Sonnet via `query_run`)
Filter `service_name='mailpilot'`, `deployment_environment=<from config>` (currently `development`),
window from `window_start`, `attributes->>'email_id' IN (<trigger ids>)`. Per case: `total_tokens`
(+ in/out + cache_read), `model`, and latency = `sync.send_email.end - run.execute_task.start`.
Allows a brief retry for span-export lag. Writes `logfire_metrics.json`.

### Report (`report.md`)
run_id/env/accounts/workflow; QA-vs-Drive validation; Run-A verdict + latency + tokens + per-token
hits; Run-B 4-row table (type/verdict/latency/tokens); aggregates (pass rate, total/avg tokens incl.
cache, avg + max latency, Run-B concurrency = wall-clock vs sum); delivery-performance section.
Failure section folds in `investigation.md` + `solutions.md` when present. `generate_report.py` is
pure formatting; the Opus markdown files are authored separately and included.

### Token-minimization strategy
- All deterministic work (send, poll, view, grade, format) is python scripts -> zero LLM tokens.
- Scripts write compact JSON to `runs/<run_id>/`; sub-agents read those, not raw bodies, and return
  distilled summaries -> orchestrator context stays minimal.
- Sonnet for routine phases; Opus only on failure, and only inside isolated sub-agents.

### Safety / side-effects
- Real Gmail: up to 5 real messages/full run between two real lab5.ca mailboxes.
- `mailpilot run` while up processes **all** accounts and could auto-reply to any genuine inbound
  mail. Skill keeps it up only for the test window and SIGTERMs at teardown; teardown is mandatory
  and idempotent. Document manual fallback `pkill -f "mailpilot run"`.
- No persistent config mutation: faster polling via `MAILPILOT_RUN_INTERVAL=10` in the spawned
  loop's env (env override > config file), not `config set`.
- Daemonization via `start_new_session=True` + stdio to `run.log` so the loop survives the setup
  sub-agent's exit; PID/PGID in `run.pid`.
- No reply-loop: the reply lands in `outbound@lab5.ca`, which has no active workflow -> routing
  `skipped_no_workflows` -> no further reply. This is exactly why "no outbound workflow" is required.
- Same-account guard §V.33 does not block outbound->inbound (different addresses).

## Open questions / future
- `email list` body completeness — design assumes `view` for body; confirmed at build.
- Logfire token must be set for cloud analysis; if absent, analysis degrades gracefully and the
  report notes it (delivery/scoring still work from CLI + DB).
- Optional hardening: TTL watchdog to self-kill an orphaned loop (skipped now for macOS
  `timeout`/`gtimeout` portability; teardown + manual fallback cover it).
