# SPEC

## §G GOAL

Agent-operated CRM. Gmail = comms layer. Claude Code = strategist; internal Pydantic AI agent = tactical executor (routing, auto-reply, follow-up).

## §C CONSTRAINTS

- Python 3.14. basedpyright strict. ruff. TDD per CLAUDE.md.
- PostgreSQL 18. psycopg, raw SQL via `psycopg.sql`. no ORM.
- IDs = UUIDv7 (`uuid.uuid7()` via `_new_id()`).
- Gmail scope = `gmail.modify` only. no additional Gmail scopes.
- Drive scope = `drive.readonly` only. Read-only KB grounding. no write/modify.
- Calendar scope = `calendar.events.readonly` only. read-only event ingestion (§V.126). no event create/update from the app.
- Auth = service account & domain-wide delegation per §V.37. Credential source in {file path (`google_application_credentials` setting or `GOOGLE_APPLICATION_CREDENTIALS` env var), Application Default Credentials (e.g. GCE metadata server, Workload Identity, Cloud Run identity)}. Per-account impersonation: file creds -> `credentials.with_subject(email)`; ADC creds -> `service_account.Credentials(signer=iam.Signer(...), service_account_email=<sa>, subject=email, ...)`. no OAuth user login.
- Email body = plain text only (control chars stripped). no HTML body persistence. no embeddings, no vector store, no ingestion pipeline.
- KB files = `.md` in Drive folder named in `workflow.instructions`. no in-app PDF/Docs/HTML conversion.
- ASCII-only project artifacts (code, docs, CLI output). Agent-generated email body content exempt.
- Tool pattern: typed sig, DI deps, dict return, error dicts on failure (no raise to agent).

## §I INTERFACES

- cli: `mailpilot <noun> <verb> [args]` -> JSON on stdout, exit code 0 or 1, errors to stderr.
  - nouns: `account`, `company`, `contact`, `workflow`, `enrollment`, `task`, `email`, `meeting`, `activity`, `tag`, `note`, `template`, `db`.
  - verbs: `list|search|view|stats|create|update|disable|enable|add|remove|reply|send|start|stop|cancel|retry|run|sync|export|import|init|migrate|check`.
  - top-level: `run` (sync & task loop), `status`, `config get|set`, global `--version|--debug|--completion|--skill`.
  - envelope: `list|search|sync|export|import` -> `{"<plural>": [...], "ok": true}` (`db export|import` exception -> `{"db": {...}, "ok": true}` status object like other `db` verbs, §V.121); `view|create|update|disable|enable|add|remove|reply|send|start|stop|cancel|retry|init|migrate|check|stats` -> `{"<singular>": {...}, "ok": true}` (`workflow stats` exception -> `{"workflow_stats": {...}, "ok": true}` §V.132; `workflow check` exception -> `{"workflow_check": {...}, "ok": true}` §V.134; `task stats` exception -> `{"task_stats": {...}, "ok": true}` §V.133; aggregate not entity row); err -> `{"error": CODE, "message": TEXT, "ok": false}`. Every `ok:true` envelope also carries top-level `record_count` (int) = records displayed: array-bearing payload (`list`/`search`/`sync`/`export`/`import`) -> array len; single-object payload (single-entity verbs + aggregate `stats`/`check` + `status`) -> 1; err omits `record_count` (§V.4).
  - email projection: `email view|list` ! project `route_method` in {`classified`, `thread_match`, `rfc_message_id_match`, `skipped_outside_window`, `skipped_no_workflows`, `skipped_predates_workflows`, `skipped_no_inbound_workflows`} so operator audits routing decision from CLI w/o Logfire.
  - contact projection: `contact list|search` ! project `title` + `company_domain` (company denorm per §V.5) so operator reads role + org from CLI w/o a separate company lookup.
  - company projection: `company list` projects `contact_count` (child COUNT, LEFT JOIN contact, incl. disabled per §V.96) + filters `--max-contacts N` / `--min-contacts N` (inclusive, compose w/ `--has-profile`) so operator finds zero-contact + under-cap companies w/o the per-company N+1 probe; `company list` default-excludes disabled companies (§V.114), `--include-disabled` opts in.
  - `template` = read-only, code-defined (registry in `src/mailpilot/agent/templates.py` per §V.44). Verbs: `list [--direction inbound|outbound]`, `view NAME`. no create/update/delete — new template = code change + PR.
  - `workflow import` accepts `--file <path>`: `.toml` single-workflow catalog entry | directory (glob `*.toml`); TOML-only, no JSON, no stdin (§V.103); import enforces `name` = kebab file-stem, globally unique; def fields `{name, template, theme, goal, instructions}` import-only — `workflow update` mutates non-def fields only (status, account binding), rename = rename file + re-import (§V.103). import envelope carries top-level int `applied` + `rejected`; `applied`=0 -> `import_failed` error envelope (per-row rows inlined) + exit 1, `applied`>=1 -> ok:true w/ per-row errors inline (§V.103, §V.4). `workflow export --account-email <addr> --out-dir <dir>` writes one `*.toml`/workflow (def fields, name-sorted) + JSON status envelope on stdout (§V.103, §V.3).
  - `workflow check` (§V.134) = read-only wording-integrity check, mirrors `db check`: live 2-way SHA-256 over wording fields `{template, theme, goal, instructions}`, keyed by workflow `name` (each `workflows/*.toml` read for its `name` field, joined to rows by name); states {`in_sync`, `out_of_sync`, `not_imported`, `orphaned`}; `--file` repeatable (multi-source, last-def-wins on dup `name`); specific-file check scoped to passed names (`orphaned` suppressed), dir check flags unaccounted row `orphaned`; report-only `ok:true`, not a deploy gate; envelope `{"workflow_check": {...}, "ok": true}` (aggregate not a workflow row).
  - `workflow stats <workflow>` (§V.132) = read-only per-campaign funnel, 1 workflow by entity ref (§V.107): enrolled, sent, bounced, replied, meeting_booked, contact_later, do_not_contact, active @ enrollment grain; envelope `{"workflow_stats": {...}, "ok": true}` (aggregate not a workflow row).
  - `task stats` (§V.133) = read-only task-cadence aggregate, optional `--workflow-id` (§V.107) + `--trigger` (§V.26 taxonomy): per-status counts {pending, completed, failed, cancelled} + `total`, `distinct_scheduled_days` (bucketed by `--bucket-tz` IANA, default UTC), first/last `scheduled_at`; envelope `{"task_stats": {...}, "ok": true}` (aggregate not a task row). `task list` + `task stats` share `--trigger` (deterministic first-touch select via `enrollment_schedule` §V.32, never reads `description`).
  - account-requiring cmds (`email send|reply`, `workflow create|export|import`) take a single `--account-email` (polymorphic — email | UUID, §V.107); `account sync` makes it optional (all accounts when omitted) + accepts `--since <iso>` to bound the initial full-INBOX backfill. keyed-entity verb targets + Scope/owner options use the natural key (account email, company domain, contact email, tag name, workflow name), UUID still accepted; single-entity verb target = positional `<key>` arg, never `--<entity>-id` (§V.107).
  - CLI API standard (§V.107, §V.115, §V.116) — verb roles: Read {list, search, view, stats (read-only aggregate, §V.132 enrollment-grain | §V.133 task-grain)}; Mutate {create, add, remove, update, disable, enable} (`create` = new entity from own fields; `add`/`remove` = link/unlink to an owner (`note remove <note_id>` exception = hard-delete one note, the sole hard-delete §V.14); `disable` = soft-retire, `enable` = clear the soft-disable; no `delete`, no hard-delete §V.10/§V.114 except `note remove <note_id>` deletes one note §V.14); Lifecycle {start, stop, cancel, retry, enrollment `run`} carry per-entity state guards (`invalid_state`, §V.109); Action {send, reply, sync} = external Gmail effect; Schema/catalog {init, migrate, check, export, import}. `activity create` -> `activity add` (attaches event to a required owner). `list` filter flags = six-family taxonomy (§V.115); error envelope `error` code = closed vocabulary — domain codes {`not_found`, `invalid_state`, `missing_filter`, `already_exists`, `schema_migration_pending`, `schema_drift`, `import_failed`, ...} around the §V.54 constraint/validation half, new code = spec change.
  - `db` = schema provisioning/migration noun, off the connection hot path (§V.110). Verbs: `init` (provision empty DB — apply `schema.sql` + stamp `schema_metadata`; refuses if `account` exists, no `--force`; idempotent no-op-with-message if current), `migrate` (apply pending `migrations/NNN_*.sql` in order, each own txn, record in `schema_migrations`; no-op if none), `check` (emit report `{recorded_hash, current_hash, applied, pending, verdict}`; verdict=current -> `ok:true` exit 0; pending|drift -> `schema_migration_pending`|`schema_drift` error envelope w/ report inlined + exit 1 per §V.4/§V.109 — scriptable deploy gate), `export` (`--file <path>` writes one JSON snapshot bundle = tag vocabulary + company + contact to disk + `{"db":{...}}` status on stdout; read-only + drift-tolerant like `check`), `import` (`--file <path>` reads the bundle, restores tags -> companies -> contacts by natural key, per-row errors continue, dead-stops on drift|pending §V.109; same `{"db":{...}}` envelope). per §V.108-110, §V.121.
  - `meeting` noun (§V.125-126): rows ingested from Google Calendar by CalendarClient, NOT operator-created. verbs `list`, `view`, `add` (link an attendee contact per §V.115 link semantics), `update` (edit summary/status), `cancel` (status -> cancelled); no `create` (ingestion owns row creation), JSON envelope per §V.4.
- agent tools (`src/mailpilot/agent/tools.py`): `send_email`, `reply_email`, `search_emails`, `read_email`, `read_contact`, `read_company`, `list_enrollments`, `create_task`, `cancel_task`, `conclude_enrollment`, `disable_contact`, `list_drive_markdown`, `read_drive_markdown`, `search_drive_markdown`, `noop`. all tool -> typed sig, dict return, err dict on failure.
- pubsub (`src/mailpilot/pubsub.py`): topic `mailpilot-topic-dev`, sub `mailpilot-sub-dev` (defaults; per-env override via `MAILPILOT_GOOGLE_PUBSUB_TOPIC` / `..._SUBSCRIPTION`). `setup_pubsub()` idempotent. `start_subscriber(settings, callback)` streaming pull. `make_notification_callback(queue, wakeup_event)` decode -> enqueue -> `wakeup_event.set()`. `renew_watches()` refresh @ T-24h.
- config (`src/mailpilot/settings.py`): `database_url`, `anthropic_api_key`, `anthropic_model` (default `claude-sonnet-4-6`), `anthropic_base_url` (default `https://api.anthropic.com`; override -> Anthropic-compatible endpoint e.g. `https://api.novita.ai/anthropic`; not secret per §V.86), `anthropic_thinking` (default `adaptive`; in {unset, `adaptive`}; workflow agent only, enables extended thinking per §V.47), `anthropic_effort` (default `high`; in {unset, `low`, `medium`, `high`, `xhigh`, `max`}; workflow agent only, reasoning effort per §V.47; `xhigh` Opus 4.7+ only, errors on Sonnet 4.6), `anthropic_max_tokens` (default `32768`; int; workflow agent only, output-token budget always passed per §V.47), `google_application_credentials`, `google_pubsub_topic`, `google_pubsub_subscription`, `logfire_token`, `logfire_environment` in {`development`, `production`}, `run_interval` (default 60s), `max_concurrent_tasks` (default 10).
- module: `src/mailpilot/gmail.py` -> `GmailClient`; `src/mailpilot/drive.py` -> `DriveClient`; `src/mailpilot/calendar.py` -> `CalendarClient` (§V.126). Mirror shape.
- entrypoint: `mailpilot = "mailpilot.cli:main"`.

## §V INVARIANTS

V1: every CLI cmd loads settings first; DB + network init after (settings-first)
V2: cli.py module-level imports = click only; heavy deps lazy-import inside cmd fns
V3: stdout = strict JSON only (all flags, incl --debug); operator lifecycle + errors -> stderr; Logfire console exporter ! target stderr (ConsoleOptions output=sys.stderr), never stdout — output unset defaults stdout so console lines corrupt JSON envelope
V4: every cmd output ! match §I.cli envelope; ok:true envelope carries top-level int record_count = records displayed (array-bearing payload -> array len; single-object payload incl aggregate/status -> 1); error path -> {"error","message","ok":false} + exit 1, record_count omitted
V5: list/view rows carry parent denorm joined at fetch — → .claude/check-extras.md §V.5
V7: EmailSummary projection includes gmail_thread_id, is_routed, route_method, recipients — → .claude/check-extras.md §V.7
V8: contact/company/meeting views inline <=10 latest notes (`_INLINE_NOTES_CAP`); ContactView/CompanyView/MeetingView = base superset; field set test-tracked vs base (no silent Pydantic-strip drift); agent read_contact/read_company route through load_contact_view/load_company_view; → .claude/check-extras.md §V.8
V10: tag soft-disable — disabled_reason TEXT NULL, reversible via `tag enable`; vocabulary-tag disable/enable write no activity — → .claude/check-extras.md §V.10
V11: status payload = fixed envelope {version, schema, sync_loop, accounts, tasks, config, counts} — → .claude/check-extras.md §V.11
V12: IDs minted client-side via _new_id() -> UUIDv7; enrollment addressed by scalar id, composite (workflow_id, contact_id) retired from signatures
V13: tag + note target = XOR — exactly one of {contact_id, company_id} set (schema CHECK)
V14: activity append-only — INSERT only, no update/delete fns; note INSERT + single-note hard-delete `note remove <note_id>` (§I) — one note row per call (no bulk-clear, no note update), operator-only NOT an agent tool, trail untouched; tag/note mutation + activity row: one txn — both or neither; `note remove` deletes note row only, no activity — prior note_added rows survive as the append-only trail
V15: enrollment.status in {active, disabled}; outcomes on activity timeline via record_enrollment_outcome — → .claude/check-extras.md §V.15
V16: race-safe create — UNIQUE-bearing create_X uses ON CONFLICT DO NOTHING -> None to race loser, exactly 1 row persists; bulk variants converge to shared ids; CLI surfaces duplicate_key envelope
V17: activity targets >= 1 of {contact_id, company_id}, both allowed (multi-target); list_activities enforces same
V18: schema drift = live structure diverged from `schema.sql` w/ no migration path (manual edit | DB ahead of code), hash-mismatch primitive §V.19 — distinct from `pending` (unapplied `migrations/`, §V.108); drift response tiered per §V.109 (`status`/`db check` tolerate+report, `run`+mutations dead-stop)
V19: schema hash = sha256 over normalized schema.sql (strip -- comments, collapse whitespace)
V20: email.route_method NULL or in 7-value enum (schema CHECK, set per §I.cli); non-NULL -> is_routed=TRUE; NULL + is_routed=TRUE = pipeline ran, no match ("unrouted" = span-only label)
V21: background loops wake on events not timers — wakeup_event set by Pub/Sub notify + pg NOTIFY task_pending (INSERT + retry-UPDATE triggers); run_interval tick = fallback only
V22: <= 1 routing.route_email span lifecycle per email_id — is_routed gate; History-API re-delivery + repeat sync never trigger second route pass; → .claude/check-extras.md §V.22
V23: task drain = bounded pool <= max_concurrent_tasks; each worker owns its psycopg.Connection; atomic claim blocks re-dispatch of in-flight tasks; each worker roots its own trace — drain worker detaches the dispatching tick's sync.loop.iteration OTel context before run.execute_task (py3.14 ThreadPoolExecutor.submit propagates the active span); trace_id 1:1 w/ agent.invoke
V24: main loop never blocks on task futures — reaper collects on later ticks + emits task.drain
V25: advisory locks 2-tier — coarse (workflow_id, contact_id) + task-scoped (task_id split-half CRC32 pair); lock acquired before agent.invoke span opens (loser -> None, no span); contention -> reschedule w/o attempt_count bump, scheduled_at push fires task_pending_trigger
V26: agent.invoke span trigger attr in {enrollment_run, enrollment_schedule, task, email, manual} = caller path
V27: routing pipeline order: thread match -> RFC message-id match -> LLM classify, all account-scoped; classifier = single-turn, no tools, body truncated @ 16384 chars, hallucinated workflow_id coerced to None, zero active inbound workflows -> no LLM call; every outcome marks is_routed=TRUE w/ distinct route_method
V28: task.enrollment_id NOT NULL; workflow_id + contact_id denorm retained for filters; enrollment guaranteed @ route time via _ensure_enrollment — ON CONFLICT once, enrollment_added activity on first insert only
V29: trigger email body inlined once under "New inbound email:"; excluded from email_history — no prompt duplicate
V30: prompt framing follows trigger — first-reach-out (enrollment_run + enrollment_schedule byte-identical) vs deferred-task vs inbound; inbound email present -> email framing wins; no synthesized task_description
V31: protocol deferred branch direction-aware — outbound: trigger='task' -> terminal-outcome instruction (conclude_enrollment) | other -> initial-send-only; inbound: every trigger -> inbound-reply instruction (reply once + stop, system records outcome, never conclude_enrollment / create_task); inbound templates bind neither conclude_enrollment nor create_task
V32: enrollment_schedule = distinct trigger label (observability split from enrollment_run); --scheduled-at -> pending first-touch task (email_id NULL), idempotent, rejected for inbound workflows
V33: enrollment self-loop rejected — contact.email == workflow account email, case-insensitive
V34: every Drive call carries Shared-Drive flags — corpora="allDrives", supportsAllDrives=True, includeItemsFromAllDrives=True
V35: Drive KB isolation = per-account impersonation (DWD with_subject); account reads only files its identity can read; list/search filter mimeType text/markdown + trashed=false; content decoded UTF-8 errors=replace
V37: auth = service account + domain-wide delegation; file creds -> with_subject(email); ADC -> iam.Signer credentials w/ subject=email; no OAuth user login
V38: Drive tools registered sequential=True (shared httplib2.Http thread-unsafe); transport faults (HttpError, TimeoutError, OSError) → structured drive_unavailable dict, never bare raise; → .claude/check-extras.md §V.38
V39: agent tool failure -> error dict {error, message}, never exception to agent; agent re-drafts via tool-error path
V40: protocol fragment naming tools ! name >= 2 distinct tools, never exactly 1
V41: KB grounding rules (search-first, 2-search budget then single list, read top >= 3 hits, per-target search budget on compare) live in the workflow definition's `instructions` field (§V.103), not a code-defined template protocol fragment; inbound-google-drive template binds the Drive tool set but carries no grounding fragment
V42: email body format lint + _SPEC_TABLE inbound pipe-table mandate — → .claude/check-extras.md §V.42
V44: agent shape owned by code-defined template registry — TEMPLATES keys == WorkflowTemplateName members; WorkflowTemplate frozen; every template carries non-empty protocol + tools + description; workflow.template + type immutable post-create (update raises ValueError), type derived from template
V45: protocol composed `_BASE → [_SPEC_TABLE (inbound only) →] deferred branch → _MUST_SEND → _DECLINE → _NO_FABRICATION`, deferred branch per §V.31 (direction+trigger); every fragment email-universal OR direction-scoped, never workflow-specific; agent-facing text carries zero SPEC citation; → .claude/check-extras.md §V.45
V46: template name = <direction>-<data-system>; prefix == direction field
V47: Anthropic model config — caching flags (both calls) + model settings (workflow agent only, classifier excluded) + telemetry attribute names — → .claude/check-extras.md §V.47
V48: Anthropic HTTP timeout = 240s (4x httpx default); APITimeoutError + httpx.ReadTimeout classified terminal not transient — mid-turn tool side-effects make retry unsafe
V49: bounded auto-retry — 4 attempts, execute_task per-task classification, manual retry = failed/cancelled only — retry matrix → .claude/check-extras.md §V.49
V51: every logfire.exception site reachable from `mailpilot run` ! paired operator_event("error", source=..., message=...); contract test sweeps run-reachable modules; → .claude/check-extras.md §V.51
V52: logfire.configure(environment=settings.logfire_environment) -> spans carry deployment_environment; cloud queries filter by env
V53: agent tool spans come from logfire.instrument_pydantic_ai() (gen_ai.tool.name attr); no logfire.span inside agent tools; agents carry explicit names mailpilot.classifier + mailpilot.workflow
V54: CLI mutation = logfire.span + operator_event; constraint code vocabulary; SystemExit absorbed at boundary — → .claude/check-extras.md §V.54
V55: `gen_ai.tool.call.result` span attr exempt from Logfire scrubbing; all other attrs scrubbed; scrubbing contract test drives a real instrumented tool call, never a fabricated span
V62: /release extends /gh:release — post-tag: push main + v<x.y.z>, uv build + gh release create v<x.y.z> --verify-tag + wheel attach; confirm-before-mutate gate; version source = pyproject.toml; deploy = published wheel — → .claude/check-extras.md §V.62
V67: persisted outbound in_reply_to + references_header mirror wire MIME headers exactly
V69: tick classifying >= 1 inbound -> next tick forces full sweep + wakeup_event set
V71: per-task reply-rejection counter (reply_rejection_scope) — format_check rejections cap 3; past cap bypasses; outside scope always enforces
V72: company.profile JSONB validated vs CompanyProfile — required {summary, products, target_customers, sources} non-empty; timezone optional, null on multi-zone; malformed -> validation_error
V73: skill-body-embedded Workflow snippet runnable as authored — (a) zero free vars; (b) args-as-collection guard; (c) prose-matches-dispatch; (d) saved-workflow byte-identical — recipe → .claude/check-extras.md §V.73
V74: CSV ingestion uses RFC-4180 parser (csv module / csv.DictReader); redirect resolution = hop-agnostic curl -sL; scope .claude/skills/**; .claude/workflows/*.js excluded — recipe → .claude/check-extras.md §V.74
V75: sync incremental via History API from gmail_history_id; 404 → full INBOX re-sync; first sync (last_synced_at NULL) → full INBOX; mid-batch 429|5xx → bounded backoff retry, NEVER dropped; checkpoint advances only past persisted messages; → .claude/check-extras.md §V.75
V76: routing eligibility window — received_at older than 7 days, zero active workflows, or predates earliest active workflow -> is_routed=TRUE w/ matching skipped_* route_method, no LLM call
V77: outbound email row persists only after Gmail accepts send; orphan recovery via get_email_by_gmail_message_id on conflict — → .claude/check-extras.md §V.77
V78: outbound MIME headers (X-MailPilot-*) + thread_id threading via send path — → .claude/check-extras.md §V.78
V79: send/reply guards + account soft-disable lifecycle — → .claude/check-extras.md §V.79
V80: bounce/unsubscribe handling + contact disable; reason prefix in {bounced:, unsubscribed:} — → .claude/check-extras.md §V.80
V81: agent run ! call >= 1 tool; noop(reason) = explicit no-op escape; zero tool calls -> AgentDidNotUseToolsError
V82: agent email history scoped to (account_id, contact_id, workflow_id) — other workflows' mail excluded
V83: execute_task pre-flight cancels task when workflow inactive/missing, contact disabled/missing, enrollment missing or status != active
V84: pubsub notification callback acks unconditionally (decode error + missing emailAddress included); sets wakeup_event when supplied
V85: settings precedence kwargs > MAILPILOT_* env > ~/.mailpilot/config.json > defaults; config file auto-created on first load
V86: secret settings (anthropic_api_key, logfire_token, database_url) redacted as '***' in telemetry; config.set event logs key + changed flag
V87: cross-account isolation — thread + RFC message-id lookups scoped to account_id; agent read_email cross-account -> None (prompt-injection guard)
V88: entity enums enforced by schema CHECK — workflow.template/type/status, enrollment.status, email.direction/status/route_method, task.status, activity.type; value sets authoritative in schema.sql
V89: singleton rows — schema_metadata id=1, sync_status id='singleton'
V90: natural-key UNIQUE constraints = canonical CLI identifiers; unknown key -> not_found — → .claude/check-extras.md §V.90
V92: email render = Markdown -> HTML inline styles only, no stylesheet; THEMES = {blue, green, orange, purple, red, slate}; None/unknown theme -> blue fallback
V93: operator_event -> stderr single line "HH:MM:SS event=NAME k=v ..."; newlines collapsed to space; whitespace values double-quoted, inner quotes escaped
V94: CLI FK validation precedes mutation — referenced entity missing -> error envelope, no partial write
V95: contact lead-metadata flat cols: title TEXT, email_confidence INT; NULL = high risk; --max-email-confidence includes NULL — → .claude/check-extras.md §V.95
V96: lead-contacts discover set + negative-verdict memoization on typed reason_code — → .claude/check-extras.md §V.96
V97: lead-companies + lead-contacts run-summary completeness — batch gate capping below stale-count -> run summary carries deferred = stale-count - processed (rows left profile/contact-NULL for a follow-up run); all stale processed -> deferred 0 or omitted; bare created|enriched|seeded counts never the sole remainder signal
V98: lead-companies seed collision visibility — resolved-apex duplicate_key recorded in run-summary collapsed when incoming CSV display name diverges from the owning company's name; fires intra-batch AND onto a previously-seeded row; same-name re-seed stays silent existing; bare existing:N never the sole entity-merge signal
V99: every path `.claude/skills/**` cites resolves on disk; cited-but-absent = erroring recovery instruction — recipe → .claude/check-extras.md §V.99
V100: skill-body progressive-disclosure — live procedure inline; rarely-loaded material + sibling shared prose in `references/*.md`; body >~500 lines -> extract — recipe → .claude/check-extras.md §V.100
V101: skill-body obligation vocabulary — `.claude/skills/**` prose marks a hard requirement w/ an explicit word (`MUST` / `required`), never bare ` ! ` as must; backticked shell (`[ ! -f ]`, `!=`) + SPEC.md/telegraph register exempt. Scope = `.claude/skills/**/*.md` prose, grep ` ! ` -> zero must-sense hits — recipe → .claude/check-extras.md §V.101
V102: project-skill frontmatter hygiene — allowed-tools + argument-hint required; zero-body-use grant banned — → .claude/check-extras.md §V.102
V103: workflow defs = `workflows/*.toml`, 1 file/workflow, pure TOML (stdlib tomllib); import enforces `name` = kebab file-stem, globally unique, def fields import-only; `workflow import/export` TOML-only (! JSON, ! stdin); `workflows/` = gitignored symlink → kborovik/workflows @ /Users/kb/github/workflows (! submodule); → .claude/check-extras.md §V.103
V104: reply-test reply-loop guard — outbound@lab5.ca has no active workflow (test precondition) — → .claude/check-extras.md §V.104
V105: in-scope cases graded deterministically, false-PASS-at-worst; atomicity enforced test-time by allowlist — → .claude/check-extras.md §V.105
V106: Drive search whitespace-tokenized OR-joined fullText predicates; never whole-phrase — → .claude/check-extras.md §V.106
V107: CLI entity = natural key (§V.90) or UUID; polymorphic resolver: UUIDv7-shaped → id, else natural key; unknown key → `not_found`; single-entity target = positional `<key>` arg, never `--<entity>-id`; `--account-email` = sole account-requiring cmd option (polymorphic); → .claude/check-extras.md §V.107
V108: `migrations/NNN_*.sql` forward-only (monotonic prefix, no down-migrations); `db migrate` each own txn + re-stamps `schema_hash` on success; `schema.sql` = canonical declarative full-schema; identity invariant: fresh `db init` == apply-all-migrations-from-zero (test-enforced); → .claude/check-extras.md §V.108
V109: three-state schema verdict in {current, pending, drift} + tiered gate; run+mutations dead-stop on drift/pending — → .claude/check-extras.md §V.109
V110: initialize_database = connect + verify, not provision; empty-DB auto-provision data-loss-free; populated DB never mutates as side-effect — → .claude/check-extras.md §V.110
V111: CLI --help carries zero SPEC citation; guard walks command tree — → .claude/check-extras.md §V.111
V112: lead-companies scoped enrich-scope; domain/URL arg enriches only rows seeded this run, never global backlog — → .claude/check-extras.md §V.112
V113: Bouncer single GET /v1.1/email/verify?email= per contact; POST /email/verify/batch/sync absent — → .claude/check-extras.md §V.113
V114: company soft-disable — disabled_reason TEXT NULL, uniform disable/enable verb pairing — → .claude/check-extras.md §V.114
V115: `list` filter flag = six-family closed taxonomy {Scope, Enum, Range, Presence, Text-match, Lifecycle}; `--limit` (default 100) = sole result control; `--direction` = canonical direction axis; shared Click decorators composed fixed-order in `cli.py`/`_filters.py`; → .claude/check-extras.md §V.115
V116: tags = `tag` vocabulary table + `tag_assignment` link table; `tag add` errors `not_found` on undefined (never auto-creates); `company|contact list --tag <name>` / `--no-tag <name>` (repeatable) membership filters; → .claude/check-extras.md §V.116
V117: batch-gate option distinctness — fixed cap suppressed when stale-count <= cap — → .claude/check-extras.md §V.117
V119: destructive DB op backs up first via `mailpilot db export`; failed export aborts drop (fail-closed) — → .claude/check-extras.md §V.119
V120: trigger run MUST leave a `reply_email`|`send_email` ToolReturnPart w/o `error` key OR a successful `noop` OR `conclude_enrollment`; send-obligated = inbound (`email is not None`) OR outbound first reach-out; `_sent_reply` walker + `AgentCompletedWithoutReplyError` guard; `manual` exempt; → .claude/check-extras.md §V.120
V121: db snapshot bundle = `{schema_version, exported_at, tags, companies, contacts}` JSON; `db export`/`db import` by natural key (never source-DB UUID); drift|pending dead-stops import; export→import round-trip field-identical (test-enforced); → .claude/check-extras.md §V.121
V122: campaign-test Touch 1 delivery identified by `rfc2822_message_id` per scenario, not agent-written subject; `send_touch1.py` captures `outbound_email_id` + `rfc2822_message_id` per enrollment; `inject_replies.py` matches received Touch 1 by Message-ID (subject fallback only); single prospect `inbound@lab5.ca`, no per-sequence aliases — → .claude/check-extras.md §V.122
V123: inbound reply routing bulk-cancels enrollment's pending future follow-up tasks (excluding first-touch `enrollment_schedule` trigger); `cancel_enrollment_followup_tasks` fires from 3 sites: routing.route_email + calendar booking + conclude_enrollment; → .claude/check-extras.md §V.123
V124: workflow.goal = observable enrollment success outcome; one field, two readers: conclude_enrollment disposition gate + classify.py semantic-match key; `_DEFERRED_TASK_TASK` fragment names the goal; `record_enrollment_outcome` is system-internal; → .claude/check-extras.md §V.124
V125: meeting + meeting_attendee schema; status gates nothing; ingest owns row creation — → .claude/check-extras.md §V.125
V126: CalendarClient mirrors GmailClient/DriveClient shape; `_poll_account_calendar(connection, account)` helper fires from run-interval tick + `account sync`; idempotent upsert on google_event_id; per-account errors isolated (logged, never raised); read-only; → .claude/check-extras.md §V.126
V127: `conclude_enrollment(disposition, note, reschedule_at)` = sole agent terminal; disposition in {meeting_booked, do_not_contact, contact_later}; system side-effects per disposition; counts as send-obligation terminal (§V.120); `record_enrollment_outcome` NOT in agent tool set; → .claude/check-extras.md §V.127
V128: calendar booking concludes active outbound enrollments + cancels follow-ups, no agent turn — → .claude/check-extras.md §V.128
V129: agent-supplied timestamps: grounded (current-date injected per run via `@agent.instructions`) + future-checked at boundary (create_task + conclude_enrollment.reschedule_at reject past timestamps); guard NOT in database.create_task; → .claude/check-extras.md §V.129
V131: terminal inbound agent failure sends one `_FALLBACK_ACKNOWLEDGEMENT` fixed reply — gate: inbound `email_id` set AND reply-emitted contextvar unset; outbound first-touch stays silent; fallback-send failure logs error + marks task failed; → .claude/check-extras.md §V.131
V132: workflow stats funnel — single SQL aggregate, 8 stages, enrollment grain, envelope {workflow_stats} — → .claude/check-extras.md §V.132
V133: task stats aggregate — single SQL, task grain, envelope {task_stats}; filters --workflow-id (§V.107) + --trigger (§V.26); returns per-status {pending,completed,failed,cancelled}+total + distinct_scheduled_days + first/last scheduled_at; → .claude/check-extras.md §V.133
V134: `workflow check` = read-only wording-integrity check per §V.108 shape — SHA-256 over {template,theme,goal,instructions} keyed by `name`; states {in_sync,out_of_sync,not_imported,orphaned}; `--file` repeatable; specific-file check scoped to passed names (no `orphaned`), dir check flags `orphaned`; report-only envelope {"workflow_check"}; not a deploy gate; → .claude/check-extras.md §V.134

## §T TASKS

## archived: §T.109..§T.166 → SPEC.archive.md (53 rows)
id|status|task|cites
T167|x|impl §V.10/§V.15/§V.80/§V.114 — `enable_company`/`enable_contact`/`enable_tag`/`enable_enrollment` in database.py + cli.py verbs; contact enable clears any reason (operator-only)|V10,V15,V80,V114,B100
T168|x|collapse enrollment to {active, disabled} — drop paused; migration 004; remove `enrollment update` + toggle path|V15,V83,V108,V88
T169|x|impl §V.118(+) + §V.79(∆) — `disable_account`/`enable_account` in database.py; disabled gate in sync loop + send/reply + `account list`|V79
T170|x|drop dead `tag_disabled` activity.type — schema.sql + models.py + migration 005|V10,V17,V108
T171|x|impl §V.96(∆) + §V.116(∆) — typed `reason_code` enum; `contacts-exhausted` tag; `--no-tag` repeatable|V96,V116,B101
T172|x|impl §V.119 — `make db-backup` target; `make clean` depends on db-backup|V119
T173|x|impl §V.105(∆) — split brittle qa-in-015 token; add `_is_brittle_inscope_token` atomicity guard|V105,B102
T174|x|impl §V.120(+) — `_sent_reply` + `AgentCompletedWithoutReplyError`; inbound-trigger enforcement|V120,B103
T175|x|impl §V.45(∆) — `_MUST_SEND` fragment in templates.py; compose into protocol_post for all three templates|V45,V120
T176|x|impl §V.121(+) + §V.119(∆) — `db export`/`db import` snapshot bundle; drop per-entity export/import|V121,V119,B104
T177|x|impl §V.75(∆) — classify 429|5xx mid-batch as retained+retried; checkpoint never advances past unstored messages|V75,B105
T178|x|impl §V.120(∆) — widen send-guard to outbound first reach-out triggers|V120,B106
T179|x|impl §V.54(∆) — absorb controlled SystemExit at `cli_mutation` boundary; parent span stays clean|V54,B107
T180|x|impl §V.7(∆) + §V.122(+) — `recipients` in EmailSummary; verify_delivery.py alias-keyed delivery verification|V7,V122,B108
T181|x|impl §V.105(∆) — flip `_is_brittle_inscope_token` to allowlist; atomize qa-in-006 + qa-in-009 tokens|V105,B109
T182|x|impl §V.123(+) — `cancel_enrollment_followup_tasks`; call from routing.route_email on inbound match|V123,B110,V28,V32
T183|x|impl §V.78(∆) — expose optional `thread_id` on `send_email` agent tool; outbound thread-continuation|V78
T184|x|impl §V.124(+) + §V.103(∆) — rename `workflow.objective` → `goal`; migration 006; update classify.py + invoke.py + TOML files|V124,V103,V108
T185|x|impl §V.125(+) — `meeting` + `meeting_attendee` tables; migration 007; database.py CRUD|V125,V108
T186|x|impl §V.126(+) + §V.128(+) — CalendarClient; sync-loop poll + upsert meetings + booking-conclusion fan-out|V126,V128,V21
T187|x|impl §V.127(+) + §V.120(∆) — `conclude_enrollment` agent tool; drop `record_enrollment_outcome` from tool set|V127,V120,V15
T188|x|impl §V.125/§I — `meeting` CLI noun: list|view|add|update|cancel|V125,V126
T189|x|impl §V.126(∆) — `_poll_account_calendar` helper; wire into `account sync` per-account calendar pass|V126,V125,V128,V107
T190|x|impl §V.129(+) — date-grounding in `@agent.instructions`; past-date guard at create_task + conclude_enrollment.reschedule_at|V129,B111,V127,V32
T191|x|impl §V.8(∆) — `list_meeting_attendees`; MeetingView superset; list_meetings compact attendee summary|V8,B112,V125,V96
T192|x|impl §V.130(+) — `anthropic_thinking` + `anthropic_effort` settings; thread into `_build_anthropic_model`|V47
T193|x|impl §V.42(∆) + §V.45(∆) — extract `_SPEC_TABLE` fragment; inbound-templates-only composition|V42,V45,B114
T194|x|impl §V.130(∆) — `anthropic_max_tokens` setting; always-passed in `_build_anthropic_model`|V47,B115
T195|x|impl §V.131(+) — `_FALLBACK_ACKNOWLEDGEMENT` + reply-emitted flag; fallback send in `_handle_agent_failure` for inbound failures|V131,B116,V48,V49,V71
T196|x|impl §V.132 disposition persistence — record_enrollment_outcome disposition param writes detail.disposition; conclude_enrollment forwards disposition + booking-conclusion passes meeting_booked; JSONB key no migration|V132,V127,V128,V15
T197|x|impl §V.132 + §I — `workflow stats` funnel: single-SQL aggregation fn in database.py + CLI `workflow stats` verb + `{"workflow_stats"}` envelope|V132,V107,V4,V54
T198|x|impl §V.14(∆) + §I — `delete_notes` fn + CLI `note remove --contact-email|--company-domain` (clear an owner's notes); campaign-test setup clears prospect notes pre-Touch-1 + send_touch1 re-enables prospect between scenarios|V14,B118
T199|x|redesign §V.14(∆) — `note remove <note_id>` single-note hard-delete replaces owner bulk-clear; `delete_note(note_id)` fn replaces `delete_notes`; campaign-test reset (setup + cleanup) loops `note list` + remove-by-id via `clear_contact_notes`|V14
T200|x|impl §V.90(∆) — lowercase `email` natural key in create_contact/get_contact_by_email/create_or_get_contact_by_email/create_contacts_bulk/get_contacts_by_emails before match+insert; migration merges existing case-variant duplicate contacts onto canonical lowercase row|V90,B121
T201|x|impl §V.133(+) + §I — `task stats` single-SQL aggregate fn in database.py + CLI `task stats` verb + `{"task_stats"}` envelope; `--trigger` Enum filter on `context->>'trigger'` for `task list` + `task stats`; `--bucket-tz` day-bucketing for distinct_scheduled_days|V133,V107,V26,V32,V4,V115
T202|x|impl §V.90(∆) + §V.107(∆) — workflow joins keyed entities, natural key `name`; migration 009 + schema.sql: `workflow.name` global UNIQUE (drop `(account_id, name)` composite) + kebab CHECK, one-time normalize existing names to kebab; polymorphic resolver resolves workflow by name case-insensitively; workflow leaves keyless list|V90,V107,V108,V103
T203|x|impl §V.103(∆) — `workflow import` enforces `name` = kebab file-stem + global unique; def fields {name,template,theme,goal,instructions} import-only; `workflow update` restricted to non-def fields (status, account binding)|V103,V107,V90
T204|x|impl §V.134(+) + §I — `workflow check` verb: 2-way live SHA-256 over wording fields {template,theme,goal,instructions} keyed by workflow `name` (read each `workflows/*.toml` name field, join rows by name); states {in_sync,out_of_sync,not_imported,orphaned}; `{"workflow_check"}` envelope|V134,V103,V107,V4,V54
T205|x|impl §V.4(∆) + §I — top-level int `record_count` = records displayed on every ok:true envelope (array payload -> len; single-object -> 1); error omits; thread through cli.py output helper|V4,V3
T206|x|migrate pydantic-ai-slim[anthropic] v1→v2 — pin >=2.2.0,<3.0.0 (1.107.0 deprecation sweep: zero warnings); classify.py `Agent[object, ClassificationResult]` (v2 generic default None→object, type-only); §V.38 sequential=True per-tool barrier verified, test_agent_drive_concurrency.py unmodified; instr-format v5 renames spans `agent run`→`invoke_agent <name>` + `running tool`→`execute_tool <name>` + attr `tool_response`→`gen_ai.tool.call.result` (scrub exemption re-keyed per §B.122); run-span usage→`gen_ai.aggregated_usage.*` (own rollup spans agent.invoke + agent.classify_email unaffected)|V38,V53,V55,V47
T208|x|impl §V.31(∆) — add _DEFERRED_TASK_INBOUND fragment (reply once + stop, system records outcome, no conclude_enrollment / create_task); build_protocol direction-aware: inbound -> INBOUND every trigger, outbound -> TASK\|INITIAL by trigger; inbound templates bind _CORE minus conclude_enrollment + create_task; composed-protocol + tool-roster tests; then trim mailpilot-demo.toml "After replying" negations (workflows repo, contingent on code landing)|V31,V45,V44
T207|x|impl §V.103(∆) + §I — workflow import zero-applied loud failure: aggregate top-level int `applied` (rows w/o `error`) + `rejected` on every import envelope; `applied`=0 -> `import_failed` error envelope w/ per-row rows inlined + exit 1; `applied`>=1 -> ok:true w/ inline row errors; `record_count` = `workflows` array len @ multi-key payload|V103,V4,V3

## §B BUGS

## archived: §B.71..§B.90 → SPEC.archive.md (18 rows)
id|date|cause|fix
B91|2026-06-16|`mailpilot db --help` renders SPEC cites via Click fn docstring + option help= strings|V111
B92|2026-06-17|seed_companies.py query_stale (`company list --no-profile`) emits global profile-NULL set; domain arg enriches backlog, not scoped rows; trips >10 batch gate|V112
B93|2026-06-17|contact-finder used Bouncer POST /email/verify/batch/sync (async, returns [] cold emails); agent read [] as status=unknown -> NULL; §B.76 patched symptom not upstream cause|V113
B94|2026-06-17|ContactView omits title + email_confidence; `**contact.model_dump()` via Pydantic extra=ignore silently strips both -> `contact view` + read_contact return fewer fields than `contact list`|V8
B95|2026-06-18|lead-contacts re-dispatches a contact-finder every run for profiled companies that yield zero contacts (no row persists to memoize the empty verdict); `company list --has-profile --max-contacts 0` accumulates, re-burning Hunter/TheOrg credits|V96
B96|2026-06-18|`schema.sql` column-ordering diverges from migration 002 `ADD COLUMN` order; fresh `db init` != migrate-from-zero schema; populated DB gets `UndefinedColumn c.disabled_reason` on `company list`|V108
B97|2026-06-18|`db migrate` never re-stamped `schema_metadata.schema_hash`; post-migration verdict sees recorded≠current + 0-pending -> phantom drift + dead-stop w/ no forward path|V108
B98|2026-06-20|batch-gate `First 25` option not suppressed when stale-count <= 25; two options dispatch one identical batch; option set never gated on distinct-batch mapping|V117
B99|2026-06-20|`output_error` writes duplicate-key envelope to stderr per §V.3; `seed_companies.py` parses empty stdout -> `JSONDecodeError`; `existing` branch never fires|V3
B100|2026-06-21|company/contact/tag/enrollment disable had no enable mirror; `company update` never wired to clear `disabled_reason`; entities stuck disabled w/ no CLI recovery path|V114
B101|2026-06-22|negative-verdict memoization tagged zero-decision-maker only; all-already-seeded companies re-entered discover set + re-burned credits (§B.95 recurrence); prose-prefix reason match not a typed `reason_code`|V96,V116
B102|2026-06-22|in-scope grader false-FAILed a correct reply; multi-word column-header token rendered across two table cells; contiguous-substring match in `_grade_inscope` never hit|V105
B103|2026-06-22|agent drafted reply in `<thinking>` + emitted no `reply_email` tool call; §V.81 (>=1 tool) passed; no send-obligation guard for inbound; reply-less completion recorded as success|V120
B104|2026-06-22|`db import` dropped exported fields + forwarded source-DB UUID as `company_id`; FK resolved to no row in fresh DB -> `ForeignKeyViolation`; uncaught FK aborted whole contact batch|V121
B105|2026-06-23|batch-fetch per-sub-request 429s dropped silently to unused `failed_ids`; checkpoint advanced past unread messages -> permanent loss|V75
B106|2026-06-24|outbound first reach-out never sent; §V.120 send-guard gated inbound-only; outbound trigger bypassed guard; agent emitted email body as text w/o `send_email` call; run recorded completed|V120
B107|2026-06-24|`output_error` `SystemExit(1)` escaped `logfire.span.__exit__`; recorded as error-level exception span (`exception.escaped=True`) despite clean exit; test asserted child only, not parent span|V54
B108|2026-06-24|`verify_delivery.py` keyed delivery on agent-written subject; two sends sharing one subject collapsed to one key; second sequence counted missing; `EmailSummary` lacked recipients field|V7,V122
B109|2026-06-24|`_is_brittle_inscope_token` denylist matched `Label (Qualifier)` only; sentence-fragment tokens slipped through -> false-FAIL on restructured grounded replies (§B.102 recurrence)|V105
B110|2026-06-25|inbound reply routing left pending follow-up tasks scheduled; firing guards gate on active-only, not terminal-outcome; replied prospect could receive later cold breakup|V123
B111|2026-06-26|agent-supplied `scheduled_at` past-dated; no current-date anchor in prompt + no future check at `create_task`; already-due task fires immediately|V129
B112|2026-06-26|`get_meeting`/`list_meetings` returned bare `Meeting` rows; no `list_meeting_attendees` + no `MeetingView`; operator could write + filter attendees but never read them|V8
B113|2026-06-26|`gen_ai.usage.cache_read_input_tokens` exists on neither span; real names = `gen_ai.usage.cache_read.input_tokens` (chat) + bare `cache_read_input_tokens` (rollup); false caching-off diagnosis|V47
B114|2026-06-26|outbound agent template carried inbound pipe-table mandate from shared `_BASE`; PR #165 claimed to drop it but only the duplicate-directive consolidation shipped; mandate absent from the diff|V42,V45
B115|2026-06-26|`_build_anthropic_model` omitted explicit `max_tokens`; default-active thinking exhausted provider-default output budget before reply text -> `UnexpectedModelBehavior`|V47
B116|2026-06-26|`_handle_agent_failure` terminal branch never composed a reply; inbound sender got silent NO_REPLY on any terminal failure class (`UnexpectedModelBehavior`, max-attempts, etc.)|V131
B117|2026-06-26|§V.105 + check-extras.md named `score_replies.py` + 3-shape allowlist; `_is_brittle_inscope_token` lives in test file with 4th shape; grep hit zero -> false /sdd:check violation|V105
B118|2026-06-27|prospect notes from prior runs (do-not-contact, wrong-person) poisoned `read_contact` grounding; setup cleared re-enable but not notes; agent concluded do_not_contact + skipped Touch 1|V14
B119|2026-06-27|V122 drift: campaign-test refactored to single-prospect multi-scenario architecture after T180; `verify_delivery.py` + `inbound{N}@lab5.ca` aliases removed; V122 text described the superseded alias-keyed design|V122
B120|2026-06-27|AI Engineering not_now branch named a two-option close (create_task OR conclude_enrollment contact_later); live agent took both — concluded contact_later AND created a follow-up task; contact_later already re-enrolls so prospect double-queued|V124
B121|2026-06-29|inbound reply From local-part cased unlike enrolled (`CThorne@` vs `cthorne@`); sync sender→contact resolve keys off raw-cased address; case-sensitive `contact.email` UNIQUE misses canonical row + mints bare duplicate; `email.sender` independently lowercased so paths disagree|V90
B122|2026-07-01|pydantic-ai v2 instr-format 5 renames tool-return attr `tool_response`→`gen_ai.tool.call.result`; path-keyed scrub exemption silently dead; fabricated-span contract test asserted the hand-set old attr so stayed green|V55
B123|2026-07-01|`workflow import` emitted per-row rejections inside ok:true envelope + exit 0 @ zero rows applied; campaign-test setup gated on exit code -> silent no-op import, late name-lookup failure|V103
B124|2026-07-02|deferred-task fragments encode outbound lifecycle but compose direction-blind into inbound templates — TASK branch orders conclude_enrollment the workflow TOML forbids; INITIAL branch mislabels inbound reply as initial send|V31
