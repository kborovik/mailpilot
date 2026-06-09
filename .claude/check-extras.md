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
