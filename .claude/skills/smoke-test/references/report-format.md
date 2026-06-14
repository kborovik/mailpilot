# Smoke Test -- Phase 5 report format

Loaded at Phase 5 when rendering the report. The report is chat-only -- do NOT write it to disk.

## §1 Execution

Phase-results matrix + 3-bullet Logfire signal summary. One screen.

```
Smoke Test Results
==================

Phase 0 (one-time setup) ..... PASS  (2 accounts, 2 contacts, 1 company)

Scenario A: Outbound workflow (sole workflow active)
  A1 Create workflow ......... PASS
  A2 Start sync loop ......... PASS
  A3 Outbound agent send ..... PASS
  A4 Gmail delivery (in) ..... PASS
  A5 Operator reply .......... PASS
  A6 thread_match routing .... PASS
  A7 Agent processes reply ... PASS
  A8 Activity timeline ....... PASS

Scenario B: KB-grounded demo (lab5.ca/mailpilot/, outbound workflow still active)
  B1  Create demo workflow ......... PASS  (workflow list shows 2 active)
  B2  Sync loop still alive ........ PASS
  B3  In-scope trigger send ........ PASS
  B4  Grounded reply (sla_agent<=50) PASS  (SLA_AGENT_B1=<Ns> / SLA_DELIVERY_B1=<Ns> / total=<Ns>; cited model: <e.g., TW-18.0K-1240>)
  B5  Drive tools used ............. PASS  (search_drive_markdown -> read_drive_markdown -> reply_email -> record_enrollment_outcome)
  B6  Out-of-scope decline ......... PASS  (SLA_AGENT_B2=<Ns> / SLA_DELIVERY_B2=<Ns> / total=<Ns>; no fabricated specs)
  B7  Compare-and-contrast reply ... PASS  (SLA_AGENT_B3=<Ns> / SLA_DELIVERY_B3=<Ns> / total=<Ns>; <N> sources, <N> read_drive_markdown calls; no single-sourced specs)
  B7.5 Concurrent dual in-scope ..... PASS  (SLA_AGENT_B75a=<Ns>/SLA_DELIVERY_B75a=<Ns>, SLA_AGENT_B75b=<Ns>/SLA_DELIVERY_B75b=<Ns>; 2 overlapping agent.invoke spans; both grounded in own source)
  B8  Activity timeline ............ PASS  (5 received / 5 sent / 5 enrollment_completed)
  B9  Outbound stayed quiet ........ PASS  (0 new outbound sends during B)

Scenario C: Burst-load oracle (8 emails @ P=8, outbound workflow still active)
  C1  Burst payload generated ...... PASS  (8 distinct subjects; mix 4 qa-in / 2 qa-out / 2 qa-cmp)
  C2  8 sends @ P=8 ................ PASS  (wait exit 0; 8 outbound rows with workflow_id=null)
  C3  8 replies received ........... PASS  (wall-clock <Ns>; all classified to demo workflow)
  C4  Logfire aggregate gates ...... PASS  (p95_sla_agent_noncompare=<Ns> <=75, 0 exc, 0 warn, cache>=0.5, overlap_pairs=<N> >=10, drive max_dur<60s; advisory: p95_sla_agent_compare=<Ns> (~<=120), p50/p95/max_sla_delivery=<Ns>/<Ns>/<Ns>, p95_total=<Ns>)
  C5  Activity timeline (delta) .... PASS  (+8 received / +8 sent / +8 enrollment_completed)
  C6  Outbound stayed quiet ........ PASS  (0 new outbound-workflow sends during C)
  C7  Stop sync loop ............... PASS

Entity IDs:
  Outbound account: <id>   Inbound account: <id>   Company: <id>
  Outbound contact: <id>   Inbound contact: <id>
  Outbound workflow: <id>  Demo workflow: <id>

Logfire signals:
  - Top span by volume: <span_name> (<N> calls)
  - Errors / warnings: <N> (<one-line summary or "none">)
  - Cache-hit ratio (multi-turn invokes): <pct> (<one-line note if below ~0.5>)
```

If a phase failed, stop §1 at the failing phase with the failure JSON and any captured stdout from the background `mailpilot run`.

## §2 Bugs

Observable failures, one paragraph each. Mandatory section -- write `Bugs: none.` if nothing fired.

A Bug = an observable failure: a gate that did not pass, a regression of a public promise, a wrong functional output, or a broken presentation. Things that "worked as designed" do not appear here. Latent risks surfaced by inspection (no concrete failure this run) belong in §3 Invariants.

**Bug block format.** One paragraph per entry, this exact shape:

> **Bug N -- <one-line title> (<severity>).** <observable failure: what the test saw vs expected.> Caught at gate <gate id>. <entity / span / file involved.> Suspected cause: <one clause>. <SPEC §V / §T cross-reference if contradicting an existing invariant.> <Logfire signal if the trace proves cause.>
>
> **Spec action:** `<routing tag>` -- `<exact /sdd:spec invocation>`

Routing tag:

- `bug` -- file an incident in §B. Use when a concrete failure occurred. BACKPROP decides whether a new §V accompanies. Invocation: `/sdd:spec bug: <body sentence(s)>`
- `bug+invariant` -- file in §B AND propose a specific new §V in the body so BACKPROP appends both. Use when the failure has a clear recurrence class and the invariant can be stated in one sentence. Invocation: `/sdd:spec bug: <body>. Propose §V: <invariant text>`
- `code-only` -- record only; no spec entry. Use only for mechanical / harness-flake failures with zero recurrence class.

Severity (number sequentially `Bug 1`, `Bug 2`, ...):

- `Critical` -- regression of a public promise (lab5.ca/mailpilot/ SLA, KB grounding, fabrication-free decline). Always at least `bug` routing.
- `High` -- wrong functional output a real user would see. Always at least `bug` routing.
- `Medium` -- correct output, broken presentation. Routing typically `bug` or `code-only`.
- `Low` -- harness-only issues. Routing typically `code-only`.

**Auto-file matrix.** After the chat-rendered report, invoke the `Spec action:` line for each Bug per this matrix:

| Severity | `bug` / `bug+invariant`      | `code-only` |
| -------- | ---------------------------- | ----------- |
| Critical | auto-invoke                  | print only  |
| High     | auto-invoke                  | print only  |
| Medium   | print only (operator review) | print only  |
| Low      | print only                   | print only  |

"Auto-invoke" = call `/sdd:spec` with the exact `Spec action:` invocation. Run them sequentially so each `## Next` reply token (`ok` / `revise` / `cancel`) applies to a single Bug -- never batch. Record outcome (`filed as §B.7 with §V.22`, `cancelled by operator`, etc.) in the hand-off block. "Print only" means the line goes into the hand-off "Operator review" list, ready for the operator to paste.

## §3 Invariants

Under-specified contracts surfaced by inspection -- no concrete failure this run, but the run revealed a gap in §V. Mandatory section -- write `Invariants: none.` if nothing fired. Always print-only; the operator pastes if they choose to file.

**Invariant block format.**

> **Invariant N -- <title>.** <one paragraph: observation, proposed change, why it helps. Plain prose, no bullets.>
>
> **Spec action:** `propose-invariant` -- `/sdd:spec amend §V add: <invariant text>`

Number sequentially across the run so each item has a unique number (`Bug 1`, `Bug 2`, `Invariant 3`, ...).

End §3 with the runtime line:

```
Total runtime: ~<N> minutes. <one-sentence verdict>.
```

## Spec hand-off block

After §3, after auto-invocations have run, print:

```
Spec hand-off
=============
Auto-filed (Critical/High Bugs):
  - Bug <N> -- <title>: <result, e.g., "filed as `§B.7` with §V.22", "cancelled by operator">
  - ...

Operator review (print-only Bugs + all Invariants):
  /sdd:spec bug: <Bug N body...>                          # Bug N (<severity>, <routing>)
  /sdd:spec amend §V add: <invariant text>                # Invariant N
  ...
```

Each line under "Operator review" MUST be the exact `Spec action:` invocation -- ready to paste. The trailing `# comment` names the originator so the operator can find it in the body.

If a `/sdd:spec` invocation is cancelled or revised by the user mid-run, record the outcome in the auto-filed list and continue with the next Bug.
