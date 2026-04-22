# Workflow Execution Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the run loop that bridges inbound email routing to agent invocation via tasks, drains the task queue, and exposes task CLI commands for visibility and manual intervention.

**Architecture:** New `run.py` module with `run_loop()` that composes sync + task bridge + task execution in a single loop. Tasks are the universal execution primitive -- inbound routing creates immediate tasks, agents create follow-up tasks, the loop drains them all. Three new CLI commands (`task list/view/cancel`) for observability. One new setting (`run_interval`).

**Tech Stack:** Python, Click, psycopg, Pydantic AI, pytest, basedpyright, ruff

**Existing code to reuse (do not modify):**
- `database.py`: `create_task()` (line 1714), `get_task()` (line 1767), `list_pending_tasks()` (line 1791), `complete_task()` (line 1815), `cancel_task()` (line 1845)
- `agent/invoke.py`: `invoke_workflow_agent()` (line 360) -- accepts `email`, `task_description`, `task_context` params
- `sync.py`: `sync_account()` (line 132) -- per-account Gmail sync + routing
- `models.py`: `Task` model (line 137), `TaskStatus` literal (line 134)

---

### Task 1: Add `run_interval` setting

**Files:**
- Modify: `src/mailpilot/settings.py:46-58` (Settings class)
- Modify: `tests/test_settings.py` (add test for new field)

- [ ] **Step 1: Write failing test for `run_interval` default**

Add to `tests/test_settings.py`:

```python
def test_run_interval_default() -> None:
    settings = Settings(
        database_url="postgresql://localhost/test",  # type: ignore[arg-type]
    )
    assert settings.run_interval == 30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_settings.py::test_run_interval_default -v`
Expected: FAIL with `AttributeError` or validation error (field doesn't exist yet)

- [ ] **Step 3: Add `run_interval` field to Settings**

In `src/mailpilot/settings.py`, add after line 58 (`google_application_credentials`):

```python
    run_interval: int = 30
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_settings.py::test_run_interval_default -v`
Expected: PASS

- [ ] **Step 5: Run lint and type check**

Run: `uv run ruff check --fix && uv run basedpyright`
Expected: Clean

- [ ] **Step 6: Commit**

```bash
git add src/mailpilot/settings.py tests/test_settings.py
git commit -m "feat: add run_interval setting (default 30s)"
```

---

### Task 2: Add `list_tasks()` database function

**Files:**
- Modify: `src/mailpilot/database.py` (add after `cancel_task` at line 1874, before `# -- Activity` section)
- Modify: `tests/test_database.py`

- [ ] **Step 1: Write failing test for `list_tasks` with no filters**

Add to `tests/test_database.py`:

```python
def test_list_tasks(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)
    create_task(
        database_connection,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="follow up",
        scheduled_at="2026-04-22T12:00:00Z",
    )
    create_task(
        database_connection,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="check reply",
        scheduled_at="2026-04-22T13:00:00Z",
    )
    results = list_tasks(database_connection)
    assert len(results) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_database.py::test_list_tasks -v`
Expected: FAIL with `ImportError` (function doesn't exist)

- [ ] **Step 3: Implement `list_tasks()`**

Add to `src/mailpilot/database.py` after `cancel_task` (line 1874), before the `# -- Activity` section:

```python
def list_tasks(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str | None = None,
    contact_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[Task]:
    """List tasks with optional filters.

    Args:
        connection: Open database connection.
        workflow_id: Filter by workflow ID.
        contact_id: Filter by contact ID.
        status: Filter by task status.
        limit: Maximum number of tasks to return.

    Returns:
        List of tasks ordered by scheduled_at descending.
    """
    with logfire.span(
        "db.task.list",
        workflow_id=workflow_id,
        contact_id=contact_id,
        status=status,
        limit=limit,
    ) as span:
        conditions: list[SQL] = []
        params: dict[str, object] = {"limit": limit}
        if workflow_id is not None:
            conditions.append(SQL("workflow_id = %(workflow_id)s"))
            params["workflow_id"] = workflow_id
        if contact_id is not None:
            conditions.append(SQL("contact_id = %(contact_id)s"))
            params["contact_id"] = contact_id
        if status is not None:
            conditions.append(SQL("status = %(status)s"))
            params["status"] = status
        where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
        query = SQL(
            "SELECT * FROM task {} ORDER BY scheduled_at DESC LIMIT %(limit)s"
        ).format(where)
        rows = connection.execute(query, params).fetchall()
        tasks = [Task.model_validate(row) for row in rows]
        span.set_attribute("task_count", len(tasks))
        return tasks
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_database.py::test_list_tasks -v`
Expected: PASS

- [ ] **Step 5: Write test for `list_tasks` with filters**

```python
def test_list_tasks_with_filters(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact_a = make_test_contact(database_connection, email="a@test.com")
    contact_b = make_test_contact(database_connection, email="b@test.com")
    create_task(
        database_connection,
        workflow_id=workflow.id,
        contact_id=contact_a.id,
        description="task for A",
        scheduled_at="2026-04-22T12:00:00Z",
    )
    task_b = create_task(
        database_connection,
        workflow_id=workflow.id,
        contact_id=contact_b.id,
        description="task for B",
        scheduled_at="2026-04-22T13:00:00Z",
    )
    cancel_task(database_connection, task_b.id)

    by_contact = list_tasks(database_connection, contact_id=contact_a.id)
    assert len(by_contact) == 1
    assert by_contact[0].contact_id == contact_a.id

    cancelled = list_tasks(database_connection, status="cancelled")
    assert len(cancelled) == 1
    assert cancelled[0].contact_id == contact_b.id

    pending = list_tasks(database_connection, status="pending")
    assert len(pending) == 1
    assert pending[0].contact_id == contact_a.id
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_database.py::test_list_tasks_with_filters -v`
Expected: PASS

- [ ] **Step 7: Run lint and type check**

Run: `uv run ruff check --fix && uv run basedpyright`
Expected: Clean

- [ ] **Step 8: Commit**

```bash
git add src/mailpilot/database.py tests/test_database.py
git commit -m "feat(db): add list_tasks() with optional filters"
```

---

### Task 3: Add `task list` CLI command

**Files:**
- Modify: `src/mailpilot/cli.py` (add `task` group after workflow contact section)
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing test for `task list`**

Add to `tests/test_cli.py`. First add the `_make_task` helper near the other `_make_*` helpers:

```python
def _make_task(**overrides: Any) -> Task:
    defaults: dict[str, Any] = {
        "id": "01234567-0000-7000-0000-a00000000001",
        "workflow_id": _WORKFLOW_ID,
        "contact_id": _CONTACT_ID,
        "email_id": None,
        "description": "follow up",
        "context": {},
        "scheduled_at": _NOW,
        "status": "pending",
        "completed_at": None,
        "created_at": _NOW,
    }
    return Task(**{**defaults, **overrides})
```

Then add the test:

```python
def test_task_list(runner: CliRunner, mock_connection: MagicMock) -> None:
    tasks = [_make_task()]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.list_tasks", return_value=tasks) as mock_list,
    ):
        result = runner.invoke(main, ["task", "list"])

    assert result.exit_code == 0, result.output
    mock_list.assert_called_once_with(
        mock_connection, workflow_id=None, contact_id=None, status=None, limit=100
    )
    data = json.loads(result.output)
    assert len(data["tasks"]) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py::test_task_list -v`
Expected: FAIL (no `task` command group)

- [ ] **Step 3: Implement `task` group and `task list` command**

Add to `src/mailpilot/cli.py` after the workflow contact section (after line 1651):

```python
# -- Task commands -------------------------------------------------------------


@main.group()
def task() -> None:
    """Manage deferred agent tasks."""


@task.command("list")
@click.option("--workflow-id", default=None, help="Filter by workflow ID.")
@click.option("--contact-id", default=None, help="Filter by contact ID.")
@click.option(
    "--status",
    default=None,
    type=click.Choice(["pending", "completed", "failed", "cancelled"]),
    help="Filter by task status.",
)
@click.option("--limit", default=100, help="Maximum results.")
def task_list(
    workflow_id: str | None,
    contact_id: str | None,
    status: str | None,
    limit: int,
) -> None:
    """List tasks with optional filters."""
    from mailpilot.database import (
        get_contact,
        get_workflow,
        initialize_database,
        list_tasks,
    )

    connection = initialize_database(_database_url())
    try:
        if workflow_id is not None and get_workflow(connection, workflow_id) is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        if contact_id is not None and get_contact(connection, contact_id) is None:
            output_error(f"contact not found: {contact_id}", "not_found")
        tasks = list_tasks(
            connection,
            workflow_id=workflow_id,
            contact_id=contact_id,
            status=status,
            limit=limit,
        )
        output({"tasks": [t.model_dump(mode="json") for t in tasks]})
    finally:
        connection.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py::test_task_list -v`
Expected: PASS

- [ ] **Step 5: Write test for `task list` with filters**

```python
def test_task_list_with_filters(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    workflow = _make_workflow()
    contact = _make_contact()
    tasks = [_make_task()]
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.database.get_contact", return_value=contact),
        patch("mailpilot.database.list_tasks", return_value=tasks) as mock_list,
    ):
        result = runner.invoke(
            main,
            [
                "task",
                "list",
                "--workflow-id",
                _WORKFLOW_ID,
                "--contact-id",
                _CONTACT_ID,
                "--status",
                "pending",
                "--limit",
                "10",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_list.assert_called_once_with(
        mock_connection,
        workflow_id=_WORKFLOW_ID,
        contact_id=_CONTACT_ID,
        status="pending",
        limit=10,
    )
```

- [ ] **Step 6: Write test for `task list` with invalid workflow ID**

```python
def test_task_list_workflow_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_workflow", return_value=None),
    ):
        result = runner.invoke(
            main, ["task", "list", "--workflow-id", "missing"]
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
```

- [ ] **Step 7: Run all new tests**

Run: `uv run pytest tests/test_cli.py::test_task_list tests/test_cli.py::test_task_list_with_filters tests/test_cli.py::test_task_list_workflow_not_found -v`
Expected: PASS

- [ ] **Step 8: Run lint and type check**

Run: `uv run ruff check --fix && uv run basedpyright`
Expected: Clean

- [ ] **Step 9: Commit**

```bash
git add src/mailpilot/cli.py tests/test_cli.py
git commit -m "feat(cli): add task list command with filters"
```

---

### Task 4: Add `task view` CLI command

**Files:**
- Modify: `src/mailpilot/cli.py` (add after `task list`)
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing test for `task view`**

```python
def test_task_view(runner: CliRunner, mock_connection: MagicMock) -> None:
    task_obj = _make_task()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_task", return_value=task_obj),
    ):
        result = runner.invoke(main, ["task", "view", task_obj.id])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["id"] == task_obj.id
    assert data["description"] == "follow up"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py::test_task_view -v`
Expected: FAIL (no `view` subcommand)

- [ ] **Step 3: Implement `task view`**

Add to `src/mailpilot/cli.py` after `task_list`:

```python
@task.command("view")
@click.argument("task_id")
def task_view(task_id: str) -> None:
    """Show a task by ID."""
    from mailpilot.database import get_task, initialize_database

    connection = initialize_database(_database_url())
    try:
        found = get_task(connection, task_id)
        if found is None:
            output_error(f"task not found: {task_id}", "not_found")
        output(found.model_dump(mode="json"))
    finally:
        connection.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py::test_task_view -v`
Expected: PASS

- [ ] **Step 5: Write test for `task view` not found**

```python
def test_task_view_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_task", return_value=None),
    ):
        result = runner.invoke(main, ["task", "view", "missing"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
```

- [ ] **Step 6: Run tests and lint**

Run: `uv run pytest tests/test_cli.py::test_task_view tests/test_cli.py::test_task_view_not_found -v && uv run ruff check --fix && uv run basedpyright`
Expected: All pass, clean

- [ ] **Step 7: Commit**

```bash
git add src/mailpilot/cli.py tests/test_cli.py
git commit -m "feat(cli): add task view command"
```

---

### Task 5: Add `task cancel` CLI command

**Files:**
- Modify: `src/mailpilot/cli.py` (add after `task view`)
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing test for `task cancel`**

```python
def test_task_cancel(runner: CliRunner, mock_connection: MagicMock) -> None:
    cancelled = _make_task(status="cancelled")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.cancel_task", return_value=cancelled) as mock_cancel,
    ):
        result = runner.invoke(main, ["task", "cancel", cancelled.id])

    assert result.exit_code == 0, result.output
    mock_cancel.assert_called_once_with(mock_connection, cancelled.id)
    data = json.loads(result.output)
    assert data["status"] == "cancelled"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py::test_task_cancel -v`
Expected: FAIL (no `cancel` subcommand)

- [ ] **Step 3: Implement `task cancel`**

Add to `src/mailpilot/cli.py` after `task_view`:

```python
@task.command("cancel")
@click.argument("task_id")
def task_cancel(task_id: str) -> None:
    """Cancel a pending task."""
    from mailpilot.database import cancel_task, initialize_database

    connection = initialize_database(_database_url())
    try:
        cancelled = cancel_task(connection, task_id)
        if cancelled is None:
            output_error(
                f"task not found or not pending: {task_id}", "not_found"
            )
        output(cancelled.model_dump(mode="json"))
    finally:
        connection.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cli.py::test_task_cancel -v`
Expected: PASS

- [ ] **Step 5: Write test for `task cancel` not found**

```python
def test_task_cancel_not_pending(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.cancel_task", return_value=None),
    ):
        result = runner.invoke(main, ["task", "cancel", "some-id"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"
    assert "not pending" in data["message"]
```

- [ ] **Step 6: Run tests and lint**

Run: `uv run pytest tests/test_cli.py::test_task_cancel tests/test_cli.py::test_task_cancel_not_pending -v && uv run ruff check --fix && uv run basedpyright`
Expected: All pass, clean

- [ ] **Step 7: Commit**

```bash
git add src/mailpilot/cli.py tests/test_cli.py
git commit -m "feat(cli): add task cancel command"
```

---

### Task 6: Add `create_tasks_for_routed_emails()` database function

This is the bridge between routing and the task queue. Finds inbound emails that have a `workflow_id` but no corresponding task row, and creates immediate tasks for them.

**Files:**
- Modify: `src/mailpilot/database.py` (add after `list_tasks`)
- Modify: `tests/test_database.py`

- [ ] **Step 1: Write failing test**

```python
def test_create_tasks_for_routed_emails(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)

    # Create a routed inbound email (has workflow_id).
    from mailpilot.database import create_email

    email = create_email(
        database_connection,
        gmail_message_id="msg-001",
        gmail_thread_id="thread-001",
        account_id=account.id,
        direction="inbound",
        subject="Re: hello",
        body_text="Got it",
        labels=["INBOX"],
        received_at="2026-04-22T12:00:00Z",
        contact_id=contact.id,
        workflow_id=workflow.id,
    )
    assert email is not None

    created = create_tasks_for_routed_emails(database_connection)
    assert len(created) == 1
    assert created[0].email_id == email.id
    assert created[0].workflow_id == workflow.id
    assert created[0].contact_id == contact.id
    assert created[0].description == "handle inbound email"

    # Idempotent: second call creates no duplicates.
    again = create_tasks_for_routed_emails(database_connection)
    assert len(again) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_database.py::test_create_tasks_for_routed_emails -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement `create_tasks_for_routed_emails()`**

Add to `src/mailpilot/database.py` after `list_tasks`. Uses the two-step approach (SELECT unmatched, then INSERT via `create_task()` loop) to match codebase ID generation conventions. The number of inbound emails per sync cycle is small, so per-row inserts are fine.

```python
def create_tasks_for_routed_emails(
    connection: psycopg.Connection[dict[str, Any]],
) -> list[Task]:
    """Create immediate tasks for routed inbound emails without tasks.

    Finds inbound emails with workflow_id set but no corresponding task
    row, and creates a task with scheduled_at=now() for each.

    Args:
        connection: Open database connection.

    Returns:
        List of newly created tasks.
    """
    with logfire.span("db.task.bridge_routed_emails") as span:
        unmatched = connection.execute(
            """\
            SELECT e.id, e.workflow_id, e.contact_id FROM email e
            WHERE e.workflow_id IS NOT NULL
              AND e.direction = 'inbound'
              AND e.contact_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM task t WHERE t.email_id = e.id)
            ORDER BY e.created_at
            """
        ).fetchall()
        tasks: list[Task] = []
        for email_row in unmatched:
            now = datetime.now(UTC).isoformat()
            task = create_task(
                connection,
                workflow_id=email_row["workflow_id"],
                contact_id=email_row["contact_id"],
                description="handle inbound email",
                scheduled_at=now,
                email_id=email_row["id"],
            )
            tasks.append(task)
        span.set_attribute("task_count", len(tasks))
        return tasks
```

Note: `datetime` and `UTC` are already imported in `database.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_database.py::test_create_tasks_for_routed_emails -v`
Expected: PASS

- [ ] **Step 5: Write test for emails that already have tasks (no duplicates)**

Already covered by the idempotency check in step 1.

- [ ] **Step 6: Write test for outbound emails (should be skipped)**

```python
def test_create_tasks_for_routed_emails_skips_outbound(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    contact = make_test_contact(database_connection)

    from mailpilot.database import create_email

    create_email(
        database_connection,
        gmail_message_id="msg-002",
        gmail_thread_id="thread-002",
        account_id=account.id,
        direction="outbound",
        subject="Hello",
        body_text="Hi there",
        labels=["SENT"],
        sent_at="2026-04-22T12:00:00Z",
        contact_id=contact.id,
        workflow_id=workflow.id,
    )

    created = create_tasks_for_routed_emails(database_connection)
    assert len(created) == 0
```

- [ ] **Step 7: Run tests and lint**

Run: `uv run pytest tests/test_database.py::test_create_tasks_for_routed_emails tests/test_database.py::test_create_tasks_for_routed_emails_skips_outbound -v && uv run ruff check --fix && uv run basedpyright`
Expected: All pass, clean

- [ ] **Step 8: Commit**

```bash
git add src/mailpilot/database.py tests/test_database.py
git commit -m "feat(db): add create_tasks_for_routed_emails() bridge function"
```

---

### Task 7: Implement `execute_task()` in `run.py`

The core function that loads context for a task and invokes the agent.

**Files:**
- Create: `src/mailpilot/run.py`
- Create: `tests/test_run.py`

- [ ] **Step 1: Write failing test for `execute_task` success path**

Create `tests/test_run.py`:

```python
"""Tests for the run loop module."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from mailpilot.models import (
    Account,
    Contact,
    Email,
    Task,
    Workflow,
)

_NOW = datetime(2024, 1, 1, tzinfo=UTC)
_ACCOUNT_ID = "01234567-0000-7000-0000-000000000001"
_WORKFLOW_ID = "01234567-0000-7000-0000-000000000002"
_CONTACT_ID = "01234567-0000-7000-0000-000000000003"
_TASK_ID = "01234567-0000-7000-0000-000000000004"
_EMAIL_ID = "01234567-0000-7000-0000-000000000005"


def _make_workflow(**overrides: Any) -> Workflow:
    defaults: dict[str, Any] = {
        "id": _WORKFLOW_ID,
        "name": "Test workflow",
        "type": "outbound",
        "account_id": _ACCOUNT_ID,
        "status": "active",
        "objective": "Test",
        "instructions": "Do the thing.",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Workflow(**{**defaults, **overrides})


def _make_contact(**overrides: Any) -> Contact:
    defaults: dict[str, Any] = {
        "id": _CONTACT_ID,
        "email": "test@example.com",
        "domain": "example.com",
        "status": "active",
        "status_reason": "",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Contact(**{**defaults, **overrides})


def _make_task(**overrides: Any) -> Task:
    defaults: dict[str, Any] = {
        "id": _TASK_ID,
        "workflow_id": _WORKFLOW_ID,
        "contact_id": _CONTACT_ID,
        "email_id": None,
        "description": "follow up",
        "context": {},
        "scheduled_at": _NOW,
        "status": "pending",
        "completed_at": None,
        "created_at": _NOW,
    }
    return Task(**{**defaults, **overrides})


def _make_email(**overrides: Any) -> Email:
    defaults: dict[str, Any] = {
        "id": _EMAIL_ID,
        "gmail_message_id": "msg-001",
        "gmail_thread_id": "thread-001",
        "account_id": _ACCOUNT_ID,
        "contact_id": _CONTACT_ID,
        "workflow_id": _WORKFLOW_ID,
        "direction": "inbound",
        "subject": "Re: hello",
        "body_text": "Got it",
        "labels": ["INBOX"],
        "status": "received",
        "is_routed": True,
        "received_at": _NOW,
        "created_at": _NOW,
    }
    return Email(**{**defaults, **overrides})


def test_execute_task_success(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from conftest import make_test_settings

    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task()
    workflow = _make_workflow()
    contact = _make_contact()

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch(
            "mailpilot.run.invoke_workflow_agent",
            return_value={"tool_calls": 2},
        ) as mock_invoke,
        patch("mailpilot.run.complete_task") as mock_complete,
    ):
        execute_task(database_connection, settings, task)

    mock_invoke.assert_called_once_with(
        database_connection,
        settings,
        workflow,
        contact,
        email=None,
        task_description="follow up",
        task_context={},
    )
    mock_complete.assert_called_once_with(
        database_connection, _TASK_ID, status="completed"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_run.py::test_execute_task_success -v`
Expected: FAIL with `ModuleNotFoundError` (run.py doesn't exist)

- [ ] **Step 3: Implement `execute_task()` in `run.py`**

Create `src/mailpilot/run.py`:

```python
"""Workflow execution loop.

Composes account sync, inbound email-to-task bridging, and task
execution in a single loop. Tasks are the universal execution
primitive -- all agent invocations flow through the task queue.
"""

from __future__ import annotations

from typing import Any

import logfire
import psycopg

from mailpilot.agent import invoke_workflow_agent
from mailpilot.database import (
    complete_task,
    get_contact,
    get_email,
    get_workflow,
)
from mailpilot.models import Task
from mailpilot.settings import Settings


def execute_task(
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
    task: Task,
) -> None:
    """Execute a single pending task by invoking the workflow agent.

    Args:
        connection: Open database connection.
        settings: Application settings.
        task: Pending task to execute.
    """
    with logfire.span(
        "run.execute_task",
        task_id=task.id,
        workflow_id=task.workflow_id,
        contact_id=task.contact_id,
    ):
        workflow = get_workflow(connection, task.workflow_id)
        if workflow is None or workflow.status != "active":
            logfire.info(
                "run.task.skip_inactive_workflow",
                task_id=task.id,
                workflow_id=task.workflow_id,
            )
            complete_task(connection, task.id, status="cancelled")
            return

        contact = get_contact(connection, task.contact_id)
        if contact is None or contact.status in ("bounced", "unsubscribed"):
            logfire.info(
                "run.task.skip_disabled_contact",
                task_id=task.id,
                contact_id=task.contact_id,
            )
            complete_task(connection, task.id, status="cancelled")
            return

        email = get_email(connection, task.email_id) if task.email_id else None

        try:
            result = invoke_workflow_agent(
                connection,
                settings,
                workflow,
                contact,
                email=email,
                task_description=task.description,
                task_context=task.context,
            )
        except Exception:
            logfire.exception(
                "run.task.agent_failed",
                task_id=task.id,
            )
            complete_task(connection, task.id, status="failed")
            return

        if result is None:
            logfire.info(
                "run.task.lock_held",
                task_id=task.id,
            )
            return

        complete_task(connection, task.id, status="completed")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_run.py::test_execute_task_success -v`
Expected: PASS

- [ ] **Step 5: Write test for inactive workflow (task cancelled)**

```python
def test_execute_task_inactive_workflow(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from conftest import make_test_settings

    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task()
    workflow = _make_workflow(status="paused")

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.complete_task") as mock_complete,
    ):
        execute_task(database_connection, settings, task)

    mock_complete.assert_called_once_with(
        database_connection, _TASK_ID, status="cancelled"
    )
```

- [ ] **Step 6: Write test for disabled contact (task cancelled)**

```python
def test_execute_task_disabled_contact(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from conftest import make_test_settings

    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task()
    workflow = _make_workflow()
    contact = _make_contact(status="bounced")

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.complete_task") as mock_complete,
    ):
        execute_task(database_connection, settings, task)

    mock_complete.assert_called_once_with(
        database_connection, _TASK_ID, status="cancelled"
    )
```

- [ ] **Step 7: Write test for lock held (task stays pending)**

```python
def test_execute_task_lock_held(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from conftest import make_test_settings

    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task()
    workflow = _make_workflow()
    contact = _make_contact()

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.invoke_workflow_agent", return_value=None),
        patch("mailpilot.run.complete_task") as mock_complete,
    ):
        execute_task(database_connection, settings, task)

    mock_complete.assert_not_called()
```

- [ ] **Step 8: Write test for agent exception (task failed)**

```python
def test_execute_task_agent_error(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from conftest import make_test_settings

    from mailpilot.run import execute_task

    settings = make_test_settings()
    task = _make_task()
    workflow = _make_workflow()
    contact = _make_contact()

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch(
            "mailpilot.run.invoke_workflow_agent",
            side_effect=RuntimeError("LLM error"),
        ),
        patch("mailpilot.run.complete_task") as mock_complete,
    ):
        execute_task(database_connection, settings, task)

    mock_complete.assert_called_once_with(
        database_connection, _TASK_ID, status="failed"
    )
```

- [ ] **Step 9: Write test for task with email reference**

```python
def test_execute_task_with_email(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from conftest import make_test_settings

    from mailpilot.run import execute_task

    settings = make_test_settings()
    email = _make_email()
    task = _make_task(email_id=_EMAIL_ID)
    workflow = _make_workflow()
    contact = _make_contact()

    with (
        patch("mailpilot.run.get_workflow", return_value=workflow),
        patch("mailpilot.run.get_contact", return_value=contact),
        patch("mailpilot.run.get_email", return_value=email),
        patch(
            "mailpilot.run.invoke_workflow_agent",
            return_value={"tool_calls": 1},
        ) as mock_invoke,
        patch("mailpilot.run.complete_task"),
    ):
        execute_task(database_connection, settings, task)

    mock_invoke.assert_called_once_with(
        database_connection,
        settings,
        workflow,
        contact,
        email=email,
        task_description="follow up",
        task_context={},
    )
```

- [ ] **Step 10: Run all tests and lint**

Run: `uv run pytest tests/test_run.py -v && uv run ruff check --fix && uv run basedpyright`
Expected: All pass, clean

- [ ] **Step 11: Commit**

```bash
git add src/mailpilot/run.py tests/test_run.py
git commit -m "feat: add execute_task() in run.py"
```

---

### Task 8: Implement `run_loop()` and wire up `mailpilot run`

**Files:**
- Modify: `src/mailpilot/run.py` (add `run_loop`)
- Modify: `src/mailpilot/cli.py:153-163` (update `run` command)
- Modify: `tests/test_run.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write test for `run_loop` single iteration**

Add to `tests/test_run.py`:

```python
def test_run_loop_single_iteration(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from conftest import make_test_settings

    from mailpilot.run import run_loop

    settings = make_test_settings()
    task = _make_task()

    call_count = 0

    def fake_sync(conn: Any, acc: Any, client: Any, settings: Any) -> int:
        return 0

    def stop_after_one(*args: Any, **kwargs: Any) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 1:
            raise KeyboardInterrupt

    with (
        patch("mailpilot.run.list_accounts", return_value=[]),
        patch("mailpilot.run.create_tasks_for_routed_emails", return_value=[]),
        patch("mailpilot.run.list_pending_tasks", return_value=[task]),
        patch("mailpilot.run.execute_task", side_effect=stop_after_one) as mock_exec,
    ):
        run_loop(database_connection, settings)

    mock_exec.assert_called_once_with(database_connection, settings, task)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_run.py::test_run_loop_single_iteration -v`
Expected: FAIL (`run_loop` not defined)

- [ ] **Step 3: Implement `run_loop()`**

Add to `src/mailpilot/run.py`:

```python
import time

from mailpilot.database import (
    complete_task,
    create_tasks_for_routed_emails,
    get_contact,
    get_email,
    get_workflow,
    list_accounts,
    list_pending_tasks,
)
from mailpilot.gmail import GmailClient
from mailpilot.sync import sync_account
```

Update the imports at the top (merge with existing). Then add the function:

```python
def run_loop(
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
) -> None:
    """Run the main execution loop.

    Each iteration:
    1. Sync all accounts (Gmail fetch + inbound routing).
    2. Bridge routed emails to tasks.
    3. Drain pending task queue.
    4. Sleep for run_interval seconds.

    Exits cleanly on KeyboardInterrupt (Ctrl+C / SIGINT).

    Args:
        connection: Open database connection.
        settings: Application settings.
    """
    logfire.info("run.loop.start", interval=settings.run_interval)
    try:
        while True:
            with logfire.span("run.loop.iteration"):
                _sync_all_accounts(connection, settings)
                create_tasks_for_routed_emails(connection)
                pending = list_pending_tasks(connection)
                for task in pending:
                    execute_task(connection, settings, task)
            time.sleep(settings.run_interval)
    except KeyboardInterrupt:
        logfire.info("run.loop.stop")


def _sync_all_accounts(
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
) -> None:
    """Sync all Gmail accounts. Errors per account are logged, not raised."""
    accounts = list_accounts(connection)
    for account in accounts:
        try:
            client = GmailClient(account.email)
            sync_account(connection, account, client, settings)
        except Exception:
            logfire.exception(
                "run.sync.account_failed",
                account_id=account.id,
                email=account.email,
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_run.py::test_run_loop_single_iteration -v`
Expected: PASS

- [ ] **Step 5: Write test for sync errors not crashing the loop**

```python
def test_run_loop_sync_error_continues(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from conftest import make_test_settings

    from mailpilot.models import Account
    from mailpilot.run import run_loop

    settings = make_test_settings()
    account = Account(
        id=_ACCOUNT_ID,
        email="test@example.com",
        display_name="Test",
        created_at=_NOW,
        updated_at=_NOW,
    )

    def stop_on_bridge(*args: Any, **kwargs: Any) -> list[Any]:
        raise KeyboardInterrupt

    with (
        patch("mailpilot.run.list_accounts", return_value=[account]),
        patch("mailpilot.run.GmailClient", side_effect=RuntimeError("auth failed")),
        patch(
            "mailpilot.run.create_tasks_for_routed_emails",
            side_effect=stop_on_bridge,
        ),
    ):
        run_loop(database_connection, settings)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_run.py::test_run_loop_sync_error_continues -v`
Expected: PASS (sync error is caught, loop continues to bridge step where we stop it)

- [ ] **Step 7: Update `mailpilot run` CLI command**

Replace `src/mailpilot/cli.py` lines 153-163:

```python
@main.command()
def run() -> None:
    """Start the execution loop (sync + task runner, foreground)."""
    from mailpilot.database import initialize_database
    from mailpilot.run import run_loop
    from mailpilot.settings import get_settings

    settings = get_settings()
    connection = initialize_database(_database_url())
    try:
        run_loop(connection, settings)
    finally:
        connection.close()
```

- [ ] **Step 8: Write CLI test for `run` command**

```python
def test_run_command(runner: CliRunner, mock_connection: MagicMock) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.run.run_loop") as mock_loop,
    ):
        result = runner.invoke(main, ["run"])

    assert result.exit_code == 0, result.output
    mock_loop.assert_called_once_with(mock_connection, make_test_settings())
```

- [ ] **Step 9: Run full test suite and lint**

Run: `uv run pytest -x && uv run ruff check --fix && uv run basedpyright`
Expected: All pass, clean

- [ ] **Step 10: Commit**

```bash
git add src/mailpilot/run.py src/mailpilot/cli.py tests/test_run.py tests/test_cli.py
git commit -m "feat: implement run_loop() and wire to mailpilot run command"
```

---

### Task 9: Update CLAUDE.md and docs

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add task CLI commands to CLAUDE.md**

In the CLI reference section, add after the `workflow contact` commands:

```
mailpilot task list [--workflow-id ID] [--contact-id ID] [--status pending|completed|failed|cancelled] [--limit N]
mailpilot task view ID
mailpilot task cancel ID
```

- [ ] **Step 2: Add `run_interval` to settings section**

In the Settings section, add:

```
- `run_interval` -- Execution loop sleep interval in seconds (default: `30`)
```

- [ ] **Step 3: Update `mailpilot run` description**

The existing `mailpilot run` line in the CLI reference should already be there. Verify it reads correctly in context with the new task commands.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add task CLI commands and run_interval setting to CLAUDE.md"
```

---

### Task 10: Full integration verification

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest -x`
Expected: All tests pass

- [ ] **Step 2: Run lint and type check**

Run: `uv run ruff check --fix && uv run basedpyright`
Expected: Clean

- [ ] **Step 3: Manual smoke test of task CLI**

```bash
uv run mailpilot task list
uv run mailpilot task list --status pending
uv run mailpilot task list --help
uv run mailpilot task view --help
uv run mailpilot task cancel --help
```

- [ ] **Step 4: Verify `mailpilot run --help`**

```bash
uv run mailpilot run --help
```

Expected: Updated help text mentioning execution loop.
