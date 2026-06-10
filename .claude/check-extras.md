## §V.6 — SKILL.md Drift Check

Mechanical audit (⊥ LLM-judgment); trigger when `src/mailpilot/SKILL.md`, `.claude/skills/**/*.md`, `src/mailpilot/cli.py`, or `src/mailpilot/settings.py` changed.

File-set scope:
- `src/mailpilot/SKILL.md` — packaged skill body (external LLM agents); all four checks apply.
- `.claude/skills/**/*.md` — operator-facing skill bodies (smoke-test, demo-test, etc.); per §V.6(+) / §B.65 only checks (i) ∧ (ii) apply (skill bodies ⊥ enumerate settings ∴ (iii) ∧ (iv) ⊥ apply).

Checks:
(i) per-noun verb roster ⊇ `@<noun>.command("<verb>")` set ∈ `cli.py` — fail mode: skill names a retired verb (e.g. `enrollment remove` post-T92).
(ii) per-verb `--<flag>` tokens ∈ recipes ⊆ `@click.option("--<flag>")` set for that handler ∈ `cli.py`.
(iii) settings key list ∈ `## Settings` ≡ `Settings.model_fields` keys ∈ `settings.py` — `src/mailpilot/SKILL.md` only.
(iv) env-var-prefix description ∈ `## Settings` ≡ `SettingsConfigDict(env_prefix=...)` value ∈ `settings.py` (`MAILPILOT_*`) — `src/mailpilot/SKILL.md` only.

## §V.68 — _fact_check_body corpus-build algorithm

Per-document scoping (see `src/mailpilot/agent/tools.py:_fact_check_body`):
- table-bearing doc (any line contains `|`): contribute pipe-row lines (`"|" in line`) + list-item lines (`re.match(r"^\s*[-*]\s", line)`)
- prose-only doc (no `|` lines): contribute full content

Zero-ledger (no `read_drive_markdown` calls this invocation) → hook no-op.

## §V.73 — Skill-body Workflow snippet executability

Mechanical audit; trigger when `.claude/skills/**/*.md` changed. Scope = every fenced ```js block that calls `parallel(`, `pipeline(`, or `agent(`.

Per ```js block:
(a) Free-symbol scan — every identifier used as a value ! resolve to an in-block definition (`const` / `let` / `function` / param) OR a runtime global. Runtime globals (do ⊥ flag): `meta`, `agent`, `parallel`, `pipeline`, `phase`, `log`, `args`, `budget`, `workflow`, plus JS built-ins (`JSON`, `Math`, `Array`, `Object`, `Promise`, `console`, ...). Any other bare identifier (e.g. `stale`, `buildPrompt`, `ENRICH_RESULT_SCHEMA`) ! be defined in the block — fail mode: free var crashes `ReferenceError` on paste (§B.68: bare `stale`).
(b) `args`-as-collection guard — if the block calls `args.map` / `args.filter` / `args.slice` / `args.length` / `args.forEach` or spreads `args`, it ! first `JSON.parse(args)` (∨ guard `typeof args === 'string'`). Why: runtime delivers `args` as a JSON STRING ∴ `args.map` throws `is not a function` (§B.68).
(c) Prose-vs-`parallel` divergence — if surrounding prose claims "concurrency N" / "N concurrent" / "Default N", the block ! chunk to N (batch loop of size N around `parallel(batch.map(...))`). A bare `parallel(xs.map(...))` dispatches all `xs.length`, bounded only by runtime cap `min(16, cores-2)` — ⊥ N. Fail mode: prose promises 3, snippet runs all (§B.68 secondary).

Mechanical greps (manual judgment on hits):
- `rg -n '```js' .claude/skills/` — enumerate blocks.
- `rg -nE '\bargs\.(map|filter|slice|length|forEach)\b' .claude/skills/` not preceded by `JSON.parse(args)` ∨ `typeof args` → (b) fail.
- prose `rg -niE 'concurrency [0-9]|[0-9] concurrent|default [0-9]' .claude/skills/` near a block with bare `parallel(` and no batch loop (`for .* += N` / `.slice(`) → (c) fail.

## §V.74 — RFC-4180 CSV-ingestion parser mandate

Mechanical audit; trigger when `.claude/skills/**/*.md` ∨ `src/**` changed. Scope = CSV-ingestion sites (handle a `.csv` path, a "CSV mode", or a comma-delimited lead export).

Checks:
(i) CSV ingestion ! use an RFC-4180 parser (`csv.DictReader` / `csv.reader` / the `csv` module). Fail mode: physical-line iteration, `.splitlines()`, `.split("\n")`, ∨ `.split(",")` over CSV content — quoted fields carry embedded newlines ∧ commas ∴ one logical row spans many physical lines (§B.69: theirstack.csv 25 logical rows over 217 physical lines).
(ii) Redirect resolution ! use `curl -sL -o /dev/null -w '%{url_effective}'` (full chain, CR-free). Fail mode: HEAD `curl -sLI | grep '^location:' | awk` — 403 bot-blocking origins answer HEAD differently; awk retains the header trailing CR ∴ corrupts a bare-host redirect target (§B.69).

Mechanical greps (manual judgment on hits — flag only in CSV context):
- `rg -n 'splitlines|\.split\(' .claude/skills/ src/` near `csv` / `CSV` / `.csv` context → (i) fail. Non-CSV `splitlines` (email-body normalization, markdown line scan) ⊥ flagged.
- `rg -n 'curl -sLI' .claude/skills/ src/` → (ii) fail (HEAD-grep redirect resolution).

Plain-text (non-CSV) line iteration is admitted (per-line domain/URL, `#`-comment skip) — ⊥ flag.
