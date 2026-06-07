## §V.6 — SKILL.md Drift Check

Mechanical audit (⊥ LLM-judgment); trigger when `src/mailpilot/SKILL.md`, `src/mailpilot/cli.py`, or `src/mailpilot/settings.py` changed:

(i) per-noun verb roster ∈ `## Grammar` ⊇ `@<noun>.command("<verb>")` set ∈ `cli.py`
(ii) per-verb `--<flag>` tokens ∈ recipes ⊆ `@click.option("--<flag>")` set for that handler ∈ `cli.py`
(iii) settings key list ∈ `## Settings` ≡ `Settings.model_fields` keys ∈ `settings.py`
(iv) env-var-prefix description ∈ `## Settings` ≡ `SettingsConfigDict(env_prefix=...)` value ∈ `settings.py` (`MAILPILOT_*`)

## §V.68 — _fact_check_body corpus-build algorithm

Per-document scoping (see `src/mailpilot/agent/tools.py:_fact_check_body`):
- table-bearing doc (any line contains `|`): contribute pipe-row lines (`"|" in line`) + list-item lines (`re.match(r"^\s*[-*]\s", line)`)
- prose-only doc (no `|` lines): contribute full content

Zero-ledger (no `read_drive_markdown` calls this invocation) → hook no-op.
