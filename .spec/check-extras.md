# check-extras — audit recipes (REPO-LOCAL)
# Resolved by check-mechanical emit-v-slices when SPEC row stubs
# `→ .spec/check-extras.md §Vn`.
# Migrated from .claude/check-extras.md + condense prong-6 extracts.

## SKILL.md Drift Check

Mechanical audit (no LLM-judgment); trigger when `src/mailpilot/SKILL.md`, `.grok/skills/**/*.md`, `src/mailpilot/cli`, or `src/mailpilot/settings.py` changed.

File-set scope:
- `src/mailpilot/SKILL.md` — packaged skill body (external LLM agents); all four checks apply.
- `.grok/skills/**/*.md` — operator-facing skill bodies (campaign-test, reply-test, prompt-audit, github-resolve-issue); per `§B.65` only checks (i) and (ii) apply (skill bodies do not enumerate settings so (iii) and (iv) do not apply).

Checks:
(i) per-noun verb roster is a superset of `@<noun>.command("<verb>")` set in `cli/` — fail mode: skill names a retired verb (e.g. `enrollment remove` post-T92).
(ii) per-verb `--<flag>` tokens in recipes are a subset of `@click.option("--<flag>")` set for that handler in `cli/`.
(iii) settings key list in `## Settings` == `Settings.model_fields` keys in `settings.py` — `src/mailpilot/SKILL.md` only.
(iv) `MAILPILOT_DATABASE_URL` is the only env settings source named in `## Settings`; other `MAILPILOT_*` keys are described as not sources — `src/mailpilot/SKILL.md` only.

## I.nouns / I.verbs set-diff

Mechanical. SPEC §I list-shape vs cli/ registrations.
Hook `.spec/scripts/check-extras.sh` (audit probe); stdout rows `id|verdict|evidence` (no header).

I.nouns: parse `- nouns:` backtick list in SPEC.md §I.
Code nouns = `@main.group()` names minus {show, config}.
I.verbs: parse `- verbs:` pipe-list in SPEC.md §I.
Code verbs = `@<noun>.command` names on those noun groups only
(top-level show.queue / config.get excluded by construction).

Verdicts: MATCH (equal) / MISSING (spec − code) / EXTRA (code − spec) / DRIFT (both nonempty).
Trigger: SPEC.md or src/mailpilot/cli changed.
Distinct from SKILL.md Drift Check (skill-body vs cli/).

## Recipe grep-runner — emit-rg

`.spec/scripts/check-extras.sh emit-rg` parses every backticked `rg` command
under each `## §Vn` header, runs it, and emits `section|line|hit_count|files`
(no header). Prose expectations (`-> present`, `-> zero hits`) stay
operator-judged — the runner collapses the execute step, not the verdict.
No-arg extras-hook path stays I.nouns / I.verbs set-diff.

## Flipped-§T pytest verify

`/sdd:build` verify and `/sdd:check` flipped-since-clean re-verify share one
shape: collect test files once, run one `uv run pytest`. Do not re-issue the
same selector list after format-only or lint-only follow-ups unless
`tests/**` changed.

Trigger: `/sdd:build` verify step, or check audit
`tasks|ADVISORY|flipped-since-clean` non-empty.
- `git diff --name-only <last_clean_sha> -- tests/` -> test files in the
  flip (check memo `last_clean_sha`; build uses the §T working tree)
- `uv run pytest <files> -q` -> pass = HOLD / verify pass; fail = STALE /
  verify FAIL
- same selector after ruff format or basedpyright only -> skip re-run

## §V4 — CLI envelope + record_count

Every cmd output MUST match the §I.cli envelope. ok:true envelope carries top-level int `record_count` = records displayed: array-bearing payload (`list`/`search`/`sync`/`export`/`import`) -> array len; single-object payload (single-entity verbs + aggregate `stats`/`check` + `status`) -> 1; `show queue` JSON -> len(rows) not 1. Error path -> `{"error", "message", "ok": false}` + exit 1; `record_count` omitted on error. Envelope key vocabulary per §I.cli (plural for arrays, singular for single-object; `workflow_stats`/`task_stats`/`workflow_check`/`db`/`queue` aggregate exceptions).

Trigger: `src/mailpilot/cli` changed.
- `rg 'record_count' src/mailpilot/cli` -> output helper stamps record_count on every ok:true envelope
- `rg 'output_error' src/mailpilot/cli | head -3` -> error helper present ({"error","message","ok":false} + exit 1)

## §V5 — parent denorm on list/view rows

`workflow` rows carry `account_email`; `enrollment` rows carry `workflow_name` + `contact_email` + `contact_name`; `contact` list/search rows carry `company_domain` (LEFT JOIN company ON company_id, NULL when company_id NULL). The `company_domain` join backs the `--company-domain` Scope filter (resolve-then-scope per §V.107/§V.115 family 1) — unknown domain → `not_found`, not silent `[]`. Every FK projection stays feed-able by natural key.

Trigger: `src/mailpilot/database` changed.
- `rg 'account_email.*workflow\|workflow.*account_email' src/mailpilot/database` -> workflow rows carry account_email
- `rg 'LEFT JOIN company\b' src/mailpilot/database` -> contact rows LEFT JOIN company for company_domain
- `rg 'workflow_name.*enrollment\|enrollment.*workflow_name' src/mailpilot/database` -> enrollment rows carry workflow_name denorm

## §V7 — EmailSummary projection

`EmailSummary` MUST include `gmail_thread_id`, `is_routed`, `route_method`, `recipients` (To/Cc/Bcc address map mirroring the Email base field), and `snippet` (first 500 chars of `body_text`; empty when body empty). Operator audits routing from CLI without Logfire. A single bulk `email list` exposes each message's recipients + snippet without a per-row `email view`. `snippet` suffices to classify OOO / left-company / referral. Full `body_text` stays `email view`. No `--include-body` — snippet always on every EmailSummary row (`list_emails` + `search_emails`). `list_emails` + `search_emails` SELECT project `recipients` + `snippet` so fields populate (not default-empty). §V.122 keys campaign-test delivery on the recipients projection.

Trigger: `src/mailpilot/database` or `src/mailpilot/models.py` changed.
- `rg 'gmail_thread_id\b' src/mailpilot/models.py | grep EmailSummary` -> gmail_thread_id in EmailSummary
- `rg 'route_method\b' src/mailpilot/models.py | grep EmailSummary` -> route_method in EmailSummary
- `rg 'recipients\b' src/mailpilot/models.py | grep EmailSummary` -> recipients in EmailSummary
- `rg 'snippet\b' src/mailpilot/models.py | grep EmailSummary` -> snippet in EmailSummary
- `rg 'recipients\b' src/mailpilot/database | grep list_emails` -> recipients projected in list_emails SELECT
- `rg 'snippet\b' src/mailpilot/database | grep -E 'list_emails|search_emails'` -> snippet projected in list+search SELECT

## §V8 — view model projections

ContactView = base Contact superset + company_domain (LEFT JOIN company) + `tags[]` (assigned names, empty ok; same shape as CompanyView.tags / `db export` company.tags §V.121). ContactSummary lean list|search row carries `tags[]` same shape. `contact list|search|view` always project `tags[]`. `company view --full` lean contacts inherit ContactSummary so they carry `tags[]` (§V.168). CompanyView = base Company superset + `tags` (assigned tag names, empty ok; same shape as CompanySummary.tags / `db export` company.tags §V.121). CompanySummary lean list row carries `tags` + `disabled_reason` (null when enabled) + `contact_count`/`has_profile`; `--full` opts in `profile.summary` only (null when no profile) — never default full profile. CompanyView carries `aliases[]` (sorted lowercased alias domains, empty ok; view-only — list lean omits). ContactView omits `verification_meta` by default (operator-only via `contact view --include-meta` §V.144; meta opt-in = CLI-only, never agent path). MeetingView = base Meeting superset + attendee contacts (list_meeting_attendees join). All three views: inline <=10 latest notes (`_INLINE_NOTES_CAP`) + total count; field set test-tracked vs base model (Pydantic `extra=ignore` silently strips fields omitted from the view model — test catches drift). `meeting list` rows carry compact attendee summary (emails or count). `meeting view` inlines full attendee list. Workflow-agent prompt pre-feed (`Contact record:` / `Company record:` sections, §V.135) routes through load_contact_view/load_company_view — agent + CLI context byte-identical except contact `tags[]` stripped from `Contact record:` (CLI inspect only; allowlist unchanged). `company view --full` inspect dossier (`contacts[]`+`tags[]`+`notes[]`) → §V.168; lean CompanyView unchanged.

Trigger: `src/mailpilot/models.py` or `src/mailpilot/database` changed.
- `rg 'ContactView|CompanyView|MeetingView' src/mailpilot/models.py` -> all three present
- `rg 'tags.*list|tags: list' src/mailpilot/models.py` -> CompanySummary + CompanyView + ContactSummary + ContactView carry tags
- `rg '_INLINE_NOTES_CAP' src/mailpilot/database` -> cap constant present
- `rg 'load_contact_view|load_company_view|load_meeting_view' src/mailpilot/database` -> loaders present
- `rg 'test.*view.*field|ContactView.*Contact\b|CompanyView.*Company\b' src/mailpilot/tests/` -> field-set invariant test present
- `rg 'profile\.summary|--full' src/mailpilot/cli src/mailpilot/models.py` -> company list --full opt-in

## §V10 — tag soft-disable

`tag.disabled_reason TEXT NULL`; non-NULL = disabled, carries reason. `tag disable <name>` sets it (disabled_reason IS NULL gate blocks double-disable). `tag enable <name>` clears it (disabled_reason IS NOT NULL gate blocks enabling an active tag). Vocabulary-tag disable/enable write no activity — a `tag` row has no contact/company owner (§V.17). `tag list` hides disabled unless `--include-disabled`. `tag disable` retires a vocabulary-table (`tag`) entry (§V.116), NOT a per-owner link — `tag remove` is the distinct unlinking verb.

Trigger: `src/mailpilot/cli` or `src/mailpilot/database` changed.
- `rg 'disabled_reason.*tag\|tag.*disabled_reason' src/mailpilot/schema.sql` -> disabled_reason col on tag table
- `rg '"tag".*"enable"\|enable_tag\b' src/mailpilot/cli src/mailpilot/database` -> tag enable verb present
- `rg 'IS NULL.*disabled_reason\|disabled_reason.*IS NULL' src/mailpilot/database | grep tag` -> double-disable gate on tag

## §V11 — status payload envelope

`mailpilot status` envelope = `{version, schema, sync_loop, accounts, tasks, config, counts}`. Schema block carries three-state `verdict` in {current, pending, drift} + `recorded_hash`/`current_hash` + applied/pending migration counts (not a bare drift bool, §V.109). Tasks block carries `pending`, `failed_24h`, `scheduled_future`, `oldest_pending_age_seconds`, `max_attempt_count_pending`.

Trigger: `src/mailpilot/cli` changed.
- `rg 'sync_loop\b' src/mailpilot/cli | grep status` -> sync_loop key in status envelope
- `rg 'failed_24h\|oldest_pending_age_seconds\|max_attempt_count_pending' src/mailpilot/cli src/mailpilot/database` -> task block fields present
- `rg 'recorded_hash\|current_hash' src/mailpilot/cli | grep status` -> hash fields in schema block

## §V14 — activity append-only + note lifecycle

Activity = INSERT only — no update/delete fns for activity rows. Note = INSERT + dual-mode hard-delete `note remove` (§I): (a) single-id `note remove <note_id>`; (b) owner bulk `note remove --company-domain|--contact-email <ref> --yes` (XOR owner, `--yes` required). Operator-only, NOT an agent tool. Tag/note mutation + its activity row commit in one txn — both or neither. `note remove` deletes note row(s) only, writes no activity — prior `note_added` rows survive as the append-only trail. Bulk envelope `{"notes_removed":{owner, removed_count, note_ids[]}}`; zero notes = ok no-op record_count=0.

Trigger: `src/mailpilot/database` or `src/mailpilot/cli` changed.
- `rg 'def update_activity\|def delete_activity' src/mailpilot/database` -> zero hits (activity append-only)
- `rg 'def delete_note\b' src/mailpilot/database` -> single-note hard-delete fn present
- `rg 'def delete_notes\b' src/mailpilot/database` -> owner bulk hard-delete fn present
- `rg 'notes_removed' src/mailpilot/cli` -> bulk envelope key present
- `rg 'note_added' src/mailpilot/database` -> note INSERT pairs its activity row in one txn

## §V15 — enrollment lifecycle + outcome model

`enrollment.status` in {active, disabled} (no `paused`). `disabled` = operator halt, requires non-empty `disabled_reason` (CHECK) + `enrollment_disabled` activity, reversible via `enrollment enable <id>` (status disabled→active, clears `disabled_reason`, `status <> 'disabled'` gate blocks enabling a live enrollment, emits `enrollment_enabled` activity). `enrollment disable`/`enable` are the sole halt/resume surface (NO `enrollment update` status verb). Outcomes live on activity timeline via `record_enrollment_outcome` (accepts only completed|failed); enrollment status untouched. `record_enrollment_outcome` bumps `enrollment.updated_at` in the same txn as the activity so `enrollment list --since`/`--until` (filters `e.updated_at`) + `--full` + `--disposition` windows a terminal outcome (incl. do_not_contact) without `contact view --timeline`. no `disposition_updated_at` col — `updated_at` is the window clock.

Trigger: `src/mailpilot/database` or `src/mailpilot/models.py` changed.
- `rg 'status.*CHECK\b' src/mailpilot/schema.sql | grep enrollment` -> status CHECK in schema
- `rg 'enrollment_disabled\b' src/mailpilot/database` -> enrollment_disabled activity on disable
- `rg 'enrollment_enabled\b' src/mailpilot/database` -> enrollment_enabled activity on enable
- `rg 'record_enrollment_outcome\b' src/mailpilot/database` -> outcome fn present
- `rg 'updated_at = CURRENT_TIMESTAMP' src/mailpilot/database` -> outcome path bumps enrollment.updated_at
- `rg "status.*<>.*'disabled'\|!=.*'disabled'" src/mailpilot/database` -> enabling guard

## §V18 — schema drift definition

Schema drift = live DB structure diverged from `schema.sql` w/ no migration path (manual edit | DB ahead of code); primitive = hash mismatch per §V.19. Distinct from `pending` = unapplied `migrations/NNN_*.sql` (§V.108). Response tiered per §V.109: `status` + `db check` tolerate + report; `run` + mutations dead-stop.

Trigger: `src/mailpilot/database` changed.
- `rg '"drift"' src/mailpilot/database` -> drift verdict present, distinct from pending
- `rg '"pending"' src/mailpilot/database` -> pending verdict present
- `rg 'schema_hash\|recorded_hash' src/mailpilot/database` -> hash-mismatch primitive

## §V22 — is_routed gate: single route pass per email

At most 1 `routing.route_email` span lifecycle per `email_id`. Gate: every routing outcome (§V.20) sets `is_routed=TRUE`; a subsequent History-API re-delivery or repeat sync sweep skips routing entirely on an already-routed message. Duplicate route spans inflate metrics and mask classifier regressions. Skip marks (`skipped_outside_window` / `skipped_no_workflows` / `skipped_predates_workflows`) go through `route_email` or shared `mark_routed` (§V.187) — `_store_inbound_message` never opens a `routing.route_email` span without that path.

Trigger: `src/mailpilot/routing.py` or `src/mailpilot/sync.py` changed.
- `rg 'is_routed\b' src/mailpilot/routing.py src/mailpilot/sync.py` -> is_routed gate present
- `rg 'is_routed.*True\b\|True.*is_routed' src/mailpilot/database` -> is_routed set on every outcome
- `rg 'if.*is_routed\b' src/mailpilot/routing.py src/mailpilot/sync.py` -> gate check before route call

## §V23 — task drain pool + per-worker trace isolation

Task drain = bounded pool <= `max_concurrent_tasks`; each worker owns its psycopg.Connection; atomic claim blocks re-dispatch of in-flight tasks. Each worker roots its own trace — the drain worker detaches the dispatching tick's `sync.loop.iteration` OTel context before `run.execute_task` (py3.14 ThreadPoolExecutor.submit propagates the active span via contextvars), so trace_id maps 1:1 w/ agent.invoke.

Trigger: `src/mailpilot/sync.py` changed.
- `rg 'max_concurrent_tasks' src/mailpilot/sync.py` -> pool bound present
- `rg 'otel_context' src/mailpilot/sync.py` -> worker attaches fresh context + detaches token
- `rg 'ThreadPoolExecutor' src/mailpilot/sync.py` -> bounded executor drain

## §V25 — advisory locks 2-tier

Advisory locks 2-tier: coarse (workflow_id, contact_id) + task-scoped (task_id split-half CRC32 pair). Lock acquired BEFORE the agent.invoke span opens — loser -> None, no span emitted. Contention -> reschedule w/o attempt_count bump; the scheduled_at push fires task_pending_trigger so the loop re-wakes.

Trigger: `src/mailpilot/agent/invoke.py` or `src/mailpilot/database` changed.
- `rg 'crc32' src/mailpilot/agent/invoke.py` -> CRC32 lock-key derivation present
- `rg 'advisory' src/mailpilot/agent/invoke.py src/mailpilot/database` -> both lock tiers present
- `rg 'task_pending_trigger' src/mailpilot/schema.sql` -> reschedule push re-wakes the loop

## §V27 — routing pipeline order + classifier bounds

Routing pipeline order: RFC message-id match -> thread match -> LLM classify; every stage account-scoped. Message-ID preferred when In-Reply-To/References present (Gmail same-subject merge must not rebind replies across multi-workflow enrollments). RFC parent lookup not queried twice for the same headers (`_try_rfc_message_id_match` vs `find_thread_enrolled_contact`; §V.187). Classifier = single-turn, no tools; body truncated @ 16384 chars; hallucinated workflow_id coerced to None; zero active inbound workflows -> no LLM call. Every outcome marks is_routed=TRUE w/ a distinct route_method (§V.20 enum).

Trigger: `src/mailpilot/routing.py` or `src/mailpilot/agent/classify.py` changed.
- `rg '_try_rfc_message_id_match' src/mailpilot/routing.py` -> RFC message-id stage before thread match
- `rg '16384' src/mailpilot/agent/classify.py` -> body truncation bound
- `rg 'is_routed' src/mailpilot/routing.py` -> every outcome marks routed

## §V31 — deferred branch direction-aware

Protocol deferred branch keyed on direction + trigger. Outbound: trigger='task' -> terminal-outcome instruction (`_DEFERRED_TASK_TASK`, names conclude_enrollment); outbound first reach-out = compose-only touch run, binds NO deferred fragment (§V.136). Inbound: every trigger -> inbound-reply instruction (`_DEFERRED_TASK_INBOUND`: reply once + stop, system records outcome, never conclude_enrollment / create_task); inbound templates bind neither conclude_enrollment nor create_task.

Trigger: `src/mailpilot/agent/templates.py` changed.
- `rg '_DEFERRED_TASK_INBOUND\|_DEFERRED_TASK_TASK' src/mailpilot/agent/templates.py` -> both direction fragments present
- `rg 'build_protocol' src/mailpilot/agent/templates.py` -> direction-aware composition fn
- `rg '_INBOUND_EXCLUDED_TOOLS' src/mailpilot/agent/templates.py` -> inbound rosters exclude conclude_enrollment + create_task

## §V38 — Drive tools sequential=True

All Drive-KB agent tools (`list_drive_markdown`, `read_drive_markdown`, `search_drive_markdown`) registered with `sequential=True` in the Pydantic-AI tool set; serializes parallel dispatch. Rationale: shared `httplib2.Http` transport is thread-unsafe; max concurrent transport = 1. One `_drive_call` wraps the three tools' HttpError 404 → `not_found` and other HttpError / TimeoutError / OSError → `drive_unavailable` mapping. Tools do not copy the try/except. NEVER bare raise to agent (§V.39).

Trigger: `src/mailpilot/agent/tools.py` changed.
- `rg 'sequential.*True\|True.*sequential' src/mailpilot/agent/tools.py | grep drive` -> sequential=True on Drive tools
- `rg 'def _drive_call' src/mailpilot/agent/tools.py` -> one mapping helper
- `rg 'drive_unavailable\b' src/mailpilot/agent/tools.py` -> error dict key present
- `rg 'except HttpError' src/mailpilot/agent/tools.py` -> only inside `_drive_call`
- `rg 'HttpError\b\|TimeoutError\b\|OSError\b' src/mailpilot/agent/tools.py | grep drive` -> all three fault classes caught

## §V41 — KB grounding rules live in workflow instructions

KB grounding rules (search-first, 2-search budget then single list, read top >= 3 hits, per-target search budget on compare) live in the workflow definition's `instructions` field (§V.103), NOT a code-defined template protocol fragment. inbound-google-drive template binds the Drive tool set but carries no grounding fragment — grounding wording is per-workflow data, not code.

Trigger: `src/mailpilot/agent/templates.py` or `workflows/*.toml` changed.
- `rg -i 'search-first\|search first\|2-search' src/mailpilot/agent/templates.py` -> zero hits (no grounding fragment in code)
- `rg 'list_drive_markdown' src/mailpilot/agent/templates.py` -> Drive tool set bound on inbound-google-drive

## §V42 — Agent email body structure: lists only; no table lint

Trigger when `src/mailpilot/agent/` or `src/mailpilot/email_renderer.py` changed.

Agent-facing text (composed protocol fragments, compose-only instructions, registered tool docstrings, ModelRetry / tool-error fix messages that reach the model) bans Markdown tables: no GFM pipe-table mandate, no `|---|` separator instruction, no "pipe table" / "Markdown table" wording. Multi-row structure = plain-text lines or `-` lists only.

`_check_spec_table` retired: no format-check rejection on `send_email` / `reply_email` / compose-only `TouchMessage` validators. No ModelRetry or tool-error that teaches pipe-table formatting.

`_SPEC_TABLE` fragment retired: not composed into any template `protocol_pre`. Product facts in agent-facing text use list or prose shape, never a table mandate.

Mechanical check:
- `rg -n '_check_spec_table|_SPEC_TABLE|_SPEC_ROW_RE|_PIPE_SEPARATOR_RE' src/mailpilot/` → zero hits
- `rg -ni 'pipe table|Markdown table|\|---\|' src/mailpilot/agent/templates.py` → zero hits in string literals (code comments ok)
- compose-only output validators + `send_email` / `reply_email` carry no body format-lint call

## §V44 — template registry owns agent shape

TEMPLATES keys == WorkflowTemplateName members (registry total). WorkflowTemplate frozen (`@dataclass(frozen=True)`). Every template carries non-empty protocol + tools + description. workflow.template + type immutable post-create — update raises ValueError on either; type derived from template (never stored independently).

Trigger: `src/mailpilot/agent/templates.py` or `src/mailpilot/database` changed.
- `rg 'WorkflowTemplateName' src/mailpilot/agent/templates.py` -> registry keyed on the enum
- `rg 'frozen=True' src/mailpilot/agent/templates.py` -> WorkflowTemplate frozen
- `rg 'immutable' src/mailpilot/database | rg -i 'template\|type'` -> post-create immutability guard in update_workflow

## §V45 — no SPEC citation in agent-visible text

Trigger when `src/mailpilot/agent/templates.py` or `src/mailpilot/agent/tools.py` changed.

Agent-visible text = the composed protocol string + every registered tool's model-visible schema. pydantic-ai derives a tool's description AND per-parameter help from the registered function's full docstring (Args/Returns included), so a `§V/§T/§B.<n>` token anywhere in a registered tool's docstring leaks dead authoring metadata into the reply-agent prompt (`§B.79`: `_BASE` literal `(§V.42)`; `§B.84`: six tool-docstring Args/Returns cites). The governing invariant is cited in an adjacent code comment, never the model-visible string.

Scope of "registered tool docstrings" = the source functions in `tools.py` named by `TEMPLATES[*].tools` (`send_email`, `reply_email`, `create_task`, `cancel_task`, `record_enrollment_outcome`, `disable_contact`, `list_enrollments`, `search_emails`, `read_email`, `noop`, `list_drive_markdown`, `read_drive_markdown`, `search_drive_markdown`). Internal helpers + module comments are NOT registered, so their §-cites are exempt — flag a hit only when it sits inside a registered tool's `"""docstring"""`.

Mechanical checks:
- `rg -n '§[VTB]\.[0-9]+' src/mailpilot/agent/templates.py` -> classify each hit: code comment -> exempt; composed-protocol fragment string (`_BASE`, `_DECLINE`, `_DEFERRED_TASK_*`, `_NO_FABRICATION`) -> fail (move the cite to a comment).
- `rg -n '§[VTB]\.[0-9]+' src/mailpilot/agent/tools.py` -> classify each hit: comment / helper docstring -> exempt; inside a registered tool's docstring (per the roster above) -> fail (move the cite to a comment or drop it).

Protocol composed `_BASE → deferred branch → _MUST_SEND → _DECLINE → _NO_FABRICATION` = tool-loop shape; compose-only touch runs per §V.136; deferred branch selected per §V.31 (direction + trigger). No `_SPEC_TABLE` fragment (retired §V.42). `_MUST_SEND` = end every trigger turn in a send or explicit noop; composed into `protocol_post` for all three templates. Every fragment is email-universal OR direction-scoped; never workflow-specific. Agent-facing text (composed protocol + registered tool docstrings) carries zero SPEC citation (`§V/§T/§B.<n>` tokens ban). Table bans for agent-facing structure → §V.42.

Trigger: `src/mailpilot/agent/templates.py` or `src/mailpilot/agent/tools.py` changed.

Mechanical checks:
- `rg -n '§[VTB]\.[0-9]+' src/mailpilot/agent/templates.py` -> classify each hit: code comment → exempt; inside a fragment string → fail.
- `rg -n '§[VTB]\.[0-9]+' src/mailpilot/agent/tools.py` -> classify each hit: comment / helper docstring → exempt; inside a registered tool docstring → fail.
- `rg -n 'may use Markdown' src/mailpilot/agent/templates.py` -> zero hits (permissive wording retired).
- `rg -n '_SPEC_TABLE' src/mailpilot/` -> zero hits (fragment retired §V.42).

Registered tool docstring scope = functions named in `TEMPLATES[*].tools` (send_email, reply_email, create_task, cancel_task, conclude_enrollment, disable_contact, list_enrollments, search_emails, read_email, noop, list_drive_markdown, read_drive_markdown, search_drive_markdown). Internal helpers + module comments exempt.

## §V47 — provider-aware model config: dispatch + caching + model settings

`llm_provider` in {`anthropic`, `xai`} (default `xai`) selects factory branch for **both** classifier + workflow agent via public `build_model(settings, *, role)`. `require_active_provider_key` runs once at `build_model`; inner `_build_anthropic_model` / `_build_xai_model` do not re-call. Active-provider API key required @ each `mailpilot run` tick before drain (and still @ model build). Missing/empty key → skip drain that tick; process stays up; zero due tasks claimed or marked `failed` (closes §B.141). Model-build remains fail-closed (no provider fallthrough) if preflight skipped. Inactive-provider keys may be empty. Error names `mailpilot config set xai_api_key` (provider=xai) or `mailpilot config set anthropic_api_key` (provider=anthropic). Keys live on `app_config`; `MAILPILOT_*` API-key env vars are not sources. Dep: `pydantic-ai-slim[anthropic,xai]`.

**Anthropic branch** (`llm_provider=anthropic`): Caching (both call sites — classifier + workflow agent): `anthropic_cache_instructions=True` + `anthropic_cache_tool_definitions=True`. Telemetry attribute names: `agent.invoke` rollup span carries bare `cache_read_input_tokens` + `cache_creation_input_tokens` (from `usage.cache_read_tokens`/`cache_write_tokens`); per-call `chat` span carries OTel `gen_ai.usage.cache_read.input_tokens` + `gen_ai.usage.details.cache_creation_input_tokens` — verify caching against these exact names. `gen_ai.usage.cache_read_input_tokens` exists on neither span (null = false caching-off diagnosis, §B.113). Model settings (workflow agent only — classifier excluded): `_build_anthropic_model` reads `anthropic_thinking`, `anthropic_effort`, `anthropic_max_tokens` into `AnthropicModelSettings`. `anthropic_max_tokens` ALWAYS passed as `max_tokens=<int>` (not empty-gated). `anthropic_thinking` and `anthropic_effort` added ONLY when non-empty. Defaults: `anthropic_model=claude-sonnet-5`, `anthropic_thinking=adaptive`, `anthropic_effort=high`, `anthropic_max_tokens=32768`. `xhigh` effort requires Opus 4.7+.

**xAI branch** (`llm_provider=xai`, default): `XaiProvider(api_key=..., api_host=optional, timeout=240)` + `XaiModel(xai_model, provider=...)`. No Anthropic cache flags (omit — no false cache telemetry). Model settings (workflow agent only — classifier excluded): `_build_xai_model` reads `xai_reasoning_effort`, `xai_max_tokens` into `XaiModelSettings` / shared `ModelSettings`. `xai_max_tokens` ALWAYS passed. Defaults: `xai_model=grok-4.5`, `xai_reasoning_effort=medium`, `xai_max_tokens=32768`. Env key: `MAILPILOT_XAI_API_KEY` / config `xai_api_key` only (bare `XAI_API_KEY` not a mailpilot source).

**Effort enums** (settings load / `config set`, not first agent turn): `anthropic_effort` in {unset, `low`, `medium`, `high`, `xhigh`, `max`}; `xai_reasoning_effort` in {`low`, `medium`, `high`} (no `none` — Grok 4.5 always reasons). Invalid value rejected at settings layer.

Trigger: `src/mailpilot/agent/invoke.py`, `src/mailpilot/agent/classify.py`, `src/mailpilot/agent/model.py`, `src/mailpilot/run.py`, or `src/mailpilot/settings.py` changed.
- `rg 'llm_provider|def build_model\b' src/mailpilot/agent/` -> provider dispatch present
- `rg 'require_active_provider_key' src/mailpilot/agent/model.py` -> one call site (`build_model`)
- Anthropic path (when selected): `rg 'anthropic_cache_instructions.*True\|anthropic_cache_tool_definitions.*True' src/mailpilot/agent/` -> caching flags on both Anthropic call sites
- Anthropic path: `rg 'max_tokens.*anthropic_max_tokens\|anthropic_max_tokens.*max_tokens' src/mailpilot/agent/` -> `max_tokens` always set (not in an `if` guard)
- xAI path: `rg 'XaiModel|XaiProvider|xai_reasoning_effort|xai_max_tokens' src/mailpilot/agent/` -> xAI factory + settings wiring
- `rg 'max_tokens\b' src/mailpilot/agent/classify.py` -> zero hits (classifier excluded from max_tokens)
- settings: `rg 'AnthropicEffort|XaiReasoningEffort|llm_provider' src/mailpilot/settings.py` -> closed enums + provider field
- `rg 'mailpilot config set xai_api_key|mailpilot config set anthropic_api_key' src/mailpilot/` -> missing-key error names config set
- `rg 'MAILPILOT_XAI_API_KEY|MAILPILOT_ANTHROPIC_API_KEY' src/mailpilot/` -> zero error-name hits (env vars are not sources)
- fixture: missing key + due T1 batch → skip drain, zero tasks written `failed`

## §V49 — bounded auto-retry parameters

4 attempts total; backoff [30, 120, 300]s; transient allow-list = Google 429/5xx, Anthropic 502/503/529, socket/TimeoutError; Drive socket timeout 60s feeds classifier; manual retry only failed/cancelled (completed + pending refused); schedule policy → §V.170; retry UPDATE fires task_pending_trigger.

## §V51 — logfire.exception + operator_event("error") pairing

Every `logfire.exception(...)` call in the call-graph reachable from `mailpilot run` MUST appear in the same `except` block as `operator_event("error", source=..., message=...)`. A contract test sweeps all run-reachable modules for `logfire.exception` sites and asserts the paired `operator_event("error")` is present in the same except block — failure = terminal error produces no operator stderr line.

Trigger: any `src/mailpilot/**/*.py` changed.
- `rg -n 'logfire\.exception\b' src/mailpilot/` -> enumerate exception sites reachable from `mailpilot run`
- For each hit file: verify same except block has `operator_event("error"` within 5 lines
- `rg 'test.*logfire.*exception\|logfire.*exception.*test' src/mailpilot/tests/` -> contract test present

## §V54 — CLI mutation spans + constraint codes

Every CLI mutation (`create`, `update`, `disable`, `enable`, `add`, `remove`, `reply`, `send`, `start`, `stop`, `cancel`, `retry`) wraps its body in `logfire.span("<noun>.<verb>")` + emits `operator_event` with changed fields. psycopg constraint exception → error code mapping: UniqueViolation → `duplicate_key`, ForeignKeyViolation → `foreign_key_violation`, NotNullViolation → `not_null_violation`, CheckViolation → `check_violation`, other `psycopg.Error` → `database_error`, `ValidationError` → `validation_error`. Controlled `output_error` path (SystemExit) absorbed inside the `with logfire.span` block — span closes clean, SystemExit re-raised after. Only a genuine non-SystemExit Exception marks the span. Business-outcome envelopes (duplicate_key, not_found, validation_error, etc.) never surface as Logfire exceptions.

Trigger: `src/mailpilot/cli` or `src/mailpilot/operator_log.py` changed.
- `rg -n 'except.*UniqueViolation|duplicate_key' src/mailpilot/operator_log.py src/mailpilot/cli` -> mapping present
- `rg -n 'except.*SystemExit|re-raise' src/mailpilot/operator_log.py` -> SystemExit absorbed + re-raised inside span
- Telemetry test: `account.create` duplicate-key span carries no `exception.escaped=True` on the parent span.

## §V62 — release flow: make target + Keep-a-Changelog + CI-gated GH release + PyPI

Release = `make release major|minor|patch` sole path. Local gates: part arg present, clean working tree, `make check`, `scripts/changelog check` (empty/`## Unreleased` no `- ` bullets → hard fail before pyproject mutates). Steps: `uv version --bump <part>` (bumps `pyproject.toml` + `uv.lock`); `scripts/changelog promote <ver>` (Unreleased body → `## [vX.Y.Z] - YYYY-MM-DD`, leave empty `## Unreleased`); commit `CHANGELOG.md` + `pyproject.toml` + `uv.lock` together as `chore: release v<x.y.z>`; tag `v<x.y.z>`; push main + tags only — no local `gh release create`. Keep-a-Changelog root `CHANGELOG.md`: user-facing work appends under `## Unreleased` (`### Added` / `### Changed` / `### Fixed`) during development. Publish = `.github/workflows/release.yml` on push tags `v*`: `check` job calls ci.yml via workflow_call (make check equivalent); only on success does `publish` run: tag must equal `v$(uv version --short)`; `uv build`; `pypa/gh-action-pypi-publish` w/ OIDC (`id-token: write`, no PyPI API token); `scripts/changelog notes <tag>` → `gh release create --notes-file` + `dist/*` (`contents: write`); not sole `--generate-notes`. GH release + PyPI only after CI passes. Dist name = `mailpilot-crm` (PyPI name `mailpilot` foreign-owned); module + CLI cmd = `mailpilot` via `[tool.uv.build-backend] module-name`. Deploy = PyPI package — `uv tool install mailpilot-crm`.

Trigger: `makefile`, `.github/workflows/release.yml`, `CHANGELOG.md`, `scripts/changelog`, or `pyproject.toml` changed.
- `rg 'uv version --bump' makefile` -> bump step present
- `rg 'scripts/changelog check' makefile` -> Unreleased hard-fail before bump
- `rg 'scripts/changelog promote' makefile` -> promote step present
- `rg 'CHANGELOG.md' makefile` -> release commit includes CHANGELOG
- `rg 'gh release create' makefile` -> zero hits (local make does not create GH release)
- `rg 'gh release create' .github/workflows/release.yml` -> CI creates GH release after check
- `rg 'scripts/changelog notes' .github/workflows/release.yml` -> notes from CHANGELOG section
- `rg -- '--notes-file' .github/workflows/release.yml` -> notes-file not generate-notes
- `rg -- '--generate-notes' .github/workflows/release.yml` -> zero hits
- `rg 'git diff --quiet' makefile` -> clean-tree gate present
- `rg 'needs: check' .github/workflows/release.yml` -> publish gated on CI
- `rg 'pypa/gh-action-pypi-publish' .github/workflows/release.yml` -> trusted-publishing action present
- `rg 'id-token: write' .github/workflows/release.yml` -> OIDC permission present
- `rg 'uv version --short' .github/workflows/release.yml` -> tag==version gate present
- `rg 'workflow_call' .github/workflows/ci.yml` -> CI reusable as publish gate
- `rg 'name = "mailpilot-crm"' pyproject.toml` -> dist name present
- `rg 'module-name = "mailpilot"' pyproject.toml` -> module override present

## §V73 — Skill-body Workflow snippet executability

Mechanical audit; trigger when `.grok/skills/**/*.md` or `.claude/workflows/*.js` changed. Scope = every fenced ```js block that calls `parallel(`, `pipeline(`, or `agent(`, plus the saved-workflow byte-identity check (d).

Per ```js block:
(a) Free-symbol scan — every identifier used as a value ! resolve to an in-block definition (`const` / `let` / `function` / param) OR a runtime global. Runtime globals (do not flag): `meta`, `agent`, `parallel`, `pipeline`, `phase`, `log`, `args`, `budget`, `workflow`, plus JS built-ins (`JSON`, `Math`, `Array`, `Object`, `Promise`, `console`, ...). Any other bare identifier (e.g. `stale`, `buildPrompt`, `ENRICH_RESULT_SCHEMA`) ! be defined in the block — fail mode: free var crashes `ReferenceError` on paste (`§B.68`: bare `stale`).
(b) `args`-as-collection guard — if the block calls `args.map` / `args.filter` / `args.slice` / `args.length` / `args.forEach` or spreads `args`, it ! first `JSON.parse(args)` (or guard `typeof args === 'string'`). Why: runtime delivers `args` as a JSON STRING so `args.map` throws `is not a function` (`§B.68`).
(c) Prose-vs-`parallel` divergence — if surrounding prose claims "concurrency N" / "N concurrent" / "Default N", the block ! chunk to N (batch loop of size N around `parallel(batch.map(...))`). A bare `parallel(xs.map(...))` dispatches all `xs.length`, bounded only by runtime cap `min(16, cores-2)` — not N. Fail mode: prose promises 3, snippet runs all (`§B.68` secondary).
(d) Saved-workflow byte-identity — every embedded workflow snippet's post-`meta` body (each `.grok/skills/<skill>/SKILL.md` FIRST js-fenced block, sliced @ first `\n}\n` after `export const meta`) ! be byte-identical to its saved `.claude/workflows/<name>.js`'s post-`meta` body (same slice). Audited pairs (extend the PAIRS list below when a new skill+workflow lands): `lead-companies/SKILL.md` <-> `lead-companies-enrich.js`; `lead-contacts/SKILL.md` <-> `lead-contacts-find.js`. Why: the skill-body embedded snippet is the spec-of-record; the saved file is invoked by name @ runtime so silent divergence ships an unaudited workflow ((a)-(c) cover the saved file only transitively, when bodies match). Saved `meta` MAY add registry-only fields (`whenToUse`, fuller `description`) so compare the post-`meta` slice only, not the whole file. Fail mode: divergence -> saved-file unaudited drift.

Mechanical greps (manual judgment on hits):
- `rg -n '```js' .grok/skills/` — enumerate blocks.
- `rg -nE '\bargs\.(map|filter|slice|length|forEach)\b' .grok/skills/` not preceded by `JSON.parse(args)` or `typeof args` -> (b) fail.
- prose `rg -niE 'concurrency [0-9]|[0-9] concurrent|default [0-9]' .grok/skills/` near a block with bare `parallel(` and no batch loop (`for .* += N` / `.slice(`) -> (c) fail.
- (d) byte-identity — extract both post-`meta` bodies (slice each @ first `\n}\n` after `export const meta`, `.strip()`), compare equal:
  ```
  python3 - <<'PY'
  import re
  PAIRS = [
      ('.claude/skills/lead-companies/SKILL.md', '.claude/workflows/lead-companies-enrich.js'),
      ('.claude/skills/lead-contacts/SKILL.md', '.claude/workflows/lead-contacts-find.js'),
  ]
  body = lambda s: s[s.find('\n}\n') + 3:].strip()
  for skill_path, saved_path in PAIRS:
      emb = re.search(r'```js\n(.*?)```', open(skill_path).read(), re.DOTALL).group(1)
      saved = open(saved_path).read()
      print(('IDENTICAL' if body(emb) == body(saved) else 'DIVERGENT'), saved_path)
  PY
  ```
  any `DIVERGENT` -> (d) fail (saved-file unaudited drift).

## §V74 — RFC-4180 CSV-ingestion parser mandate

Mechanical audit; trigger when `.grok/skills/**/*.md`, `.grok/skills/**/scripts/*.py`, or `src/**` changed. Scope = CSV-ingestion sites (handle a `.csv` path, a "CSV mode", or a comma-delimited lead export). The grep scope `.grok/skills/ src/` already recurses into `scripts/` so a `.py`-under-`scripts/` change is covered once the trigger-glob (previously `.md`-only) names it.

Checks:
(i) CSV ingestion ! use an RFC-4180 parser (`csv.DictReader` / `csv.reader` / the `csv` module). Fail mode: physical-line iteration, `.splitlines()`, `.split("\n")`, or `.split(",")` over CSV content — quoted fields carry embedded newlines and commas so one logical row spans many physical lines (`§B.69`: theirstack.csv 25 logical rows over 217 physical lines).
(ii) Redirect resolution ! use `curl -sL -o /dev/null -w '%{url_effective}'` (full chain, CR-free). Fail mode: HEAD `curl -sLI | grep '^location:' | awk` — 403 bot-blocking origins answer HEAD differently; awk retains the header trailing CR so corrupts a bare-host redirect target (`§B.69`).

Mechanical greps (manual judgment on hits — flag only in CSV context):
- `rg -n 'splitlines|\.split\(' .grok/skills/ src/` near `csv` / `CSV` / `.csv` context -> (i) fail. Non-CSV `splitlines` (email-body normalization, markdown line scan) not flagged.
- `rg -n 'curl -sLI' .grok/skills/ src/` -> (ii) fail (HEAD-grep redirect resolution).

Plain-text (non-CSV) line iteration is admitted (per-line domain/URL, `#`-comment skip) — do not flag.

## §V75 — Gmail sync incremental + checkpoint integrity

Sync incremental via History API from `gmail_history_id` checkpoint. History 404 → full INBOX re-sync (history expired). First sync (`last_synced_at NULL`) → full INBOX listing regardless of `gmail_history_id` (hydrates pre-watch state). `get_messages_batch` callback: 404 per-sub-request → skip (deleted); 429 / 5xx per-sub-request → bounded backoff retry, NEVER dropped (sibling branch to 404-skip, not the same branch). `gmail_history_id` checkpoint advances only past persisted messages — exhausted-retry batch raises, `sync_account` never commits checkpoint past unstored mail. `_BATCH_SIZE` keeps concurrent `messages.get` sub-requests below Gmail per-user cap.

Trigger: `src/mailpilot/sync.py` or `src/mailpilot/gmail.py` changed.
- `rg -n 'last_synced_at.*None\b|last_synced_at.*NULL\b' src/mailpilot/sync.py` -> first-sync full-path gate
- `rg -n 'status_code.*404\b|404.*skip' src/mailpilot/sync.py src/mailpilot/gmail.py` -> 404-skip handler
- `rg -n '429.*retry\b|retry.*429\b' src/mailpilot/sync.py src/mailpilot/gmail.py` -> 429 retry (not silent drop)
- `rg -n 'gmail_history_id.*checkpoint\b|checkpoint.*advance' src/mailpilot/sync.py` -> checkpoint only on success

## §V77 — outbound email row persistence + orphan recovery

Outbound email row persists only AFTER Gmail accepts send — Gmail failure produces no orphan row. Post-send `create_email ON CONFLICT (gmail_message_id) DO NOTHING` → None signals the row already exists; recover via `get_email_by_gmail_message_id` + return (idempotent send, never raise). Genuinely unrecoverable (no gmail_id, or conflicting row vanished after conflict): log `orphan_gmail_send` + raise.

Trigger: `src/mailpilot/sync.py` or `src/mailpilot/gmail.py` changed.
- `rg 'ON CONFLICT.*gmail_message_id\|gmail_message_id.*ON CONFLICT' src/mailpilot/database` -> conflict handling present
- `rg 'get_email_by_gmail_message_id\b' src/mailpilot/sync.py src/mailpilot/gmail.py` -> recovery fn called
- `rg 'orphan_gmail_send\b' src/mailpilot/sync.py src/mailpilot/gmail.py` -> orphan error event logged

## §V78 — outbound MIME headers + thread_id threading

Outbound MIME stamped `X-MailPilot-Version` always + `X-MailPilot-Account-Id` when account-bound. Replies set `In-Reply-To` + `References` (`References` defaults to `in_reply_to`). Send path threads via optional `thread_id` — `send_email` agent tool + `_wrap_send_email` + `email_ops.send_email` forward `thread_id` to `sync.send_email`. Supplied without explicit `in_reply_to` → `_resolve_threading_headers` derives `In-Reply-To`/`References` from prior local thread rows. Multi-touch outbound threads natively: capture `gmail_thread_id` on touch 1, pass as `thread_id` on later touches; no `contact_id` requirement (unlike `reply_email` §V.79).

Trigger: `src/mailpilot/sync.py` or `src/mailpilot/gmail.py` changed.
- `rg 'X-MailPilot-Version\b' src/mailpilot/sync.py src/mailpilot/gmail.py` -> version header stamped
- `rg '_resolve_threading_headers\b' src/mailpilot/sync.py src/mailpilot/gmail.py` -> threading header resolver present
- `rg 'thread_id\b' src/mailpilot/agent/tools.py | grep send_email` -> thread_id param on send_email tool

## §V79 — send/reply guards + account soft-disable lifecycle

Send/reply guards: disabled contact OR disabled account blocks send + reply; cold-send cooldown 30 days per (account, contact, workflow); cooldown typed err = `email_ops.CooldownError` (`exceptions.py` has no `CooldownError` or unused `ClassificationError`); reply requires original `gmail_thread_id` + `contact_id` (typed errors); reply subject gets "Re: " prefix unless already prefixed, case-insensitive.

Account soft-disable: `account.disabled_reason TEXT NULL` (non-NULL = disabled, carries reason). `account disable <ref> --reason <text>` sets it (disabled_reason IS NULL gate blocks double-disable). `account enable <ref>` clears it (disabled_reason IS NOT NULL gate blocks enabling an active account). A disabled account is gated everywhere it would touch Gmail — sync loop skips it, `account sync` all-accounts mode skips it, `renew_watches()` skips it, send + reply refuse it. `account list` default-hides disabled; `--include-disabled` opts in. Operator-only — the agent never disables or enables an account.

Trigger: `src/mailpilot/sync.py`, `src/mailpilot/database`, or `src/mailpilot/agent/invoke.py` changed.
- `rg 'disabled_reason\b' src/mailpilot/schema.sql | grep account` -> account soft-disable col
- `rg 'cold.send.*cooldown\|cooldown.*30\|30.*days' src/mailpilot/sync.py src/mailpilot/database` -> cooldown gate
- `rg 'class CooldownError' src/mailpilot/email_ops.py` -> cooldown typed err present
- `rg 'class CooldownError|class ClassificationError' src/mailpilot/exceptions.py` -> zero hits
- `rg '"account".*"disable"\b\|"account".*"enable"\b' src/mailpilot/cli` -> both verbs present
- `rg 'disabled_reason.*skip\|account.*disabled.*sync\|sync.*skip.*disabled' src/mailpilot/sync.py` -> sync loop skip gate
- `rg 'include.disabled\b' src/mailpilot/cli | grep account` -> account list --include-disabled

## §V80 — bounce/unsubscribe handling + contact disable

Bounce detection: sender local-part in {mailer-daemon, postmaster} (case-insensitive) OR label contains "BOUNCE" → most recent outbound in same thread + account marked `bounced` + contact disabled with "bounced:" reason prefix. Unsubscribe path uses "unsubscribed:" prefix. `contact enable <ref>` clears `disabled_reason` regardless of prefix (operator owns consent, no unsubscribe carve-out). `disabled_reason IS NOT NULL` gate blocks re-enabling an active contact. Operator-only — the agent disables on bounce/unsubscribe, never re-enables. Enrollment terminate on bounce lives in §V.163 (this row owns detect + email bounced + contact disable only).

Trigger: `src/mailpilot/routing.py` or `src/mailpilot/sync.py` changed.
- `rg 'mailer-daemon\|postmaster\|BOUNCE\b' src/mailpilot/routing.py src/mailpilot/sync.py` -> bounce detection strings
- `rg '"bounced:"\|"unsubscribed:"' src/mailpilot/database src/mailpilot/sync.py src/mailpilot/routing.py` -> reason prefixes
- `rg 'enable_contact\b' src/mailpilot/agent/tools.py` -> zero hits (re-enable is operator-only, not agent tool)

## §V83 — execute_task pre-flight cancellation

execute_task pre-flight cancels the task (zero LLM calls) when: workflow inactive/missing; contact disabled/missing; enrollment missing or status != active. Touch tasks (context.touch) additionally cancelled when the latest enrollment outcome is terminal OR an inbound email from the contact arrived after the prior touch — belt complementing reply-time cancellation (§V.123). inbound-after excludes OOO/auto-reply inbound (V169 detect) — pause not reply; operator retry of cancelled cadence T2 may proceed. Inbound-after uses `list_inbound_emails_from_contact_after`; no `has_inbound_email_from_contact_after` bool wrapper; tests call the list helper.

Trigger: `src/mailpilot/run.py` changed.
- `rg 'status="cancelled"' src/mailpilot/run.py` -> pre-flight cancel sites present
- `rg '_touch_cancel_reason' src/mailpilot/run.py` -> touch-specific guard fn present
- `rg 'has_inbound_email_from_contact_after' src/mailpilot tests` -> zero hits
- `rg 'list_inbound_emails_from_contact_after' src/mailpilot/run.py tests/test_database.py` -> list helper present

## §V90 — natural-key UNIQUE constraints

UNIQUE: `account.email`, `company.domain`, `contact.email`, `workflow.name` (globally unique, kebab = `*.toml` file stem §V.103), `enrollment(workflow_id, contact_id)`, `email.gmail_message_id` (nullable-unique). `tag.name` globally unique (vocabulary row §V.116). `tag_assignment` UNIQUE per (tag_id, owner). These natural keys = canonical CLI identifiers — case-insensitive handles resolved polymorphic (§V.107); unknown key → `not_found` (§V.94).

`contact.email` natural key canonicalized lowercase at every write + lookup — `create_contact`, `get_contact_by_email`, `create_or_get_contact_by_email`, `create_contacts_bulk`, `get_contacts_by_emails` lowercase the `email` arg before the `contact.email` match|insert; sync sender→contact resolve feeds the same normalized key. Mirrors `email.sender` lowercase persist + CLI polymorphic case-insensitive resolution (§V.107). `contact.email` `TEXT UNIQUE` is case-sensitive, so write-path lowercase (NOT the constraint) is the case-variant dedup guard; case-variant `From` (Outlook/Exchange recase local-part) never mints a duplicate bare contact (closes §B.121).

Trigger: `src/mailpilot/schema.sql` or `src/mailpilot/database` changed.
- `rg 'UNIQUE.*email\b' src/mailpilot/schema.sql` -> account + contact email UNIQUE
- `rg 'UNIQUE.*domain\b' src/mailpilot/schema.sql` -> company domain UNIQUE
- `rg 'UNIQUE.*gmail_message_id' src/mailpilot/schema.sql` -> email nullable-unique
- `rg 'UNIQUE.*tag_id.*owner\|UNIQUE.*owner.*tag_id' src/mailpilot/schema.sql` -> tag_assignment UNIQUE per pair
- `rg -n 'email\.lower\(\)|lower\(email' src/mailpilot/database` -> contact natural-key fns lowercase before match|insert

## §V95 — contact lead-metadata flat columns

`contact.title TEXT NULL` (role label); `contact.email_confidence INT NULL`, schema CHECK `email_confidence BETWEEN 0 AND 100`. NULL = Bouncer unknown (unbilled, unverified) = high risk. `email_confidence` = sole email-risk score. `contact list --max-email-confidence N` surfaces `email_confidence <= N OR IS NULL` — SQL inequality alone (NULL excluded) is the trap; admit-all (§V.96) never drops unknowns. No `ContactProfile` model.

Trigger: `src/mailpilot/models.py` or `src/mailpilot/database` changed.
- `rg 'email_confidence\b.*INT\|title\b.*TEXT' src/mailpilot/schema.sql` -> flat cols (not JSONB)
- `rg 'email_confidence.*BETWEEN.*0.*100\|CHECK.*email_confidence' src/mailpilot/schema.sql` -> schema CHECK
- `rg 'max.email.confidence\b' src/mailpilot/database` -> filter option present
- `rg 'IS NULL.*email_confidence\|email_confidence.*IS NULL' src/mailpilot/database` -> NULL-inclusive in filter

## §V96 — lead-contacts discovery + negative-verdict memoization

Discover set = `company list --has-profile --max-contacts 4 --no-tag no-contacts-found --no-tag contacts-exhausted` (one call, expressible as a single query). CompanySummary `contact_count` = LEFT JOIN contact COUNT including disabled (tracks memoization rule, not active-only). `--max-contacts N` and `--min-contacts N` are inclusive. <=5 contacts/company/run. Admit-all — every discovered+verified email → contact row, low/NULL `email_confidence` flags risk in summary but never gates admission. Negative-verdict memoization branches on typed `reason_code` (NOT free-text prefix): `no_decision_makers` → tag `no-contacts-found`; `all_already_seeded` (contacts_created==0) → tag `contacts-exhausted`; `status=failed` NEVER tags (retryable). Both tags applied at run end. `company list --no-tag` is repeatable (Click `multiple=True`).

Trigger: `.claude/skills/lead-contacts/**` or `.claude/skills/lead-companies/**` changed.
- `rg 'no-tag no-contacts-found.*no-tag contacts-exhausted|no-contacts-found.*contacts-exhausted' .claude/skills/lead-contacts/SKILL.md` -> both exclusion tags in discover query
- `rg 'no_decision_makers|all_already_seeded' .claude/skills/lead-contacts/SKILL.md .claude/workflows/lead-contacts-find.js` -> typed reason_code present in both
- `rg 'multiple.*True\|no.tag.*multiple' src/mailpilot/cli` -> `--no-tag` is repeatable

## §V99 — Skill-path resolution check

Trigger when `.grok/skills/**/*.md` changed.

Checks:
(i) Script refs (`uv run python .grok/skills/**/scripts/*.py`) each resolve on disk.
(ii) Source-of-truth dirs named in prose or runtime recovery gates exist.

Cited-but-absent path = recovery instruction that errors when the operator needs it.

Mechanical greps (manual judgment on hits):
- `rg -n '\.grok/skills/\S+\.py' .grok/skills/` -> each cited `.py` path must exist at that path.
- Backticked dir refs: `rg -n '`\.grok/skills/[^`]+/`' .grok/skills/` -> each cited dir must exist on disk. Non-dir backtick refs exempt.

## §V100 — Skill-body progressive-disclosure audit

Trigger when `.grok/skills/**/*.md` changed.

Checks:
(i) Body >~500 lines = VIOLATE (procedure buried among run-end-only material -> extract to `references/*.md`).
(ii) Near-verbatim prose shared across sibling skills MUST live in one shared `references/*.md` both cite, not copied per-skill.

Mechanical checks:
- `wc -l .grok/skills/*/SKILL.md | sort -rn` -> flag files over 500 lines for extraction review.
- `rg -c 'Conventions|batch gate|Next block' .grok/skills/*/SKILL.md` -> any term in multiple skill bodies -> check for shared-reference extraction opportunity.

## §V101 — must-sense ` ! ` ban

Mechanical audit; trigger when `.grok/skills/**/*.md` changed. Scope = skill-body prose. A hard requirement ! be marked w/ an explicit word (`MUST` / `required`); a bare telegraph ` ! ` (must-glyph) in prose reads as negation in code, so a model executing the skill can invert the constraint (silent constraint flip — the failure §V.101 was authored to block).

Exempt (not flagged):
- backticked / fenced-code ` ! ` — `[ ! -f ]`, `!=`, `! cmd` inside an inline-code span or a ```fence``` (shell test / negation operator, not a prose obligation).
- SPEC.md + this file (`.spec/check-extras.md`) — telegraph register, both outside the `.grok/skills/**` scope; their ` ! ` is the authored must-glyph, not a violation.

Checks:
(i) zero bare must-sense ` ! ` in `.grok/skills/**/*.md` prose. Fail mode: ` ! ` standing in for "must" in an instruction line (e.g. `the body below meta ! stay byte-identical`, `CSV mode ! parse with an RFC-4180 parser`) — convert to `MUST`.

Mechanical grep (manual judgment on hits — flag only must-sense prose, not backticked/fenced shell):
- `rg -n ' ! ' .grok/skills/` -> classify each hit: backticked-shell / fenced-code -> exempt; prose obligation -> (i) fail (convert to `MUST`). Zero hits -> pass.

## §V102 — Skill frontmatter hygiene audit

Trigger when `.grok/skills/**/*.md` changed.

Checks:
(i) Every `.grok/skills/**/SKILL.md` sets `allowed-tools` (scoped safety rail).
(ii) Every `.grok/skills/**/SKILL.md` sets `argument-hint` (invocation shape).
(iii) `description:` = triggering intent; vendor names + pipeline-stage rosters belong in body.

Mechanical greps (manual judgment on hits):
- `rg --files-without-match 'allowed-tools' .grok/skills/*/SKILL.md` -> each listed file = missing key (VIOLATE). (`rg -L` is `--follow`, not files-without-match — it prints matches, inverting the read.)
- `rg --files-without-match 'argument-hint' .grok/skills/*/SKILL.md` -> each listed file = missing key (VIOLATE).
- `rg -n '^description:' .grok/skills/*/SKILL.md` -> review for vendor roster or full pipeline-stage detail in trigger text.

## §V103 — workflow definition files

Workflow defs = `workflows/*.toml`, 1 file/workflow, pure TOML (stdlib `tomllib`, no new dep). Fields = Workflow row 1:1: `{name, template, theme, goal, instructions, touches, touch_interval_days}`, `instructions` = TOML multi-line literal string; cadence pair `touches` + `touch_interval_days` int, nullable — NULL/omitted = single-touch, no auto follow-up (§V.136). `name` = canonical cross-environment key (§V.107): import enforces `name` kebab-shaped (lowercase, hyphen-separated, no dot/at-sign/UUID-shape) AND equal to the `*.toml` file stem (`{name}.toml`), globally unique — identical in dev and prod because both import the same file. `workflow import --file X.toml` → one row + shared validation (malformed/missing-required, or `name` not kebab|not file-stem → `validation_error`, no partial write). Def fields `{name, template, theme, goal, instructions, touches, touch_interval_days}` import-only: `workflow update` mutates non-def fields only (status, account binding); rename = rename file + re-import. File = sole source of truth, so no `row_ahead` drift state (§V.134). `--file <dir>` recurses `**/*.toml` (batch, per-row errors continue; covers `campaigns/<slug>/workflows/<slug>.toml`; name==stem still file-stem). Immediate-child-only glob retired. Terminal envelope aggregates: top-level int `applied` (rows w/o `error`) + `rejected` (rows w/ `error`) on every import envelope; per-row applied object `{name, action, in_sync, catalog_hash, row_hash, changed}` — `action` ∈ {created, updated, unchanged}; `in_sync` = live written-row SHA-256 vs catalog (same hash as §V.134 check; live-row hash = `_compute_workflow_wording_hash` on stored columns as-is, no second catalog-default pass; catalog defaults only inside `catalog_def_fields`; remaining/changed same equality as hash; true after successful create/update of a complete def); `catalog_hash`/`row_hash` hex; `changed` = mutated def-field excerpts when in_sync true (instructions excerpt enough to confirm ready-copy w/o `workflow view`; empty map when unchanged); in_sync false → `changed` remaining keys still differing (not only just-written); omitted or incomplete cadence pair persists as single-touch NULL/NULL so the hash matches a legal row; error rows keep `{name, error, message}`. `applied`=0 (all rows rejected | zero rows parsed) → `import_failed` error envelope on stderr, per-row rows inlined under `workflows`, exit 1 (§V.4 error path; report-inline mirrors `db check` §V.109); `applied`>=1 → ok:true exit 0, per-row errors stay inline; `record_count` = `workflows` array len (multi-key payload, §V.4). `workflow export --account-email A --out-dir D` writes one `*.toml`/workflow (name-sorted) + JSON status envelope on stdout. Export→dir→import round-trip idempotent. `workflows/` = gitignored symlink → independent repo kborovik/workflows @ /Users/kb/github/workflows (not a submodule, no submodule pointer). Root `workflows/*.toml` (CRM defs) distinct from `.claude/workflows/*.js` (Claude Code orchestration scripts).

Trigger: `src/mailpilot/cli` or `workflows/` changed.
- `rg 'tomllib' src/mailpilot/cli src/mailpilot/database` -> stdlib tomllib (no tomlkit/toml dep)
- `rg '"--file".*toml\|toml.*"--file"' src/mailpilot/cli` -> import/export --file flag present
- `rg 'json|JSON' src/mailpilot/cli | grep -i 'workflow import\|workflow export'` -> zero hits (TOML-only, no JSON import)
- `rg 'import_failed' src/mailpilot/cli` -> zero-applied loud-failure aggregate present
- `rg 'rglob' src/mailpilot/cli` -> recursive `**/*.toml` discovery under --file dir
- `rg '"in_sync"' src/mailpilot/cli` -> import per-row in_sync present
- `rg 'catalog_hash' src/mailpilot/cli` -> import per-row catalog_hash present
- `rg 'row_hash' src/mailpilot/cli` -> import per-row row_hash present
- `rg 'workflow_import_sync_report\|catalog_def_fields' src/mailpilot/cli src/mailpilot/database` -> live-row hash shared with check
- `rg '_persisted_wording_hash' src/mailpilot/database` -> live-row hasher present

## §V104 — reply-test reply-loop guard

Live reply-test (`.grok/skills/mailpilot-reply-test`) requires `outbound@lab5.ca` have no active workflow. `inbound-google-drive` agent reply lands in outbound mailbox → `skipped_no_workflows` (§V.76), no second reply. Any active outbound workflow re-enters routing → inbound↔outbound auto-reply loop. No-outbound-workflow is a load-bearing test precondition, not incidental.

Trigger: `.grok/skills/mailpilot-reply-test/**` changed.
- `rg 'outbound@lab5.ca\b' .grok/skills/mailpilot-reply-test/SKILL.md | grep -i 'no.*workflow\|precondition'` -> guard stated in skill body
- `rg 'skipped_no_workflows\b' .grok/skills/mailpilot-reply-test/SKILL.md` -> expected routing outcome named

## §V105 — mailpilot-reply-test grading model

In-scope cases graded deterministically: `score_replies.py` checks expected-token substring presence at runtime; false-PASS-at-worst, never false-FAIL. `expected_tokens` MUST be atomic: each token a single contiguous value the reply cannot restructure away — allowlist = {model id, bare number, number+short-unit (with optional short qualifier), label <=2 words}. NOT a `Label (Qualifier)` header (§B.102), a 3-plus-word phrase, a verb-bearing sentence fragment, or a layout-dependent phrase. Atomicity enforced test-time, NOT in the runtime grader: `_is_brittle_inscope_token` allowlist (not denylist) lives in `tests/test_reply_test_scoring.py`, and `test_inscope_expected_tokens_are_atomic` iterates the live QA-Pairs.json tokens (§B.117). `select_cases.py` selection guard (>=2 tokens, len>=5) keeps real signal after brittle tokens split. Out-scope + compare cases: `score_replies.py` emits advisory signals (token_hits, fabrication_candidates, has_table) but NOT verdicts. Judge subagent reads {reply body, case rubric, signals, source datasheet} → {verdict PASS|FAIL, rationale} (verdict of record for NL-shaped cases).

Trigger: `.grok/skills/mailpilot-reply-test/scripts/score_replies.py` or `tests/test_reply_test_scoring.py` changed.
- `rg '_is_brittle_inscope_token\|allowlist' tests/test_reply_test_scoring.py` -> allowlist logic present (not denylist); the atomicity guard is test-time, NOT in score_replies.py (§B.117)
- `rg 'advisory\|emit.*signal\|signal.*emit' .grok/skills/mailpilot-reply-test/scripts/score_replies.py` -> advisory signals, not verdicts, for out-scope/compare
- `rg 'judge.*subagent\|subagent.*judge\|verdict.*judge' .grok/skills/mailpilot-reply-test/SKILL.md` -> judge subagent for NL-shaped verdict

## §V106 — Drive search whitespace-tokenized OR-joined predicates

`search_drive_markdown` query = whitespace-tokenized; each token generates `fullText contains '<token>'`; all tokens OR-joined; raw hyphenated token retained (hyphenated model tried whole + split); results union+deduped by file_id; ~8-token cap. Single salient term surfaces the file. NEVER a single whole-phrase `fullText contains '{query}'` predicate — Drive punctuation-tokenizes + AND-joins internally → false-negatives on hyphenated/multi-word queries.

Trigger: `src/mailpilot/drive.py` changed.
- `rg 'fullText contains\b' src/mailpilot/drive.py` -> OR-joined token predicates
- `rg '\.split()\b' src/mailpilot/drive.py | grep -i search` -> whitespace tokenization
- `rg 'file_id.*set\b\|dedupe\b' src/mailpilot/drive.py | grep search` -> union+dedupe by file_id

## §V107 — CLI entity reference + polymorphic resolver

Keyed entities (account=email, company=domain, contact=email, tag=name, workflow=name §V.103) addressed by natural key. Keyless entities (email, note, task, enrollment) addressed by UUID. Polymorphic resolver: value matching UUIDv7 shape (`8-4-4-4-12` hex) → resolve by id; any other value → resolve by natural key (domain has dots, email has at-sign, workflow `name` is kebab with neither nor UUID-shape — never collide), case-insensitive. Unknown key → `not_found`. Every single-entity verb target = positional `<key>` arg, NEVER `--<entity>-id` option. Scope/owner options named for owner natural key (`--company-domain`, `--contact-email`). Account-requiring cmds take a single `--account-email` (polymorphic, resolves email|UUID). `account sync --account-email` is optional (all accounts when omitted). `account sync --since <iso>` bounds full-INBOX backfill on first sync.

One `_resolve(conn, ref, *, get_id, get_key, noun, missing="error")` covers hard (`output_error` not_found) and soft (`None` on miss) lookup; soft vs hard differs only on miss. `_resolve_*_id` = fetched row `.id` (UUID-shape still fetches; pass-through unfetched = drift). `_resolve_workflow` always loads the row (UUID existence in the resolver, not re-checked at call sites). `email send` + `email reply` `--workflow-id` name|UUID via same resolver; UUID-only `get_workflow(connection, raw_flag)` without resolve = drift. Unknown UUID-shaped id still `not_found`.

`--workflow-id` on list/filter/mutate surfaces resolves name or UUID via `_resolve_workflow` / `_resolve_workflow_id` (workflow keyed by `name` §V.90/§V.103 — flag retains `-id` historically but accepts natural key). Help text: "name or ID". Unknown name or UUID → `not_found` (same envelope). Complying surfaces: `enrollment add` + `activity list` + `enrollment list` + `task list` + `task stats` + `email list` + `email send` + `email reply` (#211, #213, #258).

Trigger: `src/mailpilot/cli` changed.
- `rg '"--\w+-id"' src/mailpilot/cli` -> only `--workflow-id` (keyless) present; no `--company-id`, `--contact-id`, `--account-id` options
- `rg '"--account-email"' src/mailpilot/cli | wc -l` -> single polymorphic `--account-email` on account-requiring cmds
- `rg 'def _resolve\(' src/mailpilot/cli` -> one `_resolve` covers hard+soft
- `rg 'polymorphic\|UUIDv7.*shape\|8-4-4-4-12\|_is_uuid\|uuid.*shape' src/mailpilot/cli` -> UUID-shape resolver present
- `rg '_resolve_workflow' src/mailpilot/cli` -> always-load workflow resolver; send|reply + list/filter surfaces resolve name|UUID before get

## §V108 — migration registry + schema-hash re-stamp

`migrations/NNN_*.sql` forward-only (monotonic int prefix, no down-migrations, shipped in wheel). `db migrate` applies pending in order, each in own transaction, records `schema_migrations(version PK, name, applied_at, mailpilot_version)`. On success re-stamps `schema_metadata.schema_hash` + `mailpilot_version` to canonical `schema.sql` hash — re-baselines even at 0-pending when every migration is applied but recorded hash is stale (prevents phantom drift). `schema.sql` = canonical declarative full-schema. Identity invariant: fresh `db init` from `schema.sql` == apply-all-migrations-from-zero, byte-identical structure (test-enforced).

Trigger: `src/mailpilot/database` or `migrations/` changed.
- `rg 'schema_migrations\b' src/mailpilot/database` -> ledger table referenced
- `rg 're.stamp.*schema_hash\|schema_hash.*re.stamp\|re-stamp' src/mailpilot/database` -> re-stamp on migrate present
- `ls migrations/*.sql | sort` -> monotonic NNN_ prefix on all files
- `rg 'test.*identity\|db init.*migrate.*identical\|migrate.*init.*identical' src/mailpilot/tests/` -> byte-identity test present

## §V109 — three-state schema verdict + tiered gate

Verdict in {current, pending, drift}. `_read_schema_metadata` breakout: metadata-row-missing vs table-missing → None collapse avoided — ledger-behind = `pending`, hash-mismatch or manual-edit = `drift`. Read-only diagnosis (`status`, `db check`) tolerates + reports. `run` + every CLI mutation dead-stops: drift → `schema_drift` envelope + exit 1; pending → `schema_migration_pending` envelope + exit 1. Two distinct codes since remedy differs (drift = investigate divergence, pending = run `db migrate`). Fail at startup, not mid-batch.

Trigger: `src/mailpilot/database` changed.
- `rg 'schema_drift\b' src/mailpilot/database src/mailpilot/cli` -> drift code present
- `rg 'schema_migration_pending\b' src/mailpilot/database src/mailpilot/cli` -> pending code present (distinct from drift)
- `rg 'determine_schema_verdict\b\|_read_schema_metadata\b' src/mailpilot/database` -> verdict fn present
- `rg '"current"\b\|"pending"\b\|"drift"\b' src/mailpilot/database | grep verdict` -> three-state values

## §V110 — initialize_database off the hot path

`initialize_database()` = connect + verify, NOT provision. Empty-DB auto-provision fires only when `account` table is absent (data-loss-free; keeps `make clean` + test fixtures ergonomic). Populated DB never mutates structure as a connection side-effect. Explicit forward paths: `db init` (provision empty, refuses if `account` exists, no `--force`) + `db migrate` (advance populated).

Trigger: `src/mailpilot/database` changed.
- `rg 'initialize_database\b' src/mailpilot/database` -> fn present
- `rg 'information_schema.*account\b\|table.*account.*exist' src/mailpilot/database` -> empty-DB gate on `account` table absence
- `rg '_provision_schema\b' src/mailpilot/database` -> provision fn separate from initialize

## §V111 — CLI help agent surface

Top-level `mailpilot --help` emits packaged `src/mailpilot/SKILL.md` body verbatim (plain text stdout, exit 0). Missing package data → stderr hard-fail exit 1. No `--skill` flag (retired — content lives only under top-level `--help`). SKILL.md = LLM-agent CLI reference (grammar, JSON envelope, exit codes, settings, recipes); register dense agent prose, zero SPEC §-cites in the body. Subcommand/verb `--help` stays Click-rendered (docstring + option `help=`). Every Click command/group `--help` renders free of `§V/§T/§B.<n>` (operator-facing twin of §V.45 agent-prompt text).

Trigger: `src/mailpilot/cli` or `src/mailpilot/SKILL.md` changed.
- Top-level: render `mailpilot --help` → stdout byte-identical to package `SKILL.md`; exit 0; no `--skill` option on root group
- `rg '§[VTB]\.[0-9]+' src/mailpilot/cli | grep -v '^\s*#'` -> classify each hit: in a Click `help=` string or docstring → fail; in a `#` comment → exempt
- `rg '§[VTB]\.[0-9]+' src/mailpilot/SKILL.md` -> zero hits
- Full guard: walk Click tree (each sub-command `--help`, not root), grep rendered output for `§[VTB]` pattern → zero hits

## §V112 — lead-companies scoped enrich-scope

Domain/URL/UUID args enrich ONLY rows resolved or seeded this run, never the global profile-NULL backlog. `seed_companies.py` emits `seeded_stale` (rows created/matched this run via `touched_apexes` accumulator) distinct from global `stale`. Fast path feeds `seeded_stale` for domain/URL-token runs, `stale` for file/bare runs. Global stale set is never the dispatch fan-out for a scoped arg.

Trigger: `.claude/skills/lead-companies/**` changed.
- `rg 'seeded_stale\b' .claude/skills/lead-companies/scripts/seed_companies.py` -> seeded_stale present (not global stale)
- `rg 'touched_apexes\b' .claude/skills/lead-companies/scripts/seed_companies.py` -> accumulator present
- `rg 'backlog\b\|global.*stale\|stale.*global' .claude/skills/lead-companies/SKILL.md` -> explicit not-backlog statement

## §V113 — Bouncer single GET per contact

Bouncer email verify = real-time single GET `/v1.1/email/verify?email=` per contact (at most 5 per company per run; per-email billing). NEVER POST `/email/verify/batch/sync`. Empty body, 4xx/5xx, or missing status = verify FAILURE (retry once, then NULL with noted reason) — never a clean Bouncer `status="unknown"`.

Trigger: `.claude/skills/lead-contacts/**` changed.
- `rg 'batch/sync\|batch.sync' .claude/skills/lead-contacts/` -> zero hits (POST batch absent)
- `rg '/v1.1/email/verify\b' .claude/skills/lead-contacts/` -> single-GET present
- `rg 'retry.*once\|once.*retry' .claude/skills/lead-contacts/SKILL.md .claude/skills/lead-contacts/scripts/` -> single retry on failure

## §V114 — company soft-disable

`company.disabled_reason TEXT NULL` (non-NULL = disabled, carries reason). `company disable <id> --reason <text>` sets it (disabled_reason IS NULL gate blocks double-disable, mirrors §V.10). `company list` hides disabled unless `--include-disabled`. `company enable <ref>` clears `disabled_reason` (disabled_reason IS NOT NULL gate blocks re-enabling an active company). Part of uniform disable/enable verb pairing across company|contact|tag|enrollment (§V.10/§V.15/§V.80). Operator-only — lead-contacts negative-verdict memoization moved to the `no-contacts-found` tag (§V.96, §V.116).

Trigger: `src/mailpilot/cli` or `src/mailpilot/database` changed.
- `rg 'disabled_reason\b' src/mailpilot/schema.sql | grep company` -> disabled_reason col on company table
- `rg '"company".*"disable"\b\|"company".*"enable"\b' src/mailpilot/cli` -> both verbs present
- `rg 'include.disabled\b' src/mailpilot/cli | grep company` -> --include-disabled on company list
- `rg 'IS NULL.*disabled_reason\|disabled_reason.*IS NULL' src/mailpilot/database | grep company` -> double-disable gate

## §V115 — CLI list filter six-family taxonomy

Six families, each with fixed naming + semantics:
1. Scope: `--<owner-natural-key>` or `--<noun>-id <UUID>` for keyless parent; resolves polymorphic (§V.107); absent parent → `not_found`.
2. Enum: `--<axis>` `type=click.Choice` mirroring schema CHECK set; never free string.
3. Range: `--min-<field>`/`--max-<field>`, both inclusive + composable; NULL-inclusive where nullable + meaningful.
4. Presence: `--has-<field>/--no-<field>` single tri-state `default=None`; Click derives `has_<field>` param from positive side.
5. Text-match: field-named, exact only on `list`, case-fold per natural-key semantics; substring/fuzzy → `search` verb only.
6. Lifecycle: `--include-disabled` (is_flag False) + `--since`/`--until <ISO>` closed inclusive interval over one declared column.

Result-control set (not filters): `--limit <int>` (default 100 unless noun opts higher — company list|search default 500 per §V.148), `--offset <int>` (default 0), `--sort` (noun-declared Choice; absent → noun default order), `--desc` (is_flag; flips ASC→DESC). `contact|email|workflow search` uses the same `--limit` result-control as list (default 100). `record_count` = page length only (no total/has_more MVP). `--direction` = canonical inbound/outbound axis across email + workflow + template. Families realized as shared Click decorators (`limit_option`, `offset_option`, `sort_option`, `desc_option`, `time_window_options(col)`, `include_disabled_option`, `scope_option`, `enum_option`, `range_options`, `presence_option`) composed fixed-order in `cli/`/`_filters.py`. New list flag = new vocabulary decorator or spec change.

Trigger: `src/mailpilot/cli` or `src/mailpilot/_filters.py` changed.
- `rg 'limit_option|time_window_options|include_disabled_option|scope_option|enum_option|range_options|presence_option' src/mailpilot/` -> all 7 base decorator names present
- `rg 'offset_option|sort_option|desc_option' src/mailpilot/_filters.py` -> result-control decorators present
- `rg '"--direction"' src/mailpilot/cli` -> present on email|workflow|template list (no `--type`)
- `rg '"--route-method".*Choice\|click\.Choice.*route.method' src/mailpilot/cli` -> route-method is a Choice not free string
- `rg '"--limit"' src/mailpilot/cli | wc -l` -> present on every list cmd

## §V116 — tags controlled vocabulary

Two tables: `tag` (vocabulary, one row/defined tag, `name` globally unique §V.90, soft-delete via `disabled_reason`) + `tag_assignment` (link, one row/(tag, owner), owner XOR company|contact). CLI verbs: `tag create <name>`, `tag view`, `tag disable <name>`, `tag enable <name>`, `tag add`, `tag remove`, `tag list`, `tag search`. `tag add` errors `not_found` on undefined tag, NEVER auto-creates. `tag list` = vocabulary + projected `usage_count`. `company list --tag` / `contact list --tag` = membership filter, repeatable, AND-compose (row ! carry every named tag). `company list --no-tag` / `contact list --no-tag` = negated membership filter, repeatable, AND-compose (carry none of the named tags). Both resolve through vocabulary (undefined → `not_found`). `company list|view` + `contact list|search|view` project assigned tags as `tags[]` names (empty ok; list/view shape identical; company same as `db export` company.tags) — membership filter alone ! substitute for projection.

Trigger: `src/mailpilot/cli` or `src/mailpilot/database` changed.
- `rg '"tag"\b.*"create"\|"tag create"' src/mailpilot/cli` -> all verbs registered
- `rg 'not_found.*tag\b\|tag.*not_found' src/mailpilot/cli src/mailpilot/database` -> `not_found` on undefined (no auto-create)
- `rg '"--no-tag".*multiple.*True\|multiple.*True.*"--no-tag"' src/mailpilot/cli` -> `--no-tag` is repeatable
- `rg 'tags' src/mailpilot/models.py | rg 'CompanySummary|CompanyView|ContactSummary|ContactView'` -> company+contact list/view project tags

## §V117 — batch-gate option distinctness

Mechanical audit; trigger when `.claude/skills/lead-companies/**` or `.claude/skills/lead-contacts/**` changed. Scope = the shared Batch-gate § in `.claude/skills/lead-companies/references/lead-pipeline-conventions.md`.

`§B.98`: the unconditional fixed cap (`First 24`) capped to all rows once the stale-count reached the cap, so `First 24` and `All <N>` dispatched one identical batch. §V.117 fixes this pre-cap (during option construction): every gate option maps to a distinct batch at the current stale-count, so a fixed-cap option is suppressed once its cap reaches the stale-count (`First 24` dropped at stale-count <= 24, == `All <N>` there; `First 9` always distinct since the gate fires only at stale-count > 9). Per §V.100 the rule lives once in the conventions file; the sibling SKILL bodies cite it, they do not restate the suppression mechanics.

Mechanical checks (over the conventions file Batch-gate §):
- `rg -n 'distinct batch' .claude/skills/lead-companies/references/lead-pipeline-conventions.md` -> at least one hit (the distinct-batch rule is stated).
- `rg -n 'stale-count > 24' .claude/skills/lead-companies/references/lead-pipeline-conventions.md` -> at least one hit (`First 24` offered only above its cap).
- `rg -n 'stale-count <= 24' .claude/skills/lead-companies/references/lead-pipeline-conventions.md` -> at least one hit (`First 24` dropped at/below its cap).
- `rg -n 'stale-count <= 24' .claude/skills/lead-companies/SKILL.md .claude/skills/lead-contacts/SKILL.md` -> zero hits (rule not duplicated into a SKILL body, §V.100).

## §V119 — make clean is a deliberate wipe

`make clean` drops and recreates the databases. It does not auto-run `mailpilot db export`. Operator snapshot remains `mailpilot db export --file <path>` (§V.121); restore = `mailpilot db import --file <snap>`. Makefile has no `db-backup`, `config-backup`, or `env-backup` targets (`app_config` is the settings store; secrets restore from `pass mailpilot/` after reset).

Trigger: `makefile` changed.
- `rg '^(db-backup|config-backup|env-backup)\s*:' makefile` -> zero hits
- `rg 'clean:.*db-backup' makefile` -> zero hits

## §V120 — send-obligation guard

Every send-obligated trigger turn MUST leave a `reply_email`|`send_email` ToolReturnPart without an `error` key, OR a successful `noop` ({acknowledged: true}), OR a `conclude_enrollment` terminal (§V.127). Send-obligated (walker scope) = inbound (`email is not None`, trigger in {email, task}); outbound first reach-out (trigger in {enrollment_run, enrollment_schedule}, `email is None`) runs compose-only — harness sends the validated TouchMessage itself, obligation structural, not walker-checked (§V.136). `manual` trigger exempt. Guard `_sent_reply(result)` walks `result.all_messages()` after the §V.81 tool-count check; none of the above → raise `AgentCompletedWithoutReplyError`. Class is non-transient → `_handle_agent_failure` takes it terminal `failed` + `operator_event("error")`, NEVER silent completed. Prompt-side preventive = `_MUST_SEND` template fragment (§V.45).

Trigger: `src/mailpilot/agent/invoke.py` changed.
- `rg '_sent_reply\b' src/mailpilot/agent/invoke.py` -> guard present
- `rg 'AgentCompletedWithoutReplyError' src/mailpilot/exceptions.py` -> exception defined
- `rg 'enrollment_run\|enrollment_schedule' src/mailpilot/agent/invoke.py` -> outbound first-reach-out triggers dispatch compose-only shape (§V.136), walker scope inbound-only
- `rg '"manual"\b.*exempt\|trigger.*manual.*exempt\|manual.*skip' src/mailpilot/agent/invoke.py` -> manual exempt
- `rg 'conclude_enrollment.*_sent_reply\|_sent_reply.*conclude_enrollment' src/mailpilot/agent/invoke.py` -> conclude_enrollment in walker

## §V121 — db snapshot bundle

`db export --file <path>` writes one JSON bundle + `{"db":{path, companies:N, contacts:M, tags:K}, "ok":true}` status to stdout (singular envelope, not plural). `db import --file <path>` restores fixed code order: tags → companies → contacts. Bundle format: `{schema_version:int, exported_at:ts, tags:[{name, disabled_reason}], companies:[{...profile, disabled_reason, tags:[name,...]}], contacts:[{...title, email_confidence, disabled_reason, company_domain, tags:[name,...]}]}`. Scope = tag vocabulary + company + contact ONLY (emails, workflows, enrollments, tasks, accounts excluded). Every link resolves by natural key — company domain, contact email, tag name; source-DB UUID NEVER forwarded. Per-row errors continue batch (FK-unresolvable → per-row error entry, NOT batch abort). `db export` = read-only + drift-tolerant. `db import` dead-stops on drift|pending. Export→fresh-import round-trip is field-identical (test-enforced).

Trigger: `src/mailpilot/database` or `src/mailpilot/cli` db-export/import section changed.
- `rg 'schema_version\|exported_at' src/mailpilot/database` -> bundle fields present
- `rg 'company_domain.*contact\|by.*natural.*key\|natural.*key.*restore' src/mailpilot/database` -> natural-key restore (not UUID-based)
- `rg 'export.*import.*round.trip\|round.trip.*field.identical' src/mailpilot/tests/` -> round-trip test present
- `rg 'company_id.*export\|export.*company_id' src/mailpilot/database` -> zero hits (source UUID not forwarded)

## §V122 — campaign-test Touch 1 delivery keyed on rfc2822_message_id per scenario

`send_touch1.py` captures `outbound_email_id` + `rfc2822_message_id` per scenario enrollment immediately after each Touch 1 send. One shared prospect contact (`inbound@lab5.ca`) receives all sends; isolation is by ephemeral workflow (one per scenario), not by recipient alias — no `inbound{N}@lab5.ca` aliases exist. `inject_replies.py` matches received Touch 1 emails by `rfc2822_message_id` (primary key); subject match is a fallback only. A scenario whose send status is not `sent` or whose `rfc2822_message_id` is missing fails before reply injection. Subject = agent-generated + collision-prone, NEVER the primary identity key.

Trigger: `.grok/skills/mailpilot-campaign-test/**` changed.
- `rg 'rfc2822_message_id' .grok/skills/mailpilot-campaign-test/scripts/send_touch1.py` -> message-id captured at send
- `rg 'rfc2822_message_id' .grok/skills/mailpilot-campaign-test/scripts/inject_replies.py` -> message-id used to match received Touch 1
- `rg 'inbound[1-9]@lab5\.ca' .grok/skills/mailpilot-campaign-test/scripts/` -> zero hits (no per-scenario aliases)

## §V123 — reply-cancels-followups

Inbound reply routing to an enrollment bulk-cancels that enrollment's pending future follow-up tasks: `UPDATE task SET status='cancelled' WHERE enrollment_id=%(id)s AND status='pending' AND scheduled_at > now() AND COALESCE(context->>'trigger','') <> 'enrollment_schedule'`. First-touch exclusion: rows whose trigger = `enrollment_schedule` (§V.32) are excluded. `cancel_enrollment_followup_tasks` still fires from 5 conceptual sites: inbound `routing.route_email` calls it directly; calendar booking (§V.128), agent `conclude_enrollment` (§V.127), cadence sequence exhaustion (§V.136), and bounce (§V.163) cancel inside the §V.186 helper.

Trigger: `src/mailpilot/routing.py`, `src/mailpilot/sync.py`, `src/mailpilot/agent/tools.py`, `src/mailpilot/cadence.py`, or `src/mailpilot/database` changed.
- `rg 'cancel_enrollment_followup_tasks' src/mailpilot/routing.py` -> inbound direct cancel
- `rg 'cancel_enrollment_followup_tasks' src/mailpilot/database` -> helper cancel
- `rg 'enrollment_schedule.*exclude\b\|exclude.*enrollment_schedule\b' src/mailpilot/database` -> first-touch exclusion in the query
- `rg 'scheduled_at.*>.*now\(\)\|now\(\).*<.*scheduled_at' src/mailpilot/database` -> only future tasks cancelled

## §V124 — workflow.goal field

`workflow.goal` = free-text observable outcome that concludes the enrollment (e.g. "prospect books a Google Meet"). Renamed from `workflow.objective` via migration 006. One field, two readers: (1) conclude_enrollment disposition gate — agent calls `conclude_enrollment` when it judges goal met; system concludes deterministically on calendar booking regardless of stated goal; (2) classify.py semantic-match key for inbound workflow routing (§V.76). `_DEFERRED_TASK_TASK` fragment (§V.45) names "the workflow goal" (not "objective"). `record_enrollment_outcome` is system-internal (§V.15) — NOT exposed to the agent.

Definition text matches the composed-protocol mechanism: `goal` + reply-branch `instructions` claim only outcomes/actions the trigger branch can reach. Inbound (`trigger=email`) goal claims no terminal record — §V.31 composes initial-send-only + forbids `conclude_enrollment`, so 'record the outcome completed' never fires. A reply branch names exactly one terminal action, never a two-option close (`create_task` OR `conclude_enrollment`) — agent takes both (§B.120); `contact_later` already schedules re-enrollment (§V.127), so a same-turn `create_task` double-queues.

Trigger: `src/mailpilot/models.py`, `src/mailpilot/agent/classify.py`, or `src/mailpilot/agent/invoke.py` changed.
- `rg '\bgoal\b' src/mailpilot/models.py` -> `goal` present; `rg '\bobjective\b' src/mailpilot/models.py` -> zero hits
- `rg '"Goal:"' src/mailpilot/agent/invoke.py` -> `Goal:` label in agent prompt
- `rg '\bgoal\b' src/mailpilot/agent/classify.py` -> classify.py reads goal column
- `rg 'record_enrollment_outcome' src/mailpilot/agent/tools.py` -> zero hits (system-internal, not in tool set)

## §V125 — meeting + meeting_attendee schema

`meeting` table cols: `{id, google_event_id, meet_url, summary, scheduled_at, ends_at, status, created_at, updated_at}`. `google_event_id` nullable-unique (idempotent ingest, mirrors `email.gmail_message_id` §V.90). `status` CHECK in {scheduled, completed, cancelled, no_show}. `meeting_attendee(meeting_id, contact_id)` link table UNIQUE per pair (mirrors `tag_assignment` §V.116). One meeting links at least 1 attendee. Attendees matched to contacts by email; unmatched email = no link. `status` col = operator record-keeping only, gates NOTHING — booking conclusion (§V.128) fires at booking regardless of later completed|no_show.

Trigger: `src/mailpilot/schema.sql` or `src/mailpilot/database` changed.
- `rg 'google_event_id\b' src/mailpilot/schema.sql` -> nullable-unique col present
- `rg 'meeting_attendee\b' src/mailpilot/schema.sql` -> link table present
- `rg 'scheduled.*completed.*cancelled.*no_show\|status.*CHECK\b' src/mailpilot/schema.sql | grep meeting` -> status enum
- `rg 'UNIQUE.*meeting_id.*contact_id\|UNIQUE.*contact_id.*meeting_id' src/mailpilot/schema.sql` -> link table UNIQUE per pair

## §V126 — CalendarClient + poll sites

`CalendarClient` in `calendar.py` mirrors GmailClient/DriveClient shape: service account + DWD, `with_subject(email)`, scope `calendar.events.readonly`. Shared per-account helper `_poll_account_calendar(connection, account)` fires from two sites: (1) run-interval full-sweep tick via `_poll_all_calendars` (§V.21 fallback), (2) `account_sync` (cli/) per-account after `sync_account`. Each site upserts one `meeting` row/event idempotently on `google_event_id` (re-poll = no dup row) + links email-matched attendees + concludes each booking exactly once. Per-account calendar errors isolated: logged via `operator_event`, NEVER raised — one account's calendar fault stalls neither loop nor Gmail sync. Read-only — NO event create|update from the app.

Trigger: `src/mailpilot/calendar.py` or `src/mailpilot/sync.py` changed.
- `rg 'CalendarClient\b' src/mailpilot/calendar.py` -> class present
- `rg '_poll_account_calendar\b' src/mailpilot/sync.py` -> helper present
- `rg '_poll_all_calendars.*_poll_account_calendar\|_poll_account_calendar.*_poll_all_calendars' src/mailpilot/sync.py` -> called from both sites
- `rg 'calendar.events.readonly' src/mailpilot/calendar.py` -> correct scope
- `rg 'create_event\|update_event\|insert_event' src/mailpilot/calendar.py` -> zero hits (read-only)

## §V127 — conclude_enrollment agent terminal

`conclude_enrollment(disposition, note, reschedule_at)` = sole agent-facing terminal tool. Disposition in {meeting_booked, do_not_contact, contact_later}. Agent tool validates LLM input (disposition enum + future `reschedule_at` per §V.129) then calls the §V.186 helper. System side-effects per disposition stay: `meeting_booked` → outcome + cancel + booking note; `do_not_contact` → conclude + cancel + `disable_contact`; `contact_later` → conclude + cancel + scheduled re-enrollment task at `reschedule_at` (agent-supplied, default >=3 months out). Counts as valid send-obligation terminal (§V.120) — `_sent_reply` walker accepts it like noop. `record_enrollment_outcome` is NOT in the agent tool set — it is system-internal (§V.15, §V.124). System-internal conclusion sites (calendar booking §V.128, cadence exhaustion §V.136, bounce §V.163) call the same helper, not `record_enrollment_outcome` directly.

Trigger: `src/mailpilot/agent/tools.py` or `src/mailpilot/agent/invoke.py` changed.
- `rg 'conclude_enrollment\b' src/mailpilot/agent/tools.py` -> tool present
- `rg 'meeting_booked\|do_not_contact\|contact_later' src/mailpilot/agent/tools.py` -> all 3 dispositions
- `rg 'record_enrollment_outcome' src/mailpilot/agent/tools.py` -> zero hits (helper, not agent tool)
- `rg 'conclude_enrollment.*_sent_reply\|_sent_reply.*conclude_enrollment' src/mailpilot/agent/invoke.py` -> conclude_enrollment in send-obligation walker

## §V128 — calendar booking concludes enrollments, no agent turn

For each attendee contact (§V.125) holding an active outbound enrollment: system concludes via the §V.186 helper (`meeting_booked`, system reason, note written, `skip_if_terminal` default false) so already-terminal enrollments still conclude unless the flag says otherwise. Fan-out fires for EVERY active outbound enrollment the attendee holds — a booked meeting outranks any cold sequence regardless of stated goal (§V.124). Helper cancel preserves first-touch exclusion (§V.32). Distinct from bounce (`skip_if_terminal` true, §V.163).

Trigger: `src/mailpilot/calendar.py` or `src/mailpilot/sync.py` changed.
- `rg 'skip_if_terminal' src/mailpilot/sync.py` -> booking passes flag (default false)
- `rg 'meeting_booked' src/mailpilot/sync.py` -> booking disposition
- `rg 'record_enrollment_outcome\b' src/mailpilot/sync.py` -> zero hits (helper, not pasted)
- `rg 'active.*outbound\b.*enrollment\|outbound.*active\b.*enrollment' src/mailpilot/calendar.py src/mailpilot/sync.py` -> only active outbound enrollments concluded

## §V129 — agent-supplied timestamp grounding + future guard

Two-pronged: PREVENT via grounding, GUARD at boundary. PREVENT: `@agent.instructions` fn in `_build_agent` (invoke.py) injects current date per run-start (PydanticAI idiom, `date.today()` evaluated each run, cache-safe — date rolls slower than cache TTL). GUARD: `create_task` (tools.py) rejects `scheduled_at` not strictly after `now()` → `{error: 'past_scheduled_at', message}`, persists no row. `conclude_enrollment` contact_later (tools.py) rejects past `reschedule_at` same way. A rejected `conclude_enrollment` carries `error` key → `_sent_reply` skips it (§V.120 unsatisfied → agent must retry or noop). Guard at agent boundary NOT in `database.create_task` — system-computed paths (enrollment_schedule first-touch §V.32, default-omitted reschedule_at §V.127) are exempt.

Trigger: `src/mailpilot/agent/tools.py` or `src/mailpilot/agent/invoke.py` changed.
- `rg '@agent\.instructions\b' src/mailpilot/agent/invoke.py` -> dynamic instructions present
- `rg 'date\.today\(\)\|current.*date\b\|today.*date\b' src/mailpilot/agent/invoke.py` -> date injected
- `rg 'past_scheduled_at\b' src/mailpilot/agent/tools.py` -> guard error code present
- `rg 'past_scheduled_at\b' src/mailpilot/database` -> zero hits (guard at boundary, not DB layer)

## §V131 — fallback acknowledgement on terminal inbound failure

`_handle_agent_failure` (run.py) terminal branch sends one fixed `_FALLBACK_ACKNOWLEDGEMENT` reply before `complete_task(status='failed')` when: `task.email_id` is set (inbound task) AND the reply-emitted contextvar flag (set by a successful `reply_email`/`send_email` in tools.py) is unset. Outbound first-touch (`email_id` NULL) stays silent. `_FALLBACK_ACKNOWLEDGEMENT` = code-defined fixed string in templates.py, content-free, never model-generated, first-person singular (`I`) not `we`/`our team` (live sender is one person; campaign bodies use `I`). Tests pin exact body. Idempotency keyed on in-memory contextvar flag (NOT DB read — `connection.rollback()` at head of `_handle_agent_failure` erases mid-turn email row). Fallback-send failure: logs `operator_event("error", source='run.task.fallback_failed')` + falls through to `complete_task(status='failed')` — original terminal never masked.

Trigger: `src/mailpilot/run.py` or `src/mailpilot/agent/templates.py` changed.
- `rg '_FALLBACK_ACKNOWLEDGEMENT\b' src/mailpilot/agent/templates.py` -> constant present
- `rg 'We have received|our team' src/mailpilot/agent/templates.py` -> zero hits (team-voice retired)
- `rg 'reply_emitted_scope\|reply_emitted\b' src/mailpilot/agent/tools.py src/mailpilot/run.py` -> contextvar flag present in both
- `rg 'email_id.*fallback\|fallback.*email_id\|task\.email_id\b' src/mailpilot/run.py` -> inbound gate in `_handle_agent_failure`
- `rg 'fallback_failed\b' src/mailpilot/run.py` -> best-effort send failure error event

## §V132 — workflow stats funnel

`workflow stats <workflow>` = read-only per-campaign funnel, 1 workflow by entity ref (§V.107), single deterministic SQL aggregate or fixed small query set (no LLM). Envelope `{"workflow_stats": {...}, "ok": true}` (aggregate not a workflow entity row — singular-key exception cf `db export` §V.121). 8 stages at enrollment grain (contact-distinct, multi-touch never double-counts):
- `enrolled` = workflow's enrollment rows
- `sent` = enrollments with at least 1 outbound `status='sent'` email
- `bounced` = enrollments with at least 1 outbound `status='bounced'` email
- `replied` = enrollments with at least 1 inbound routed email (route sets contact_id + workflow_id §V.27)
- `meeting_booked` = latest-outcome `enrollment_completed` (disposition-independent)
- `contact_later` / `do_not_contact` = latest-outcome `enrollment_failed` split by `detail->>'disposition'`
- `active` = `status='active'` enrollment with no terminal outcome

Touch-level + execution slices (additive; still enrollment grain where applicable):
- `touches` = map touch-number string → `{sent, pending}` for each configured def `touches` N (outbound sent count vs pending tasks whose resolved touch = N per §V.162: parse context.touch; absent/unparseable + trigger in {enrollment_run, enrollment_schedule} → 1)
- `awaiting_first_touch` = active enrollments with no outbound email for this workflow (enrollment grain; ! required equal `touches.1.pending`)
- `disabled` = enrollments with `status='disabled'`

Disposition persistence: `record_enrollment_outcome` writes `detail.disposition` in {meeting_booked, do_not_contact, contact_later} from `conclude_enrollment.disposition` (§V.127) + booking-conclusion `meeting_booked` (§V.128). JSONB key, no migration. Pre-change failed rows lack disposition (legacy gap; forward campaigns are exact).

Takes already-loaded `Workflow` (no inner `get_workflow`); callers share stack §V.184.

Trigger: `src/mailpilot/database` or `src/mailpilot/cli` changed.
- `rg 'workflow_stats\b' src/mailpilot/database` -> aggregate fn present
- `rg 'meeting_booked\|contact_later\|do_not_contact' src/mailpilot/database | grep stats` -> disposition stages
- `rg '"workflow_stats"' src/mailpilot/cli` -> envelope key correct
- `rg 'DISTINCT.*contact_id\b' src/mailpilot/database | grep stats` -> enrollment grain aggregate
- `rg 'awaiting_first_touch\|touches' src/mailpilot/database src/mailpilot/models.py` -> touch-level fields present

## §V133 — task stats aggregate

`task stats` = read-only aggregate, single SQL query, task grain, no LLM. Envelope `{"task_stats": {...}, "ok": true}` (aggregate, not a task entity row, cf §V.132). Filter options: `--workflow-id` (polymorphic §V.107); `--trigger` Enum filter on `COALESCE(context->>'trigger', '')` against §V.26 taxonomy — NEVER reads `description`. Shared `--trigger` decorator with `task list`. Returns: per-status counts `{pending, completed, failed, cancelled}` + `total` + `distinct_scheduled_days` (day-bucketed count) + `first_scheduled_at` + `last_scheduled_at`. `--bucket-tz <IANA>` (default UTC) buckets `distinct_scheduled_days` only; per-status counts are timezone-independent. `--trigger enrollment_schedule` selects first-touch tasks (§V.32).

Trigger: `src/mailpilot/database` or `src/mailpilot/cli` changed.
- `rg 'task_stats\b' src/mailpilot/database` -> aggregate fn present
- `rg 'distinct_scheduled_days\b' src/mailpilot/database` -> day-bucket field present
- `rg '"task_stats"' src/mailpilot/cli` -> envelope key correct
- `rg "COALESCE.*trigger\b\|context.*trigger" src/mailpilot/database | grep task` -> trigger from context JSONB not description
- `rg '"--trigger".*Choice\b' src/mailpilot/cli | grep task` -> trigger is a Choice (closed enum)

## §V134 — workflow check: def-integrity states

`workflow check` = read-only live 2-way SHA-256 over def fields `{template, theme, goal, instructions, touches, touch_interval_days}`. Join key = workflow `name` (§V.90 global-unique, NOT a hashed field). Each discovered `*.toml` read for its `name` field (NOT file stem, §V.103); row set read from DB; joined by name. States: `in_sync` (name both sides + hash equal); `out_of_sync` (name both sides + hash differs → re-import due); `not_imported` (name in catalog def, no DB row); `orphaned` (name in DB row, no catalog def). `--file` repeatable: every passed source read + merged, last-def-wins on dup `name`. Discovery shares import recurse `**/*.toml` (§V.103) so `--file campaigns/` sees `campaigns/<slug>/workflows/<slug>.toml`. `--file` always `scope_to_catalog=True` (file or dir) — report iterates discovered catalog names only; unpassed DB row dropped (never `orphaned`). Dir no longer flips to full-catalog. `--account-email` + `--file` → filter live rows to that account + `scope_to_catalog=False` (full envelope — orphans of that account included). `--file` stays required; empty `--file` → `validation_error`. No `conflict` state — duplicate `name` across files is import-forbidden (§V.103 name==unique-stem), hand-edit-only. No `row_ahead` state — def fields import-only (§V.103) so any mismatch = catalog ahead only. Report-only envelope `{"workflow_check": {...}, "ok": true}` (aggregate, not a workflow row, cf §V.132); NOT a deploy gate. Import-time `name==stem` enforcement (§V.103) is separate — `workflow check` reads the TOML `name` field, not the file stem.

Trigger: `src/mailpilot/cli` changed.
- `rg '"in_sync"\|"out_of_sync"\|"not_imported"\|"orphaned"' src/mailpilot/cli src/mailpilot/database` -> all 4 states present
- `rg 'scope_to_catalog' src/mailpilot/cli src/mailpilot/database` -> --file alone suppresses `orphaned`; --account-email keeps it
- `rg 'multiple=True' src/mailpilot/cli | grep -i check` -> `workflow check --file` repeatable
- `rg 'account_email' src/mailpilot/cli | grep -i check` -> optional --account-email on workflow check
- `rg 'workflow_check\b' src/mailpilot/cli` -> envelope key present
- `rg 'sha256\b\|hashlib.*sha256' src/mailpilot/cli src/mailpilot/database | grep workflow` -> SHA-256 hash present
- `rg 'toml.*\["name"\]\|tomllib.*name\b\|name.*toml' src/mailpilot/cli src/mailpilot/database | grep workflow_check` -> reads `name` field from TOML (not file stem)

## §V135 — mechanical context pre-feed

invoke_workflow_agent loads ContactView (+ CompanyView when contact.company_id set) via load_contact_view/load_company_view — the same shared loaders the CLI uses (§V.8), so agent + operator context stay byte-identical. _build_user_prompt renders `Contact record:` / `Company record:` JSON sections. read_contact/read_company absent from EVERY template roster (inbound included); _BASE names no read tools (§V.40 fragment-naming floor).

Trigger: `src/mailpilot/agent/invoke.py` or `src/mailpilot/agent/templates.py` changed.
- `rg 'load_contact_view\|load_company_view' src/mailpilot/agent/invoke.py` -> shared loaders pre-feed
- `rg 'Contact record:' src/mailpilot/agent/invoke.py` -> prompt section rendered
- `rg 'read_contact\|read_company' src/mailpilot/agent/templates.py` -> zero roster hits

## §V136 — system-owned touch cadence

Workflow def fields `touches` + `touch_interval_days` (nullable pair, §V.103; NULL = single-touch, no auto follow-up). Cadence engine (`cadence.py`) owns schedule math (weekend -> Monday roll) + touch scheduled_at — system-computed only, §V.129 exempt path. Successful touch-N send -> harness creates touch-N+1 task w/ context {touch: N+1, prior_email_id}. Final touch -> §V.186 helper `contact_later` "sequence exhausted" (no re-enrollment task, no agent turn). Touch runs (context.touch present | trigger enrollment_schedule | enrollment_run) = compose-only agent: output_type TouchMessage {subject: str|None, body: str}, zero tools; output validator (bounded ModelRetry, same agent retry budget): first-touch subject require — new-thread touch (touch 1 / no prior outbound to reply on) → after strip `subject` ! non-empty; None/"" /whitespace-only → ModelRetry (message: subject required for new thread); follow-up that continues existing thread may leave subject empty (harness reply_email keeps thread subject) (closes §B.127); body format lint retired (§V.42 / §B.128); harness sends via email_ops + schedules the next touch — 1 LLM call per touch, send structural (§V.120). Outbound task|email triggers keep the tool loop; inbound unchanged (§V.44 registry owns both shapes). prior_email_id from task context; absent -> enrollment's latest outbound email. NULL-cadence belt: touch >= 2 vs NULL cadence -> reschedule +1h + operator warn (§V.25 shape); touch 1 vs NULL -> send + schedule nothing. create_task stays bound for reply-branch soft follow-ups. Cadence + after-touch prose live in def fields, never TOML instructions.

Trigger: `src/mailpilot/cadence.py`, `src/mailpilot/agent/invoke.py`, or `src/mailpilot/email_ops.py` changed.
- `rg 'touch_interval_days' src/mailpilot/cadence.py` -> cadence engine owns the pair
- `rg 'TouchMessage' src/mailpilot/agent/invoke.py src/mailpilot/models.py` -> compose-only output type
- `rg 'sequence exhausted' src/mailpilot/cadence.py` -> final-touch system conclusion
- `rg 'prior_email_id' src/mailpilot/` -> touch context threading
- `rg 'ModelRetry' src/mailpilot/agent/invoke.py` -> compose-only output validators raise ModelRetry
- first-touch subject guard: empty/None/whitespace subject rejected for new-thread compose-only only (not follow-up)

## §V137

connect-fail operator UX — `_connect_database` maps OperationalError text ordered (role-missing → role/URL not createdb; database-missing → createdb; `no pg_hba.conf entry` → client-host allowlist; resolve/`nodename nor servname`/`Name or service not known` → DNS/hostname; password|Peer auth failed → credentials/auth; Connection refused → service-running; else database_url) → SystemExit hint; expected fail → logfire.error (not exception) + operator_event("error", source="database.connect") + SystemExit — zero console Traceback (closes §B.125)

## §V138

company cohort pipeline status — `company list --status` Enum ∈ {ready, needs_contacts, needs_profile, disabled}; rules: ready = has_profile + contact_count ≥ 1 + not disabled; needs_contacts = has_profile + contact_count = 0 + not disabled; needs_profile = !has_profile + not disabled; disabled = disabled_reason set; Enum family §V.115; `--status disabled` overrides default hide; AND-composes w/ `--tag`/`--no-tag`/`--min/max-contacts`/`--has-profile`/`--include-disabled`; rules in --skill/help (zero SPEC cites §V.111); no `company audit` verb

## §V139

stdin NDJSON batch mutation — selected Mutate verbs accept `--stdin` (NDJSON, 1 object/line); exclusive w/ positional single-entity target (not both); line schema verb-specific (`company disable`: {domain, reason}; `contact create`: create fields + optional `upsert:true` §V.147); envelope always `{"results":[{ref, status}|{ref, status:"error", error, message}], "ok":true, "record_count":N}` full stream (never abort mid-batch w/o reporting prior rows); exit 0 iff zero error rows, exit 1 if any error (still emit full results JSON); per-row errors continue; safe-idempotent defaults: re-disable already-disabled → status ok no-op; duplicate contact natural-key → status ok skip unless line `upsert:true` then field-selective update per §V.147; MVP verbs: `company disable --stdin`, `contact create --stdin`; optional later: `tag add --stdin`, `company update --stdin`; --skill recipes document batch disable + batch contact create; help zero SPEC cites §V.111

## §V140

company profile write paths — `company create` + `company update` full-replace via exclusive XOR of {`--profile-json`, `--profile-file <path>`, `--profile -` (stdin)}; all three validate vs CompanyProfile (§V.72) before write; field-patch flags {`--summary`, `--product` (multi), `--source` (multi), `--timezone`, `--target-customers`} merge into existing profile (null existing → patch builds base then validates full object); full-replace exclusive w/ any patch flag; invalid → `validation_error` no partial write; create + `--tag` oneshot → §V.167; success envelope = full company w/ profile (ok:true, record_count=1); --skill recipes prefer file/stdin over inline JSON; create and update share the profile write flags and replace/patch helpers; one JSON-object parser serves profile + verification meta (error nouns `profile` / `meta` §V.144); help zero SPEC cites §V.111

## §V141

multi-owner tag link + set-replace — `tag add`/`tag remove --tag <name>` accept repeatable `--company-domain` or repeatable `--contact-email`; owner-kind XOR per call (companies or contacts, not mixed; ≥1 owner); undefined tag → `not_found` never auto-create (§V.116); N>1 → results envelope §V.139 shape + exit 0 iff zero errors; N=1 → `tag_assignment` entity envelope; already-linked multi `add` row → status ok skip; already-unlinked multi `remove` row → status ok skip; `tag set` owner XOR + `--tags` comma-list replaces owner's full assignment set one txn (add missing, remove extras, activity per change §V.14); empty `--tags` clears all; undefined name in set → `not_found` zero writes; `company create --tag` (repeatable) additive same as tag add (§V.167); company list|view `tags[]` always (§V.8/§V.116); help/--skill zero SPEC cites §V.111. CLI `tag add` + `tag remove` share one `_tag_link_owners(verb, assign_or_remove, ...)` — owner XOR, single vs multi envelope, soft lookup, ok-skip; only verb + DB call + error (`already_exists` vs `not_found`) differ. N=1 envelope stays `tag_assignment`; N>1 stays `results`. DB `_assign_tag(owner_col, ..., *, commit: bool)` + `_remove_tag(...)`; four public writers thin wrappers; `set_company_tags`/`set_contact_tags` loop those `commit=False` then one commit (helpers ! always-commit so set ! copy INSERT/DELETE+activity).

Trigger: `src/mailpilot/cli` or `src/mailpilot/database` tag-link paths changed.
- `rg 'def _tag_link_owners\(' src/mailpilot/cli` -> one helper add+remove
- `rg 'def _assign_tag\(|def _remove_tag\(' src/mailpilot/database` -> shared writers `commit: bool`

## §V142

company domain aliases — table `company_alias` {domain TEXT UNIQUE NOT NULL lowercased, company_id FK company}; domain space shared: each string is either `company.domain` or `company_alias.domain`, never both + never two owners; `get_company_by_domain` + CLI polymorphic company ref + contact `--company-domain` resolve alias → canonical company (§V.90/§V.107); `company create --alias` (repeatable) registers aliases same txn; create/seed domain already canonical or alias → `already_exists` (no silent second firm); `company view` projects `aliases[]` (sorted, empty ok; list lean omits §V.8); domains lowercased before match+insert; db export/import company.aliases[] (§V.121); migration 011; help/--skill zero SPEC cites §V.111

## §V143

company merge into survivor — `company merge --from <domain|uuid> --into <domain|uuid> [--move-contacts]`; records `from.domain` as alias on into if missing (§V.142); soft-disables from w/ `disabled_reason` = `merged:into <into.domain>` (§V.114) even when source already disabled for another reason; `--move-contacts` reassigns contact.company_id from→into same txn, omit → contacts stay on disabled source; success envelope = survivor company (ok:true, record_count=1, aliases[] incl. from.domain); idempotent already-merged (from disabled w/ matching reason + alias present) → ok no-op; reject self-merge → `invalid_state`; disabled source allowed (no prior enable); disabled survivor allowed — keep survivor `disabled_reason` (never re-enable); both disabled allowed; missing key → `not_found`; enable of company whose domain is alias of another → `invalid_state` (MVP no alias-remove verb); help/--skill zero SPEC cites §V.111

## §V144

contact operator-only verification meta — JSONB `verification_meta` NULL ok on contact; write via `contact create|update --meta-json` (JSON object not array; invalid → `validation_error`); never written to notes; default ContactView + load_contact_view omit field; `contact view --include-meta` projects `verification_meta` (null when unset); `contact create --stdin` line schema ? optional `meta` object same rules; workflow agent prompt allowlist = {name, title, email, email_confidence, company profile, lean notes ≤ cap} — `verification_meta` never on allowlist; tests pin meta absent from agent context builder path; --skill/help zero SPEC cites §V.111

## §V145

company tracker export — `company export` writes NDJSON (1 company object/line) stable schema keys {domain, name, tags[], has_profile, contact_count, disabled_reason}; `--full` embeds `profile` object or null; filters compose w/ company list family (`--tag`/`--no-tag`/`--status`/`--include-disabled`/`--has-profile`/`--min/max-contacts` §V.138/§V.116/§V.114); domains lowercased; tags sorted; order domain ASC; `--format jsonl` MVP only; `--out <path>` → write file + status envelope on stdout `{"company_export":{path,format,record_count},"ok":true,"record_count":N}` (path null when body on stdout); without `--out` NDJSON body on stdout (stream format exclusion from single-object envelope for tracker pipes; operator lifecycle still stderr §V.3); empty set → zero lines / empty file + record_count 0; not `db export` snapshot (§V.121); --skill schema docs; help zero SPEC cites §V.111

## §V146

company tracker dry-run import — `company import --from <path.jsonl> --dry-run` compares tracker NDJSON to CRM by lowercased domain; dry-run only MVP (no apply writes); optional filters scope CRM side same as export (§V.145); report buckets domain lists: `missing_in_crm` (file not CRM), `missing_profile` (in both or CRM, !has_profile), `zero_contacts` (contact_count=0), `disabled` (disabled_reason set), `extra_in_crm` (CRM scope not in file); envelope `{"company_import_diff":{...},"ok":true,"record_count":N}` (N = |file domains ∪ CRM-scope domains|); missing file → `not_found`; invalid NDJSON line → `validation_error` (no partial report required); --skill bucket docs; help zero SPEC cites §V.111

## §V147

company/contact create upsert — `company create` / `contact create` accept `--upsert`; natural-key conflict w/o flag → existing error codes preserved (`duplicate_key` contact §V.16; `already_exists` company domain/alias §V.142); w/ `--upsert` → field-selective update only supplied flags (contact: title, email_confidence, company_domain if present; ? verification_meta if `--meta-json` present — never clobber omitted; company: name if provided; profile only when `--profile-*` or field-patch flags also passed per §V.140 — bare upsert never wipes profile; new `--alias` ? register missing only, never move ownership); company create oneshot profile+tags → §V.167; success = final entity envelope + top-level bool `created` (true=insert, false=update) + record_count=1 exit 0; `contact create --stdin` line schema ? optional `upsert:true` same per-row semantics; --skill preferred agent path uses upsert; help zero SPEC cites §V.111

## §V148

company list/search order + page — `company list|search` accept `--sort` Enum ∈ {name, domain, created_at, contact_count} default `name` (ORDER BY LOWER(name) today); `--desc` flips ASC→DESC; `--offset N` default 0 + `--limit` default 500 (tag-cohort sized; other nouns keep 100 per §V.115); invalid sort → `validation_error`; `record_count` = page length only (no total/has_more MVP); list filters unchanged (§V.138/§V.116/§V.114/§V.96); search text-match name/domain/alias; lean row list|search same fields {domain, name, has_profile, contact_count, tags[], disabled_reason}; stdout one JSON document, diagnostics stderr (§V.3); --skill docs defaults + sort keys; help zero SPEC cites §V.111

## §V149

disable reason-file — `company disable` + `contact disable` accept `--reason-file <path>` XOR `--reason` (exactly one reason source in single-entity mode); file UTF-8, strip one trailing newline, empty → `validation_error`; missing path → `not_found`; `--stdin` exclusive w/ both reason sources (company disable batch still per-line reason); reason ! empty after resolve; success envelope unchanged; --skill; help zero SPEC cites §V.111

## §V150

enrollment tag-cohort dry-run — `enrollment add --workflow-id <ref> --tag <name> --dry-run` [optional `--min-contacts N`]; `--tag` matches company-tag or contact-tag (union, dedup contact id); company-tag expand = enabled contacts @ tagged companies; contact-tag expand = enabled contacts carrying tag (disabled company excluded §V.114); dry-run w/o tag → `validation_error`; tag w/o `--dry-run` → apply §V.171 (`--scheduled-at` required) else `validation_error`; optional `--limit` / `--company-atomic` / `--exclude-peer` on dry-run reuse §V.171 pack (preview packed contacts, still no writes; excluded + peer + over_limit); single-contact `--contact-email` path unchanged (no dry-run needed); drop already-enrolled this workflow + self-loop contacts (§V.33) + disabled contacts; optional `--min-contacts N` filters companies before expand (company-tag) / contact's company contact_count (contact-tag); envelope `{"enrollment_preview":{workflow, tag, count, contacts:[{email, title, company_domain, company_tags[], contact_tags[], email_confidence, peer_workflows[]}], excluded:{disabled_companies, already_enrolled, self_loop, disabled_contacts}},"ok":true,"record_count":count}` (aggregate not enrollment row); contacts sorted company_domain then email (group-stable); `peer_workflows` = other-workflow names w/ active enrollment (empty ok); undefined tag → `not_found`; zero candidates → ok empty record_count=0; no writes; --skill one-call cohort recipe; help zero SPEC cites §V.111; preview internals (enrolled-id SELECT, `_preview_from_contacts`, tag `ANY`) §V.185

## §V3 — stdout strict JSON + stderr lifecycle

stdout = strict JSON only by default (all flags, incl --debug); opt-in non-JSON via `--format` per §V.156 on report/list; `show` group table-default + `--format json` opt-in per §V.166 (not §V.156); operator lifecycle + errors -> stderr; Logfire console exporter ! target stderr (ConsoleOptions output=sys.stderr), never stdout — output unset defaults stdout so console lines corrupt JSON envelope

Trigger: `src/mailpilot/cli` or logging config changed.
- `rg 'ConsoleOptions' src/mailpilot/ --type py` -> console exporter targets stderr
- `rg 'format.*table|table.*csv|ndjson' src/mailpilot/cli` -> opt-in --format surface present
- `rg 'show.*queue|--format' src/mailpilot/cli` -> show group format surface present

## §V48 — provider transport timeout 240s

provider transport timeout = 240s — Anthropic HTTP 240s (`APITimeoutError` + `httpx.ReadTimeout` terminal); xAI `XaiProvider(timeout=240)` hard-coded (no operator setting); xAI/gRPC timeouts terminal same spirit; mid-turn tool side-effects make retry unsafe

Trigger: `src/mailpilot/agent/` model build changed.
- `rg 'timeout.?=.?240|timeout=240' src/mailpilot/` -> 240s timeout sites
- `rg 'APITimeoutError|ReadTimeout' src/mailpilot/` -> Anthropic timeout terminal class

## §V97 — lead-* run-summary deferred completeness

lead-companies + lead-contacts run-summary completeness — batch gate capping below stale-count -> run summary carries deferred = stale-count - processed (rows left profile/contact-NULL for a follow-up run); all stale processed -> deferred 0 or omitted; bare created|enriched|seeded counts never the sole remainder signal

Trigger: `.claude/skills/lead-companies/**` or `lead-contacts/**` changed.
- `rg 'deferred' .claude/skills/lead-companies/ .claude/skills/lead-contacts/` -> deferred field in run-summary prose

## §V98 — lead-companies seed collision visibility

lead-companies seed collision visibility — resolved-apex duplicate_key recorded in run-summary collapsed when incoming CSV display name diverges from the owning company's name; fires intra-batch AND onto a previously-seeded row; same-name re-seed stays silent existing; bare existing:N never the sole entity-merge signal

Trigger: lead-companies seed path changed.
- `rg 'collapsed' .claude/skills/lead-companies/ --type py -g '*.py' 2>/dev/null; rg 'collapsed' .claude/skills/lead-companies/` -> name-divergent collapse signal

## §V151 — account email signature

account email signature — per-account AccountSignature fields {full_name, title, website, phone} = nullable TEXT cols `signature_full_name|title|website|phone` on account; `display_name` = From only (not aliased); CLI create|update flags `--signature-full-name|--signature-title|--signature-website|--signature-phone` field-selective (omit=leave, empty str clears); website ! absolute http(s) URL else `validation_error` (no auto-prefix); list|view|create|update project nested `signature:{full_name,title,website,phone}` or null when all empty; harness `render_signature_html` + `render_signature_text` take `AccountSignature | None` via `Account.account_signature()` (no `_signature_fields` reimplementation) after §V.92 body render, before MIME; wire HTML = body_html + signature — table mark layout: 60px embedded lab5 logo (PNG data-URI constant) + 18px spacer + 2px four-colour vertical rule (`#0969da|#cf222e|#f9c513|#1f883d`) + 18px spacer + detail rows (name bold `#101820` 16px Helvetica; title monospace `#0969da` 11px uppercase letter-spacing; `web  ` + host link `#101820` monospace 12px, href=absolute website; `cell  ` + phone `tel:` link `#101820`; muted labels `#8A939B`; font families Helvetica/Consolas stack; `margin-top:20px` on outer table; inline styles only; empty fields omit their rows, all-empty → no block/no logo); text/plain mirrors stacked lines = body + `--` + name/title/`web  host`/`cell  phone` (scheme stripped from web display; empty fields omitted); ! persist signature HTML into `email.body` (§C plain-text body holds); every outbound path (`email send|reply`, agent send/reply tools, cadence touch send, §V.131 fallback); agent never drafts signature; body theme (§V.92 THEMES) does not recolor signature; migration + schema.sql

Trigger: `src/mailpilot/` account/signature/render paths changed.
- `rg 'signature_full_name|render_signature_html|render_signature_text' src/mailpilot/` -> cols + renderers present
- `rg 'AccountSignature|signature:' src/mailpilot/models.py src/mailpilot/cli` -> nested projection
- `rg 'lab5|data:image/png|0969da' src/mailpilot/` -> mark layout constants
- `rg '_signature_fields' src/mailpilot/` -> zero hits
- `rg 'account_signature\(\)' src/mailpilot/sync.py src/mailpilot/models.py` -> callers pass nested signature

## §V152 — enrollment execution projection

enrollment execution projection — default list lean; `--full` denser {company_domain, company_name, emails_sent, last_touch, next_scheduled_at, next_touch, disposition, created_at}; filters `--has-pending-task` / `--touch N` / `--disposition` (§V.160) / `--since`/`--until` on `e.updated_at`; `--touch 1` matches pending first-touch when `emails_sent=0` AND `next_scheduled_at` IS NOT NULL even when `context.touch` absent / `next_touch` null (`enrollment_schedule` §V.32); `--full` projects `next_touch=1` on that row; `--touch N` N>=2 unchanged (pending context.touch=N or no-pending last-sent=N); sort next_scheduled_at; envelope `enrollments`; entity refs name|UUID (§V.107). `enrollment list --workflow-id` polymorphic name|UUID via `_resolve_workflow_id` (§V.107); help "name or ID"; unknown → `not_found` (#207). dated-window DNC = `--full --disposition do_not_contact --since --until` — sufficient without `--timeline` (`updated_at` clock §V.15). loader SQL share §V.185.

Trigger: enrollment list/view projection changed.
- `rg 'next_scheduled_at|emails_sent|last_touch|--full' src/mailpilot/cli src/mailpilot/database` -> denser projection fields
- `rg 'def enrollment_list' -A 40 src/mailpilot/cli` -> list path calls `_resolve_workflow_id` for workflow filter
- `rg '--disposition|disposition' src/mailpilot/cli src/mailpilot/database` -> disposition filter surface
- `rg 'emails_sent=0|next_scheduled_at|next_touch' src/mailpilot/database` -> first-touch --touch 1 fallback present

## §V153 — workflow report composite

workflow report — pure-SQL composite `workflow report <ref>`: meta + funnel (§V.132) + task aggregate (§V.133) + enrollment matrix (§V.152); filters `--stuck` (§V.155) / `--touch` / `--status`; envelope `{workflow_report}`; no LLM, no CRM write; fetch workflow once then call stats (§V.184)

Trigger: workflow report path changed.
- `rg 'workflow_report|def .*workflow_report' src/mailpilot/` -> composite report surface

## §V154 — workflow-scoped activity/email list

workflow-scoped activity/email list — `activity list` ≥1 of contact|company|workflow else `missing_filter`; `email list` ≥1 scope filter (no unbounded dump); `--workflow-id` composes w/ existing filters; lean rows + limit

Trigger: activity/email list filters changed.
- `rg 'missing_filter|workflow_id' src/mailpilot/cli` -> scope gate present

## §V155 — stuck/overdue filters

stuck/overdue filters — `task list --overdue` = pending + scheduled_at < now; enrollment/report `--stuck` heuristics (active no pending no terminal + never-sent past SLA or cadence lag; bounced w/o disposition; high attempt_count fails); default first-send SLA 24h; read-only

Trigger: stuck/overdue filter paths changed.
- `rg 'overdue|--stuck|first.send|24' src/mailpilot/cli src/mailpilot/database` -> overdue/stuck surfaces

## §V156 — CLI output format modes

CLI output format modes — `--format json|table|csv|ndjson` on report/list surfaces (default json = §V.4 envelope); table human stdout; csv|ndjson prefer `--out`; JSON-path errors/exits unchanged; exclusion from strict-JSON-only §V.3; `show` group not this set (table-default, json|table only, §V.166)

Trigger: CLI format output path changed.
- `rg 'table|csv|ndjson|--format' src/mailpilot/cli` -> format modes present

## §V157 — workflow status ops-health

workflow status ops-health — `workflow status <ref>`: meta + wording via `check_workflow_wording` (§V.134; never hardcoded `"unknown"`) + run_loop heartbeat + overdue_tasks + failed_tasks_24h + enrollments_never_sent + optional funnel_active; envelope `{workflow_status}`; not funnel (funnel stays stats/report); no LLM; fetch workflow once then call stats (§V.184)

Trigger: workflow status path changed.
- `rg 'workflow_status|overdue_tasks|enrollments_never_sent' src/mailpilot/` -> ops-health envelope

## §V158 — contact search multi-token

contact search multi-token — `search_contacts` / `contact search <query>`: single-token query = per-field LIKE on {email, first_name, last_name, title} (status quo); full-name = order-preserving match on `TRIM(COALESCE(first_name,'') || ' ' || COALESCE(last_name,''))` LIKE pattern so `"David Drouin"` hits first=David last=Drouin; multi-token (whitespace-split) = every token AND-matches ≥1 of the same fields (no flood from partial noise); disabled contacts remain searchable (forensics); company domain not required in match set unless already present; CLI help + SKILL document full-name + multi-token behavior (closes §B.129)

Trigger: contact search path changed.
- `rg 'search_contacts|TRIM|first_name.*last_name' src/mailpilot/database` -> multi-token / full-name SQL
- `rg 'contact search|full.name|multi.token' src/mailpilot/SKILL.md src/mailpilot/cli` -> help/docs

## §V159 — contact view timeline dossier

contact view `--timeline` — opt-in bounded dossier on `contact view <ref>`: existing notes + enrollments (status, disposition, last/next touch) + last N emails + last N activities in one JSON envelope; default N=10, hard cap (document in help); bare `contact view` without flag = notes only (agent prompt budget preserved — no timeline keys or empty per chosen shape); works for disabled / do_not_contact contacts (forensics); no auto-enroll; no Gmail body rewrite (reuse email list/view fields)

Trigger: contact view path changed.
- `rg '--timeline|timeline' src/mailpilot/cli src/mailpilot/database` -> timeline flag + loader
- `rg 'enrollments|activities|emails' src/mailpilot/models.py src/mailpilot/cli` -> dossier projection keys

## §V160 — enrollment list disposition filter

enrollment list `--disposition` — filter enrollments by latest terminal disposition ∈ {`do_not_contact`, `contact_later`, `meeting_booked`} (product vocabulary = §V.127 conclude set); composes w/ `--workflow-id` / `--status` / `--full` / `--stuck` / `--since`/`--until` / other list filters; unknown value → `validation_error` listing allowed set; help documents flag + allowed values; empty match → ok envelope record_count=0

Trigger: enrollment list disposition filter changed.
- `rg '--disposition|disposition' src/mailpilot/cli src/mailpilot/database` -> filter flag + SQL
- `rg 'validation_error|do_not_contact|contact_later|meeting_booked' src/mailpilot/cli` -> allowed-set error path

## §V161 — address-change auto-reply hard-stop

address-change auto-reply hard-stop — active outbound enrollment + inbound address-change / "update your records" / hard email-redirect auto-reply → conclude do_not_contact (cancel follow-ups + disable old contact); note ! carry redirect + new email when present (campaign-review referral); agent never enrolls new address; ! OOO pause-once (noop, no terminal); last-day-was past → §V.179

Trigger: inbound classify / conclude / campaign-test address-change path changed.
- `rg 'address.change|update your records|do_not_contact' src/mailpilot/` -> hard-stop path present
- `rg 'auto_reply|ooo|out.of.office' src/mailpilot/agent/ tests/` -> OOO stays non-terminal

## §V162 — touch-context-parse

touch-context-parse — `task.context.touch` JSON number `N` or string `T<n>` or `"n"` → int N; SQL readers (get_workflow_stats, list_enrollments_detailed --full/--touch) never raw `(context->>'touch')::int`; unparseable → NULL not crash; new writers (cadence + OOO-resume create_task + enrollment_schedule first-touch) emit numeric N; enrollment_schedule writer (`enrollment add --scheduled-at`) persists `context.touch` numeric 1; `resolve_touch_number` same parse + trigger in {enrollment_run, enrollment_schedule} → 1 when touch absent. SQL pending count (`get_workflow_stats`) + queue readers (`format_queue_touch`, workflow-grain t1/t2/t3/t4p) share that fallback — enrollment_schedule w/ empty touch → 1 not NULL/blank.

Trigger: stats / enrollment --full/--touch / cadence task write / enrollment_schedule writer / show queue changed.
- `rg 'resolve_touch_number' src/mailpilot/` -> shared parse present
- `rg "context->>'touch'\\)\\s*::int" src/mailpilot/` -> zero raw ::int casts
- `rg 'enrollment_schedule' src/mailpilot/cli src/mailpilot/agent/tools.py` -> first-touch writer sites emit touch:1
- `rg 'format_queue_touch' src/mailpilot/queue.py` -> queue touch cell present

## §V163 — bounce enrollment hard-stop

bounce enrollment hard-stop — outbound bounce (§V.80) → every active outbound enrollment for that contact via §V.186 helper (`do_not_contact`, system reason, `skip_if_terminal` true); skip already-terminal; enrollment status untouched; updated_at bumped (§V.15); contact disable stays §V.80; ! defer to execute-time §V.83

Trigger: bounce handler changed.
- `rg 'skip_if_terminal' src/mailpilot/routing.py` -> bounce passes true
- `rg 'do_not_contact' src/mailpilot/routing.py` -> bounce disposition
- `rg 'record_enrollment_outcome' src/mailpilot/routing.py` -> zero hits (helper, not pasted)

## §V164 — thread-alias inbound bind

thread-alias inbound bind — inbound on existing outbound thread binds email.contact_id to enrolled contact even when From: local-part differs; ! mint or auto-enroll alias From; left-company/retired → conclude original do_not_contact + cancel follow-ups (§V.161/§V.123); referral addrs stay note (agent never enrolls); distinct from case-variant (§V.90) and same-contact address-change (§V.161). Thread-enrolled contact resolved once per inbound message — account-scoped cache keyed by thread + In-Reply-To; `_thread_bound_sender_emails`, `_store_inbound_message`, `route_email` share that result (§V.187).

Trigger: inbound routing / thread match / contact bind changed.
- `rg 'thread_match|contact_id|gmail_thread' src/mailpilot/routing.py src/mailpilot/sync.py` -> thread bind to enrolled contact
- `rg 'auto.enroll|create_contact' src/mailpilot/routing.py` -> alias From does not mint enroll

## §V165 — live E2E skills DEV-only

Live skill tests (`.grok/skills/mailpilot-campaign-test` + `.grok/skills/mailpilot-reply-test`) MUST read `settings.environment` before any CRM or Gmail mutate (account create/update, send, inject, handle). Value != `dev` → blocking preflight issue + skill bail. Gate is `environment` (source of truth §V.176; derived `logfire_environment=development` is equivalent), not a host heuristic. Pytest unit tests exempt (constructor kwargs / fixtures). Skill procedure checks env before account-ensure (step 0b).

Trigger: campaign-test or reply-test scripts/skills changed.
- `rg 'environment' .grok/skills/mailpilot-campaign-test/scripts/preflight.py` -> gate present
- `rg 'environment' .grok/skills/mailpilot-reply-test/scripts/preflight.py` -> gate present
- `rg '"dev"' .grok/skills/mailpilot-campaign-test/scripts/preflight.py` -> required value
- `rg '"dev"' .grok/skills/mailpilot-reply-test/scripts/preflight.py` -> required value

## §V176 — target-env derived pubsub + logfire

settings.environment ∈ {dev, prd} default dev. Sole env setting. Operator knob: `config set environment` on `app_config`. Pair map (internal, not a setting): dev=development, prd=production — used only at `logfire.configure(environment=...)`. Derived pubsub: `google_pubsub_topic` = `mailpilot-topic-{environment}`; `google_pubsub_subscription` = `mailpilot-sub-{environment}`. No `logfire_environment` field / Settings key / config get key / status config key. Derived pubsub keys not independently settable (`config set` → `invalid_key`; `MAILPILOT_GOOGLE_PUBSUB_TOPIC` / `MAILPILOT_GOOGLE_PUBSUB_SUBSCRIPTION` / `MAILPILOT_LOGFIRE_ENVIRONMENT` not sources). Persist `environment` only on `app_config`. No `logfire_environment` load-compat. `config get` + status `config` block project `environment` + resolved topic/sub only. `environment` / `logfire_token` change @ run tick → reconfigure Logfire + restart subscriber.

Trigger: `src/mailpilot/settings.py` changed.
- `rg 'environment' src/mailpilot/settings.py` -> field present (Literal dev|prd)
- `rg 'mailpilot-topic-|mailpilot-sub-' src/mailpilot/settings.py` -> derive formula
- `rg 'def logfire_environment' src/mailpilot/settings.py` -> zero Settings property
- `rg 'logfire_environment' src/mailpilot/database src/mailpilot/cli` -> zero status/config projection
- `rg 'google_pubsub_topic|google_pubsub_subscription' src/mailpilot/database` -> status config block projects resolved topic/sub

## §V166 — show queue human report hub

`mailpilot show queue` = read-only operator report hub (not CRM noun). Default stdout ASCII table via `tabulate` `tablefmt=simple` (no Unicode box, no ANSI color, no TTY-variant bytes). `--format json|table` (default table). No csv/ndjson. Errors stay JSON stderr + exit 1. `--format json` envelope `{"queue":{grain,tz,rows},"ok":true,"record_count":N}` N=len(rows). `grain` in {`workflow`,`task`}. `--detail` -> task grain else workflow grain.

Workflow grain: 1 row / workflow in scope incl draft|active|paused; omit `--workflow-name` = every workflow; columns {workflow_name, status, t1, t2, t3, t4p, next_at} only (drop active, pending, overdue, due_today, failed_24h, never_sent; keep status). tN = pending-task count whose resolved touch = N (§V.162 parse + first-touch trigger fallback). t4p = resolved touch ≥4. table+JSON `next_at` = full ISO datetime in `--tz` (empty if none; ISO offset required); next_at = MIN pending scheduled_at (all pending, not t1-only); sort next_at ASC (empty last) then name; no `--limit`.

Task grain: 1 row / pending task; sort scheduled_at ASC (queue order; ! change `list_tasks` DESC); table+JSON cols {workflow_name, company_domain, contact, email, touch, attempts, next_at} only (drop when, trigger, state); hide UUIDs on table; JSON ? include task_id + enrollment_id; `--limit` default 100; `--overdue` = pending + scheduled_at < now; stuck-without-task ! rows (stays §V.155 enrollment/report `--stuck`).

`--workflow-name` name|UUID §V.107 unknown -> not_found; flag matches table+JSON col `workflow_name` (name, not UUID). `--tz` IANA default host local (TZ env or OS zoneinfo); omit → host TZ; explicit `--tz` overrides; unresolvable local → UTC. table+JSON `next_at` = full ISO datetime in `--tz` (same both grains; ISO offset required). Envelope `tz` = resolved IANA name. `touch` = T<n> via §V.162 parse + first-touch trigger fallback (`enrollment_schedule` / `enrollment_run` → T1 when touch absent). No LLM. No CRM write. Entity JSON verbs byte-stable. Empty -> `(no rows)` exit 0.

Trigger: `show queue` path changed.
- `rg 'show.*queue|def show_queue' src/mailpilot/cli` -> command present
- `rg 'tabulate|tablefmt' src/mailpilot/` -> tabulate simple renderer
- `rg 'QueueWorkflowRow|QueueTaskRow|QueueReport' src/mailpilot/models.py` -> read models

## §V167 — company create oneshot profile+tags

`company create` accepts §V.140 profile flags (`--profile-json` | `--profile-file` | `--profile -` XOR field-patch) + repeatable `--tag <name>` same invocation. One txn: company row + optional profile write + additive tag links. Invalid profile → `validation_error` zero writes. Undefined tag → `not_found` never auto-create (§V.116) zero writes. `--tag` additive (same as `tag add` §V.141); already-linked → skip no dup; not `tag set` replace. `--upsert` field-selective per §V.147: profile written only when profile flags passed; bare upsert never wipes profile; second identical call exit 0 + update profile if flags + no tag dups. Success envelope = company entity w/ `has_profile` true when profile flags passed + `tags[]` incl requested names + `created` flag + record_count=1. `--stdin` NDJSON batch of oneshot rows = follow-on (not this row). --skill preferred agent path uses oneshot; help zero SPEC cites §V.111

Trigger: `company create` path changed.
- `rg '--tag|profile.file|profile_file' src/mailpilot/cli` -> create accepts profile + tag flags
- `rg 'def create_company|company create' src/mailpilot/cli` -> create handler present

## §V168 — company view --full inspect dossier

`company view --full` embeds `contacts[]` (lean contact fields incl. `tags[]`; omit `verification_meta` unless `--include-meta`) + existing `tags[]` + `notes[]` on the company envelope. Lean `company view` (no `--full`) unchanged. Distinct from `company list --full` (profile.summary only, §V.8).

Trigger: `company view` path changed.
- `rg 'list_company_inspect_contacts' src/mailpilot/` -> inspect-dossier loader
- `rg 'company view --full|include-meta' src/mailpilot/cli` -> --full + meta opt-in

## §V169 — OOO pause-resume

active outbound + inbound OOO/temporary-absence auto-reply (detect: subject Automatic reply / Auto-Submitted / agent OOO class) → no reply incl. no §V.131 fallback ACK; ! conclude (distinct §V.161); last-day-was past → §V.179 DNC not pause; ! bump last_touch/emails_sent; §V.123 cancel pending follow-ups; harness schedule resume @ parseable return date (context.touch numeric §V.162, reason=ooo_pause); explicit year in body wins; year-less week-range containing now → resume day-after range-end same year (! next-year on range-start ≤ now); year-less same-day month+day (`on <Month> <D>`, named day = today, no year) → resume ≥ next calendar day same year (! next-year) (closes §B.142); year-less weekday-month-day leave-start/notice (effective/begins, not until/returning/back) + this-year date months past → unparseable ! next-year; multi year-less month-day → resume @ stated return / "fully back online" (still-ahead same year), never earlier event-week month-day; "week of <Month> <D>" = event-week not return; past event-week ! year+1 (closes §B.145); unparseable → +touch_interval_days (or +3d if NULL cadence); enrollment stays active, disposition null; OOO inbound ! count as inbound-reply for §V.83 pre-flight

Trigger: inbound OOO / auto-reply / cadence resume path changed.
- `rg 'ooo_pause|_maybe_ooo_pause' src/mailpilot/` -> pause+resume path
- `rg 'Automatic reply|Auto-Submitted' src/mailpilot/ooo.py` -> mechanical detect
- `rg '_FALLBACK_ACKNOWLEDGEMENT|_ack_or_ooo_pause' src/mailpilot/run.py` -> OOO exempt from fallback ACK
- `rg 'is_mechanical_ooo|is_ooo_auto_reply' src/mailpilot/run.py src/mailpilot/database` -> OOO excluded from §V.83 replied-after
- fixture: same-day `on <Month> <D>` no year → resume ≥ next day same year not next-year
- fixture: event-week "week of August 17th" + "fully back online on Monday, August 24th" → resume 2026-08-24 not 2027-08-17

## §V179 — last-day-past left-company

last-day-past left-company — active outbound + inbound auto-reply past-tense last-day ("last day was", "last day with … was", last-day date already past) even when subject Automatic reply or Auto-Submitted → conclude do_not_contact (cancel follow-ups + disable contact); named successors without emails stay in the note, never a 1-year pause; ! OOO pause/year-roll on the last-day date (§V.169); distinct from address-change (§V.161) and thread-alias left-company (§V.164); retired / no longer with keep current DNC. Harness `_TERMINAL_AUTO_REPLY` owns detect — playbook last-day bullet alone does not skip OOO.

Trigger: inbound OOO / terminal auto-reply / campaign-test left_company path changed.
- `rg 'last day|_TERMINAL_AUTO_REPLY' src/mailpilot/ooo.py` -> last-day-past in terminal detect
- `rg 'retired|no longer with' src/mailpilot/ooo.py tests/test_ooo.py` -> contrast cases still not OOO
- fixture: "My last day with CLA was Wednesday, July 22" + Automatic reply + named successors w/o emails → not is_mechanical_ooo, not is_ooo_auto_reply, no year-pause
- campaign-test `left_company` expect contact_disabled + no followup year-pause

## §V170 — task-retry-schedule

`task retry` resets failed|cancelled only (§V.49). omit `--scheduled-at` + stored scheduled_at still future → keep stored (no now-reset). `--scheduled-at ISO` → that instant. omit + stored past/now → now (fail-retry). `--scheduled-at` past → validation_error. completed|pending → invalid_state. envelope task entity. retry UPDATE fires task_pending_trigger.

Trigger: `src/mailpilot/cli` or `src/mailpilot/database` `retry_tasks_matching` changed.
- `rg 'scheduled-at' src/mailpilot/cli` -> task retry flag present
- `rg 'retry_tasks_matching' src/mailpilot/database` -> scheduled_at keep-or-override
- `rg 'task retry' src/mailpilot/SKILL.md` -> one-call recipe present

## §V171 — enrollment-add-scheduled-batch

enrollment add scheduled-batch apply — source XOR `--file` | `--tag` (no `--dry-run`) | `--contact-email` (single unchanged §V.32); `--file` exclusive w/ `--tag` + `--contact-email`; apply source (`--file` or `--tag` w/o dry-run) ! `--scheduled-at` ISO future (else `validation_error`); inbound workflow → `invalid_state`; `--file` = JSON array of email strings or `{email, scheduled_at?}` objects; missing path → `not_found`; bad JSON / missing email → `validation_error`; unknown email → `not_found` zero writes; `--tag` expand = §V.150 candidate set (drop already-enrolled this workflow + self-loop §V.33 + disabled company/contact §V.114); `--min-contacts` same pre-expand filter; `--exclude-peer` drops contacts w/ other-workflow active enrollment; `--limit N` (N≥1) caps included seats — w/o `--company-atomic` hard cap first N in company_domain then email order; w/ `--company-atomic` soft cap: take whole company atoms in that order, last atom may exceed N, never split a domain; `--company-atomic` ! same calendar day (offset of `--scheduled-at` or per-company file override) for every included seat on a domain; file row `scheduled_at` overrides flag per contact; `--company-atomic` + conflicting per-row instants on one domain → `validation_error`; one txn; per-row first-touch writer = §V.32 (insert-once; last-write-wins on `--file` existing emails_sent=0 first-reach; never move T2+); tag apply never restamps already-enrolled; envelope `{"enrollment_batch":{workflow, scheduled_at, source, tag?, limit?, company_atomic, count, enrolled:[{email, company_domain, enrollment_id, scheduled_at, action}], excluded:{disabled_companies, already_enrolled, self_loop, disabled_contacts, peer, over_limit, not_found}},"ok":true,"record_count":count}` action ∈ {created, scheduled_first_send, unchanged}; `--file|--tag --dry-run` + packing flags = preview packed set (no writes; `enrollment_preview` §V.150 + excluded.peer/over_limit); `--tag` w/o `--dry-run` and w/o `--scheduled-at` → `validation_error`; `--limit` <1 → `validation_error`; --skill one-call recipe replaces N-call `--contact-email` loop; help zero SPEC cites §V.111

Trigger: enrollment add flags / first-touch path changed.
- `rg 'company-atomic|--exclude-peer' src/mailpilot/cli` -> batch packing flags present
- `rg 'enrollment_batch' src/mailpilot/` -> apply envelope present
- `rg 'enrollment add' src/mailpilot/SKILL.md` -> one-call batch recipe present

## §V172 — TaskSummary projection

`TaskSummary` ! project `result.reason` (string or null). `task list` failed rows ! carry stored reason so campaign-review classifies fail cause without `task view`. null when unset. Full `result` other keys + `context` stay `task view`. `list_tasks` SELECT projects `result->>'reason'`. Agent one-call recipe drops N `task view` loop.

Trigger: `src/mailpilot/database` or `src/mailpilot/models.py` changed.
- `rg 'result.reason|result_reason' src/mailpilot/models.py` -> TaskSummary carries reason
- `rg "result->>'reason'|result_reason" src/mailpilot/database` -> list_tasks SELECT projects reason

## §V173 — task-cancel-filter

`task cancel` dual-mode: positional TASK_ID XOR filter flags. Filter set = `task list` {`--workflow-id`,`--contact-email`,`--status`,`--trigger`,`--overdue`,`--since`,`--until`} + repeatable `--touch N` (parse §V.162). `--touch` also on `task list`. Filter-mode requires ≥1 of {`--touch`,`--workflow-id`,`--contact-email`,`--trigger`,`--overdue`}. `--status` default pending; other → `validation_error`. TASK_ID+filters → `validation_error`. One txn cancel every matching pending; no default `--limit`. Envelope `{"task_cancel":{cancelled_count,ids[],leftover_pending_by_touch},"ok":true}` record_count=cancelled_count. Zero match → ok no-op. Id-mode entity envelope unchanged. Never `--description`. SKILL one-call replaces list-then-N-cancel.

Trigger: `task cancel` / `task list` path changed.
- `rg 'cancel_tasks_matching|leftover_pending_by_touch' src/mailpilot/` -> filter-mode join present
- `rg 'touch_option|--touch' src/mailpilot/cli src/mailpilot/_filters.py` -> --touch on list + cancel
- `rg 'task cancel --workflow-id|leftover_pending_by_touch' src/mailpilot/SKILL.md` -> one-call recipe present

## §V174 — workflow-review

`workflow review <slug|all> --since ISO --until ISO` dated one-envelope collect (both ISO ! else `validation_error`). slug = one name|UUID (§V.107); `all` = every active (`list_active_workflows` SQL `WHERE status = 'active'`, not Python filter of `list_workflows_full`). Payload ! funnel (§V.132) + task counts {failed,overdue,pending} + window emails w/ snippet (§V.7, via `list_emails`; `_review_window_emails` absent) + window activities incl. inbound email_received w/ snippet + failed tasks w/ contact_email + result.reason (§V.172) + enrollments ! cap below live enrolled count. Envelope `{"workflow_review":{...},"ok":true}`; record_count = review count. No LLM. No CRM write. Fetch workflow once then call stats (§V.184).

Trigger: `workflow review` path changed.
- `rg 'get_workflow_review|workflow_review' src/mailpilot/` -> collect surface present
- `rg 'workflow review' src/mailpilot/SKILL.md` -> one-call recipe present

## §V175 — task-retry-filter

`task retry` dual-mode: positional TASK_ID XOR filter flags. Filter set = `task list` {`--workflow-id`,`--contact-email`,`--status`,`--trigger`,`--overdue`,`--since`,`--until`} + repeatable `--touch N` (parse §V.162). Filter-mode requires ≥1 of {`--touch`,`--workflow-id`,`--contact-email`,`--trigger`}. `--status` default failed; other than failed|cancelled → `validation_error`. TASK_ID+filters → `validation_error`. `--scheduled-at` applies §V.170 to every selected (same day-window). `--dry-run` preview ids+companies no writes. One txn retry every matching failed|cancelled; no default `--limit`. Envelope `{"task_retry":{retried_count,ids[],scheduled_at,companies[{domain,count}],dry_run},"ok":true}` record_count=retried_count. Zero match → ok no-op. Id-mode entity envelope unchanged. `--dry-run` + TASK_ID → `task_retry` envelope no write. Never `--description`. SKILL one-call replaces list-then-N-retry. Distinct from §V.173 cancel.

Trigger: `task retry` path changed.
- `rg 'retry_tasks_matching|task_retry' src/mailpilot/` -> filter-mode join present
- `rg 'dry-run|--touch' src/mailpilot/cli` -> dry-run + touch on retry
- `rg 'task retry --workflow-id|retried_count' src/mailpilot/SKILL.md` -> one-call recipe present

## §V16 — race-safe create

UNIQUE-bearing `create_X` uses `ON CONFLICT DO NOTHING` -> None to race loser, exactly 1 row persists; bulk variants converge to shared ids; CLI surfaces `duplicate_key` envelope

Trigger: `src/mailpilot/database` create paths changed.
- `rg 'ON CONFLICT DO NOTHING' src/mailpilot/database` -> race-safe create present
- `rg 'duplicate_key' src/mailpilot/` -> CLI envelope code present

## §V20 — email.route_method enum

email.route_method NULL or in 7-value enum (schema CHECK, set per §I.cli); non-NULL -> is_routed=TRUE; NULL + is_routed=TRUE = pipeline ran, no match ("unrouted" = span-only label)

Trigger: schema or routing persist changed.
- `rg 'route_method' src/mailpilot/schema.sql src/mailpilot/cli` -> enum + projection present
- `rg 'skipped_outside_window|rfc_message_id_match|thread_match' src/mailpilot/` -> 7-value set present

## §V21 — event-wake loops

background loops wake on events not timers — wakeup_event set by Pub/Sub notify + pg NOTIFY task_pending (INSERT + retry-UPDATE triggers); run_interval tick = fallback only

Trigger: `src/mailpilot/sync.py` or `src/mailpilot/pubsub.py` changed.
- `rg 'wakeup_event' src/mailpilot/` -> event wake present
- `rg 'task_pending' src/mailpilot/` -> pg NOTIFY wake present

## §V28 — task.enrollment_id + _ensure_enrollment

task.enrollment_id NOT NULL; workflow_id + contact_id denorm retained for filters; enrollment guaranteed @ route time via `_ensure_enrollment` — ON CONFLICT once, enrollment_added activity on first insert only

Trigger: `src/mailpilot/routing.py` or task schema changed.
- `rg '_ensure_enrollment' src/mailpilot/routing.py` -> route-time ensure present
- `rg 'enrollment_id' src/mailpilot/schema.sql` -> NOT NULL on task

## §V30 — prompt framing follows trigger

prompt framing follows trigger — first-reach-out (`enrollment_run` + `enrollment_schedule` byte-identical) vs deferred-task vs inbound; inbound email present -> email framing wins; no synthesized task_description

Trigger: `src/mailpilot/agent/` prompt compose changed.
- `rg 'enrollment_run|enrollment_schedule|task_description' src/mailpilot/agent/` -> trigger-keyed framing present
- `rg 'New inbound email' src/mailpilot/agent/` -> inbound email frame present

## §V32 — enrollment_schedule trigger

enrollment_schedule = distinct trigger label (observability split from enrollment_run). `--scheduled-at` on outbound → pending first-touch (email_id NULL, context.trigger=enrollment_schedule, context.touch numeric 1). Insert-once: no second first-reach task, no second enrollment. Re-run `enrollment add --contact-email --scheduled-at` on active emails_sent=0 enrollment w/ pending first-reach: parsed instant differs → UPDATE that task scheduled_at in place + persist touch 1 if absent + `changed` includes `scheduled_first_send`; same instant → no-op `changed=[]`. emails_sent>0 or pending row not first-reach (trigger not in {enrollment_schedule, enrollment_run} or resolved touch ≥2) → no-op (never move T2/T3). inbound workflow → invalid_state. Compare instants not strings. Batch apply first-reach writer same last-write-wins (§V.171).

Trigger: enrollment schedule / first-touch path changed.
- `rg 'enrollment_schedule' src/mailpilot/` -> distinct trigger label present
- `rg 'scheduled.at|scheduled_at' src/mailpilot/cli` -> --scheduled-at first-touch present
- `rg 'scheduled_first_send' src/mailpilot/cli` -> changed token on first-touch write

## §V35 — Drive KB isolation

Drive KB isolation = per-account impersonation (DWD with_subject); account reads only files its identity can read; list/search filter mimeType text/markdown + trashed=false; content decoded UTF-8 errors=replace

Trigger: `src/mailpilot/drive.py` changed.
- `rg 'text/markdown|trashed' src/mailpilot/drive.py` -> list/search filters present
- `rg 'errors=.?replace|utf-8' src/mailpilot/drive.py` -> UTF-8 replace decode

## §V53 — agent tool span source

agent tool spans come from `logfire.instrument_pydantic_ai()` (`gen_ai.tool.name` attr); no `logfire.span` inside agent tools; agents carry explicit names `mailpilot.classifier` + `mailpilot.workflow`

Trigger: `src/mailpilot/cli` or `src/mailpilot/agent/` changed.
- `rg 'instrument_pydantic_ai' src/mailpilot/` -> instrumentation site present
- `rg 'mailpilot.classifier|mailpilot.workflow' src/mailpilot/` -> named agents present

## §V55 — tool-result scrub exemption

`gen_ai.tool.call.result` span attr exempt from Logfire scrubbing; all other attrs scrubbed; scrubbing contract test drives a real instrumented tool call, never a fabricated span

Trigger: logfire scrubbing / agent tool instrumentation changed.
- `rg 'gen_ai.tool.call.result' src/mailpilot/ tests/` -> exemption key present
- `rg 'instrument_pydantic_ai|scrub' tests/test_logfire_scrubbing.py` -> real-call contract test

## §V72 — CompanyProfile JSONB validation

company.profile JSONB validated vs CompanyProfile — required {summary, products, target_customers, sources} non-empty; timezone optional, null on multi-zone; malformed -> validation_error

Trigger: `src/mailpilot/models.py` or company profile write changed.
- `rg 'class CompanyProfile' src/mailpilot/models.py` -> schema present
- `rg 'CompanyProfile.model_validate' src/mailpilot/` -> validate-on-write present

## §V76 — routing eligibility window

routing eligibility window — received_at older than 7 days, zero active workflows, or predates earliest active workflow -> is_routed=TRUE w/ matching skipped_* route_method, no LLM call. Recency, no-workflows, predates-workflow computed once in `sync_account` and passed in; skip marks go through `route_email` or shared `mark_routed` (§V.187). Recency gate still runs before the LLM classifier.

Trigger: `src/mailpilot/sync.py` or `src/mailpilot/routing.py` changed.
- `rg 'skipped_outside_window|skipped_no_workflows|skipped_predates' src/mailpilot/` -> skipped_* methods present
- `rg '7 days|timedelta.days.?=.?7' src/mailpilot/` -> 7-day window present

## §V81 — tool-loop send-or-noop

tool-loop agent run ! call >= 1 tool; `noop(reason)` = explicit no-op escape; zero tool calls -> AgentDidNotUseToolsError; compose-only structured-output runs exempt (§V.136) — validated output IS the action

Trigger: `src/mailpilot/agent/invoke.py` changed.
- `rg 'AgentDidNotUseToolsError' src/mailpilot/` -> zero-tool guard present
- `rg 'def noop' src/mailpilot/agent/` -> noop escape present

## §V85 — settings precedence

`database_url` kwargs > env `MAILPILOT_DATABASE_URL` > cwd `.env` (`MAILPILOT_DATABASE_URL` only) > default `postgresql://localhost/mailpilot`. Other keys kwargs (tests) > `app_config` row > field-literal defaults. No `config.json`. No non-url `MAILPILOT_*`. Missing `.env` = no-op. Field defaults = literals on Settings fields; no unused module-level `DEFAULT_*` constants.

Trigger: `src/mailpilot/settings.py` changed.
- `rg 'bootstrap_database_url|MAILPILOT_DATABASE_URL' src/mailpilot/settings.py` -> URL bootstrap present
- `rg 'config.json' src/mailpilot/settings.py` -> zero hits
- `rg 'app_config' src/mailpilot/settings.py` -> row hydrate present
- `rg 'def get_field_value' src/mailpilot/settings.py` -> zero hits

## §V88 — entity enum schema CHECK

entity enums enforced by schema CHECK — workflow.template/type/status, enrollment.status, email.direction/status/route_method, task.status, activity.type; value sets authoritative in schema.sql

Trigger: `src/mailpilot/schema.sql` changed.
- `rg 'CHECK' src/mailpilot/schema.sql` -> enum CHECKs present
- `rg 'route_method|enrollment.status|workflow.status' src/mailpilot/schema.sql` -> entity enum cols present

## §V92 — email HTML render

email render = Markdown -> HTML inline styles only, no stylesheet; hard_wrap=True (soft newlines -> <br>); body container ! max-width (fluid); THEMES = {blue, green, orange, purple, red, slate}; None/unknown theme -> blue fallback. EmailRenderer ! override mistune hooks that add no inline style (`strong`, `emphasis`, `table_head`, `table_body`, `table_row`); `table` + `table_cell` keep theme styles.

Trigger: `src/mailpilot/email_renderer.py` changed.
- `rg 'hard_wrap' src/mailpilot/email_renderer.py` -> hard_wrap=True present
- `rg 'THEMES' src/mailpilot/email_renderer.py` -> theme enum present
- `rg 'max-width' src/mailpilot/email_renderer.py` -> zero body max-width
- `rg 'def (strong|emphasis|table_head|table_body|table_row)\b' src/mailpilot/email_renderer.py` -> zero hits

## §V1 — two-phase settings load

two-phase settings load — bootstrap `database_url` (kwargs > env `MAILPILOT_DATABASE_URL` > `.env` > default `postgresql://localhost/mailpilot`); DB connect + schema gate; hydrate `Settings` from `app_config`; network/Logfire after; `--help`/`--version` skip load; `db init|migrate|check` bootstrap only; `mailpilot run` re-SELECT `app_config` each tick; `database_url` process-lifetime

Trigger: `src/mailpilot/settings.py` or `src/mailpilot/run.py` or `src/mailpilot/cli` changed.
- `rg 'bootstrap_database_url|MAILPILOT_DATABASE_URL' src/mailpilot/settings.py` -> URL bootstrap present
- `rg 'app_config' src/mailpilot/settings.py src/mailpilot/run.py` -> hydrate + tick re-SELECT
- `rg 'skip load|--help|--version' src/mailpilot/cli src/mailpilot/settings.py` -> help/version skip load
- `rg 'initialize_database' src/mailpilot/cli` -> db init|migrate|check bootstrap path

## §V37 — service-account DWD auth

auth = service account + domain-wide delegation; JSON `google_application_credentials` on `app_config` -> `from_service_account_info` + with_subject(email); null column -> ADC iam.Signer credentials w/ subject=email; no file-path setting; no OAuth user login

Trigger: Google client construction (`gmail.py` / `drive.py` / `calendar.py` / settings creds) changed.
- `rg 'from_service_account_info|with_subject' src/mailpilot/` -> JSON creds + subject impersonation
- `rg 'iam.Signer|service_account.Credentials' src/mailpilot/` -> ADC signer path
- `rg 'GOOGLE_APPLICATION_CREDENTIALS' src/mailpilot/settings.py` -> not a mailpilot settings source

## §V52 — logfire environment map

logfire.configure maps settings.environment internally (dev→development, prd→production per §V.176) -> spans carry deployment_environment; cloud queries filter by env; no `logfire_environment` setting

Trigger: `src/mailpilot/settings.py` or logfire.configure call site changed.
- `rg 'logfire.configure' src/mailpilot/` -> configure present
- `rg 'deployment_environment|environment=' src/mailpilot/` -> env map passed
- `rg 'def logfire_environment' src/mailpilot/settings.py` -> zero Settings property

## §V86 — secret settings redaction

secret settings (`anthropic_api_key`, `xai_api_key`, `logfire_token`, `database_url`, `google_application_credentials`) redacted as '***' in telemetry; config.set event logs key + changed flag

Trigger: `src/mailpilot/settings.py` or `src/mailpilot/cli` config path changed.
- `rg '\*\*\*' src/mailpilot/settings.py src/mailpilot/cli` -> secret redaction present
- `rg 'config.set|config_set' src/mailpilot/` -> set event logs key + changed

## §V177 — CLI db helper

cli-db-context — `_db(*, mutate: bool = False)` sole CLI connection helper; lazy-import `initialize_database` (module-level click-only §V.2); `mutate=True` → `require_current_schema=True` (§V.109); mutate success → `connection.commit()` before close; exception/`output_error` skip commit (rollback); close success+error; `cli_mutation` stays per-cmd outside helper (§V.54); cmds use helper not copy open/close

Trigger: `src/mailpilot/cli` changed.
- `rg 'def _db\(' src/mailpilot/cli` -> sole helper present
- `rg 'initialize_database' src/mailpilot/cli` -> lazy-import inside helper
- `rg 'connection.commit' src/mailpilot/cli` -> mutate success commit
- `rg 'cli_mutation' src/mailpilot/cli` -> span stays per-cmd outside helper

## §V178 — shared query fragments

shared-query-fragments — `list_companies`/`export_companies` (and import via export) share one WHERE/HAVING builder for profile/pipeline/contact-count/tag predicates; `--tag`/`--no-tag` resolve through the existing tag-id helper; include/exclude tag filters one helper `negate` flag; EmailSummary/WorkflowSummary/TagSummary list+search (and review emails) share one SELECT list each; review window emails uncapped matching `list_emails` filters/order; export still unlimited `ORDER BY domain` + tracker-shaped dicts (§V.145)

Trigger: `src/mailpilot/database` or company/email/workflow/tag list SQL changed.
- `rg 'def _company_scope_clauses' src/mailpilot/database` -> shared WHERE/HAVING builder
- `rg 'negate' src/mailpilot/database` -> include/exclude tag helper flag
- `rg 'list_emails' src/mailpilot/cli` -> review reuses list_emails
- `rg 'ORDER BY domain' src/mailpilot/database` -> export unlimited domain order

## §V180 — task filter stack share

task-filter-stack-share — `task list|cancel|retry` share `@task_scope_options` in `_filters.py` (`--workflow-id`,`--contact-email`,`--status`,`--trigger`,`--overdue`,`@touch_option`,`@time_window_options("scheduled_at")`); one `_task_filter_mode(task_id, *, required, allowed_status)` encodes TASK_ID XOR filters; one `_resolve_task_scope` loads workflow+contact; id retry uses `retry_tasks_matching(task_id=...)` then `get_task`; drop `manual_retry_task` when tests use bulk path; `get_task_stats` uses `_task_filter_clauses`; cancel vs retry required-filter + status stay distinct (§V.173, §V.175)

Trigger: `src/mailpilot/cli` or `src/mailpilot/_filters.py` or task DB filter path changed.
- `rg 'def task_scope_options|task_scope_options' src/mailpilot/` -> shared option decorator
- `rg 'def _task_filter_mode' src/mailpilot/cli` -> XOR helper present
- `rg 'def _resolve_task_scope' src/mailpilot/cli` -> scope loader present
- `rg 'retry_tasks_matching' src/mailpilot/` -> id + bulk retry path
- `rg 'def manual_retry_task' src/mailpilot/` -> zero hits when tests use bulk
- `rg '_task_filter_clauses' src/mailpilot/database` -> stats shares clauses

## §V181 — app_config singleton

app-config-db — table `app_config` id='singleton'; typed cols every Settings field except `database_url` + derived pubsub keys; schema CHECKs own enums; col defaults = field-literal defaults; `google_application_credentials` JSONB document (null → ADC); missing row @ load insert defaults; `config get|set` read/write the row; `config set` persist visible on new connection; `config set` of `database_url` or derived pubsub keys or unknown → `invalid_key`; invalid JSON for credentials → `validation_error`

Trigger: `src/mailpilot/schema.sql` or `src/mailpilot/settings.py` or `src/mailpilot/cli` config path changed.
- `rg 'CREATE TABLE app_config' src/mailpilot/schema.sql` -> singleton table present
- `rg 'google_application_credentials' src/mailpilot/schema.sql src/mailpilot/settings.py` -> JSONB col + load
- `rg 'invalid_key' src/mailpilot/cli` -> config set rejects database_url + derived pubsub
- `rg "id=.singleton." src/mailpilot/` -> singleton id

## §V183 — email-history pre-feed

invoke_workflow_agent loads enrollment-scoped email history (account_id, contact_id, workflow_id per §V.82) in one query returning full Email rows including `body_text`. Not `list_emails` (EmailSummary, no body) then N `get_email`. `_format_email_history` caps already-loaded `body_text` at 500 (same length as §V.7 snippet). Trigger email excluded from history per §V.29. `read_email` remains on roster for `search_emails` hits outside this enrollment; cross-account still None per §V.87. Distinct from §V.135 (contact/company records; those read tools dropped).

Trigger: `src/mailpilot/agent/invoke.py` or `src/mailpilot/database` email list path changed.
- `rg 'list_emails' src/mailpilot/agent/invoke.py` -> zero N+1 hydrate via `get_email` per summary
- `rg 'get_email\(connection, s.id\)' src/mailpilot/agent/invoke.py` -> zero hits
- `rg 'body_text\[:500\]' src/mailpilot/agent/invoke.py` -> cap on already-loaded body
- `rg 'read_email' src/mailpilot/agent/templates.py` -> still bound

## §V184 — campaign query stack

campaign-query-stack — `get_workflow_stats` takes already-loaded `Workflow` (no inner `get_workflow`); `get_workflow_report` / `get_workflow_status_health` / `_review_one_workflow` fetch workflow once then call stats; one `_sql_outbound_sent_count(e)` Composed fragment reused by stats funnel+touch, enrollment `--full` `emails_sent`, `count_outbound_sent`; `--full` `emails_sent AS last_touch` keeps JSON key (same count, two aliases); `_review_window_emails` absent — review window emails via `list_emails` (§V.178); `list_active_workflows` `WHERE status = 'active'` not Python filter of `list_workflows_full`; status `wording` via `check_workflow_wording` never hardcoded `"unknown"`; CLI verbs {stats,report,status,review} + envelope keys {workflow_stats,workflow_report,workflow_status,workflow_review} unchanged. Distinct from §V.178 (company/email/workflow/tag SELECT share) and §V.185 (enrollment list/preview stack).

Trigger: `src/mailpilot/database` campaign stack changed.
- `rg 'def get_workflow_stats' -A 12 src/mailpilot/database` -> takes Workflow not workflow_id fetch
- `rg 'def _sql_outbound_sent_count' src/mailpilot/database` -> fragment present
- `rg 'AS last_touch' src/mailpilot/database` -> alias keeps JSON key
- `rg 'def _review_window_emails' src/mailpilot/` -> zero hits
- `rg 'def list_active_workflows' -A 20 src/mailpilot/database` -> SQL WHERE status active
- `rg 'wording=.unknown.|wording="unknown"' src/mailpilot/` -> zero hits
- `rg 'check_workflow_wording' src/mailpilot/database` -> status health wires check
- `rg '"workflow_stats"|"workflow_report"|"workflow_status"|"workflow_review"' src/mailpilot/cli` -> envelope keys stay

## §V185 — enrollment list stack

enrollment-list-stack — one `_enrollment_parent_select()` Composed fragment used by row+list loaders {create_enrollment, get_enrollment, get_enrollment_by_id, list_enrollments, list_active_outbound_enrollments_for_contact, disable_enrollment, enable_enrollment}; not pasted JOIN SQL. `list_enrollments_detailed` splits `_enrollment_where` + lean SELECT + full SELECT. Outcome LATERAL (latest enrollment_completed/failed) folds into `list_enrollments_detailed(full=True)`; `list_enrollments_with_outcomes` + `EnrollmentWithOutcome` absent. Agent tool `list_enrollments` still exposes {latest_outcome, latest_outcome_reason, latest_outcome_at} (completed/failed). Preview: enrolled set = SELECT contact_id FROM enrollment (not hydrate `list_enrollments`); one `_preview_from_contacts` covers exclude + tag + peer hydrate; tag preview `company_id = ANY(...)` not per-company `list_contacts` loop. `--touch` on enrollment list stays `_sql_parse_touch` (not `_sql_resolve_touch`) unless tests prove resolve is the intended match. CLI lean/full fields + envelope key `enrollments` unchanged (§V.152); preview envelope unchanged (§V.150). Distinct from §V.178 (company/email/workflow/tag SELECT share) and §V.184 (campaign stats/report/status/review).

Trigger: `src/mailpilot/database` enrollment list/preview path changed.
- `rg 'def _enrollment_parent_select' src/mailpilot/database` -> parent SELECT fragment
- `rg 'def _enrollment_where' src/mailpilot/database` -> shared WHERE
- `rg 'def list_enrollments_with_outcomes' src/mailpilot/` -> zero hits
- `rg 'class EnrollmentWithOutcome' src/mailpilot/` -> zero hits
- `rg 'latest_outcome' src/mailpilot/agent/tools.py src/mailpilot/models.py` -> agent envelope keys stay
- `rg 'def _preview_from_contacts' src/mailpilot/database` -> shared preview helper
- `rg 'company_id = ANY' src/mailpilot/database` -> tag preview batched

## §V186 — conclude enrollment helper

conclude-enrollment-helper — one internal helper takes `enrollment_id`, `disposition`, `reason`, plus `reschedule_at`, `note`, `skip_if_terminal`. Always records outcome + cancels follow-ups (§V.123 first-touch exclusion). Optional disable / note / reschedule: `do_not_contact` disables contact (agent); bounce contact disable stays §V.80; `note` set → `create_note`; `reschedule_at` set → re-enrollment task (agent `contact_later`); omitted `reschedule_at` → no task (cadence). `skip_if_terminal` true → skip when latest outcome exists (bounce); false → still conclude (booking default; agent default). `meeting_booked` writes a note. Cadence `contact_later` "sequence exhausted" does not create a re-enrollment task. Agent tool validates LLM input then calls; bounce/booking/cadence pass system reasons. `record_enrollment_outcome` stays system-internal, not an agent tool (§V.127/§V.15). Distinct from inbound-reply cancel (§V.123 direct).

Trigger: conclude helper or agent/bounce/booking/cadence conclude path changed.
- `rg 'skip_if_terminal' src/mailpilot/` -> flag present
- `rg 'record_enrollment_outcome' src/mailpilot/agent/tools.py src/mailpilot/cadence.py src/mailpilot/routing.py src/mailpilot/sync.py` -> zero hits (helper, not pasted)
- `rg 'def conclude_enrollment' src/mailpilot/` -> helper + agent tool

## §V187 — route-email skip marks + thread-contact cache

route-email-skip-and-thread-cache — skip marks (`skipped_outside_window`, `skipped_no_workflows`, `skipped_predates_workflows`) go through `route_email` or shared `mark_routed(email, method)`; `_store_inbound_message` never opens a `routing.route_email` span + `update_email(is_routed=True)` without that path. Recency, no-workflows, predates-workflow computed once in `sync_account` and passed in; recency gate still before LLM classifier (§V.76). Thread-enrolled contact resolved once per inbound message — account-scoped cache keyed by thread + In-Reply-To; `_thread_bound_sender_emails`, `_store_inbound_message`, `route_email` share that result. RFC parent lookup not queried twice for the same headers. `route_method` enum values stay the same (§V.20). Bind-versus-From alias stays §V.164.

Trigger: `src/mailpilot/routing.py` or `src/mailpilot/sync.py` changed.
- `rg 'skipped_outside_window|skipped_no_workflows|skipped_predates_workflows' src/mailpilot/routing.py` -> skip marks owned in routing
- `rg 'routing.route_email' src/mailpilot/sync.py` -> zero span names in sync
- `rg 'find_thread_enrolled_contact' src/mailpilot/sync.py src/mailpilot/routing.py` -> shared resolve, not three independent walks

## §V188 — mechanical OOO no inbound agent task

mechanical-ooo-no-agent-task — `is_mechanical_ooo` inbound on outbound enrollment after `route_email` → `_maybe_ooo_pause` schedules resume once; `create_tasks_for_routed_emails` never inserts `handle inbound email` for that email; later sync ! re-enqueue that inbound (processed marker or equivalent); language-only (`is_ooo_auto_reply` and not `is_mechanical_ooo`) still enqueues agent; `_maybe_ooo_resume_after_invoke` stays for agent `noop`; `_ack_or_ooo_pause_on_failure` stays for language-only terminal fail (skip ACK). Pause/no-ACK/resume-date stay §V.169. campaign-test `mechanical` replies present Automatic-reply subject and/or AUTO_SUBMITTED before `route_email` so `_maybe_ooo_pause` fires; stamp-after-route + leftover `_complete_mechanical_ooo` ! second resume.

Trigger: OOO route / inbound-task enqueue / run-loop mechanical-OOO path changed; `.grok/skills/mailpilot-campaign-test/scripts/handle_replies.py` changed.
- `rg 'is_mechanical_ooo' src/mailpilot/database src/mailpilot/routing.py` -> enqueue skip or route processed-marker
- `rg '_complete_mechanical_ooo' src/mailpilot/run.py` -> leftover-only or not a second resume site
- `rg '_stamp_mechanical|_wait_for_routing' .grok/skills/mailpilot-campaign-test/scripts/handle_replies.py` -> mechanical signal before `_wait_for_routing` or inject
- fixture: mechanical Automatic reply after route → zero handle-inbound-email tasks, one ooo_pause resume
- fixture: language-only absence body w/o Automatic reply / Auto-Submitted → inbound agent task created
- isolation: campaign-test mechanical stamp/inject precedes `_wait_for_routing`
