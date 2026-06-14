## SKILL.md Drift Check

Mechanical audit (no LLM-judgment); trigger when `src/mailpilot/SKILL.md`, `.claude/skills/**/*.md`, `src/mailpilot/cli.py`, or `src/mailpilot/settings.py` changed.

File-set scope:
- `src/mailpilot/SKILL.md` — packaged skill body (external LLM agents); all four checks apply.
- `.claude/skills/**/*.md` — operator-facing skill bodies (test-google-drive, lead-companies, etc.); per `§B.65` only checks (i) and (ii) apply (skill bodies do not enumerate settings so (iii) and (iv) do not apply).

Checks:
(i) per-noun verb roster is a superset of `@<noun>.command("<verb>")` set in `cli.py` — fail mode: skill names a retired verb (e.g. `enrollment remove` post-T92).
(ii) per-verb `--<flag>` tokens in recipes are a subset of `@click.option("--<flag>")` set for that handler in `cli.py`.
(iii) settings key list in `## Settings` == `Settings.model_fields` keys in `settings.py` — `src/mailpilot/SKILL.md` only.
(iv) env-var-prefix description in `## Settings` == `SettingsConfigDict(env_prefix=...)` value in `settings.py` (`MAILPILOT_*`) — `src/mailpilot/SKILL.md` only.

## §V.68 — _fact_check_body corpus-build algorithm

Per-document scoping (see `src/mailpilot/agent/tools.py:_fact_check_body`):
- table-bearing doc (any line contains `|`): contribute pipe-row lines (`"|" in line`) + list-item lines (`re.match(r"^\s*[-*]\s", line)`)
- prose-only doc (no `|` lines): contribute full content

Zero-ledger (no `read_drive_markdown` calls this invocation) -> hook no-op.

## §V.73 — Skill-body Workflow snippet executability

Mechanical audit; trigger when `.claude/skills/**/*.md` or `.claude/workflows/*.js` changed. Scope = every fenced ```js block that calls `parallel(`, `pipeline(`, or `agent(`, plus the saved-workflow byte-identity check (d).

Per ```js block:
(a) Free-symbol scan — every identifier used as a value ! resolve to an in-block definition (`const` / `let` / `function` / param) OR a runtime global. Runtime globals (do not flag): `meta`, `agent`, `parallel`, `pipeline`, `phase`, `log`, `args`, `budget`, `workflow`, plus JS built-ins (`JSON`, `Math`, `Array`, `Object`, `Promise`, `console`, ...). Any other bare identifier (e.g. `stale`, `buildPrompt`, `ENRICH_RESULT_SCHEMA`) ! be defined in the block — fail mode: free var crashes `ReferenceError` on paste (`§B.68`: bare `stale`).
(b) `args`-as-collection guard — if the block calls `args.map` / `args.filter` / `args.slice` / `args.length` / `args.forEach` or spreads `args`, it ! first `JSON.parse(args)` (or guard `typeof args === 'string'`). Why: runtime delivers `args` as a JSON STRING so `args.map` throws `is not a function` (`§B.68`).
(c) Prose-vs-`parallel` divergence — if surrounding prose claims "concurrency N" / "N concurrent" / "Default N", the block ! chunk to N (batch loop of size N around `parallel(batch.map(...))`). A bare `parallel(xs.map(...))` dispatches all `xs.length`, bounded only by runtime cap `min(16, cores-2)` — not N. Fail mode: prose promises 3, snippet runs all (`§B.68` secondary).
(d) Saved-workflow byte-identity — every embedded workflow snippet's post-`meta` body (each `.claude/skills/<skill>/SKILL.md` FIRST js-fenced block, sliced @ first `\n}\n` after `export const meta`) ! be byte-identical to its saved `.claude/workflows/<name>.js`'s post-`meta` body (same slice). Audited pairs (extend the PAIRS list below when a new skill+workflow lands): `lead-companies/SKILL.md` <-> `lead-companies-enrich.js`; `lead-contacts/SKILL.md` <-> `lead-contacts-find.js`. Why: the skill-body embedded snippet is the spec-of-record; the saved file is invoked by name @ runtime so silent divergence ships an unaudited workflow ((a)-(c) cover the saved file only transitively, when bodies match). Saved `meta` MAY add registry-only fields (`whenToUse`, fuller `description`) so compare the post-`meta` slice only, not the whole file. Fail mode: divergence -> saved-file unaudited drift.

Mechanical greps (manual judgment on hits):
- `rg -n '```js' .claude/skills/` — enumerate blocks.
- `rg -nE '\bargs\.(map|filter|slice|length|forEach)\b' .claude/skills/` not preceded by `JSON.parse(args)` or `typeof args` -> (b) fail.
- prose `rg -niE 'concurrency [0-9]|[0-9] concurrent|default [0-9]' .claude/skills/` near a block with bare `parallel(` and no batch loop (`for .* += N` / `.slice(`) -> (c) fail.
- (d) byte-identity — extract both post-`meta` bodies (slice each @ first `\n}\n` after `export const meta`, `.strip()`), compare equal:
  ```
  python3 - <<'PY'
  import re
  PAIRS = [
      ('.claude/skills/lead-companies/SKILL.md', '.claude/workflows/lead-companies-enrich.js'),
      ('.claude/skills/lead-contacts/SKILL.md', '.claude/workflows/lead-contacts-find.js'),
  ]
  body = lambda s: s[s.find('\n}\n') + 3:].strip()
  for skill_path, saved_path in PAIRS:
      emb = re.search(r'```js\n(.*?)```', open(skill_path).read(), re.DOTALL).group(1)
      saved = open(saved_path).read()
      print(('IDENTICAL' if body(emb) == body(saved) else 'DIVERGENT'), saved_path)
  PY
  ```
  any `DIVERGENT` -> (d) fail (saved-file unaudited drift).

## §V.74 — RFC-4180 CSV-ingestion parser mandate

Mechanical audit; trigger when `.claude/skills/**/*.md`, `.claude/skills/**/scripts/*.py`, or `src/**` changed. Scope = CSV-ingestion sites (handle a `.csv` path, a "CSV mode", or a comma-delimited lead export). The grep scope `.claude/skills/ src/` already recurses into `scripts/` so a `.py`-under-`scripts/` change is covered once the trigger-glob (previously `.md`-only) names it.

Checks:
(i) CSV ingestion ! use an RFC-4180 parser (`csv.DictReader` / `csv.reader` / the `csv` module). Fail mode: physical-line iteration, `.splitlines()`, `.split("\n")`, or `.split(",")` over CSV content — quoted fields carry embedded newlines and commas so one logical row spans many physical lines (`§B.69`: theirstack.csv 25 logical rows over 217 physical lines).
(ii) Redirect resolution ! use `curl -sL -o /dev/null -w '%{url_effective}'` (full chain, CR-free). Fail mode: HEAD `curl -sLI | grep '^location:' | awk` — 403 bot-blocking origins answer HEAD differently; awk retains the header trailing CR so corrupts a bare-host redirect target (`§B.69`).

Mechanical greps (manual judgment on hits — flag only in CSV context):
- `rg -n 'splitlines|\.split\(' .claude/skills/ src/` near `csv` / `CSV` / `.csv` context -> (i) fail. Non-CSV `splitlines` (email-body normalization, markdown line scan) not flagged.
- `rg -n 'curl -sLI' .claude/skills/ src/` -> (ii) fail (HEAD-grep redirect resolution).

Plain-text (non-CSV) line iteration is admitted (per-line domain/URL, `#`-comment skip) — do not flag.

## §V.101 — must-sense ` ! ` ban

Mechanical audit; trigger when `.claude/skills/**/*.md` changed. Scope = skill-body prose. A hard requirement ! be marked w/ an explicit word (`MUST` / `required`); a bare telegraph ` ! ` (must-glyph) in prose reads as negation in code, so a model executing the skill can invert the constraint (silent constraint flip — the failure §V.101 was authored to block).

Exempt (not flagged):
- backticked / fenced-code ` ! ` — `[ ! -f ]`, `!=`, `! cmd` inside an inline-code span or a ```fence``` (shell test / negation operator, not a prose obligation).
- SPEC.md + this file (`.claude/check-extras.md`) — telegraph register, both outside the `.claude/skills/**` scope; their ` ! ` is the authored must-glyph, not a violation.

Checks:
(i) zero bare must-sense ` ! ` in `.claude/skills/**/*.md` prose. Fail mode: ` ! ` standing in for "must" in an instruction line (e.g. `the body below meta ! stay byte-identical`, `CSV mode ! parse with an RFC-4180 parser`) — convert to `MUST`.

Mechanical grep (manual judgment on hits — flag only must-sense prose, not backticked/fenced shell):
- `rg -n ' ! ' .claude/skills/` -> classify each hit: backticked-shell / fenced-code -> exempt; prose obligation -> (i) fail (convert to `MUST`). Zero hits -> pass.

## §V.49 — bounded auto-retry parameters

4 attempts total; backoff [30, 120, 300]s; transient allow-list = Google 429/5xx, Anthropic 502/503/529, socket/TimeoutError; Drive socket timeout 60s feeds classifier; manual retry only failed/cancelled (completed + pending refused); retry UPDATE fires task_pending_trigger.

## §V.59 — /test-google-drive pass gates

2 variants from source outbound@lab5.ca, each judged by C4 Logfire aggregate over its own deployment_environment (§V.52), window [T_SEND_C, T_SEND_C+300s], span_name=agent.invoke, trigger=task. dev also scopes `workflow_id=DEMO_WORKFLOW_ID`; prod omits it (deployed workflow id unknown locally — burst identified by env + trigger + window, assumes deployed demo otherwise quiet).

- variant-prod: target hello@lab5.ca, env=production, warm/non-destructive (no make clean / no workflow create / no local run loop — deployed instance owns inbound); pre-flight requires outbound account else skip w/o send.
- variant-dev: target inbound@lab5.ca, env=development, full Phase 0 (make clean + `config set logfire_environment development` + accounts/contacts/demo-workflow + local `mailpilot run` loop).

N=4 burst, mix 2 in-scope / 1 out-of-scope / 1 compare via `qa.py pick` (§V.57). Subject `[TGD-<HHMMSS>-<i>]` fresh-randomized. Round-trip poll (local `email list` for dev, direct Gmail for prod) is sanity only, never the latency verdict (§V.61).

C4 gated assertions (PASS = all hold, per variant; compare = read_drive_markdown count >= 2 in trace):
- n_invokes == 4 AND n_distinct_email_ids == 4 (one span per inbound email §V.26; prod n_invokes > 4 = other demo traffic in window -> re-run quiet).
- n_compare == 1 AND n_noncompare == 3.
- p95(sla_agent_seconds) non-compare <= 75 (§V.61 burst over the 50s steady ceiling).
- p95(sla_delivery_seconds) <= 75 (§V.69 per-variant burst delivery gate).
- n_exceptions == 0 AND n_warns == 0 (zero error/warn scoped to env).
- n_tool_errors_noncompare == 0 AND n_tool_errors_compare <= 2 (§V.70 per-branch; flat retry_rate <= 0.05 void at N=4, governs larger N only). Compare exhausting §V.71 cap-3 -> cap_reached warn -> already fails n_warns == 0.
- avg_cache_hit_ratio >= 0.5 (§V.47 cache warmth).
- overlap_pairs >= 2 (concurrency proof §V.23; max C(4,2)=6).
- read_drive_markdown max_dur_s < 60 AND n_exc == 0 (httplib2-race signature absent, §V.38).

Report (NOT gated): max_sla_agent_compare_s (compare-type advisory ceiling 120s per §V.61), max_sla_delivery_s, max_total_s, token totals.

Output: per-variant PASS|FAIL line + 4-bullet C4 metrics block + final OVERALL line; chat-only (no report file); no /sdd:spec auto-invoke.

### §V.59 Report (chat-only, advisory, every run per variant)

Emitted after each variant's C4 metrics block, every run (PASS or FAIL). Never alters the verdict (§V.59 — C4 gates alone decide); no .md artifact; structural/timing aggregates only, never reply content (§V.57 intact). Format = 2 labelled parts (a)/(b), a few lines each — NOT a phase matrix / §1-§2-§3 report. Templates + SQL live here, not inlined in the skill body (§V.100).

(a) Breach attribution. Per FAILED gate -> the failing span(s) + owning §V:

| Breached gate | Failing span(s) to name | Owning §V |
| --- | --- | --- |
| n_invokes != 4 / n_distinct_email_ids != 4 | merged/dropped/extra agent.invoke (by email_id) | §V.26 |
| n_compare != 1 / n_noncompare != 3 | mis-branched agent.invoke (x-check C4.c read counts) | §V.27 |
| p95_sla_agent_noncompare > 75 | slowest non-compare agent.invoke (email_id) | §V.61 |
| p95_sla_delivery > 75 | agent.invoke w/ max sla_delivery_seconds | §V.69 |
| n_exceptions / n_warns > 0 | the is_exception / level='warn' span | §V.59 |
| n_tool_errors_noncompare > 0 / n_tool_errors_compare > 2 | agent.invoke w/ tool_error_count > 0 (branch: non-compare strict, compare cap-2) | §V.70 |
| avg_cache_hit_ratio < 0.5 | agent.invoke w/ low cache_read/in_tok | §V.47 |
| overlap_pairs < 2 | the serialized agent.invoke set | §V.23 |
| read_drive_markdown max_dur >= 60 / n_exc > 0 | the read_drive_markdown span | §V.38 |

self-heal-timing artifact vs real regression (sla_delivery only): if the breaching invoke's subject is in the B2 self-heal resend set, the resend re-anchored its delivery_s after T_SEND_C -> flag artifact (advisory, not a §V.69 regression). Else real regression. On PASS -> "no gate breached".

(b) per-invoke tool-call timeline SQL (search/read/chat order per trace; call shape only, not content per §V.57):

```sql
WITH invoke AS (
  SELECT trace_id, attributes->>'email_id' AS email_id, start_timestamp AS invoke_start
  FROM records
  WHERE deployment_environment = '<ENV>'
    AND span_name = 'agent.invoke'
    AND attributes->>'trigger' = 'task'
    AND start_timestamp >= '<T_SEND_C>'
    AND start_timestamp <= '<T_SEND_C>'::timestamptz + INTERVAL '300 seconds'
    <WF_PREDICATE>
)
SELECT
  i.email_id,
  array_agg(COALESCE(r.attributes->>'gen_ai.tool.name', 'chat') ORDER BY r.start_timestamp) AS call_timeline,
  COUNT(*) FILTER (WHERE r.attributes->>'gen_ai.tool.name' = 'search_drive_markdown') AS n_search,
  COUNT(*) FILTER (WHERE r.attributes->>'gen_ai.tool.name' = 'read_drive_markdown')   AS n_read
FROM invoke i
JOIN records r ON r.trace_id = i.trace_id
WHERE r.attributes->>'gen_ai.tool.name' IN ('search_drive_markdown', 'read_drive_markdown')
   OR r.attributes->>'gen_ai.operation.name' = 'chat'
GROUP BY i.email_id, i.invoke_start
ORDER BY i.invoke_start;
```

call_timeline shows search-first ordering (§V.41); n_read >= 2 marks the compare invoke (matches C4.a is_compare).

Report-format template (chat output):
```
report [<variant>/<ENV>]:
  (a) breach: <"no gate breached" | <gate> -> <email_id> (§V.NN)[, self-heal artifact|real regression]; ...>
  (b) timeline: <email_id>: [search,read,chat,...] (search <n>/read <n>) ; ... one line per invoke
```

### §V.59 On FAIL -> Next remedies (auto-investigation, advisory)

On FAIL the skill auto-investigates the current-run records itself (same `query_run` window [T_SEND_C, T_SEND_C+300s], no manual /logfire:debug) and emits a chat-only `## Next` block of suggested remedies derived from the part-(a) breach attribution. Per breached gate -> what to inspect (current-run records) -> remedy target:

| Breached gate (§V) | Inspect (current-run records) | Remedy target |
| --- | --- | --- |
| sla_delivery (§V.69), real regression | breaching agent.invoke + surrounding loop.tick: did classify-forces-full-sweep re-sweep fire? | §V.69 wakeup_event path |
| tool_error (§V.70) | offending agent.invoke tool-error span(s); classify the rejection | §V.42 format-lint / §V.68 fact-check / §V.41 search-order |
| overlap_pairs (§V.23) | task.drain / claim spans in window (drain pool serialized) | max_concurrent_tasks; advisory-lock contention |
| read_drive_markdown max_dur (§V.38) | the long Drive read span's trace | sequential=True Drive registration |

self-heal-timing artifact (sla_delivery subject resent in B2) -> advisory, drop from Next (not a §V.69 regression). Next block = 1-5 atomic items, each cites owning §V + span/trace; chat-only, never alters the verdict.

## §V.61 — reply-latency SLA thresholds

sla_agent_seconds gating: > 50s steady-state critical; compare-type > 90s critical, 50-90s advisory.
sla_delivery_seconds: advisory (Gmail-side uncontrolled).
Verdict derived from agent.invoke span in Logfire; CLI poll = round-trip check only.

## §V.70 — burst retry-rate contract measurement

Per-branch at the N=4 burst (the flat ratio is statistically void at small sample: 1 tool error = 25% >> 5%):
- non-compare invocations: SUM(tool_error_count) FILTER (WHERE NOT is_compare) == 0 (verbatim-citing single-source replies must be retry-clean).
- compare invocation: SUM(tool_error_count) FILTER (WHERE is_compare) <= 2 (cross-datasheet synthesis structurally induces §V.68 fact-check re-drafts from unit conversion; the agent self-corrects within the §V.71 cap-3). A compare that exhausts cap-3 emits a reply_email.reply_rejection.cap_reached logfire.warn -> already fails the C4 n_warns == 0 gate, so the floor cannot mask a non-self-correcting agent.
- larger-N bursts (P <= 8, N <= 25): the flat agent.tool_errors / agent.invoke ratio <= 5% still governs (reported for trend at N=4).
Measured in /test-google-drive per-variant burst window [T_SEND_C, T_SEND_C+300s] (prod env + dev env measured separately) against sla_agent per §V.61.
Breach = prompt-fidelity regression under load -> investigate §V.41 (search-first / verbatim citation), §V.57 (KB coverage), §V.42 (format-lint sensitivity), §V.68 (fact-check).
Orthogonal to §V.69: V70 binds agent-execution quality, V69 delivery timing.
