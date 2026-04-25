# Enrollment Rename Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the `workflow_contact` join entity to `enrollment` across schema, models, database functions, agent tools, CLI, ADRs, and tests. Restructure the CLI from `mailpilot workflow contact ...` to top-level `mailpilot enrollment ...` and add a `view` verb. Consolidate ADR-06 into ADR-03.

**Architecture:** Bottom-up rename. Two coordinated slices: (A) schema + models + DB functions + routing, (B) agent tools + CLI + docs. Each slice ends green (`make check` clean). No compatibility shim. No migration script -- `make clean` reapplies the new schema. Final verification is `/smoke-test` end-to-end, since the original misleading-narration bug was caught there.

**Tech Stack:** Python 3.14, PostgreSQL 18, psycopg, Pydantic, Click, basedpyright strict, ruff, pytest.

**Spec:** `docs/superpowers/specs/2026-04-25-enrollment-rename-design.md`. **Issue:** #80.

---

## Conventions for this plan

**Refactor TDD pattern.** Tests already exist for almost everything. The TDD cycle for the rename is: rename the test (it fails because the impl still uses the old name), rename the impl (test passes), commit. For genuinely new behavior (only `enrollment view` and `enrollment list` cross-workflow filtering), write the failing test first.

**Commit cadence.** Commit at the end of each task. Use `refactor(area): ...` for renames and `feat(cli): ...` for the new `view` verb / cross-workflow filter.

**Verification at slice boundaries.** Slice A ends with `make clean && make check`. Slice B ends with `make check` then `/smoke-test`. Within a slice, intermediate commits may not be green -- that's expected for a coordinated rename.

---

## File Structure

| File | Disposition |
| --- | --- |
| `src/mailpilot/schema.sql` | Modify -- table + index renamed |
| `src/mailpilot/models.py` | Modify -- `WorkflowContact` -> `Enrollment`, `WorkflowContactDetail` -> `EnrollmentDetail`, `ContactOutcome` -> `EnrollmentStatus` |
| `src/mailpilot/database.py` | Modify -- six function renames, section header |
| `src/mailpilot/routing.py` | Modify -- caller of `create_workflow_contact` |
| `src/mailpilot/agent/tools.py` | Modify -- `update_contact_status` -> `update_enrollment_status`, `list_workflow_contacts` -> `list_enrollments`, references to `workflow_contact` |
| `src/mailpilot/agent/invoke.py` | Modify -- wrapper renames, `_TOOLS` registration, `_SYSTEM_PREFIX` |
| `src/mailpilot/cli.py` | Modify -- remove `workflow contact` group, add `enrollment` group with new `view` verb and cross-workflow `list` filtering |
| `tests/test_database.py` | Modify -- DB function tests |
| `tests/test_models.py` | Modify -- model tests |
| `tests/test_routing.py` | Modify -- routing tests |
| `tests/test_agent_tools.py` | Modify -- agent tool tests |
| `tests/test_agent_invoke.py` | Modify -- agent invocation tests |
| `tests/test_cli.py` | Modify -- CLI tests, plus new `enrollment view` test |
| `tests/conftest.py` | Modify -- fixture references |
| `docs/adr-03-workflow-model.md` | Rewritten -- match current code, fold in field definitions |
| `docs/adr-06-workflow-field-definitions.md` | **Delete** |
| `docs/adr-08-crm-evolution.md` | Modify -- mechanical rename |
| `CLAUDE.md` | Modify -- CLI command reference block |
| `.claude/skills/smoke-test/SKILL.md` | Modify -- CLI references |

---

## Slice A: Schema + Models + Database + Routing

### Task A1: Rename schema table and index

**Files:**
- Modify: `src/mailpilot/schema.sql:66-75` (table) and `src/mailpilot/schema.sql:125` (index)

- [ ] **Step 1: Edit schema.sql**

Replace the table definition:

```sql
CREATE TABLE IF NOT EXISTS enrollment (
    workflow_id   TEXT NOT NULL REFERENCES workflow(id),
    contact_id    TEXT NOT NULL REFERENCES contact(id),
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'active', 'completed', 'failed')),
    reason        TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (workflow_id, contact_id)
);
```

Replace the index:

```sql
CREATE INDEX IF NOT EXISTS idx_enrollment_contact_id ON enrollment(contact_id);
```

- [ ] **Step 2: Drop and reapply schema for both DBs**

Run:
```bash
make clean
DATABASE_URL=postgresql://localhost/mailpilot_test uv run python -c "from mailpilot.database import initialize_database; initialize_database('postgresql://localhost/mailpilot_test').close()"
```

Expected: no error. The `database_connection` fixture also reapplies on first connection; the explicit run above just makes the test DB ready before tests touch it.

- [ ] **Step 3: Commit**

```bash
git add src/mailpilot/schema.sql
git commit -m "refactor(schema): rename workflow_contact table to enrollment"
```

---

### Task A2: Rename models (`WorkflowContact` -> `Enrollment`)

**Files:**
- Modify: `src/mailpilot/models.py:85-109`

- [ ] **Step 1: Update models.py**

Replace lines 85-109 with:

```python
EnrollmentStatus = Literal["pending", "active", "completed", "failed"]


class Enrollment(BaseModel):
    """A contact's participation in a workflow with lifecycle outcome."""

    workflow_id: str
    contact_id: str
    status: EnrollmentStatus = "pending"
    reason: str = ""
    created_at: datetime
    updated_at: datetime


class EnrollmentDetail(BaseModel):
    """Enrollment with denormalised contact info for list display."""

    workflow_id: str
    contact_id: str
    contact_email: str
    contact_name: str
    status: EnrollmentStatus
    reason: str
    created_at: datetime
    updated_at: datetime
```

- [ ] **Step 2: Update tests/test_models.py**

Find every reference to `WorkflowContact`, `WorkflowContactDetail`, `ContactOutcome` and rename to `Enrollment`, `EnrollmentDetail`, `EnrollmentStatus`. Use Grep to locate, Edit to rename. Test names and docstrings should also use "enrollment".

- [ ] **Step 3: Run model tests**

Run: `uv run pytest tests/test_models.py -x -v`

Expected: PASS. (No DB or other layers exercised here.)

- [ ] **Step 4: Commit**

```bash
git add src/mailpilot/models.py tests/test_models.py
git commit -m "refactor(models): rename WorkflowContact to Enrollment"
```

Note: at this point `database.py`, `routing.py`, `agent/`, `cli.py` will have unresolved imports referencing the old names. They get fixed in the next tasks. `make check` is intentionally not run yet.

---

### Task A3: Rename database functions

**Files:**
- Modify: `src/mailpilot/database.py:36-37` (imports), section header near line 985, function definitions at lines 988, 1022, 1054, 1081, 1110, 1136

Function rename map:

| Old | New |
| --- | --- |
| `create_workflow_contact` | `create_enrollment` |
| `get_workflow_contact` | `get_enrollment` |
| `list_workflow_contacts` | `list_enrollments` |
| `list_workflow_contacts_enriched` | `list_enrollments_detailed` |
| `update_workflow_contact` | `update_enrollment` |
| `delete_workflow_contact` | `delete_enrollment` |

- [ ] **Step 1: Update database.py imports**

Replace:
```python
    WorkflowContact,
    WorkflowContactDetail,
```
with:
```python
    Enrollment,
    EnrollmentDetail,
```

- [ ] **Step 2: Rename section header**

Find the `# -- Workflow Contact ---` (or similar) section header and rename to `# -- Enrollment ---`.

- [ ] **Step 3: Rename each function**

For each of the six functions:
- Rename the function (`create_workflow_contact` -> `create_enrollment`, etc.)
- Update the SQL string `workflow_contact` -> `enrollment` (table name only; column names unchanged)
- Update docstring to use "enrollment" terminology
- Update internal references to `WorkflowContact`/`WorkflowContactDetail` -> `Enrollment`/`EnrollmentDetail`
- For `list_workflow_contacts_enriched` -> `list_enrollments_detailed`, also update the comment that references the sibling function name

Use Grep with pattern `workflow_contact|WorkflowContact` on `src/mailpilot/database.py` to confirm all references are renamed (expected: zero matches). Then run:

`uv run ruff check --fix src/mailpilot/database.py`

Expected: no errors. Imports clean.

- [ ] **Step 4: Update tests/test_database.py**

Grep for `workflow_contact|WorkflowContact`. Rename:
- Function calls (`create_workflow_contact(...)` -> `create_enrollment(...)`)
- Imports
- Test function names (`test_create_workflow_contact_*` -> `test_create_enrollment_*`)
- Docstrings and inline comments

- [ ] **Step 5: Run database tests**

Run: `uv run pytest tests/test_database.py -x -v`

Expected: PASS for all tests that don't touch the still-old routing/CLI/agent layers. If a test fails because of cross-module imports, defer that test until Task A4 / Slice B; don't try to fix it here.

- [ ] **Step 6: Commit**

```bash
git add src/mailpilot/database.py tests/test_database.py
git commit -m "refactor(database): rename workflow_contact functions to enrollment"
```

---

### Task A4: Update routing.py callers

**Files:**
- Modify: `src/mailpilot/routing.py:10` (docstring), `:23` (import), `:45` (docstring), `:93` (call site), `:220-226` (helper)

- [ ] **Step 1: Edit routing.py**

- Update docstrings: `workflow_contact` -> `enrollment`.
- Update import: `create_workflow_contact` -> `create_enrollment`.
- Rename helper: `_ensure_workflow_contact` -> `_ensure_enrollment`. Update its docstring and its single call site at line 93.

- [ ] **Step 2: Update tests/test_routing.py**

Grep for `workflow_contact|WorkflowContact|_ensure_workflow_contact`. Rename mechanically.

- [ ] **Step 3: Update tests/conftest.py**

Grep for `workflow_contact`. The likely hit is a fixture or helper that creates rows for tests. Rename the function name, the SQL/ORM calls inside, and any references from other test files.

- [ ] **Step 4: Run routing tests**

Run: `uv run pytest tests/test_routing.py -x -v`

Expected: PASS.

- [ ] **Step 5: Slice A green-gate**

Run:
```bash
make clean
make check
```

Expected: `make check` clean. This is the slice boundary. Note: tests in `test_agent_tools.py`, `test_agent_invoke.py`, `test_cli.py` will still fail because Slice B hasn't run yet. They are excluded from `make py-test` only if the rename is complete -- if any fail, you must do a partial fix in `agent/` and `cli.py` to keep the import graph valid (i.e. rename the `from mailpilot.database import ...` lines without yet renaming the agent tool name or CLI command name).

If the import graph fails to resolve, do a minimal fix: rename only the imports in `agent/tools.py`, `agent/invoke.py`, `cli.py` to point to the new database function names, keeping all *other* names (tool names, CLI commands) unchanged. This buys you a green Slice A boundary and preserves the rest of the rename for Slice B.

- [ ] **Step 6: Commit**

```bash
git add src/mailpilot/routing.py tests/test_routing.py tests/conftest.py
# Also stage any minimal import-only fixes in src/mailpilot/agent/ and src/mailpilot/cli.py
git commit -m "refactor(routing): use create_enrollment in routing"
```

---

## Slice B: Agent tools + CLI + docs

### Task B1: Rename agent tool implementations

**Files:**
- Modify: `src/mailpilot/agent/tools.py` -- `update_contact_status` -> `update_enrollment_status` (line 337+), `list_workflow_contacts` -> `list_enrollments` (line 384), and the helper at line 44 that transitions from pending to active.

- [ ] **Step 1: Rename `update_contact_status` to `update_enrollment_status`**

Rename the function. Update its docstring to describe enrollments. Update the error message body `f"workflow_contact not found: {workflow_id}/{contact_id}"` -> `f"enrollment not found: {workflow_id}/{contact_id}"`. Update the internal calls `database.update_workflow_contact(...)` -> `database.update_enrollment(...)`.

- [ ] **Step 2: Rename `list_workflow_contacts` to `list_enrollments`**

Rename the function. Update its docstring. Update the internal call `database.list_workflow_contacts(...)` -> `database.list_enrollments(...)`.

- [ ] **Step 3: Update the activation helper**

The helper at line 44 (currently transitions enrollment from pending to active) has docstring text "Transition workflow_contact from pending to active". Update to "Transition enrollment from pending to active". Update the variable name `wc` -> `enrollment` for clarity. Update internal calls to use the renamed database functions.

- [ ] **Step 4: Update module docstring**

Top-of-file docstring (line 19 area) lists `list_workflow_contacts` -- change to `list_enrollments`.

- [ ] **Step 5: Update tests/test_agent_tools.py**

Grep for `update_contact_status|list_workflow_contacts|workflow_contact|WorkflowContact`. Rename:
- Imports
- Function calls
- Test function names (`test_update_contact_status_*` -> `test_update_enrollment_status_*`, `test_list_workflow_contacts_*` -> `test_list_enrollments_*`)
- Section header comments (`# -- update_contact_status -----`)
- Docstring/comment references

- [ ] **Step 6: Run agent tool tests**

Run: `uv run pytest tests/test_agent_tools.py -x -v`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/mailpilot/agent/tools.py tests/test_agent_tools.py
git commit -m "refactor(agent): rename update_contact_status to update_enrollment_status"
```

---

### Task B2: Rename agent tool registration and system prompt

**Files:**
- Modify: `src/mailpilot/agent/invoke.py:179-217` (wrappers), `:270-282` (`_TOOLS`), `:285-289` (`_SYSTEM_PREFIX`)

- [ ] **Step 1: Rename wrappers**

- `_wrap_update_contact_status` -> `_wrap_update_enrollment_status`. Update its docstring and the call to `agent_tools.update_contact_status(...)` -> `agent_tools.update_enrollment_status(...)`.
- `_wrap_list_workflow_contacts` -> `_wrap_list_enrollments`. Update its docstring and the call.

- [ ] **Step 2: Update `_TOOLS` registration**

```python
    Tool(_wrap_update_enrollment_status, name="update_enrollment_status"),
    Tool(_wrap_disable_contact, name="disable_contact"),
    Tool(_wrap_list_enrollments, name="list_enrollments"),
```

- [ ] **Step 3: Update `_SYSTEM_PREFIX`**

Find the line:
```python
    "update_contact_status with status='completed' and a brief reason.\n\n"
```
Replace with:
```python
    "update_enrollment_status with status='completed' and a brief reason.\n\n"
```

Read the surrounding system prompt lines and update any other `contact_status` / `workflow_contact` / `workflow contact` phrasing to "enrollment" where it improves clarity. Be conservative -- only touch lines that are factually wrong now.

- [ ] **Step 4: Update tests/test_agent_invoke.py**

Grep for `update_contact_status|list_workflow_contacts|_wrap_update_contact_status|_wrap_list_workflow_contacts|workflow_contact`. Rename mechanically.

- [ ] **Step 5: Run agent invoke tests**

Run: `uv run pytest tests/test_agent_invoke.py -x -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mailpilot/agent/invoke.py tests/test_agent_invoke.py
git commit -m "refactor(agent): rename agent tool registrations and system prompt to enrollment"
```

---

### Task B3: Replace CLI `workflow contact` group with `enrollment` group

**Files:**
- Modify: `src/mailpilot/cli.py:1568-1726` (delete old group), insert new group at the same anchor

This task adds **one new behavior** beyond the rename: `enrollment view` and `enrollment list` accepting `--workflow-id` and `--contact-id` as independent optional filters. TDD applies for these.

- [ ] **Step 1: Write failing test for `enrollment view`**

Add to `tests/test_cli.py`:

```python
def test_enrollment_view_returns_record(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel_enrollment = SimpleNamespace(
        model_dump=lambda mode: {
            "workflow_id": "wf-1",
            "contact_id": "c-1",
            "status": "active",
            "reason": "",
        },
    )
    monkeypatch.setattr(
        "mailpilot.database.get_enrollment", lambda *_a, **_k: sentinel_enrollment
    )
    monkeypatch.setattr(
        "mailpilot.database.initialize_database", lambda *_a, **_k: SimpleNamespace(close=lambda: None)
    )

    result = CliRunner().invoke(
        cli, ["enrollment", "view", "--workflow-id", "wf-1", "--contact-id", "c-1"]
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["workflow_id"] == "wf-1"
    assert payload["contact_id"] == "c-1"


def test_enrollment_view_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mailpilot.database.get_enrollment", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "mailpilot.database.initialize_database", lambda *_a, **_k: SimpleNamespace(close=lambda: None)
    )

    result = CliRunner().invoke(
        cli, ["enrollment", "view", "--workflow-id", "wf-1", "--contact-id", "c-1"]
    )

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["error"] == "not_found"
```

- [ ] **Step 2: Write failing test for `enrollment list` cross-workflow filter**

Add to `tests/test_cli.py`:

```python
def test_enrollment_list_filters_by_contact(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_list_detailed(*args: object, **kwargs: object) -> list[object]:
        captured["kwargs"] = kwargs
        return []

    monkeypatch.setattr("mailpilot.database.list_enrollments_detailed", fake_list_detailed)
    monkeypatch.setattr(
        "mailpilot.database.initialize_database", lambda *_a, **_k: SimpleNamespace(close=lambda: None)
    )

    result = CliRunner().invoke(cli, ["enrollment", "list", "--contact-id", "c-1"])

    assert result.exit_code == 0
    assert captured["kwargs"].get("contact_id") == "c-1"
    assert captured["kwargs"].get("workflow_id") is None
```

- [ ] **Step 3: Run the new tests to confirm they fail**

Run: `uv run pytest tests/test_cli.py::test_enrollment_view_returns_record tests/test_cli.py::test_enrollment_view_not_found tests/test_cli.py::test_enrollment_list_filters_by_contact -x -v`

Expected: FAIL (commands not registered).

- [ ] **Step 4: Add new database helper to support cross-workflow listing**

In `src/mailpilot/database.py`, update `list_enrollments_detailed` to accept *both* `workflow_id` and `contact_id` as optional filters. Today it takes only `workflow_id` as required. New signature:

```python
def list_enrollments_detailed(
    connection: psycopg.Connection,
    *,
    workflow_id: str | None = None,
    contact_id: str | None = None,
    status: EnrollmentStatus | None = None,
    limit: int = 100,
) -> list[EnrollmentDetail]:
    ...
```

Build the WHERE clause dynamically using `psycopg.sql` (no f-strings). At least one of `workflow_id` / `contact_id` is supplied in practice but the function does not enforce that -- the CLI does, via the limit cap.

- [ ] **Step 5: Add tests for the new database signature**

In `tests/test_database.py`, add:

```python
def test_list_enrollments_detailed_filters_by_contact(database_connection):
    # Setup: two workflows, one contact enrolled in both
    # ... use existing fixture helpers, see tests/conftest.py
    results = list_enrollments_detailed(database_connection, contact_id=contact.id)
    assert len(results) == 2
    assert {r.workflow_id for r in results} == {wf_a.id, wf_b.id}


def test_list_enrollments_detailed_filters_by_workflow_and_contact(database_connection):
    results = list_enrollments_detailed(
        database_connection, workflow_id=wf.id, contact_id=contact.id
    )
    assert len(results) == 1
```

(Read `tests/conftest.py` for fixture helpers; mirror the patterns used in existing `test_list_workflow_contacts_enriched_*` tests.)

- [ ] **Step 6: Implement the new CLI group**

Replace the entire `# -- Workflow Contact subgroup ---` block in `src/mailpilot/cli.py` (currently lines 1615-1726) with:

```python
# -- Enrollment commands -------------------------------------------------------


@cli.group("enrollment")
def enrollment() -> None:
    """Manage contact enrollments in workflows."""


@enrollment.command("add")
@click.option("--workflow-id", required=True, help="Workflow ID.")
@click.option("--contact-id", required=True, help="Contact ID.")
def enrollment_add(workflow_id: str, contact_id: str) -> None:
    """Enroll a contact in a workflow."""
    from mailpilot.database import (
        create_enrollment,
        get_contact,
        get_enrollment,
        get_workflow,
        initialize_database,
    )

    connection = initialize_database(_database_url())
    try:
        if get_workflow(connection, workflow_id) is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        if get_contact(connection, contact_id) is None:
            output_error(f"contact not found: {contact_id}", "not_found")
        created = create_enrollment(connection, workflow_id, contact_id)
        if created is not None:
            output(created.model_dump(mode="json"))
            return
        existing = get_enrollment(connection, workflow_id, contact_id)
        if existing is not None:
            output(existing.model_dump(mode="json"))
            return
    finally:
        connection.close()


@enrollment.command("remove")
@click.option("--workflow-id", required=True, help="Workflow ID.")
@click.option("--contact-id", required=True, help="Contact ID.")
def enrollment_remove(workflow_id: str, contact_id: str) -> None:
    """Remove an enrollment."""
    from mailpilot.database import delete_enrollment, initialize_database

    connection = initialize_database(_database_url())
    try:
        deleted = delete_enrollment(connection, workflow_id, contact_id)
        if not deleted:
            output_error("enrollment not found", "not_found")
        output({"workflow_id": workflow_id, "contact_id": contact_id})
    finally:
        connection.close()


@enrollment.command("view")
@click.option("--workflow-id", required=True, help="Workflow ID.")
@click.option("--contact-id", required=True, help="Contact ID.")
def enrollment_view(workflow_id: str, contact_id: str) -> None:
    """View an enrollment by composite key."""
    from mailpilot.database import get_enrollment, initialize_database

    connection = initialize_database(_database_url())
    try:
        record = get_enrollment(connection, workflow_id, contact_id)
        if record is None:
            output_error("enrollment not found", "not_found")
        output(record.model_dump(mode="json"))
    finally:
        connection.close()


@enrollment.command("list")
@click.option("--workflow-id", default=None, help="Filter by workflow ID.")
@click.option("--contact-id", default=None, help="Filter by contact ID.")
@click.option(
    "--status",
    default=None,
    type=click.Choice(["pending", "active", "completed", "failed"]),
    help="Filter by enrollment status.",
)
@click.option("--limit", default=100, help="Maximum results.")
def enrollment_list(
    workflow_id: str | None,
    contact_id: str | None,
    status: str | None,
    limit: int,
) -> None:
    """List enrollments. Filter by workflow, contact, or both."""
    from mailpilot.database import (
        get_contact,
        get_workflow,
        initialize_database,
        list_enrollments_detailed,
    )

    connection = initialize_database(_database_url())
    try:
        if workflow_id is not None and get_workflow(connection, workflow_id) is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        if contact_id is not None and get_contact(connection, contact_id) is None:
            output_error(f"contact not found: {contact_id}", "not_found")
        rows = list_enrollments_detailed(
            connection,
            workflow_id=workflow_id,
            contact_id=contact_id,
            status=status,
            limit=limit,
        )
        output({"enrollments": [r.model_dump(mode="json") for r in rows]})
    finally:
        connection.close()


@enrollment.command("update")
@click.option("--workflow-id", required=True, help="Workflow ID.")
@click.option("--contact-id", required=True, help="Contact ID.")
@click.option(
    "--status",
    required=True,
    type=click.Choice(["pending", "active", "completed", "failed"]),
    help="New enrollment status.",
)
@click.option("--reason", default=None, help="Status reason.")
def enrollment_update(
    workflow_id: str, contact_id: str, status: str, reason: str | None
) -> None:
    """Update enrollment status and reason."""
    from mailpilot.database import initialize_database, update_enrollment

    connection = initialize_database(_database_url())
    try:
        fields: dict[str, object] = {"status": status}
        if reason is not None:
            fields["reason"] = reason
        updated = update_enrollment(connection, workflow_id, contact_id, **fields)
        if updated is None:
            output_error("enrollment not found", "not_found")
        output(updated.model_dump(mode="json"))
    finally:
        connection.close()
```

Update the `workflow run` command (around line 1560-1610) which separately imports and calls `get_workflow_contact` for FK validation:

- Change the import `get_workflow_contact` -> `get_enrollment` (around line 1568).
- Change the call `if get_workflow_contact(connection, workflow_id, contact.id) is None:` -> `if get_enrollment(connection, workflow_id, contact.id) is None:` (around line 1587). Update the surrounding error message if it references "workflow-contact" -- replace with "enrollment".

Re-grep `cli.py` for any remaining `workflow_contact|WorkflowContact|workflow contact` reference and clean up. Expected: zero matches after this step.

- [ ] **Step 7: Update existing CLI tests for the rename**

In `tests/test_cli.py`, grep for `workflow_contact|workflow contact|update_workflow_contact|list_workflow_contacts_enriched|create_workflow_contact|delete_workflow_contact|get_workflow_contact`. Rename:
- `monkeypatch.setattr("mailpilot.database.create_workflow_contact", ...)` -> `mailpilot.database.create_enrollment`
- Click command invocations: `["workflow", "contact", "add", ...]` -> `["enrollment", "add", ...]`
- Test names

- [ ] **Step 8: Run all CLI and database tests**

Run: `uv run pytest tests/test_cli.py tests/test_database.py -x -v`

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/mailpilot/cli.py src/mailpilot/database.py tests/test_cli.py tests/test_database.py
git commit -m "feat(cli): replace workflow-contact group with enrollment group

Adds enrollment view command. enrollment list now accepts --workflow-id
and --contact-id as independent optional filters; the underlying
list_enrollments_detailed signature accepts both."
```

---

### Task B4: Update CLAUDE.md and smoke-test skill

**Files:**
- Modify: `CLAUDE.md` -- CLI command reference block
- Modify: `.claude/skills/smoke-test/SKILL.md` -- any `workflow contact` references

- [ ] **Step 1: Update CLAUDE.md command reference**

Find the block:
```
mailpilot workflow contact add --workflow-id ID --contact-id ID
mailpilot workflow contact remove --workflow-id ID --contact-id ID
mailpilot workflow contact list --workflow-id ID [--status pending|active|completed|failed] [--limit N]
mailpilot workflow contact update --workflow-id ID --contact-id ID --status S [--reason R]
```

Replace with:
```
mailpilot enrollment add --workflow-id ID --contact-id ID
mailpilot enrollment remove --workflow-id ID --contact-id ID
mailpilot enrollment view --workflow-id ID --contact-id ID
mailpilot enrollment list [--workflow-id ID] [--contact-id ID] [--status pending|active|completed|failed] [--limit N]
mailpilot enrollment update --workflow-id ID --contact-id ID --status S [--reason R]
```

Also re-read the "Tables:" line in the schema section and replace `workflow_contact` with `enrollment` in the table list.

- [ ] **Step 2: Update smoke-test SKILL.md**

Use Grep on `.claude/skills/smoke-test/SKILL.md` with pattern `workflow contact|workflow_contact|workflow-contact`. Rename each occurrence to the `enrollment` form. Update CLI invocations consistently (e.g. `mailpilot workflow contact add ...` -> `mailpilot enrollment add ...`).

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md .claude/skills/smoke-test/SKILL.md
git commit -m "docs: update CLAUDE.md and smoke-test skill for enrollment rename"
```

---

### Task B5: Rewrite ADR-03, delete ADR-06, update ADR-08

**Files:**
- Rewrite: `docs/adr-03-workflow-model.md`
- Delete: `docs/adr-06-workflow-field-definitions.md`
- Modify: `docs/adr-08-crm-evolution.md`

- [ ] **Step 1: Read ADR-03 and ADR-06 in full**

Read both files end to end. Take notes on:
- What ADR-06 covers that ADR-03 does not (this becomes the "Field definitions" section).
- Where ADR-03 disagrees with current code. Cross-reference against:
  - `src/mailpilot/models.py` (Workflow model, Enrollment model, status literals)
  - `src/mailpilot/agent/invoke.py` (`_TOOLS` and `_SYSTEM_PREFIX`)
  - `src/mailpilot/routing.py` (thread match -> classify -> enrollment ensure flow)
  - `src/mailpilot/schema.sql` (workflow + enrollment tables, CHECK constraints)

- [ ] **Step 2: Rewrite ADR-03**

Rewrite ADR-03 reflecting current code, with these sections (in order):

1. **Status & date** -- Updated, 2026-04-25 (was: original date, now updated for enrollment rename + field-definitions consolidation).
2. **Context** -- two-layer agent model, workflow as the central abstraction, why workflows bind account to instructions.
3. **Decision** -- workflow + enrollment data model. Reference current `models.Workflow` and `models.Enrollment` field-by-field.
4. **Workflow lifecycle** -- draft / active / paused. Match the CHECK constraint exactly.
5. **Enrollment lifecycle** -- pending / active / completed / failed. Match the CHECK constraint exactly. Explain who transitions each (agent on send -> active, agent on objective met -> completed/failed, never the system on its own).
6. **Field definitions** -- *folded in from ADR-06*. Each Workflow field with its semantics, what the agent sees, what the system enforces.
7. **Routing flow** -- inbound: thread match (Gmail thread_id) -> LLM classify against active workflows -> enrollment ensure. Outbound: enrollment created via `mailpilot enrollment add`, agent invoked via `workflow run`.
8. **Agent tool inventory** -- list the actual tool names from `_TOOLS` in `agent/invoke.py`. State which mutate `enrollment` vs `contact` vs `task`.
9. **Consequences** -- positive/negative.
10. **Note on consolidation** -- one paragraph: "ADR-06 (workflow field definitions) was folded into this ADR's Field definitions section on 2026-04-25 and deleted. The join entity was renamed `workflow_contact` -> `enrollment` in the same change."

Use ASCII-only per CLAUDE.md (`->`, `--`, etc.).

- [ ] **Step 3: Delete ADR-06**

```bash
git rm docs/adr-06-workflow-field-definitions.md
```

- [ ] **Step 4: Update ADR-08**

Grep ADR-08 for `workflow_contact|workflow-contact|workflow contact`. Rename to `enrollment` form. Verify the entity inventory section lists `enrollment` alongside `contact`, `company`, `tag`, `note`, `activity`. If ADR-08 references ADR-06 explicitly, update the reference to point to ADR-03's Field definitions section.

- [ ] **Step 5: Commit**

```bash
git add docs/adr-03-workflow-model.md docs/adr-08-crm-evolution.md
git commit -m "docs(adr): rewrite ADR-03 to match current code, fold in ADR-06, rename workflow_contact

ADR-03 now reflects current implementation (workflow fields, lifecycle,
agent tools, routing flow) after smoke-test-driven changes. Field
definitions previously in ADR-06 are folded in as a section. ADR-06
deleted -- no production history to preserve. ADR-08 mechanically
renamed."
```

---

### Task B6: Final verification

- [ ] **Step 1: Confirm no `workflow_contact` references remain in src/**

Use Grep on `src/` with pattern `workflow_contact|WorkflowContact|ContactOutcome|workflow contact`. Expected: zero matches.

- [ ] **Step 2: Confirm no `workflow_contact` references remain in tests/**

Use Grep on `tests/` with the same pattern. Expected: zero matches.

- [ ] **Step 3: Run `make check`**

Run: `make check`

Expected: clean (lint + basedpyright + unit tests all pass).

- [ ] **Step 4: Reset and reapply schema**

Run: `make clean`

Expected: tables dropped, new schema applied with `enrollment` table.

- [ ] **Step 5: Run /smoke-test**

Run the `/smoke-test` skill end-to-end. Watch the agent's narration in Logfire spans -- confirm it now references "enrollment" rather than "contact" when marking outcomes.

Expected: smoke test passes. Original misleading-narration bug is gone (agent says "marked enrollment completed" or similar).

- [ ] **Step 6: Final commit (if any cleanup needed)**

If the smoke test surfaced any issue (e.g. missed reference in a SKILL.md, agent tool docstring), fix and commit:

```bash
git commit -m "refactor: clean up missed references after smoke-test verification"
```

---

## Self-Review Checklist (run before handing off)

Spec coverage:

- Schema rename -- Task A1
- Models rename (`Enrollment`, `EnrollmentDetail`, `EnrollmentStatus`) -- Task A2
- Database functions (six renames) -- Task A3
- Routing.py callers -- Task A4
- Agent tools (`update_enrollment_status`, `list_enrollments`) -- Task B1
- Agent registration + system prompt -- Task B2
- CLI rename + new `view` verb + cross-workflow `list` filter -- Task B3
- CLAUDE.md + smoke-test skill -- Task B4
- ADR-03 rewrite + ADR-06 delete + ADR-08 rename -- Task B5
- Final verification (`make check` + `/smoke-test`) -- Task B6

Type consistency:

- Function names match spec table (e.g. `list_enrollments_detailed` not `list_enrollments_enriched`).
- Tool names match spec (`update_enrollment_status`, `list_enrollments`).
- CLI commands match spec (`enrollment add/remove/view/list/update`).
- Model names match (`Enrollment`, `EnrollmentDetail`, `EnrollmentStatus`).

No placeholders in code blocks. Every step shows the actual content.
