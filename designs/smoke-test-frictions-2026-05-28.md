# Skill improvements — frictions observed while running the smoke test (2026-05-28)

Run: full `/smoke-test` against `outbound@lab5.ca` / `inbound@lab5.ca` with the new B7.5 concurrent-dual-send gate active. 11 of 12 gates passed; B7.5 surfaced a serialization Bug (filed separately as §B). This doc captures the *friction* — the things the operator had to work around or guess at while executing the skill, in order of "would bite the next operator first".

## 1. `agent.invoke` carries no `email_id` attribute, but gate B7.5 Logfire query depends on it

Gate B7.5 prose says: *"Exactly 2 `agent.invoke` spans, each with its own `email_id` attribute matching B75a / B75b."*

Actual `agent.invoke` attributes observed on this run:
`contact_id`, `workflow_id`, `trigger`, `workflow_type`, `input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `tool_call_count`, `prompt_length`, `model`, `result`, `status`, `agent_reasoning`, `output_tokens`, `total_tokens`, `llm_requests`.

No `email_id`. An operator pasting the gate's literal SQL gets zero rows and concludes the spans are missing.

**Fix options:**
- **(a) Span-side:** add `email_id` to the rollup span attrs in `src/mailpilot/agent/invoke.py` (`invoke_workflow_agent`). Would also help downstream cost-per-message dashboards.
- **(b) Skill-side:** rewrite the gate to identify B75a vs B75b by `agent_reasoning` substring or by joining child `running tool` spans grouped under the same `trace_id`.

(a) is the better default — `email_id` is a load-bearing dimension for per-message cost / latency analysis, not just for this gate.

## 2. SLA window starts at `T_SEND`, but the agent can't start until the next sync tick

Gate B4 / B7 / B7.5 grade reply latency against `T_SEND_*` (operator wall-clock send), but the agent can only start once `mailpilot run`'s sync loop next ticks and pulls the routed inbound. This run measured a ~12s sync lag (T_SEND_B75=15:14:24Z, first `agent.invoke` start=15:14:36Z) — that lag is unavoidable architectural state, not agent latency.

The 60s SLA per reply should be specified against `agent.invoke.end_timestamp - T_SEND`, derived from Logfire, with the 5s `email list` poll only used as a "did the reply round-trip back to the outbound mailbox at all" gate.

Tracked separately as Invariant 5.

## 3. Polling overhead — 5s cadence inflates latency and floods CLI invocations

Every email-presence poll re-runs `mailpilot email list ...` and re-parses JSON. Under coarse 5s cadence and ~30s+ agent runs this produces 6–13 redundant CLI invocations per gate (B4 alone hit 6; B7 hit 11). Worse, the 5s cadence inflates measured latency: B7's true agent span was 51s but the poll first observed the reply at attempt 11 (~64s polled), producing a borderline result that needed cross-checking against Logfire.

**Fix options:**
- **(a)** A `--watch` mode on `mailpilot email list` that streams new rows over `LISTEN/NOTIFY`. Eliminates polling entirely.
- **(b)** Poll the `task` table instead of `email` — `mailpilot run` updates task status within a tick of agent finish, granularity is the same but rows are smaller.
- **(c)** Read latency from Logfire `agent.invoke.end_timestamp` directly (also Invariant 5).

## 4. `EmailSummary` projection drops `gmail_thread_id`, `sender`, `recipients`

Gate A4 needs `gmail_thread_id` to confirm threading worked. It's on the `view` payload but absent from the `list` projection. Same for `sender` and `recipients` (the actual field names — not `from_address` / `to_addresses` as I assumed mid-run).

**Fix options:**
- **(a)** Add `gmail_thread_id` to `EmailSummary` per §V.51 — it's an FK-like join key for the routing pipeline, not display-only. (`sender` and `recipients` are arguably display fields, can stay in view.)
- **(b)** Update the SKILL.md A4 prose to explicitly say "after matching by subject in `list`, fetch detail via `view` to read `gmail_thread_id`."

## 5. Python `f"...{d[\"x\"]}"` inside `python3 -c "..."` is silently mangled by zsh

The Conventions section warns about `echo "$VAR" | python3 -c ...` (zsh swallows `\n` inside JSON). The same friction class hits any:

```
python3 -c "import json; d=json.load(sys.stdin); print(f'x={d[\"x\"]}')"
```

zsh sees the backslash-quotes and breaks the python source. The fix is mixing quote styles — single inside double:

```
python3 -c "import json,sys; d=json.load(sys.stdin); print('x=', d['x'])"
```

Worth a one-liner in Conventions alongside the existing `echo` warning.

## 6. §V.29 spec-table lint is too narrow — agent slips through with single-space "tables"

Filed separately as Bug 2. The `_check_spec_table` regex requires `\s{2,}` between label and value; the agent learned to render specs as `**label** value` (single space + bold), which slips the lint AND visually presents as a table thanks to the bold.

Fix is either widening the heuristic or moving presentation enforcement to a system-prompt-level regression assertion that's graded operator-side at smoke-test time.

## 7. SKILL.md references `classify_email` span, actual span name is `agent.classify_email`

Filed separately as Bug 4. Mechanical doc fix.

## 8. Subject generator yields obscure 2-word dictionary topics

This run got `[ST-110844] taur stallment`, `[ST-110846] tarbet matrix`, etc. Fine for Gmail uniqueness but visually low-signal — if the operator later greps for "taur" they'll wonder. Consider switching default to 1 longer word (`sort -R /usr/share/dict/words | grep -E '^[A-Za-z]{6,12}$' | head -1`) so subjects stay distinguishable at a glance. Minor; the constraint is "distinct", which both forms satisfy.

## 9. Gate numbering — B6.5 between B6 and B7 in file order, but runs after B7

B6.5 is documented as running after B7's tool-use gate (it reads B7's Logfire window). The new B7.5 also reads post-B7 spans. File-position ordering (B6, B6.5, B7, B7.5) misleads the eye into thinking B6.5 happens before B7.

**Fix options:**
- **(a)** Rename B6.5 → B7.6 (post-B7 race check) and the new gate stays B7.5. Operational and file order align.
- **(b)** Group all post-B7 Logfire-only checks under one numbered section like "B7.6 Logfire regression sweep" with two subsections (race, concurrency).

## 10. Phase 0 KB-visibility gate doesn't catch model-number collisions across `source_file`s

Two files describe the WS36-600-2 softener model with different numbers:

- `pure-aqua-industrial-water-softener.md` — 110 GPM continuous / 125 GPM peak (qa-in-022)
- `pure-aqua-sf-100s-industrial-water-softener.md` — 65 GPM continuous / 120 GPM peak (qa-in-026)

The agent in B75a flagged the discrepancy and grounded against the SF-100S file. Operator graded as PASS (excellent reply quality) but `cites_source_file=true` was ambiguous since the citation didn't match the pair's pinned `source_file`.

Tracked separately as Invariant 6 (proposed `source_file_alts: list[str]` on in-scope pairs).

## 11. Personalization gate A1a's body-token check is order-fragile

Gate A3 says `body_text` must contain both `$CONTACT_NOTE_TOKEN` and `$COMPANY_NOTE_TOKEN`. This ran clean. But if the agent ever inlines them inside the spec-table cells (e.g. `| Reference | 3c74a4f6e31d |`) instead of the expected `Reference: <token>` line, the gate would still pass on substring match while silently regressing the "agent echoes the note's Reference:" semantic. Consider tightening to `Reference: <token>` substring.

Minor risk; surfaces only on a prompt change.

---

## Cross-cutting theme

Most of the friction above is the smoke-test skill encoding its expectations against the operator's wall-clock view (email send, mailbox poll) rather than the runtime's Logfire span view. Logfire is the authoritative source for *what happened* — the operator's CLI invocations are tooling around that. The skill's next major edit should consider standardizing on Logfire as the gate source-of-truth wherever a span exists, with CLI gates reserved for *side-effect verification* (entity created, row persisted, etc.).

This also aligns with §V.46 (liveness probes query authoritative production-facing surface, not local DB mirror).
