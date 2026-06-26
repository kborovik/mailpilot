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

Scope of "registered tool docstrings" = the source functions in `tools.py` named by `TEMPLATES[*].tools` (`send_email`, `reply_email`, `create_task`, `cancel_task`, `record_enrollment_outcome`, `disable_contact`, `list_enrollments`, `search_emails`, `read_contact`, `read_company`, `read_email`, `noop`, `list_drive_markdown`, `read_drive_markdown`, `search_drive_markdown`). Internal helpers (`_check_spec_table`) + module comments are NOT registered, so their §-cites are exempt — flag a hit only when it sits inside a registered tool's `"""docstring"""`.

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

ContactView = base Contact superset + company_domain (LEFT JOIN company). CompanyView = base Company superset. MeetingView = base Meeting superset + attendee contacts (list_meeting_attendees join). All three: inline <=10 latest notes (`_INLINE_NOTES_CAP`) + total count; field set test-tracked vs base model (Pydantic `extra=ignore` silently strips fields omitted from the view model — test catches drift). `meeting list` rows carry compact attendee summary (emails or count). `meeting view` inlines full attendee list. Agent read_contact/read_company route through load_contact_view/load_company_view — agent + CLI context byte-identical.

Trigger: `src/mailpilot/models.py` or `src/mailpilot/database.py` changed.
- `rg 'ContactView|CompanyView|MeetingView' src/mailpilot/models.py` -> all three present
- `rg '_INLINE_NOTES_CAP' src/mailpilot/database.py` -> cap constant present
- `rg 'load_contact_view|load_company_view|load_meeting_view' src/mailpilot/database.py` -> loaders present
- `rg 'test.*view.*field|ContactView.*Contact\b|CompanyView.*Company\b' src/mailpilot/tests/` -> field-set invariant test present

## §V.45 — protocol composition + zero SPEC cites

Protocol composed `_BASE → [_SPEC_TABLE (inbound only) →] trigger branch → _MUST_SEND → _DECLINE → _NO_FABRICATION`. `_SPEC_TABLE` = GFM pipe-table mandate for inbound product-spec; composed into inbound-general + inbound-google-drive `protocol_pre` only — outbound-general `protocol_pre` = `_BASE` alone. `_MUST_SEND` = end every trigger turn in a send or explicit noop; composed into `protocol_post` for all three templates. Every fragment is email-universal OR direction-scoped; never workflow-specific. Agent-facing text (composed protocol + registered tool docstrings) carries zero SPEC citation (`§V/§T/§B.<n>` tokens ban).

Trigger: `src/mailpilot/agent/templates.py` or `src/mailpilot/agent/tools.py` changed.

Mechanical checks:
- `rg -n '§[VTB]\.[0-9]+' src/mailpilot/agent/templates.py` -> classify each hit: code comment → exempt; inside a fragment string → fail.
- `rg -n '§[VTB]\.[0-9]+' src/mailpilot/agent/tools.py` -> classify each hit: comment / helper docstring → exempt; inside a registered tool docstring → fail.
- `rg -n 'may use Markdown' src/mailpilot/agent/templates.py` -> zero hits (permissive wording retired).
- `_SPEC_TABLE` in inbound-general + inbound-google-drive `protocol_pre` only; outbound-general `protocol_pre` == `_BASE` alone; the composed-protocol test asserts outbound carries no `pipe table` / `flow rates` / `product specifications`.

Registered tool docstring scope = functions named in `TEMPLATES[*].tools` (send_email, reply_email, create_task, cancel_task, conclude_enrollment, disable_contact, list_enrollments, search_emails, read_contact, read_company, read_email, noop, list_drive_markdown, read_drive_markdown, search_drive_markdown). Internal helpers + module comments exempt.

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

Workflow defs = `workflows/*.toml`, 1 file/workflow, pure TOML (stdlib `tomllib`, no new dep). Fields = Workflow row 1:1: `{name, template, theme, goal, instructions}`, `instructions` = TOML multi-line literal string. `workflow import --file X.toml` → one row + shared validation (malformed/missing-required → `validation_error`, no partial write). `--file <dir>` globs `*.toml` (batch, per-row errors continue). `workflow export --account-email A --out-dir D` writes one `*.toml`/workflow (name-sorted) + JSON status envelope on stdout. Export→dir→import round-trip idempotent. `workflows/` = gitignored symlink → independent repo kborovik/workflows @ /Users/kb/github/workflows (not a submodule, no submodule pointer). Root `workflows/*.toml` (CRM defs) distinct from `.claude/workflows/*.js` (Claude Code orchestration scripts).

Trigger: `src/mailpilot/cli.py` or `workflows/` changed.
- `rg 'tomllib' src/mailpilot/cli.py src/mailpilot/database.py` -> stdlib tomllib (no tomlkit/toml dep)
- `rg '"--file".*toml\|toml.*"--file"' src/mailpilot/cli.py` -> import/export --file flag present
- `rg 'json|JSON' src/mailpilot/cli.py | grep -i 'workflow import\|workflow export'` -> zero hits (TOML-only, no JSON import)

## §V.105 — mailpilot-reply-test grading model

In-scope cases graded deterministically: `score_replies.py` checks expected-token substring presence at runtime; false-PASS-at-worst, never false-FAIL. `expected_tokens` MUST be atomic: each token a single contiguous value the reply cannot restructure away — allowlist = {model id, bare number, number+short-unit (with optional short qualifier), label <=2 words}. NOT a `Label (Qualifier)` header (§B.102), a 3-plus-word phrase, a verb-bearing sentence fragment, or a layout-dependent phrase. Atomicity enforced test-time, NOT in the runtime grader: `_is_brittle_inscope_token` allowlist (not denylist) lives in `tests/test_reply_test_scoring.py`, and `test_inscope_expected_tokens_are_atomic` iterates the live QA-Pairs.json tokens (§B.117). `select_cases.py` selection guard (>=2 tokens, len>=5) keeps real signal after brittle tokens split. Out-scope + compare cases: `score_replies.py` emits advisory signals (token_hits, fabrication_candidates, has_table) but NOT verdicts. Sonnet judge sub-agent reads {reply body, case rubric, signals, source datasheet} → {verdict PASS|FAIL, rationale} (verdict of record for NL-shaped cases).

Trigger: `.claude/skills/mailpilot-reply-test/scripts/score_replies.py` or `tests/test_reply_test_scoring.py` changed.
- `rg '_is_brittle_inscope_token\|allowlist' tests/test_reply_test_scoring.py` -> allowlist logic present (not denylist); the atomicity guard is test-time, NOT in score_replies.py (§B.117)
- `rg 'advisory\|emit.*signal\|signal.*emit' .claude/skills/mailpilot-reply-test/scripts/score_replies.py` -> advisory signals, not verdicts, for out-scope/compare
- `rg 'judge.*Sonnet\|Sonnet.*judge\|verdict.*judge' .claude/skills/mailpilot-reply-test/SKILL.md` -> Sonnet judge sub-agent for NL-shaped verdict

## §V.107 — CLI entity reference + polymorphic resolver

Keyed entities (account=email, company=domain, contact=email, tag=name) addressed by natural key. Keyless entities (email, note, task, workflow, enrollment) addressed by UUID. Polymorphic resolver: value matching UUIDv7 shape (`8-4-4-4-12` hex) → resolve by id; any other value → resolve by natural key (domain has dots, email has at-sign — never collide), case-insensitive. Unknown key → `not_found`. Every single-entity verb target = positional `<key>` arg, NEVER `--<entity>-id` option. Scope/owner options named for owner natural key (`--company-domain`, `--contact-email`). Account-requiring cmds take a single `--account-email` (polymorphic, resolves email|UUID). `account sync --account-email` is optional (all accounts when omitted). `account sync --since <iso>` bounds full-INBOX backfill on first sync.

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

`--limit <int>` (default 100) = sole result control (no `--offset`/`--order-by`/`--desc`). `--direction` = canonical inbound/outbound axis across email + workflow + template. Families realized as shared Click decorators (`limit_option`, `time_window_options(col)`, `include_disabled_option`, `scope_option`, `enum_option`, `range_options`, `presence_option`) composed fixed-order in `cli.py`/`_filters.py`. New list flag = new vocabulary decorator or spec change.

Trigger: `src/mailpilot/cli.py` or `src/mailpilot/_filters.py` changed.
- `rg 'limit_option|time_window_options|include_disabled_option|scope_option|enum_option|range_options|presence_option' src/mailpilot/` -> all 7 decorator names present
- `rg '"--direction"' src/mailpilot/cli.py` -> present on email|workflow|template list (no `--type`)
- `rg '"--route-method".*Choice\|click\.Choice.*route.method' src/mailpilot/cli.py` -> route-method is a Choice not free string
- `rg '"--limit"' src/mailpilot/cli.py | wc -l` -> present on every list cmd

## §V.116 — tags controlled vocabulary

Two tables: `tag` (vocabulary, one row/defined tag, `name` globally unique §V.90, soft-delete via `disabled_reason`) + `tag_assignment` (link, one row/(tag, owner), owner XOR company|contact). CLI verbs: `tag create <name>`, `tag view`, `tag disable <name>`, `tag enable <name>`, `tag add`, `tag remove`, `tag list`, `tag search`. `tag add` errors `not_found` on undefined tag, NEVER auto-creates. `tag list` = vocabulary + projected `usage_count`. `company list --tag <name>` / `contact list --tag <name>` = membership filter. `company list --no-tag <name>` = negated membership filter, repeatable (each one negated-membership predicate, all intersected). `--no-tag` resolves through vocabulary (undefined → `not_found`).

Trigger: `src/mailpilot/cli.py` or `src/mailpilot/database.py` changed.
- `rg '"tag"\b.*"create"\|"tag create"' src/mailpilot/cli.py` -> all verbs registered
- `rg 'not_found.*tag\b\|tag.*not_found' src/mailpilot/cli.py src/mailpilot/database.py` -> `not_found` on undefined (no auto-create)
- `rg '"--no-tag".*multiple.*True\|multiple.*True.*"--no-tag"' src/mailpilot/cli.py` -> `--no-tag` is repeatable

## §V.120 — send-obligation guard

Every send-obligated trigger turn MUST leave a `reply_email`|`send_email` ToolReturnPart without an `error` key, OR a successful `noop` ({acknowledged: true}), OR a `conclude_enrollment` terminal (§V.127). Send-obligated = inbound (`email is not None`, trigger in {email, task}) OR outbound first reach-out (trigger in {enrollment_run, enrollment_schedule}, `email is None`). `manual` trigger exempt. Guard `_sent_reply(result)` walks `result.all_messages()` after the §V.81 tool-count check; none of the above → raise `AgentCompletedWithoutReplyError`. Class is non-transient → `_handle_agent_failure` takes it terminal `failed` + `operator_event("error")`, NEVER silent completed. Prompt-side preventive = `_MUST_SEND` template fragment (§V.45).

Trigger: `src/mailpilot/agent/invoke.py` changed.
- `rg '_sent_reply\b' src/mailpilot/agent/invoke.py` -> guard present
- `rg 'AgentCompletedWithoutReplyError' src/mailpilot/exceptions.py` -> exception defined
- `rg 'enrollment_run\|enrollment_schedule' src/mailpilot/agent/invoke.py` -> outbound triggers included in guard (not inbound-only)
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

Inbound reply routing to an enrollment bulk-cancels that enrollment's pending future follow-up tasks: `UPDATE task SET status='cancelled' WHERE enrollment_id=%(id)s AND status='pending' AND scheduled_at > now() AND COALESCE(context->>'trigger','') <> 'enrollment_schedule'`. First-touch exclusion: rows whose trigger = `enrollment_schedule` (§V.32) are excluded. `cancel_enrollment_followup_tasks(connection, enrollment_id)` fires from 3 sites: (1) `routing.route_email` on successful inbound match (including pre-existing enrollment, not only first-insert branch), (2) calendar booking ingestion (§V.126/§V.128), (3) `conclude_enrollment` (§V.127).

Trigger: `src/mailpilot/routing.py`, `src/mailpilot/sync.py`, or `src/mailpilot/database.py` changed.
- `rg 'cancel_enrollment_followup_tasks' src/mailpilot/routing.py src/mailpilot/sync.py src/mailpilot/agent/invoke.py` -> present in 3 files
- `rg 'enrollment_schedule.*exclude\b\|exclude.*enrollment_schedule\b' src/mailpilot/database.py` -> first-touch exclusion in the query
- `rg 'scheduled_at.*>.*now\(\)\|now\(\).*<.*scheduled_at' src/mailpilot/database.py` -> only future tasks cancelled

## §V.124 — workflow.goal field

`workflow.goal` = free-text observable outcome that concludes the enrollment (e.g. "prospect books a Google Meet"). Renamed from `workflow.objective` via migration 006. One field, two readers: (1) conclude_enrollment disposition gate — agent calls `conclude_enrollment` when it judges goal met; system concludes deterministically on calendar booking regardless of stated goal; (2) classify.py semantic-match key for inbound workflow routing (§V.76). `_DEFERRED_TASK_TASK` fragment (§V.45) names "the workflow goal" (not "objective"). `record_enrollment_outcome` is system-internal (§V.15) — NOT exposed to the agent.

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

`conclude_enrollment(disposition, note, reschedule_at)` = sole agent-facing terminal tool. Disposition in {meeting_booked, do_not_contact, contact_later}. System side-effects per disposition: `meeting_booked` → `record_enrollment_outcome` + `cancel_enrollment_followup_tasks` + booking note; `do_not_contact` → conclude + cancel + `disable_contact`; `contact_later` → conclude + cancel + scheduled re-enrollment task at `reschedule_at` (agent-supplied, default >=3 months out). Counts as valid send-obligation terminal (§V.120) — `_sent_reply` walker accepts it like noop. `record_enrollment_outcome` is NOT in the agent tool set — it is system-internal (§V.15, §V.124).

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

## §V.130 — workflow-agent model settings

`_build_anthropic_model` (invoke.py) reads `anthropic_thinking`, `anthropic_effort`, `anthropic_max_tokens` settings into `AnthropicModelSettings`. `anthropic_max_tokens` ALWAYS passed as `max_tokens=<int>` (not empty-gated) — caps output stream so default-active thinking cannot exhaust the provider-default budget. `anthropic_thinking` and `anthropic_effort` added ONLY when non-empty (empty = knob off). Defaults: `anthropic_thinking=adaptive`, `anthropic_effort=high`, `anthropic_max_tokens=16384`. `xhigh` effort requires Opus 4.7+ (errors on Sonnet 4.6). Classifier `_get_model` (classify.py) EXCLUDED — one-shot structured-output decision, carries no settings. Caching flags (§V.47) unchanged on both call sites.

Trigger: `src/mailpilot/agent/invoke.py` or `src/mailpilot/settings.py` changed.
- `rg 'max_tokens.*anthropic_max_tokens\|anthropic_max_tokens.*max_tokens' src/mailpilot/agent/invoke.py` -> `max_tokens` always set (not in an `if` guard)
- `rg 'if.*anthropic_thinking\|if.*anthropic_effort' src/mailpilot/agent/invoke.py` -> conditionally added
- `rg 'max_tokens\b' src/mailpilot/agent/classify.py` -> zero hits (classifier excluded)
- `rg 'anthropic_cache_instructions.*True\|anthropic_cache_tool_definitions.*True' src/mailpilot/agent/invoke.py` -> caching flags still present (§V.47 regression)

## §V.131 — fallback acknowledgement on terminal inbound failure

`_handle_agent_failure` (run.py) terminal branch sends one fixed `_FALLBACK_ACKNOWLEDGEMENT` reply before `complete_task(status='failed')` when: `task.email_id` is set (inbound task) AND the reply-emitted contextvar flag (set by a successful `reply_email`/`send_email` in tools.py) is unset. Outbound first-touch (`email_id` NULL) stays silent. `_FALLBACK_ACKNOWLEDGEMENT` = code-defined fixed string in templates.py, content-free, never model-generated. Idempotency keyed on in-memory contextvar flag (NOT DB read — `connection.rollback()` at head of `_handle_agent_failure` erases mid-turn email row). Fallback-send failure: logs `operator_event("error", source='run.task.fallback_failed')` + falls through to `complete_task(status='failed')` — original terminal never masked.

Trigger: `src/mailpilot/run.py` or `src/mailpilot/agent/templates.py` changed.
- `rg '_FALLBACK_ACKNOWLEDGEMENT\b' src/mailpilot/agent/templates.py` -> constant present
- `rg 'reply_emitted_scope\|reply_emitted\b' src/mailpilot/agent/tools.py src/mailpilot/run.py` -> contextvar flag present in both
- `rg 'email_id.*fallback\|fallback.*email_id\|task\.email_id\b' src/mailpilot/run.py` -> inbound gate in `_handle_agent_failure`
- `rg 'fallback_failed\b' src/mailpilot/run.py` -> best-effort send failure error event
