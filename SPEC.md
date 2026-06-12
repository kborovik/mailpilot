# SPEC

## §G GOAL

Agent-operated CRM. Gmail = comms layer. Claude Code = strategist; internal Pydantic AI agent = tactical executor (routing, auto-reply, follow-up).

## §C CONSTRAINTS

- Python 3.14. basedpyright strict. ruff. TDD per CLAUDE.md.
- PostgreSQL 18. psycopg, raw SQL via `psycopg.sql`. ⊥ ORM.
- IDs = UUIDv7 (`uuid.uuid7()` via `_new_id()`).
- Gmail scope = `gmail.modify` only. ⊥ additional Gmail scopes.
- Drive scope = `drive.readonly` only. Read-only KB grounding. ⊥ write/modify.
- Auth = service account & domain-wide delegation per §V.37. Credential source ∈ {file path (`google_application_credentials` setting ∨ `GOOGLE_APPLICATION_CREDENTIALS` env var), Application Default Credentials (e.g. GCE metadata server, Workload Identity, Cloud Run identity)}. Per-account impersonation: file creds → `credentials.with_subject(email)`; ADC creds → `service_account.Credentials(signer=iam.Signer(...), service_account_email=<sa>, subject=email, ...)`. ⊥ OAuth user login.
- Email body = plain text only (control chars stripped). ⊥ HTML body persistence. ⊥ embeddings, ⊥ vector store, ⊥ ingestion pipeline.
- KB files = `.md` in Drive folder named in `workflow.instructions`. ⊥ in-app PDF/Docs/HTML conversion.
- ASCII-only project artifacts (code, docs, CLI output). Agent-generated email body content exempt.
- Tool pattern: typed sig, DI deps, dict return, error dicts on failure (⊥ raise to agent).

## §I INTERFACES

- cli: `mailpilot <noun> <verb> [args]` → JSON on stdout, exit code 0 ∨ 1, errors to stderr.
  - nouns: `account`, `company`, `contact`, `workflow`, `enrollment`, `task`, `email`, `activity`, `tag`, `note`, `template`.
  - verbs: `list|search|view|create|update|disable|add|reply|send|start|stop|cancel|retry|run|sync|export|import`.
  - top-level: `run` (sync & task loop), `status`, `config get|set`, global `--version|--debug|--completion|--skill`.
  - envelope: `list|search|sync|export|import` → `{"<plural>": [...], "ok": true}`; `view|create|update|disable|add|reply|send|start|stop|cancel|retry` → `{"<singular>": {...}, "ok": true}`; err → `{"error": CODE, "message": TEXT, "ok": false}`.
  - email projection: `email view|list` ! project `route_method` ∈ {`classified`, `thread_match`, `rfc_message_id_match`, `skipped_outside_window`, `skipped_no_workflows`, `skipped_predates_workflows`, `skipped_no_inbound_workflows`} ∴ operator audits routing decision from CLI w/o Logfire.
  - `template` = read-only, code-defined (registry in `src/mailpilot/agent/templates.py` per §V.44). Verbs: `list [--direction inbound|outbound]`, `view NAME`. ⊥ create/update/delete — new template = code change + PR.
- agent tools (`src/mailpilot/agent/tools.py`): `send_email`, `reply_email`, `search_emails`, `read_email`, `read_contact`, `read_company`, `list_enrollments`, `create_task`, `cancel_task`, `record_enrollment_outcome`, `disable_contact`, `list_drive_markdown`, `read_drive_markdown`, `search_drive_markdown`, `noop`. ∀ tool → typed sig, dict return, err dict on failure.
- pubsub (`src/mailpilot/pubsub.py`): topic `mailpilot-topic-dev`, sub `mailpilot-sub-dev` (defaults; per-env override via `MAILPILOT_GOOGLE_PUBSUB_TOPIC` / `..._SUBSCRIPTION`). `setup_pubsub()` idempotent. `start_subscriber(settings, callback)` streaming pull. `make_notification_callback(queue, wakeup_event)` decode → enqueue → `wakeup_event.set()`. `renew_watches()` refresh @ T-24h.
- config (`src/mailpilot/settings.py`): `database_url`, `anthropic_api_key`, `anthropic_model` (default `claude-sonnet-4-6`), `google_application_credentials`, `google_pubsub_topic`, `google_pubsub_subscription`, `logfire_token`, `logfire_environment` ∈ {`development`, `production`}, `run_interval` (default 60s), `max_concurrent_tasks` (default 10).
- module: `src/mailpilot/gmail.py` → `GmailClient`; `src/mailpilot/drive.py` → `DriveClient`. Mirror shape.
- entrypoint: `mailpilot = "mailpilot.cli:main"`.

## §V INVARIANTS

rebuild in progress -> re-derive from code per SPEC-REBUILD-PLAN.md. Old §V: `git show 98e1576:SPEC.md`.

## §T TASKS

## archived: §T.1..§T.108 → SPEC.archive.md (108 rows)

## §B BUGS

## archived: §B.1..§B.70 → SPEC.archive.md (64 rows)
