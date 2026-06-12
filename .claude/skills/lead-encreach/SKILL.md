---
name: lead-encreach
description: |
  Create company records from domain names or CSV exports (TheirStack and
  similar), query companies missing profile enrichment, and dispatch
  concurrent Sonnet enricher agents that fetch the company website
  (curl + lynx, Tavily fallback) and distill a cold-email-grade JSON
  profile into company.profile. Single free-form invocation -- no
  sub-commands; the skill classifies the input itself. External data
  sources contribute the apex domain plus an optional CSV display-name
  placeholder -- all profile fields are agent-discovered from the website.
  Triggers on "/lead-encreach", "enrich companies", "create companies from domains".
argument-hint: [<domain>... | <file-path>] [--limit N]
allowed-tools: Bash(mailpilot company *), Bash(curl *), Bash(lynx *), Bash(python3 *), Read, Task, Workflow, AskUserQuestion
model: opus
---

# lead-encreach

Bootstrap company records from external lead dumps (TheirStack CSV, plain-text domain lists), then enrich each row w/ a cold-email-grade `CompanyProfile` JSON via concurrent Sonnet enricher agents.

Spec: §V.72 (`company.profile` JSONB column and `CompanyProfile` Pydantic model and `company list --no-profile|--has-profile` filter).

## Pipeline

One implicit pipeline — no sub-command dispatch. Classify free-form args, run every applicable stage in order:

| Args look like | Stages |
|---|---|
| file path(s) (existing file on disk) | ingest -> seed -> stale -> gate -> enrich |
| domain/URL token(s) | seed -> stale (scoped to those rows) -> enrich |
| UUID token(s) | stale (scoped to those rows) -> enrich |
| bare invocation | stale -> gate -> enrich |

Classification rules:

- Arg resolves to an existing file on disk -> file path.
- Arg matches UUID shape -> company id.
- Else -> domain/URL token.
- Mixed args admitted — ingest files first, then seed inline domains, single combined stale pass.
- `--limit N` pre-answers the batch gate (no AskUserQuestion fires).

Semantics: domain arg is idempotent "ensure exists and enriched". Seeding a duplicate is a no-op; an already-enriched row (`profile` not NULL) -> skip, report `already_enriched`. So no seed/enrich verb distinction — re-running the same invocation converges.

## Fast path (consolidated execution)

Minimize tool calls — run the two stages each as ONE tool call:

- **Ingest + seed + stale** -> ONE Bash call to `scripts/seed_companies.py` (this skill's dir), not a per-row `curl` + `company create` loop. The script implements the §V.74 recipes verbatim (csv.DictReader RFC-4180 parse; `curl -w '%{url_effective}'` redirect resolve) and §V.72 (apex + optional CSV name placeholder only, not profile-body prepopulation). The per-stage recipe blocks below stay the canonical spec-of-record the script mirrors.

  ```
  python3 .claude/skills/lead-encreach/scripts/seed_companies.py [--dry-run] [--column NAME] <file-or-domain>...
  ```

  Emits ONE JSON object: `{created|would_create, existing, skipped, collapsed, stale, dry_run, ok}`. The `stale` field is the exact `[{id, domain, name}]` projection the enrich Workflow consumes so not a second `company list --no-profile` round trip. `--dry-run` resolves + reports w/o DB writes (preview). Mixed file + inline-domain args admitted; UUID args land in `skipped` (enrich-only, not seedable).

- **Enrich (>=2 stale rows)** -> ONE Workflow call BY NAME: `Workflow({name: 'lead-encreach-enrich', args: <stale array from the seed script>})`, not pasting the inline snippet. Single stale row -> direct `Task(subagent_type="company-profiler", ...)` per §Stage: enrich.

So full file-arg pipeline = 1 Bash (seed script) + 1 Workflow (enrich) + the batch-gate `AskUserQuestion` when >10 stale and not `--limit` — replacing the ~2N+3 per-row Bash calls. The per-stage sections below document the canonical behavior; reach for them only when debugging a row the script reported in `skipped`.

## Scope

- Ingest: file -> apex domains (+ optional CSV display name) -> `mailpilot company create` (placeholder name = CSV name when present, else apex domain).
- Seed: domain -> same per-domain create pipeline.
- Stale: rows w/ `profile IS NULL` — internal query step, not operator verb.
- Enrich: dispatch `company-profiler` sub-agent (`.claude/agents/company-profiler.md`, `model: sonnet`) which fetches the site, distills a `CompanyProfile`, persists via `mailpilot company update --profile-json`.

External data sources contribute the apex domain + an optional `company.name` display placeholder (CSV mode only). The profile JSONB body {summary, products, target_customers, timezone, sources} is 100% agent-discovered from the website — do not pre-populate any profile field from CSV columns or text-line annotations (§V.72). The name placeholder is a transient pre-enrichment label; the enricher overwrites it w/ the site-discovered canonical name.

## Conventions

- ASCII-only project artifacts per §C. Math-glyph encoding admitted for skill prose per `/sdd:glyph`.
- All `mailpilot` commands run via `uv run mailpilot`.
- Envelope shape per §V.4: `list|search|...` -> `{"<plural>": [...], "ok": true}`; `view|create|update|...` -> `{"<singular>": {...}, "ok": true}`. Extract through the wrap.
- Parse JSON via `python3 -c '...'`; use `printf '%s' "$VAR"` over `echo "$VAR"` when piping captured JSON (`echo` mangles `\n` inside string fields).
- Capture stdout only (`2>/dev/null`) before any JSON parse. Every `uv run mailpilot` command writes an always-on operator-log line to stderr (`HH:MM:SS event=... k=v`); the JSON envelope — including the `{"error":"duplicate_key", ...}` failure case from `company create` — is on stdout. Do not `2>&1` into a JSON parser: the leading stderr line corrupts the parse (`Extra data: line 1 column 3`) while the command actually succeeded.

## Prerequisites

- `mailpilot` installed locally w/ a working DB (`mailpilot config get database_url`).
- `curl` and `lynx` on PATH.
- `mcp__claude_ai_Tavily__tavily_extract` reachable — the enricher's sole fetch fallback (curl + lynx -> Tavily). Matches `company-profiler` agent tool surface; not FireCrawl (token historically dead, agent not granted the tool).
- Anthropic credentials reachable (Sonnet enrichers).

## Stage: ingest (file args)

Source- and format-agnostic ingestion. Extracts the apex domain (+ optional CSV display name per §V.72 carve-out); not profile-body content.

1. Detect format from the file's first non-empty line (peek at raw bytes, not the `Read` tool's line-numbered output):
   - Contains `,` and >=1 known header token (`domain`, `website`, `company_url`, `url`) -> **CSV mode**.
   - Else -> **plain-text mode**.
2. **CSV mode** — ! parse with an RFC-4180 parser (`csv.DictReader`), not physical-line iteration of `Read`-tool output or split-on-`\n` / split-on-`,` (per §V.74). Quoted fields carry embedded newlines and commas (lead-export `company_description` columns) so one logical row spans many physical lines; line iteration mis-seeds prose fragments as phantom rows. Domain column auto-detect = first match in `[domain, website, company_url, url]`; operator MAY name a column in the invocation prose to override. Name column auto-detect = first match in `[company_name, name, company]` — seeds the `company.name` placeholder per §V.72 carve-out (display label only, not a profile field; enricher overwrites w/ site-canonical name). Extraction recipe — one printed line per logical row, TAB-separated `<domain>\t<display-name>` (name blank when no name-ish column):
   ```
   python3 - "$CSV_PATH" "${COLUMN:-}" <<'PY'
   import csv, sys
   path, override = sys.argv[1], (sys.argv[2] or None)
   domain_candidates = ["domain", "website", "company_url", "url"]
   name_candidates = ["company_name", "name", "company"]
   with open(path, newline="", encoding="utf-8-sig") as handle:
       reader = csv.DictReader(handle)
       columns = reader.fieldnames or []
       column = override or next((c for c in domain_candidates if c in columns), None)
       if column is None:
           sys.exit("no domain column found; name the column in the invocation")
       name_col = next((c for c in name_candidates if c in columns), None)
       for row in reader:
           value = (row.get(column) or "").strip()
           if not value:
               continue
           name = (row.get(name_col) or "").strip() if name_col else ""
           print(value + "\t" + name)  # <domain> TAB <display-name>; name MAY be empty
   PY
   ```
   `newline=""` keeps the parser in charge of embedded newlines; `encoding="utf-8-sig"` strips a leading BOM. Split each printed line on the first TAB -> `(domain, display_name)`; an empty `display_name` -> seed stage falls back to the resolved apex.
3. **Plain-text mode**: every non-empty non-comment line (skip lines starting w/ `#`): extract apex from the line (raw domain or full URL admitted). Line iteration admitted here per §V.74 — non-CSV, one domain/URL per physical line.
4. Every extracted `(domain, display_name)` pair: hand to the seed stage below (plain-text mode yields `display_name = ""`).
5. Do not pre-populate the profile JSONB body from CSV columns or text-line annotations — every seeded row lands `profile IS NULL` so agent enrichment downstream (§V.72). The CSV `company.name` placeholder (step 2) is the ONLY non-domain datum carried; all other columns (descriptions, industry, revenue, LinkedIn) discarded.

## Stage: seed (domain values)

Every value — `(domain, display_name)` from CSV ingest, or a bare domain from an inline/plain-text arg (`display_name = ""`):

1. `apex = extract_apex(domain)` — lowercase, strip leading `www.`, parse via `urllib.parse.urlsplit` if URL-shaped.
2. `resolved = resolve_apex(apex)` — follow the full redirect chain via `curl -sL -o /dev/null --max-time 12 -w '%{url_effective}' -A "Mozilla/5.0" "https://<apex>/"` (hop-agnostic, CR-free per §V.74); re-extract apex from the final effective URL if the chain ended elsewhere; else `resolved = apex`. The `display_name` travels with the row unchanged (a redirect to a sister domain does not alter the company's display label).
3. `name = display_name if display_name else resolved` (§V.72 carve-out: CSV display name when present, else the apex placeholder). `uv run mailpilot company create --domain <resolved> --name <name>` — race-safe per §V.16 (duplicate -> envelope `{"error":"duplicate_key", ...}` w/ exit 1 -> treat as existing, continue). Name is placeholder; the enricher overwrites it w/ the site-canonical name.
4. Track per-value outcome for the run summary: `{"created": [<id>...], "existing": [<domain>...], "skipped": [{"input": ..., "resolved": ..., "reason": ...}]}`.

Collision-on-resolved-apex (resolved-apex already owned by another company row): skip + log per §V.72 design — operator dedups manually. Record each collision in a `collapsed: [{"inputs": [...], "resolved": <apex>}]` accumulator for the run summary — distinct source rows redirecting onto one apex (e.g. two differently-named CSV rows sharing a resolved domain) silently become one company; the `collapsed` detail is the only operator-visible signal of the merge.

## Stage: stale query

```
uv run mailpilot company list --no-profile [--limit N]
```

Envelope unwrap -> `companies[]` (`CompanySummary` w/ `has_profile=false`). Domain/UUID-scoped runs: resolve each arg to its row (`uv run mailpilot company search "<arg>" --limit 1` for domains, `uv run mailpilot company view <ID>` for UUIDs; no match -> record `{"error": "not_found", "input": <arg>}` in the run summary) and enrich only those still stale — already-enriched rows report `already_enriched`.

## Stage: batch gate

- `len(companies) == 0` -> emit `{"enriched": 0, "skipped": 0, "failed": 0, "results": [], "ok": true}` and stop.
- `--limit N` given -> cap to first N, no question.
- `len(companies) > 10` and no `--limit` -> invoke `AskUserQuestion` (sole interaction gate):
  - **question**: `"<N> companies need enrichment. How many should the enricher process this run?"`
  - **header**: `"Enrich batch"`
  - **options**:
    - `"First 10 (Recommended)"` — cap to first 10
    - `"First 25"` — cap to first 25
    - `"All <N>"` — every stale row
- Else (1-10 rows) -> proceed w/ all, no question.

## Stage: enrich

Single stale row -> direct dispatch, not Workflow overhead: `Task(subagent_type="company-profiler", prompt=<built prompt>)`. Built prompt template:

```
Enrich the company profile for:
  company_id: <ID>
  domain: <company.domain>
  placeholder_name: <company.name>

Follow your system prompt procedure. Return the JSON verdict per spec.
```

>=2 stale rows -> hand the capped `companies[]` to the `Workflow` tool as `args`. Capture the array from the stale query first (stdout only — see §Conventions), projecting just the fields the snippet reads:

```
uv run mailpilot company list --no-profile 2>/dev/null \
  | python3 -c 'import sys, json; rows = json.load(sys.stdin)["companies"]; print(json.dumps([{"id": c["id"], "domain": c["domain"], "name": c["name"]} for c in rows]))'
```

Pass that JSON array as the Workflow `args` value directly — an actual JSON value, not a file path or a re-stringified blob. The snippet's `typeof args === 'string'` guard covers the runtime-stringifies case either way.

The enrich logic is saved at `.claude/workflows/lead-encreach-enrich.js` so INVOKE BY NAME — `Workflow({name: 'lead-encreach-enrich', args: <array>})` — do not re-paste the body. The block below is the §V.73 spec-of-record mirror of that saved file (self-contained, runnable as authored); the body below `meta` ! stay byte-identical to the saved file (the saved `meta` adds registry-only fields — `whenToUse`, a fuller `description`):

```js
export const meta = {
  name: 'lead-encreach-enrich',
  description: 'Concurrently enrich stale company profiles',
  phases: [{title: 'Enrich', detail: 'enricher agents, 3 in flight'}],
}

// `stale` source: the `companies[]` captured from the seed script's `stale`
// field (or `mailpilot company list --no-profile`), handed in via Workflow
// `args`. The runtime delivers `args` as a JSON string, so parse it (guard the
// already-parsed case). To paste rows directly instead, replace this line with
// an inline literal: const stale = [{...}, ...].
const stale = typeof args === 'string' ? JSON.parse(args) : args

const ENRICH_RESULT_SCHEMA = {
  type: 'object',
  required: ['company_id', 'domain', 'status'],
  properties: {
    company_id: {type: 'string'},
    domain: {type: 'string'},
    status: {enum: ['enriched', 'skipped', 'failed']},
    reason: {type: 'string'},
  },
}

function buildPrompt(c) {
  return [
    'Enrich the company profile for:',
    `  company_id: ${c.id}`,
    `  domain: ${c.domain}`,
    `  placeholder_name: ${c.name}`,
    '',
    'Follow your system prompt procedure. Return the JSON verdict per spec.',
  ].join('\n')
}

phase('Enrich')

// Chunk into batches of 3 so at most 3 enricher agents run at once -- this
// (not the runtime cap) is what honors the concurrency-3 budget (V.72/V.73). A
// bare parallel(stale.map(...)) would submit all stale.length at once, bounded
// only by the runtime cap min(16, cores-2).
const results = []
for (let i = 0; i < stale.length; i += 3) {
  const batch = stale.slice(i, i + 3)
  const batchResults = await parallel(batch.map(c => () =>
    agent(buildPrompt(c), {
      label: `enrich:${c.domain}`,
      agentType: 'company-profiler',
      schema: ENRICH_RESULT_SCHEMA,
    })
  ))
  results.push(...batchResults)
}
return results.filter(Boolean)
```

The batch loop caps in-flight enrichers at 3 per `parallel()` call — the chunking, not any runtime setting, enforces the concurrency-3 budget per §V.73. The Workflow runtime's own per-call cap = `min(16, cores-2)` is a separate, higher ceiling.

## Run summary

After all stages, emit one aggregate JSON: `{"created": N, "existing": N, "enriched": N, "skipped": N, "failed": N, "results": [...], "ok": true}` — omit seed fields on bare invocations, omit enrich fields when 0 stale rows. When >=1 collision-on-resolved-apex fired (per §Stage: seed), append `"collapsed": [{"inputs": [...], "resolved": <apex>}]` so the operator sees which distinct source rows merged onto one company — a bare `existing: N` count hides the merge.

## Domain extraction & redirect resolution

```
extract_apex(url_or_domain):
    1. parse -> host (urllib.parse.urlsplit)
    2. lowercase
    3. strip leading "www."
    4. return host (no further subdomain stripping)

resolve_apex(initial):
    # -w '%{url_effective}' prints the final URL after the full redirect chain,
    # CR-free (per spec V.74). It is always set, so when there is no redirect
    # the final apex equals `initial`. A HEAD-based location-header grep is
    # brittle: 403 bot-blocking origins answer HEAD differently, and awk
    # retains the header's trailing CR -> corrupts a bare-host redirect target.
    final_url = curl -sL -o /dev/null --max-time 12 -w '%{url_effective}' \
                -A "Mozilla/5.0" "https://<initial>/"
    return extract_apex(final_url) if final_url else initial
```

Subdomain preservation: `shop.acme.com` not collapsed to `acme.com` — preserves distinct entity identity if shop is a separate company row.

## Rendering

§V.3 JSON-stdout invariant untouched. After `mailpilot company view <id>` returns the JSON envelope (carrying `profile` inline per §V.8), skill formats `profile` blob as YAML for operator display in chat. No CLI change.

## Profile shape

Stored as `company.profile JSONB NULL`, validated server-side via `CompanyProfile` Pydantic model (`src/mailpilot/models.py` per §V.72):

```yaml
summary: str           # 1-5 sentences -- what they do, who they serve, hook
products: list[str]    # what they sell -- pitch relevance
target_customers: str  # who they sell to -- ICP signal
timezone: str | None   # IANA, e.g. "America/Toronto"; null if multi-zone or unclear
sources: list[str]     # >=1 url fetched/cited
```

Required = {summary, products, target_customers, sources}. Optional = {timezone}. Invalid JSON -> CLI rejects w/ `{"error":"validation_error", ...}` envelope per §V.54.

## OUTPUT — "Next" block

Heading `## Next`; 1-5 atomic items (one sentence each, no `Reply` prefix); positional dispatch.

Canonical examples — after a seeding run:

```
## Next

1. /lead-encreach -- enrich the newly seeded stale rows
2. mailpilot company list --no-profile -- inspect stale rows first
3. /lead-encreach <domain> -- enrich one row before the batch
```

After an enrichment run:

```
## Next

1. mailpilot company list --no-profile -- confirm zero stale rows remain
2. mailpilot company list --has-profile --limit 5 -- spot-check enrichment quality
3. /lead-encreach <domain> -- re-run a failed row
```

## Why this skill exists

External lead dumps (TheirStack CSV and similar) seed company rows via `mailpilot company create --domain` then need cold-email-grade distillation per row. Notes (§V.13 / §V.14) append-only so wrong shape for idempotently-updatable lead profile. JSONB + Pydantic = in-place updates + mechanical schema check + single-column stale-query (`WHERE profile IS NULL`).
