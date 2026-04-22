# Workflow Execution Loop

## Problem

The workflow system has solid agent invocation mechanics (tools, advisory locking, prompt assembly) but no orchestration that triggers invocations automatically. Three gaps:

1. **No inbound reaction** -- email arrives, gets routed to a workflow, but no agent fires.
2. **No task runner** -- agent can create deferred tasks (`create_task` tool), but nothing polls `list_pending_tasks()` and re-invokes the agent.
3. **No visibility** -- no CLI commands to inspect the task queue or cancel pending tasks.

The `task` table, database functions (`create_task`, `list_pending_tasks`, `complete_task`, `cancel_task`), and agent tools (`create_task`, `cancel_task`) already exist. What's missing is the loop that connects them and the CLI to observe them.

## Design

### Core concept: task as universal execution primitive

Every agent invocation is triggered by a task. Inbound email routing creates an immediate task. The agent creates follow-up tasks. The run loop drains tasks. One queue, one code path.

### The run loop

New module: `src/mailpilot/run.py` with `run_loop(settings, on_progress)`.

New setting: `run_interval` -- loop sleep interval in seconds (default: `30`). Configurable via `mailpilot config set run_interval 5` for testing.

Each iteration:

1. **Sync** -- call `sync_accounts(connection, settings)` (existing, unchanged).
2. **Bridge routed emails to tasks** -- find inbound emails with `workflow_id` set but no corresponding task. Create a task with `scheduled_at=now()` for each:

```sql
SELECT e.* FROM email e
WHERE e.workflow_id IS NOT NULL
  AND e.direction = 'inbound'
  AND NOT EXISTS (SELECT 1 FROM task t WHERE t.email_id = e.id)
```

3. **Drain task queue** -- call `list_pending_tasks(connection)` (existing, `scheduled_at <= now() AND status = 'pending'`). For each task, call `execute_task()`.
4. **Sleep** -- sleep for `run_interval` seconds.

### Task execution

`execute_task(connection, settings, task)`:

1. Load workflow and contact from task FK references.
2. If workflow is not active: mark task cancelled, skip.
3. If contact is bounced/unsubscribed: mark task cancelled, skip.
4. Call `invoke_workflow_agent(connection, settings, workflow, contact, email=task.email, task_description=task.description, task_context=task.context)`.
5. Agent returns None (advisory lock held): leave task pending, retry next iteration.
6. Agent succeeds: `complete_task(connection, task.id, status="completed")`.
7. Agent raises: `complete_task(connection, task.id, status="failed")`.

### CLI commands

```
mailpilot task list [--workflow-id ID] [--contact-id ID] [--status pending|completed|failed|cancelled] [--limit N]
mailpilot task view ID
mailpilot task cancel ID
```

`task list` returns all statuses by default (consistent with other list commands). Use `--status pending` to see the active queue. FK filters validated when provided.

`task view` returns the full Task model including the `context` JSONB field.

`task cancel` validates task exists and is in `pending` status. Rejects completed/failed/cancelled tasks. Returns updated task record on success.

### Changes to existing code

**No changes:**
- `sync.py` -- routing continues to set `workflow_id` on emails and create `workflow_contact` entries.
- `routing.py` -- unchanged.
- `models.py` -- `Task` model already has all fields.
- `schema.sql` -- `task` table already has the right schema.

**`database.py` -- two new functions:**
- `list_tasks(connection, workflow_id?, contact_id?, status?, limit?) -> list[Task]` -- for CLI `task list` with optional filters.
- `get_task(connection, id) -> Task | None` -- for CLI `task view`.

Existing functions unchanged: `create_task`, `list_pending_tasks`, `complete_task`, `cancel_task`.

**`settings.py` -- one new field:**
- `run_interval: int = 30`

**`cli.py` -- additions:**
- `task` command group with `list`, `view`, `cancel` subcommands.
- Update `mailpilot run` to call `run_loop()` from `run.py`.

### `workflow run` -- unchanged

`workflow run --workflow-id ID --contact-id ID` stays as the manual outbound trigger. It does not drain tasks. For testing, set `run_interval` to 5 seconds and use `mailpilot run`.

## Updated CLAUDE.md CLI spec

Add to the CLI reference:

```
mailpilot task list [--workflow-id ID] [--contact-id ID] [--status pending|completed|failed|cancelled] [--limit N]
mailpilot task view ID
mailpilot task cancel ID
```

Update settings section to include `run_interval`.
