# CLAUDE Instructions

## Project Overview

Agent-operated CRM with Gmail as the communication layer.

MailPilot manages contacts, companies, and communication workflows through Gmail API with service account delegation. Each email account syncs and sends independently.

**Two-layer intelligence model:**

1. **Claude Code** -- strategic orchestrator. Creates workflows, assigns contacts, reviews outcomes, generates reports. Operates the system via CLI. Handles all long-running and analytical work.
2. **Internal Pydantic AI agent** -- subordinate tactical executor. Handles real-time reactive work within workflows: inbound email classification, auto-replies, follow-up scheduling. Stateless, tool-based, workflow-scoped.

Claude Code is the primary operator of MailPilot. The CLI is LLM-agent-friendly (JSON output, meaningful exit codes, actionable errors). The internal agent handles only time-sensitive work that cannot wait for a Claude Code session.

## Principles

- Technical accuracy over politeness
- Simplicity above all. YAGNI is law.
- Agent-driven, not system-driven. The system provides tools and scheduling; LLM agents make all business decisions (what to send, when to follow up, when to give up).
- Type-safety is non-negotiable. basedpyright strict mode.
- TDD for ALL changes.
- Background loops wake on events, not on timers. When a real-time mechanism exists (Pub/Sub callback, PG `LISTEN/NOTIFY`, signal handler), the main loop's `wait()` MUST be on a shared `wakeup_event` that those mechanisms set. Periodic timers are the upper-bound fallback, not the primary trigger -- otherwise the real-time path silently degrades to polling and the test suite cannot tell the difference. Clear the wakeup event BEFORE processing so events arriving mid-iteration re-trigger the next wait. See `start_sync_loop` in `src/mailpilot/sync.py` for the canonical shape.

## Architecture

### Gmail Integration

Gmail API with `google-api-python-client` + service account domain-wide delegation (see `docs/adr-01-gmail-api-integration.md`). Single scope: `gmail.modify`. Per-account impersonation via `credentials.with_subject(email)`. Custom headers on sent emails (`X-MailPilot-Version`, `X-MailPilot-Account-Id`).

Each account syncs independently via ThreadPoolExecutor. Pub/Sub streaming pull (`google-cloud-pubsub`) for real-time notifications. History API for incremental sync. Full re-sync on history 404.

Email body stored as plain text only (see `docs/adr-02-email-body-storage-strategy.md`).

### Workflows

Workflow is the central abstraction for both outbound campaigns and inbound auto-reply (see `docs/adr-03-workflow-model.md`). Each workflow is executed by a Pydantic AI agent with tool access. Inbound emails are routed via thread matching then LLM classification. Agent plans multi-step work via deferred tasks. See `docs/email-flow.md` for execution flows. See `docs/adr-08-crm-evolution.md` for the CRM evolution design.

### Email Rendering

`email_renderer.py` provides Markdown-to-HTML email rendering with inline styles and theme support. `EmailTheme` defines color palettes; six built-in themes: blue, green, orange, purple, red, slate. `THEME_NAMES` is the validation set used by CLI `--theme` options.

### CLI

The CLI must be LLM Agent friendly: JSON output only. Exit codes must be meaningful. Error messages must be actionable. The CLI is a thin dispatcher -- no domain logic or logging. All `logfire` calls belong in sync/agent modules where decisions happen. CLI only does `logfire.configure()` and `output()`.

**Lazy imports in `cli.py`.** Only `click` is imported at module level. All heavy dependencies (`logfire`, `psycopg`, `httpx`, `pydantic`, `mailpilot.database`, `mailpilot.sync`, `mailpilot.settings`) are imported inside command function bodies so that `--help` / `--version` stay fast (~50 ms). When adding or modifying CLI commands, always put `from mailpilot.*` imports inside the function, never at the top of the file. Tests must patch functions at their source module (e.g. `mailpilot.sync.func`), not at `mailpilot.cli.func`.

**Settings-first parameter passing.** CLI commands never pass config values (API keys) as separate function arguments. Instead: (1) load `Settings` via `get_settings()`, (2) pass the `Settings` instance to sync/agent functions. These functions read all config from `settings`. Only operational params (`limit`, `scope`, `on_progress`) stay as function arguments.

**Convention: GitHub CLI (`gh`) as reference.** Standard verbs: `list` (summary), `view ID` (full record), `get` (fetch from external API), `set` (update config). All IDs are UUIDv7.

**Input validation in CLI commands.** All commands validate before touching the database:

1. Required text fields reject empty/whitespace-only values: `output_error("X cannot be empty", "validation_error")` -- checked before `initialize_database()`.
2. FK references (`--contact-id`, `--company-id`, `--account-id`) are validated with `get_X()` after connection, before the main operation. Return `not_found` if the entity doesn't exist.
3. List commands with optional FK filters (`email list --contact-id`, `contact list --company-id`, etc.) validate entity existence when the filter is provided. This prevents silent empty results from typos.
4. Use `if x is not None and get_y(...) is None:` (single `if`) to avoid ruff SIM102.

```
mailpilot --version
mailpilot --debug COMMAND
mailpilot --completion bash|zsh|fish

mailpilot account create --email E [--display-name N]
mailpilot account list
mailpilot account view ID
mailpilot account update ID [--display-name N]
mailpilot account sync [--account-id ID]

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
mailpilot contact list [--limit N] [--domain D] [--company-id ID] [--status active|bounced|unsubscribed]
mailpilot contact view ID
mailpilot contact export FILE
mailpilot contact import FILE

mailpilot workflow create --name N --type inbound|outbound --account-id ID [--objective O] [--instructions TEXT | --instructions-file F] [--draft]
mailpilot workflow update ID [--name N] [--objective O] [--instructions TEXT | --instructions-file F]
mailpilot workflow search QUERY [--limit N]
mailpilot workflow list [--account-id ID] [--status draft|active|paused] [--type inbound|outbound]
mailpilot workflow view ID
mailpilot workflow start ID
mailpilot workflow stop ID
mailpilot workflow run --workflow-id ID --contact-id ID
mailpilot enrollment add --workflow-id ID --contact-id ID
mailpilot enrollment remove --workflow-id ID --contact-id ID
mailpilot enrollment view --workflow-id ID --contact-id ID
mailpilot enrollment list [--workflow-id ID] [--contact-id ID] [--status pending|active|completed|failed] [--limit N]
mailpilot enrollment update --workflow-id ID --contact-id ID --status S [--reason R]

mailpilot task list [--workflow-id ID] [--contact-id ID] [--status pending|completed|failed|cancelled] [--limit N]
mailpilot task view ID
mailpilot task cancel ID

mailpilot email search QUERY [--limit N]
mailpilot email list [--limit N] [--contact-id ID] [--account-id ID] [--since ISO] [--thread-id TEXT] [--direction inbound|outbound] [--workflow-id ID] [--status sent|received|bounced] [--from ADDR] [--to ADDR]
mailpilot email view ID
mailpilot email send --account-id ID --to E [--to E2 ...] --subject S --body B [--contact-id ID] [--workflow-id ID] [--thread-id ID] [--cc E] [--bcc E]

mailpilot activity list --contact-id ID [--type TYPE] [--limit N] [--since ISO]
mailpilot activity list --company-id ID [--type TYPE] [--limit N] [--since ISO]
mailpilot activity create --contact-id ID --type TYPE --summary TEXT [--detail JSON] [--company-id ID]

mailpilot tag add --contact-id ID NAME
mailpilot tag add --company-id ID NAME
mailpilot tag remove --contact-id ID NAME
mailpilot tag remove --company-id ID NAME
mailpilot tag list --contact-id ID
mailpilot tag list --company-id ID
mailpilot tag search NAME [--type contact|company] [--limit N]

mailpilot note add --contact-id ID --body TEXT
mailpilot note add --company-id ID --body TEXT
mailpilot note view ID
mailpilot note list --contact-id ID [--limit N] [--since ISO]
mailpilot note list --company-id ID [--limit N] [--since ISO]

mailpilot run

mailpilot config get [KEY]
mailpilot config set KEY VALUE

mailpilot status
```

### PostgreSQL Schema

See `src/mailpilot/schema.sql`. Requires PostgreSQL 18. Connection: `database_url` setting (default: `postgresql://localhost/mailpilot`). Schema applied automatically on first connection.

Tables: `account`, `company`, `contact`, `workflow`, `enrollment`, `email`, `task`, `sync_status`, `activity`, `tag`, `note`.

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

For race-safe inserts under concurrent writers, use `INSERT ... ON CONFLICT (...) DO NOTHING RETURNING *` and return `Model | None` -- `None` signals the row was inserted by a concurrent worker (see `create_email`). For bulk reads/writes, prefer `WHERE col = ANY(%s)` and `INSERT ... SELECT FROM unnest(%s::type[])` over per-row loops (see `get_contacts_by_emails` / `create_contacts_bulk`).

CRM entities (`activity`, `tag`, `note`) follow the same conventions. Activity is append-only (no update function). Tag uses ON CONFLICT DO NOTHING for idempotent creation. Note is append-only.

### CRM Model

MailPilot is a CRM with Gmail as the communication channel. Core concepts:

- **Contact** -- a person with an email address. May belong to a company.
- **Company** -- an organization identified by domain.
- **Tag** -- flexible label on contacts or companies for segmentation. Convention: lowercase, hyphenated (e.g., `prospect`, `logistics`, `cold`). No formal pipeline stages -- tags are freeform.
- **Note** -- freeform text annotation on a contact or company. Append-only.
- **Activity** -- chronological event log per contact. All significant interactions (emails, notes, tags, status changes, workflow events) are recorded as activities. This is the unified timeline Claude Code queries for relationship health and reporting.
- **Workflow** -- binds an account to agent instructions for email communication (inbound or outbound). Unchanged from prior design (see `docs/adr-03-workflow-model.md`).

### Reporting

All reports are generated by Claude Code querying the database via CLI commands. No built-in reporting engine. Report types:

- **Activity summary**: `activity list` with time filters
- **Relationship health**: `activity list` sorted by recency, detect cold contacts
- **Campaign effectiveness**: join `email` + `activity` + `tag` data
- **Pipeline snapshot**: `tag search` by stage tags (prospect, contacted, etc.)

Claude Code composes these from CLI primitives. The schema is designed for efficient querying, not for specific report formats.

### Settings

API keys and config stored in `~/.mailpilot/config.json` via `mailpilot config set KEY VALUE`:

- `anthropic_api_key` -- Anthropic Claude API key
- `anthropic_model` -- Anthropic model ID (default: `claude-sonnet-4-6`)
- `google_application_credentials` -- Path to service account JSON (falls back to `GOOGLE_APPLICATION_CREDENTIALS` env var). The file's `project_id` field is the source of truth for the GCP project.
- `google_pubsub_topic` -- Pub/Sub topic name (default: `gmail-watch`)
- `google_pubsub_subscription` -- Pub/Sub subscription name (default: `mailpilot-watch`)
- `database_url` -- PostgreSQL connection (default: `postgresql://localhost/mailpilot`)
- `logfire_token` -- Pydantic Logfire token (optional)
- `logfire_environment` -- deployment environment tag. Literal: `development` | `production` (default: `development`). Tags every span sent to Logfire so traces from dev runs can be filtered out when investigating production behaviour.
- `run_interval` -- Execution loop sleep interval in seconds (default: `30`).

### Test Accounts

Two real Google Workspace accounts are provisioned for end-to-end smoke tests against Gmail API:

- `inbound@lab5.ca` -- Inbound (receives messages, used for auto-reply flows)
- `outbound@lab5.ca` -- Outbound (sends cold email, used for campaign flows)

Both are delegated via the service account in `google_application_credentials` and can be re-created with `mailpilot account create --email ... --display-name ...` after a `make clean`.

**Full smoke test:** Use `/smoke-test` to run a phased end-to-end test that exercises the complete agent loop: entity setup, outbound email send via agent, inbound sync + routing, inbound agent reply, and round-trip verification. See `.claude/skills/smoke-test/SKILL.md`.

## LLM-First Code Style

- Explicit, fully descriptive names (no abbreviations)
- Flat, linear code structure
- Type hints on all functions, parameters, and return values
- Docstrings on public functions (Google convention)
- Import order: stdlib, third-party, local
- Python 3.14 unparenthesized `except E1, E2:` is intentional. The project pins `requires-python = ">=3.14"` and ruff is configured for `target-version = "py314"`. Do not rewrite to the parenthesized tuple form.

## Commands

```bash
make check              # lint + tests
make lint               # py-format + py-lint + py-types
make py-test            # pytest -x
make e2e                # live-Gmail smoke tests against mailpilot_e2e DB (opt-in)
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

Tests use a separate database: `postgresql://localhost/mailpilot_test` (override with `DATABASE_URL` env var). The `database_connection` fixture truncates all tables before each test. Use `make_test_settings()` for Settings instances and `load_fixture()` for JSON fixtures -- all in `conftest.py`. HTTP mocking uses `pytest-httpx`. Span-contract tests use the `capfire: CaptureLogfire` fixture from `logfire.testing` (see `tests/test_database_telemetry.py`). The `e2e` pytest marker is excluded from default runs (`addopts = "-m 'not e2e'"`); live-Gmail tests live under `tests/e2e/` and run via `make e2e` against `mailpilot_e2e`.

**Patching gotcha for entity validation.** When a CLI command calls `get_contact()`, `get_company()`, or `get_account()` for FK validation, every test for that command must patch the `get_*` function with a valid return value. Adding FK validation to an existing command will break its tests until the patches are added.

## Observability

Logging and tracing use [Pydantic Logfire](https://pydantic.dev/logfire) (OpenTelemetry-based). All modules use `import logfire` directly -- no per-module logger variable. Conventions are defined in `docs/adr-07-observability-with-logfire.md`; module-level instrumentation TODOs live in `docs/logfire-instrumentation-plan.md`.

- `logfire.debug("msg", key=value)` / `logfire.warn("msg", key=value)` for logging
- `logfire.span("name")` context manager for sync stage tracing (not in agent tools -- `instrument_pydantic_ai()` handles tool spans automatically)
- `configure_logging()` in `cli.py` enables console output only with `--debug` flag
- Token: `mailpilot config set logfire_token <TOKEN>` or `LOGFIRE_TOKEN` env var
- Cloud send: `send_to_logfire='if-token-present'` -- console-only when no token

**Cloud project.** All records land in dedicated Logfire project **`mailpilot`** (scope of the project-scoped write token). The sibling `leadpilot` service uses its own project, so no `service_name` filter is needed when querying. Spans are split by `deployment_environment` (`development` | `production`), set from the `logfire_environment` setting. When querying via MCP, always pass `project='mailpilot'` and filter with `WHERE deployment_environment = 'production'` (or `'development'`).

**Skills for Logfire work.** Prefer these skills over ad-hoc commands:

- `/logfire:instrument` -- add Logfire instrumentation to a language/framework in this repo
- `/logfire:instrumentation` -- same, general-purpose variant (multi-language)
- `/logfire:dev-session` -- start a local dev session with write tokens for sending traces while debugging
- `/logfire:debug` -- investigate errors and debug production issues using existing traces

## Standards

ASCII-only. Use: `<-` `->` `--` `"` `(c)` `(tm)` `...`
