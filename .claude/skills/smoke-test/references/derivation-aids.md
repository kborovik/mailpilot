# Smoke Test -- Phase 5 derivation aids

Loaded at Phase 5 to derive §2 Bugs / §3 Invariants. NOT rendered to the report.

## Derivation aids (NOT rendered to the report)

These are scan inputs that feed §2 (concrete failures) and §3 (under-specified contracts). Do NOT render the SQL queries, the 13-cat checklist, or per-category narrative as sections in the output.

**Logfire pass.** Run once across both scenarios via `/logfire:debug` with the test window:

```sql
-- Volume by span name (find noise)
SELECT span_name, COUNT(*) AS count, AVG(duration) AS avg_ms
FROM records
WHERE start_timestamp >= '<EARLIEST_TEST_START>'
GROUP BY span_name
ORDER BY count DESC
LIMIT 30
```

```sql
-- Errors and warnings
SELECT start_timestamp, span_name, message, attributes
FROM records
WHERE start_timestamp >= '<EARLIEST_TEST_START>'
  AND (is_exception = true OR level = 'warn')
ORDER BY start_timestamp
LIMIT 50
```

```sql
-- Agent invocations (one row per agent.invoke)
SELECT start_timestamp, attributes->>'workflow_type' AS type,
       attributes->>'trigger' AS trigger,
       attributes->>'tool_call_count' AS tools,
       attributes->>'cache_read_input_tokens' AS cache_read,
       attributes->>'input_tokens' AS in_tok,
       attributes->>'output_tokens' AS out_tok
FROM records
WHERE start_timestamp >= '<EARLIEST_TEST_START>'
  AND span_name = 'agent.invoke'
ORDER BY start_timestamp
LIMIT 50
```

**Scan checklist** (categories, scenario-agnostic; skip cleanly if the run did not exercise one). Use these to find anything that should escalate to §2 Bugs or §3 Invariants:

1. CLI usability -- awkward sequencing, missing fields, unhelpful error messages.
2. Logfire observability -- missing spans/attributes, noisy span families, broken parent-child causality.
3. Agent prompt fidelity -- DOs/DON'Ts honoured, format constraints, stop conditions, decline vs fabricate, reasoning matches tool calls.
4. Agent context window -- prompt cache active, tool inventory appropriate, redundant context.
5. Latency -- end-to-end per stage, scenario SLAs, cold vs warm.
6. Cost / token economics -- per-invoke tokens, cache hit ratio, classifier cost share.
7. Tool-call efficiency -- calls per outcome, redundant calls, refusal-round waste.
8. Routing & classification quality -- `route_method` distribution, classifier reasoning sanity, false-skip cases.
9. Concurrent workflow safety -- cross-workflow / cross-account leakage.
10. Tool integration health -- per-integration error rate, informative tool errors.
11. Data integrity -- agent claims vs DB rows, no orphans, no silent drops.
12. Determinism / variance -- run-over-run drift on critical paths.
13. Other -- timing, races, performance, anything off-grid.
