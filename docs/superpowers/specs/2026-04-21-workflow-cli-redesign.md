# Workflow CLI Redesign

## Problem

The current workflow CLI has friction for the primary use case (create a workflow and start using it):

1. No inline `--instructions` option -- forces creating a temp file for simple text.
2. Workflows always start as `draft`, requiring a separate `activate` step even when all fields are provided.
3. The `activate` error message ("instructions must be non-empty to activate") doesn't tell the user how to fix it.
4. `activate`/`pause` verbs are formal; `start`/`stop` are simpler and more intuitive.

## Changes

### 1. Add `--instructions` inline option

Both `workflow create` and `workflow update` gain `--instructions` as a mutually exclusive alternative to `--instructions-file`.

```
mailpilot workflow create --name N --type T --account-id ID \
  --objective "..." --instructions "..."

mailpilot workflow update ID --instructions "new instructions"
```

Mutual exclusion: providing both `--instructions` and `--instructions-file` is an error.

```
Error: --instructions and --instructions-file are mutually exclusive
```

`--instructions-file` remains for long/multi-paragraph instructions loaded from disk.

### 2. Active by default on create

When `workflow create` is called with both `--objective` and instructions (via `--instructions` or `--instructions-file`), the workflow is created and immediately activated. No separate `start` step needed.

The `--draft` flag overrides this behavior, keeping the workflow in `draft` status regardless of which fields are provided.

```
# Active immediately (objective + instructions present):
mailpilot workflow create --name N --type outbound --account-id ID \
  --objective "..." --instructions "..."

# Explicit draft:
mailpilot workflow create --name N --type outbound --account-id ID \
  --objective "..." --instructions "..." --draft
```

When `--objective` or instructions are missing and `--draft` is not set, the command errors:

```
Error: cannot activate workflow without objective and instructions. Use --draft to create without them.
```

This eliminates the 3-step ceremony (create -> update instructions -> activate) for the common case.

### 3. Rename `activate`/`pause` to `start`/`stop`

| Current | New |
|---------|-----|
| `workflow activate ID` | `workflow start ID` |
| `workflow pause ID` | `workflow stop ID` |

Semantics are unchanged: `start` transitions `draft`/`paused` to `active`; `stop` transitions `active` to `paused`.

### 4. Actionable error messages on `start`

Current errors from the database layer are accurate but don't guide the user. The CLI wraps them with fix suggestions:

| Condition | Current message | New message |
|-----------|----------------|-------------|
| Missing objective | `objective must be non-empty to activate` | `cannot start: objective is empty. Run: workflow update ID --objective "..."` |
| Missing instructions | `instructions must be non-empty to activate` | `cannot start: instructions are empty. Run: workflow update ID --instructions "..."` |
| Already active | `workflow is already active` | `workflow is already active` (unchanged -- no fix needed) |

The database function (`activate_workflow`) continues to raise `ValueError` with the current messages. The CLI command catches them and rewrites to the actionable versions.

## Updated CLI surface

Only changed commands shown. All other workflow commands (`list`, `view`, `search`, `run`, `contact add/remove/list/update`) are unchanged.

```
mailpilot workflow create --name N --type T --account-id ID \
  [--objective O] [--instructions TEXT | --instructions-file F] [--draft]

mailpilot workflow update ID \
  [--name N] [--objective O] [--instructions TEXT | --instructions-file F]

mailpilot workflow start ID
mailpilot workflow stop ID
```

## Updated CLAUDE.md CLI spec

Replace in the workflow section:

```
mailpilot workflow create --name N --type inbound|outbound --account-id ID [--objective O] [--instructions TEXT | --instructions-file F] [--draft]
mailpilot workflow update ID [--name N] [--objective O] [--instructions TEXT | --instructions-file F]
mailpilot workflow start ID
mailpilot workflow stop ID
```

Remove `workflow activate` and `workflow pause` lines.

## Implementation notes

- Click mutual exclusion: use a custom callback or `click.option` group. The simplest approach is to check in the function body and call `output_error()` if both are set.
- The `--draft` flag is `is_flag=True, default=False`.
- `workflow create` auto-activation: after `create_workflow()` + `update_workflow()` with extras, call `activate_workflow()` if not `--draft` and both objective and instructions are present. If activation fails (shouldn't happen since we just set the fields), propagate the error.
- Database functions `activate_workflow` and `pause_workflow` are unchanged. Only the CLI command names and error message wrapping change.
- Rename CLI functions: `workflow_activate` -> `workflow_start`, `workflow_pause` -> `workflow_stop`. The click command names change from `"activate"`/`"pause"` to `"start"`/`"stop"`.
- Tests: update all tests referencing `activate`/`pause` commands to use `start`/`stop`. Add tests for `--instructions`, mutual exclusion, `--draft` flag, auto-activation, and actionable error messages.
