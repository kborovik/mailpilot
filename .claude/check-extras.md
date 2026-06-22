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

## §V.42 — Outbound format-check rejection algorithm

Trigger when `src/mailpilot/agent/` or `src/mailpilot/email_renderer.py` changed.

Rejection condition: >= 3 consecutive spec-shape lines (short label + whitespace + value) in reply body w/o `|---|` separator -> `format_check_mismatch`. ASCII rule-lines (`---`, `===`, `___`) not treated as separators.

_BASE requirement: MUST explicitly mandate GFM pipe table w/ header row + `|---|` separator for spec rows (model numbers, flow rates, dimensions, capacities); "may use Markdown tables" (permissive) alone insufficient — format-lint is backstop, not primary enforcement.

Mechanical check:
- `rg -n 'may use Markdown' src/mailpilot/agent/templates.py` -> zero hits (permissive wording retired).

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
