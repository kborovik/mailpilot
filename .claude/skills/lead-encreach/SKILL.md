---
name: lead-encreach
description: |
  Create company records from domain names or CSV exports (TheirStack and
  similar), query companies missing profile enrichment, and dispatch
  concurrent Sonnet enricher agents that fetch the company website
  (curl + lynx, Firecrawl fallback) and distill a cold-email-grade JSON
  profile into company.profile. Single free-form invocation -- no
  sub-commands; the skill classifies the input itself. External data
  sources contribute the apex domain only -- all profile fields are
  agent-discovered.
  Triggers on "/lead-encreach", "enrich companies", "create companies from domains".
argument-hint: [<domain>... | <file-path>] [--limit N]
allowed-tools: Bash(mailpilot company *), Bash(curl *), Bash(lynx *), Read, Task, AskUserQuestion
model: opus
---

# lead-encreach

Bootstrap company records from external lead dumps (TheirStack CSV, plain-text domain lists), then enrich each row w/ a cold-email-grade `CompanyProfile` JSON via concurrent Sonnet enricher agents.

Spec: §V.72 (`company.profile` JSONB column ∧ `CompanyProfile` Pydantic model ∧ `company list --no-profile|--has-profile` filter).

## Pipeline

One implicit pipeline — ⊥ sub-command dispatch. Classify free-form args, run every applicable stage in order:

| Args look like | Stages |
|---|---|
| file path(s) (existing file on disk) | ingest → seed → stale → gate → enrich |
| domain/URL token(s) | seed → stale (scoped to those rows) → enrich |
| UUID token(s) | stale (scoped to those rows) → enrich |
| bare invocation | stale → gate → enrich |

Classification rules:

- Arg resolves to an existing file on disk → file path.
- Arg matches UUID shape → company id.
- Else → domain/URL token.
- Mixed args admitted — ingest files first, then seed inline domains, single combined stale pass.
- `--limit N` pre-answers the batch gate (⊥ AskUserQuestion fires).

Semantics: domain arg ≡ idempotent "ensure exists ∧ enriched". Seeding a duplicate is a no-op; an already-enriched row (`profile` ⊥ NULL) → skip, report `already_enriched`. ∴ ⊥ seed/enrich verb distinction — re-running the same invocation converges.

## Scope

- Ingest: file → apex domains → `mailpilot company create` (placeholder name = apex domain).
- Seed: domain → same per-domain create pipeline.
- Stale: rows w/ `profile IS NULL` — internal query step, ⊥ operator verb.
- Enrich: dispatch `company-profiler` sub-agent (`.claude/agents/company-profiler.md`, `model: sonnet`) which fetches the site, distills a `CompanyProfile`, persists via `mailpilot company update --profile-json`.

External data sources contribute the apex domain only. All profile fields are agent-discovered. ⊥ pre-populate from CSV columns ∨ text-line annotations.

## Conventions

- ASCII-only project artifacts per §C. Math-glyph encoding admitted for skill prose per `/sdd:glyph`.
- All `mailpilot` commands run via `uv run mailpilot`.
- Envelope shape per §V.4: `list|search|...` → `{"<plural>": [...], "ok": true}`; `view|create|update|...` → `{"<singular>": {...}, "ok": true}`. Extract through the wrap.
- Parse JSON via `python3 -c '...'`; use `printf '%s' "$VAR"` over `echo "$VAR"` when piping captured JSON (`echo` mangles `\n` inside string fields).

## Prerequisites

- `mailpilot` installed locally w/ a working DB (`mailpilot config get database_url`).
- `curl` ∧ `lynx` on PATH.
- `mcp__claude_ai_FireCrawl__firecrawl_scrape` reachable (fallback).
- Anthropic credentials reachable (Sonnet enrichers).

## Stage: ingest (file args)

Source- ∧ format-agnostic ingestion. Apex-domain-only extraction.

1. Detect format from the file's first non-empty line (peek at raw bytes, ⊥ the `Read` tool's line-numbered output):
   - Contains `,` ∧ ≥1 known header token (`domain`, `website`, `company_url`, `url`) → **CSV mode**.
   - Else → **plain-text mode**.
2. **CSV mode** — ! parse with an RFC-4180 parser (`csv.DictReader`), ⊥ physical-line iteration of `Read`-tool output ∨ split-on-`\n` / split-on-`,` (per §V.74). Quoted fields carry embedded newlines ∧ commas (lead-export `company_description` columns) ∴ one logical row spans many physical lines; line iteration mis-seeds prose fragments as phantom rows. Column auto-detect = first match in `[domain, website, company_url, url]`; operator MAY name a column in the invocation prose to override. Extraction recipe — one printed line per logical row:
   ```
   python3 - "$CSV_PATH" "${COLUMN:-}" <<'PY'
   import csv, sys
   path, override = sys.argv[1], (sys.argv[2] or None)
   candidates = ["domain", "website", "company_url", "url"]
   with open(path, newline="", encoding="utf-8-sig") as handle:
       reader = csv.DictReader(handle)
       columns = reader.fieldnames or []
       column = override or next((c for c in candidates if c in columns), None)
       if column is None:
           sys.exit("no domain column found; name the column in the invocation")
       for row in reader:
           value = (row.get(column) or "").strip()
           if value:
               print(value)
   PY
   ```
   `newline=""` keeps the parser in charge of embedded newlines; `encoding="utf-8-sig"` strips a leading BOM.
3. **Plain-text mode**: ∀ non-empty non-comment line (skip lines starting w/ `#`): extract apex from the line (raw domain ∨ full URL admitted). Line iteration admitted here per §V.74 — non-CSV, one domain/URL per physical line.
4. ∀ extracted value: hand to the seed stage below.
5. ⊥ pre-populate profile from CSV columns ∨ text-line annotations. All non-domain content discarded — every seeded row lands stale ∴ agent enrichment downstream.

## Stage: seed (domain values)

∀ value (inline arg ∨ ingest output):

1. `apex = extract_apex(domain)` — lowercase, strip leading `www.`, parse via `urllib.parse.urlsplit` if URL-shaped.
2. `resolved = resolve_apex(apex)` — follow the full redirect chain via `curl -sL -o /dev/null --max-time 12 -w '%{url_effective}' -A "Mozilla/5.0" "https://<apex>/"` (hop-agnostic, CR-free per §V.74); re-extract apex from the final effective URL if the chain ended elsewhere; else `resolved = apex`.
3. `uv run mailpilot company create --domain <resolved> --name <resolved>` — race-safe per §V.16 (duplicate → envelope `{"error":"duplicate_key", ...}` w/ exit 1 → treat as existing, continue).
4. Track per-value outcome for the run summary: `{"created": [<id>...], "existing": [<domain>...], "skipped": [{"input": ..., "resolved": ..., "reason": ...}]}`.

Collision-on-resolved-apex (resolved-apex already owned by another company row): skip + log per §V.72 design — operator dedups manually.

## Stage: stale query

```
uv run mailpilot company list --no-profile [--limit N]
```

Envelope unwrap → `companies[]` (`CompanySummary` w/ `has_profile=false`). Domain/UUID-scoped runs: resolve each arg to its row (`uv run mailpilot company search "<arg>" --limit 1` for domains, `uv run mailpilot company view <ID>` for UUIDs; ⊥ match → record `{"error": "not_found", "input": <arg>}` in the run summary) ∧ enrich only those still stale — already-enriched rows report `already_enriched`.

## Stage: batch gate

- `len(companies) == 0` → emit `{"enriched": 0, "skipped": 0, "failed": 0, "results": [], "ok": true}` ∧ stop.
- `--limit N` given → cap to first N, ⊥ question.
- `len(companies) > 10` ∧ no `--limit` → invoke `AskUserQuestion` (sole interaction gate):
  - **question**: `"<N> companies need enrichment. How many should the enricher process this run?"`
  - **header**: `"Enrich batch"`
  - **options**:
    - `"First 10 (Recommended)"` — cap to first 10
    - `"First 25"` — cap to first 25
    - `"All <N>"` — every stale row
- Else (1-10 rows) → proceed w/ all, ⊥ question.

## Stage: enrich

Single stale row → direct dispatch, ⊥ Workflow overhead: `Task(subagent_type="company-profiler", prompt=<built prompt>)`. Built prompt template:

```
Enrich the company profile for:
  company_id: <ID>
  domain: <company.domain>
  placeholder_name: <company.name>

Follow your system prompt procedure. Return the JSON verdict per spec.
```

≥2 stale rows → hand the capped `companies[]` to the `Workflow` tool as `args`. The snippet below is self-contained and runnable as authored (per §V.73):

```js
export const meta = {
  name: 'lead-encreach-enrich',
  description: 'Concurrently enrich stale company profiles',
  phases: [{title: 'Enrich', detail: 'enricher agents, 3 in flight'}],
}

// `stale` source: the `companies[]` captured from the stale-query stage,
// handed in via Workflow `args`. The runtime delivers `args` as a JSON
// string, so parse it (guard the already-parsed case). To paste rows
// directly instead, replace this line with an inline literal:
// const stale = [{...}, ...].
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
// (not the runtime cap) is what honors the concurrency-3 budget (V.72). A
// bare parallel(stale.map(...)) would submit all stale.length at once,
// bounded only by the runtime cap min(16, cores-2).
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

The batch loop caps in-flight enrichers at 3 per `parallel()` call — the chunking, ⊥ any runtime setting, enforces the concurrency-3 budget per §V.73. The Workflow runtime's own per-call cap = `min(16, cores-2)` is a separate, higher ceiling.

## Run summary

After all stages, emit one aggregate JSON: `{"created": N, "existing": N, "enriched": N, "skipped": N, "failed": N, "results": [...], "ok": true}` — omit seed fields on bare invocations, omit enrich fields when 0 stale rows.

## Domain extraction & redirect resolution

```
extract_apex(url_or_domain):
    1. parse → host (urllib.parse.urlsplit)
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

Subdomain preservation: `shop.acme.com` ⊥ collapsed to `acme.com` — preserves distinct entity identity if shop is a separate company row.

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

Required ≡ {summary, products, target_customers, sources}. Optional ≡ {timezone}. Invalid JSON → CLI rejects w/ `{"error":"validation_error", ...}` envelope per §V.54.

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

External lead dumps (TheirStack CSV ∧ similar) seed company rows via `mailpilot company create --domain` then need cold-email-grade distillation per row. Notes (§V.13 / §V.14) append-only ∴ wrong shape for idempotently-updatable lead profile. JSONB + Pydantic = in-place updates + mechanical schema check + single-column stale-query (`WHERE profile IS NULL`).
