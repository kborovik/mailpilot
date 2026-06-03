---
name: demo-test
description: |
  Liveness probe of the public lab5.ca/mailpilot/ system. Sends one KB-grounded question from outbound@lab5.ca to hello@lab5.ca, waits for the production-deployed agent to reply (within ~60s), and asserts the required Logfire spans fired in the `production` deployment_environment with zero errors or warnings. Output is a single PASS / FAIL line plus a 3-bullet Logfire summary -- no detailed report, no auto-write to disk, no `/sdd:spec` invocation. Assumes warm state (the demo workflow already runs on hello@lab5.ca in production); does NOT `make clean`, does NOT create accounts or workflows. Use whenever the user asks to verify the demo, says "demo test", "is the demo alive?", "check lab5.ca/mailpilot/", "demo liveness", or after a production deploy of MailPilot when a quick spot-check is wanted.
model: sonnet
---

# Demo Test

## What this tests

The public demo at https://lab5.ca/mailpilot// promises: email a question to `hello@lab5.ca`, get a KB-grounded reply within ~60 seconds. This skill exercises exactly that promise against the deployed production instance.

It is a **liveness probe**, not a regression suite -- that is `/smoke-test`. Use `/demo-test` after a production deploy, or as a quick "is the demo alive?" spot-check.

## What this does NOT do

- Does NOT run `make clean`.
- Does NOT create accounts, contacts, or workflows. The demo workflow must already exist on `hello@lab5.ca` in production.
- Does NOT start a local `mailpilot run` loop. The production deployment handles all inbound processing on `hello@lab5.ca`.
- Does NOT test the out-of-scope decline path -- in-scope only.
- Does NOT test outbound workflows -- Scenario A from `/smoke-test` is out of scope here.
- Does NOT auto-write a report file to disk.
- Does NOT auto-invoke `/sdd:spec`.

## Conventions

- ASCII only.
- All `mailpilot` commands run via `uv run mailpilot`.
- Parse JSON output by piping `mailpilot ... | python3 -c '...'` directly. Do NOT round-trip JSON through `echo "$VAR"` -- shell `echo` corrupts `\n` inside `body_text` and breaks parsing. Use `printf '%s' "$VAR"` when a variable is required.
- Envelope shape per SPEC `§V.4`: `list|search|sync` -> `{"<plural>": [...], "ok": true}`; `view|send|...` -> `{"<singular>": {...}, "ok": true}`. Extract through the wrap.

## Prerequisites

- `mailpilot` installed locally with config pointing at a local database (any -- the local CLI only persists the outbound send; no demo-workflow state is needed locally).
- `mailpilot config get google_application_credentials` returns a valid path, or ADC reachable per SPEC `§V.37`.
- Network access to Gmail API, Drive API (for `qa.py source`), and the Logfire backend.
- Logfire MCP reachable; project = `mailpilot`.

## Procedure

### Step 1: Pre-flight

```
uv run mailpilot account list
```

Confirm a row with `email == "outbound@lab5.ca"` is present. If absent:

```
FAIL: outbound@lab5.ca account missing -- run `mailpilot account create --email outbound@lab5.ca --display-name "Outbound"` first
```

and stop. Do NOT send.

Capture `OUTBOUND_ACCOUNT_ID` from the row's `id`.

### Step 2: Pick an in-scope question

Reuse the smoke-test QA helper verbatim -- this skill does not own its own scripts:

```
QA_JSON=$(uv run python .claude/skills/smoke-test/scripts/qa.py pick --type inscope)
QA_ID=$(printf '%s' "$QA_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
QUESTION=$(printf '%s' "$QA_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["question"])')
SOURCE_FILE=$(printf '%s' "$QA_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["source_file"])')
```

### Step 3: Generate a fresh subject and capture TEST_START

Subject prefix `[DEMO-...]` is distinct from `/smoke-test`'s `[ST-...]` so traces never collide if both run on the same day.

```
TOPIC=$(sort -R /usr/share/dict/words 2>/dev/null \
  | grep -E '^[A-Za-z]{4,9}$' | head -2 | tr '\n' ' ' | sed 's/ *$//')
[ -z "$TOPIC" ] && TOPIC=$(head -c 12 /dev/urandom | base32 | tr -d '=' | head -c 10)
SUBJECT="[DEMO-$(date +%H%M%S)] ${TOPIC}"

TEST_START_EPOCH=$(uv run python -c 'import datetime; print(int(datetime.datetime.now(datetime.UTC).timestamp()))')
TEST_START=$(uv run python -c "import datetime; print(datetime.datetime.fromtimestamp($TEST_START_EPOCH, tz=datetime.UTC).isoformat())")
```

`TEST_START_EPOCH` is the Unix-seconds form consumed by Gmail's `after:` search operator in Step 5; `TEST_START` is the ISO form used in Logfire SQL windows in Steps 7 and 8. Both refer to the same instant (the ISO is derived from the epoch).

Generate `TOPIC` via Bash per run. Do NOT invent a topic in your head and do NOT reuse one from a prior run -- LLMs anchor on examples and have been observed copying the same subject across runs, which collides Logfire windows.

### Step 4: Send to hello@lab5.ca

```
uv run mailpilot email send \
  --account-id "$OUTBOUND_ACCOUNT_ID" \
  --to hello@lab5.ca \
  --subject "$SUBJECT" \
  --body "$QUESTION"
```

Capture `OUTBOUND_EMAIL_ID` from `.email.id` in the response envelope.

If the send fails:

```
FAIL: mailpilot email send failed -- <one-line stderr summary>
```

and stop. The production instance never saw the question; Logfire would be empty for this window.

### Step 5: G1 -- reply round-trip + Logfire latency verdict

Per SPEC `§V.61`, the 60s latency verdict is derived from the production `agent.invoke` span in Logfire (Step 7 query already runs there); the CLI poll here is a `did-round-trip?` side-effect check only, capped at 120s (24 attempts × 5s) so a borderline reply does not false-fail the round-trip check.

Poll Gmail directly via service-account impersonation of `outbound@lab5.ca` (per SPEC `§V.37` and `§V.60` -- liveness probes must hit the production-facing surface, not the local mailpilot DB which would require a separately-started `mailpilot run` to stay fresh). Up to 24 attempts, 5s apart (~120s):

```
TAIL="${SUBJECT#*] }"
REPLY_ID=""
REPLY_BODY=""
for i in $(seq 1 24); do
  HIT_JSON=$(uv run python -c "
import json
from mailpilot.gmail import GmailClient, extract_text_from_message

client = GmailClient('outbound@lab5.ca')
stubs = client.list_messages(
    query='from:hello@lab5.ca after:$TEST_START_EPOCH',
    label_ids=['INBOX'],
)
for stub in stubs:
    full = client.get_message(stub['id'])
    if full is None:
        continue
    headers = {h['name'].lower(): h['value'] for h in full.get('payload', {}).get('headers', [])}
    if '$TAIL' in headers.get('subject', ''):
        print(json.dumps({'id': stub['id'], 'body': extract_text_from_message(full)}))
        break
")
  if [ -n "$HIT_JSON" ]; then
    REPLY_ID=$(printf '%s' "$HIT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
    REPLY_BODY=$(printf '%s' "$HIT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["body"])')
    break
  fi
  sleep 5
done
```

`REPLY_ID` is the Gmail message id (used only for trace/log; Step 6 consumes `REPLY_BODY` directly). Match by the unique `<topic>` tail since Gmail typically prepends `Re: ` to the subject.

If `REPLY_ID` is empty after ~120s: this is a **G1 FAIL** -- record `no reply round-trip within 120s`, but still run Step 7 (Logfire) so the summary has signal.

Otherwise, query Logfire for the production agent.invoke span's `end_timestamp` and compute latency from `T_SEND` (the wall-clock instant captured pre-`email send` in Step 3, equivalent to `TEST_START` here):

```sql
SELECT EXTRACT(EPOCH FROM (end_timestamp - TIMESTAMPTZ '$TEST_START')) AS latency_s
FROM records
WHERE deployment_environment = 'production'
  AND span_name = 'agent.invoke'
  AND start_timestamp >= '$TEST_START'
  AND attributes->>'trigger' = 'task'
ORDER BY start_timestamp
LIMIT 1
```

`latency_s > 60` -> **G1 FAIL** -- record `agent latency=<latency_s>s exceeds 60s SLA`. Zero rows means the production deploy never processed the trigger; G1 FAIL with `no production agent.invoke span in window`. The 60s SLA verdict is the Logfire row, not the CLI poll cap.

### Step 6: G2 -- operator-judged groundedness

Load the source document the agent should have grounded against. The loader impersonates `inbound@lab5.ca` and reads from the production demo Drive folder:

```
uv run python .claude/skills/smoke-test/scripts/qa.py source --id "$QA_ID"
```

If the loader exits non-zero, the source doc is absent from the live folder -- this is a KB-drift signal and counts as **G2 FAIL** (the pair points at a doc the agent could not have grounded in either; replace the QA pair or restore the doc).

The reply body was captured into `$REPLY_BODY` by Step 5 (Gmail-side `extract_text_from_message` strips MIME wrapping and normalises whitespace). Echo it for the verdict:

```
printf '%s\n' "$REPLY_BODY"
```

Then emit a structured JSON verdict per SPEC `§V.57`:

```json
{
  "qa_id": "<QA_ID>",
  "question": "<original question>",
  "source_file": "<SOURCE_FILE>",
  "answers_question": true,
  "every_factual_claim_supported_by_source": true,
  "cites_source_file": true,
  "unsupported_claims": [],
  "verdict": "pass"
}
```

Rules:
- `verdict = "pass"` iff all three booleans are `true` AND `unsupported_claims == []`.
- List each unsupported claim verbatim from the reply -- no free-form rating, no sycophancy.
- `cites_source_file` is satisfied by the reply naming the source filename OR a clearly identifying portion of it.

`verdict == "fail"` -> **G2 FAIL**.

### Step 7: G3 -- Logfire production-env span and error gate

Single Logfire MCP query against project `mailpilot`, window `[TEST_START, now]`, filter `deployment_environment = 'production'`:

```sql
WITH base AS (
  SELECT *
  FROM records
  WHERE deployment_environment = 'production'
    AND start_timestamp >= '$TEST_START'
)
SELECT
  (SELECT count(*) FROM base
     WHERE span_name = 'agent.invoke'
       AND attributes->>'trigger' = 'task') AS agent_invoke_task,
  (SELECT count(*) FROM base
     WHERE span_name ILIKE 'running tool%'
       AND attributes->>'gen_ai.tool.name' = 'search_drive_markdown') AS search_drive_tool,
  (SELECT count(*) FROM base
     WHERE span_name = 'gmail.send_message') AS gmail_send,
  (SELECT count(*) FROM base
     WHERE is_exception = true OR level IN ('error', 'warn')) AS errors_warns
LIMIT 1
```

PASS requires:

- `agent_invoke_task >= 1` (production processed an inbound task in the window)
- `search_drive_tool >= 1` (agent grounded via the demo KB)
- `gmail_send >= 1` (agent's reply went out from production)
- `errors_warns == 0` (no error / warn / exception spans in the window)

Any required counter below threshold OR `errors_warns > 0` -> **G3 FAIL**, naming the failing counter.

### Step 8: Logfire 3-bullet summary (always, even on FAIL)

Three follow-up queries (or one query with three columns) for the summary bullets:

```sql
-- Top span by volume
SELECT span_name, count(*) AS n
FROM records
WHERE deployment_environment = 'production'
  AND start_timestamp >= '$TEST_START'
GROUP BY span_name
ORDER BY n DESC
LIMIT 1
```

```sql
-- Error / warn count in window (same expression as G3, repeated for the bullet)
SELECT count(*) AS errors_warns
FROM records
WHERE deployment_environment = 'production'
  AND start_timestamp >= '$TEST_START'
  AND (is_exception = true OR level IN ('error', 'warn'))
```

```sql
-- Cache-hit ratio per SPEC §V.47 (sum across agent.invoke rollup spans in window)
SELECT
  sum((attributes->>'cache_read_input_tokens')::int)     AS read_cache_tokens,
  sum((attributes->>'cache_creation_input_tokens')::int) AS creation_cache_tokens,
  sum((attributes->>'input_tokens')::int)                AS input_tokens
FROM records
WHERE deployment_environment = 'production'
  AND start_timestamp >= '$TEST_START'
  AND span_name = 'agent.invoke'
```

Format the three bullets as:

- `top span: <span_name> (n=<count>)`
- `errors/warns in window: <count>`
- `cache_read / (cache_read + input): <read>/<read+input> (<ratio>%)` -- or `cache attrs missing` if all three sums are NULL (production deploy predates §V.47 wiring).

## Output contract

Print exactly two blocks to stdout, in order, and nothing else:

1. One line:
   - `PASS` -- all of G1, G2, G3 passed.
   - `FAIL: <one-line reason naming the failing gate>` -- as soon as any gate fails. Reason format examples: `G1 -- no reply within 60s`, `G2 -- verdict=fail (3 unsupported_claims)`, `G3 -- search_drive_tool=0`, `G3 -- errors_warns=2`.

2. Header `Logfire production window <TEST_START>..<now>:` followed by the three bullets from Step 8.

Do NOT:

- Auto-write a `.md` file to the repo. (Contrast `/smoke-test`, which writes `smoke-test-<YYYY-MM-DD>-<HHMMSS>.md` per SPEC `§V.58`.)
- Auto-invoke `/sdd:spec`.
- Render a phase matrix, §1/§2/§3 sections, or any of the smoke-test report structure per `§V.58`.
- Speculate about causes. This skill is a gate, not a diagnostic suite.

## On FAIL

`/demo-test` is a probe, not a debugger. If FAIL, the operator's next move is:

- `/smoke-test` -- full regression to localise the break.
- `/logfire:debug` -- drill into the window with full span context.
- Inspect the production deploy directly (`gcloud logs ...` or the Logfire UI) for the failing window.

This skill does not retry, does not amend the spec, and does not auto-file an issue.

## Spec references

- SPEC `§V.59` -- this skill's contract.
- SPEC `§V.60` -- liveness probes must hit the production-facing surface (G1 queries Gmail directly, not the local mailpilot DB).
- SPEC `§V.37` -- Gmail credential construction via `GmailClient("outbound@lab5.ca")` (delegated impersonation; supports both file-creds and ADC).
- SPEC `§V.57` -- in-scope grounding gate (G2 inherits the operator-judged JSON verdict structure).
- SPEC `§V.52` -- `deployment_environment` filter (G3).
- SPEC `§V.26` -- `agent.invoke` `trigger` attribute (G3 span match).
- SPEC `§V.53` -- `gen_ai.tool.name` attribute (G3 span match).
- SPEC `§V.47` -- cache_control attrs (Logfire summary bullet).
- SPEC `§V.58` -- smoke-test report shape (explicitly NOT applied here).
- SPEC `§V.61` -- gate-verdict source rule: latency verdict from `agent.invoke` span (G1), not CLI poll cadence.
