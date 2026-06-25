# Workflow goal: the field that concludes an enrollment

## Problem

The workflow definition carries a single-line field `objective` (§V.103,
alongside name, template, theme, instructions). The field serves two roles today
and SPEC defines neither.

1. Classification key — classify.py semantically matches an inbound email against
   each active workflow's objective to pick the owning workflow.
2. Conclusion trigger — the `_DEFERRED_TASK_TASK` template fragment tells the
   agent to call `record_enrollment_outcome` with outcome 'completed' after
   completing the workflow objective.

§V.103 lists objective as a field name and nothing more. The conclusion role
lives only in a prose template string. So the answer to "when does the agent
close the enrollment" rests on the agent's free-text read of an undefined field.
The calendar-meetings design (designs/calendar-meetings.md) adds
`conclude_enrollment`, which assumes a clear success condition that no invariant
defines.

## Proposal

Rename objective to `goal` and define it in SPEC as the workflow success
condition: the observable outcome that, once reached, concludes the enrollment.
Example: the prospect books a Google Meet.

The goal is one free-text prose field. The agent interprets it. It keeps both
roles.

- Conclusion — the agent concludes via `conclude_enrollment` (calendar-meetings
  design) when it judges the goal met (meeting_booked) or unreachable
  (do_not_contact, contact_later). The system concludes deterministically when a
  hard signal proves the goal met (the calendar booking).
- Classification — the goal stays the semantic-match key for inbound routing. One
  field, two readers.

The goal says what success is. `conclude_enrollment` is how the agent records the
terminal. `record_enrollment_outcome` stays the internal recorder (§V.15). The
two compose: goal-met leads to disposition meeting_booked; goal-refused leads to
do_not_contact or contact_later.

## Migration: objective to goal (006)

Schema and migration:

- schema.sql line 52 — column becomes `goal TEXT NOT NULL DEFAULT ''`.
- new migrations/006_rename_workflow_objective_to_goal.sql — `ALTER TABLE
  workflow RENAME COLUMN objective TO goal;`. Byte-identity holds (§V.108): fresh
  init equals migrate-from-zero, because 001 still creates objective and 006
  renames it. Migrations 001 through 005 stay untouched.

Code:

- models.py line 155 — `objective: str` leads to `goal: str` on Workflow.
- database.py — search_workflows SQL, update_workflow allowed set,
  `_WORKFLOW_IMPORT_UPDATABLE` tuple.
- cli.py — `--objective` leads to `--goal` on workflow create and update; param
  names; help text; the two activate and start validation messages; the TOML
  export line; the TOML import entry.get read; def-field docstrings.
- classify.py — column reads, the classification prompt text, the "objective"
  dict key.
- invoke.py — the `Objective:` prompt line becomes `Goal:`; docstring.
- templates.py line 107 — `_DEFERRED_TASK_TASK` wording sharpens to "After
  achieving the workflow goal".
- tools.py line 413, routing.py line 323 — comment and prompt text.

TOML catalog (detached workflows/ repo):

- workflows/mailpilot-demo.toml and workflows/ai-engineering.toml — `objective =
  ...` leads to `goal = ...`. Committed and pushed in /Users/kb/github/workflows
  automatically. Export to dir to import round-trip stays idempotent because the
  field name moves in lockstep.

Tests:

- fixtures, conftest helpers, and every assertion over the CLI flag, model field,
  TOML round-trip, classify, and invoke prompt.

## Effect on in-flight SPEC items

- §V.103 — the workflow def field list renames objective to goal; the field count
  and the 1:1 row mapping stay unchanged.
- §I — the `workflow` CLI noun `--objective` renames to `--goal` (create, update);
  agent-prompt and classification references rename.
- §V.15 — `record_enrollment_outcome` semantics stay intact; conclusion now reads
  against a defined goal rather than an undefined objective.
- New §V invariant — goal is the workflow success condition; reaching it concludes
  the enrollment; the field doubles as the inbound classification key; free-text
  prose, agent-interpreted.
- §V.108 — one new migration 006; byte-identity preserved.
- Relationship to designs/calendar-meetings.md — that design's
  `conclude_enrollment` is the agent's terminal for a met or refused goal; this
  design supplies the goal definition it assumes. The goal §V should land before
  or with `conclude_enrollment`.

## Design decisions

- **Decision:** Name the field goal, migrating from objective. **Why:** the
  field's job is to define when the enrollment closes, which is goal-attainment
  language; objective was overloaded with the classification role and never
  defined in SPEC.
- **Decision:** One field serves both classification-match and
  conclusion-definition. **Why:** a well-formed goal statement is also the best
  classification key; a second field earns nothing (YAGNI).
- **Decision:** The goal stays free-text prose the agent interprets. **Why:**
  `conclude_enrollment`'s disposition enum already supplies the structured
  terminal; a booking concludes every active outbound enrollment regardless of
  stated goal, so a typed goal buys no scoping the design wants.
- **Decision:** Migrate rather than keep objective. **Why:** a one-time rename
  plus migration 006 is cheap and mechanical (the T168 pattern); leaving the name
  overloaded keeps the SPEC gap open.

## Success criterion

- SPEC defines a goal invariant; the word objective no longer names the field in
  schema, models, CLI, agent prompts, TOML, or §V.103.
- Fresh init and migrate-from-zero both end with column workflow.goal and stay
  byte-identical (§V.108).
- Export to dir to import round-trip over a workflow with a goal stays idempotent.
- The agent concludes an enrollment by reading a defined goal; the
  `_DEFERRED_TASK_TASK` fragment names the goal, not an objective.
- classify.py routes an inbound email by matching the goal field.

## Out of scope

- `conclude_enrollment`, the meeting entity, and calendar ingestion — designed in
  designs/calendar-meetings.md, not restated here.
- Structured or typed goals — deferred; free-text prose ships, and the
  disposition enum carries the structured terminal.
- Per-workflow goal-type scoping of the deterministic calendar conclusion — out;
  a booking concludes every active outbound enrollment.
