# Logfire Instrumentation Plan

Living plan derived from [ADR-07](adr-07-observability-with-logfire.md). Tracks what is instrumented today, what is missing, and in what order the gaps should be closed. Update this doc -- not the ADR -- as the picture changes.

## Current Coverage (2026-04-22)

Result of querying `project='mailpilot'` in Logfire and grepping `logfire.` across `src/mailpilot/`:

| Module                                                  | Current emissions                                                                                                | Gap                                                                                       |
| ------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| [cli.py](../src/mailpilot/cli.py)                       | `logfire.configure()` only                                                                                       | Correct by design -- CLI stays thin                                                       |
| [run.py](../src/mailpilot/run.py)                       | `run.loop.start/stop`, `run.loop.iteration` (span), `run.execute_task` (span), `run.task.lock_held/agent_failed` | Missing: rollup attributes on `run.loop.iteration`                                        |
| [sync.py](../src/mailpilot/sync.py)                     | `sync.account.run` (span), `sync.account.message_stored` (debug), `sync.account.resolve_contacts` (span), `sync.send_email` (span) | Missing: `fetched_count`/`stored_count` on sync span; `gmail message sent` uses old naming |
| [gmail.py](../src/mailpilot/gmail.py)                   | `warn` on retryable HTTP error; `info` `gmail message sent` (old naming)                                         | **Missing: spans around every API call** -- biggest blind spot (#27)                      |
| [settings.py](../src/mailpilot/settings.py)             | No emissions                                                                                                     | Missing: `info` on `config set` when values change                                        |
| [database.py](../src/mailpilot/database.py)             | **Fully instrumented.** 30+ `db.*` span types covering all CRUD operations                                       | Noisy: 500+ spans/iteration on hot-path reads (#30)                                       |
| [agent/invoke.py](../src/mailpilot/agent/invoke.py)     | `agent.invoke` (span) with `result`, `agent_reasoning`, `tool_calls` attributes                                  | None                                                                                      |
| [agent/classify.py](../src/mailpilot/agent/classify.py) | No emissions                                                                                                     | Classification decisions must be traced with inputs + outputs                              |
| [agent/tools.py](../src/mailpilot/agent/tools.py)       | **Fully instrumented.** `agent.tool.*` spans on every tool call                                                  | None                                                                                      |
| [exceptions.py](../src/mailpilot/exceptions.py)         | N/A -- definitions only                                                                                          | Callers must emit `exception` when raising these                                           |

## Logfire MCP Audit (2026-04-22)

Queried against the dedicated `mailpilot` project. 56 distinct span names in development environment.

### Span inventory by module

| Module       | Span count | Span names (sample)                                                         |
| ------------ | ---------- | --------------------------------------------------------------------------- |
| `db.*`       | 30         | `db.email.create`, `db.account.list`, `db.task.complete`, ...               |
| `sync.*`     | 4          | `sync.account.run`, `sync.send_email`, `sync.account.message_stored`, ...   |
| `run.*`      | 5          | `run.loop.iteration`, `run.execute_task`, `run.task.lock_held`, ...         |
| `agent.*`    | 8          | `agent.invoke`, `agent.tool.send_email`, `agent.tool.read_contact`, ...     |
| `routing.*`  | 1          | `routing.route_email`                                                       |
| `gmail*`     | 1          | `gmail message sent` (info log, old naming -- not a span)                   |

### Observations

1. **Gmail API is the biggest blind spot.** `sync.account.run` takes 0.4-12.8s depending on message volume. DB spans inside total ~100ms. The remaining 95%+ is Gmail API round-trips (`list_messages`, `get_message`) with zero span coverage. Issue #27.
2. **DB spans are noisy.** 500+ DB spans per loop iteration with 2 accounts. Hot-path reads (`get_by_gmail_message_id`, `get_by_email`) dominate. Issue #30.
3. **`run.loop.iteration` has no rollup attributes.** Cannot answer "how many messages stored per iteration?" without scanning children. Issue #28.
4. **`sync.account.run` still uses `result=success|failure`.** Redundant with OTel span status. Issue #32.
5. **`gmail message sent` is the only non-dot-separated span name.** Legacy from pre-ADR-07. Should become `gmail.send_message`.
6. **`agent/classify.py` still uninstrumented.** Classification decisions are invisible in traces.
7. **Zero production records.** Only `development` environment has data.

## Module-by-Module Proposed Instrumentation

### `database.py` -- DONE (landed in PRs #20, #26)

All CRUD functions have `logfire.span("db.<entity>.<op>")` wrappers. 30+ distinct span types confirmed in Logfire. See issue #30 for follow-up work to reduce hot-path read span noise.

### `sync.py` + `run.py` -- PARTIALLY DONE

Dot-separated naming is in place. `sync.account.run`, `run.loop.iteration`, `run.execute_task` all emit spans. Remaining gaps:

- `sync.account.run` missing `fetched_count`, `stored_count`, `duplicate_skipped_count` attributes (issue #28)
- `run.loop.iteration` missing rollup attributes (`account_count`, `task_count`, `total_stored`) (issue #28)
- `result=success|failure` on `sync.account.run` is redundant with OTel span status (issue #32)
- Pub/Sub and watch renewal spans deferred until those features land (issues #8, #13)

### `gmail.py` (Priority 2)

Every API call wraps in a span. The retry decorator already logs warnings; add a final `logfire.exception` when retries are exhausted.

| Operation      | Span name                          | Attributes                                                 |
| -------------- | ---------------------------------- | ---------------------------------------------------------- |
| Get profile    | `gmail.get_profile`                | `email`                                                    |
| List messages  | `gmail.list_messages`              | `email`, `query`, `message_count`                          |
| Fetch message  | `gmail.fetch_message`              | `email`, `message_id`                                      |
| Send message   | `gmail.send_message`               | `email`, `message_id` (from result), `thread_id`, `result` |
| Modify message | `gmail.modify_message`             | `email`, `message_id`, `add_labels`, `remove_labels`       |
| Get history    | `gmail.get_history`                | `email`, `start_history_id`, `change_count`                |
| Watch / stop   | `gmail.watch` / `gmail.stop_watch` | `email`, `expiration_ms`                                   |
| Create label   | `gmail.create_label`               | `email`, `label_name`, `label_id`                          |
| Retry tick     | existing `logfire.warn`            | add `operation` attribute (the wrapped func name)          |

`build_gmail_service` exits on missing credentials via `SystemExit` -- add `logfire.exception` before raising so the cause is captured.

### `agent/classify.py` (Priority 2)

Classification is the highest-stakes decision in inbound flow. Instrument so every routing decision is reconstructable from traces alone.

| Event / span              | Type      | Attributes                                                                                              |
| ------------------------- | --------- | ------------------------------------------------------------------------------------------------------- |
| `agent.classify.inbound`  | span      | `email_id`, `account_id`, `candidate_workflow_ids` (list), `chosen_workflow_id`, `confidence`, `result` |
| `agent.classify.no_match` | info      | `email_id`, `account_id`, `candidate_workflow_ids`                                                      |
| `agent.classify.error`    | exception | `email_id`, `account_id`                                                                                |

Do not log prompt contents. Log only input/output identifiers and the model's decision.

### `agent/invoke.py` + `agent/tools.py` -- DONE (landed in PRs #26, #56)

All 9 agent tools have `logfire.span("agent.tool.*")` wrappers. `agent.invoke` span carries `result`, `agent_reasoning`, `tool_calls` attributes. Agent reasoning is also persisted to `task.result` JSONB column.

### `agent/classify.py` (Priority 2)

Still uninstrumented. Classification decisions are invisible in traces. When inbound email routing triggers LLM classification, there is no span recording the candidate workflows, chosen workflow, or confidence. Issue not yet filed -- bundle with next observability PR.

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

Verify in Logfire (`project='mailpilot'`):

```sql
SELECT span_name, attributes, start_timestamp
FROM records
WHERE deployment_environment = 'development'
  AND span_name LIKE 'db.%'
ORDER BY start_timestamp DESC
LIMIT 50
```

Expected: one span per CRUD operation above, each with the documented attributes.

## Prioritized Follow-Up Issues

| Priority | Issue | Scope | Status |
| -------- | ----- | ----- | ------ |
| ~~P1~~   | ~~#20, #26~~ | ~~`database.py` -- spans on every CRUD function~~ | **DONE** |
| ~~P1~~   | ~~#26~~ | ~~`sync.py` -- rename spans to dot-notation~~ | **DONE** |
| ~~P2~~   | ~~#56~~ | ~~`agent/tools.py` -- tool call spans~~ | **DONE** |
| P1       | [#27](https://github.com/kborovik/mailpilot/issues/27) | `gmail.py` -- wrap every API call in a span | Open -- biggest blind spot |
| P2       | [#28](https://github.com/kborovik/mailpilot/issues/28) | Enrich `sync.account.run` + `run.loop.iteration` attributes | Open |
| P2       | [#29](https://github.com/kborovik/mailpilot/issues/29) | `gmail.retry.exhausted` error log | Open |
| P2       | [#30](https://github.com/kborovik/mailpilot/issues/30) | Reduce DB span noise on hot-path reads | Open |
| P2       | [#33](https://github.com/kborovik/mailpilot/issues/33) | `trace_id` in CLI error output | Open |
| P3       | [#32](https://github.com/kborovik/mailpilot/issues/32) | Remove redundant `result` attribute from sync spans | Open |
| P3       | [#31](https://github.com/kborovik/mailpilot/issues/31) | OpenTelemetry metrics for sync throughput | Open |
| P3       | (not filed) | `settings.py` -- `config.set` info event | Deferred |
| P3       | (not filed) | `agent/classify.py` -- classification decision tracing | Deferred |

## Revision Log

- 2026-04-17 -- Initial plan derived from ADR-07 and Logfire MCP audit ([#19](https://github.com/kborovik/mailpilot/issues/19)).
- 2026-04-17 -- Migrated from shared `pilot` project to dedicated `mailpilot` project. MCP access to the new project confirmed (empty as expected). `service_name` filter no longer required in queries.
- 2026-04-22 -- Updated coverage table and audit. `database.py`, `agent/tools.py`, `agent/invoke.py`, `sync.py`, `run.py` now instrumented. Gmail API spans remain the biggest gap. Updated all open issues (#27-#33) with current Logfire data.
