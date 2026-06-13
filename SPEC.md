# SPEC

## §G GOAL

Agent-operated CRM. Gmail = comms layer. Claude Code = strategist; internal Pydantic AI agent = tactical executor (routing, auto-reply, follow-up).

## §C CONSTRAINTS

- Python 3.14. basedpyright strict. ruff. TDD per CLAUDE.md.
- PostgreSQL 18. psycopg, raw SQL via `psycopg.sql`. no ORM.
- IDs = UUIDv7 (`uuid.uuid7()` via `_new_id()`).
- Gmail scope = `gmail.modify` only. no additional Gmail scopes.
- Drive scope = `drive.readonly` only. Read-only KB grounding. no write/modify.
- Auth = service account & domain-wide delegation per §V.37. Credential source in {file path (`google_application_credentials` setting or `GOOGLE_APPLICATION_CREDENTIALS` env var), Application Default Credentials (e.g. GCE metadata server, Workload Identity, Cloud Run identity)}. Per-account impersonation: file creds -> `credentials.with_subject(email)`; ADC creds -> `service_account.Credentials(signer=iam.Signer(...), service_account_email=<sa>, subject=email, ...)`. no OAuth user login.
- Email body = plain text only (control chars stripped). no HTML body persistence. no embeddings, no vector store, no ingestion pipeline.
- KB files = `.md` in Drive folder named in `workflow.instructions`. no in-app PDF/Docs/HTML conversion.
- ASCII-only project artifacts (code, docs, CLI output). Agent-generated email body content exempt.
- Tool pattern: typed sig, DI deps, dict return, error dicts on failure (no raise to agent).

## §I INTERFACES

- cli: `mailpilot <noun> <verb> [args]` -> JSON on stdout, exit code 0 or 1, errors to stderr.
  - nouns: `account`, `company`, `contact`, `workflow`, `enrollment`, `task`, `email`, `activity`, `tag`, `note`, `template`.
  - verbs: `list|search|view|create|update|disable|add|reply|send|start|stop|cancel|retry|run|sync|export|import`.
  - top-level: `run` (sync & task loop), `status`, `config get|set`, global `--version|--debug|--completion|--skill`.
  - envelope: `list|search|sync|export|import` -> `{"<plural>": [...], "ok": true}`; `view|create|update|disable|add|reply|send|start|stop|cancel|retry` -> `{"<singular>": {...}, "ok": true}`; err -> `{"error": CODE, "message": TEXT, "ok": false}`.
  - email projection: `email view|list` ! project `route_method` in {`classified`, `thread_match`, `rfc_message_id_match`, `skipped_outside_window`, `skipped_no_workflows`, `skipped_predates_workflows`, `skipped_no_inbound_workflows`} so operator audits routing decision from CLI w/o Logfire.
  - `template` = read-only, code-defined (registry in `src/mailpilot/agent/templates.py` per §V.44). Verbs: `list [--direction inbound|outbound]`, `view NAME`. no create/update/delete — new template = code change + PR.
- agent tools (`src/mailpilot/agent/tools.py`): `send_email`, `reply_email`, `search_emails`, `read_email`, `read_contact`, `read_company`, `list_enrollments`, `create_task`, `cancel_task`, `record_enrollment_outcome`, `disable_contact`, `list_drive_markdown`, `read_drive_markdown`, `search_drive_markdown`, `noop`. all tool -> typed sig, dict return, err dict on failure.
- pubsub (`src/mailpilot/pubsub.py`): topic `mailpilot-topic-dev`, sub `mailpilot-sub-dev` (defaults; per-env override via `MAILPILOT_GOOGLE_PUBSUB_TOPIC` / `..._SUBSCRIPTION`). `setup_pubsub()` idempotent. `start_subscriber(settings, callback)` streaming pull. `make_notification_callback(queue, wakeup_event)` decode -> enqueue -> `wakeup_event.set()`. `renew_watches()` refresh @ T-24h.
- config (`src/mailpilot/settings.py`): `database_url`, `anthropic_api_key`, `anthropic_model` (default `claude-sonnet-4-6`), `google_application_credentials`, `google_pubsub_topic`, `google_pubsub_subscription`, `logfire_token`, `logfire_environment` in {`development`, `production`}, `run_interval` (default 60s), `max_concurrent_tasks` (default 10).
- module: `src/mailpilot/gmail.py` -> `GmailClient`; `src/mailpilot/drive.py` -> `DriveClient`. Mirror shape.
- entrypoint: `mailpilot = "mailpilot.cli:main"`.

## §V INVARIANTS

V1: every CLI cmd loads settings first; DB + network init after (settings-first)
V2: cli.py module-level imports = click only; heavy deps lazy-import inside cmd fns
V3: stdout = strict JSON only (all flags, incl --debug); operator lifecycle + errors -> stderr; Logfire console exporter ! target stderr (ConsoleOptions output=sys.stderr), never stdout — output unset defaults stdout so console lines corrupt JSON envelope
V4: every cmd output ! match §I.cli envelope; error path -> {"error","message","ok":false} + exit 1
V5: list/view rows carry parent denorm joined @ fetch — workflow rows + account_email; enrollment rows + workflow_name + contact_email + contact_name
V7: EmailSummary projection ! include gmail_thread_id, is_routed, route_method — operator audits routing from CLI w/o Logfire
V8: contact/company views inline <= 10 latest notes (_INLINE_NOTES_CAP, 2 queries, no JOIN) + total count; agent read_contact/read_company route through load_contact_view/load_company_view -> agent + CLI context byte-identical; _BASE protocol carries personalize-via-notes directive
V10: tag disable = soft — disabled_reason set marks terminal row; disabled_reason IS NULL gate blocks double-disable; emits tag_disabled activity; list hides disabled unless --include-disabled
V11: status payload = fixed envelope {version, schema, sync_loop, accounts, tasks, config, counts}; tasks block carries pending, failed_24h, scheduled_future, oldest_pending_age_seconds, max_attempt_count_pending
V12: IDs minted client-side via _new_id() -> UUIDv7; enrollment addressed by scalar id, composite (workflow_id, contact_id) retired from signatures
V13: tag + note target = XOR — exactly one of {contact_id, company_id} set (schema CHECK)
V14: activity + note append-only — INSERT only, no update/delete fns
V15: enrollment.status in {active, paused, disabled}; disabled = terminal operator exit, requires non-empty disabled_reason (CHECK) + enrollment_disabled activity; outcomes live on activity timeline via record_enrollment_outcome (accepts only completed|failed), enrollment row untouched
V16: race-safe create — UNIQUE-bearing create_X uses ON CONFLICT DO NOTHING -> None to race loser, exactly 1 row persists; bulk variants converge to shared ids; CLI surfaces duplicate_key envelope
V17: activity targets >= 1 of {contact_id, company_id}, both allowed (multi-target); list_activities enforces same
V18: schema drift — metadata row/table missing or hash mismatch -> status schema.drift=true + warn, never silent
V19: schema hash = sha256 over normalized schema.sql (strip -- comments, collapse whitespace)
V20: email.route_method NULL or in 7-value enum (schema CHECK, set per §I.cli); non-NULL -> is_routed=TRUE; NULL + is_routed=TRUE = pipeline ran, no match ("unrouted" = span-only label)
V21: background loops wake on events not timers — wakeup_event set by Pub/Sub notify + pg NOTIFY task_pending (INSERT + retry-UPDATE triggers); run_interval tick = fallback only
V22: <= 1 routing.route_email span lifecycle per email_id — History-API re-delivery + repeat sync sweep never trigger second route pass (is_routed gate); duplicate route spans inflate metrics + mask classifier regressions
V23: task drain = bounded pool <= max_concurrent_tasks; each worker owns its psycopg.Connection; atomic claim blocks re-dispatch of in-flight tasks
V24: main loop never blocks on task futures — reaper collects on later ticks + emits task.drain
V25: advisory locks 2-tier — coarse (workflow_id, contact_id) + task-scoped (task_id split-half CRC32 pair); lock acquired before agent.invoke span opens (loser -> None, no span); contention -> reschedule w/o attempt_count bump, scheduled_at push fires task_pending_trigger
V26: agent.invoke span trigger attr in {enrollment_run, enrollment_schedule, task, email, manual} = caller path
V27: routing pipeline order: thread match -> RFC message-id match -> LLM classify, all account-scoped; classifier = single-turn, no tools, body truncated @ 16384 chars, hallucinated workflow_id coerced to None, zero active inbound workflows -> no LLM call; every outcome marks is_routed=TRUE w/ distinct route_method
V28: task.enrollment_id NOT NULL; workflow_id + contact_id denorm retained for filters; enrollment guaranteed @ route time via _ensure_enrollment — ON CONFLICT once, enrollment_added activity on first insert only
V29: trigger email body inlined once under "New inbound email:"; excluded from email_history — no prompt duplicate
V30: prompt framing follows trigger — first-reach-out (enrollment_run + enrollment_schedule byte-identical) vs deferred-task vs inbound; inbound email present -> email framing wins; no synthesized task_description
V31: protocol branches on trigger — trigger='task' -> terminal-outcome instruction (record_enrollment_outcome); other triggers -> initial-send-only
V32: enrollment_schedule = distinct trigger label (observability split from enrollment_run); --scheduled-at -> pending first-touch task (email_id NULL), idempotent, rejected for inbound workflows
V33: enrollment self-loop rejected — contact.email == workflow account email, case-insensitive
V34: every Drive call carries Shared-Drive flags — corpora="allDrives", supportsAllDrives=True, includeItemsFromAllDrives=True
V35: Drive KB isolation = per-account impersonation (DWD with_subject); account reads only files its identity can read; list/search filter mimeType text/markdown + trashed=false; content decoded UTF-8 errors=replace
V37: auth = service account + domain-wide delegation; file creds -> with_subject(email); ADC -> iam.Signer credentials w/ subject=email; no OAuth user login
V38: Drive tools registered sequential=True — serializes parallel dispatch (shared httplib2.Http thread-unsafe, max concurrent transport = 1); transport faults (HttpError, TimeoutError, OSError) -> structured drive_unavailable dict, never bare raise
V39: agent tool failure -> error dict {error, message}, never exception to agent; agent re-drafts via tool-error path
V40: protocol fragment naming tools ! name >= 2 distinct tools, never exactly 1
V41: KB grounding rules (search-first, 2-search budget then single list, read top >= 3 hits, per-target search budget on compare) live only in inbound-google-drive protocol fragment _DRIVE_GROUNDING
V42: outbound body format lint — >= 3 consecutive spec-shape lines (short label + whitespace + value) w/o |---| separator -> format_check rejection; ASCII rule-lines (---, ===, ___) not separators
V44: agent shape owned by code-defined template registry — TEMPLATES keys == WorkflowTemplateName members; WorkflowTemplate frozen; every template carries non-empty protocol + tools + description; workflow.template + type immutable post-create (update raises ValueError), type derived from template
V45: protocol composed in canonical fragment order _BASE -> trigger branch -> overlay? -> _DECLINE -> _NO_FABRICATION; template owns tool set + protocol, no per-workflow overrides
V46: template name = <direction>-<data-system>; prefix == direction field
V47: Anthropic calls set anthropic_cache_instructions=True + anthropic_cache_tool_definitions=True (classifier + workflow agent); agent.invoke span carries cache_read_input_tokens + cache_creation_input_tokens
V48: Anthropic HTTP timeout = 240s (4x httpx default); APITimeoutError + httpx.ReadTimeout classified terminal not transient — mid-turn tool side-effects make retry unsafe
V49: bounded auto-retry — 4 attempts total, backoff [30, 120, 300]s; transient allow-list = Google 429/5xx, Anthropic 502/503/529, socket/TimeoutError; classified per-task inside execute_task; Drive socket timeout 60s feeds classifier; manual retry only failed/cancelled (completed + pending refused); retry UPDATE fires task_pending_trigger
V51: every logfire.exception site reachable from `mailpilot run` ! paired operator_event("error", source=..., message=...); contract test sweeps run-reachable modules for logfire.exception sites — each ! operator_event("error") in same except block
V52: logfire.configure(environment=settings.logfire_environment) -> spans carry deployment_environment; cloud queries filter by env
V53: agent tool spans come from logfire.instrument_pydantic_ai() (gen_ai.tool.name attr); no logfire.span inside agent tools; agents carry explicit names mailpilot.classifier + mailpilot.workflow
V54: every CLI mutation = logfire.span("<noun>.<verb>") + paired operator_event w/ changed-field list; psycopg constraint -> envelope code {UniqueViolation: duplicate_key, ForeignKeyViolation: foreign_key_violation, NotNullViolation: not_null_violation, CheckViolation: check_violation}; other psycopg.Error -> database_error; pydantic ValidationError -> validation_error; logfire.exception + operator_event("error") fire before envelope; non-psycopg exceptions re-raise w/o envelope
V55: tool_response span attr exempt from Logfire scrubbing; all other attrs scrubbed
V57: smoke QA grading — out-of-scope decline validated mechanically vs forbidden_token_pairs + decline_signals; in-scope + compare graded vs live Drive source (source_file + source_file_alts)
V59: /demo-test = non-destructive liveness probe of public demo — outbound@lab5.ca sends one in-scope KB question to hello@lab5.ca against prod deploy; warm state assumed (no make clean / account create / import), pre-flight requires outbound account else FAIL w/o send; subject [DEMO-<HHMMSS>] <topic> fresh-randomized; PASS requires all gates: G1 reply round-trip <= 90s via direct Gmail query, G2 groundedness verdict vs live source doc per §V.57, G3 Logfire production env per §V.52 w/ required spans {agent.invoke trigger=task, gen_ai.tool.name=search_drive_markdown, gmail.send_message} + zero error/warn; output = single PASS|FAIL line + 3-bullet Logfire summary, no report file, no /sdd:spec auto-invoke
V61: reply-latency verdict derived from agent.invoke span in Logfire, CLI poll = round-trip check only; two-budget split: sla_agent_seconds gating (> 50s steady-state critical; compare-type > 90s critical, 50-90s advisory band), sla_delivery_seconds advisory (Gmail-side uncontrolled)
V62: /release extends /gh:release — post-tag: push main + v<x.y.z>, uv build asserts dist/mailpilot-<x.y.z>-py3-none-any.whl exists, gh release create v<x.y.z> --verify-tag --notes-from-tag attaches wheel; confirm-before-mutate gate covers push + upload; version source = pyproject.toml [project].version; deploy = published wheel asset — tag-only release not deployable
V63: declarative export/import — per-row errors continue batch w/ per-row error entries; upsert keyed on natural unique fields; round-trip idempotent; export order deterministic (workflows by name); import w/o --file on TTY stdin -> validation_error
V67: persisted outbound in_reply_to + references_header mirror wire MIME headers exactly
V68: pre-send fact-check — numeric tokens (>= 2 digits or decimal) in body ! appear in union of same-invocation read_drive_markdown ledger; prose-only docs admit full content; table-bearing docs admit pipe-rows + bullet-list lines only; empty ledger skips check; ledger written on successful read only; mismatch -> fact_check_mismatch w/ unsupported tokens
V69: tick classifying >= 1 inbound -> next tick forces full sweep + wakeup_event set; 10-email burst T_delivery <= 75s
V70: burst retry-rate contract — N-burst window (P <= 8, N <= 25): agent.tool_errors / agent.invoke span ratio <= 5%, measured in smoke scenario-C Logfire window [T_SEND_C, T_SEND_C+300s] against sla_agent per §V.61; breach = prompt-fidelity regression under load -> investigate §V.41 (search-first), §V.57 (KB coverage), §V.42 (format-lint sensitivity); orthogonal to §V.69 — V70 binds agent-execution quality, V69 delivery timing
V71: per-task reply-rejection counter (reply_rejection_scope) — format_check + fact_check rejections share cap 3; past cap both checks bypass; outside scope checks always enforce
V72: company.profile JSONB validated vs CompanyProfile — required {summary, products, target_customers, sources} non-empty; timezone optional, null on multi-zone; malformed -> validation_error
V73: skill-body-embedded Workflow snippet runnable as authored — every fenced js block in .claude/skills/**/*.md invoking parallel( / pipeline( / agent(: (a) zero free vars — every binding defined in snippet or source shown (CLI capture or inline literal); (b) snippet consuming args as collection guards string delivery (typeof check or JSON.parse); (c) prose concurrency claim matches snippet behavior — unchunked parallel(xs.map(...)) = all-N dispatch, never stated-N unless chunked; embedded snippet = spec-of-record — saved .claude/workflows/*.js body below meta byte-identical (saved meta may add registry-only fields)
V74: CSV ingestion uses RFC-4180 parser (csv module / csv.DictReader), never physical-line iteration or split-on-newline/comma — quoted fields carry embedded newlines + commas; plain-text non-CSV ingestion may iterate lines; redirect resolution = hop-agnostic curl -sL -w '%{url_effective}', never HEAD + location-grep (403-on-HEAD origins, trailing-CR corruption); scope = .claude/skills/** incl. scripts/*.py; .claude/workflows/*.js excluded — executability owned by §V.73
V75: sync incremental via History API from gmail_history_id checkpoint; history 404 -> full INBOX re-sync; checkpoint snapshot pre-fetch, last_synced_at post-store; message 404 mid-batch -> skip (deleted)
V76: routing eligibility window — received_at older than 7 days, zero active workflows, or predates earliest active workflow -> is_routed=TRUE w/ matching skipped_* route_method, no LLM call
V77: outbound email row persists only after Gmail accepts send — Gmail failure -> no orphan row
V78: outbound MIME stamped X-MailPilot-Version always + X-MailPilot-Account-Id when account-bound; replies set In-Reply-To + References, References defaults to in_reply_to
V79: send/reply guards — disabled contact blocks send + reply; cold-send cooldown 30 days per (account, contact, workflow); reply requires original gmail_thread_id + contact_id (typed errors); reply subject gets "Re: " prefix unless already prefixed, case-insensitive
V80: bounce handling — sender local-part in {mailer-daemon, postmaster} (case-insensitive) or label contains "BOUNCE" -> most recent outbound in same thread + account marked bounced + contact disabled w/ "bounced:" reason prefix; unsubscribe path uses "unsubscribed:" prefix
V81: agent run ! call >= 1 tool; noop(reason) = explicit no-op escape; zero tool calls -> AgentDidNotUseToolsError
V82: agent email history scoped to (account_id, contact_id, workflow_id) — other workflows' mail excluded
V83: execute_task pre-flight cancels task when workflow inactive/missing, contact disabled/missing, enrollment missing or status != active
V84: pubsub notification callback acks unconditionally (decode error + missing emailAddress included); sets wakeup_event when supplied
V85: settings precedence kwargs > MAILPILOT_* env > ~/.mailpilot/config.json > defaults; config file auto-created on first load
V86: secret settings (anthropic_api_key, logfire_token, database_url) redacted as '***' in telemetry; config.set event logs key + changed flag
V87: cross-account isolation — thread + RFC message-id lookups scoped to account_id; agent read_email cross-account -> None (prompt-injection guard)
V88: entity enums enforced by schema CHECK — workflow.template/type/status, enrollment.status, email.direction/status/route_method, task.status, activity.type; value sets authoritative in schema.sql
V89: singleton rows — schema_metadata id=1, sync_status id='singleton'
V90: natural-key UNIQUE — account.email, company.domain, contact.email, workflow(account_id, name), enrollment(workflow_id, contact_id), email.gmail_message_id nullable-unique; tag names unique per owner among active rows (partial index excludes disabled)
V91: tag/note mutation + its activity row commit in one transaction — both or neither
V92: email render = Markdown -> HTML inline styles only, no stylesheet; THEMES = {blue, green, orange, purple, red, slate}; None/unknown theme -> blue fallback
V93: operator_event -> stderr single line "HH:MM:SS event=NAME k=v ..."; newlines collapsed to space; whitespace values double-quoted, inner quotes escaped
V94: CLI FK validation precedes mutation — referenced entity missing -> error envelope, no partial write
V95: contact lead-metadata = flat columns not JSONB — contact.title TEXT NULL (role label); contact.email_confidence INT NULL, schema CHECK email_confidence BETWEEN 0 AND 100; NULL = Bouncer unknown (unbilled, no signal); email_confidence = sole email-risk score (low = high risk); no ContactProfile model
V96: lead-contacts discovery — discover set = company.profile IS NOT NULL and contact-count < 5; <= 5 contacts/company/run; admit-all — every discovered+verified email -> contact row, low/NULL email_confidence flags risk in run summary never gates admission; persistence memoizes via contact.email UNIQUE §V.90 -> idempotent re-run skips existing, no re-discovery of known-bad addresses

## §T TASKS

id|status|task|cites
T109|x|impl §V.51(+) per §B.71 — pair `database.py:151` connect-fail `logfire.exception` w/ `operator_event("error", source="database.connect", message=str(exc))`; add V51 sweep contract test enumerating run-reachable `logfire.exception` sites. Scope = grep `rg -n 'logfire\.exception' src/mailpilot/` -> each hit ! paired `operator_event("error")` in same except block (test asserts pairing). Failing test first per TDD.|V51,B71
T110|x|prerequisite (src/mailpilot): add contact.title TEXT NULL + contact.email_confidence INT NULL columns (schema.sql) w/ CHECK email_confidence BETWEEN 0 AND 100; Contact model (models.py) gains both fields; CLI contact create/update accept --title + --email-confidence; contact list --max-email-confidence N surfaces low-score rows for cross-run operator review; range CHECK + TDD per CLAUDE.md|V95
T111|x|family rename lead-encreach -> lead-companies — skill dir + frontmatter name + trigger phrases; lead-encreach-enrich.js -> lead-companies-enrich.js (meta.name in saved file and skill-body mirror byte-identical per §V.73); all intra-skill lead-encreach ref; company-profiler agent unchanged; post-rename cite-DAG sweep over `rg -n 'lead-encreach' .claude/` per §B.72 lesson|V73,B72
T112|x|new lead-contacts skill + lead-contacts-find.js workflow + contact-finder agent (sonnet) — per-company pipeline discover (Hunter Domain Search) -> org-chart (TheOrg) -> pick <= 5 decision-makers -> gap-fill (Hunter Email Finder) -> verify (Bouncer batch/sync 1 call) -> seed (mailpilot contact create); vendor keys env-only HUNTER_API_KEY/THEORG_API_KEY/BOUNCER_API_KEY (no settings.py, no telemetry); skill thin: stale-query -> batch gate -> Workflow fan-out 3 contact-finder in flight, single company -> direct Task; new workflow body byte-identical to skill-mirror per §V.73|V96,V73
T113|.|impl §V.3(+) per §B.73 — Logfire console exporter ! write stdout: configure_logging (cli.py:66) set ConsoleOptions(output=sys.stderr) so warn/debug console lines land stderr (all flags incl --debug); stdout stays JSON-only. Recurrence-guard test: warn-emitting CLI cmd (schema-drift path) -> assert stdout parses JSON and warn text in stderr not in stdout (capsys). Scope = grep `rg -n 'ConsoleOptions' src/mailpilot/` -> all hit carries output=sys.stderr. Failing test first per TDD.|V3,B73

## §B BUGS

id|date|cause|fix
B71|2026-06-12|`initialize_database` connect-fail path (`database.py:151`): logfire.exception w/o paired operator_event("error"), reachable from `mailpilot run` startup — operator stderr silent on DB-connect failure. Recurrence-class: per-site behavioral tests (run.py paths only), no whole-surface sweep so new exception site ships unpaired. Fix: §V.51(+) + §T.109.|V51
B72|2026-06-12|SPEC rebuild Phase 1 derivation sources + Phase 2 sweep (src, tests, CLAUDE.md only) excluded `.claude/skills/**` citers — 6 operative §V rows dropped, 13 cites dangled; surfaced by /sdd:check cite-DAG whole-repo scan. Fix: restore V22, V59, V62, V70, V73, V74 w/ original ids per numbering-anchor rule.|V22,V59,V62,V70,V73,V74
B73|2026-06-13|configure_logging (cli.py:66) installs Logfire console exporter w/ ConsoleOptions output unset -> defaults stdout; min_log_level="warn" (non-debug) so logfire.warn lines (e.g. database.py:175 "schema drift detected") print stdout ahead of JSON envelope, violating V3 — json.load over `mailpilot company list` stdout fails "Extra data: line 1 column 2". Recurrence-class: any logfire console-exporter / diagnostic-lib write defaulting stdout; `2>/dev/null` convention blind (pollution on stdout not stderr). Fix: §V.3(+) + §T.113.|V3
