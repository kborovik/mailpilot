---
name: test-google-drive
description: |
  Drive-KB burst-load oracle for the MailPilot demo. Fires an N=4 burst of mixed questions from outbound@lab5.ca at the demo agent and asserts aggregate Logfire SLA + retry-rate + concurrency gates with zero errors or warnings, scoped per deployment_environment. Default runs the development variant against a fresh local setup; the production variant (deployed lab5.ca/mailpilot/ instance) and both are opt-in. Structural health only -- reply content is NOT graded per-message. Use whenever the user says "test google drive", "burst-test the demo", "is the demo healthy under load", "run the Drive KB oracle", or after non-trivial changes to sync, routing, agent execution, KB grounding, or Pub/Sub code.
argument-hint: "[dev|prod|both] (default: dev)"
# Broad Bash: the oracle runs `make clean` + `uv run mailpilot` + `uv run python` (burst.py, GmailClient impersonation, qa.py) + a background run loop; per-utility scoping would break the harness. Logfire gates query the MCP directly.
allowed-tools: Bash, Read, mcp__claude_ai_logfire__query_run
model: sonnet
---

# Test Google Drive -- Drive-KB burst-load oracle

## What this tests

The lab5.ca/mailpilot/ demo promises a KB-grounded reply within ~90 seconds. This skill is the
**load oracle** for that system: it fires a small burst of mixed questions (in-scope,
out-of-scope, compare-and-contrast) at the demo workflow under real concurrency and asserts the
aggregate structural health of the run -- per-span SLA, retry rate, concurrency, and zero
error/warn spans -- entirely from Logfire, scoped to one `deployment_environment` (SPEC §V.52).

It is an **aggregate-only** oracle (SPEC §V.57). Reply *content* is not graded per message; the
burst payload exists to exercise all three classifier branches under load, and structural health
is measured by the C4 Logfire gates, not by reading any reply body. **C4 gate logic and the
per-variant PASS/FAIL verdict are behavior-invariant** -- the gates alone decide (SPEC §V.59).

## Variants and argument

One phase-numbered pipeline runs per variant. Both source the burst from `outbound@lab5.ca`;
they differ only in **Phase 0** (setup), **Phase 5** (teardown), and three parameters. Phases 1-4
are byte-shared (SPEC §V.59).

| Parameter        | prod (opt-in)              | dev (DEFAULT)                                    |
| ---------------- | -------------------------- | ------------------------------------------------ |
| `TARGET`         | `hello@lab5.ca`            | `inbound@lab5.ca`                                 |
| `ENV`            | `production`               | `development`                                     |
| `WF_PREDICATE`   | *(empty)*                  | `AND r.attributes->>'workflow_id' = '<DEMO_WORKFLOW_ID>'` |
| `burst --mode`   | `gmail`                    | `local`                                           |
| Phase 0 setup    | pre-flight only (no mutation) | full setup + KB gate + run loop                |
| Phase 5 teardown | none                       | SIGTERM the run loop                              |

**Argument `[dev|prod|both]`, default `dev`:**

- no argument or `dev` -- run the **dev** variant only (validates the local development build).
- `prod` -- run the **prod** variant only (the deployed production instance).
- `both` -- run **prod then dev** (warm ordering: prod first while local state is warm, then
  dev's `make clean` rebuilds local state from scratch).

Each variant is judged by C4 Logfire gates filtered on its own `deployment_environment`; the
verdicts are independent, and `OVERALL PASS` requires every requested variant to pass.

## Conventions

- **ASCII only.** No emojis. Use `->`, `--`, plain pipes, `ok`/`!!` gate marks.
- All `mailpilot` commands run via `uv run mailpilot`; all helper scripts via `uv run python`.
- **CLI parsing.** Parse JSON by piping `mailpilot ... | python3 -c '...'`. Do NOT round-trip
  JSON through `echo "$VAR"` -- shell `echo` corrupts `\n` inside `body_text`. Use `printf '%s'`.
- **Envelope shape (SPEC §V.4).** `list|search|sync` -> `{"<plural>": [...], "ok": true}`;
  `view|create|update|send|reply` -> `{"<singular>": {...}, "ok": true}`. Extract through the wrap.
- **Latency verdict is Logfire-derived (SPEC §V.61).** The reply round-trip poll (inside
  `burst.py`) is a `did-it-come-back?` sanity check only -- never the latency verdict. All SLA
  verdicts come from the `agent.invoke` spans in Phase 2.
- **No per-message content grading.** This oracle does not load source docs, does not run
  `qa.py source` / `qa.py check`, and does not read any reply body (SPEC §V.57).

## Scripts

Located at `.claude/skills/test-google-drive/scripts/`.

- `burst.py --account-id <id> --target <addr> --env <env> --mode {local|gmail} [--n 4]` --
  the deterministic burst driver (Phase 1): payload-gen + P=N concurrent fire + self-heal resend
  + round-trip poll. Emits one JSON object; `--mode` absorbs the prod/dev poll divergence.
- `qa.py pick [--type inscope|outscope|compare] [--id ID]` -- emit one Q/A pair as JSON
  (`burst.py` calls this for the burst payload). The `source` / `check` modes belong to the
  retired per-message grading path and are NOT used here.
- `qa_pairs.json` -- the corpus `qa.py pick` draws from.
- `generate_qa_pairs.py` -- maintenance regenerator, only run when the demo Drive folder changes.

The demo KB is hand-maintained directly in the demo Drive folder
(`1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat`, inside Shared Drive `MailPilot`); there is no repo
source-of-truth directory and no sync script.

## Prerequisites

- PostgreSQL running locally.
- `mailpilot config get google_application_credentials` returns a valid path, or ADC reachable
  per SPEC §V.37.
- `mailpilot config get anthropic_api_key` returns a non-empty value (dev's local agent needs it).
- Network access to Gmail API, Drive API, Anthropic API, and the Logfire backend.
- Logfire MCP reachable; project = `mailpilot`.

---

## Pipeline (per variant)

For each requested variant (`dev` by default; `prod` and `both` opt-in), run Phases 0-5 below
with that variant's parameter column. Phases 1-4 are identical across variants; only Phase 0 and
Phase 5 diverge.

## Phase 0 -- Setup

**prod (pre-flight only, no mutation).** Confirm the source account exists:

```
uv run mailpilot account list
```

Capture the row with `email == "outbound@lab5.ca"` as `OUTBOUND_ACCOUNT_ID`. If absent:

```
FAIL: outbound@lab5.ca account missing -- run `mailpilot account create --email outbound@lab5.ca --display-name "Outbound"` first
```

and skip the prod variant (do NOT send). The deployed instance owns inbound on `hello@lab5.ca`;
no local workflow state or run loop is needed.

**dev (full setup).** First materialize the catalog submodule -- Step 5 imports from it and a
bare clone lacks the file until then (SPEC §V.103):

```
git submodule update --init
```

1. `make clean` -- drops and re-applies the schema; Gmail mailbox contents are untouched.
2. Scope telemetry to development so the burst's spans carry
   `deployment_environment='development'` (SPEC §V.52): `mailpilot config set logfire_environment development`.
3. Create accounts (save `OUTBOUND_ACCOUNT_ID`, `INBOUND_ACCOUNT_ID`); both MUST delegate through
   the service account in `google_application_credentials`. If `inbound@lab5.ca` cannot be
   created, stop -- the dev variant cannot run.
   ```
   mailpilot account create --email outbound@lab5.ca --display-name "Outbound TGD"
   mailpilot account create --email inbound@lab5.ca  --display-name "Inbound TGD (hosts demo workflow)"
   ```
4. Create company + the contact the demo workflow enrolls (save `COMPANY_ID`, `OUTBOUND_CONTACT_ID`):
   ```
   mailpilot company create --domain lab5.ca --name Lab5
   mailpilot contact create --email outbound@lab5.ca --first-name Outbound --last-name TGD --company-id <COMPANY_ID>
   ```
5. Import the demo inbound workflow from the catalog submodule (declarative, SPEC §V.63/§V.103),
   then capture its id and pre-enroll the sender:
   ```
   mailpilot workflow import --account-id <INBOUND_ACCOUNT_ID> --file workflows/demo-lab5-mailpilot.toml
   DEMO_WORKFLOW_ID=$(mailpilot workflow list --account-id <INBOUND_ACCOUNT_ID> \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["workflows"][0]["id"])')
   mailpilot enrollment add --workflow-id <DEMO_WORKFLOW_ID> --contact-id <OUTBOUND_CONTACT_ID>
   ```
   `workflow import` upserts on `(account_id, name)` and auto-activates when objective +
   instructions are non-empty. `DEMO_WORKFLOW_ID` fills `WF_PREDICATE` for Phase 2.

   **KB visibility gate.** The demo KB lives in Shared Drive `MailPilot` (ID `0AJIvyECg210LUk9PVA`),
   folder `MailPilot Demo` (ID `1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat`). `inbound@lab5.ca` is a Reader
   on the Shared Drive; that membership -- not per-file ACL -- makes the files visible to the
   impersonated user. Verify against the actual subject the agent will use:
   ```
   uv run python -c "
   from mailpilot.drive import DriveClient
   files = DriveClient('inbound@lab5.ca').list_markdown('1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat')
   print(len(files), [f['name'] for f in files])
   "
   ```
   Expect **at least 30 markdown files**. If the count is zero or `not_found`, the failure is
   Drive ACL (`inbound@lab5.ca`'s Shared Drive Reader membership), not KB content -- fix that
   first. If a specific datasheet is missing, upload the `.md` to the demo folder as `kb@lab5.ca`
   with `anyoneWithLink:reader`; the folder is hand-maintained, so there is no sync script to
   re-run. On any failure: stop the dev variant and report the error JSON.

6. **Start the sync loop.** Start `mailpilot run` in the background via `Bash` with
   `run_in_background: true`; capture the bash_id. The loop emits curated `event=...` lifecycle
   lines on stderr regardless of `--debug`.
   ```
   uv run mailpilot --debug run
   ```
   Wait ~3s, read the captured output, confirm: `Sync loop started (pid <pid>)`,
   `Pub/Sub subscriber started` (a `Warning: Pub/Sub setup failed` is acceptable -- periodic sync
   still works), and at least one `event=loop.tick` line.

## Phase 1 -- Burst

Run the deterministic burst driver with the variant's parameters (it owns payload-gen + P=4
concurrent fire + self-heal resend + round-trip poll). It polls up to ~240s, so allow a generous
Bash timeout:

```
uv run python .claude/skills/test-google-drive/scripts/burst.py \
  --account-id <OUTBOUND_ACCOUNT_ID> --target <TARGET> --env <ENV> --mode <local|gmail>
```

Parse the single JSON object it prints, then gate:

- **Exit non-zero or `fatal` set** -- a setup artifact, not the system under test (subject
  collision, wrong classifier mix, or `< 4` outbound rows persisted after self-heal). Stop this
  variant and report `FAIL [<variant>/<ENV>]: <fatal>`; do NOT run Phase 2.
- **Exit 0** -- capture `t_send_c`, `t_send_c_epoch`, `subjects`, `resent` (the self-heal resend
  set -- Phase 4 uses it to tell artifact from regression). `round_trip < 4` is a sanity
  shortfall, NOT fatal: note it and still run Phase 2 (the authoritative verdict is C4).

## Phase 2 -- Gates (C4)

Load `references/gates.md` and run gates **G.a / G.b / G.c** with
`mcp__claude_ai_logfire__query_run` (project `mailpilot`), substituting `<ENV>`, `<T_SEND_C>`
(from Phase 1), and `<WF_PREDICATE>` per the variant's parameter column (dev fills it with
`DEMO_WORKFLOW_ID`; prod leaves it empty). The returned rows ARE the verdict. PASS = every gated
assertion in `references/gates.md` holds; the per-gate thresholds and owning §V live there, not
inlined here (SPEC §V.100). Primary verdict = `sla_agent_seconds` per SPEC §V.61.

## Phase 3 -- Gate report

Emitted **every run** (PASS or FAIL), **per variant**, from the C4 outputs. It is **advisory**
(it never alters the verdict -- the C4 gates alone decide, SPEC §V.59), **chat-only** (no `.md`
artifact), and **span-free**: structural/timing aggregates only, never any reply body, `email`/
`trace` id, or per-invoke timeline (SPEC §V.57). One row per C4 gate: `mark  gate  measured
threshold  §V`, where `ok` = held and `!!` = breach. Header line carries the C4-AND verdict.

```
report [<variant>/<ENV>]: <PASS|FAIL>
  ok  n_invokes          4        == 4          §V.26
  ok  branch split       3nc/1c   3nc/1c        §V.27
  ok  p95_sla_agent      41s      <= 75         §V.61
  !!  p95_sla_delivery   88s      <= 75         §V.69   <- breach
  ok  warns / errors     0 / 0    == 0          §V.59
  ok  tool_errors        0nc/0c   nc==0,c<=2    §V.70
  ok  cache_hit          0.81     >= 0.5        §V.47
  ok  overlap_pairs      3        >= 2          §V.23
  ok  read_drive max_dur 12s      < 60          §V.38
```

The per-gate measured/threshold/§V mapping is in `references/gates.md` ("Per-gate Gate-report
mapping"). Keep it terse; do NOT write it to a file, do NOT let it change the verdict, do NOT
grade reply content, and do NOT print any span / `email` id / `trace` id / timeline.

## Phase 4 -- On FAIL: investigate + remedy

**Only when this variant FAILed.** Load `references/investigate.md` and auto-investigate the
**current-run** Logfire records yourself (same `mcp__claude_ai_logfire__query_run` access, window
`[<T_SEND_C>, <T_SEND_C> + 300s]`, scoped to `<ENV>` -- no manual `/logfire:debug`, no separate
debugger). Span detail (`email`/`trace` ids, the per-invoke timeline) lives there, FAIL-only --
this is the only place it appears. Per the investigate.md steps: attribute each breach to its
failing span(s) + owning §V, run the timeline query, map breach -> inspect -> remedy target, and
emit one paste-ready `/sdd:spec`-ready remedy block per breached gate under a `## Next` heading.

A `sla_delivery` breach whose subject is in the Phase 1 `resent[]` self-heal set is a
**self-heal-timing artifact** (advisory, not a §V.69 regression) -- drop it from the remedy. The
investigation does not retry the burst, does not amend the spec, and does not auto-file an issue
(SPEC §V.59 / §V.57). The remedy block is advisory -- it never alters the PASS/FAIL verdict.

## Phase 5 -- Teardown

**dev only.** After the gates are evaluated, SIGTERM the background `mailpilot run` (e.g.
`kill <pid>`). Wait for `Sync loop stopped` in the captured output. If it does not exit within
10s, SIGKILL and note it. (prod started no loop, so it has no teardown.)

---

## Output contract

Per variant, in order: the **Phase 3 Gate report** (its header line is the variant's PASS/FAIL
verdict), then -- on FAIL only -- the **Phase 4 `## Next`** remedy block. After all variants,
one final line:

- `OVERALL PASS` -- every requested variant passed.
- `OVERALL FAIL: <which variant(s) failed>` -- otherwise.

Do NOT:

- Write any `.md` file to the repo. This oracle is chat-only.
- Auto-invoke `/sdd:spec` (the Phase 4 remedy is paste-ready, not auto-run).
- Render a phase matrix, §1/§2/§3 sections, or read/grade any reply body. (The `## Phase`
  headers above structure the *procedure*; the *chat output* is the span-free Gate report plus
  the FAIL-only remedy -- not a phase matrix.) Structural health is the C4 aggregate.
- Print any Logfire span / `email` id / `trace` id in the every-run output (span detail is
  Phase-4 / FAIL-only, SPEC §V.59).
- Derive latency from the round-trip poll (SPEC §V.61).

## Spec references

- SPEC §V.52 -- `logfire.configure(environment=...)` so spans carry `deployment_environment`;
  each variant filters its own.
- SPEC §V.57 -- burst payload from `qa.py pick`, three-branch mix; aggregate-only, content not
  graded per message; no `.md` artifact.
- SPEC §V.59 -- this skill's contract: phase-numbered body, 2 variants from `outbound@lab5.ca`
  (prod warm/non-destructive vs dev full Phase 0), `[dev|prod|both]` default dev,
  `[TGD-<HHMMSS>-<i>]` subjects, burst owned by `scripts/burst.py`, PASS = aggregate C4 + zero
  error/warn per env, span-free Gate report every run, FAIL-only auto-investigation ->
  `/sdd:spec`-ready remedy. Gate SQL: `references/gates.md`; FAIL-only investigation SQL + remedy
  template: `references/investigate.md` (kept there per SPEC §V.100 progressive-disclosure).
- SPEC §V.61 -- latency verdict from `agent.invoke` spans (CLI poll = round-trip only);
  two-budget `sla_agent`/`sla_delivery` split. Thresholds: `.claude/check-extras.md` §V.61.
- SPEC §V.69 -- event-driven full sweep on classify; N=4 per-variant burst `T_delivery <= 75s`.
- SPEC §V.70 -- per-branch burst retry-rate: non-compare `== 0`, compare `<= 2` self-correcting
  fact-check re-drafts; flat `<= 5%` ratio governs larger N. Prod + dev measured separately.
  Measurement detail: `.claude/check-extras.md` §V.70.
- SPEC §V.23 / §V.26 / §V.38 / §V.47 -- concurrent drain pool, one span per inbound email,
  sequential Drive dispatch, prompt-cache attrs (G.b / G.a / G.c gates).
- SPEC §V.37 -- Gmail credential construction via `GmailClient("outbound@lab5.ca")` (prod
  round-trip poll, in `burst.py`).
- SPEC §V.63 / §V.103 -- declarative `workflow import` from the `workflows/` catalog submodule
  (dev Phase 0 demo workflow).
