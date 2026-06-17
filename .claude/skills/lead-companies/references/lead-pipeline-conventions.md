# lead-pipeline shared conventions

Single source of truth for the near-verbatim prose shared by the sibling
lead-pipeline skills `/lead-companies` and `/lead-contacts` (§V.100). Both skills
cite this one file instead of keeping per-skill copies aligned by hand. Only the
shared mechanics live here -- skill-specific parameters (run-summary counter
keys, the gate question/header text, the canonical Next examples) stay inline in
each skill body.

## Conventions

- ASCII-only project artifacts per §C. 
- All `mailpilot` commands run via `uv run mailpilot`.
- Envelope shape per §V.4: `list|search|...` -> `{"<plural>": [...], "ok": true}`; `view|create|update|...` -> `{"<singular>": {...}, "ok": true}`. Extract through the wrap.
- Parse JSON via `python3 -c '...'`; use `printf '%s' "$VAR"` over `echo "$VAR"` when piping captured JSON (`echo` mangles `\n` inside string fields).
- Capture stdout only (`2>/dev/null`) before any JSON parse. Every `uv run mailpilot` command writes an always-on operator-log line to stderr (`HH:MM:SS event=... k=v`); the JSON envelope -- including the `{"error":"duplicate_key", ...}` failure case from a `create` (`company create` / `contact create`) -- is on stdout. Do not `2>&1` into a JSON parser: the leading stderr line corrupts the parse (`Extra data: line 1 column N`) while the command actually succeeded.

## Batch gate

The stale-count gate each lead-pipeline skill runs before dispatch. `<rows>` =
the stale array the skill's stale query produced. The empty-set run summary, the
`AskUserQuestion` `question`, and its `header` are per-skill parameters each
citing skill supplies inline (their counter keys differ).

- `len(<rows>) == 0` -> emit the skill's empty-set run summary (every counter `0`, `"results": [], "ok": true`) and stop.
- `--limit N` given -> cap to first N, no question.
- `len(<rows>) > 10` and no `--limit` -> invoke `AskUserQuestion` (sole interaction gate) with the skill's `question` + `header` and these options:
  - `"First 10 (Recommended)"` -- cap to first 10
  - `"First 25"` -- cap to first 25
  - `"All <N>"` -- every stale row
- Else (1-10 rows) -> proceed w/ all, no question.

## OUTPUT -- "Next" block

Heading `## Next`; 1-5 atomic items (one sentence each, no `Reply` prefix);
positional dispatch. Each citing skill supplies its own canonical example items
under this format rule.
