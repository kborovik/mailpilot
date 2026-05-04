# CLAUDE Instructions

## Goal

Agent-operated CRM. Gmail = comms layer. Claude Code = strategist; internal Pydantic AI agent = tactical executor (routing, auto-reply, follow-up).

Spec is authoritative → **SPEC.md** (§G goal, §C constraints, §I interfaces, §V invariants, §T tasks, §B bugs). This file = onboarding, style, ops. ⊥ duplicate spec content here; cite `§V.N` instead.

## Two-layer intelligence

1. **Claude Code** — strategic orchestrator. Workflows, assignments, reviews, reports. Operates via CLI. Long-running ∧ analytical work.
2. **Pydantic AI agent** — subordinate tactical executor. Inbound classification, auto-reply, follow-up. Stateless, tool-based, workflow-scoped. Time-sensitive work only.

## Always-on skills

- `/sdd:spec` — sole mutator of SPEC.md.
- `/sdd:check` — drift detector, read-only.
- `/sdd:build` — plan-then-execute against SPEC.md.
- `/sdd:backprop` — bug → spec protocol.
- `/sdd:explain` — math-glyph → prose.
- `/sdd:glyph` — encoding rules for SPEC.md ∧ spec-adjacent writes (CLAUDE.md, plans, design docs).
- `/smoke-test` — end-to-end Gmail smoke (`outbound@lab5.ca` ↔ `inbound@lab5.ca`, `demo@lab5.ca` KB).
- `/gh:*` — GitHub ops (issue / pr-create / merge / release / commit / design). ⊥ drive `gh` ∨ `git push`-to-PR by hand.
- `/logfire:*` — instrument, dev-session, debug.

Default to `/sdd:*` for any spec touch — ⊥ hand-edit SPEC.md.

## Style for this file & project docs

Apply `/sdd:glyph` encoding to SPEC.md ∧ spec-adjacent writes (this file, plans, design docs). ⊥ apply to code, error strings, commit messages, PR descriptions, agent-generated email body.

- Drop articles, filler, aux verbs where fragments work.
- ⊥ hedging.
- Bullets > prose when listing >2 items.
- `→` = leads to / becomes (ASCII `->` also fine).
- `MUST` / `MUST NOT` / `MAY` over softer forms.
- One rule per line. `Why:` suffix only when non-obvious.
- Preserve verbatim: code, paths, identifiers, env vars, SQL, numbers, URLs.
- ASCII-only for project artifacts. Agent email body exempt.

## Principles

- Technical accuracy > politeness.
- Simplicity above all. YAGNI = law.
- Agent-driven, ⊥ system-driven. System provides tools ∧ scheduling; LLM agents make all business decisions.
- Type-safety non-negotiable. basedpyright strict.
- TDD ∀ changes.
- Background loops wake on events ⊥ timers. Canonical: `start_sync_loop` in `src/mailpilot/sync.py`. See §V.3.

## Architecture pointers

Concrete shape lives in code; spec invariants govern behaviour. Quick map:

- **Gmail** — `gmail.modify` only, service account + DWD, per-account `with_subject(email)`. Custom `X-MailPilot-Version` ∧ `X-MailPilot-Account-Id` headers on sent. ThreadPoolExecutor per account. Pub/Sub streaming pull. History API + 404 → full re-sync. Body = plain text only.
- **Drive KB** — `drive.readonly` only. Folder ID lives in `workflow.instructions`. Shared-Drive flags per §V.14, §V.24. Permission model = isolation per §V.17.
- **Workflows** — agent shape owned by template registry (`src/mailpilot/agent/templates.py`). See §V.32-§V.34. ⊥ per-workflow tool ∨ protocol overrides.
- **Email rendering** — `email_renderer.py` — Markdown → HTML, inline styles. `THEME_NAMES` ∈ {blue, green, orange, purple, red, slate}.
- **CLI** — thin dispatcher, JSON-only stdout. See §V.1 (settings-first), §V.2 (lazy imports), §V.4-§V.6 (envelope ∧ summary contract). Full surface in SPEC §I.
- **Schema** — `src/mailpilot/schema.sql`. PostgreSQL 18. Connection: `database_url`. Auto-applied on first connection. Tables: `account`, `company`, `contact`, `workflow`, `enrollment`, `email`, `task`, `sync_status`, `activity`, `tag`, `note`.
- **Database layer** — flat `database.py` w/ `# -- Entity ---` headers. ⊥ per-entity modules. Conventions: `create_X` / `get_X` / `list_X` / `update_X` / `search_X`. ∀ fn takes `psycopg.Connection`, returns model from `models.py` via `Model.model_validate(row)`. Dynamic SQL via `psycopg.sql` (⊥ f-strings). UUIDv7 IDs (§V.7). Race-safe inserts (§V.18). Bulk via `WHERE col = ANY(%s)` ∧ `INSERT ... SELECT FROM unnest(%s::type[])`.
- **CRM** — Contact, Company, Tag, Note, Activity, Enrollment, Workflow. XOR rules §V.8 (tag, note), append-only §V.9 (activity, note), enrollment status §V.10, activity multi-target §V.23.
- **Reporting** — Claude Code composes from CLI primitives. ⊥ built-in engine.
- **Settings** — `~/.mailpilot/config.json` via `mailpilot config set KEY VALUE`. Keys per SPEC §I.
- **Test accounts** — `outbound@lab5.ca`, `inbound@lab5.ca`, `demo@lab5.ca`. Service-account delegated. Re-create after `make clean` w/ `mailpilot account create --email ... --display-name ...`.

## LLM-First code style

- Explicit, fully descriptive names. ⊥ abbreviations.
- Flat, linear structure.
- Type hints on all fns / params / returns.
- Docstrings on public fns (Google convention).
- Import order: stdlib, third-party, local.
- Python 3.14 unparenthesized `except E1, E2:` intentional. `requires-python = ">=3.14"`, ruff `target-version = "py314"`. ⊥ rewrite to tuple form.

## Commands

```bash
make check              # lint + tests
make lint               # py-format + py-lint + py-types
make py-test            # pytest -x
make py-format          # ruff format
make py-lint            # ruff check --fix
make py-types           # basedpyright
make clean              # export, drop, re-apply schema
make py-update          # uv sync --upgrade
make py-reset           # rebuild venv
```

## TDD process

1. Failing test first.
2. Minimal impl to pass.
3. `uv run ruff check --fix` then `uv run basedpyright`.

Tests use `postgresql://localhost/mailpilot_test` (override w/ `DATABASE_URL`). `database_connection` fixture truncates tables before each test. `make_test_settings()` ∧ `load_fixture()` in `conftest.py`. HTTP mocks via `pytest-httpx`. Span-contract tests use `capfire: CaptureLogfire` from `logfire.testing` (see `tests/test_database_telemetry.py`). Live-Gmail coverage → `/smoke-test`.

**Patching gotcha.** CLI cmd calling `get_contact()` / `get_company()` / `get_account()` for FK validation → ∀ test for that cmd ! patch `get_*` w/ valid return. Adding FK validation to existing cmd breaks tests until patches added.

## Observability

Pydantic Logfire (OTel-based). `import logfire` directly — ⊥ per-module logger var. Invariants in SPEC §V.19, §V.22, §V.26.

- `logfire.debug(msg, **k)` / `logfire.warn(msg, **k)` — logging.
- `logfire.span(name)` — sync stage tracing. ⊥ in agent tools — `instrument_pydantic_ai()` handles tool spans (§V.26 → `gen_ai.tool.name`).
- `configure_logging()` in `cli.py` — console output only w/ `--debug`.
- Token: `mailpilot config set logfire_token <T>` ∨ `LOGFIRE_TOKEN` env.
- Cloud send: `send_to_logfire='if-token-present'`.

**Operator log.** `src/mailpilot/operator_log.py` → `operator_event(name, **fields)` → stderr line `HH:MM:SS event=NAME k1=v1 ...`. Always on. Curated events: `loop.start`, `loop.tick`, `loop.stop`, `pubsub.notify`, `sync.account`, `route.match`, `route.no_match`, `agent.run`, `task.drain`, `error`. ∀ new `logfire.exception` site reachable from `mailpilot run` ! paired `operator_event("error", source=<event>, message=str(exc))` per §V.19. Newlines in field values → spaces (one-line-per-event contract).

**Cloud project.** `mailpilot` (token-scoped). MCP queries ! `project='mailpilot'`, filter `WHERE deployment_environment = '<env>'` (§V.22).

## Help

- `/help` — Claude Code help.
- Feedback: https://github.com/anthropics/claude-code/issues
