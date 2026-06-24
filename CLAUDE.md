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
- Company and contact rows are live, paid data (discovery credits). Never drop
  or recreate the database without backing them up first (§V.119).

## Code style

- Explicit, fully descriptive names. No abbreviations.
- Flat, linear structure.
- Type hints on all functions, params, and returns.
- Docstrings on public functions (Google convention).
- Import order: stdlib, third-party, local.
- Python 3.14 unparenthesized `except E1, E2:` is intentional
  (`requires-python = ">=3.14"`, ruff `target-version = "py314"`). Do not
  rewrite to tuple form.

## Prose register (steno)

Human-facing review prose follows the `steno` skill (sdd plugin). Lead with the
fact, spell out symbols, drop idiom. Applies to GitHub issues and pull requests,
commit-message bodies, READMEs, and user-facing docs. LLM-facing writes (SPEC.md
and spec-adjacent files) use `telegraph` instead — keep the two registers apart.

- **Lead-first** — subject and verb open the sentence, at most 8 words. Qualifier
  and topic-shift clauses go to the tail.
- **Spell out symbols** — keep only `|` and `§`. Write "leads to" for `→`, "at
  least" for `≥`, "at most" for `≤`, "and" for `&`.
- **No idiom** — write the literal meaning. No metaphor, colloquialism, or
  jargon-idiom (`load-bearing`, `hand-rolled`, `low-hanging fruit`).
- **Preserve verbatim** — code, paths, URLs, identifiers, flags, numbers,
  versions, SHAs, error strings, and `#123` issue or pull-request refs.
- **Cite rides the tail** — `§V.<n>` or `§T.<n>` closes the sentence, never opens
  it. Subject and verb lead.

The Conventional Commits title prefix (`type(area):`) stays fixed — the register
applies to the body, not the subject. Full rules live in the `steno` skill.

## Clarity standard for human-facing output

These rules govern chat replies and human-facing writing — GitHub issues and
pull requests, commit-message bodies, READMEs, and user docs. SPEC.md and
spec-adjacent files use the telegraph register instead, so these rules do not
apply there.

<!-- sdd:direct-instruction:begin -->

- **Main point first.** Open each reply, paragraph, or bullet with the fact. Put
  background and qualifiers after it.
- **One idea per sentence.** Keep sentences short. Split a long sentence rather
  than trimming words from it.
- **Plain words.** Write the literal meaning. Do not use idiom (`low-hanging
  fruit`), word-level metaphor (`smell`, `bite`), colloquialism, or jargon-idiom
  (`load-bearing`, `hand-rolled`).
- **Spell out symbols.** Write `→` as "leads to", `≥` as "at least", `≤` as "at
  most", `&` as "and", and a leading `~` before a number as "about". Keep `|` for
  separators and `§` for spec citations.
- **Citation at the tail.** End a sentence with the `§V.<n>` citation; never open
  on it.
- **When the operator asks you to decide,** state the choice in one sentence,
  list each option on one line, and recommend one. Do not end with a prose "or
  keep going?" question.

<!-- sdd:direct-instruction:end -->

## Architecture map

Concrete shape lives in code; spec invariants govern behaviour.

- **Gmail** (`gmail.py`, `GmailClient`) — `gmail.modify` scope only, service
  account + DWD, per-account `with_subject(email)`. Plain-text bodies. Pub/Sub
  streaming pull, History API.
- **Drive KB** (`drive.py`, `DriveClient`) — `drive.readonly` only. Folder ID in
  `workflow.instructions`. Isolation per §V.34-35.
- **Workflows** — agent shape owned by template registry (`agent/templates.py`),
  §V.44-46. File-based workflow *definitions* live in `workflows/*.toml` (the
  independent `kborovik/workflows` repo, reached through the gitignored
  `workflows/` symlink, §V.103) — distinct from `.claude/workflows/*.js` Claude
  Code orchestration *scripts* (§V.73-74).
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

## Workflows repo (detached)

The `workflows/` directory is a gitignored symlink to the independent
`kborovik/workflows` repo at `/Users/kb/github/workflows` (§V.103). It is no
longer a git submodule, so mailpilot records no submodule pointer and does not
track the symlink.

When you edit a workflow `.toml` file through `workflows/`, commit and push the
change in the `/Users/kb/github/workflows` repo automatically. Do not wait for a
separate request. This overrides the default of committing or pushing only when
asked. There is no parent pointer to update.

## Operating friction

File a GitHub issue in this repo for every friction or error you hit operating
the mailpilot application. Capture the command, the error output, and what you
expected. One issue per distinct problem. Follow the steno register for the title
and body.

## Help

- `/help` — Claude Code help.
- Feedback: https://github.com/anthropics/claude-code/issues
