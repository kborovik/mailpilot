# CLAUDE Instructions

## Project Overview

AI email platform on Google Workspace with two objectives:

1. **Outbound cold email** -- discover target companies, enrich via Firecrawl + Claude, qualify via AI, find contacts via Hunter.io, verify emails via Bouncer, send and track campaigns through Gmail API.
2. **Inbound auto-reply** -- monitor incoming emails via Pub/Sub, search a knowledge base (RAG), generate and send AI-powered replies based on predefined instructions.

Both objectives operate through Gmail API with service account delegation. Each email account syncs and sends independently.

## Principles

- Technical accuracy over politeness
- Simplicity above all. YAGNI is law.
- Agent-driven, not system-driven. The system provides tools and scheduling; LLM agents make all business decisions (what to send, when to follow up, when to give up).
- Type-safety is non-negotiable. basedpyright strict mode.
- TDD for ALL changes.

## Architecture

### Gmail Integration

Gmail API with `google-api-python-client` + service account domain-wide delegation (see `docs/adr-01-gmail-api-integration.md`). Single scope: `gmail.modify`. Per-account impersonation via `credentials.with_subject(email)`. Custom headers on sent emails (`X-MailPilot-Version`, `X-MailPilot-Account-Id`).

Each account syncs independently via ThreadPoolExecutor. Pub/Sub streaming pull (`google-cloud-pubsub`) for real-time notifications. History API for incremental sync. Full re-sync on history 404.

Email body stored as plain text only (see `docs/adr-02-email-body-storage-strategy.md`).

### Workflows

Workflow is the central abstraction for both outbound campaigns and inbound auto-reply (see `docs/adr-03-workflow-model.md`). Each workflow is executed by a Pydantic AI agent with tool access. Inbound emails are routed via thread matching then LLM classification. Agent plans multi-step work via deferred tasks. See `docs/email-flow.md` for execution flows.

### CLI

The CLI must be LLM Agent friendly: JSON output only. Exit codes must be meaningful. Error messages must be actionable. The CLI is a thin dispatcher -- no domain logic or logging. All `logfire` calls belong in sync/agent modules where decisions happen. CLI only does `logfire.configure()` and `output()`.

**Lazy imports in `cli.py`.** Only `click` is imported at module level. All heavy dependencies (`logfire`, `psycopg`, `httpx`, `pydantic`, `mailpilot.database`, `mailpilot.sync`, `mailpilot.settings`) are imported inside command function bodies so that `--help` / `--version` stay fast (~50 ms). When adding or modifying CLI commands, always put `from mailpilot.*` imports inside the function, never at the top of the file. Tests must patch functions at their source module (e.g. `mailpilot.sync.func`), not at `mailpilot.cli.func`.

**Settings-first parameter passing.** CLI commands never pass config values (API keys) as separate function arguments. Instead: (1) load `Settings` via `get_settings()`, (2) pass the `Settings` instance to sync/agent functions. These functions read all config from `settings`. Only operational params (`limit`, `scope`, `on_progress`) stay as function arguments.

**Convention: GitHub CLI (`gh`) as reference.** Standard verbs: `list` (summary), `view ID` (full record), `get` (fetch from external API), `set` (update config). All IDs are UUIDv7.

```
mailpilot --version
mailpilot --debug COMMAND

mailpilot account create --email E [--display-name N]
mailpilot account list
mailpilot account view ID

mailpilot company create --domain D [--name N] [opts]
mailpilot company update ID [--name N]
mailpilot company search QUERY [--limit N]
mailpilot company list [--limit N]
mailpilot company view ID
mailpilot company export FILE
mailpilot company import FILE

mailpilot contact create --email E [--first-name F] [--last-name L] [opts]
mailpilot contact update ID [--email E] [--first-name F] [--last-name L] [opts]
mailpilot contact search QUERY [--limit N]
mailpilot contact list [--limit N] [--domain D] [--company-id ID]
mailpilot contact view ID
mailpilot contact export FILE
mailpilot contact import FILE

mailpilot workflow create --name N --type inbound|outbound --account-id ID [--objective O] [--instructions-file F]
mailpilot workflow update ID [--name N] [--objective O] [--instructions-file F]
mailpilot workflow search QUERY [--limit N]
mailpilot workflow list [--account-id ID]
mailpilot workflow view ID
mailpilot workflow activate ID
mailpilot workflow pause ID
mailpilot workflow run --workflow-id ID --contact-id ID

mailpilot email search QUERY [--limit N]
mailpilot email list [--limit N] [--contact-id ID] [--account-id ID]
mailpilot email view ID

mailpilot run

mailpilot config get [KEY]
mailpilot config set KEY VALUE

mailpilot status
```

### PostgreSQL Schema

See `src/mailpilot/schema.sql`. Requires PostgreSQL 18. Connection: `database_url` setting (default: `postgresql://localhost/mailpilot`). Schema applied automatically on first connection.

Tables: `account`, `company`, `contact`, `workflow`, `workflow_contact`, `email`, `task`.

Prefer atomic single-query operations: use `UPDATE ... FROM ... RETURNING` to join, mutate, and return in one round-trip instead of SELECT-then-UPDATE.

### Database Layer

Single flat `database.py` with `# -- Entity ---` section headers. All functions in `database.py`, no per-entity modules.

Function convention:

- `create_X(connection, ...) -> X` -- INSERT RETURNING \*, commit, return model
- `get_X(connection, id) -> X | None` -- SELECT by PK
- `list_X(connection, ...) -> list[X]` -- SELECT with optional filters
- `update_X(connection, id, **fields) -> X | None` -- dynamic SET via `_build_update()`
- `search_X(connection, query, ...) -> list[X]` -- LIKE search

All functions take `psycopg.Connection` as first arg, return domain models from `models.py` (never raw dicts). Use `Model.model_validate(row)` at the DB boundary. IDs are UUIDv7 via `_new_id()` (`uuid.uuid7()`). Dynamic SQL uses `psycopg.sql` (`SQL`, `Identifier`, `Placeholder`) -- never f-strings in queries.

### Settings

API keys and config stored in `~/.mailpilot/config.json` via `mailpilot config set KEY VALUE`:

- `anthropic_api_key` -- Anthropic Claude API key
- `anthropic_model` -- Anthropic model ID (default: `claude-sonnet-4-6`)
- `google_project_id` -- Google Cloud project ID (for Pub/Sub)
- `google_pubsub_topic` -- Pub/Sub topic name (default: `gmail-watch`)
- `google_pubsub_subscription` -- Pub/Sub subscription name (default: `mailpilot-watch`)
- `database_url` -- PostgreSQL connection (default: `postgresql://localhost/mailpilot`)
- `logfire_token` -- Pydantic Logfire token (optional)
- `logfire_environment` -- deployment environment tag. Literal: `development` | `production` (default: `development`). Tags every span sent to Logfire so traces from dev runs can be filtered out when investigating production behaviour.

## LLM-First Code Style

- Explicit, fully descriptive names (no abbreviations)
- Flat, linear code structure
- Type hints on all functions, parameters, and return values
- Docstrings on public functions (Google convention)
- Import order: stdlib, third-party, local

## Commands

```bash
make check              # lint + tests
make lint               # py-format + py-lint + py-types
make py-test            # pytest -x
make py-format          # ruff format
make py-lint            # ruff check --fix
make py-types           # basedpyright
make clean              # export data, drop tables, re-apply schema
make py-update          # upgrade venv + dependencies (uv sync --upgrade)
make py-reset           # clean __pycache__/build artifacts, rebuild venv
uv run ruff format      # format (without make)
uv run ruff check --fix # lint (without make)
uv run basedpyright     # type check (without make)
```

## GitHub

Use the `/github-*` skills for all GitHub operations -- never drive `gh` or `git push`-to-PR flows by hand. The skills encode project conventions (Conventional Commits, PR-to-issue linking via `Resolves #N`, squash-merge defaults, release-note-ready merge messages).

- `/github-issue-create` -- file an issue
- `/github-pr-create` -- open a PR from an issue number or objective
- `/github-pr-merge` -- merge a PR with a release-note-ready commit message
- `/github-commit-staged` -- commit staged changes with a descriptive message

If a GitHub operation isn't covered by a skill (e.g. reviewing comments, closing an issue manually), fall back to `gh`.

## TDD Process

1. Write failing test first
2. Implement minimal code to pass
3. Run: `uv run ruff check --fix` then `uv run basedpyright`

Tests use a separate database: `postgresql://localhost/mailpilot_test` (override with `DATABASE_URL` env var). The `database_connection` fixture truncates all tables before each test. Use `make_test_settings()` for Settings instances and `load_fixture()` for JSON fixtures -- all in `conftest.py`. HTTP mocking uses `pytest-httpx`.

## Observability

Logging and tracing use [Pydantic Logfire](https://pydantic.dev/logfire) (OpenTelemetry-based). All modules use `import logfire` directly -- no per-module logger variable.

- `logfire.debug("msg", key=value)` / `logfire.warn("msg", key=value)` for logging
- `logfire.span("name")` context manager for sync/agent stage tracing
- `configure_logging()` in `cli.py` enables console output only with `--debug` flag
- Token: `mailpilot config set logfire_token <TOKEN>` or `LOGFIRE_TOKEN` env var
- Cloud send: `send_to_logfire='if-token-present'` -- console-only when no token

**Cloud project.** All records land in Logfire project **`pilot`** (scope of the shared write token). That project holds records for two services distinguished by `service_name`: `mailpilot` (this repo) and `leadpilot` (sibling project). Within each service, spans are further split by `deployment_environment` (`development` | `production`), set from the `logfire_environment` setting. When querying via MCP, always pass `project='pilot'` and filter with `WHERE service_name = 'mailpilot' AND deployment_environment = 'production'` (or `'development'`).

**Skills for Logfire work.** Prefer these skills over ad-hoc commands:

- `/logfire:instrument` -- add Logfire instrumentation to a language/framework in this repo
- `/logfire:instrumentation` -- same, general-purpose variant (multi-language)
- `/logfire:dev-session` -- start a local dev session with write tokens for sending traces while debugging
- `/logfire:debug` -- investigate errors and debug production issues using existing traces

## Standards

ASCII-only. Use: `<-` `->` `--` `"` `(c)` `(tm)` `...`
