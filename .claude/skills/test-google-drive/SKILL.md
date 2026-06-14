---
name: test-google-drive
description: |
  Drive-KB burst-load oracle for the MailPilot demo. Fires an N=4 burst of mixed questions from outbound@lab5.ca at the demo agent and asserts aggregate Logfire SLA + retry-rate + concurrency gates with zero errors or warnings, scoped per deployment_environment. Runs two variants: a production variant against the deployed lab5.ca/mailpilot/ instance and a development variant against a fresh local setup. Structural health only -- reply content is NOT graded per-message. Use whenever the user says "test google drive", "burst-test the demo", "is the demo healthy under load", "run the Drive KB oracle", or after non-trivial changes to sync, routing, agent execution, KB grounding, or Pub/Sub code.
argument-hint: "[prod|dev] (default: both variants, prod first)"
# Broad Bash: the oracle runs `make clean` + `uv run mailpilot` + `uv run python` (GmailClient impersonation, qa.py) + shell burst/poll loops; per-utility scoping would break the harness. Logfire gates query the MCP directly.
allowed-tools: Bash, Read, mcp__claude_ai_logfire__query_run
model: sonnet
---

# Test Google Drive -- Drive-KB burst-load oracle

## What this tests

The lab5.ca/mailpilot/ demo promises a KB-grounded reply within ~90 seconds. This skill is the **load oracle** for that system: it fires a small burst of mixed questions (in-scope, out-of-scope, compare-and-contrast) at the demo workflow under real concurrency and asserts the aggregate structural health of the run -- per-span SLA, retry rate, concurrency, and zero error/warn spans -- entirely from Logfire, scoped to one `deployment_environment`.

It is an **aggregate-only** oracle (SPEC `§V.57`). Reply *content* is not graded per message; the burst payload exists to exercise all three classifier branches under load, and structural health is measured by the C4 Logfire gates, not by reading any reply body.

## Variants

The skill runs in two variants. Both source the burst from `outbound@lab5.ca`; they differ in target mailbox, environment, and setup posture (SPEC `§V.59`).

| Variant | Target          | `deployment_environment` | Setup posture                                                                                 |
| ------- | --------------- | ------------------------ | --------------------------------------------------------------------------------------------- |
| prod    | `hello@lab5.ca` | `production`             | Warm / non-destructive. NO `make clean`, NO workflow create. The deployed instance owns inbound on `hello@lab5.ca`; no local `mailpilot run` is started. |
| dev     | `inbound@lab5.ca` | `development`          | Full Phase 0: `make clean` + create accounts/contacts/demo-workflow + local `mailpilot run` loop. |

Default (no argument) runs **both**, prod first (while local state is warm), then dev (whose `make clean` rebuilds local state from scratch). Pass `prod` or `dev` to run a single variant.

Each variant fires its own N=4 burst and is judged by C4 Logfire gates filtered on its own `deployment_environment` (SPEC `§V.52`). The two variants' verdicts are independent; the overall result is PASS only if every requested variant passed.

## Conventions

- **ASCII only.** No emojis. Use `->`, `--`, plain pipes.
- All `mailpilot` commands run via `uv run mailpilot`; all helper scripts via `uv run python`.
- **Subject format `[TGD-<HHMMSS>-<i>] <topic>`** (SPEC `§V.59`), freshly randomized per run via Bash. Do NOT invent a topic in your head and do NOT reuse one from a prior run -- LLMs anchor on examples and have been observed copying subjects across runs, which collides Logfire windows. The `-<i>` index suffix keeps the four burst subjects mutually distinct. If `/usr/share/dict/words` is unavailable, fall back to `head -c 12 /dev/urandom | base32 | tr -d '=' | head -c 10`.
- **CLI parsing.** Parse JSON by piping `mailpilot ... | python3 -c '...'` directly. Do NOT round-trip JSON through `echo "$VAR"` -- shell `echo` corrupts `\n` inside `body_text` and breaks parsing. Use `printf '%s' "$VAR"` when a variable is required.
- **Envelope shape (SPEC `§V.4`).** `list|search|sync` -> `{"<plural>": [...], "ok": true}`; `view|create|update|send|reply` -> `{"<singular>": {...}, "ok": true}`. Always extract through the wrap.
- **Latency verdict is Logfire-derived (SPEC `§V.61`).** The reply round-trip poll is a `did-it-come-back?` side-effect check only -- never the latency verdict. All SLA verdicts come from the `agent.invoke` spans in C4.
- **No per-message content grading.** This oracle does not load source docs, does not run `qa.py source` / `qa.py check`, and does not read any reply body. Content fidelity is the smoke-era correctness oracle's job; this skill trusts it and measures structural health under burst.

## Scripts

Located at `.claude/skills/test-google-drive/scripts/`.

- `qa.py pick [--type inscope|outscope|compare] [--id ID]` -- emit one Q/A pair as JSON on stdout (random unless `--id` given; default type `inscope`). This is the **only** `qa.py` mode this skill uses -- the burst-payload generator. The `source` and `check` modes exist for the retired per-message grading path and are not invoked here.
- `qa_pairs.json` -- the corpus `qa.py pick` draws from (in-scope + out-of-scope + compare pairs).
- `generate_qa_pairs.py` -- maintenance regenerator for `qa_pairs.json`, only run when the demo Drive folder content changes.

The demo KB is hand-maintained directly in the demo Drive folder (`1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat`, inside Shared Drive `MailPilot`); there is no repo source-of-truth directory and no sync script.

## Prerequisites

- PostgreSQL running locally.
- `mailpilot config get google_application_credentials` returns a valid path, or ADC reachable per SPEC `§V.37`.
- `mailpilot config get anthropic_api_key` returns a non-empty value (dev variant's local agent needs it).
- Network access to Gmail API, Drive API, Anthropic API, and the Logfire backend.
- Logfire MCP reachable; project = `mailpilot`.

---

## Variant: prod (deployed production, warm)

Run this first when running both. It is non-destructive: it sends from `outbound@lab5.ca` to `hello@lab5.ca` and relies entirely on the deployed production instance to classify, ground, and reply. No `make clean`, no workflow create, no local `mailpilot run`.

### prod pre-flight

```
uv run mailpilot account list
```

Confirm a row with `email == "outbound@lab5.ca"` is present and capture its `id` as `OUTBOUND_ACCOUNT_ID`. If absent:

```
FAIL: outbound@lab5.ca account missing -- run `mailpilot account create --email outbound@lab5.ca --display-name "Outbound"` first
```

and skip the prod variant (do NOT send). The local CLI only persists the outbound send; no demo-workflow state is needed locally because the deployed instance owns inbound.

### prod run

Run the **Burst Procedure** below with:

- `OUTBOUND_ACCOUNT_ID` = the row from pre-flight
- `TARGET` = `hello@lab5.ca`
- `ENV` = `production`
- `WF_PREDICATE` = *(empty -- the local CLI does not know the deployed workflow's id)*
- round-trip mode = **gmail** (poll Gmail directly; there is no local run loop)

---

## Variant: dev (full local setup, development)

This variant rebuilds local state from scratch and runs a local `mailpilot run` loop in the `development` environment, then bursts at `inbound@lab5.ca`.

### dev Phase 0 setup

1. `make clean` -- drops and re-applies the schema; Gmail mailbox contents are untouched.
2. Ensure development-scoped telemetry so the burst's spans carry `deployment_environment='development'` (SPEC `§V.52`):
   ```
   mailpilot config set logfire_environment development
   ```
3. Create accounts (save `OUTBOUND_ACCOUNT_ID`, `INBOUND_ACCOUNT_ID`):
   ```
   mailpilot account create --email outbound@lab5.ca --display-name "Outbound TGD"
   mailpilot account create --email inbound@lab5.ca  --display-name "Inbound TGD (hosts demo workflow)"
   ```
   Both must be delegated through the service account in `google_application_credentials`. If `inbound@lab5.ca` cannot be created, stop -- the dev variant cannot run.
4. Create company + contacts (the demo workflow enrolls the sender):
   ```
   mailpilot company create --domain lab5.ca --name Lab5
   mailpilot contact create --email outbound@lab5.ca --first-name Outbound --last-name TGD --company-id <COMPANY_ID>
   ```
   Save `COMPANY_ID`, `OUTBOUND_CONTACT_ID`.
5. Import the demo inbound workflow (declarative, SPEC `§V.63`; the operator-style instructions cite the real folder id):
   ```
   mailpilot workflow import \
     --account-id <INBOUND_ACCOUNT_ID> \
     --file tests/fixtures/workflows-inbound.json
   ```
   `workflow import` upserts on `(account_id, name)` and auto-activates when objective + instructions are non-empty. Capture the id and pre-enroll the sender:
   ```
   DEMO_WORKFLOW_ID=$(mailpilot workflow list --account-id <INBOUND_ACCOUNT_ID> \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["workflows"][0]["id"])')
   mailpilot enrollment add --workflow-id <DEMO_WORKFLOW_ID> --contact-id <OUTBOUND_CONTACT_ID>
   ```

### dev KB visibility gate

The demo KB lives in Shared Drive `MailPilot` (ID `0AJIvyECg210LUk9PVA`), folder `MailPilot Demo` (ID `1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat`). `inbound@lab5.ca` is a Reader on the Shared Drive; that membership -- not per-file ACL -- makes the files visible to the impersonated user. Verify against the actual subject the agent will use:

```
uv run python -c "
from mailpilot.drive import DriveClient
files = DriveClient('inbound@lab5.ca').list_markdown('1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat')
print(len(files), [f['name'] for f in files])
"
```

Expect **at least 30 markdown files**. If the count is zero or `not_found`, the failure is Drive ACL (`inbound@lab5.ca`'s Shared Drive Reader membership), not KB content -- fix that first. If a specific datasheet is missing, upload the `.md` to the demo folder as `kb@lab5.ca` with `anyoneWithLink:reader`; the folder is hand-maintained, so there is no sync script to re-run. On any failure: stop the dev variant and report the error JSON.

### dev start the sync loop

Start `mailpilot run` in the background via `Bash` with `run_in_background: true`; capture the bash_id. The loop emits curated `event=...` lifecycle lines on stderr regardless of `--debug`.

```
uv run mailpilot --debug run
```

Wait ~3s, read the captured output, confirm:

- `Sync loop started (pid <pid>)` printed.
- `Pub/Sub subscriber started` printed (a `Warning: Pub/Sub setup failed` is acceptable -- periodic sync still works).
- At least one `event=loop.tick` line has appeared.

### dev run

Run the **Burst Procedure** below with:

- `OUTBOUND_ACCOUNT_ID` = from Phase 0
- `TARGET` = `inbound@lab5.ca`
- `ENV` = `development`
- `WF_PREDICATE` = `AND r.attributes->>'workflow_id' = '<DEMO_WORKFLOW_ID>'`
- round-trip mode = **local** (poll the local outbound mailbox; the run loop synced it)

### dev teardown

After the gates are evaluated, SIGTERM the background `mailpilot run` (e.g. `kill <pid>`). Wait for `Sync loop stopped` in the captured output. If it does not exit within 10s, SIGKILL and note it.

---

## Burst Procedure (parameterized)

Run once per variant with the parameters that variant section set. The mix is fixed at N=4 = **2 in-scope / 1 out-of-scope / 1 compare** (SPEC `§V.57` -- exercises all three classifier branches under burst).

### B1. Generate the burst payload

```bash
QA_PY=.claude/skills/test-google-drive/scripts/qa.py

SUBJECTS_BURST=()
for i in $(seq 1 4); do
  TOPIC=$(sort -R /usr/share/dict/words 2>/dev/null \
    | grep -E '^[A-Za-z]{4,9}$' | head -2 | tr '\n' ' ' | sed 's/ *$//')
  [ -z "$TOPIC" ] && TOPIC=$(head -c 12 /dev/urandom | base32 | tr -d '=' | head -c 10)
  SUBJECTS_BURST+=("[TGD-$(date +%H%M%S)-${i}] ${TOPIC}")
done
[ "$(printf '%s\n' "${SUBJECTS_BURST[@]}" | sort -u | wc -l)" -eq 4 ] \
  || { echo "FAIL: subject collision in burst"; exit 1; }

QA_IDS_BURST=()
for i in 1 2; do
  QA_IDS_BURST+=("$(uv run python "$QA_PY" pick --type inscope \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')")
done
QA_IDS_BURST+=("$(uv run python "$QA_PY" pick --type outscope \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')")
QA_IDS_BURST+=("$(uv run python "$QA_PY" pick --type compare \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')")

QUESTIONS_BURST=()
for qa_id in "${QA_IDS_BURST[@]}"; do
  QUESTIONS_BURST+=("$(uv run python "$QA_PY" pick --id "$qa_id" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["question"])')")
done
```

**Gate B1:**

- `SUBJECTS_BURST` has 4 distinct entries (the explicit `sort -u | wc -l` check above must pass).
- `QA_IDS_BURST` has 4 entries: exactly 2 starting `qa-in-`, 1 starting `qa-out-`, 1 starting `qa-cmp-`. A different split means `qa.py pick --type <T>` returned the wrong type -- record as a Bug and stop (the mix is what makes the classifier-under-load verdict meaningful).
- `QUESTIONS_BURST` has 4 entries, none empty.

### B2. Fire 4 sends at P=4

Capture `T_SEND_C` as a single wall-clock anchor immediately before the loop (epoch + ISO refer to the same instant). All 4 sends complete within seconds, so a single anchor is precise enough for the per-span latency derivation in C4.

```bash
T_SEND_C_EPOCH=$(uv run python -c 'import datetime; print(int(datetime.datetime.now(datetime.UTC).timestamp()))')
T_SEND_C=$(uv run python -c "import datetime; print(datetime.datetime.fromtimestamp($T_SEND_C_EPOCH, tz=datetime.UTC).isoformat())")

for i in $(seq 0 3); do
  mailpilot email send \
    --account-id <OUTBOUND_ACCOUNT_ID> \
    --to <TARGET> \
    --subject "${SUBJECTS_BURST[$i]}" \
    --body "${QUESTIONS_BURST[$i]}" >/dev/null &
done
wait
```

**Gate B2:**

- `wait` returns 0 (all 4 background sends exited cleanly). A non-zero return means a Gmail-side or CLI-side failure during the burst -- record which subject(s) failed and stop; the run is invalidated by the failed send(s), not by the system under test.
- `mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction outbound --since $T_SEND_C` returns 4 rows, each with `workflow_id == null` (operator-driven outbound). Any deviation -- extra rows, missing rows, non-null `workflow_id` -- is a separate Bug.

### B3. Round-trip poll (sanity only, cap 240s)

The public SLA is 90s for a single reply; at N=4/P=4 the burst is one wave, so ~240s is a generous ceiling that keeps the poll from false-failing. This poll confirms replies round-trip; it does NOT derive latency (SPEC `§V.61` -- the verdict is the C4 span query).

**round-trip mode = local** (dev variant):

```bash
mailpilot email list --account-id <OUTBOUND_ACCOUNT_ID> --direction inbound --since "$T_SEND_C"
```

Match each row's `subject` against `Re: <one of SUBJECTS_BURST>` (Gmail typically prepends `Re:`; match on the `[TGD-<HHMMSS>-<i>]` bracket substring).

**round-trip mode = gmail** (prod variant -- there is no local run loop; the deployed instance replies from `hello@lab5.ca`). Poll Gmail directly via service-account impersonation of `outbound@lab5.ca` (SPEC `§V.37`), up to 48 attempts 5s apart (~240s):

```bash
FOUND=0
for attempt in $(seq 1 48); do
  FOUND=$(uv run python -c "
from mailpilot.gmail import GmailClient
client = GmailClient('outbound@lab5.ca')
stubs = client.list_messages(query='from:hello@lab5.ca after:$T_SEND_C_EPOCH', label_ids=['INBOX'])
print(len(stubs))
")
  [ "$FOUND" -ge 4 ] && break
  sleep 5
done
```

**Gate B3 (sanity):** 4 replies round-trip within 240s. Fewer than 4 is a strong signal the system dropped or queued a trigger -- but the authoritative verdict is C4 below; record the round-trip shortfall and still run C4 so the summary has signal.

### B4. Logfire aggregate gates (C4) -- the verdict

All gates query `deployment_environment = '<ENV>'`, window `[T_SEND_C, T_SEND_C + 300s]`, `span_name = 'agent.invoke'`, `trigger = 'task'`. The dev variant additionally scopes by `<WF_PREDICATE>`; the prod variant omits it (the deployed workflow id is not known locally -- the burst is identified by env + trigger + window, which assumes the deployed demo system is otherwise quiet during the test). Primary verdict = `sla_agent_seconds` (our-side agent execution) per `§V.61`.

**Gate C4.a -- per-span SLA + token economics (two-budget split, compare vs non-compare):**

```sql
WITH read_counts AS (
  SELECT trace_id, COUNT(*) AS read_count
  FROM records
  WHERE deployment_environment = '<ENV>'
    AND attributes->>'gen_ai.tool.name' = 'read_drive_markdown'
    AND start_timestamp >= '<T_SEND_C>'
    AND start_timestamp <= '<T_SEND_C>'::timestamptz + INTERVAL '300 seconds'
  GROUP BY trace_id
),
burst AS (
  SELECT
    r.attributes->>'email_id' AS email_id,
    r.start_timestamp,
    r.end_timestamp,
    EXTRACT(EPOCH FROM (r.end_timestamp - r.start_timestamp)) AS sla_agent_seconds,
    EXTRACT(EPOCH FROM (r.start_timestamp - TIMESTAMPTZ '<T_SEND_C>')) AS sla_delivery_seconds,
    EXTRACT(EPOCH FROM (r.end_timestamp - TIMESTAMPTZ '<T_SEND_C>')) AS total_latency_s,
    r.is_exception,
    r.level,
    (r.attributes->>'input_tokens')::int AS in_tok,
    (r.attributes->>'output_tokens')::int AS out_tok,
    (r.attributes->>'cache_read_input_tokens')::int AS cache_read,
    COALESCE((r.attributes->>'tool_error_count')::int, 0) AS tool_error_count,
    COALESCE(rc.read_count, 0) >= 2 AS is_compare
  FROM records r
  LEFT JOIN read_counts rc ON rc.trace_id = r.trace_id
  WHERE r.deployment_environment = '<ENV>'
    AND r.span_name = 'agent.invoke'
    AND r.start_timestamp >= '<T_SEND_C>'
    AND r.start_timestamp <= '<T_SEND_C>'::timestamptz + INTERVAL '300 seconds'
    AND r.attributes->>'trigger' = 'task'
    <WF_PREDICATE>
)
SELECT
  COUNT(*) AS n_invokes,
  COUNT(DISTINCT email_id) AS n_distinct_email_ids,
  SUM(CASE WHEN is_compare THEN 1 ELSE 0 END) AS n_compare,
  SUM(CASE WHEN NOT is_compare THEN 1 ELSE 0 END) AS n_noncompare,
  MAX(sla_agent_seconds) FILTER (WHERE NOT is_compare) AS max_sla_agent_noncompare_s,
  approx_percentile_cont(sla_agent_seconds, 0.95) FILTER (WHERE NOT is_compare) AS p95_sla_agent_noncompare_s,
  MAX(sla_agent_seconds) FILTER (WHERE is_compare) AS max_sla_agent_compare_s,
  MAX(sla_delivery_seconds) AS max_sla_delivery_s,
  approx_percentile_cont(sla_delivery_seconds, 0.95) AS p95_sla_delivery_s,
  MAX(total_latency_s) AS max_total_s,
  SUM(CASE WHEN is_exception THEN 1 ELSE 0 END) AS n_exceptions,
  SUM(CASE WHEN level = 'warn' THEN 1 ELSE 0 END) AS n_warns,
  SUM(tool_error_count) AS n_tool_errors,
  SUM(tool_error_count)::float / NULLIF(COUNT(*)::float, 0) AS retry_rate,
  AVG(cache_read::float / NULLIF(in_tok::float, 0)) AS avg_cache_hit_ratio,
  SUM(in_tok) AS total_in_tok,
  SUM(out_tok) AS total_out_tok
FROM burst;
```

Gated assertions (all must hold):

- `n_invokes == 4` AND `n_distinct_email_ids == 4` -- no merged or dropped triggers (SPEC `§V.26` / `§T.63`: one span per inbound email). On the **prod** variant, `n_invokes > 4` means other lab5.ca/mailpilot/ demo traffic shared the window -- re-run during a quiet period; this is an environment caveat, not a system failure.
- `n_compare == 1` AND `n_noncompare == 3` -- matches the C1 mix (1 qa-cmp + 2 qa-in + 1 qa-out). A mismatch means a compare invocation skipped a required read OR a non-compare invocation issued a stray second read; cross-check against C4.c before flagging.
- `p95_sla_agent_noncompare_s <= 75` -- burst gate over non-compare invocations (SPEC `§V.61`; the `§V.23` burst-load tolerance over the 50s steady single-source ceiling). A breach is an our-side regression of agent execution under load.
- `p95_sla_delivery_s <= 75` -- per-variant burst delivery gate (SPEC `§V.69`). The event-driven full-sweep-on-classify (`§V.69`: a tick that classifies >=1 inbound forces the next tick's full sweep + sets `wakeup_event`) is what bounds delivery under burst; a breach means that re-sweep mechanism regressed.
- `n_exceptions == 0` AND `n_warns == 0` (SPEC `§V.59` -- zero error/warn scoped to this variant's env).
- `retry_rate <= 0.05` -- `§V.70` burst retry-rate ceiling. At N=4 any non-zero `n_tool_errors` already exceeds the ceiling (1/4 = 0.25 > 0.05), so this gate is effectively `n_tool_errors == 0`. A breach signals a prompt-fidelity regression under load -- investigate `§V.41` (search-first ordering), `§V.42` (format-lint sensitivity), `§V.68` (fact-check). Measured separately for prod and dev.
- `avg_cache_hit_ratio >= 0.5` -- prompt cache stays warm across the burst (SPEC `§V.47`; catches cache-key churn where each invocation re-pays the full system-prompt token cost).

Report (NOT gated):

- `max_sla_agent_compare_s` -- compare-type advisory ceiling 120s (`§B.62`: 2-datasheet synthesis structurally exceeds the single-source band). Trend-escalate on run-over-run drift past 120s; do NOT fail on a single breach.
- `max_sla_delivery_s` is reported alongside the gated p95 for trend continuity.
- `max_total_s`, `total_in_tok`, `total_out_tok` -- end-to-end and token totals for run-over-run trend.

**Gate C4.b -- concurrency proof (no serialization regression):**

```sql
WITH read_counts AS ( /* same CTE as C4.a */ ),
burst AS ( /* same CTE as C4.a */ )
SELECT COUNT(*) AS overlap_pairs
FROM burst a, burst b
WHERE a.email_id < b.email_id
  AND a.start_timestamp < b.end_timestamp
  AND b.start_timestamp < a.end_timestamp;
```

Assert `overlap_pairs >= 2`. With N=4 fired in one wave, max possible overlap is C(4,2)=6; a floor of 2 is generous enough that only strict serialization (drain-layer pool regression, SPEC `§V.23`) fails it. A failure means the dispatcher serialized invocations -- record as a Critical Bug, since it defeats the burst-load oracle.

**Gate C4.c -- Drive race signatures absent (`§B.34`):**

```sql
SELECT MAX(EXTRACT(EPOCH FROM (end_timestamp - start_timestamp))) AS max_dur_s,
       SUM(CASE WHEN is_exception THEN 1 ELSE 0 END) AS n_exc
FROM records
WHERE deployment_environment = '<ENV>'
  AND attributes->>'gen_ai.tool.name' = 'read_drive_markdown'
  AND start_timestamp >= '<T_SEND_C>'
  AND start_timestamp <= '<T_SEND_C>'::timestamptz + INTERVAL '300 seconds';
```

Assert:

- `max_dur_s < 60` -- the `§B.34` 60s socket-timeout signature is absent. A 60s+ `read_drive_markdown` span under burst means the structural `sequential=True` Drive-tool registration (SPEC `§V.38`) regressed across concurrent agent invocations. Record as a Critical Bug.
- `n_exc == 0` -- no unhandled exceptions escaped the Drive tool wrappers.

---

## Output contract

For **each** variant run, print one PASS/FAIL line followed by a short aggregate-metrics block, then a final overall line. Nothing else.

Per-variant line:

- `PASS [<variant>/<ENV>]` -- every C4 gate held.
- `FAIL [<variant>/<ENV>]: <one-line reason naming the failing gate>` -- e.g. `n_invokes=3 (dropped trigger)`, `p95_sla_agent_noncompare=88s exceeds 75s`, `p95_sla_delivery=91s exceeds 75s (V69)`, `retry_rate=0.25 (n_tool_errors=1)`, `n_warns=2`, `overlap_pairs=0 (serialized)`, `read_drive_markdown max_dur=61s (B34)`.

Per-variant metrics block (header `C4 <ENV> window <T_SEND_C>..+300s:`), 4 bullets:

- `invokes: <n_invokes> (distinct email_ids <n>), mix <n_noncompare> non-compare / <n_compare> compare`
- `sla_agent p95(non-compare) <p95>s / max(compare) <max>s ; sla_delivery p95 <p95>s`
- `errors/warns: <n_exceptions>/<n_warns> ; retry_rate <retry_rate> ; overlap_pairs <n>`
- `cache_hit <avg_cache_hit_ratio> ; tokens in/out <total_in_tok>/<total_out_tok> ; drive max_dur <max_dur_s>s`

Final line: `OVERALL PASS` if every requested variant passed, else `OVERALL FAIL: <which variant(s) failed>`.

Do NOT:

- Write any `.md` file to the repo. This oracle is chat-only.
- Auto-invoke `/sdd:spec`.
- Render a phase matrix, §1/§2/§3 sections, or read/grade any reply body. Structural health is the C4 aggregate, not message content.
- Derive latency from the round-trip poll (SPEC `§V.61`).

## On FAIL

This skill is a gate, not a debugger. If a variant FAILs, the operator's next move is `/logfire:debug` to drill into the failing `[T_SEND_C, T_SEND_C+300s]` window with full span context, or to inspect the deployed instance (prod variant) / the local `mailpilot run` capture (dev variant). This skill does not retry, does not amend the spec, and does not auto-file an issue.

## Spec references

- SPEC `§V.52` -- `logfire.configure(environment=...)` so spans carry `deployment_environment`; each variant filters its own.
- SPEC `§V.57` -- burst payload from `qa.py pick`, three-branch mix; aggregate-only, content not graded per-message.
- SPEC `§V.59` -- this skill's contract: 2 variants from `outbound@lab5.ca`, prod warm/non-destructive vs dev full Phase 0, `[TGD-<HHMMSS>-<i>]` subjects, PASS = aggregate C4 + zero error/warn per env. Gate detail: `.claude/check-extras.md` `§V.59`.
- SPEC `§V.61` -- latency verdict from `agent.invoke` spans (CLI poll = round-trip only); two-budget `sla_agent`/`sla_delivery` split. Thresholds: `.claude/check-extras.md` `§V.61`.
- SPEC `§V.69` -- event-driven full sweep on classify; N=4 per-variant burst `T_delivery <= 75s`.
- SPEC `§V.70` -- burst retry-rate `<= 5%` per variant, prod + dev measured separately. Measurement detail: `.claude/check-extras.md` `§V.70`.
- SPEC `§V.23` / `§V.26` / `§V.38` / `§V.47` -- concurrent drain pool, one span per inbound email, sequential Drive dispatch, prompt-cache attrs (C4.b / C4.a / C4.c gates).
- SPEC `§V.37` -- Gmail credential construction via `GmailClient("outbound@lab5.ca")` (prod round-trip poll).
- SPEC `§V.63` -- declarative `workflow import` (dev Phase 0 demo workflow).
