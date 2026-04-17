# ADR-07: Observability with Pydantic Logfire

## Status

Accepted

## Context

MailPilot runs headless: a long-lived sync loop, per-account Gmail pipelines, LLM agent invocations, and a CLI used both interactively and by LLM agents. When something goes wrong -- a stale history id, a delegation failure, an agent that never reached a tool, a campaign that did not progress -- the only record is whatever the code chose to emit.

Requirements:

- Structured logs and spans with queryable key-value attributes (not free-form strings)
- A single hosted destination so traces from the author's laptop, a CI run, and a production VM end up in the same place
- Separation between development noise and production signal
- Cheap to query after the fact (e.g. "show me every failed workflow run for contact X last week")
- No per-module boilerplate that contributors have to remember

The tool chosen is [Pydantic Logfire](https://pydantic.dev/logfire) (OpenTelemetry-based, SQL-queryable). This ADR codifies the conventions for using it so follow-up instrumentation work has a single source of truth.

## Decision

### Tool

Pydantic Logfire is the only observability backend. No parallel `logging` module usage, no `print` for diagnostics, no custom file log sinks. The `logfire` SDK is imported directly (`import logfire`) in every module that emits telemetry. **No per-module `logger = logging.getLogger(__name__)` variable.** This keeps logging call sites uniform (`logfire.info(...)`) and lets the SDK attach file/function/line metadata automatically.

### Project and service identity

| Attribute                | Value                                      | Source                                      |
| ------------------------ | ------------------------------------------ | ------------------------------------------- |
| Logfire project          | `mailpilot`                                | Dedicated project -- no cross-service traffic |
| `service_name`           | `mailpilot`                                | Set in [cli.py](../src/mailpilot/cli.py) `configure_logging()` |
| `deployment_environment` | `development` \| `production`              | `logfire_environment` setting (default `development`) |
| Token                    | `logfire_token` setting or `LOGFIRE_TOKEN` | Scoped to the `mailpilot` project; optional -- console-only when absent |

The project is dedicated to this service, so MCP queries and dashboards do not need a `service_name` filter. They only need `deployment_environment` to separate dev and prod traffic. The sibling `leadpilot` service uses its own project; if cross-service correlation is ever needed, query each project separately and join on shared ids in application code.

### Configuration ownership

`logfire.configure()` is called exactly once, in [cli.py](../src/mailpilot/cli.py) `configure_logging()`. Other modules never configure Logfire; they only emit. The CLI entrypoint calls `configure_logging(debug=...)` before dispatching to any command. Tests do not configure Logfire -- the SDK no-ops without configuration.

The `--debug` flag only changes the **console** minimum level (`debug` vs `warn`). It does not change what is sent to the cloud -- the cloud always receives every span and log when a token is present.

### What to emit and where

CLI stays thin: `cli.py` emits no spans and no logs beyond `configure_logging()`. All telemetry lives in the module that makes the decision or performs the I/O.

| Concern                      | Owner module            | Primitive              |
| ---------------------------- | ----------------------- | ---------------------- |
| Sync loop lifecycle          | `sync.py`               | `logfire.info` + spans |
| Database CRUD                | `database.py`           | `logfire.span`         |
| Gmail API I/O                | `gmail.py`              | `logfire.span` + `logfire.warn` on retry |
| Agent runs and tool calls    | `agent/*`               | `logfire.span` (Pydantic AI integration) |
| Classification decisions     | `agent/classify.py`     | `logfire.info` with inputs + outputs |
| Configuration changes        | `settings.py`           | `logfire.info` |
| Error paths                  | module where caught     | `logfire.exception` |

### Primitive selection

| Use                                                                 | API                           |
| ------------------------------------------------------------------- | ----------------------------- |
| Verbose state, loop tick, cache hit, heartbeat                      | `logfire.debug(msg, **kv)`    |
| Normal business events (sync started, email sent, workflow activated) | `logfire.info(msg, **kv)`   |
| Recoverable anomaly (retryable API error, stale state, skipped row) | `logfire.warn(msg, **kv)`     |
| Unrecoverable error, unexpected state                               | `logfire.exception(msg, **kv)` inside `except:` |
| Bounded unit of work                                                | `with logfire.span("name", **kv):` |
| Decorator for a whole function                                      | `@logfire.instrument("name")` |

`logfire.error` is reserved for structured error emission without an active exception (rare). `logfire.exception` is preferred inside `except` blocks because it captures the traceback automatically.

### Span naming

- `lower.dot.separated` noun phrases describing the unit of work, not the code path.
- Include the action and the subject: `account.sync`, `gmail.fetch_message`, `db.workflow.create`, `agent.classify_inbound`.
- Do not include ids or parameters in the name -- put them in attributes.
- Span names are stable strings (used as dashboard keys). Rename only with a deliberate schema change.

### Attribute conventions

Attributes are structured key-value pairs attached to spans and log events. They are what makes telemetry queryable.

**Required on every business-meaningful span:**

| Key            | When                            |
| -------------- | ------------------------------- |
| `account_id`   | Any per-account operation       |
| `workflow_id`  | Any workflow-scoped operation   |
| `contact_id`   | Any contact-scoped operation    |
| `email_id`     | Any inbound/outbound email op   |
| `result`       | Terminal spans -- `success` \| `failure` \| `skipped` |

**Naming:**

- `snake_case`
- Ids are the UUIDv7 string, not a display name
- Counts use the plural noun: `message_count`, `contact_count`
- Durations go in attribute `duration_ms` (integer milliseconds) only if Logfire's built-in span duration is insufficient
- External API status codes: `http_status`
- Boolean outcomes: `hit`, `authorized`, `retryable` (prefix-free yes/no)

**Forbidden as attributes:**

- Secrets (`anthropic_api_key`, `logfire_token`, service account JSON)
- Full email bodies (use `email_id` and query the DB if needed)
- PII beyond what is already persisted (store pointers, not raw content)

### Error handling

Errors are logged at the point they stop flowing:

```python
try:
    ...
except ApiError as exc:
    logfire.exception("gmail.fetch_message failed", email_id=email_id)
    raise
```

- Use `logfire.exception` inside `except` blocks -- it captures the traceback.
- Re-raise unless the caller cannot act on the error. Do not swallow.
- Retry loops (see `gmail.py`) emit `logfire.warn` per attempt with `attempt` and `backoff` attributes, then `logfire.exception` when retries are exhausted.

### Testing and development

- Tests do not call `logfire.configure()`. The SDK no-ops when unconfigured.
- `make check` must pass with no Logfire token.
- Local development with live traces: use `/logfire:dev-session` to obtain write credentials. The session token lands in `~/.mailpilot/config.json` via `mailpilot config set logfire_token`.
- Console output defaults to `warn`. Use `mailpilot --debug COMMAND` to see `debug` and `info` locally.

### Investigation workflow

When debugging production behavior, use the `/logfire:debug` skill, which drives the Logfire MCP. Pass `project='mailpilot'` on every call. Standard filter prefix for production queries:

```sql
WHERE deployment_environment = 'production'
```

Queries span a maximum of 14 days (MCP limit). Include a `LIMIT` clause on every `query_run` call.

## Consequences

### Positive

- One observability tool -- contributors learn one API (`logfire.*`) and one query language.
- Uniform attribute keys (`account_id`, `workflow_id`, etc.) make cross-module queries trivial ("every span touching this workflow" is a single `WHERE` clause).
- Strict separation between `cli.py` (dispatch only) and worker modules (emit telemetry) keeps concerns pinned to a single location.
- `deployment_environment` tag lets us ignore dev runs without also losing them -- the user can filter them out in production investigations.
- Hosted backend means no log-rotation, disk-pressure, or backup concerns.

### Negative

- A dedicated `mailpilot` project is a second project to provision and a second write token to rotate alongside the sibling `leadpilot` project; cross-service correlation requires two queries instead of one.
- All structured attributes must be serializable; rich objects (e.g. Pydantic models) need explicit `.model_dump()` before being attached.
- Logfire write token grants full send access -- it must not be checked into the repo (stored in `~/.mailpilot/config.json` or `LOGFIRE_TOKEN` env var).
- OpenTelemetry span export is network-bound; outbound restrictions on a deployment host would require a side-channel.

## References

- [Pydantic Logfire](https://pydantic.dev/logfire)
- [CLAUDE.md "Observability" section](../CLAUDE.md)
- [docs/logfire-instrumentation-plan.md](logfire-instrumentation-plan.md) -- living plan derived from this ADR
- `/logfire:instrument`, `/logfire:debug`, `/logfire:dev-session` skills
