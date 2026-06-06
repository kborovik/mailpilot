# enrollment.id — scalar UUIDv7 PK

## Problem

`enrollment` is the only first-class entity in the system w/o a scalar UUID id. PK = composite `(workflow_id, contact_id)` per `schema.sql`. Consequences observed in spec:

- **§V.6 SKILL.md audit recipe carves out a special case** for enrollment — "recipes for composite-PK entities (`enrollment` `run`/`view`/`update`/`remove`) ⊥ render single-positional id form — `enrollment` ⊥ scalar id per `schema.sql` composite PK". That carve-out is a smell: the doc surface bends around an entity shape that does not fit the project's own conventions.
- **Every enrollment-touching CLI verb takes two flags** (`--workflow-id`, `--contact-id`) where every other single-id entity verb takes one positional arg (`account view <ID>`, `email view <ID>`, `task cancel <ID>`).
- **`task` rows denormalize `workflow_id` + `contact_id`** (§V.28, §V.32) instead of carrying a `task.enrollment_id` FK. Idempotency filters and Logfire dashboards reach for the pair where one id would do. Worse: today a `task` row may exist w/o a corresponding `enrollment` row (no FK enforces it), so the implicit "task belongs to an enrollment" relationship is unenforced.
- **§V.12 already mandates UUIDv7 for every ID** — enrollment is structurally non-compliant per the letter of §V.12 ("∀ ID = UUIDv7 via `_new_id()`. ⊥ uuid4, ⊥ serial.") since enrollment effectively has no ID at all.
- **Forcing function:** the parked `designs/enrollment-preview.md` is the 6th enrollment verb that would take the composite pair. Adding a scalar id now collapses preview's selection surface to a single flag, and unblocks symmetric reduction of the other five verbs in one bundled migration.

## Proposal

Add `enrollment.id UUID PRIMARY KEY DEFAULT _new_id()` (UUIDv7 per §V.12). Demote `(workflow_id, contact_id)` to `UNIQUE` constraint to preserve the at-most-one-enrollment-per-(workflow,contact) invariant. Migrate CLI verbs, agent tools, and `task` FK to address enrollment by scalar id. Introduce new invariant: every `task` row has a corresponding `enrollment` row.

### Schema shape

`schema.sql` updates in place — `make clean` re-applies (§V.18). No ALTER, no data migration. IDs follow project convention: `TEXT PRIMARY KEY`, generated in Python via `_new_id()` (= `uuid.uuid7()` per §V.12), never DB-side default.

```sql
CREATE TABLE IF NOT EXISTS enrollment (
    id            TEXT PRIMARY KEY,
    workflow_id   TEXT NOT NULL REFERENCES workflow(id),
    contact_id    TEXT NOT NULL REFERENCES contact(id),
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'paused')),
    reason        TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (workflow_id, contact_id)
);

CREATE INDEX IF NOT EXISTS idx_enrollment_contact_id ON enrollment(contact_id);
```

`UNIQUE (workflow_id, contact_id)` preserves §V.15 enrollment-status semantics and the implicit "one enrollment per workflow per contact" rule today carried by the composite PK. Existing `reason` + `updated_at` columns and `idx_enrollment_contact_id` index retained.

### task table

```sql
CREATE TABLE IF NOT EXISTS task (
    id             TEXT PRIMARY KEY,
    enrollment_id  TEXT NOT NULL REFERENCES enrollment(id),
    workflow_id    TEXT NOT NULL REFERENCES workflow(id),
    contact_id     TEXT NOT NULL REFERENCES contact(id),
    email_id       TEXT REFERENCES email(id),
    description    TEXT NOT NULL,
    context        JSONB NOT NULL DEFAULT '{}',
    scheduled_at   TIMESTAMPTZ NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'completed', 'failed', 'cancelled')),
    result         JSONB NOT NULL DEFAULT '{}',
    attempt_count  INTEGER NOT NULL DEFAULT 0,
    completed_at   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

`enrollment_id` non-null. Every `task` row has a corresponding `enrollment` row — new invariant per §V.28 update below. `task.workflow_id` and `task.contact_id` columns stay (used by operator filters in `mailpilot task list`, Logfire dashboards, and §V.28's email-routing filter `e.created_at >= w.created_at`). Net: `task` rows carry `enrollment_id` + redundant `workflow_id` + `contact_id` denorm — explicit cost paid for ergonomics and existing filter compat. Existing indexes (`idx_task_workflow_id`, `idx_task_contact_id`, `idx_task_scheduled_at`) and the `notify_task_pending` trigger unchanged.

### activity table

```sql
CREATE TABLE IF NOT EXISTS activity (
    id              TEXT PRIMARY KEY,
    contact_id      TEXT REFERENCES contact(id),
    company_id      TEXT REFERENCES company(id),
    email_id        TEXT REFERENCES email(id),
    workflow_id     TEXT REFERENCES workflow(id),
    task_id         TEXT REFERENCES task(id),
    enrollment_id   TEXT REFERENCES enrollment(id),
    type            TEXT NOT NULL
                    CHECK (type IN (
                        'email_sent', 'email_received',
                        'note_added', 'tag_added', 'tag_removed',
                        'status_changed',
                        'enrollment_added',
                        'enrollment_completed', 'enrollment_failed',
                        'enrollment_paused', 'enrollment_resumed'
                    )),
    summary         TEXT NOT NULL DEFAULT '',
    detail          JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (contact_id IS NOT NULL OR company_id IS NOT NULL)
);
```

`enrollment_id` nullable — enrollment-lifecycle activity types (`enrollment_added`, `enrollment_completed`, `enrollment_failed`, `enrollment_paused`, `enrollment_resumed`) populate it; other activity types leave it NULL. §V.17 multi-target rule unaffected (still requires at-least-one of {contact_id, company_id}).

### CLI surface migration

Live CLI convention: positional `<ID>` for single-required-id verbs (`account view <ID>`, `email view <ID>`, `task cancel <ID>`); flagged for multi-arg ops and optional-id ops. Enrollment verbs migrate to match:

```
mailpilot enrollment add      --workflow-id <WID> --contact-id <CID> [--scheduled-at <ISO>]
mailpilot enrollment run      <ENROLLMENT_ID>
mailpilot enrollment view     <ENROLLMENT_ID>
mailpilot enrollment update   <ENROLLMENT_ID> [--status active|paused]
mailpilot enrollment remove   <ENROLLMENT_ID>
mailpilot enrollment list     [--workflow-id <WID>] [--contact-id <CID>]
```

`enrollment add` keeps `--workflow-id` + `--contact-id` (the constructor pair); response envelope carries the freshly-minted `id`. `enrollment list` keeps both pair-flags as optional filter dimensions.

### Agent tool surface migration

`list_enrollments` return adds `id` field per row (alongside `workflow_id`, `workflow_name`, `contact_id`, `contact_email`, status — see Q7 decision below). `record_enrollment_outcome` signature changes:

```python
# before
record_enrollment_outcome(workflow_id: str, contact_id: str, outcome: str, ...)
# after
record_enrollment_outcome(enrollment_id: str, outcome: str, ...)
```

Tool definitions live in `src/mailpilot/agent/tools.py`. The `enrollment_id` arg name matches the CLI flag and stays consistent w/ existing tool arg conventions (`email_id`, `contact_id`, `workflow_id`). Protocol fragment text in `_DEFERRED_TASK` (`src/mailpilot/agent/templates.py`, §V.45) referencing `record_enrollment_outcome` updates accordingly.

### Operator event payload migration (§V.54)

All enrollment events (`enrollment.add`, `enrollment.update`, `enrollment.remove`, `enrollment.start`, `enrollment.stop`, `enrollment.cancel`) carry `entity_id = enrollment.id`. Matches the §V.54 template `entity_id=...` uniformly across nouns. No §V.54 text change required — the invariant is uniform across nouns; this migration just makes enrollment compliant.

### §V.6 SKILL.md audit-recipe simplification

The composite-PK carve-out in §V.6 deletes. SKILL.md recipes use `mailpilot enrollment {run|view|update|remove} <ENROLLMENT_ID>` positional uniformly. §V.6 audit recipe (mechanical rule) treats enrollment exactly like every other single-id entity post-migration.

`src/mailpilot/SKILL.md:160-165` currently shows the flagged forms (`--workflow-id <WID> --contact-id <CID>`) for the four single-id enrollment verbs; those lines rewrite to positional `<ENROLLMENT_ID>` in the same commit. `enrollment add` (constructor pair) and `enrollment list` (optional filters) stay flagged.

### §V.28 task creation update — populate task.enrollment_id

`create_tasks_for_routed_emails` (in `src/mailpilot/database.py`) populates `task.enrollment_id` per row. Enrollment row already exists at this point: `routing.py:_ensure_enrollment` runs earlier in the inbound pipeline and inserts the enrollment + emits the `enrollment_added` activity. By the time `create_tasks_for_routed_emails` runs, the enrollment row is guaranteed present.

Inbound flow:

1. Routed inbound email arrives w/ `workflow_id` + `contact_id` set; `_ensure_enrollment` has already run.
2. Per email: `SELECT id FROM enrollment WHERE workflow_id=? AND contact_id=?` to fetch the id (or hoist to a JOIN in the outer SELECT that already drives task creation).
3. `task.enrollment_id` populated from result.

Outbound first-touch path (§V.32) already creates the enrollment row before scheduling the first task; pass `enrollment.id` directly into `create_task`. No second task-creation path exists.

**Single activity emit site.** `enrollment_added` continues to be emitted by `routing.py:_ensure_enrollment` only. `create_tasks_for_routed_emails` does NOT emit a second activity row. No `detail.source='auto'` marker needed — every enrollment is created via either `_ensure_enrollment` (inbound routing) or `enrollment add` (operator-explicit); the call site is the distinguishing marker, not a payload field. Activity log remains the auditable lifecycle history; one row per lifecycle event.

§V.28 wording extends to declare `task.enrollment_id` population during task creation. §V.32 wording extends to declare `task.enrollment_id` population during outbound first-touch scheduling. §V.54 unaffected.

### §V.32 idempotency filter update

Pre: `task.workflow_id = WF AND task.contact_id = CT AND task.email_id IS NULL AND task.status = 'pending'`
Post: `task.enrollment_id = E AND task.email_id IS NULL AND task.status = 'pending'`

Same semantics, one column instead of two.

### Migration mechanics

No data migration. `make clean` recreates schema per §V.18 / §T.13 / §T.34 / §T.47 precedent. `mailpilot status` `schema.drift` will fire on existing dev/test DBs until `make clean` runs (correct behavior per §V.18). Existing `workflow export` / `workflow import` round-trip (§V.63) unaffected — enrollment is not exported. Tests reset their fixtures per `database_connection` truncation per `conftest.py`.

### Internal DB lookups

CLI ∧ agent-tool surfaces collapse to scalar `enrollment_id`. Internal DB-layer composite-key lookups (`get_enrollment(connection, workflow_id, contact_id)` in `run.py`, `routing.py`) MAY remain post-migration — the `UNIQUE (workflow_id, contact_id)` constraint keeps composite-key lookups valid. A `get_enrollment_by_id(connection, enrollment_id)` companion ships alongside; conversion of existing composite-key call sites is optional cleanup, not migration-blocking.

### Backwards compat

Clean break. No `--workflow-id` + `--contact-id` alias on the migrated verbs. Per §V.11 precedent ("Backwards compat ⊥ retained — operator confirmed no scripts rely on prior shape; release-note bump in same commit"), MailPilot's house style is sharp reshape on structural changes, with the release note carrying the migration story. SKILL.md updates in the same commit (§V.6 audit recipe).

## Effect on in-flight SPEC items

- **§V.6** — composite-PK carve-out deletes; audit recipe gains the positional-form row for enrollment. Material simplification.
- **§V.12** — no text change, but the universal quantifier now actually holds for enrollment (today it does not, strictly).
- **§V.15** — unchanged (status enum binds the column, not the PK shape).
- **§V.17** — unchanged.
- **§V.28** — wording amended: task creation upserts enrollment via race-safe ON CONFLICT pattern and sets `task.enrollment_id`. Auto-create emits `enrollment_added` activity row w/ `detail.source='auto'`.
- **§V.32** — idempotency filter switches from `(workflow_id, contact_id)` pair to `enrollment_id`. One-row spec touch.
- **§V.54** — `entity_id` in enrollment events becomes `enrollment.id` (no §V.54 text change — uniform across nouns).
- **§V.63** — `workflow export`/`import` unaffected (enrollment not exported).
- **§V.64** — new test coverage for migrated CLI cmds; FK validators patch list extends w/ `get_enrollment_by_id`. Fixtures rewrite across: `tests/test_cli.py` (enrollment verbs), `tests/test_database.py` (Enrollment model + task/activity FK assertions), `tests/test_agent_tools.py` (`record_enrollment_outcome` signature, `list_enrollments` projection), `tests/test_agent_invoke.py` (deferred-task protocol references), `tests/test_routing.py` (`_ensure_enrollment` + `task.enrollment_id` population).
- **new invariant** — every `task` row has a corresponding `enrollment` row (enforced via `task.enrollment_id NOT NULL` FK). Worth a §V row of its own; spec amend lands it.
- **enrollment-preview design (parked at `designs/enrollment-preview.md`)** — selection surface collapses to `--enrollment-id <ID>` (or positional `<ID>` per the new convention) on resume. Q1 of preview resolves automatically. `designs/enrollment-preview.md:15-19` CLI block rewrites at resume time; preview design file itself is not edited as part of this PR.

## Design decisions

**Decision (Q1):** Positional `<ENROLLMENT_ID>` for `enrollment view|run|update|remove`. Flagged for `enrollment add` (constructor pair) and `enrollment list` (optional filters).
**Why:** matches live CLI convention — positional for single-required-id verbs, flagged for multi-arg ops, confirmed via spot-check of `account view <ID>` (cli.py:331-332), `email view <ID>` (cli.py:1135-1136), `task cancel <ID>` (cli.py:2886-2887), and `account sync --account-id` (cli.py:382-387, flagged because id is optional). §V.6 §B.47 entries about positional drift were three distinct cases sharing a verb-shape, not a project-wide flagged convention.

**Decision (Q2):** Agent tool arg name = `enrollment_id: str`.
**Why:** matches CLI flag and existing tool arg conventions (`email_id`, `contact_id`, `workflow_id`). `id` alone is ambiguous in tools that also touch contact/workflow.

**Decision (Q3):** `task.enrollment_id NOT NULL`. `create_tasks_for_routed_emails` auto-upserts the enrollment row.
**Why:** every task belongs to an enrollment is the symmetrical invariant — §V.32 already creates enrollment before scheduling first-touch tasks; §V.28 should mirror that. Explicit FK > implicit via (workflow_id, contact_id) denorm. Race-safe upsert per §V.16 pattern.

**Decision (Q3a):** Auto-created enrollment `status = 'active'`.
**Why:** schema default; inbound from contact is implicit consent to enroll. Terminal state vocabulary is `record_enrollment_outcome` per §V.15, not `enrollment.status` sentinel values.

**Decision (Q3b):** Auto-create emits `enrollment_added` activity row w/ `detail.source = 'auto'`.
**Why:** activity log = auditable lifecycle history regardless of trigger source. `source='auto'` marker distinguishes implicit-via-inbound from operator-explicit `enrollment add`. §V.54 unaffected (CLI-handler-scoped invariant); DB-layer emission is parallel observability.

**Decision (Q6):** Bundle the migration into one PR / one spec amend.
**Why:** half-migrated state (some surfaces use `--enrollment-id`, others use the pair) is worse than either endpoint. Atomic cutover matches §V.11 / §T.54 / §T.29 sharp-reshape precedent.

**Decision (Q7):** `list_enrollments` per-row projection = `{id, workflow_id, workflow_name, contact_id, contact_email, status}`.
**Why:** §V.5 parent-NI rule (denormalized NI paired w/ FK) + §V.7 FK rule (list projections include FK columns). `workflow_name` and `contact_email` are display ergonomics; `workflow_id` and `contact_id` are the canonical join keys.

## Success criterion

- `enrollment.id` column exists, UUIDv7, PRIMARY KEY. `(workflow_id, contact_id)` enforced via UNIQUE constraint.
- All enrollment CLI verbs except `add` and `list` take positional `<ENROLLMENT_ID>` as their sole selection arg.
- `mailpilot --skill` audit (§V.6 recipe) passes against the new CLI surface, w/ no enrollment-shaped carve-out.
- `task.enrollment_id` NOT NULL across every task row; `create_tasks_for_routed_emails` auto-upserts enrollment for every routed inbound email.
- §V.32 idempotency filter uses `task.enrollment_id`, single column.
- Agent tool `record_enrollment_outcome` takes `enrollment_id` only; all three template protocols compile and pass the tool-list contract test.
- `list_enrollments` per-row projection includes `{id, workflow_id, workflow_name, contact_id, contact_email, status}`.
- Auto-created enrollments emit `enrollment_added` activity rows w/ `detail.source='auto'`.
- `make clean && make check` green.
- `/smoke-test` Scenario A (outbound) + Scenario B (inbound) PASS without behavior delta.

## Out of scope

- Dropping `task.workflow_id` + `task.contact_id` denorm columns — keep both for filter-path and dashboard compat. Re-evaluate after one release cycle.
- Renaming `activity` enrollment-event types (`enrollment_added`, etc.) — naming is fine; only the FK reach changes.
- Migrating historical data — `make clean` is the migration story.
- Adding `enrollment.id` to `workflow export` / `workflow import` payload — enrollment is not exported per §V.63.

## Unresolved

(none — all Open Questions resolved)
