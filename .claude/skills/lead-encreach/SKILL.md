---
name: lead-encreach
description: |
  Create company records from domain names or CSV exports (TheirStack and
  similar), query companies missing profile enrichment, and dispatch
  concurrent Sonnet enricher agents that fetch the company website
  (curl + lynx, Firecrawl fallback) and distill a cold-email-grade JSON
  profile into company.profile. External data sources contribute the
  apex domain only -- all profile fields are agent-discovered.
  Triggers on "/lead-encreach", "enrich companies", "create companies from domains".
argument-hint: [seed <domain>... | seed-file <path> [--column NAME] | list-stale [--limit N] | enrich <id|domain> | enrich-all [--limit N]]
allowed-tools: Bash(mailpilot company *), Bash(curl *), Bash(lynx *), Read, Task, AskUserQuestion
model: opus
---

# lead-encreach

Bootstrap company records from external lead dumps (TheirStack CSV, plain-text domain lists), then enrich each row w/ a cold-email-grade `CompanyProfile` JSON via concurrent Sonnet enricher agents.

Spec: §V.72 (`company.profile` JSONB column ∧ `CompanyProfile` Pydantic model ∧ `company list --no-profile|--has-profile` filter).

## Scope

- Seed: domain → `mailpilot company create` (placeholder name = apex domain).
- List stale: rows w/ `profile IS NULL`.
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

## Sub-commands

### `seed <domain> [<domain>...]`

∀ arg:

1. `apex = extract_apex(domain)` — lowercase, strip leading `www.`, parse via `urllib.parse.urlsplit` if URL-shaped.
2. `resolved = resolve_apex(apex)` — `curl -sLI --max-time 10 -A "Mozilla/5.0" "https://<apex>/"`; tail `^location:` header; re-extract apex from final URL if redirect chain ended elsewhere; else `resolved = apex`.
3. `uv run mailpilot company create --domain <resolved> --name <resolved>` — race-safe per §V.16 (duplicate → envelope `{"error":"duplicate_key", ...}` w/ exit 1).
4. Aggregate per-arg outcome → JSON `{"created": [<id>...], "existing": [<domain>...], "skipped": [{"input": ..., "resolved": ..., "reason": ...}], "ok": true}`.

Collision-on-resolved-apex (resolved-apex already owned by another company row): skip + log per §V.72 design — operator dedups manually.

### `seed-file <path> [--column NAME]`

Source- ∧ format-agnostic ingestion. Apex-domain-only extraction.

1. Read `<path>` via `Read` tool.
2. Format detection from first non-empty line:
   - Contains `,` ∧ ≥1 known header token (`domain`, `website`, `company_url`, `url`) → **CSV mode**.
   - Else → **plain-text mode**.
3. **CSV mode**: identify domain column — explicit `--column NAME` → use it; else auto-detect first match in `[domain, website, company_url, url]`. ∀ row: extract apex from that column's value.
4. **Plain-text mode**: ∀ non-empty non-comment line (skip lines starting w/ `#`): extract apex from the line (raw domain ∨ full URL admitted).
5. ∀ extracted value: same per-row pipeline as `seed` (lowercase, strip `www.`, resolve redirects, `mailpilot company create`).
6. ⊥ pre-populate profile from CSV columns ∨ text-line annotations. All non-domain content discarded — every seeded row lands stale ∴ agent enrichment downstream.
7. Output: aggregate JSON `{"created": N, "existing": N, "skipped": [{"row": N, "input": ..., "resolved": ..., "reason": ...}], "ok": true}`.

### `list-stale [--limit N]`

```
uv run mailpilot company list --no-profile [--limit N]
```

Envelope unwrap → JSON `{"companies": [<CompanySummary w/ has_profile=false>...], "ok": true}`.

### `enrich <id|domain>`

1. If arg looks like a UUID: use directly as `<ID>`. Else: `uv run mailpilot company search "<arg>" --limit 1` → unwrap `.companies[0].id` → `<ID>`. ⊥ match → emit `{"error": "not_found", "input": <arg>, "ok": false}` exit 1.
2. Capture company row: `uv run mailpilot company view <ID>` → `.company`.
3. Dispatch enricher sub-agent: `Task(subagent_type="company-profiler", prompt=<built prompt>)`. Built prompt template:
   ```
   Enrich the company profile for:
     company_id: <ID>
     domain: <company.domain>
     placeholder_name: <company.name>

   Follow your system prompt procedure. Return the JSON verdict per spec.
   ```
4. Return agent's JSON verdict verbatim (envelope inherited from sub-agent).

### `enrich-all [--limit N]`

1. Run `list-stale [--limit N]` → capture `companies[]`.
2. `len(companies) == 0` → emit `{"enriched": 0, "skipped": 0, "failed": 0, "results": [], "ok": true}` ∧ exit.
3. `len(companies) > 10` → invoke `AskUserQuestion`:
   - **question**: `"<N> companies need enrichment. How many should the enricher process this run?"`
   - **header**: `"Enrich batch"`
   - **options**:
     - `"First 10 (Recommended)"` — cap to first 10
     - `"First 25"` — cap to first 25
     - `"All <N>"` — every stale row
4. Dispatch via `Workflow` tool, concurrency = 3:
   ```js
   export const meta = {
     name: 'lead-encreach-enrich-all',
     description: 'Concurrently enrich stale company profiles',
     phases: [{title: 'Enrich', detail: '3 concurrent enricher agents'}],
   }
   phase('Enrich')
   const results = await parallel(stale.map(c => () =>
     agent(buildPrompt(c), {
       label: `enrich:${c.domain}`,
       agentType: 'company-profiler',
       schema: ENRICH_RESULT_SCHEMA,
     })
   ))
   return results.filter(Boolean)
   ```
   `ENRICH_RESULT_SCHEMA`:
   ```json
   {
     "type": "object",
     "required": ["company_id", "domain", "status"],
     "properties": {
       "company_id": {"type": "string"},
       "domain": {"type": "string"},
       "status": {"enum": ["enriched", "skipped", "failed"]},
       "reason": {"type": "string"}
     }
   }
   ```
5. Aggregate → JSON `{"enriched": N, "skipped": N, "failed": N, "results": [...], "ok": true}`.

Workflow per-call concurrency cap = `min(16, cores-2)` ∴ 3 sits well under. Default 3.

## Domain extraction & redirect resolution

```
extract_apex(url_or_domain):
    1. parse → host (urllib.parse.urlsplit)
    2. lowercase
    3. strip leading "www."
    4. return host (no further subdomain stripping)

resolve_apex(initial):
    final_url = curl -sLI --max-time 10 -A "Mozilla/5.0" "https://<initial>/" \
                | grep -i "^location:" | tail -1 | awk '{print $2}'
    if final_url: return extract_apex(final_url)
    else: return initial
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

Canonical examples per sub-command:

```
## Next

1. /lead-encreach list-stale -- inspect newly seeded rows
2. /lead-encreach enrich-all -- dispatch enrichers for stale rows
3. /lead-encreach enrich <id> -- enrich one row before batch
```

After `enrich-all`:

```
## Next

1. /lead-encreach list-stale -- confirm zero rows remain
2. mailpilot company list --has-profile --limit 5 -- spot-check enrichment quality
3. /lead-encreach enrich <id> -- re-run a failed row
```

## Why this skill exists

External lead dumps (TheirStack CSV ∧ similar) seed company rows via `mailpilot company create --domain` then need cold-email-grade distillation per row. Notes (§V.13 / §V.14) append-only ∴ wrong shape for idempotently-updatable lead profile. JSONB + Pydantic = in-place updates + mechanical schema check + single-column stale-query (`WHERE profile IS NULL`).
