---
name: lead-companies
description: |
  Create and enrich company records for cold-email outreach in the mailpilot
  CRM. Use this whenever the user hands over one or more companies to get into
  the CRM and profiled: any request to add, create, seed, or import company
  rows from domains, website URLs, a domains.txt, or a CSV / lead export (e.g.
  TheirStack), and/or to research, profile, or enrich what each company does,
  its products, and who it sells to. Seeding and enrichment run as one
  free-form pass (no sub-commands; the skill classifies the input) and scale
  from a single company to dozens. Also the right call for "enrich the
  companies with no profile yet." Fires on "/lead-companies". Not for finding
  people -- decision-makers, titles, or emails belong to /lead-contacts; nor
  for one-off "just tell me about this site" lookups that save nothing,
  drafting individual emails, or merging/deduping rows and schema questions.
argument-hint: [<domain>... | <file-path>] [--limit N]
allowed-tools: Bash(uv run mailpilot company *), Bash(curl *), Bash(lynx *), Bash(python3 *), Read, Task, Workflow, AskUserQuestion
model: opus
---

# lead-companies

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
  python3 .claude/skills/lead-companies/scripts/seed_companies.py [--dry-run] [--column NAME] <file-or-domain>...
  ```

  Emits ONE JSON object: `{created|would_create, existing, skipped, collapsed, stale, seeded_stale, dry_run, ok}`. Both `stale` and `seeded_stale` are `[{id, domain, name}]` projections ready to hand the enrich Workflow as `args` — no second `company list --no-profile` round trip. `stale` = every `profile IS NULL` row (the enrich set for a **file or bare** run's global stale pass); `seeded_stale` = the subset touched this run (the scoped enrich set for a **domain/URL-token** run, so one seeded domain does not drag the whole backlog into enrichment — the Pipeline table's "stale scoped to those rows"). `--dry-run` resolves + reports w/o DB writes (preview). Mixed file + inline-domain args admitted; UUID args land in `skipped` (enrich-only, not seedable).

- **Enrich (>=2 stale rows)** -> ONE Workflow call BY NAME: `Workflow({name: 'lead-companies-enrich', args: <enrich array>})`, not pasting the inline snippet. The `<enrich array>` is the seed script's `stale` field for a file/bare run, or its `seeded_stale` field for a domain/URL-token run (pick by which Pipeline-table row the args matched). Single stale row -> direct `Task(subagent_type="company-profiler", ...)` per §Stage: enrich.

So full file-arg pipeline = 1 Bash (seed script) + 1 Workflow (enrich) + the batch-gate `AskUserQuestion` when >9 stale and not `--limit` — replacing the ~2N+3 per-row Bash calls. The canonical ingest/seed/domain recipes the script mirrors live in `references/lead-companies-stages.md` (see §Stage recipes); reach for them only when debugging a row the script reported in `skipped`.

## Scope

- Ingest: file -> apex domains (+ optional CSV display name) -> `mailpilot company create` (placeholder name = CSV name when present, else apex domain).
- Seed: domain -> same per-domain create pipeline.
- Stale: rows w/ `profile IS NULL` — internal query step, not operator verb.
- Enrich: dispatch `company-profiler` sub-agent (`.claude/agents/company-profiler.md`, `model: sonnet`) which fetches the site, distills a `CompanyProfile`, persists via `mailpilot company update --profile-json`.

External data sources contribute the apex domain + an optional `company.name` display placeholder (CSV mode only). The profile JSONB body {summary, products, target_customers, timezone, sources} is 100% agent-discovered from the website — do not pre-populate any profile field from CSV columns or text-line annotations (§V.72). The name placeholder is a transient pre-enrichment label; the enricher overwrites it w/ the site-discovered canonical name.

## Conventions

Shared across the lead-pipeline siblings (§V.100 single-source) -> see `.claude/skills/lead-companies/references/lead-pipeline-conventions.md` (Conventions: ASCII / `uv run mailpilot` / §V.4 envelope-unwrap / JSON-via-`python3` + `printf` / stdout-only capture past the always-on stderr operator-log line). The `{"error":"duplicate_key", ...}` failure case here is `company create`.

## Prerequisites

- `mailpilot` installed locally w/ a working DB (`mailpilot config get database_url`).
- `curl` and `lynx` on PATH.
- `mcp__claude_ai_Tavily__tavily_extract` reachable — the enricher's sole fetch fallback (curl + lynx -> Tavily). Matches `company-profiler` agent tool surface; not FireCrawl (token historically dead, agent not granted the tool).
- Anthropic credentials reachable (Sonnet enrichers).

## Stage recipes (canonical / debugging)

The ingest, seed, and domain-resolution mechanics live in `.claude/skills/lead-companies/references/lead-companies-stages.md` — the human-readable mirror of what `scripts/seed_companies.py` runs verbatim (CSV `csv.DictReader` parse + `company create` per §V.74/§V.72, the `%{url_effective}` redirect resolve, and the §V.98 `collapsed` collision rule). The fast path covers all of it in one Bash call; reach for that file only to debug a row the script reported under `skipped`. The live procedure (stale query, batch gate, enrich, run summary) stays inline below.

## Stage: stale query

```
uv run mailpilot company list --no-profile [--limit N]
```

Envelope unwrap -> `companies[]` (`CompanySummary` w/ `has_profile=false`). Domain/UUID-scoped runs: resolve each arg to its row via `uv run mailpilot company view <arg>` — one polymorphic resolve per §V.107 (a domain matches its natural key, a UUID matches by id; no fuzzy `company search`), `not_found` envelope -> record `{"error": "not_found", "input": <arg>}` in the run summary — and enrich only those still stale; already-enriched rows report `already_enriched`.

## Stage: batch gate

Shared gate mechanics (`--limit`, the `>9` `AskUserQuestion`, the First-9 / First-25 / All-N options, the 1-9 proceed rule) -> see `.claude/skills/lead-companies/references/lead-pipeline-conventions.md` (Batch gate). `<rows>` = the `companies[]` from the stale query. This skill's per-skill gate parameters:

- empty-set run summary (`len(companies) == 0`): `{"enriched": 0, "skipped": 0, "failed": 0, "results": [], "ok": true}`.
- **question**: `"<N> companies need enrichment. How many should the enricher process this run?"`
- **header**: `"Enrich batch"`

## Stage: enrich

Single stale row -> direct dispatch, not Workflow overhead: `Task(subagent_type="company-profiler", prompt=<built prompt>)`. Built prompt template:

```
Enrich the company profile for:
  company_id: <ID>
  domain: <company.domain>
  placeholder_name: <company.name>

Follow your system prompt procedure. Return the JSON verdict per spec.
```

>=2 stale rows -> hand the capped `companies[]` to the `Workflow` tool as `args`. On a seed-bearing run the array is already in hand — the seed script's `stale` field (file/bare run) or `seeded_stale` field (domain/URL-token run, scoped to the rows just touched so the backlog is not dragged in). Only a bare invocation with no seed script run needs to capture it fresh from the stale query (stdout only — see §Conventions), projecting just the fields the snippet reads:

```
uv run mailpilot company list --no-profile 2>/dev/null \
  | python3 -c 'import sys, json; rows = json.load(sys.stdin)["companies"]; print(json.dumps([{"id": c["id"], "domain": c["domain"], "name": c["name"]} for c in rows]))'
```

Pass that JSON array as the Workflow `args` value directly — an actual JSON value, not a file path or a re-stringified blob. The snippet's `typeof args === 'string'` guard covers the runtime-stringifies case either way.

The enrich logic is saved at `.claude/workflows/lead-companies-enrich.js` so INVOKE BY NAME — `Workflow({name: 'lead-companies-enrich', args: <array>})` — do not re-paste the body. The block below is the §V.73 spec-of-record mirror of that saved file (self-contained, runnable as authored); the body below `meta` MUST stay byte-identical to the saved file (the saved `meta` adds registry-only fields — `whenToUse`, a fuller `description`):

```js
export const meta = {
  name: 'lead-companies-enrich',
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

After all stages, emit one aggregate JSON: `{"created": N, "existing": N, "enriched": N, "skipped": N, "failed": N, "results": [...], "ok": true}` — omit seed fields on bare invocations, omit enrich fields when 0 stale rows. When the batch gate (per §Stage: batch gate) caps below the stale-count — operator picks `First 9`/`First 25` over a larger N, or `--limit N` < stale-count — append `"deferred": <stale-count - dispatched>` (the stale rows the stale query found minus the capped count actually dispatched to enrich) so the operator sees how many rows were left `profile IS NULL` for a follow-up run per §V.97; all stale dispatched -> `deferred: 0` or omit the field. A bare `created`/`enriched` count is never the sole remainder signal. When >=1 name-divergent collision-on-resolved-apex fired (the §V.98 rule the seed script applies — see §Stage recipes), append `"collapsed": [{"resolved": <apex>, "owner_name": <owner>, "incoming_names": [...]}]` so the operator sees which distinct-entity rows merged onto one company (incoming CSV display name diverging from the owner's name per §V.98) — a bare `existing: N` count hides the merge. Same-name re-seeds stay folded into `existing`.

## Rendering

§V.3 JSON-stdout invariant untouched. After `mailpilot company view <id>` returns the JSON envelope (carrying `profile` inline per §V.8), skill formats `profile` blob as YAML for operator display in chat. No CLI change.

## Profile shape

The enrichment target is the `company.profile JSONB NULL` column, validated server-side against the `CompanyProfile` Pydantic model (`src/mailpilot/models.py`); field semantics, the required/optional split, and the multi-zone `timezone` null rule are authoritative in §V.72, and invalid JSON is rejected with `{"error":"validation_error", ...}` per §V.54. The operator-facing YAML gloss lives in `references/lead-companies-stages.md` (Profile shape); the `company-profiler` agent owns producing it.

## OUTPUT — "Next" block

Next-block format (heading, 1-5 atomic items, positional dispatch) -> see `.claude/skills/lead-companies/references/lead-pipeline-conventions.md` (OUTPUT -- "Next" block).

Canonical examples — after a seeding run:

```
## Next

1. /lead-companies -- enrich the newly seeded stale rows
2. mailpilot company list --no-profile -- inspect stale rows first
3. /lead-companies <domain> -- enrich one row before the batch
```

After an enrichment run:

```
## Next

1. mailpilot company list --no-profile -- confirm zero stale rows remain
2. mailpilot company list --has-profile --limit 5 -- spot-check enrichment quality
3. /lead-companies <domain> -- re-run a failed row
```

## Why this skill exists

External lead dumps (TheirStack CSV and similar) seed company rows via `mailpilot company create --domain` then need cold-email-grade distillation per row. Notes (§V.13 / §V.14) append-only so wrong shape for idempotently-updatable lead profile. JSONB + Pydantic = in-place updates + mechanical schema check + single-column stale-query (`WHERE profile IS NULL`).
