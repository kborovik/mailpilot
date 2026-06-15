# FAIL-only investigation + /sdd:spec-ready remedy (SPEC §V.59)

Loaded at **Phase 4** of `SKILL.md`, **only when a variant FAILs**. The skill
auto-investigates the current-run Logfire records itself (same
`mcp__claude_ai_logfire__query_run` access the gates used, window
`[<T_SEND_C>, <T_SEND_C> + 300s]`, scoped to the variant's
`deployment_environment` -- no manual `/logfire:debug`), classifies each breach,
and emits a `>` blockquote breach callout per breach + one consolidated dispatchable
`## Next`. Span detail (`email_id` / `trace_id` / timeline) lives ONLY here -- it
never appears in the every-run Gate report, which stays a span-free health verdict.

This investigation is single-context (NOT a `.claude/workflows/*.js` multi-agent
fan-out). It does not retry the burst, does not amend the spec, and does not
auto-file an issue (SPEC §V.59 / §V.57).

## Step 1 -- breach attribution

For each FAILED C4 gate, name the failing span(s) and the owning invariant:

| Breached gate                                     | Failing span(s) to name                                              | Owning §V |
| ------------------------------------------------- | -------------------------------------------------------------------- | --------- |
| `n_invokes != 4` / distinct-id mismatch           | merged / dropped / extra `agent.invoke` (by `email_id`)              | §V.26     |
| `n_compare != 1` / `n_noncompare != 3`            | mis-branched `agent.invoke` (x-check G.c read counts)              | §V.27     |
| `p95_sla_agent_noncompare > 75`                   | slowest non-compare `agent.invoke` (`email_id`)                     | §V.61     |
| `p95_sla_delivery > 75`                            | `agent.invoke` with max `sla_delivery_seconds`                     | §V.69     |
| `n_exceptions > 0` / `n_warns > 0`                 | the `is_exception` / `level='warn'` span                          | §V.59     |
| `n_tool_errors_noncompare > 0` / `..._compare > 2`| `agent.invoke` with `tool_error_count > 0` (nc strict, c cap-2)    | §V.70     |
| `avg_cache_hit_ratio < 0.5`                        | `agent.invoke` with low `cache_read/in_tok`                       | §V.47     |
| `overlap_pairs < 2`                                | the serialized `agent.invoke` set                                  | §V.23     |
| `read_drive_markdown max_dur >= 60` / `n_exc > 0`  | the `read_drive_markdown` span                                     | §V.38     |

**Self-heal-timing artifact vs real regression (`sla_delivery` only).** If the breaching
invoke's subject is in the Phase 1 self-heal resend set (`resent[]` from `burst.py`), the
resend re-anchored its `sla_delivery_seconds` after `T_SEND_C` -> flag it a **self-heal-timing
artifact** (advisory, NOT a §V.69 regression) and **drop it from the remedy**. Otherwise it is a
**real regression**.

## Step 2 -- per-invoke tool-call timeline

Surfaces search-first ordering (SPEC §V.41) and compare-vs-non-compare shape; it is the
order/shape of calls, never reply content (SPEC §V.57).

```sql
WITH invoke AS (
  SELECT span_id AS invoke_span_id, attributes->>'email_id' AS email_id,
         start_timestamp AS invoke_start
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
  array_agg(COALESCE(call_span.attributes->>'gen_ai.tool.name', 'chat') ORDER BY call_span.start_timestamp) AS call_timeline,
  COUNT(*) FILTER (WHERE call_span.attributes->>'gen_ai.tool.name' = 'search_drive_markdown') AS n_search,
  COUNT(*) FILTER (WHERE call_span.attributes->>'gen_ai.tool.name' = 'read_drive_markdown')   AS n_read
FROM invoke i
-- §V.59/§B.81: walk the parent-span lineage (agent.invoke -> agent-run span ->
-- tool/chat span), not by the shared trace. Co-tick invokes share one
-- trace under §V.23, so a trace join would smear every sibling invoke's
-- calls into this row.
JOIN records agent_run ON agent_run.parent_span_id = i.invoke_span_id
JOIN records call_span ON call_span.parent_span_id = agent_run.span_id
WHERE call_span.attributes->>'gen_ai.tool.name' IN ('search_drive_markdown', 'read_drive_markdown')
   OR call_span.attributes->>'gen_ai.operation.name' = 'chat'
GROUP BY i.email_id, i.invoke_start
ORDER BY i.invoke_start;
```

`call_timeline` shows search-first ordering (§V.41); `n_read >= 2` marks the compare invoke
(matches G.a `is_compare`).

## Step 3 -- breach -> inspect -> remedy target

Per breached gate, what to inspect in the current-run records and the remedy target:

| Breached gate (§V)                    | Inspect (current-run records)                                                          | Remedy target                                     |
| ------------------------------------- | -------------------------------------------------------------------------------------- | ------------------------------------------------- |
| `sla_delivery` (§V.69), real regression | breaching `agent.invoke` + surrounding `loop.tick`: did classify-forces-full-sweep re-sweep fire? | §V.69 `wakeup_event` path                          |
| `tool_error` (§V.70)                   | offending `agent.invoke` tool-error span(s); classify the rejection                    | §V.42 format-lint / §V.68 fact-check / §V.41 search-order |
| `overlap_pairs` (§V.23)                | `task.drain` / claim spans in window (drain pool serialized)                          | `max_concurrent_tasks`; advisory-lock contention   |
| `read_drive_markdown max_dur` (§V.38)  | the long Drive read span's trace                                                       | `sequential=True` Drive registration               |
| `n_invokes` / distinct-id (§V.26)      | merged / dropped / extra `agent.invoke`; the `is_routed` gate                          | §V.22 single-route-pass; §V.28 enrollment ensure   |
| `cache_hit` (§V.47)                    | `agent.invoke` `cache_read` / `cache_creation` attrs                                   | §V.47 cache-key churn (instructions/tool defs)     |

## Step 4 -- emit the breach callout(s) + one consolidated `## Next`

Per breached gate (drop self-heal-timing artifacts), emit one `>` blockquote callout --
**Breach** / **Cause** / **Recurrence class** (backprop-shaped: breach -> cause ->
recurrence-class). Then emit exactly ONE consolidated `## Next` per the `/sdd:spec`
`## OUTPUT -- "Next" block` contract: 1-5 positional `run <int>` items -- one
`/sdd:spec <remedy intent>` per breached gate (**item 1 a `/sdd:spec` remedy**) plus a final
`/test-google-drive <variant>` re-run item. The operator dispatches a fix with `run 1` (or
selectively) -- no manual paste line. Advisory only -- it never alters the PASS/FAIL verdict (the
C4 gates alone decide, SPEC §V.59) and writes no `.md` artifact (SPEC §V.57). Emit it as rendered
markdown (real blockquote + real `## Next`), never inside a code fence.

Shape (one blockquote per breach, then one `## Next`; fill `<...>` from Steps 1-3):

> **Breach** -- <gate>=<measured> > <threshold> (§V.NN), <real regression | artifact dropped>
> **Cause** -- <what the current-run spans show -- the concrete failing path>
> **Recurrence class** -- <the class of failure a new invariant/bug would catch>

## Next

1. /sdd:spec <free-form intent: the failing path + the trace to cite + the guard to add>
2. /test-google-drive <variant> -- re-run once the fix lands

Worked example (`sla_delivery` real regression):

> **Breach** -- p95_sla_delivery = 88s > 75 (§V.69), real regression.
> **Cause** -- tick @14:30:11 classified 2 inbound but did not set `wakeup_event`; the next
> tick's full sweep never fired -> 2 replies missed the 75s band.
> **Recurrence class** -- delivery-SLA regression when classify-forces-full-sweep skips a burst tick.

## Next

1. /sdd:spec a burst tick that classified >=1 inbound failed to set wakeup_event, so the next
   tick's full sweep never fired and delivery p95 breached §V.69 under load (trace <trace_id>);
   add §B + a wakeup_event guard invariant
2. /test-google-drive dev -- re-run once the fix lands
