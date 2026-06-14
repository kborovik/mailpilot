# Smoke Test -- Timing

Expected total: ~10-11 minutes. Phase 0 once, run loop once, no reset between scenarios. The added compare-and-contrast question (B7) costs roughly one extra ~60-90s `sla_agent` window over the prior baseline because it forces 2-4 `read_drive_markdown` calls and a multi-doc synthesis (compare-type steady ceiling is 90s per §V.61(+) / `§B.61`, vs 50s for single-source). The concurrent dual-send (B7.5) adds another ~50s window for the second of the two parallel replies (the two windows overlap, so the marginal cost is one reply window, not two). Scenario C (burst-load oracle) adds ~1-2 minutes for the 8-send burst plus aggregate verification; per-span `sla_agent` p95 must stay <=75s under burst on non-compare invocations (§V.61(+)), compare-type p95 is reported separately with an advisory 120s ceiling per `§B.62`, while `sla_delivery` is advisory because Gmail-side Pub/Sub batching dominates the tail.

| Phase / scenario                          | Duration |
| ----------------------------------------- | -------- |
| Phase 0 (once, 2 accounts)                | ~15s     |
| A1 / B1 workflow setup                    | ~5s      |
| A2 start run loop                         | ~5s      |
| A3 outbound agent                         | ~10s     |
| A4 sync + route                           | ~10-60s  |
| A5 / B3 / B6 / B7 operator send           | ~3s each |
| A6 reply round-trip                       | ~10-60s  |
| A7 task drain                             | ~10-60s  |
| B4 in-scope reply (sla_agent<=50s)        | ~10-50s  |
| B6 out-of-scope reply (sla_agent<=50s)    | ~10-50s  |
| B7 compare reply (sla_agent<=90s)         | ~20-90s  |
| B7.5 concurrent dual reply (sla_agent<=50s each, overlapping) | ~20-50s |
| A8 / B8 activity check                    | ~3s      |
| C1 burst payload generation               | ~3s      |
| C2 8 sends @ P=8                          | ~5-15s   |
| C3 poll for 8 replies (240s CLI cap)      | ~60-120s |
| C4 Logfire aggregate gates (4 queries)    | ~10s     |
| C5 / C6 activity + quiet check            | ~5s      |
| C7 stop run loop                          | ~3s      |
| Report                                    | ~10s     |
