# enrollment preview command

## Problem

Operator wants to test workflow wording and contact/company context grounding without sending mail or persisting state. Today the only way to exercise a workflow agent end-to-end is `mailpilot enrollment run`, which sends real email and writes `email` rows, `task` rows, and `activity` rows.

No spec anchor on a read-only/dry-run surface yet — closest neighbors: §V.26 trigger taxonomy, §V.54 mutation-event contract, §V.68 reply-time fact-check ledger, §V.4 envelope shape.

## Proposal

New CLI verb `mailpilot enrollment preview` on the `enrollment` noun. Runs the workflow agent against a chosen enrollment in read-only mode — same code path as `enrollment run`, with outbound side-effect tools shimmed to capture-only and DB writes suppressed via transaction rollback. Output = the proposed email envelope (to / subject / body) plus tool transcript.

### Shape

```
mailpilot enrollment preview --workflow-id <WID> --contact-id <CID>
                             [--trigger {enrollment_run|enrollment_schedule|task}]
                             [--task-id <TID>]            # only valid w/ --trigger task
```

NOTE: surface collapses to `--enrollment-id <ID>` once the enrollment_id design lands and is folded into SPEC.md. This draft assumes pre-enrollment_id shape; refresh on resume.

**Envelope** (per §V.4 singular):
```json
{
  "preview": {
    "workflow_id": "...",
    "contact_id": "...",
    "trigger": "enrollment_run",
    "proposed_email": {
      "to": "contact@example.com",
      "subject": "...",
      "body_text": "<verbatim agent draft>",
      "tool": "send_email" | "reply_email",
      "in_reply_to": null,
      "references": null
    },
    "tool_calls": [
      {"name": "read_contact", "args": {...}, "result": {...}},
      {"name": "read_company", "args": {...}, "result": {...}}
    ],
    "agent_messages": [...],
    "usage": {"input_tokens": N, "output_tokens": N, "cache_read_input_tokens": N}
  },
  "ok": true
}
```

Agent runs `noop` or decides not to send → `proposed_email: null` w/ `decision: "noop"|"declined"|"no_outbound_tool_called"`.

### Side-effect gating (structural)

1. **Tool substitution** — `send_email`, `reply_email` replaced w/ capture-only shims that:
   - Run §V.68 pre-send fact-check ledger verbatim (whole point of preview is testing wording/grounding).
   - On fact-check PASS → return `{"ok": true, "preview": true, "id": "<synthetic-uuid7>"}` to agent so it terminates cleanly.
   - Capture `{to, subject, body, in_reply_to, references}` into preview output.
   - No Gmail API touch, no `email` row persist.
2. **Tool gating** — `create_task`, `cancel_task`, `record_enrollment_outcome`, `disable_contact` replaced w/ capture-only shims (return `{"ok": true, "preview": true}`); intent captured in `tool_calls[]`.
3. **Read-only tools unchanged** — `read_contact`, `read_company`, `read_email`, `search_emails`, `list_enrollments`, `read_drive_markdown`, `list_drive_markdown`, `search_drive_markdown`, `noop` run normally → realistic context.
4. **DB suppression** — entire handler runs inside `BEGIN ... ROLLBACK`. CLI handler opens its own connection, so a final `connection.rollback()` discards any incidental writes.
5. **Span carries `preview=True` attr** — `agent.invoke` span attaches `preview: bool` attribute alongside existing `trigger` attr (§V.26) so Logfire dashboards can filter preview noise out of regression metrics. Trigger value unchanged from the simulated path.

### Tool ownership

Shims live in new module `src/mailpilot/agent/preview.py` returning a `PreviewTools` dataclass: `(tools: list[Tool], captured: CapturedSideEffects)`. Builder takes the template's normal tool tuple + the `PreviewDeps` instance, returns a tool list w/ shimmed entries swapped in. Same `Agent(...)` instantiation path as `_build_agent`; `instructions` unchanged per §V.45.

### Selection — "next enrollment task" interpretation

User phrased it as "next enrollment task" (singular default), so default selection should be ergonomic:
- Explicit form: `--workflow-id W --contact-id C` (mirrors `enrollment run`) → preview that enrollment's next outbound message. Default `--trigger enrollment_run`.
- Future-deferrable: a `--next` flag that picks the oldest-due pending `task` row across all enrollments. See Q1 under Unresolved.

### §V.54 observability stance

Preview = not a CRM-config mutation (no `create|update|disable|add|remove|start|stop|cancel|import` verb) → §V.54 obligation does not apply. But emit a single `operator_event("enrollment.preview", workflow_id, contact_id, trigger, decision)` for symmetry w/ smoke-test observability.

## Effect on in-flight SPEC items

- **§V.26** — `agent.invoke` trigger attr untouched; preview leans on existing values. New `preview: bool` attr is additive → §V.26 enum unchanged. May warrant a follow-up amend declaring the attr or rolling it into §V.26 description; not blocking.
- **§V.54** — preview verb deliberately scoped *outside* operator-event obligation (read-only). See Q6 under Unresolved.
- **§V.68** — fact-check ledger fires *inside* the shimmed `reply_email`/`send_email` → preview honors §V.68 verbatim. Test extends: shimmed reply path w/ fabricated WS36-600-2=65 still returns `fact_check_mismatch`, drives agent re-draft.
- **§V.4** — new singular envelope `preview`; verb `preview` added to verb list under §I (one-line amend).
- **§V.64** — new CLI cmd → tests patch `get_workflow`, `get_contact`, `get_account` FK validators per CLAUDE.md gotcha.
- **§V.6** — SKILL.md audit recipe entry added: `enrollment preview` recipe must use the same selection-flag form as the other enrollment verbs (will collapse to `--enrollment-id <ID>` post-enrollment_id fold-in).

## Design decisions

(none — all open questions parked pending enrollment_id design)

## Success criterion

- `mailpilot enrollment preview --workflow-id W --contact-id C` returns proposed email envelope w/ no Gmail send, no `email` row, no `task` row, no `activity` row written.
- §V.68 fact-check ledger triggers `fact_check_mismatch` on fabricated spec values inside preview (test: agent fabricates WS36-600-2=65 → error returned, agent re-drafts).
- `mailpilot account list`, `mailpilot email list`, `mailpilot task list` show no state delta after a preview run.
- `agent.invoke` Logfire span carries `preview=True` attr; existing dashboards can filter on it.

## Out of scope

- Preview UI / interactive flow — JSON envelope only.
- Saving previews as persistable drafts — separate `enrollment draft` surface, future work.
- Preview against arbitrary historical emails (replay mode) — distinct use case.
- Inbound preview against a fake/synthetic email body — preview takes existing routed state, does not synthesize new inbound triggers.

## Unresolved

Q1 — **Selection semantics:** explicit `--workflow-id` + `--contact-id` only, or also support `--task-id <TID>` (preview a specific queued task, drained as `trigger=task`) and `--next` (preview the oldest-due pending task)? `--next` is closest to the verbatim phrasing but adds drain-queue plumbing.

Q2 — **Verb name:** `enrollment preview` vs. `enrollment dry-run` vs. `enrollment draft`. `preview` reads cleanly and does not collide w/ existing verbs; `draft` overloads the noun (drafts as persistable rows = future surface); `dry-run` is conventional flag naming, not verb naming.

Q3 — **Inbound trigger preview:** should `preview` also support inbound auto-reply previews (i.e. `--trigger task --task-id <TID>` against an existing routed inbound email) or stay scoped to outbound first-touch only? Inbound case is harder (needs an existing email + routed task) but it is where workflow wording matters most for KB-grounded replies.

Q4 — **Tool-call transcript verbosity:** full message trace incl. system prompt + every chat turn, or compact summary (tool names + args + trimmed results, system prompt elided)? Full trace = large output but matches "test workflow wording" intent; compact = JSON-greppable.

Q5 — **Tool-shim return contract:** when `send_email`/`reply_email` is shimmed, return `{"ok": true, "preview": true, "id": "<synthetic-uuid7>"}` (agent sees structure ~ real return) or a sentinel `{"ok": true, "preview": true, "no_id": true}`? Synthetic ID is more realistic; sentinel is more honest. §V.39 binds error-dict shape only → either is admissible.

Q6 — **§V.54 carve-out wording:** amend §V.54 to declare read-only verbs (`preview`, future kin) exempt, or leave §V.54 alone and trust the verb-list enumeration? Latter is YAGNI-correct (§V.54 enumerates verbs it binds; `preview` not in that list → not bound by it).

Q7 — **Sequencing (resolved → parked):** enrollment_id design runs first (separate `/sdd:design enrollment-id` pass). Once enrollment_id is folded into SPEC.md, this preview design refreshes: selection surface collapses to `--enrollment-id <ID>`; Q1 mostly resolves itself; §V.6 audit-recipe entry simplifies; some FK references in operator-event payloads change.
