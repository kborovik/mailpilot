# Rename `workflow_contact` to `enrollment`

Date: 2026-04-25
Status: Approved (pre-implementation)

## Problem

The join entity between `workflow` and `contact` is currently named `workflow_contact`, which conflicts with how it is described in code, docstrings, and agent prompts. Three concrete symptoms:

1. **Misleading agent narration.** The agent's tool `update_contact_status` mutates `workflow_contact.status` (a per-workflow outcome), but the name suggests it mutates `contact.status` (a global flag set by `disable_contact`). In a recent smoke test, the agent reported "I'll mark the contact as completed", which describes the wrong entity. The narration tracks the tool name, and the tool name names the wrong entity.
2. **Half-named domain.** CLI docstrings already say "enrollment" / "enroll" / "enrolled" ("Manage contact enrollment in workflows", "Enroll a contact in a workflow"). Only the entity name -- in SQL, models, DB functions, agent tools -- lags behind. The codebase reaches for a noun it does not have.
3. **Shape of CLI nesting.** `mailpilot workflow contact ...` puts the join entity under `workflow`, but the same row is equally a property of `contact`. Top-level `mailpilot enrollment ...` lets queries cut both ways and matches the existing convention used by every other entity (`contact`, `company`, `task`, `email`).

## Decision

Rename the entity to `enrollment` everywhere -- schema, models, database functions, agent tools, CLI, ADRs, tests. No compatibility shim. No rename script (no production data). One coherent change.

## Domain model

`enrollment` is the join entity that records *a contact's participation in a workflow and its lifecycle outcome*.

- One row per `(workflow_id, contact_id)` pair.
- Composite primary key `(workflow_id, contact_id)`.
- Status lifecycle unchanged: `pending` -> `active` -> `completed` | `failed`.
- `reason` text field unchanged.

Rationale: the record is about *the enrollment* (a relationship-with-state), not about the contact or the workflow individually. When the agent says "marked enrollment completed", the noun matches what was mutated.

## Schema

`src/mailpilot/schema.sql`:

```sql
CREATE TABLE IF NOT EXISTS enrollment (
    workflow_id  TEXT NOT NULL REFERENCES workflow(id),
    contact_id   TEXT NOT NULL REFERENCES contact(id),
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'active', 'completed', 'failed')),
    reason       TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (workflow_id, contact_id)
);

CREATE INDEX IF NOT EXISTS idx_enrollment_contact_id ON enrollment(contact_id);
```

The structure is identical to today's `workflow_contact`. Only the table and index names change.

## Models (`src/mailpilot/models.py`)

| Old | New |
| --- | --- |
| `WorkflowContact` | `Enrollment` |
| `WorkflowContactDetail` | `EnrollmentDetail` |

Field shapes unchanged.

## Database functions (`src/mailpilot/database.py`)

| Old | New |
| --- | --- |
| `create_workflow_contact` | `create_enrollment` |
| `get_workflow_contact` | `get_enrollment` |
| `list_workflow_contacts` | `list_enrollments` |
| `list_workflow_contacts_enriched` | `list_enrollments_detailed` |
| `update_workflow_contact` | `update_enrollment` |
| `delete_workflow_contact` | `delete_enrollment` |

Section header in `database.py` renamed `# -- Workflow Contact ---` -> `# -- Enrollment ---`. Signatures and return types follow the new model names. The `_enriched` -> `_detailed` change keeps the function suffix consistent with the model rename.

## Agent tools (`src/mailpilot/agent/tools.py`, `src/mailpilot/agent/invoke.py`)

| Old | New |
| --- | --- |
| `update_contact_status` (tool name + wrapper) | `update_enrollment_status` |
| `list_workflow_contacts` (tool name + wrapper) | `list_enrollments` |
| `disable_contact` | unchanged (this one really does mutate `contact`) |

`_SYSTEM_PREFIX` in `invoke.py` updated to instruct the agent to call `update_enrollment_status`. Tool docstrings updated to describe enrollments rather than "contact in workflow".

## CLI (`src/mailpilot/cli.py`)

The `mailpilot workflow contact` group is removed. A new top-level `mailpilot enrollment` group replaces it:

```
mailpilot enrollment add --workflow-id ID --contact-id ID
mailpilot enrollment remove --workflow-id ID --contact-id ID
mailpilot enrollment view --workflow-id ID --contact-id ID
mailpilot enrollment list [--workflow-id ID] [--contact-id ID] [--status pending|active|completed|failed] [--limit N]
mailpilot enrollment update --workflow-id ID --contact-id ID --status S [--reason R]
```

Two shape changes from today:

1. **`enrollment view`** is added to match the `list/view` convention used by every other entity. (`workflow contact` had no `view` verb.)
2. **`enrollment list` accepts `--workflow-id` and `--contact-id` as independent optional filters.** Today, `--workflow-id` is required. The schema already indexes `contact_id` (`idx_enrollment_contact_id`), so the cross-workflow query "all enrollments for this contact" is cheap. Either filter alone narrows; both together narrow further; neither returns all enrollments (capped by `--limit`).

CLI help string for the group: `"Manage contact enrollments in workflows."`

CLAUDE.md command reference is updated in the same change: the `mailpilot workflow contact ...` block is replaced with the `mailpilot enrollment ...` block.

## ADR consolidation

- **ADR-03 (workflow model)** -- rewritten to match current code. Beyond the mechanical `workflow_contact -> enrollment` rename, this ADR has drifted from implementation since smoke-test-driven changes landed. Reconcile against:
  - `models.Workflow` field semantics (objective, instructions, type, status, account_id)
  - `agent/invoke.py` tool inventory (`_TOOLS`) and system prefix
  - `routing.py` flow (thread match -> LLM classify -> enrollment ensure)
  - Status lifecycle and `CHECK` constraint values in `schema.sql`
  - "Field definitions" section folded in from ADR-06
- **ADR-06 (workflow field definitions)** -- deleted. Content folded into ADR-03 as a "Field definitions" section. No production history to preserve.
- **ADR-08 (CRM evolution)** -- mechanical `workflow_contact -> enrollment` rename. Verify the entity inventory section lists `enrollment` alongside `contact`, `company`, `tag`, `note`, `activity`.
- **No new ADR-09** for the rename itself. Git history, the GitHub issue, and the merge commit are sufficient paper trail.

## Tests

Existing tests carry over by name change. Files affected:

- `tests/test_database.py` -- DB function tests
- `tests/test_cli.py` -- CLI command tests (group rename, plus new `enrollment view` test)
- `tests/test_agent_tools.py` -- agent tool tests
- `tests/test_agent_invoke.py` -- agent invocation tests
- `tests/test_routing.py` -- routing tests that ensure enrollment rows
- `tests/test_models.py` -- model validation tests
- `tests/conftest.py` -- fixtures referencing `workflow_contact`

One new test: `enrollment view` command (mirroring `contact view`, `company view`, `task view`).

The rename is a refactor, not a feature, so test coverage is preserved by mechanical rename rather than by adding new cases. Patching gotcha (per CLAUDE.md): tests that previously patched `mailpilot.database.get_workflow_contact` or `mailpilot.cli.get_workflow_contact` for FK validation must be updated to the new function names.

## Migration

No prod data. Steps:

1. `make clean` drops all tables and reapplies `schema.sql` with the new `enrollment` table.
2. Test DB (`mailpilot_test`) -- `database_connection` fixture truncates per-test and picks up the new schema on first connection.
3. E2E DB (`mailpilot_e2e`) -- same.

No `ALTER TABLE`, no data copy, no compatibility shim. The `workflow contact` CLI group is removed in the same PR; consumers (Claude Code, smoke-test skill) update to `enrollment` in the same change.

## Verification

Pre-merge:

- `make check` clean (lint + basedpyright + unit tests).
- One `/smoke-test` run end-to-end. The original misleading narration was caught by the smoke test, so the smoke test is the ground-truth check that this rename fixed it. (Note: `make e2e` will be retired in favour of `/smoke-test` in a future PR -- not used here.)

Post-merge:

- The smoke-test skill (`.claude/skills/smoke-test/SKILL.md`) and any other skill or doc that references `workflow contact` CLI commands are updated in the same PR.

## Out of scope

- Backwards-compatibility shim for the old `workflow contact` CLI group. Removed cleanly.
- Renaming `disable_contact` -- it correctly names the entity it mutates.
- Changing the status lifecycle, primary key, or any other structural property of the table.
- Production migration tooling. None exists; none is added.
