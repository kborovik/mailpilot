# Workflow CLI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve the workflow CLI with inline `--instructions`, active-by-default creation, `start`/`stop` verbs, and actionable error messages.

**Architecture:** CLI-only changes in `cli.py` and `test_cli.py`. No database changes. The `activate_workflow`/`pause_workflow` database functions keep their names and behavior; only the CLI commands that call them are renamed.

**Tech Stack:** Python, Click, pytest, basedpyright, ruff

---

### Task 1: Add `--instructions` inline option to `workflow update`

Starting with `update` because it's simpler (no auto-activation logic).

**Files:**
- Modify: `src/mailpilot/cli.py:1282-1317` (workflow_update command)
- Modify: `tests/test_cli.py:1646-1707` (workflow update tests)

- [ ] **Step 1: Write failing test for `--instructions` inline on update**

Add to `tests/test_cli.py` after `test_workflow_update_with_instructions_file`:

```python
def test_workflow_update_with_inline_instructions(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    updated = _make_workflow(instructions="Be concise.")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.update_workflow", return_value=updated
        ) as mock_update,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "update",
                _WORKFLOW_ID,
                "--instructions",
                "Be concise.",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_update.assert_called_once_with(
        mock_connection, _WORKFLOW_ID, instructions="Be concise."
    )
```

- [ ] **Step 2: Write failing test for mutual exclusion on update**

```python
def test_workflow_update_instructions_mutual_exclusion(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    instructions_file = tmp_path / "instructions.md"
    instructions_file.write_text("From file.")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "update",
                _WORKFLOW_ID,
                "--instructions",
                "Inline text",
                "--instructions-file",
                str(instructions_file),
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "mutually exclusive" in data["message"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py::test_workflow_update_with_inline_instructions tests/test_cli.py::test_workflow_update_instructions_mutual_exclusion -v`
Expected: FAIL (no such option `--instructions`)

- [ ] **Step 4: Implement `--instructions` on `workflow update`**

In `src/mailpilot/cli.py`, modify the `workflow_update` command. Add the `--instructions` option and mutual exclusion check:

```python
@workflow.command("update")
@click.argument("workflow_id")
@click.option("--name", default=None, help="Workflow name.")
@click.option("--objective", default=None, help="Workflow objective.")
@click.option(
    "--instructions",
    default=None,
    help="Workflow instructions (inline text).",
)
@click.option(
    "--instructions-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a file with the workflow instructions (system prompt).",
)
def workflow_update(
    workflow_id: str,
    name: str | None,
    objective: str | None,
    instructions: str | None,
    instructions_file: str | None,
) -> None:
    """Update a workflow."""
    import pathlib

    from mailpilot.database import initialize_database, update_workflow

    if instructions is not None and instructions_file is not None:
        output_error(
            "--instructions and --instructions-file are mutually exclusive",
            "validation_error",
        )
    connection = initialize_database(_database_url())
    try:
        fields: dict[str, object] = {}
        if name is not None:
            fields["name"] = name
        if objective is not None:
            fields["objective"] = objective
        if instructions is not None:
            fields["instructions"] = instructions
        if instructions_file is not None:
            fields["instructions"] = pathlib.Path(instructions_file).read_text()
        updated = update_workflow(connection, workflow_id, **fields)
        if updated is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        output(updated.model_dump(mode="json"))
    finally:
        connection.close()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -k "test_workflow_update" -v`
Expected: all update tests PASS

- [ ] **Step 6: Run lint and type checks**

Run: `uv run ruff check --fix && uv run basedpyright`

- [ ] **Step 7: Commit**

```bash
git add src/mailpilot/cli.py tests/test_cli.py
git commit -m "feat(cli): add --instructions inline option to workflow update"
```

### Task 2: Add `--instructions` inline option to `workflow create`

**Files:**
- Modify: `src/mailpilot/cli.py:1222-1279` (workflow_create command)
- Modify: `tests/test_cli.py:1483-1641` (workflow create tests)

- [ ] **Step 1: Write failing test for `--instructions` inline on create**

Add to `tests/test_cli.py` after `test_workflow_create_with_objective_and_instructions`:

```python
def test_workflow_create_with_inline_instructions(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    workflow = _make_workflow(
        objective="Book demo", instructions="You are a sales rep."
    )
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.create_workflow", return_value=_make_workflow()),
        patch(
            "mailpilot.database.update_workflow", return_value=workflow
        ) as mock_update,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "create",
                "--name",
                "Demo outreach",
                "--type",
                "outbound",
                "--account-id",
                _ACCOUNT_ID,
                "--objective",
                "Book demo",
                "--instructions",
                "You are a sales rep.",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_update.assert_called_once_with(
        mock_connection,
        _WORKFLOW_ID,
        objective="Book demo",
        instructions="You are a sales rep.",
    )
```

- [ ] **Step 2: Write failing test for mutual exclusion on create**

```python
def test_workflow_create_instructions_mutual_exclusion(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    instructions_file = tmp_path / "instructions.md"
    instructions_file.write_text("From file.")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "create",
                "--name",
                "Test",
                "--type",
                "outbound",
                "--account-id",
                _ACCOUNT_ID,
                "--instructions",
                "Inline text",
                "--instructions-file",
                str(instructions_file),
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "mutually exclusive" in data["message"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py::test_workflow_create_with_inline_instructions tests/test_cli.py::test_workflow_create_instructions_mutual_exclusion -v`
Expected: FAIL

- [ ] **Step 4: Implement `--instructions` on `workflow create`**

In `src/mailpilot/cli.py`, modify the `workflow_create` command. Add `--instructions` option and mutual exclusion check:

```python
@workflow.command("create")
@click.option("--name", required=True, help="Workflow name.")
@click.option(
    "--type",
    "workflow_type",
    required=True,
    type=click.Choice(["inbound", "outbound"]),
    help="Workflow direction. Immutable after creation.",
)
@click.option("--account-id", required=True, help="Owning Gmail account ID.")
@click.option("--objective", default=None, help="Workflow objective.")
@click.option(
    "--instructions",
    default=None,
    help="Workflow instructions (inline text).",
)
@click.option(
    "--instructions-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a file with the workflow instructions (system prompt).",
)
def workflow_create(
    name: str,
    workflow_type: str,
    account_id: str,
    objective: str | None,
    instructions: str | None,
    instructions_file: str | None,
) -> None:
    """Create a new workflow."""
    import pathlib

    from mailpilot.database import (
        create_workflow,
        get_account,
        initialize_database,
        update_workflow,
    )

    if not name.strip():
        output_error("workflow name cannot be empty", "validation_error")
    if instructions is not None and instructions_file is not None:
        output_error(
            "--instructions and --instructions-file are mutually exclusive",
            "validation_error",
        )
    connection = initialize_database(_database_url())
    try:
        if get_account(connection, account_id) is None:
            output_error(f"account not found: {account_id}", "not_found")
        created = create_workflow(
            connection,
            name=name,
            workflow_type=workflow_type,
            account_id=account_id,
        )
        extras: dict[str, object] = {}
        if objective is not None:
            extras["objective"] = objective
        if instructions is not None:
            extras["instructions"] = instructions
        if instructions_file is not None:
            extras["instructions"] = pathlib.Path(instructions_file).read_text()
        if extras:
            updated = update_workflow(connection, created.id, **extras)
            if updated is not None:
                created = updated
        output(created.model_dump(mode="json"))
    finally:
        connection.close()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -k "test_workflow_create" -v`
Expected: all create tests PASS

- [ ] **Step 6: Run lint and type checks**

Run: `uv run ruff check --fix && uv run basedpyright`

- [ ] **Step 7: Commit**

```bash
git add src/mailpilot/cli.py tests/test_cli.py
git commit -m "feat(cli): add --instructions inline option to workflow create"
```

### Task 3: Add `--draft` flag and active-by-default to `workflow create`

**Files:**
- Modify: `src/mailpilot/cli.py:1222-1279` (workflow_create command)
- Modify: `tests/test_cli.py` (workflow create tests)

- [ ] **Step 1: Write failing test for auto-activation on create**

When both `--objective` and `--instructions` are provided (no `--draft`), the workflow should be activated automatically.

```python
def test_workflow_create_auto_activates(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    created = _make_workflow()
    updated = _make_workflow(objective="Book demo", instructions="You are a sales rep.")
    activated = _make_workflow(
        status="active", objective="Book demo", instructions="You are a sales rep."
    )
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.create_workflow", return_value=created),
        patch("mailpilot.database.update_workflow", return_value=updated),
        patch(
            "mailpilot.database.activate_workflow", return_value=activated
        ) as mock_activate,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "create",
                "--name",
                "Demo outreach",
                "--type",
                "outbound",
                "--account-id",
                _ACCOUNT_ID,
                "--objective",
                "Book demo",
                "--instructions",
                "You are a sales rep.",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_activate.assert_called_once_with(mock_connection, _WORKFLOW_ID)
    data = json.loads(result.output)
    assert data["status"] == "active"
```

- [ ] **Step 2: Write failing test for `--draft` flag suppressing activation**

```python
def test_workflow_create_draft_skips_activation(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    workflow = _make_workflow(
        objective="Book demo", instructions="You are a sales rep."
    )
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.create_workflow", return_value=_make_workflow()),
        patch("mailpilot.database.update_workflow", return_value=workflow),
        patch("mailpilot.database.activate_workflow") as mock_activate,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "create",
                "--name",
                "Demo outreach",
                "--type",
                "outbound",
                "--account-id",
                _ACCOUNT_ID,
                "--objective",
                "Book demo",
                "--instructions",
                "You are a sales rep.",
                "--draft",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_activate.assert_not_called()
    data = json.loads(result.output)
    assert data["status"] == "draft"
```

- [ ] **Step 3: Write failing test for missing fields without `--draft`**

```python
def test_workflow_create_missing_fields_without_draft(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "create",
                "--name",
                "Demo outreach",
                "--type",
                "outbound",
                "--account-id",
                _ACCOUNT_ID,
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"
    assert "--draft" in data["message"]
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py::test_workflow_create_auto_activates tests/test_cli.py::test_workflow_create_draft_skips_activation tests/test_cli.py::test_workflow_create_missing_fields_without_draft -v`
Expected: FAIL

- [ ] **Step 5: Implement `--draft` flag and auto-activation**

In `src/mailpilot/cli.py`, add `--draft` flag and auto-activation logic to `workflow_create`. Add the `--draft` option decorator and `draft` parameter. Add `activate_workflow` to the imports. After the existing create/update logic, add:

```python
@click.option("--draft", is_flag=True, default=False, help="Keep workflow in draft status.")
```

Add `draft: bool` to the function signature. Update the import to include `activate_workflow`.

After the existing output logic, replace the final section of the function body (after `create_workflow` and the extras block) with:

```python
        has_objective = objective is not None
        has_instructions = instructions is not None or instructions_file is not None
        if not draft and not (has_objective and has_instructions):
            output_error(
                "cannot activate workflow without objective and instructions. "
                "Use --draft to create without them.",
                "validation_error",
            )
        created = create_workflow(
            connection,
            name=name,
            workflow_type=workflow_type,
            account_id=account_id,
        )
        extras: dict[str, object] = {}
        if objective is not None:
            extras["objective"] = objective
        if instructions is not None:
            extras["instructions"] = instructions
        if instructions_file is not None:
            extras["instructions"] = pathlib.Path(instructions_file).read_text()
        if extras:
            updated = update_workflow(connection, created.id, **extras)
            if updated is not None:
                created = updated
        if not draft and has_objective and has_instructions:
            created = activate_workflow(connection, created.id)
        output(created.model_dump(mode="json"))
```

- [ ] **Step 6: Fix existing test `test_workflow_create` -- now expects validation error without `--draft`**

The bare `workflow create` test (no objective, no instructions, no `--draft`) now errors. Update it to use `--draft`:

```python
def test_workflow_create(runner: CliRunner, mock_connection: MagicMock) -> None:
    workflow = _make_workflow()
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch(
            "mailpilot.database.create_workflow", return_value=workflow
        ) as mock_create,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "create",
                "--name",
                "Demo outreach",
                "--type",
                "outbound",
                "--account-id",
                _ACCOUNT_ID,
                "--draft",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_create.assert_called_once_with(
        mock_connection,
        name="Demo outreach",
        workflow_type="outbound",
        account_id=_ACCOUNT_ID,
    )
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["id"] == workflow.id
```

- [ ] **Step 7: Fix existing tests that now auto-activate**

Both `test_workflow_create_with_objective_and_instructions` and `test_workflow_create_with_inline_instructions` (from Task 2) provide objective + instructions without `--draft`, so they now auto-activate. Add the `activate_workflow` mock to both.

For `test_workflow_create_with_objective_and_instructions`:

```python
def test_workflow_create_with_objective_and_instructions(
    runner: CliRunner, mock_connection: MagicMock, tmp_path: pathlib.Path
) -> None:
    workflow = _make_workflow(
        objective="Book demo", instructions="You are a sales rep."
    )
    activated = _make_workflow(
        status="active", objective="Book demo", instructions="You are a sales rep."
    )
    instructions_file = tmp_path / "instructions.md"
    instructions_file.write_text("You are a sales rep.")
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.create_workflow", return_value=_make_workflow()),
        patch("mailpilot.database.update_workflow", return_value=workflow),
        patch(
            "mailpilot.database.activate_workflow", return_value=activated
        ) as mock_activate,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "create",
                "--name",
                "Demo outreach",
                "--type",
                "outbound",
                "--account-id",
                _ACCOUNT_ID,
                "--objective",
                "Book demo",
                "--instructions-file",
                str(instructions_file),
            ],
        )

    assert result.exit_code == 0, result.output
    mock_activate.assert_called_once_with(mock_connection, _WORKFLOW_ID)
    data = json.loads(result.output)
    assert data["status"] == "active"
```

For `test_workflow_create_with_inline_instructions` (added in Task 2), apply the same pattern -- add `activate_workflow` mock and assert activation:

```python
def test_workflow_create_with_inline_instructions(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    workflow = _make_workflow(
        objective="Book demo", instructions="You are a sales rep."
    )
    activated = _make_workflow(
        status="active", objective="Book demo", instructions="You are a sales rep."
    )
    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.create_workflow", return_value=_make_workflow()),
        patch("mailpilot.database.update_workflow", return_value=workflow),
        patch(
            "mailpilot.database.activate_workflow", return_value=activated
        ) as mock_activate,
    ):
        result = runner.invoke(
            main,
            [
                "workflow",
                "create",
                "--name",
                "Demo outreach",
                "--type",
                "outbound",
                "--account-id",
                _ACCOUNT_ID,
                "--objective",
                "Book demo",
                "--instructions",
                "You are a sales rep.",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_activate.assert_called_once_with(mock_connection, _WORKFLOW_ID)
    data = json.loads(result.output)
    assert data["status"] == "active"
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -k "test_workflow_create" -v`
Expected: all create tests PASS

- [ ] **Step 9: Run lint and type checks**

Run: `uv run ruff check --fix && uv run basedpyright`

- [ ] **Step 10: Commit**

```bash
git add src/mailpilot/cli.py tests/test_cli.py
git commit -m "feat(cli): active-by-default workflow create with --draft override"
```

### Task 4: Rename `activate`/`pause` to `start`/`stop` with actionable errors

**Files:**
- Modify: `src/mailpilot/cli.py:1387-1418` (activate and pause commands)
- Modify: `tests/test_cli.py:1826-1901` (activate and pause tests)

- [ ] **Step 1: Write failing test for `workflow start` with actionable error**

Replace the activate/pause test section in `tests/test_cli.py`:

```python
# -- workflow start / stop -----------------------------------------------------


def test_workflow_start(runner: CliRunner, mock_connection: MagicMock) -> None:
    activated = _make_workflow(
        status="active",
        objective="Book demo",
        instructions="You are a sales rep.",
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.activate_workflow", return_value=activated
        ) as mock_activate,
    ):
        result = runner.invoke(main, ["workflow", "start", _WORKFLOW_ID])

    assert result.exit_code == 0, result.output
    mock_activate.assert_called_once_with(mock_connection, _WORKFLOW_ID)
    data = json.loads(result.output)
    assert data["status"] == "active"


def test_workflow_start_missing_objective(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.activate_workflow",
            side_effect=ValueError("objective must be non-empty to activate"),
        ),
    ):
        result = runner.invoke(main, ["workflow", "start", _WORKFLOW_ID])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "invalid_state"
    assert "workflow update" in data["message"]
    assert "--objective" in data["message"]


def test_workflow_start_missing_instructions(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.activate_workflow",
            side_effect=ValueError("instructions must be non-empty to activate"),
        ),
    ):
        result = runner.invoke(main, ["workflow", "start", _WORKFLOW_ID])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "invalid_state"
    assert "workflow update" in data["message"]
    assert "--instructions" in data["message"]
```

- [ ] **Step 2: Write failing test for `workflow stop`**

```python
def test_workflow_stop(runner: CliRunner, mock_connection: MagicMock) -> None:
    paused = _make_workflow(status="paused")
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.pause_workflow", return_value=paused) as mock_pause,
    ):
        result = runner.invoke(main, ["workflow", "stop", _WORKFLOW_ID])

    assert result.exit_code == 0, result.output
    mock_pause.assert_called_once_with(mock_connection, _WORKFLOW_ID)
    data = json.loads(result.output)
    assert data["status"] == "paused"


def test_workflow_stop_invalid_state(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch(
            "mailpilot.database.pause_workflow",
            side_effect=ValueError("cannot pause workflow in status 'draft'"),
        ),
    ):
        result = runner.invoke(main, ["workflow", "stop", _WORKFLOW_ID])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "invalid_state"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py::test_workflow_start tests/test_cli.py::test_workflow_stop -v`
Expected: FAIL (no such command `start`/`stop`)

- [ ] **Step 4: Delete old activate/pause commands and tests, implement start/stop**

Remove the old `workflow_activate` and `workflow_pause` commands from `src/mailpilot/cli.py` (lines 1387-1418) and replace with:

```python
@workflow.command("start")
@click.argument("workflow_id")
def workflow_start(workflow_id: str) -> None:
    """Start a workflow (requires non-empty objective and instructions)."""
    from mailpilot.database import activate_workflow, initialize_database

    connection = initialize_database(_database_url())
    try:
        try:
            activated = activate_workflow(connection, workflow_id)
        except ValueError as exc:
            message = str(exc)
            if "objective" in message:
                output_error(
                    f"cannot start: objective is empty. "
                    f"Run: workflow update {workflow_id} --objective \"...\"",
                    "invalid_state",
                )
            if "instructions" in message:
                output_error(
                    f"cannot start: instructions are empty. "
                    f"Run: workflow update {workflow_id} --instructions \"...\"",
                    "invalid_state",
                )
            output_error(message, "invalid_state")
        output(activated.model_dump(mode="json"))
    finally:
        connection.close()


@workflow.command("stop")
@click.argument("workflow_id")
def workflow_stop(workflow_id: str) -> None:
    """Stop an active workflow."""
    from mailpilot.database import initialize_database, pause_workflow

    connection = initialize_database(_database_url())
    try:
        try:
            paused = pause_workflow(connection, workflow_id)
        except ValueError as exc:
            output_error(str(exc), "invalid_state")
        output(paused.model_dump(mode="json"))
    finally:
        connection.close()
```

Remove old activate/pause tests (`test_workflow_activate`, `test_workflow_activate_missing_objective`, `test_workflow_pause`, `test_workflow_pause_invalid_state`) from `tests/test_cli.py`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -k "test_workflow_start or test_workflow_stop" -v`
Expected: all PASS

- [ ] **Step 6: Run full test suite**

Run: `uv run pytest tests/test_cli.py -v`
Expected: all PASS (no leftover references to `activate`/`pause` commands)

- [ ] **Step 7: Run lint and type checks**

Run: `uv run ruff check --fix && uv run basedpyright`

- [ ] **Step 8: Commit**

```bash
git add src/mailpilot/cli.py tests/test_cli.py
git commit -m "feat(cli): rename workflow activate/pause to start/stop with actionable errors"
```

### Task 5: Update CLAUDE.md CLI spec

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the CLI surface in CLAUDE.md**

In `CLAUDE.md`, replace the workflow section of the CLI spec:

Replace:
```
mailpilot workflow create --name N --type inbound|outbound --account-id ID [--objective O] [--instructions-file F]
```

With:
```
mailpilot workflow create --name N --type inbound|outbound --account-id ID [--objective O] [--instructions TEXT | --instructions-file F] [--draft]
```

Replace:
```
mailpilot workflow update ID [--name N] [--objective O] [--instructions-file F]
```

With:
```
mailpilot workflow update ID [--name N] [--objective O] [--instructions TEXT | --instructions-file F]
```

Replace:
```
mailpilot workflow activate ID
mailpilot workflow pause ID
```

With:
```
mailpilot workflow start ID
mailpilot workflow stop ID
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLI spec for workflow redesign"
```

### Task 6: Verify everything works end-to-end

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest tests/test_cli.py -v`
Expected: all PASS

- [ ] **Step 2: Run full lint + type check**

Run: `uv run ruff check --fix && uv run basedpyright`
Expected: clean

- [ ] **Step 3: Run `make check`**

Run: `make check`
Expected: clean
