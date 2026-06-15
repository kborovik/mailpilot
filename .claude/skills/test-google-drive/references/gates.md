# C4 gate SQL + assertion detail (SPEC §V.59)

Loaded at **Phase 2** of `SKILL.md`. Run each gate with
`mcp__claude_ai_logfire__query_run` (project `mailpilot`). All gates query
`deployment_environment = '<ENV>'`, window `[<T_SEND_C>, <T_SEND_C> + 300s]`,
`span_name = 'agent.invoke'`, `trigger = 'task'`. The **dev** variant additionally
scopes by `<WF_PREDICATE>` (`AND r.attributes->>'workflow_id' = '<DEMO_WORKFLOW_ID>'`);
the **prod** variant omits it -- the deployed workflow id is not known locally, so the
burst is identified by env + trigger + window (which assumes the deployed demo system is
otherwise quiet during the test). Primary verdict = `sla_agent_seconds` (our-side agent
execution) per SPEC §V.61. A `compare` invocation = `read_drive_markdown` count >= 2 in its
trace.

The rows these gates return ARE the verdict; the Phase 3 Gate report renders them
span-free, and Phase 4 (FAIL only) drills into `references/investigate.md`.

## Gate G.a -- per-span SLA + token economics (two-budget split, compare vs non-compare)

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
  COUNT(DISTINCT email_id) AS n_distinct_ids,
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
  SUM(tool_error_count) FILTER (WHERE NOT is_compare) AS n_tool_errors_noncompare,
  SUM(tool_error_count) FILTER (WHERE is_compare) AS n_tool_errors_compare,
  SUM(tool_error_count)::float / NULLIF(COUNT(*)::float, 0) AS retry_rate,
  AVG(cache_read::float / NULLIF(in_tok::float, 0)) AS avg_cache_hit_ratio,
  SUM(in_tok) AS total_in_tok,
  SUM(out_tok) AS total_out_tok
FROM burst;
```

Gated assertions (all MUST hold):

- `n_invokes == 4` AND `n_distinct_ids == 4` -- no merged or dropped triggers (SPEC §V.26 /
  §T.63: one span per inbound email). On the **prod** variant, `n_invokes > 4` means other
  lab5.ca/mailpilot/ demo traffic shared the window -- re-run during a quiet period; this is an
  environment caveat, not a system failure.
- `n_compare == 1` AND `n_noncompare == 3` -- matches the burst mix (1 qa-cmp + 2 qa-in + 1
  qa-out, SPEC §V.27). A mismatch means a compare invocation skipped a required read OR a
  non-compare invocation issued a stray second read; cross-check against G.c before flagging.
- `p95_sla_agent_noncompare_s <= 75` -- burst gate over non-compare invocations (SPEC §V.61;
  the §V.23 burst-load tolerance over the 50s steady single-source ceiling). A breach is an
  our-side regression of agent execution under load.
- `p95_sla_delivery_s <= 75` -- per-variant burst delivery gate (SPEC §V.69). The event-driven
  full-sweep-on-classify (§V.69: a tick that classifies >=1 inbound forces the next tick's full
  sweep + sets `wakeup_event`) is what bounds delivery under burst; a breach means that re-sweep
  mechanism regressed.
- `n_exceptions == 0` AND `n_warns == 0` (SPEC §V.59 -- zero error/warn scoped to this variant's
  env).
- `n_tool_errors_noncompare == 0` AND `n_tool_errors_compare <= 2` -- §V.70 per-branch burst
  retry-rate contract. At N=4 the flat ratio is statistically void (1 error = 0.25 >> 0.05), so
  the gate is per-branch: non-compare invocations must be retry-clean, while the lone compare
  invocation tolerates up to 2 self-correcting §V.68 fact-check re-drafts (cross-datasheet unit
  conversion structurally fabricates numeric tokens). A compare that exhausts the §V.71 cap-3
  emits a `reply_email.reply_rejection.cap_reached` warn -- already caught by the `n_warns == 0`
  gate above, so this floor cannot mask a non-self-correcting agent. The flat `retry_rate <= 0.05`
  ratio still governs larger-N bursts (P<=8, N<=25) and is reported for trend. A breach signals a
  prompt-fidelity regression under load -- investigate §V.41 (search-first ordering), §V.42
  (format-lint sensitivity), §V.68 (fact-check). Measured separately for prod and dev.
- `avg_cache_hit_ratio >= 0.5` -- prompt cache stays warm across the burst (SPEC §V.47; catches
  cache-key churn where each invocation re-pays the full system-prompt token cost).

Reported (NOT gated):

- `max_sla_agent_compare_s` -- compare-type advisory ceiling 120s (§B.62: 2-datasheet synthesis
  structurally exceeds the single-source band); do NOT fail on a single breach.
- `max_sla_delivery_s` -- reported alongside the gated p95.
- `max_total_s`, `total_in_tok`, `total_out_tok` -- end-to-end and token totals, for reference.

## Gate G.b -- concurrency proof (no serialization regression)

```sql
WITH read_counts AS ( /* same CTE as G.a */ ),
burst AS ( /* same CTE as G.a */ )
SELECT COUNT(*) AS overlap_pairs
FROM burst a, burst b
WHERE a.email_id < b.email_id
  AND a.start_timestamp < b.end_timestamp
  AND b.start_timestamp < a.end_timestamp;
```

Assert `overlap_pairs >= 2`. With N=4 fired in one wave, max possible overlap is C(4,2)=6; a
floor of 2 is generous enough that only strict serialization (drain-layer pool regression, SPEC
§V.23) fails it. A failure means the dispatcher serialized invocations -- a Critical Bug, since
it defeats the burst-load oracle.

## Gate G.c -- Drive race signatures absent (§B.34)

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

- `max_dur_s < 60` -- the §B.34 60s socket-timeout signature is absent. A 60s+
  `read_drive_markdown` span under burst means the structural `sequential=True` Drive-tool
  registration (SPEC §V.38) regressed across concurrent agent invocations. Critical Bug.
- `n_exc == 0` -- no unhandled exceptions escaped the Drive tool wrappers.

## Per-gate Gate-report mapping (Phase 3)

The Phase 3 Gate report renders one row per gate from the G.a/b/c outputs above:

| Gate row             | Measured (from C4)             | Threshold        | §V    |
| -------------------- | ------------------------------ | ---------------- | ----- |
| n_invokes            | `n_invokes` (`n_distinct_ids`) | `== 4`           | §V.26 |
| branch split         | `n_noncompare`c/`n_compare`... | `3 nc / 1 c`     | §V.27 |
| p95_sla_agent        | `p95_sla_agent_noncompare_s`   | `<= 75`          | §V.61 |
| p95_sla_delivery     | `p95_sla_delivery_s`           | `<= 75`          | §V.69 |
| warns / errors       | `n_warns` / `n_exceptions`     | `== 0`           | §V.59 |
| tool_errors nc / c   | `n_tool_errors_noncompare` / `..._compare` | `nc==0, c<=2` | §V.70 |
| cache_hit            | `avg_cache_hit_ratio`          | `>= 0.5`         | §V.47 |
| overlap_pairs        | `overlap_pairs` (G.b)         | `>= 2`           | §V.23 |
| read_drive max_dur   | `max_dur_s` (G.c)             | `< 60`           | §V.38 |
