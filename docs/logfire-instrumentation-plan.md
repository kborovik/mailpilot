# Logfire Instrumentation Plan

Living plan derived from [ADR-07](adr-07-observability-with-logfire.md). Tracks what is instrumented today, what is missing, and in what order the gaps should be closed. Update this doc -- not the ADR -- as the picture changes.

## Current Coverage (2026-04-17)

Result of grepping `logfire.` across `src/mailpilot/`:

| Module                                                 | Current emissions                                    | Gap                                           |
| ------------------------------------------------------ | ---------------------------------------------------- | --------------------------------------------- |
| [cli.py](../src/mailpilot/cli.py)                      | `logfire.configure()` only                           | Correct by design -- CLI stays thin           |
| [sync.py](../src/mailpilot/sync.py)                    | `info` on start/stop/shutdown; `debug` heartbeat; `warn` on stale sync status | Missing: per-account sync spans, Pub/Sub callback spans, watch-renewal spans (pipeline unimplemented) |
| [gmail.py](../src/mailpilot/gmail.py)                  | `warn` on retryable HTTP error; `info` on send and label create; `info`/`warn` on watch ops | Missing: spans around every API call, `exception` on non-retryable failures |
| [settings.py](../src/mailpilot/settings.py)            | No emissions                                         | Missing: `info` on `config set` when values change |
| [database.py](../src/mailpilot/database.py)            | **No emissions**                                     | Missing: spans around every create/update/search/list/view |
| [agent/classify.py](../src/mailpilot/agent/classify.py) | **No emissions**                                    | Classification decisions must be traced with inputs + outputs |
| [agent/tools.py](../src/mailpilot/agent/tools.py)      | **No emissions**                                     | Every tool call is a span (Pydantic AI can auto-instrument) |
| [exceptions.py](../src/mailpilot/exceptions.py)        | N/A -- definitions only                              | Callers must emit `exception` when raising these |

## Logfire MCP Gap Analysis (2026-04-17)

Query against project `pilot`, `service_name = 'mailpilot'`, last 14 days:

```sql
SELECT span_name, level, COUNT(*) AS count
FROM records
WHERE service_name = 'mailpilot'
GROUP BY span_name, level
ORDER BY count DESC
LIMIT 100
```

Result:

| span_name                  | level | count |
| -------------------------- | ----- | ----- |
| `sync heartbeat`           | debug | 3     |
| `sync loop stopped`        | info  | 1     |
| `shutdown signal received` | info  | 1     |
| `sync loop started`        | info  | 1     |

Environment breakdown: `development` = 6, `production` = 0.

### Observations

1. **Zero records from CLI commands.** `account`, `company`, `contact`, `config`, `status` all execute without emitting anything. Every one of these hits `database.py`, which is uninstrumented -- so instrumenting `database.py` will light up every CLI path at once.
2. **Zero records from `production`.** No deployed service is sending traces. Either production does not exist yet or `logfire_token` is unset there.
3. **Span names are sentence-case strings, not dot-separated.** Existing names (`"sync loop started"`) do not follow the ADR-07 convention (`sync.loop.start`). Rename during instrumentation work; the low current volume makes renames cheap.
4. **No error records.** Either nothing has failed, or failures are escaping without being logged. Adding `logfire.exception` in error paths is the only way to tell.
5. **Console-only development data.** The single `production` filter matters: the `pilot` project currently contains more `leadpilot` traffic than `mailpilot` traffic, so untagged queries would mislead.

## Module-by-Module Proposed Instrumentation

### `database.py` (Priority 1)

Every CRUD function gets a span. Use `@logfire.instrument("db.<entity>.<op>")` as a decorator to keep boilerplate out of function bodies.

| Function                              | Span name                  | Attributes                                       |
| ------------------------------------- | -------------------------- | ------------------------------------------------ |
| `create_account`                      | `db.account.create`        | `account_id` (from return), `email`              |
| `get_account`                         | `db.account.get`           | `account_id`, `hit` (bool)                       |
| `list_accounts`                       | `db.account.list`          | `account_count`                                  |
| `update_account`                      | `db.account.update`        | `account_id`, `updated_fields` (list of keys)    |
| `create_company` / `update` / `list` / `view` / `search` | `db.company.*`  | `company_id`, `domain` where applicable          |
| `create_contact` / `update` / `list` / `view` / `search` | `db.contact.*`  | `contact_id`, `email`, `company_id` where applicable |
| `create_workflow` / `update` / `activate` / `pause` | `db.workflow.*`   | `workflow_id`, `account_id`, `status`            |
| `create_email` / `list` / `view` / `search` | `db.email.*`         | `email_id`, `account_id`, `direction`            |
| `create_task` / `complete_task`       | `db.task.*`                | `task_id`, `workflow_id`                         |
| Sync state (`get_sync_status`, `upsert_sync_status`, `update_sync_heartbeat`, `delete_sync_status`) | `db.sync_status.*` | `pid`        |
| `initialize_database`                 | `db.schema.apply`          | `database_url_host`, `schema_applied` (bool)     |

Level: spans for all; `logfire.exception` on the `psycopg.OperationalError` path in `initialize_database`.

### `sync.py` (Priority 1 for existing code, Priority 3 for unimplemented pipeline)

Already partially instrumented. Changes:

- Rename existing log events to dot-separated: `sync.loop.start`, `sync.loop.stop`, `sync.loop.heartbeat`, `sync.shutdown.signal_received`.
- Add `pid` + `existing_pid` attributes consistently (already present).
- When Pub/Sub and per-account sync land (issues [#8](https://github.com/kborovik/mailpilot/issues/8), [#13](https://github.com/kborovik/mailpilot/issues/13)):
  - `sync.pubsub.notification_received` (info) -- `email`, `history_id`
  - `sync.account.run` (span) -- `account_id`, `result`, `message_count`
  - `sync.account.history_fallback` (warn) -- `account_id`, `old_history_id`
  - `sync.watch.renew` (span) -- `account_id`, `expiration_ms`

### `gmail.py` (Priority 2)

Every API call wraps in a span. The retry decorator already logs warnings; add a final `logfire.exception` when retries are exhausted.

| Operation       | Span name                 | Attributes                                       |
| --------------- | ------------------------- | ------------------------------------------------ |
| Get profile     | `gmail.get_profile`       | `email`                                          |
| List messages   | `gmail.list_messages`     | `email`, `query`, `message_count`                |
| Fetch message   | `gmail.fetch_message`     | `email`, `message_id`                            |
| Send message    | `gmail.send_message`      | `email`, `message_id` (from result), `thread_id`, `result` |
| Modify message  | `gmail.modify_message`    | `email`, `message_id`, `add_labels`, `remove_labels` |
| Get history     | `gmail.get_history`       | `email`, `start_history_id`, `change_count`      |
| Watch / stop    | `gmail.watch` / `gmail.stop_watch` | `email`, `expiration_ms`                |
| Create label    | `gmail.create_label`      | `email`, `label_name`, `label_id`                |
| Retry tick      | existing `logfire.warn`   | add `operation` attribute (the wrapped func name) |

`build_gmail_service` exits on missing credentials via `SystemExit` -- add `logfire.exception` before raising so the cause is captured.

### `agent/classify.py` (Priority 2)

Classification is the highest-stakes decision in inbound flow. Instrument so every routing decision is reconstructable from traces alone.

| Event / span                  | Type    | Attributes                                                  |
| ----------------------------- | ------- | ----------------------------------------------------------- |
| `agent.classify.inbound`      | span    | `email_id`, `account_id`, `candidate_workflow_ids` (list), `chosen_workflow_id`, `confidence`, `result` |
| `agent.classify.no_match`     | info    | `email_id`, `account_id`, `candidate_workflow_ids`          |
| `agent.classify.error`        | exception | `email_id`, `account_id`                                   |

Do not log prompt contents. Log only input/output identifiers and the model's decision.

### `agent/tools.py` (Priority 2)

Pydantic AI has an opt-in Logfire integration (`logfire.instrument_pydantic_ai()`). Enable it once in `configure_logging()`; every tool call then becomes a span automatically.

Additional manual instrumentation only where auto-instrumentation is insufficient:

- Tool invocations that mutate database state (e.g. `update_contact_status`): add explicit `logfire.info("agent.tool.<name>", workflow_id=..., contact_id=..., new_status=...)`.
- Tool invocations that send external email: `logfire.info("agent.tool.send_email", workflow_id=..., email_id=..., to=...)`.

### `settings.py` (Priority 3)

- `logfire.info("config.set", key=..., changed=true)` on `config_set` when the new value differs from the old.
- Never log values for `anthropic_api_key`, `logfire_token`, service account paths -- log only the key name.

### `cli.py` (unchanged)

Remains emission-free per ADR-07. Any command-level observability comes for free through the worker modules.

## Smoke Test Plan

After Priority 1 lands, run these commands against the default `mailpilot` database with a `development` token configured:

```bash
mailpilot account create --email test@example.com --display-name Test
mailpilot account list
mailpilot account view <ID>

mailpilot company create --domain example.com --name Example
mailpilot company search example
mailpilot company list

mailpilot contact create --email alice@example.com --first-name Alice
mailpilot contact list --domain example.com
mailpilot contact view <ID>
```

Verify in Logfire:

```sql
SELECT span_name, attributes, start_timestamp
FROM records
WHERE service_name = 'mailpilot'
  AND deployment_environment = 'development'
  AND span_name LIKE 'db.%'
ORDER BY start_timestamp DESC
LIMIT 50
```

Expected: one span per CRUD operation above, each with the documented attributes.

## Prioritized Follow-Up Issues

To be filed after this plan lands:

| Priority | Scope                                           | Expected follow-up issue |
| -------- | ----------------------------------------------- | ------------------------ |
| P1       | `database.py` -- spans on every CRUD function  | `feat(observability): instrument database.py per ADR-07` |
| P1       | `sync.py` -- rename existing spans to dot-notation | `chore(observability): align sync.py span names with ADR-07` |
| P2       | `gmail.py` -- wrap every API call in a span     | `feat(observability): instrument gmail.py per ADR-07` |
| P2       | `agent/classify.py` + `agent/tools.py` -- classification and tool-call spans | Blocked on issues [#10](https://github.com/kborovik/mailpilot/issues/10), [#11](https://github.com/kborovik/mailpilot/issues/11), [#12](https://github.com/kborovik/mailpilot/issues/12) |
| P3       | `settings.py` -- `config.set` info event       | `feat(observability): emit config.set telemetry` |
| P3       | Sync pipeline spans                              | Bundled into the sync-implementation issues [#8](https://github.com/kborovik/mailpilot/issues/8), [#13](https://github.com/kborovik/mailpilot/issues/13), [#14](https://github.com/kborovik/mailpilot/issues/14) |

## Revision Log

- 2026-04-17 -- Initial plan derived from ADR-07 and Logfire MCP audit ([#19](https://github.com/kborovik/mailpilot/issues/19)).
