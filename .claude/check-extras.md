## SKILL.md Drift Check

Mechanical audit (no LLM-judgment); trigger when `src/mailpilot/SKILL.md`, `.claude/skills/**/*.md`, `src/mailpilot/cli.py`, or `src/mailpilot/settings.py` changed.

File-set scope:
- `src/mailpilot/SKILL.md` — packaged skill body (external LLM agents); all four checks apply.
- `.claude/skills/**/*.md` — operator-facing skill bodies (test-google-drive, lead-companies, etc.); per `§B.65` only checks (i) and (ii) apply (skill bodies do not enumerate settings so (iii) and (iv) do not apply).

Checks:
(i) per-noun verb roster is a superset of `@<noun>.command("<verb>")` set in `cli.py` — fail mode: skill names a retired verb (e.g. `enrollment remove` post-T92).
(ii) per-verb `--<flag>` tokens in recipes are a subset of `@click.option("--<flag>")` set for that handler in `cli.py`.
(iii) settings key list in `## Settings` == `Settings.model_fields` keys in `settings.py` — `src/mailpilot/SKILL.md` only.
(iv) env-var-prefix description in `## Settings` == `SettingsConfigDict(env_prefix=...)` value in `settings.py` (`MAILPILOT_*`) — `src/mailpilot/SKILL.md` only.

## §V.42 — Email body format-check rejection algorithm

Trigger when `src/mailpilot/agent/` or `src/mailpilot/email_renderer.py` changed.

Rejection condition: >= 3 consecutive spec-shape lines (short label + whitespace + value) in reply body w/o `|---|` separator -> `format_check_mismatch`. ASCII rule-lines (`---`, `===`, `___`) not treated as separators.

_SPEC_TABLE requirement: MUST explicitly mandate GFM pipe table w/ header row + `|---|` separator for spec rows (model numbers, flow rates, dimensions, capacities); the prompt mandate lives in the _SPEC_TABLE fragment composed into inbound templates' protocol_pre only (inbound-general + inbound-google-drive), NOT outbound-general; "may use Markdown tables" (permissive) alone insufficient — format-lint is backstop, not primary enforcement. _check_spec_table stays email-universal core (§V.45 format glue) — fires on send_email + reply_email both; §V.71 caps any retry loop.

Mechanical check:
- `rg -n 'may use Markdown' src/mailpilot/agent/templates.py` -> zero hits (permissive wording retired).
- _SPEC_TABLE composed into inbound-general + inbound-google-drive protocol_pre only; outbound-general protocol_pre = _BASE alone (no pipe-table mandate) — read the TEMPLATES dict in templates.py; the composed-protocol test asserts outbound carries no `pipe table` / `flow rates` / `product specifications`.

## §V.45 — no SPEC citation in agent-visible text

Trigger when `src/mailpilot/agent/templates.py` or `src/mailpilot/agent/tools.py` changed.

Agent-visible text = the composed protocol string + every registered tool's model-visible schema. pydantic-ai derives a tool's description AND per-parameter help from the registered function's full docstring (Args/Returns included), so a `§V/§T/§B.<n>` token anywhere in a registered tool's docstring leaks dead authoring metadata into the reply-agent prompt (`§B.79`: `_BASE` literal `(§V.42)`; `§B.84`: six tool-docstring Args/Returns cites). The governing invariant is cited in an adjacent code comment, never the model-visible string.

Scope of "registered tool docstrings" = the source functions in `tools.py` named by `TEMPLATES[*].tools` (`send_email`, `reply_email`, `create_task`, `cancel_task`, `record_enrollment_outcome`, `disable_contact`, `list_enrollments`, `search_emails`, `read_email`, `noop`, `list_drive_markdown`, `read_drive_markdown`, `search_drive_markdown`). Internal helpers (`_check_spec_table`) + module comments are NOT registered, so their §-cites are exempt — flag a hit only when it sits inside a registered tool's `"""docstring"""`.

Mechanical checks:
- `rg -n '§[VTB]\.[0-9]+' src/mailpilot/agent/templates.py` -> classify each hit: code comment -> exempt; composed-protocol fragment string (`_BASE`, `_DECLINE`, `_DEFERRED_TASK_*`, `_NO_FABRICATION`) -> fail (move the cite to a comment).
- `rg -n '§[VTB]\.[0-9]+' src/mailpilot/agent/tools.py` -> classify each hit: comment / helper docstring -> exempt; inside a registered tool's docstring (per the roster above) -> fail (move the cite to a comment or drop it).

## §V.73 — Skill-body Workflow snippet executability

Mechanical audit; trigger when `.claude/skills/**/*.md` or `.claude/workflows/*.js` changed. Scope = every fenced ```js block that calls `parallel(`, `pipeline(`, or `agent(`, plus the saved-workflow byte-identity check (d).

Per ```js block:
(a) Free-symbol scan — every identifier used as a value ! resolve to an in-block definition (`const` / `let` / `function` / param) OR a runtime global. Runtime globals (do not flag): `meta`, `agent`, `parallel`, `pipeline`, `phase`, `log`, `args`, `budget`, `workflow`, plus JS built-ins (`JSON`, `Math`, `Array`, `Object`, `Promise`, `console`, ...). Any other bare identifier (e.g. `stale`, `buildPrompt`, `ENRICH_RESULT_SCHEMA`) ! be defined in the block — fail mode: free var crashes `ReferenceError` on paste (`§B.68`: bare `stale`).
(b) `args`-as-collection guard — if the block calls `args.map` / `args.filter` / `args.slice` / `args.length` / `args.forEach` or spreads `args`, it ! first `JSON.parse(args)` (or guard `typeof args === 'string'`). Why: runtime delivers `args` as a JSON STRING so `args.map` throws `is not a function` (`§B.68`).
(c) Prose-vs-`parallel` divergence — if surrounding prose claims "concurrency N" / "N concurrent" / "Default N", the block ! chunk to N (batch loop of size N around `parallel(batch.map(...))`). A bare `parallel(xs.map(...))` dispatches all `xs.length`, bounded only by runtime cap `min(16, cores-2)` — not N. Fail mode: prose promises 3, snippet runs all (`§B.68` secondary).
(d) Saved-workflow byte-identity — every embedded workflow snippet's post-`meta` body (each `.claude/skills/<skill>/SKILL.md` FIRST js-fenced block, sliced @ first `\n}\n` after `export const meta`) ! be byte-identical to its saved `.claude/workflows/<name>.js`'s post-`meta` body (same slice). Audited pairs (extend the PAIRS list below when a new skill+workflow lands): `lead-companies/SKILL.md` <-> `lead-companies-enrich.js`; `lead-contacts/SKILL.md` <-> `lead-contacts-find.js`. Why: the skill-body embedded snippet is the spec-of-record; the saved file is invoked by name @ runtime so silent divergence ships an unaudited workflow ((a)-(c) cover the saved file only transitively, when bodies match). Saved `meta` MAY add registry-only fields (`whenToUse`, fuller `description`) so compare the post-`meta` slice only, not the whole file. Fail mode: divergence -> saved-file unaudited drift.

Mechanical greps (manual judgment on hits):
- `rg -n '```js' .claude/skills/` — enumerate blocks.
- `rg -nE '\bargs\.(map|filter|slice|length|forEach)\b' .claude/skills/` not preceded by `JSON.parse(args)` or `typeof args` -> (b) fail.
- prose `rg -niE 'concurrency [0-9]|[0-9] concurrent|default [0-9]' .claude/skills/` near a block with bare `parallel(` and no batch loop (`for .* += N` / `.slice(`) -> (c) fail.
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

## §V.74 — RFC-4180 CSV-ingestion parser mandate

Mechanical audit; trigger when `.claude/skills/**/*.md`, `.claude/skills/**/scripts/*.py`, or `src/**` changed. Scope = CSV-ingestion sites (handle a `.csv` path, a "CSV mode", or a comma-delimited lead export). The grep scope `.claude/skills/ src/` already recurses into `scripts/` so a `.py`-under-`scripts/` change is covered once the trigger-glob (previously `.md`-only) names it.

Checks:
(i) CSV ingestion ! use an RFC-4180 parser (`csv.DictReader` / `csv.reader` / the `csv` module). Fail mode: physical-line iteration, `.splitlines()`, `.split("\n")`, or `.split(",")` over CSV content — quoted fields carry embedded newlines and commas so one logical row spans many physical lines (`§B.69`: theirstack.csv 25 logical rows over 217 physical lines).
(ii) Redirect resolution ! use `curl -sL -o /dev/null -w '%{url_effective}'` (full chain, CR-free). Fail mode: HEAD `curl -sLI | grep '^location:' | awk` — 403 bot-blocking origins answer HEAD differently; awk retains the header trailing CR so corrupts a bare-host redirect target (`§B.69`).

Mechanical greps (manual judgment on hits — flag only in CSV context):
- `rg -n 'splitlines|\.split\(' .claude/skills/ src/` near `csv` / `CSV` / `.csv` context -> (i) fail. Non-CSV `splitlines` (email-body normalization, markdown line scan) not flagged.
- `rg -n 'curl -sLI' .claude/skills/ src/` -> (ii) fail (HEAD-grep redirect resolution).

Plain-text (non-CSV) line iteration is admitted (per-line domain/URL, `#`-comment skip) — do not flag.

## §V.99 — Skill-path resolution check

Trigger when `.claude/skills/**/*.md` changed.

Checks:
(i) Script refs (`uv run python .claude/skills/**/scripts/*.py`) each resolve on disk.
(ii) Source-of-truth dirs named in prose or runtime recovery gates exist.

Cited-but-absent path = recovery instruction that errors when the operator needs it.

Mechanical greps (manual judgment on hits):
- `rg -n '\.claude/skills/\S+\.py' .claude/skills/` -> each cited `.py` path must exist at that path.
- Backticked dir refs: `rg -n '`\.claude/skills/[^`]+/`' .claude/skills/` -> each cited dir must exist on disk. Non-dir backtick refs exempt.

## §V.100 — Skill-body progressive-disclosure audit

Trigger when `.claude/skills/**/*.md` changed.

Checks:
(i) Body >~500 lines = VIOLATE (procedure buried among run-end-only material -> extract to `references/*.md`).
(ii) Near-verbatim prose shared across sibling skills MUST live in one shared `references/*.md` both cite, not copied per-skill.

Mechanical checks:
- `wc -l .claude/skills/*/SKILL.md | sort -rn` -> flag files over 500 lines for extraction review.
- `rg -c 'Conventions|batch gate|Next block' .claude/skills/*/SKILL.md` -> any term in multiple skill bodies -> check for shared-reference extraction opportunity.

## §V.101 — must-sense ` ! ` ban

Mechanical audit; trigger when `.claude/skills/**/*.md` changed. Scope = skill-body prose. A hard requirement ! be marked w/ an explicit word (`MUST` / `required`); a bare telegraph ` ! ` (must-glyph) in prose reads as negation in code, so a model executing the skill can invert the constraint (silent constraint flip — the failure §V.101 was authored to block).

Exempt (not flagged):
- backticked / fenced-code ` ! ` — `[ ! -f ]`, `!=`, `! cmd` inside an inline-code span or a ```fence``` (shell test / negation operator, not a prose obligation).
- SPEC.md + this file (`.claude/check-extras.md`) — telegraph register, both outside the `.claude/skills/**` scope; their ` ! ` is the authored must-glyph, not a violation.

Checks:
(i) zero bare must-sense ` ! ` in `.claude/skills/**/*.md` prose. Fail mode: ` ! ` standing in for "must" in an instruction line (e.g. `the body below meta ! stay byte-identical`, `CSV mode ! parse with an RFC-4180 parser`) — convert to `MUST`.

Mechanical grep (manual judgment on hits — flag only must-sense prose, not backticked/fenced shell):
- `rg -n ' ! ' .claude/skills/` -> classify each hit: backticked-shell / fenced-code -> exempt; prose obligation -> (i) fail (convert to `MUST`). Zero hits -> pass.

## §V.102 — Skill frontmatter hygiene audit

Trigger when `.claude/skills/**/*.md` changed.

Checks:
(i) Every `.claude/skills/**/SKILL.md` sets `allowed-tools` (scoped safety rail).
(ii) Every `.claude/skills/**/SKILL.md` sets `argument-hint` (invocation shape).
(iii) `description:` = triggering intent; vendor names + pipeline-stage rosters belong in body.

Mechanical greps (manual judgment on hits):
- `rg --files-without-match 'allowed-tools' .claude/skills/*/SKILL.md` -> each listed file = missing key (VIOLATE). (`rg -L` is `--follow`, not files-without-match — it prints matches, inverting the read.)
- `rg --files-without-match 'argument-hint' .claude/skills/*/SKILL.md` -> each listed file = missing key (VIOLATE).
- `rg -n '^description:' .claude/skills/*/SKILL.md` -> review for vendor roster or full pipeline-stage detail in trigger text.

## §V.117 — batch-gate option distinctness

Mechanical audit; trigger when `.claude/skills/lead-companies/**` or `.claude/skills/lead-contacts/**` changed. Scope = the shared Batch-gate § in `.claude/skills/lead-companies/references/lead-pipeline-conventions.md`.

`§B.98`: the unconditional fixed cap (`First 24`) capped to all rows once the stale-count reached the cap, so `First 24` and `All <N>` dispatched one identical batch. §V.117 fixes this pre-cap (during option construction): every gate option maps to a distinct batch at the current stale-count, so a fixed-cap option is suppressed once its cap reaches the stale-count (`First 24` dropped at stale-count <= 24, == `All <N>` there; `First 9` always distinct since the gate fires only at stale-count > 9). Per §V.100 the rule lives once in the conventions file; the sibling SKILL bodies cite it, they do not restate the suppression mechanics.

Mechanical checks (over the conventions file Batch-gate §):
- `rg -n 'distinct batch' .claude/skills/lead-companies/references/lead-pipeline-conventions.md` -> at least one hit (the distinct-batch rule is stated).
- `rg -n 'stale-count > 24' .claude/skills/lead-companies/references/lead-pipeline-conventions.md` -> at least one hit (`First 24` offered only above its cap).
- `rg -n 'stale-count <= 24' .claude/skills/lead-companies/references/lead-pipeline-conventions.md` -> at least one hit (`First 24` dropped at/below its cap).
- `rg -n 'stale-count <= 24' .claude/skills/lead-companies/SKILL.md .claude/skills/lead-contacts/SKILL.md` -> zero hits (rule not duplicated into a SKILL body, §V.100).

## §V.49 — bounded auto-retry parameters

4 attempts total; backoff [30, 120, 300]s; transient allow-list = Google 429/5xx, Anthropic 502/503/529, socket/TimeoutError; Drive socket timeout 60s feeds classifier; manual retry only failed/cancelled (completed + pending refused); retry UPDATE fires task_pending_trigger.

## §V.8 — view model projections

ContactView = base Contact superset + company_domain (LEFT JOIN company). CompanyView = base Company superset + `tags` (assigned tag names, empty ok; same shape as CompanySummary.tags / `db export` company.tags §V.121). CompanySummary lean list row carries `tags` + `disabled_reason` (null when enabled) + `contact_count`/`has_profile`; `--full` opts in `profile.summary` only (null when no profile) — never default full profile. MeetingView = base Meeting superset + attendee contacts (list_meeting_attendees join). All three views: inline <=10 latest notes (`_INLINE_NOTES_CAP`) + total count; field set test-tracked vs base model (Pydantic `extra=ignore` silently strips fields omitted from the view model — test catches drift). `meeting list` rows carry compact attendee summary (emails or count). `meeting view` inlines full attendee list. Workflow-agent prompt pre-feed (`Contact record:` / `Company record:` sections, §V.135) routes through load_contact_view/load_company_view — agent + CLI context byte-identical.

Trigger: `src/mailpilot/models.py` or `src/mailpilot/database.py` changed.
- `rg 'ContactView|CompanyView|MeetingView' src/mailpilot/models.py` -> all three present
- `rg 'tags.*list|tags: list' src/mailpilot/models.py` -> CompanySummary + CompanyView carry tags
- `rg '_INLINE_NOTES_CAP' src/mailpilot/database.py` -> cap constant present
- `rg 'load_contact_view|load_company_view|load_meeting_view' src/mailpilot/database.py` -> loaders present
- `rg 'test.*view.*field|ContactView.*Contact\b|CompanyView.*Company\b' src/mailpilot/tests/` -> field-set invariant test present
- `rg 'profile\.summary|--full' src/mailpilot/cli.py src/mailpilot/models.py` -> company list --full opt-in

## §V.45 — protocol composition + zero SPEC cites

Protocol composed `_BASE → [_SPEC_TABLE (inbound only) →] deferred branch → _MUST_SEND → _DECLINE → _NO_FABRICATION` = tool-loop shape; compose-only touch runs per §V.136; deferred branch selected per §V.31 (direction + trigger). `_SPEC_TABLE` = GFM pipe-table mandate for inbound product-spec; composed into inbound-general + inbound-google-drive `protocol_pre` only — outbound-general `protocol_pre` = `_BASE` alone. `_MUST_SEND` = end every trigger turn in a send or explicit noop; composed into `protocol_post` for all three templates. Every fragment is email-universal OR direction-scoped; never workflow-specific. Agent-facing text (composed protocol + registered tool docstrings) carries zero SPEC citation (`§V/§T/§B.<n>` tokens ban).

Trigger: `src/mailpilot/agent/templates.py` or `src/mailpilot/agent/tools.py` changed.

Mechanical checks:
- `rg -n '§[VTB]\.[0-9]+' src/mailpilot/agent/templates.py` -> classify each hit: code comment → exempt; inside a fragment string → fail.
- `rg -n '§[VTB]\.[0-9]+' src/mailpilot/agent/tools.py` -> classify each hit: comment / helper docstring → exempt; inside a registered tool docstring → fail.
- `rg -n 'may use Markdown' src/mailpilot/agent/templates.py` -> zero hits (permissive wording retired).
- `_SPEC_TABLE` in inbound-general + inbound-google-drive `protocol_pre` only; outbound-general `protocol_pre` == `_BASE` alone; the composed-protocol test asserts outbound carries no `pipe table` / `flow rates` / `product specifications`.

Registered tool docstring scope = functions named in `TEMPLATES[*].tools` (send_email, reply_email, create_task, cancel_task, conclude_enrollment, disable_contact, list_enrollments, search_emails, read_email, noop, list_drive_markdown, read_drive_markdown, search_drive_markdown). Internal helpers + module comments exempt.

## §V.54 — CLI mutation spans + constraint codes

Every CLI mutation (`create`, `update`, `disable`, `enable`, `add`, `remove`, `reply`, `send`, `start`, `stop`, `cancel`, `retry`) wraps its body in `logfire.span("<noun>.<verb>")` + emits `operator_event` with changed fields. psycopg constraint exception → error code mapping: UniqueViolation → `duplicate_key`, ForeignKeyViolation → `foreign_key_violation`, NotNullViolation → `not_null_violation`, CheckViolation → `check_violation`, other `psycopg.Error` → `database_error`, `ValidationError` → `validation_error`. Controlled `output_error` path (SystemExit) absorbed inside the `with logfire.span` block — span closes clean, SystemExit re-raised after. Only a genuine non-SystemExit Exception marks the span. Business-outcome envelopes (duplicate_key, not_found, validation_error, etc.) never surface as Logfire exceptions.

Trigger: `src/mailpilot/cli.py` or `src/mailpilot/operator_log.py` changed.
- `rg -n 'except.*UniqueViolation|duplicate_key' src/mailpilot/operator_log.py src/mailpilot/cli.py` -> mapping present
- `rg -n 'except.*SystemExit|re-raise' src/mailpilot/operator_log.py` -> SystemExit absorbed + re-raised inside span
- Telemetry test: `account.create` duplicate-key span carries no `exception.escaped=True` on the parent span.

## §V.75 — Gmail sync incremental + checkpoint integrity

Sync incremental via History API from `gmail_history_id` checkpoint. History 404 → full INBOX re-sync (history expired). First sync (`last_synced_at NULL`) → full INBOX listing regardless of `gmail_history_id` (hydrates pre-watch state). `get_messages_batch` callback: 404 per-sub-request → skip (deleted); 429 / 5xx per-sub-request → bounded backoff retry, NEVER dropped (sibling branch to 404-skip, not the same branch). `gmail_history_id` checkpoint advances only past persisted messages — exhausted-retry batch raises, `sync_account` never commits checkpoint past unstored mail. `_BATCH_SIZE` keeps concurrent `messages.get` sub-requests below Gmail per-user cap.

Trigger: `src/mailpilot/sync.py` or `src/mailpilot/gmail.py` changed.
- `rg -n 'last_synced_at.*None\b|last_synced_at.*NULL\b' src/mailpilot/sync.py` -> first-sync full-path gate
- `rg -n 'status_code.*404\b|404.*skip' src/mailpilot/sync.py src/mailpilot/gmail.py` -> 404-skip handler
- `rg -n '429.*retry\b|retry.*429\b' src/mailpilot/sync.py src/mailpilot/gmail.py` -> 429 retry (not silent drop)
- `rg -n 'gmail_history_id.*checkpoint\b|checkpoint.*advance' src/mailpilot/sync.py` -> checkpoint only on success

## §V.96 — lead-contacts discovery + negative-verdict memoization

Discover set = `company list --has-profile --max-contacts 4 --no-tag no-contacts-found --no-tag contacts-exhausted` (one call, expressible as a single query). CompanySummary `contact_count` = LEFT JOIN contact COUNT including disabled (tracks memoization rule, not active-only). `--max-contacts N` and `--min-contacts N` are inclusive. <=5 contacts/company/run. Admit-all — every discovered+verified email → contact row, low/NULL `email_confidence` flags risk in summary but never gates admission. Negative-verdict memoization branches on typed `reason_code` (NOT free-text prefix): `no_decision_makers` → tag `no-contacts-found`; `all_already_seeded` (contacts_created==0) → tag `contacts-exhausted`; `status=failed` NEVER tags (retryable). Both tags applied at run end. `company list --no-tag` is repeatable (Click `multiple=True`).

Trigger: `.claude/skills/lead-contacts/**` or `.claude/skills/lead-companies/**` changed.
- `rg 'no-tag no-contacts-found.*no-tag contacts-exhausted|no-contacts-found.*contacts-exhausted' .claude/skills/lead-contacts/SKILL.md` -> both exclusion tags in discover query
- `rg 'no_decision_makers|all_already_seeded' .claude/skills/lead-contacts/SKILL.md .claude/workflows/lead-contacts-find.js` -> typed reason_code present in both
- `rg 'multiple.*True\|no.tag.*multiple' src/mailpilot/cli.py` -> `--no-tag` is repeatable

## §V.103 — workflow definition files

Workflow defs = `workflows/*.toml`, 1 file/workflow, pure TOML (stdlib `tomllib`, no new dep). Fields = Workflow row 1:1: `{name, template, theme, goal, instructions, touches, touch_interval_days}`, `instructions` = TOML multi-line literal string; cadence pair `touches` + `touch_interval_days` int, nullable — NULL/omitted = single-touch, no auto follow-up (§V.136). `name` = canonical cross-environment key (§V.107): import enforces `name` kebab-shaped (lowercase, hyphen-separated, no dot/at-sign/UUID-shape) AND equal to the `*.toml` file stem (`{name}.toml`), globally unique — identical in dev and prod because both import the same file. `workflow import --file X.toml` → one row + shared validation (malformed/missing-required, or `name` not kebab|not file-stem → `validation_error`, no partial write). Def fields `{name, template, theme, goal, instructions, touches, touch_interval_days}` import-only: `workflow update` mutates non-def fields only (status, account binding); rename = rename file + re-import. File = sole source of truth, so no `row_ahead` drift state (§V.134). `--file <dir>` globs `*.toml` (batch, per-row errors continue). Terminal envelope aggregates: top-level int `applied` (rows w/o `error`) + `rejected` (rows w/ `error`) on every import envelope; `applied`=0 (all rows rejected | zero rows parsed) → `import_failed` error envelope on stderr, per-row rows inlined under `workflows`, exit 1 (§V.4 error path; report-inline mirrors `db check` §V.109); `applied`>=1 → ok:true exit 0, per-row errors stay inline; `record_count` = `workflows` array len (multi-key payload, §V.4). `workflow export --account-email A --out-dir D` writes one `*.toml`/workflow (name-sorted) + JSON status envelope on stdout. Export→dir→import round-trip idempotent. `workflows/` = gitignored symlink → independent repo kborovik/workflows @ /Users/kb/github/workflows (not a submodule, no submodule pointer). Root `workflows/*.toml` (CRM defs) distinct from `.claude/workflows/*.js` (Claude Code orchestration scripts).

Trigger: `src/mailpilot/cli.py` or `workflows/` changed.
- `rg 'tomllib' src/mailpilot/cli.py src/mailpilot/database.py` -> stdlib tomllib (no tomlkit/toml dep)
- `rg '"--file".*toml\|toml.*"--file"' src/mailpilot/cli.py` -> import/export --file flag present
- `rg 'json|JSON' src/mailpilot/cli.py | grep -i 'workflow import\|workflow export'` -> zero hits (TOML-only, no JSON import)
- `rg 'import_failed' src/mailpilot/cli.py` -> zero-applied loud-failure aggregate present

## §V.105 — mailpilot-reply-test grading model

In-scope cases graded deterministically: `score_replies.py` checks expected-token substring presence at runtime; false-PASS-at-worst, never false-FAIL. `expected_tokens` MUST be atomic: each token a single contiguous value the reply cannot restructure away — allowlist = {model id, bare number, number+short-unit (with optional short qualifier), label <=2 words}. NOT a `Label (Qualifier)` header (§B.102), a 3-plus-word phrase, a verb-bearing sentence fragment, or a layout-dependent phrase. Atomicity enforced test-time, NOT in the runtime grader: `_is_brittle_inscope_token` allowlist (not denylist) lives in `tests/test_reply_test_scoring.py`, and `test_inscope_expected_tokens_are_atomic` iterates the live QA-Pairs.json tokens (§B.117). `select_cases.py` selection guard (>=2 tokens, len>=5) keeps real signal after brittle tokens split. Out-scope + compare cases: `score_replies.py` emits advisory signals (token_hits, fabrication_candidates, has_table) but NOT verdicts. Sonnet judge sub-agent reads {reply body, case rubric, signals, source datasheet} → {verdict PASS|FAIL, rationale} (verdict of record for NL-shaped cases).

Trigger: `.claude/skills/mailpilot-reply-test/scripts/score_replies.py` or `tests/test_reply_test_scoring.py` changed.
- `rg '_is_brittle_inscope_token\|allowlist' tests/test_reply_test_scoring.py` -> allowlist logic present (not denylist); the atomicity guard is test-time, NOT in score_replies.py (§B.117)
- `rg 'advisory\|emit.*signal\|signal.*emit' .claude/skills/mailpilot-reply-test/scripts/score_replies.py` -> advisory signals, not verdicts, for out-scope/compare
- `rg 'judge.*Sonnet\|Sonnet.*judge\|verdict.*judge' .claude/skills/mailpilot-reply-test/SKILL.md` -> Sonnet judge sub-agent for NL-shaped verdict

## §V.107 — CLI entity reference + polymorphic resolver

Keyed entities (account=email, company=domain, contact=email, tag=name, workflow=name §V.103) addressed by natural key. Keyless entities (email, note, task, enrollment) addressed by UUID. Polymorphic resolver: value matching UUIDv7 shape (`8-4-4-4-12` hex) → resolve by id; any other value → resolve by natural key (domain has dots, email has at-sign, workflow `name` is kebab with neither nor UUID-shape — never collide), case-insensitive. Unknown key → `not_found`. Every single-entity verb target = positional `<key>` arg, NEVER `--<entity>-id` option. Scope/owner options named for owner natural key (`--company-domain`, `--contact-email`). Account-requiring cmds take a single `--account-email` (polymorphic, resolves email|UUID). `account sync --account-email` is optional (all accounts when omitted). `account sync --since <iso>` bounds full-INBOX backfill on first sync.

Trigger: `src/mailpilot/cli.py` changed.
- `rg '"--\w+-id"' src/mailpilot/cli.py` -> only `--workflow-id` (keyless) present; no `--company-id`, `--contact-id`, `--account-id` options
- `rg '"--account-email"' src/mailpilot/cli.py | wc -l` -> single polymorphic `--account-email` on account-requiring cmds
- `rg 'polymorphic\|UUIDv7.*shape\|8-4-4-4-12\|_is_uuid\|uuid.*shape' src/mailpilot/cli.py` -> UUID-shape resolver present

## §V.108 — migration registry + schema-hash re-stamp

`migrations/NNN_*.sql` forward-only (monotonic int prefix, no down-migrations, shipped in wheel). `db migrate` applies pending in order, each in own transaction, records `schema_migrations(version PK, name, applied_at, mailpilot_version)`. On success re-stamps `schema_metadata.schema_hash` + `mailpilot_version` to canonical `schema.sql` hash — re-baselines even at 0-pending when every migration is applied but recorded hash is stale (prevents phantom drift). `schema.sql` = canonical declarative full-schema. Identity invariant: fresh `db init` from `schema.sql` == apply-all-migrations-from-zero, byte-identical structure (test-enforced).

Trigger: `src/mailpilot/database.py` or `migrations/` changed.
- `rg 'schema_migrations\b' src/mailpilot/database.py` -> ledger table referenced
- `rg 're.stamp.*schema_hash\|schema_hash.*re.stamp\|re-stamp' src/mailpilot/database.py` -> re-stamp on migrate present
- `ls migrations/*.sql | sort` -> monotonic NNN_ prefix on all files
- `rg 'test.*identity\|db init.*migrate.*identical\|migrate.*init.*identical' src/mailpilot/tests/` -> byte-identity test present

## §V.115 — CLI list filter six-family taxonomy

Six families, each with fixed naming + semantics:
1. Scope: `--<owner-natural-key>` or `--<noun>-id <UUID>` for keyless parent; resolves polymorphic (§V.107); absent parent → `not_found`.
2. Enum: `--<axis>` `type=click.Choice` mirroring schema CHECK set; never free string.
3. Range: `--min-<field>`/`--max-<field>`, both inclusive + composable; NULL-inclusive where nullable + meaningful.
4. Presence: `--has-<field>/--no-<field>` single tri-state `default=None`; Click derives `has_<field>` param from positive side.
5. Text-match: field-named, exact only on `list`, case-fold per natural-key semantics; substring/fuzzy → `search` verb only.
6. Lifecycle: `--include-disabled` (is_flag False) + `--since`/`--until <ISO>` closed inclusive interval over one declared column.

Result-control set (not filters): `--limit <int>` (default 100 unless noun opts higher — company list|search default 500 per §V.148), `--offset <int>` (default 0), `--sort` (noun-declared Choice; absent → noun default order), `--desc` (is_flag; flips ASC→DESC). `record_count` = page length only (no total/has_more MVP). `--direction` = canonical inbound/outbound axis across email + workflow + template. Families realized as shared Click decorators (`limit_option`, `time_window_options(col)`, `include_disabled_option`, `scope_option`, `enum_option`, `range_options`, `presence_option`; plus `offset_option`/`sort_option` when §V.148 lands) composed fixed-order in `cli.py`/`_filters.py`. New list flag = new vocabulary decorator or spec change.

Trigger: `src/mailpilot/cli.py` or `src/mailpilot/_filters.py` changed.
- `rg 'limit_option|time_window_options|include_disabled_option|scope_option|enum_option|range_options|presence_option' src/mailpilot/` -> all 7 decorator names present
- `rg '"--direction"' src/mailpilot/cli.py` -> present on email|workflow|template list (no `--type`)
- `rg '"--route-method".*Choice\|click\.Choice.*route.method' src/mailpilot/cli.py` -> route-method is a Choice not free string
- `rg '"--limit"' src/mailpilot/cli.py | wc -l` -> present on every list cmd

## §V.116 — tags controlled vocabulary

Two tables: `tag` (vocabulary, one row/defined tag, `name` globally unique §V.90, soft-delete via `disabled_reason`) + `tag_assignment` (link, one row/(tag, owner), owner XOR company|contact). CLI verbs: `tag create <name>`, `tag view`, `tag disable <name>`, `tag enable <name>`, `tag add`, `tag remove`, `tag list`, `tag search`. `tag add` errors `not_found` on undefined tag, NEVER auto-creates. `tag list` = vocabulary + projected `usage_count`. `company list --tag <name>` / `contact list --tag <name>` = membership filter. `company list --no-tag <name>` = negated membership filter, repeatable (each one negated-membership predicate, all intersected). `--no-tag` resolves through vocabulary (undefined → `not_found`). `company list` + `company view` project assigned tags as `tags[]` names (empty ok; list/view shape identical; same as `db export` company.tags) — membership filter alone ! substitute for projection (agent triage needs tags on the list row).

Trigger: `src/mailpilot/cli.py` or `src/mailpilot/database.py` changed.
- `rg '"tag"\b.*"create"\|"tag create"' src/mailpilot/cli.py` -> all verbs registered
- `rg 'not_found.*tag\b\|tag.*not_found' src/mailpilot/cli.py src/mailpilot/database.py` -> `not_found` on undefined (no auto-create)
- `rg '"--no-tag".*multiple.*True\|multiple.*True.*"--no-tag"' src/mailpilot/cli.py` -> `--no-tag` is repeatable
- `rg 'tags' src/mailpilot/models.py | rg 'CompanySummary|CompanyView'` -> company list/view project tags

## §V.120 — send-obligation guard

Every send-obligated trigger turn MUST leave a `reply_email`|`send_email` ToolReturnPart without an `error` key, OR a successful `noop` ({acknowledged: true}), OR a `conclude_enrollment` terminal (§V.127). Send-obligated (walker scope) = inbound (`email is not None`, trigger in {email, task}); outbound first reach-out (trigger in {enrollment_run, enrollment_schedule}, `email is None`) runs compose-only — harness sends the validated TouchMessage itself, obligation structural, not walker-checked (§V.136). `manual` trigger exempt. Guard `_sent_reply(result)` walks `result.all_messages()` after the §V.81 tool-count check; none of the above → raise `AgentCompletedWithoutReplyError`. Class is non-transient → `_handle_agent_failure` takes it terminal `failed` + `operator_event("error")`, NEVER silent completed. Prompt-side preventive = `_MUST_SEND` template fragment (§V.45).

Trigger: `src/mailpilot/agent/invoke.py` changed.
- `rg '_sent_reply\b' src/mailpilot/agent/invoke.py` -> guard present
- `rg 'AgentCompletedWithoutReplyError' src/mailpilot/exceptions.py` -> exception defined
- `rg 'enrollment_run\|enrollment_schedule' src/mailpilot/agent/invoke.py` -> outbound first-reach-out triggers dispatch compose-only shape (§V.136), walker scope inbound-only
- `rg '"manual"\b.*exempt\|trigger.*manual.*exempt\|manual.*skip' src/mailpilot/agent/invoke.py` -> manual exempt
- `rg 'conclude_enrollment.*_sent_reply\|_sent_reply.*conclude_enrollment' src/mailpilot/agent/invoke.py` -> conclude_enrollment in walker

## §V.121 — db snapshot bundle

`db export --file <path>` writes one JSON bundle + `{"db":{path, companies:N, contacts:M, tags:K}, "ok":true}` status to stdout (singular envelope, not plural). `db import --file <path>` restores fixed code order: tags → companies → contacts. Bundle format: `{schema_version:int, exported_at:ts, tags:[{name, disabled_reason}], companies:[{...profile, disabled_reason, tags:[name,...]}], contacts:[{...title, email_confidence, disabled_reason, company_domain, tags:[name,...]}]}`. Scope = tag vocabulary + company + contact ONLY (emails, workflows, enrollments, tasks, accounts excluded). Every link resolves by natural key — company domain, contact email, tag name; source-DB UUID NEVER forwarded. Per-row errors continue batch (FK-unresolvable → per-row error entry, NOT batch abort). `db export` = read-only + drift-tolerant. `db import` dead-stops on drift|pending. Export→fresh-import round-trip is field-identical (test-enforced).

Trigger: `src/mailpilot/database.py` or `src/mailpilot/cli.py` db-export/import section changed.
- `rg 'schema_version\|exported_at' src/mailpilot/database.py` -> bundle fields present
- `rg 'company_domain.*contact\|by.*natural.*key\|natural.*key.*restore' src/mailpilot/database.py` -> natural-key restore (not UUID-based)
- `rg 'export.*import.*round.trip\|round.trip.*field.identical' src/mailpilot/tests/` -> round-trip test present
- `rg 'company_id.*export\|export.*company_id' src/mailpilot/database.py` -> zero hits (source UUID not forwarded)

## §V.123 — reply-cancels-followups

Inbound reply routing to an enrollment bulk-cancels that enrollment's pending future follow-up tasks: `UPDATE task SET status='cancelled' WHERE enrollment_id=%(id)s AND status='pending' AND scheduled_at > now() AND COALESCE(context->>'trigger','') <> 'enrollment_schedule'`. First-touch exclusion: rows whose trigger = `enrollment_schedule` (§V.32) are excluded. `cancel_enrollment_followup_tasks(connection, enrollment_id)` fires from 4 sites: (1) `routing.route_email` on successful inbound match (including pre-existing enrollment, not only first-insert branch), (2) calendar booking ingestion (§V.126/§V.128), (3) `conclude_enrollment` (§V.127), (4) cadence sequence exhaustion — `advance_touch_cadence` final-touch conclude (§V.136).

Trigger: `src/mailpilot/routing.py`, `src/mailpilot/sync.py`, `src/mailpilot/agent/tools.py`, `src/mailpilot/cadence.py`, or `src/mailpilot/database.py` changed.
- `rg 'cancel_enrollment_followup_tasks' src/mailpilot/routing.py src/mailpilot/sync.py src/mailpilot/agent/tools.py src/mailpilot/cadence.py` -> present in 4 files
- `rg 'enrollment_schedule.*exclude\b\|exclude.*enrollment_schedule\b' src/mailpilot/database.py` -> first-touch exclusion in the query
- `rg 'scheduled_at.*>.*now\(\)\|now\(\).*<.*scheduled_at' src/mailpilot/database.py` -> only future tasks cancelled

## §V.124 — workflow.goal field

`workflow.goal` = free-text observable outcome that concludes the enrollment (e.g. "prospect books a Google Meet"). Renamed from `workflow.objective` via migration 006. One field, two readers: (1) conclude_enrollment disposition gate — agent calls `conclude_enrollment` when it judges goal met; system concludes deterministically on calendar booking regardless of stated goal; (2) classify.py semantic-match key for inbound workflow routing (§V.76). `_DEFERRED_TASK_TASK` fragment (§V.45) names "the workflow goal" (not "objective"). `record_enrollment_outcome` is system-internal (§V.15) — NOT exposed to the agent.

Definition text matches the composed-protocol mechanism: `goal` + reply-branch `instructions` claim only outcomes/actions the trigger branch can reach. Inbound (`trigger=email`) goal claims no terminal record — §V.31 composes initial-send-only + forbids `conclude_enrollment`, so 'record the outcome completed' never fires. A reply branch names exactly one terminal action, never a two-option close (`create_task` OR `conclude_enrollment`) — agent takes both (§B.120); `contact_later` already schedules re-enrollment (§V.127), so a same-turn `create_task` double-queues.

Trigger: `src/mailpilot/models.py`, `src/mailpilot/agent/classify.py`, or `src/mailpilot/agent/invoke.py` changed.
- `rg '\bgoal\b' src/mailpilot/models.py` -> `goal` present; `rg '\bobjective\b' src/mailpilot/models.py` -> zero hits
- `rg '"Goal:"' src/mailpilot/agent/invoke.py` -> `Goal:` label in agent prompt
- `rg '\bgoal\b' src/mailpilot/agent/classify.py` -> classify.py reads goal column
- `rg 'record_enrollment_outcome' src/mailpilot/agent/tools.py` -> zero hits (system-internal, not in tool set)

## §V.126 — CalendarClient + poll sites

`CalendarClient` in `calendar.py` mirrors GmailClient/DriveClient shape: service account + DWD, `with_subject(email)`, scope `calendar.events.readonly`. Shared per-account helper `_poll_account_calendar(connection, account)` fires from two sites: (1) run-interval full-sweep tick via `_poll_all_calendars` (§V.21 fallback), (2) `account_sync` (cli.py) per-account after `sync_account`. Each site upserts one `meeting` row/event idempotently on `google_event_id` (re-poll = no dup row) + links email-matched attendees + concludes each booking exactly once. Per-account calendar errors isolated: logged via `operator_event`, NEVER raised — one account's calendar fault stalls neither loop nor Gmail sync. Read-only — NO event create|update from the app.

Trigger: `src/mailpilot/calendar.py` or `src/mailpilot/sync.py` changed.
- `rg 'CalendarClient\b' src/mailpilot/calendar.py` -> class present
- `rg '_poll_account_calendar\b' src/mailpilot/sync.py` -> helper present
- `rg '_poll_all_calendars.*_poll_account_calendar\|_poll_account_calendar.*_poll_all_calendars' src/mailpilot/sync.py` -> called from both sites
- `rg 'calendar.events.readonly' src/mailpilot/calendar.py` -> correct scope
- `rg 'create_event\|update_event\|insert_event' src/mailpilot/calendar.py` -> zero hits (read-only)

## §V.127 — conclude_enrollment agent terminal

`conclude_enrollment(disposition, note, reschedule_at)` = sole agent-facing terminal tool. Disposition in {meeting_booked, do_not_contact, contact_later}. System side-effects per disposition: `meeting_booked` → `record_enrollment_outcome` + `cancel_enrollment_followup_tasks` + booking note; `do_not_contact` → conclude + cancel + `disable_contact`; `contact_later` → conclude + cancel + scheduled re-enrollment task at `reschedule_at` (agent-supplied, default >=3 months out). Counts as valid send-obligation terminal (§V.120) — `_sent_reply` walker accepts it like noop. `record_enrollment_outcome` is NOT in the agent tool set — it is system-internal (§V.15, §V.124). System-internal conclusion sites call `record_enrollment_outcome` directly: calendar booking (§V.128) and cadence sequence exhaustion — after the final cadence touch the harness records `contact_later` ("sequence exhausted"), no re-enrollment task, no agent turn (§V.136).

Trigger: `src/mailpilot/agent/tools.py` or `src/mailpilot/agent/invoke.py` changed.
- `rg 'conclude_enrollment\b' src/mailpilot/agent/tools.py` -> tool present
- `rg 'meeting_booked\|do_not_contact\|contact_later' src/mailpilot/agent/tools.py` -> all 3 dispositions
- `rg 'record_enrollment_outcome' src/mailpilot/agent/tools.py` -> zero hits (system-internal, not agent tool)
- `rg 'conclude_enrollment.*_sent_reply\|_sent_reply.*conclude_enrollment' src/mailpilot/agent/invoke.py` -> conclude_enrollment in send-obligation walker

## §V.129 — agent-supplied timestamp grounding + future guard

Two-pronged: PREVENT via grounding, GUARD at boundary. PREVENT: `@agent.instructions` fn in `_build_agent` (invoke.py) injects current date per run-start (PydanticAI idiom, `date.today()` evaluated each run, cache-safe — date rolls slower than cache TTL). GUARD: `create_task` (tools.py) rejects `scheduled_at` not strictly after `now()` → `{error: 'past_scheduled_at', message}`, persists no row. `conclude_enrollment` contact_later (tools.py) rejects past `reschedule_at` same way. A rejected `conclude_enrollment` carries `error` key → `_sent_reply` skips it (§V.120 unsatisfied → agent must retry or noop). Guard at agent boundary NOT in `database.create_task` — system-computed paths (enrollment_schedule first-touch §V.32, default-omitted reschedule_at §V.127) are exempt.

Trigger: `src/mailpilot/agent/tools.py` or `src/mailpilot/agent/invoke.py` changed.
- `rg '@agent\.instructions\b' src/mailpilot/agent/invoke.py` -> dynamic instructions present
- `rg 'date\.today\(\)\|current.*date\b\|today.*date\b' src/mailpilot/agent/invoke.py` -> date injected
- `rg 'past_scheduled_at\b' src/mailpilot/agent/tools.py` -> guard error code present
- `rg 'past_scheduled_at\b' src/mailpilot/database.py` -> zero hits (guard at boundary, not DB layer)

## §V.47 — provider-aware model config: dispatch + caching + model settings

`llm_provider` in {`anthropic`, `xai`} (default `xai`) selects factory branch for **both** classifier + workflow agent via `_build_model(settings, *, role)`. Active-provider API key required at model build; missing key fails closed (no fallthrough). Inactive-provider keys may be empty. Dep: `pydantic-ai-slim[anthropic,xai]`.

**Anthropic branch** (`llm_provider=anthropic`): Caching (both call sites — classifier + workflow agent): `anthropic_cache_instructions=True` + `anthropic_cache_tool_definitions=True`. Telemetry attribute names: `agent.invoke` rollup span carries bare `cache_read_input_tokens` + `cache_creation_input_tokens` (from `usage.cache_read_tokens`/`cache_write_tokens`); per-call `chat` span carries OTel `gen_ai.usage.cache_read.input_tokens` + `gen_ai.usage.details.cache_creation_input_tokens` — verify caching against these exact names. `gen_ai.usage.cache_read_input_tokens` exists on neither span (null = false caching-off diagnosis, §B.113). Model settings (workflow agent only — classifier excluded): `_build_anthropic_model` reads `anthropic_thinking`, `anthropic_effort`, `anthropic_max_tokens` into `AnthropicModelSettings`. `anthropic_max_tokens` ALWAYS passed as `max_tokens=<int>` (not empty-gated). `anthropic_thinking` and `anthropic_effort` added ONLY when non-empty. Defaults: `anthropic_model=claude-sonnet-5`, `anthropic_thinking=adaptive`, `anthropic_effort=high`, `anthropic_max_tokens=32768`. `xhigh` effort requires Opus 4.7+.

**xAI branch** (`llm_provider=xai`, default): `XaiProvider(api_key=..., api_host=optional, timeout=240)` + `XaiModel(xai_model, provider=...)`. No Anthropic cache flags (omit — no false cache telemetry). Model settings (workflow agent only — classifier excluded): `_build_xai_model` reads `xai_reasoning_effort`, `xai_max_tokens` into `XaiModelSettings` / shared `ModelSettings`. `xai_max_tokens` ALWAYS passed. Defaults: `xai_model=grok-4.5`, `xai_reasoning_effort=medium`, `xai_max_tokens=32768`. Env key: `MAILPILOT_XAI_API_KEY` / config `xai_api_key` only (bare `XAI_API_KEY` not a mailpilot source).

**Effort enums** (settings load / `config set`, not first agent turn): `anthropic_effort` in {unset, `low`, `medium`, `high`, `xhigh`, `max`}; `xai_reasoning_effort` in {`low`, `medium`, `high`} (no `none` — Grok 4.5 always reasons). Invalid value rejected at settings layer.

Trigger: `src/mailpilot/agent/invoke.py`, `src/mailpilot/agent/classify.py`, or `src/mailpilot/settings.py` changed.
- `rg 'llm_provider|_build_model\b' src/mailpilot/agent/` -> provider dispatch present
- Anthropic path (when selected): `rg 'anthropic_cache_instructions.*True\|anthropic_cache_tool_definitions.*True' src/mailpilot/agent/` -> caching flags on both Anthropic call sites
- Anthropic path: `rg 'max_tokens.*anthropic_max_tokens\|anthropic_max_tokens.*max_tokens' src/mailpilot/agent/` -> `max_tokens` always set (not in an `if` guard)
- xAI path: `rg 'XaiModel|XaiProvider|xai_reasoning_effort|xai_max_tokens' src/mailpilot/agent/` -> xAI factory + settings wiring
- `rg 'max_tokens\b' src/mailpilot/agent/classify.py` -> zero hits (classifier excluded from max_tokens)
- settings: `rg 'AnthropicEffort|XaiReasoningEffort|llm_provider' src/mailpilot/settings.py` -> closed enums + provider field

## §V.131 — fallback acknowledgement on terminal inbound failure

`_handle_agent_failure` (run.py) terminal branch sends one fixed `_FALLBACK_ACKNOWLEDGEMENT` reply before `complete_task(status='failed')` when: `task.email_id` is set (inbound task) AND the reply-emitted contextvar flag (set by a successful `reply_email`/`send_email` in tools.py) is unset. Outbound first-touch (`email_id` NULL) stays silent. `_FALLBACK_ACKNOWLEDGEMENT` = code-defined fixed string in templates.py, content-free, never model-generated. Idempotency keyed on in-memory contextvar flag (NOT DB read — `connection.rollback()` at head of `_handle_agent_failure` erases mid-turn email row). Fallback-send failure: logs `operator_event("error", source='run.task.fallback_failed')` + falls through to `complete_task(status='failed')` — original terminal never masked.

Trigger: `src/mailpilot/run.py` or `src/mailpilot/agent/templates.py` changed.
- `rg '_FALLBACK_ACKNOWLEDGEMENT\b' src/mailpilot/agent/templates.py` -> constant present
- `rg 'reply_emitted_scope\|reply_emitted\b' src/mailpilot/agent/tools.py src/mailpilot/run.py` -> contextvar flag present in both
- `rg 'email_id.*fallback\|fallback.*email_id\|task\.email_id\b' src/mailpilot/run.py` -> inbound gate in `_handle_agent_failure`
- `rg 'fallback_failed\b' src/mailpilot/run.py` -> best-effort send failure error event

## §V.5 — parent denorm on list/view rows

`workflow` rows carry `account_email`; `enrollment` rows carry `workflow_name` + `contact_email` + `contact_name`; `contact` list/search rows carry `company_domain` (LEFT JOIN company ON company_id, NULL when company_id NULL). The `company_domain` join backs the `--company-domain` Scope filter (resolve-then-scope per §V.107/§V.115 family 1) — unknown domain → `not_found`, not silent `[]`. Every FK projection stays feed-able by natural key.

Trigger: `src/mailpilot/database.py` changed.
- `rg 'account_email.*workflow\|workflow.*account_email' src/mailpilot/database.py` -> workflow rows carry account_email
- `rg 'LEFT JOIN company\b' src/mailpilot/database.py` -> contact rows LEFT JOIN company for company_domain
- `rg 'workflow_name.*enrollment\|enrollment.*workflow_name' src/mailpilot/database.py` -> enrollment rows carry workflow_name denorm

## §V.7 — EmailSummary projection

`EmailSummary` MUST include `gmail_thread_id`, `is_routed`, `route_method`, and `recipients` (To/Cc/Bcc address map mirroring the Email base field). Operator audits routing from CLI without Logfire. A single bulk `email list` exposes each message's recipients without a per-row `email view`. `list_emails` SELECT projects `recipients` so the map populates (not default-empty). §V.122 keys campaign-test delivery on the recipients projection.

Trigger: `src/mailpilot/database.py` or `src/mailpilot/models.py` changed.
- `rg 'gmail_thread_id\b' src/mailpilot/models.py | grep EmailSummary` -> gmail_thread_id in EmailSummary
- `rg 'route_method\b' src/mailpilot/models.py | grep EmailSummary` -> route_method in EmailSummary
- `rg 'recipients\b' src/mailpilot/models.py | grep EmailSummary` -> recipients in EmailSummary
- `rg 'recipients\b' src/mailpilot/database.py | grep list_emails` -> recipients projected in list_emails SELECT

## §V.10 — tag soft-disable

`tag.disabled_reason TEXT NULL`; non-NULL = disabled, carries reason. `tag disable <name>` sets it (disabled_reason IS NULL gate blocks double-disable). `tag enable <name>` clears it (disabled_reason IS NOT NULL gate blocks enabling an active tag). Vocabulary-tag disable/enable write no activity — a `tag` row has no contact/company owner (§V.17). `tag list` hides disabled unless `--include-disabled`. `tag disable` retires a vocabulary-table (`tag`) entry (§V.116), NOT a per-owner link — `tag remove` is the distinct unlinking verb.

Trigger: `src/mailpilot/cli.py` or `src/mailpilot/database.py` changed.
- `rg 'disabled_reason.*tag\|tag.*disabled_reason' src/mailpilot/schema.sql` -> disabled_reason col on tag table
- `rg '"tag".*"enable"\|enable_tag\b' src/mailpilot/cli.py src/mailpilot/database.py` -> tag enable verb present
- `rg 'IS NULL.*disabled_reason\|disabled_reason.*IS NULL' src/mailpilot/database.py | grep tag` -> double-disable gate on tag

## §V.11 — status payload envelope

`mailpilot status` envelope = `{version, schema, sync_loop, accounts, tasks, config, counts}`. Schema block carries three-state `verdict` in {current, pending, drift} + `recorded_hash`/`current_hash` + applied/pending migration counts (not a bare drift bool, §V.109). Tasks block carries `pending`, `failed_24h`, `scheduled_future`, `oldest_pending_age_seconds`, `max_attempt_count_pending`.

Trigger: `src/mailpilot/cli.py` changed.
- `rg 'sync_loop\b' src/mailpilot/cli.py | grep status` -> sync_loop key in status envelope
- `rg 'failed_24h\|oldest_pending_age_seconds\|max_attempt_count_pending' src/mailpilot/cli.py src/mailpilot/database.py` -> task block fields present
- `rg 'recorded_hash\|current_hash' src/mailpilot/cli.py | grep status` -> hash fields in schema block

## §V.15 — enrollment lifecycle + outcome model

`enrollment.status` in {active, disabled} (no `paused`). `disabled` = operator halt, requires non-empty `disabled_reason` (CHECK) + `enrollment_disabled` activity, reversible via `enrollment enable <id>` (status disabled→active, clears `disabled_reason`, `status <> 'disabled'` gate blocks enabling a live enrollment, emits `enrollment_enabled` activity). `enrollment disable`/`enable` are the sole halt/resume surface (NO `enrollment update` status verb). Outcomes live on activity timeline via `record_enrollment_outcome` (accepts only completed|failed), enrollment row untouched.

Trigger: `src/mailpilot/database.py` or `src/mailpilot/models.py` changed.
- `rg 'status.*CHECK\b' src/mailpilot/schema.sql | grep enrollment` -> status CHECK in schema
- `rg 'enrollment_disabled\b' src/mailpilot/database.py` -> enrollment_disabled activity on disable
- `rg 'enrollment_enabled\b' src/mailpilot/database.py` -> enrollment_enabled activity on enable
- `rg 'record_enrollment_outcome\b' src/mailpilot/database.py` -> outcome fn present (activity-only)
- `rg "status.*<>.*'disabled'\|!=.*'disabled'" src/mailpilot/database.py` -> enabling guard

## §V.77 — outbound email row persistence + orphan recovery

Outbound email row persists only AFTER Gmail accepts send — Gmail failure produces no orphan row. Post-send `create_email ON CONFLICT (gmail_message_id) DO NOTHING` → None signals the row already exists; recover via `get_email_by_gmail_message_id` + return (idempotent send, never raise). Genuinely unrecoverable (no gmail_id, or conflicting row vanished after conflict): log `orphan_gmail_send` + raise.

Trigger: `src/mailpilot/sync.py` or `src/mailpilot/gmail.py` changed.
- `rg 'ON CONFLICT.*gmail_message_id\|gmail_message_id.*ON CONFLICT' src/mailpilot/database.py` -> conflict handling present
- `rg 'get_email_by_gmail_message_id\b' src/mailpilot/sync.py src/mailpilot/gmail.py` -> recovery fn called
- `rg 'orphan_gmail_send\b' src/mailpilot/sync.py src/mailpilot/gmail.py` -> orphan error event logged

## §V.78 — outbound MIME headers + thread_id threading

Outbound MIME stamped `X-MailPilot-Version` always + `X-MailPilot-Account-Id` when account-bound. Replies set `In-Reply-To` + `References` (`References` defaults to `in_reply_to`). Send path threads via optional `thread_id` — `send_email` agent tool + `_wrap_send_email` + `email_ops.send_email` forward `thread_id` to `sync.send_email`. Supplied without explicit `in_reply_to` → `_resolve_threading_headers` derives `In-Reply-To`/`References` from prior local thread rows. Multi-touch outbound threads natively: capture `gmail_thread_id` on touch 1, pass as `thread_id` on later touches; no `contact_id` requirement (unlike `reply_email` §V.79).

Trigger: `src/mailpilot/sync.py` or `src/mailpilot/gmail.py` changed.
- `rg 'X-MailPilot-Version\b' src/mailpilot/sync.py src/mailpilot/gmail.py` -> version header stamped
- `rg '_resolve_threading_headers\b' src/mailpilot/sync.py src/mailpilot/gmail.py` -> threading header resolver present
- `rg 'thread_id\b' src/mailpilot/agent/tools.py | grep send_email` -> thread_id param on send_email tool

## §V.79 — send/reply guards + account soft-disable lifecycle

Send/reply guards: disabled contact OR disabled account blocks send + reply; cold-send cooldown 30 days per (account, contact, workflow); reply requires original `gmail_thread_id` + `contact_id` (typed errors); reply subject gets "Re: " prefix unless already prefixed, case-insensitive.

Account soft-disable: `account.disabled_reason TEXT NULL` (non-NULL = disabled, carries reason). `account disable <ref> --reason <text>` sets it (disabled_reason IS NULL gate blocks double-disable). `account enable <ref>` clears it (disabled_reason IS NOT NULL gate blocks enabling an active account). A disabled account is gated everywhere it would touch Gmail — sync loop skips it, `account sync` all-accounts mode skips it, `renew_watches()` skips it, send + reply refuse it. `account list` default-hides disabled; `--include-disabled` opts in. Operator-only — the agent never disables or enables an account.

Trigger: `src/mailpilot/sync.py`, `src/mailpilot/database.py`, or `src/mailpilot/agent/invoke.py` changed.
- `rg 'disabled_reason\b' src/mailpilot/schema.sql | grep account` -> account soft-disable col
- `rg 'cold.send.*cooldown\|cooldown.*30\|30.*days' src/mailpilot/sync.py src/mailpilot/database.py` -> cooldown gate
- `rg '"account".*"disable"\b\|"account".*"enable"\b' src/mailpilot/cli.py` -> both verbs present
- `rg 'disabled_reason.*skip\|account.*disabled.*sync\|sync.*skip.*disabled' src/mailpilot/sync.py` -> sync loop skip gate
- `rg 'include.disabled\b' src/mailpilot/cli.py | grep account` -> account list --include-disabled

## §V.80 — bounce/unsubscribe handling + contact disable

Bounce detection: sender local-part in {mailer-daemon, postmaster} (case-insensitive) OR label contains "BOUNCE" → most recent outbound in same thread + account marked `bounced` + contact disabled with "bounced:" reason prefix. Unsubscribe path uses "unsubscribed:" prefix. `contact enable <ref>` clears `disabled_reason` regardless of prefix (operator owns consent, no unsubscribe carve-out). `disabled_reason IS NOT NULL` gate blocks re-enabling an active contact. Operator-only — the agent disables on bounce/unsubscribe, never re-enables.

Trigger: `src/mailpilot/routing.py` or `src/mailpilot/sync.py` changed.
- `rg 'mailer-daemon\|postmaster\|BOUNCE\b' src/mailpilot/routing.py src/mailpilot/sync.py` -> bounce detection strings
- `rg '"bounced:"\|"unsubscribed:"' src/mailpilot/database.py src/mailpilot/sync.py src/mailpilot/routing.py` -> reason prefixes
- `rg 'enable_contact\b' src/mailpilot/agent/tools.py` -> zero hits (re-enable is operator-only, not agent tool)

## §V.90 — natural-key UNIQUE constraints

UNIQUE: `account.email`, `company.domain`, `contact.email`, `workflow.name` (globally unique, kebab = `*.toml` file stem §V.103), `enrollment(workflow_id, contact_id)`, `email.gmail_message_id` (nullable-unique). `tag.name` globally unique (vocabulary row §V.116). `tag_assignment` UNIQUE per (tag_id, owner). These natural keys = canonical CLI identifiers — case-insensitive handles resolved polymorphic (§V.107); unknown key → `not_found` (§V.94).

`contact.email` natural key canonicalized lowercase at every write + lookup — `create_contact`, `get_contact_by_email`, `create_or_get_contact_by_email`, `create_contacts_bulk`, `get_contacts_by_emails` lowercase the `email` arg before the `contact.email` match|insert; sync sender→contact resolve feeds the same normalized key. Mirrors `email.sender` lowercase persist + CLI polymorphic case-insensitive resolution (§V.107). `contact.email` `TEXT UNIQUE` is case-sensitive, so write-path lowercase (NOT the constraint) is the case-variant dedup guard; case-variant `From` (Outlook/Exchange recase local-part) never mints a duplicate bare contact (closes §B.121).

Trigger: `src/mailpilot/schema.sql` or `src/mailpilot/database.py` changed.
- `rg 'UNIQUE.*email\b' src/mailpilot/schema.sql` -> account + contact email UNIQUE
- `rg 'UNIQUE.*domain\b' src/mailpilot/schema.sql` -> company domain UNIQUE
- `rg 'UNIQUE.*gmail_message_id' src/mailpilot/schema.sql` -> email nullable-unique
- `rg 'UNIQUE.*tag_id.*owner\|UNIQUE.*owner.*tag_id' src/mailpilot/schema.sql` -> tag_assignment UNIQUE per pair
- `rg -n 'email\.lower\(\)|lower\(email' src/mailpilot/database.py` -> contact natural-key fns lowercase before match|insert

## §V.95 — contact lead-metadata flat columns

`contact.title TEXT NULL` (role label); `contact.email_confidence INT NULL`, schema CHECK `email_confidence BETWEEN 0 AND 100`. NULL = Bouncer unknown (unbilled, unverified) = high risk. `email_confidence` = sole email-risk score. `contact list --max-email-confidence N` surfaces `email_confidence <= N OR IS NULL` — SQL inequality alone (NULL excluded) is the trap; admit-all (§V.96) never drops unknowns. No `ContactProfile` model.

Trigger: `src/mailpilot/models.py` or `src/mailpilot/database.py` changed.
- `rg 'email_confidence\b.*INT\|title\b.*TEXT' src/mailpilot/schema.sql` -> flat cols (not JSONB)
- `rg 'email_confidence.*BETWEEN.*0.*100\|CHECK.*email_confidence' src/mailpilot/schema.sql` -> schema CHECK
- `rg 'max.email.confidence\b' src/mailpilot/database.py` -> filter option present
- `rg 'IS NULL.*email_confidence\|email_confidence.*IS NULL' src/mailpilot/database.py` -> NULL-inclusive in filter

## §V.104 — reply-test reply-loop guard

Live reply-test (`.claude/skills/mailpilot-reply-test`) requires `outbound@lab5.ca` have no active workflow. `inbound-google-drive` agent reply lands in outbound mailbox → `skipped_no_workflows` (§V.76), no second reply. Any active outbound workflow re-enters routing → inbound↔outbound auto-reply loop. No-outbound-workflow is a load-bearing test precondition, not incidental.

Trigger: `.claude/skills/mailpilot-reply-test/**` changed.
- `rg 'outbound@lab5.ca\b' .claude/skills/mailpilot-reply-test/SKILL.md | grep -i 'no.*workflow\|precondition'` -> guard stated in skill body
- `rg 'skipped_no_workflows\b' .claude/skills/mailpilot-reply-test/SKILL.md` -> expected routing outcome named

## §V.106 — Drive search whitespace-tokenized OR-joined predicates

`search_drive_markdown` query = whitespace-tokenized; each token generates `fullText contains '<token>'`; all tokens OR-joined; raw hyphenated token retained (hyphenated model tried whole + split); results union+deduped by file_id; ~8-token cap. Single salient term surfaces the file. NEVER a single whole-phrase `fullText contains '{query}'` predicate — Drive punctuation-tokenizes + AND-joins internally → false-negatives on hyphenated/multi-word queries.

Trigger: `src/mailpilot/drive.py` changed.
- `rg 'fullText contains\b' src/mailpilot/drive.py` -> OR-joined token predicates
- `rg '\.split()\b' src/mailpilot/drive.py | grep -i search` -> whitespace tokenization
- `rg 'file_id.*set\b\|dedupe\b' src/mailpilot/drive.py | grep search` -> union+dedupe by file_id

## §V.109 — three-state schema verdict + tiered gate

Verdict in {current, pending, drift}. `_read_schema_metadata` breakout: metadata-row-missing vs table-missing → None collapse avoided — ledger-behind = `pending`, hash-mismatch or manual-edit = `drift`. Read-only diagnosis (`status`, `db check`) tolerates + reports. `run` + every CLI mutation dead-stops: drift → `schema_drift` envelope + exit 1; pending → `schema_migration_pending` envelope + exit 1. Two distinct codes since remedy differs (drift = investigate divergence, pending = run `db migrate`). Fail at startup, not mid-batch.

Trigger: `src/mailpilot/database.py` changed.
- `rg 'schema_drift\b' src/mailpilot/database.py src/mailpilot/cli.py` -> drift code present
- `rg 'schema_migration_pending\b' src/mailpilot/database.py src/mailpilot/cli.py` -> pending code present (distinct from drift)
- `rg 'determine_schema_verdict\b\|_read_schema_metadata\b' src/mailpilot/database.py` -> verdict fn present
- `rg '"current"\b\|"pending"\b\|"drift"\b' src/mailpilot/database.py | grep verdict` -> three-state values

## §V.110 — initialize_database off the hot path

`initialize_database()` = connect + verify, NOT provision. Empty-DB auto-provision fires only when `account` table is absent (data-loss-free; keeps `make clean` + test fixtures ergonomic). Populated DB never mutates structure as a connection side-effect. Explicit forward paths: `db init` (provision empty, refuses if `account` exists, no `--force`) + `db migrate` (advance populated).

Trigger: `src/mailpilot/database.py` changed.
- `rg 'initialize_database\b' src/mailpilot/database.py` -> fn present
- `rg 'information_schema.*account\b\|table.*account.*exist' src/mailpilot/database.py` -> empty-DB gate on `account` table absence
- `rg '_provision_schema\b' src/mailpilot/database.py` -> provision fn separate from initialize

## §V.111 — CLI --help zero SPEC citations

Every Click command/group `--help` (docstring-derived help + option `help=` strings) renders free of `§V/§T/§B.<n>`. Guard walks the command tree, renders each `--help`, greps for `§[VTB].[0-9]+` → zero hits. Operator-facing twin of §V.45 (agent-prompt text).

Trigger: `src/mailpilot/cli.py` changed.
- `rg '§[VTB]\.[0-9]+' src/mailpilot/cli.py | grep -v '^\s*#'` -> classify each hit: in a Click `help=` string or docstring → fail; in a `#` comment → exempt
- Full guard: render `mailpilot --help` recursively (each sub-command), grep rendered output for `§[VTB]` pattern → zero hits

## §V.112 — lead-companies scoped enrich-scope

Domain/URL/UUID args enrich ONLY rows resolved or seeded this run, never the global profile-NULL backlog. `seed_companies.py` emits `seeded_stale` (rows created/matched this run via `touched_apexes` accumulator) distinct from global `stale`. Fast path feeds `seeded_stale` for domain/URL-token runs, `stale` for file/bare runs. Global stale set is never the dispatch fan-out for a scoped arg.

Trigger: `.claude/skills/lead-companies/**` changed.
- `rg 'seeded_stale\b' .claude/skills/lead-companies/scripts/seed_companies.py` -> seeded_stale present (not global stale)
- `rg 'touched_apexes\b' .claude/skills/lead-companies/scripts/seed_companies.py` -> accumulator present
- `rg 'backlog\b\|global.*stale\|stale.*global' .claude/skills/lead-companies/SKILL.md` -> explicit not-backlog statement

## §V.113 — Bouncer single GET per contact

Bouncer email verify = real-time single GET `/v1.1/email/verify?email=` per contact (at most 5 per company per run; per-email billing). NEVER POST `/email/verify/batch/sync`. Empty body, 4xx/5xx, or missing status = verify FAILURE (retry once, then NULL with noted reason) — never a clean Bouncer `status="unknown"`.

Trigger: `.claude/skills/lead-contacts/**` changed.
- `rg 'batch/sync\|batch.sync' .claude/skills/lead-contacts/` -> zero hits (POST batch absent)
- `rg '/v1.1/email/verify\b' .claude/skills/lead-contacts/` -> single-GET present
- `rg 'retry.*once\|once.*retry' .claude/skills/lead-contacts/SKILL.md .claude/skills/lead-contacts/scripts/` -> single retry on failure

## §V.114 — company soft-disable

`company.disabled_reason TEXT NULL` (non-NULL = disabled, carries reason). `company disable <id> --reason <text>` sets it (disabled_reason IS NULL gate blocks double-disable, mirrors §V.10). `company list` hides disabled unless `--include-disabled`. `company enable <ref>` clears `disabled_reason` (disabled_reason IS NOT NULL gate blocks re-enabling an active company). Part of uniform disable/enable verb pairing across company|contact|tag|enrollment (§V.10/§V.15/§V.80). Operator-only — lead-contacts negative-verdict memoization moved to the `no-contacts-found` tag (§V.96, §V.116).

Trigger: `src/mailpilot/cli.py` or `src/mailpilot/database.py` changed.
- `rg 'disabled_reason\b' src/mailpilot/schema.sql | grep company` -> disabled_reason col on company table
- `rg '"company".*"disable"\b\|"company".*"enable"\b' src/mailpilot/cli.py` -> both verbs present
- `rg 'include.disabled\b' src/mailpilot/cli.py | grep company` -> --include-disabled on company list
- `rg 'IS NULL.*disabled_reason\|disabled_reason.*IS NULL' src/mailpilot/database.py | grep company` -> double-disable gate

## §V.119 — destructive DB op backup-first

Every destructive DB op (`make clean`, any `dropdb`) runs `mailpilot db export --file ~/Documents/MailPilot/snap-<ts>.json` first (single JSON snapshot = tag vocabulary + company + contact, §V.121). CLI writes the file + exits 0 before any `dropdb`. Failed export aborts the drop (fail-closed, no `|| true` swallow). Restore = `mailpilot db import --file <snap>`. Company + contact = paid live data (discovery credits §V.96/§V.113), never wiped without a durable backup. Backup dir `~/Documents/MailPilot/` is iCloud-synced (portable across macOS instances), outside the dropped DB.

Trigger: `Makefile` changed.
- `rg 'db-backup\b' makefile` -> db-backup target present
- `rg 'db.*export.*snap\|snap.*db.*export' makefile` -> export-to-snap in db-backup target
- `rg 'clean.*db-backup\|db-backup.*clean\|clean.*:.*db-backup' makefile` -> make clean depends on db-backup

## §V.122 — campaign-test Touch 1 delivery keyed on rfc2822_message_id per scenario

`send_touch1.py` captures `outbound_email_id` + `rfc2822_message_id` per scenario enrollment immediately after each Touch 1 send. One shared prospect contact (`inbound@lab5.ca`) receives all sends; isolation is by ephemeral workflow (one per scenario), not by recipient alias — no `inbound{N}@lab5.ca` aliases exist. `inject_replies.py` matches received Touch 1 emails by `rfc2822_message_id` (primary key); subject match is a fallback only. A scenario whose send status is not `sent` or whose `rfc2822_message_id` is missing fails before reply injection. Subject = agent-generated + collision-prone, NEVER the primary identity key.

Trigger: `.claude/skills/mailpilot-campaign-test/**` changed.
- `rg 'rfc2822_message_id' .claude/skills/mailpilot-campaign-test/scripts/send_touch1.py` -> message-id captured at send
- `rg 'rfc2822_message_id' .claude/skills/mailpilot-campaign-test/scripts/inject_replies.py` -> message-id used to match received Touch 1
- `rg 'inbound[1-9]@lab5\.ca' .claude/skills/mailpilot-campaign-test/scripts/` -> zero hits (no per-scenario aliases)

## §V.125 — meeting + meeting_attendee schema

`meeting` table cols: `{id, google_event_id, meet_url, summary, scheduled_at, ends_at, status, created_at, updated_at}`. `google_event_id` nullable-unique (idempotent ingest, mirrors `email.gmail_message_id` §V.90). `status` CHECK in {scheduled, completed, cancelled, no_show}. `meeting_attendee(meeting_id, contact_id)` link table UNIQUE per pair (mirrors `tag_assignment` §V.116). One meeting links at least 1 attendee. Attendees matched to contacts by email; unmatched email = no link. `status` col = operator record-keeping only, gates NOTHING — booking conclusion (§V.128) fires at booking regardless of later completed|no_show.

Trigger: `src/mailpilot/schema.sql` or `src/mailpilot/database.py` changed.
- `rg 'google_event_id\b' src/mailpilot/schema.sql` -> nullable-unique col present
- `rg 'meeting_attendee\b' src/mailpilot/schema.sql` -> link table present
- `rg 'scheduled.*completed.*cancelled.*no_show\|status.*CHECK\b' src/mailpilot/schema.sql | grep meeting` -> status enum
- `rg 'UNIQUE.*meeting_id.*contact_id\|UNIQUE.*contact_id.*meeting_id' src/mailpilot/schema.sql` -> link table UNIQUE per pair

## §V.128 — calendar booking concludes enrollments, no agent turn

For each attendee contact (§V.125) holding an active outbound enrollment: system concludes via `record_enrollment_outcome` (§V.15) + cancels pending future follow-ups via `cancel_enrollment_followup_tasks` + writes a system booking note. Fan-out fires for EVERY active outbound enrollment the attendee holds — a booked meeting outranks any cold sequence regardless of stated goal (§V.124). `cancel_enrollment_followup_tasks` fires from four sites: inbound reply routing (§V.123), calendar booking ingestion (§V.126), `conclude_enrollment` (§V.127), cadence sequence exhaustion (§V.136); first-touch exclusion (§V.32) holds at every site.

Trigger: `src/mailpilot/calendar.py` or `src/mailpilot/sync.py` changed.
- `rg 'record_enrollment_outcome\b' src/mailpilot/calendar.py src/mailpilot/sync.py` -> conclusion call present
- `rg 'cancel_enrollment_followup_tasks\b' src/mailpilot/calendar.py src/mailpilot/sync.py` -> follow-up cancel call present
- `rg 'booking.*note\|system.*note\b' src/mailpilot/calendar.py src/mailpilot/sync.py` -> system booking note written
- `rg 'active.*outbound\b.*enrollment\|outbound.*active\b.*enrollment' src/mailpilot/calendar.py src/mailpilot/sync.py` -> only active outbound enrollments concluded

## §V.132 — workflow stats funnel

`workflow stats <workflow>` = read-only per-campaign funnel, 1 workflow by entity ref (§V.107), single deterministic SQL aggregate (no LLM). Envelope `{"workflow_stats": {...}, "ok": true}` (aggregate not a workflow entity row — singular-key exception cf `db export` §V.121). 8 stages at enrollment grain (contact-distinct, multi-touch never double-counts):
- `enrolled` = workflow's enrollment rows
- `sent` = enrollments with at least 1 outbound `status='sent'` email
- `bounced` = enrollments with at least 1 outbound `status='bounced'` email
- `replied` = enrollments with at least 1 inbound routed email (route sets contact_id + workflow_id §V.27)
- `meeting_booked` = latest-outcome `enrollment_completed` (disposition-independent)
- `contact_later` / `do_not_contact` = latest-outcome `enrollment_failed` split by `detail->>'disposition'`
- `active` = `status='active'` enrollment with no terminal outcome

Disposition persistence: `record_enrollment_outcome` writes `detail.disposition` in {meeting_booked, do_not_contact, contact_later} from `conclude_enrollment.disposition` (§V.127) + booking-conclusion `meeting_booked` (§V.128). JSONB key, no migration. Pre-change failed rows lack disposition (legacy gap; forward campaigns are exact).

Trigger: `src/mailpilot/database.py` or `src/mailpilot/cli.py` changed.
- `rg 'workflow_stats\b' src/mailpilot/database.py` -> aggregate fn present
- `rg 'meeting_booked\|contact_later\|do_not_contact' src/mailpilot/database.py | grep stats` -> disposition stages
- `rg '"workflow_stats"' src/mailpilot/cli.py` -> envelope key correct
- `rg 'DISTINCT.*contact_id\b' src/mailpilot/database.py | grep stats` -> enrollment grain aggregate

## §V.22 — is_routed gate: single route pass per email

At most 1 `routing.route_email` span lifecycle per `email_id`. Gate: every routing outcome (§V.20) sets `is_routed=TRUE`; a subsequent History-API re-delivery or repeat sync sweep skips routing entirely on an already-routed message. Duplicate route spans inflate metrics and mask classifier regressions.

Trigger: `src/mailpilot/routing.py` or `src/mailpilot/sync.py` changed.
- `rg 'is_routed\b' src/mailpilot/routing.py src/mailpilot/sync.py` -> is_routed gate present
- `rg 'is_routed.*True\b\|True.*is_routed' src/mailpilot/database.py` -> is_routed set on every outcome
- `rg 'if.*is_routed\b' src/mailpilot/routing.py src/mailpilot/sync.py` -> gate check before route call

## §V.38 — Drive tools sequential=True

All Drive-KB agent tools (`list_drive_markdown`, `read_drive_markdown`, `search_drive_markdown`) registered with `sequential=True` in the Pydantic-AI tool set; serializes parallel dispatch. Rationale: shared `httplib2.Http` transport is thread-unsafe; max concurrent transport = 1. Transport faults (`HttpError`, `TimeoutError`, `OSError`) → structured `drive_unavailable` error dict, NEVER bare raise to agent (§V.39).

Trigger: `src/mailpilot/agent/tools.py` changed.
- `rg 'sequential.*True\|True.*sequential' src/mailpilot/agent/tools.py | grep drive` -> sequential=True on Drive tools
- `rg 'drive_unavailable\b' src/mailpilot/agent/tools.py` -> error dict key present
- `rg 'HttpError\b\|TimeoutError\b\|OSError\b' src/mailpilot/agent/tools.py | grep drive` -> all three fault classes caught

## §V.51 — logfire.exception + operator_event("error") pairing

Every `logfire.exception(...)` call in the call-graph reachable from `mailpilot run` MUST appear in the same `except` block as `operator_event("error", source=..., message=...)`. A contract test sweeps all run-reachable modules for `logfire.exception` sites and asserts the paired `operator_event("error")` is present in the same except block — failure = terminal error produces no operator stderr line.

Trigger: any `src/mailpilot/**/*.py` changed.
- `rg -n 'logfire\.exception\b' src/mailpilot/` -> enumerate exception sites reachable from `mailpilot run`
- For each hit file: verify same except block has `operator_event("error"` within 5 lines
- `rg 'test.*logfire.*exception\|logfire.*exception.*test' src/mailpilot/tests/` -> contract test present

## §V.62 — release flow: make target + PyPI pipeline

Release = `make release major|minor|patch`. Gates: part arg present, clean working tree. Steps: `uv version --bump <part>` (bumps `pyproject.toml` + `uv.lock`), commit `chore: release v<x.y.z>`, tag `v<x.y.z>`, push main + tags, `gh release create v<x.y.z> --generate-notes`. Publish = `.github/workflows/release.yml` on release published: ci.yml via workflow_call gates publish; tag must equal `v$(uv version --short)`; `uv build`; `pypa/gh-action-pypi-publish` w/ OIDC trusted publishing (`id-token: write`, no PyPI API token). Dist name = `mailpilot-crm` (PyPI name `mailpilot` foreign-owned); module + CLI cmd = `mailpilot` via `[tool.uv.build-backend] module-name`. Deploy = PyPI package — `uv tool install mailpilot-crm`.

Trigger: `makefile`, `.github/workflows/release.yml`, or `pyproject.toml` changed.
- `rg 'uv version --bump' makefile` -> bump step present
- `rg 'gh release create' makefile` -> GitHub release step present
- `rg 'git diff --quiet' makefile` -> clean-tree gate present
- `rg 'pypa/gh-action-pypi-publish' .github/workflows/release.yml` -> trusted-publishing action present
- `rg 'id-token: write' .github/workflows/release.yml` -> OIDC permission present
- `rg 'uv version --short' .github/workflows/release.yml` -> tag==version gate present
- `rg 'workflow_call' .github/workflows/ci.yml` -> CI reusable as publish gate
- `rg 'name = "mailpilot-crm"' pyproject.toml` -> dist name present
- `rg 'module-name = "mailpilot"' pyproject.toml` -> module override present

## §V.133 — task stats aggregate

`task stats` = read-only aggregate, single SQL query, task grain, no LLM. Envelope `{"task_stats": {...}, "ok": true}` (aggregate, not a task entity row, cf §V.132). Filter options: `--workflow-id` (polymorphic §V.107); `--trigger` Enum filter on `COALESCE(context->>'trigger', '')` against §V.26 taxonomy — NEVER reads `description`. Shared `--trigger` decorator with `task list`. Returns: per-status counts `{pending, completed, failed, cancelled}` + `total` + `distinct_scheduled_days` (day-bucketed count) + `first_scheduled_at` + `last_scheduled_at`. `--bucket-tz <IANA>` (default UTC) buckets `distinct_scheduled_days` only; per-status counts are timezone-independent. `--trigger enrollment_schedule` selects first-touch tasks (§V.32).

Trigger: `src/mailpilot/database.py` or `src/mailpilot/cli.py` changed.
- `rg 'task_stats\b' src/mailpilot/database.py` -> aggregate fn present
- `rg 'distinct_scheduled_days\b' src/mailpilot/database.py` -> day-bucket field present
- `rg '"task_stats"' src/mailpilot/cli.py` -> envelope key correct
- `rg "COALESCE.*trigger\b\|context.*trigger" src/mailpilot/database.py | grep task` -> trigger from context JSONB not description
- `rg '"--trigger".*Choice\b' src/mailpilot/cli.py | grep task` -> trigger is a Choice (closed enum)

## §V.134 — workflow check: def-integrity states

`workflow check` = read-only live 2-way SHA-256 over def fields `{template, theme, goal, instructions, touches, touch_interval_days}`. Join key = workflow `name` (§V.90 global-unique, NOT a hashed field). Each `workflows/*.toml` read for its `name` field (NOT file stem, §V.103); row set read from DB; joined by name. States: `in_sync` (name both sides + hash equal); `out_of_sync` (name both sides + hash differs → re-import due); `not_imported` (name in catalog def, no DB row); `orphaned` (name in DB row, no catalog def). `--file` repeatable: every passed source read + merged, last-def-wins on dup `name`. Scope: specific-file source(s) → `scope_to_catalog=True`, report iterates catalog names only so an unpassed DB row is dropped (never `orphaned`) — presents only the inquired workflows; any directory source → `scope_to_catalog=False`, report iterates catalog∪rows so an unaccounted row surfaces as `orphaned` drift (full-catalog check). Empty `--file` → `validation_error`. No `conflict` state — duplicate `name` across files is import-forbidden (§V.103 name==unique-stem), hand-edit-only. No `row_ahead` state — def fields import-only (§V.103) so any mismatch = catalog ahead only. Report-only envelope `{"workflow_check": {...}, "ok": true}` (aggregate, not a workflow row, cf §V.132); NOT a deploy gate. Import-time `name==stem` enforcement (§V.103) is separate — `workflow check` reads the TOML `name` field, not the file stem.

Trigger: `src/mailpilot/cli.py` changed.
- `rg '"in_sync"\|"out_of_sync"\|"not_imported"\|"orphaned"' src/mailpilot/cli.py src/mailpilot/database.py` -> all 4 states present
- `rg 'scope_to_catalog' src/mailpilot/cli.py src/mailpilot/database.py` -> specific-file check suppresses `orphaned`, dir check keeps it
- `rg 'multiple=True' src/mailpilot/cli.py | grep -i check` -> `workflow check --file` repeatable
- `rg 'workflow_check\b' src/mailpilot/cli.py` -> envelope key present
- `rg 'sha256\b\|hashlib.*sha256' src/mailpilot/cli.py src/mailpilot/database.py | grep workflow` -> SHA-256 hash present
- `rg 'toml.*\["name"\]\|tomllib.*name\b\|name.*toml' src/mailpilot/cli.py src/mailpilot/database.py | grep workflow_check` -> reads `name` field from TOML (not file stem)

## §V.4 — CLI envelope + record_count

Every cmd output MUST match the §I.cli envelope. ok:true envelope carries top-level int `record_count` = records displayed: array-bearing payload (`list`/`search`/`sync`/`export`/`import`) -> array len; single-object payload (single-entity verbs + aggregate `stats`/`check` + `status`) -> 1. Error path -> `{"error", "message", "ok": false}` + exit 1; `record_count` omitted on error. Envelope key vocabulary per §I.cli (plural for arrays, singular for single-object; `workflow_stats`/`task_stats`/`workflow_check`/`db` aggregate exceptions).

Trigger: `src/mailpilot/cli.py` changed.
- `rg 'record_count' src/mailpilot/cli.py` -> output helper stamps record_count on every ok:true envelope
- `rg 'output_error' src/mailpilot/cli.py | head -3` -> error helper present ({"error","message","ok":false} + exit 1)

## §V.14 — activity append-only + note lifecycle

Activity = INSERT only — no update/delete fns for activity rows. Note = INSERT + single-note hard-delete `note remove <note_id>` (§I): one note row per call, no bulk-clear, no note update; operator-only, NOT an agent tool. Tag/note mutation + its activity row commit in one txn — both or neither. `note remove` deletes the note row only, writes no activity — prior `note_added` rows survive as the append-only trail.

Trigger: `src/mailpilot/database.py` or `src/mailpilot/cli.py` changed.
- `rg 'def update_activity\|def delete_activity' src/mailpilot/database.py` -> zero hits (activity append-only)
- `rg 'def delete_note\b' src/mailpilot/database.py` -> single-note hard-delete fn present
- `rg 'def delete_notes\b' src/mailpilot/database.py` -> zero hits (owner bulk-clear retired)
- `rg 'note_added' src/mailpilot/database.py` -> note INSERT pairs its activity row in one txn

## §V.18 — schema drift definition

Schema drift = live DB structure diverged from `schema.sql` w/ no migration path (manual edit | DB ahead of code); primitive = hash mismatch per §V.19. Distinct from `pending` = unapplied `migrations/NNN_*.sql` (§V.108). Response tiered per §V.109: `status` + `db check` tolerate + report; `run` + mutations dead-stop.

Trigger: `src/mailpilot/database.py` changed.
- `rg '"drift"' src/mailpilot/database.py` -> drift verdict present, distinct from pending
- `rg '"pending"' src/mailpilot/database.py` -> pending verdict present
- `rg 'schema_hash\|recorded_hash' src/mailpilot/database.py` -> hash-mismatch primitive

## §V.23 — task drain pool + per-worker trace isolation

Task drain = bounded pool <= `max_concurrent_tasks`; each worker owns its psycopg.Connection; atomic claim blocks re-dispatch of in-flight tasks. Each worker roots its own trace — the drain worker detaches the dispatching tick's `sync.loop.iteration` OTel context before `run.execute_task` (py3.14 ThreadPoolExecutor.submit propagates the active span via contextvars), so trace_id maps 1:1 w/ agent.invoke.

Trigger: `src/mailpilot/sync.py` changed.
- `rg 'max_concurrent_tasks' src/mailpilot/sync.py` -> pool bound present
- `rg 'otel_context' src/mailpilot/sync.py` -> worker attaches fresh context + detaches token
- `rg 'ThreadPoolExecutor' src/mailpilot/sync.py` -> bounded executor drain

## §V.25 — advisory locks 2-tier

Advisory locks 2-tier: coarse (workflow_id, contact_id) + task-scoped (task_id split-half CRC32 pair). Lock acquired BEFORE the agent.invoke span opens — loser -> None, no span emitted. Contention -> reschedule w/o attempt_count bump; the scheduled_at push fires task_pending_trigger so the loop re-wakes.

Trigger: `src/mailpilot/agent/invoke.py` or `src/mailpilot/database.py` changed.
- `rg 'crc32' src/mailpilot/agent/invoke.py` -> CRC32 lock-key derivation present
- `rg 'advisory' src/mailpilot/agent/invoke.py src/mailpilot/database.py` -> both lock tiers present
- `rg 'task_pending_trigger' src/mailpilot/schema.sql` -> reschedule push re-wakes the loop

## §V.27 — routing pipeline order + classifier bounds

Routing pipeline order: thread match -> RFC message-id match -> LLM classify; every stage account-scoped. Classifier = single-turn, no tools; body truncated @ 16384 chars; hallucinated workflow_id coerced to None; zero active inbound workflows -> no LLM call. Every outcome marks is_routed=TRUE w/ a distinct route_method (§V.20 enum).

Trigger: `src/mailpilot/routing.py` or `src/mailpilot/agent/classify.py` changed.
- `rg 'rfc_message_id' src/mailpilot/routing.py` -> RFC message-id stage after thread match
- `rg '16384' src/mailpilot/agent/classify.py` -> body truncation bound
- `rg 'is_routed' src/mailpilot/routing.py` -> every outcome marks routed

## §V.31 — deferred branch direction-aware

Protocol deferred branch keyed on direction + trigger. Outbound: trigger='task' -> terminal-outcome instruction (`_DEFERRED_TASK_TASK`, names conclude_enrollment); outbound first reach-out = compose-only touch run, binds NO deferred fragment (§V.136). Inbound: every trigger -> inbound-reply instruction (`_DEFERRED_TASK_INBOUND`: reply once + stop, system records outcome, never conclude_enrollment / create_task); inbound templates bind neither conclude_enrollment nor create_task.

Trigger: `src/mailpilot/agent/templates.py` changed.
- `rg '_DEFERRED_TASK_INBOUND\|_DEFERRED_TASK_TASK' src/mailpilot/agent/templates.py` -> both direction fragments present
- `rg 'build_protocol' src/mailpilot/agent/templates.py` -> direction-aware composition fn
- `rg '_INBOUND_EXCLUDED_TOOLS' src/mailpilot/agent/templates.py` -> inbound rosters exclude conclude_enrollment + create_task

## §V.41 — KB grounding rules live in workflow instructions

KB grounding rules (search-first, 2-search budget then single list, read top >= 3 hits, per-target search budget on compare) live in the workflow definition's `instructions` field (§V.103), NOT a code-defined template protocol fragment. inbound-google-drive template binds the Drive tool set but carries no grounding fragment — grounding wording is per-workflow data, not code.

Trigger: `src/mailpilot/agent/templates.py` or `workflows/*.toml` changed.
- `rg -i 'search-first\|search first\|2-search' src/mailpilot/agent/templates.py` -> zero hits (no grounding fragment in code)
- `rg 'list_drive_markdown' src/mailpilot/agent/templates.py` -> Drive tool set bound on inbound-google-drive

## §V.44 — template registry owns agent shape

TEMPLATES keys == WorkflowTemplateName members (registry total). WorkflowTemplate frozen (`@dataclass(frozen=True)`). Every template carries non-empty protocol + tools + description. workflow.template + type immutable post-create — update raises ValueError on either; type derived from template (never stored independently).

Trigger: `src/mailpilot/agent/templates.py` or `src/mailpilot/database.py` changed.
- `rg 'WorkflowTemplateName' src/mailpilot/agent/templates.py` -> registry keyed on the enum
- `rg 'frozen=True' src/mailpilot/agent/templates.py` -> WorkflowTemplate frozen
- `rg 'immutable' src/mailpilot/database.py | rg -i 'template\|type'` -> post-create immutability guard in update_workflow

## §V.83 — execute_task pre-flight cancellation

execute_task pre-flight cancels the task (zero LLM calls) when: workflow inactive/missing; contact disabled/missing; enrollment missing or status != active. Touch tasks (context.touch) additionally cancelled when the latest enrollment outcome is terminal OR an inbound email from the contact arrived after the prior touch — belt complementing reply-time cancellation (§V.123).

Trigger: `src/mailpilot/run.py` changed.
- `rg 'status="cancelled"' src/mailpilot/run.py` -> pre-flight cancel sites present
- `rg '_touch_cancel_reason' src/mailpilot/run.py` -> touch-specific guard fn present

## §V.135 — mechanical context pre-feed

invoke_workflow_agent loads ContactView (+ CompanyView when contact.company_id set) via load_contact_view/load_company_view — the same shared loaders the CLI uses (§V.8), so agent + operator context stay byte-identical. _build_user_prompt renders `Contact record:` / `Company record:` JSON sections. read_contact/read_company absent from EVERY template roster (inbound included); _BASE names no read tools (§V.40 fragment-naming floor).

Trigger: `src/mailpilot/agent/invoke.py` or `src/mailpilot/agent/templates.py` changed.
- `rg 'load_contact_view\|load_company_view' src/mailpilot/agent/invoke.py` -> shared loaders pre-feed
- `rg 'Contact record:' src/mailpilot/agent/invoke.py` -> prompt section rendered
- `rg 'read_contact\|read_company' src/mailpilot/agent/templates.py` -> zero roster hits

## §V.136 — system-owned touch cadence

Workflow def fields `touches` + `touch_interval_days` (nullable pair, §V.103; NULL = single-touch, no auto follow-up). Cadence engine (`cadence.py`) owns schedule math (weekend -> Monday roll) + touch scheduled_at — system-computed only, §V.129 exempt path. Successful touch-N send -> harness creates touch-N+1 task w/ context {touch: N+1, prior_email_id}. Final touch -> system-internal conclude contact_later "sequence exhausted" (§V.127 record path, §V.128 shape, no agent turn). Touch runs (context.touch present | trigger enrollment_schedule | enrollment_run) = compose-only agent: output_type TouchMessage {subject: str|None, body: str}, zero tools; §V.42 lint runs as output validator (bounded ModelRetry); harness sends via email_ops + schedules the next touch — 1 LLM call per touch, send structural (§V.120). Outbound task|email triggers keep the tool loop; inbound unchanged (§V.44 registry owns both shapes). prior_email_id from task context; absent -> enrollment's latest outbound email. NULL-cadence belt: touch >= 2 vs NULL cadence -> reschedule +1h + operator warn (§V.25 shape); touch 1 vs NULL -> send + schedule nothing. create_task stays bound for reply-branch soft follow-ups. Cadence + after-touch prose live in def fields, never TOML instructions.

Trigger: `src/mailpilot/cadence.py`, `src/mailpilot/agent/invoke.py`, or `src/mailpilot/email_ops.py` changed.
- `rg 'touch_interval_days' src/mailpilot/cadence.py` -> cadence engine owns the pair
- `rg 'TouchMessage' src/mailpilot/agent/invoke.py src/mailpilot/models.py` -> compose-only output type
- `rg 'sequence exhausted' src/mailpilot/cadence.py` -> final-touch system conclusion
- `rg 'prior_email_id' src/mailpilot/` -> touch context threading

## Recipe grep-runner — mechanization candidate (not implemented)

Observed 2026-07-02 (T212 build probe + §V.123/§V.128 amends): recipe `rg` lines
hand-run repeatedly to validate check-extras bodies against code — same shape,
different file lists. Candidate mode: script parses every backticked `rg` line
under each `## §V.<n>` header, executes it, emits per-section {line, hit_count,
files}; prose expectations (`-> present in N files`, `-> zero hits`) stay
operator-judged — the runner collapses the execute step, not the verdict.
Overlaps /sdd:check's interactive recipe runs; implement only if hand-running
recurs. Promotion path: seed a §T row.
