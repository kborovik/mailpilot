# CLAUDE Instructions

Onboarding, style, and ops for this repo. **SPEC.md is authoritative** for goal
(§G), constraints (§C), interfaces (§I), invariants (§V), tasks (§T), and bugs
(§B). Do not duplicate spec content here — cite `§V.N` instead.

## What this is

Agent-operated CRM. Gmail is the comms layer. Two layers of intelligence:

- **Claude Code** — strategic orchestrator. Workflows, assignments, reviews,
  reports. Runs via CLI. Long-running, analytical work.
- **Pydantic AI agent** — subordinate tactical executor. Inbound classification,
  auto-reply, follow-up. Stateless, tool-based, workflow-scoped, time-sensitive
  only.

## Principles

- Technical accuracy over politeness.
- Simplicity above all. YAGNI is law.
- Agent-driven, not system-driven. The system provides tools and scheduling;
  LLM agents make all business decisions.
- Type-safety is non-negotiable. basedpyright strict.
- TDD for all changes.
- Background loops wake on events, not timers. Canonical: `start_sync_loop` in
  `src/mailpilot/sync.py` (§V.21).

## Code style

- Explicit, fully descriptive names. No abbreviations.
- Flat, linear structure.
- Type hints on all functions, params, and returns.
- Docstrings on public functions (Google convention).
- Import order: stdlib, third-party, local.
- Python 3.14 unparenthesized `except E1, E2:` is intentional
  (`requires-python = ">=3.14"`, ruff `target-version = "py314"`). Do not
  rewrite to tuple form.

## Architecture map

Concrete shape lives in code; spec invariants govern behaviour.

- **Gmail** (`gmail.py`, `GmailClient`) — `gmail.modify` scope only, service
  account + DWD, per-account `with_subject(email)`. Plain-text bodies. Pub/Sub
  streaming pull, History API.
- **Drive KB** (`drive.py`, `DriveClient`) — `drive.readonly` only. Folder ID in
  `workflow.instructions`. Isolation per §V.34-35.
- **Workflows** — agent shape owned by template registry (`agent/templates.py`),
  §V.44-46. File-based workflow *definitions* live in `workflows/*.toml` (private
  `kborovik/workflows` submodule, §V.103) — distinct from `.claude/workflows/*.js`
  Claude Code orchestration *scripts* (§V.73-74).
- **Email rendering** (`email_renderer.py`) — Markdown to HTML, inline styles.
  Themes per §V.92.
- **CLI** (`cli.py`) — thin dispatcher, JSON-only stdout. §V.1-5; full surface
  in §I.
- **Schema** (`schema.sql`) — PostgreSQL 18. Empty DBs auto-provision on first
  connection (data-loss-free, §V.110); a populated DB is never mutated as a
  connection side-effect — advance it explicitly via `mailpilot db init`
  (provision empty) / `mailpilot db migrate` (apply pending `migrations/`), and
  audit with `mailpilot db check`. Connection via `database_url`.
- **Database** (`database.py`) — flat module with `# -- Entity ---` headers.
  Conventions: `create_X` / `get_X` / `list_X` / `update_X` / `search_X`. Every
  fn takes `psycopg.Connection` and returns a `models.py` model. Dynamic SQL via
  `psycopg.sql`, never f-strings.
- **Settings** (`settings.py`) — `~/.mailpilot/config.json` via
  `mailpilot config set KEY VALUE`. Keys in §I.
- **Test accounts** — `outbound@lab5.ca`, `inbound@lab5.ca`, `hello@lab5.ca`.
  Re-create after `make clean`.

## Commands

```bash
make check       # lint + tests
make lint        # py-format + py-lint + py-types
make py-test     # pytest -x
make py-format   # ruff format
make py-lint     # ruff check --fix
make py-types    # basedpyright
make clean       # export, drop, re-apply schema
make py-update   # uv sync --upgrade
make py-reset    # rebuild venv
```

Use `rg` (ripgrep) over `awk` or `grep` for code search.

## TDD

1. Failing test first.
2. Minimal impl to pass.
3. `uv run ruff check --fix`, then `uv run basedpyright`.

Tests use `postgresql://localhost/mailpilot_test` (override with `DATABASE_URL`).
The `database_connection` fixture truncates tables before each test. Helpers
`make_test_settings()` and `load_fixture()` live in `conftest.py`. HTTP mocks via
`pytest-httpx`. Span-contract tests use `capfire` from `logfire.testing`.
Live-Gmail coverage goes through `/smoke-test`.

**Patching gotcha.** A CLI command that calls `get_contact()` / `get_company()`
/ `get_account()` for FK validation requires every test for that command to
patch `get_*` with a valid return. Adding FK validation to an existing command
breaks its tests until the patches are added.

## Observability

Pydantic Logfire (OTel). `import logfire` directly — no per-module logger var.
Invariants in §V.51-55.

- `logfire.debug` / `logfire.warn` for logging. `logfire.span(name)` for sync
  stage tracing; never inside agent tools (`instrument_pydantic_ai()` handles
  tool spans).
- `configure_logging()` in `cli.py` — console output only with `--debug`.
- Token via `mailpilot config set logfire_token <T>` or `LOGFIRE_TOKEN`. Cloud
  send: `send_to_logfire='if-token-present'`.
- **Operator log** (`operator_log.py`) — `operator_event(name, **fields)` writes
  one stderr line, always on. Every `logfire.exception` site reachable from
  `mailpilot run` needs a paired `operator_event("error", ...)` (§V.51).
- **Cloud project** — `mailpilot` (token-scoped). MCP queries set
  `project='mailpilot'` and filter by `deployment_environment` (§V.52).

## Help

- `/help` — Claude Code help.
- Feedback: https://github.com/anthropics/claude-code/issues
