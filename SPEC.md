# SPEC

## §G GOAL

Agent-operated CRM. Gmail = comms layer. Claude Code = strategist; internal Pydantic AI agent = tactical executor (routing, auto-reply, follow-up).

## §C CONSTRAINTS

- Python 3.14. basedpyright strict. ruff. TDD per CLAUDE.md.
- PostgreSQL 18. psycopg, raw SQL via `psycopg.sql`. ⊥ ORM.
- IDs = UUIDv7 (`uuid.uuid7()` via `_new_id()`).
- Gmail scope = `gmail.modify` only. ⊥ additional Gmail scopes.
- Drive scope = `drive.readonly` only. Read-only KB grounding. ⊥ write/modify.
- Auth = service account + domain-wide delegation. Per-account impersonation via `credentials.with_subject(email)`. ⊥ OAuth user login.
- Email body = plain text only (control chars stripped). ⊥ HTML body persistence. ⊥ embeddings, ⊥ vector store, ⊥ ingestion pipeline.
- KB files = `.md` in Drive folder named in `workflow.instructions`. ⊥ in-app PDF/Docs/HTML conversion.
- ASCII-only project artifacts (code, docs, CLI output). Agent-generated email body content exempt.
- Tool pattern: typed sig, DI deps, dict return, error dicts on failure (⊥ raise to agent).

## §I INTERFACES

- cli: `mailpilot <noun> <verb> [args]` → JSON on stdout, exit code 0|1, errors to stderr.
  - nouns: `account`, `company`, `contact`, `workflow`, `enrollment`, `task`, `email`, `activity`, `tag`, `note`.
  - verbs: `list|search|view|create|update|add|remove|reply|send|start|stop|cancel|run|export|import`.
  - top-level: `run` (sync+task loop), `status`, `config get|set`, global `--version|--debug|--completion`.
  - envelope: `list|search` → `{"<plural>": [...], "ok": true}`; `view|create|update|add|reply|send|start|stop|cancel` → `{"<singular>": {...}, "ok": true}`; err → `{"error": CODE, "message": TEXT, "ok": false}`.
- agent tools (`src/mailpilot/agent/tools.py`): `send_email`, `reply_email`, `search_emails`, `read_email`, `read_contact`, `read_company`, `list_enrollments`, `create_task`, `cancel_task`, `record_enrollment_outcome`, `disable_contact`, `list_drive_markdown`, `read_drive_markdown`, `noop`. ∀ tool → typed sig, dict return, err dict on failure.
- pubsub (`src/mailpilot/pubsub.py`): topic `gmail-watch`, sub `mailpilot-watch`. `setup_pubsub()` idempotent. `start_subscriber(settings, callback)` streaming pull. `make_notification_callback(queue, wakeup_event)` decode → enqueue → `wakeup_event.set()`. `renew_watches()` refresh @ T-24h.
- config (`src/mailpilot/settings.py`): `database_url`, `anthropic_api_key`, `anthropic_model` (default `claude-sonnet-4-6`), `google_application_credentials`, `google_pubsub_topic`, `google_pubsub_subscription`, `logfire_token`, `logfire_environment` ∈ {`development`, `production`}, `run_interval` (default 30s).
- module: `src/mailpilot/gmail.py` → `GmailClient`; `src/mailpilot/drive.py` → `DriveClient`. Mirror shape.
- entrypoint: `mailpilot = "mailpilot.cli:main"`.

## §V INVARIANTS

V1: ∀ Settings consumer → receive `Settings` instance. ⊥ pass API keys / config values as separate fn args. CLI loads via `get_settings()` & forwards.
V2: `cli.py` module top-level imports → only `click`. Heavy deps (`logfire`, `psycopg`, `pydantic`, `mailpilot.*`) ! lazy inside fn bodies. Why: `--help` ~50ms.
V3: `mailpilot run` main loop ! `wait()` on shared `wakeup_event` set by Pub/Sub callback / `LISTEN/NOTIFY` / signal handlers. Clear `wakeup_event` BEFORE processing → mid-iter events re-trigger next wait. Periodic timer = upper-bound fallback only. Canonical: `start_sync_loop` in `src/mailpilot/sync.py`.
V4: ∀ JSON-yielding single-shot CLI command → stdout strict-JSON. ⊥ preceded | interleaved by operator-log lines. `operator_event(...)` → stderr always. Long-running `mailpilot run` exempt (operator console).
V5: CLI envelope shape: `list|search` → `{"<plural>": [...], "ok": true}`; `view|create|update|add|reply|send|start|stop|cancel` → `{"<singular>": {...}, "ok": true}`; ⊥ inline entity fields at top level. Use `output_entity("<singular>", model)` & `output({"<plural>": [...]})`.
V6: ∀ list|search row projects via `<Entity>Summary` — fields ⊆ {id, natural identifier, filter fields exposed as `--flag`, timestamp ordered by}. ⊥ long-text (`body_text`, `instructions`, `objective`), ⊥ JSON blobs, ⊥ variable-cardinality lists, ⊥ Gmail bookkeeping.
V7: ∀ ID = UUIDv7 via `_new_id()` (`uuid.uuid7()`). ⊥ uuid4, ⊥ serial.
V8: `tag`, `note` rows → exactly one of {`contact_id`, `company_id`} set (XOR). Enforced via schema CHECK (`schema.sql:184-187`, `schema.sql:203-206`).
V9: `activity` & `note` append-only. ⊥ update fn.
V10: `enrollment.status` ∈ {`active`, `paused`} (schema CHECK at `schema.sql:77`). Agent records terminal outcomes via `record_enrollment_outcome` → activity log; ⊥ mutate `enrollment.status` as terminal signal.
V11: `agent.invoke` span ! `trigger` attr ∈ {`enrollment_run`, `task`, `email`, `manual`}, explicit caller-passed. ⊥ heuristic inference from arg presence. Why: conflated trigger labels mask operator-initiated retries as task drains, breaking Logfire regression detection.
V12: Routing pipeline: `thread_match` (Gmail thread_id) → RFC `In-Reply-To` / `References` match → LLM `_try_classify(candidates=active inbound workflows for account)` → unrouted. Per ADR-04.
V13: Inbound task creation filter: `direction='inbound' AND workflow_id IS NOT NULL AND NOT EXISTS (task WHERE email_id=e.id)`. Why: outbound mailbox seeing own send ⊥ create task; History re-delivery idempotent.
V14: Drive list query: `mimeType='text/markdown' AND parents in '<folder_id>' AND trashed = false`. Flags ! `corpora="allDrives"` & `supportsAllDrives=True` & `includeItemsFromAllDrives=True`. `read_drive_markdown` ! `supportsAllDrives=True` on `files.get` & `files.get_media`. Why: KB folder MAY live in Shared Drive; default corpora excludes SD children → silent empty for impersonated SD member.
V15: ∀ agent tool failure → return `{"error": CODE, "message": TEXT}` dict. ⊥ raise to agent.
V16: ∀ agent run → ≥1 tool call. Decline path ! `list_drive_markdown` + `reply_email` ∴ holds.
V17: Folder access = Drive permission of impersonated user. Folder ID ∉ secrets, ∉ access grants. Multi-tenant isolation delegated to Drive permission model.
V18: Race-safe email insert: `INSERT ... ON CONFLICT (...) DO NOTHING RETURNING *` → `Model | None`. `None` = concurrent worker won. History re-delivery idempotent.
V19: ∀ new logfire.exception site reachable from `mailpilot run` → paired `operator_event("error", source=<event_name>, message=str(exc))`. Why: keeps operator stderr stream complete under journald.
V20: ∀ new agent tool → unit tests cover {hit, no-hit, error}. ∀ new CLI cmd → tests patch `get_*` FK validators.
V21: `make check` ! green (ruff + basedpyright strict + pytest).
V22: Logfire spans split by `deployment_environment` ∈ {`development`, `production`} from `logfire_environment` setting. Project = `mailpilot` (token-scoped). MCP queries ! `WHERE deployment_environment = '<env>'`.
V23: `activity` row → ≥1 of {`contact_id`, `company_id`} set; both allowed. Enforced via schema CHECK (`schema.sql:169`). Why: activity may attach to contact, company, or both (e.g., email sent to contact at a known company).

## §T TASKS

id|status|task|cites
T1|.|spec ratified — capture future work as new §T rows; backprop bugs via `/sdd:spec bug:`|-
T2|x|pair the 3 unpaired logfire.exception sites in `run.py:199` (run.sync.account_failed), `pubsub.py:203` (pubsub.notification.decode_error), `pubsub.py:286` (pubsub.watch.renewal_failed) w/ `operator_event("error", source=<event_name>, message=str(exc))`. Add span-contract tests asserting paired emission per site|V19

## §B BUGS

id|date|cause|fix
B1|2026-05-01|3 logfire.exception sites in `run.py:199`, `pubsub.py:203`, `pubsub.py:286` reachable from `mailpilot run` ⊥ paired w/ `operator_event("error", ...)` → operator stderr stream under journald has gaps where exceptions occur. Surfaced by `/sdd:check` post-rebuild on 2026-05-01|V19
