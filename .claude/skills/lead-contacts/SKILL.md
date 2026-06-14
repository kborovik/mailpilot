---
name: lead-contacts
description: |
  Discover decision-maker contacts for already-enriched companies and seed
  verified contact rows. For every company with a profile and fewer than 5
  contacts, dispatch concurrent Sonnet contact-finder agents that run a
  per-company pipeline -- Hunter Domain Search for observed emails, TheOrg
  for org-chart role precision, an agent pick of <=5 decision-makers, Hunter
  Email Finder gap-fill, a single Bouncer batch verification, then seed each
  verified email via `mailpilot contact create` with a Bouncer-scored
  email_confidence. Admit-all: every discovered + verified email becomes a
  contact; low/unknown scores flag risk in the run summary, never drop a row.
  Single free-form invocation -- no sub-commands. Triggers on "/lead-contacts",
  "find contacts", "discover decision-makers for enriched companies".
argument-hint: [<company-id>... | <domain>...] [--limit N]
allowed-tools: Bash(mailpilot company *), Bash(mailpilot contact *), Bash(python3 *), Read, Task, Workflow, AskUserQuestion
model: opus
---

# lead-contacts

Turn enriched company rows into verified decision-maker `contact` rows. For each company w/ a `profile` and `< 5` contacts, a Sonnet `contact-finder` agent discovers people (Hunter + TheOrg), picks `<= 5` decision-makers, finds + verifies their emails (Hunter Email Finder + Bouncer), and seeds them via `mailpilot contact create`.

Spec: §V.96 (discover set + admit-all + idempotent memoization), §V.95 (`contact.title` + `contact.email_confidence` lead-metadata columns), §V.73 (`lead-contacts-find.js` body byte-identical to the skill-mirror snippet below).

Sibling skill `/lead-companies` seeds + enriches the company rows this skill consumes -- run it first.

## Pipeline

One implicit pipeline -- no sub-command dispatch. Classify free-form args, run every applicable stage in order:

| Args look like | Stages |
|---|---|
| bare invocation | stale -> gate -> discover |
| UUID token(s) | stale (scoped to those company rows) -> discover |
| domain/URL token(s) | stale (resolve domain -> company row) -> discover |

Classification rules:

- Arg matches UUID shape -> company id; resolve via `mailpilot company view <ID>`.
- Else -> domain token; resolve via `mailpilot company search "<arg>" --limit 1`.
- A company that is not yet enriched (`profile IS NULL`) or already has `>= 5` contacts -> record `{"skipped": ..., "reason": "no_profile" | "contact_cap"}`, do not dispatch.
- `--limit N` pre-answers the batch gate (no AskUserQuestion fires).

Semantics: idempotent "ensure this company's decision-makers are discovered + verified". A previously seeded email is skipped on re-run (`contact.email` UNIQUE §V.90) -- so re-running the same invocation converges and never re-discovers a known-bad address (§V.96 memoization).

## Fast path (consolidated execution)

Minimize tool calls -- run the two stages each as ONE tool call:

- **Stale query** -> ONE Bash call (`python3` heredoc below) that emits the exact `[{id, domain, name}]` discover set the Workflow consumes -- not a per-company `contact list` loop in skill prose.
- **Discover (>=2 stale rows)** -> ONE Workflow call BY NAME: `Workflow({name: 'lead-contacts-find', args: <stale array>})`, not pasting the inline snippet. Single stale row -> direct `Task(subagent_type="contact-finder", ...)` per §Stage: discover.

So full pipeline = 1 Bash (stale query) + 1 Workflow (discover) + the batch-gate `AskUserQuestion` when `>10` stale and not `--limit`.

## Stage: stale query

Discover set per §V.96 = companies w/ `profile IS NOT NULL` AND contact-count `< 5`. There is no single CLI filter for this join, so cross-reference `company list --has-profile` against a per-company `contact list` count. Count INCLUDES disabled rows (`--include-disabled`) so a company whose 5 discovered addresses later bounce/unsubscribe still drops out of the discover set -- the persisted rows memoize the verdict and re-run does not re-discover them (§V.96).

ONE Bash call (stdout-only JSON; see §Conventions on the always-on stderr operator-log line):

```
python3 - <<'PY'
import json, subprocess

def mp(*args):
    out = subprocess.run(["uv", "run", "mailpilot", *args],
                         capture_output=True, text=True).stdout
    return json.loads(out)

companies = mp("company", "list", "--has-profile", "--limit", "100")["companies"]
stale = []
for company in companies:
    contacts = mp("contact", "list", "--company-id", company["id"],
                  "--include-disabled", "--limit", "5")["contacts"]
    if len(contacts) < 5:
        stale.append({"id": company["id"], "domain": company["domain"],
                      "name": company["name"]})
print(json.dumps(stale))
PY
```

`--limit 5` on the contact list caps the count probe -- any company already at `>= 5` returns 5 rows, fails `< 5`, and is excluded. The printed JSON array is the `stale` value handed to the discover stage.

Scoped runs (UUID/domain args): resolve each arg to its company row first; drop rows that are `profile IS NULL` (`no_profile`) or already at `>= 5` contacts (`contact_cap`); pass only the survivors as the `stale` array.

## Stage: batch gate

- `len(stale) == 0` -> emit `{"seeded": 0, "skipped": 0, "failed": 0, "results": [], "ok": true}` and stop.
- `--limit N` given -> cap to first N, no question.
- `len(stale) > 10` and no `--limit` -> invoke `AskUserQuestion` (sole interaction gate):
  - **question**: `"<N> enriched companies need contacts. How many should the finder process this run?"`
  - **header**: `"Find batch"`
  - **options**:
    - `"First 10 (Recommended)"` -- cap to first 10
    - `"First 25"` -- cap to first 25
    - `"All <N>"` -- every stale row
- Else (1-10 rows) -> proceed w/ all, no question.

## Stage: discover

Single stale row -> direct dispatch, not Workflow overhead: `Task(subagent_type="contact-finder", prompt=<built prompt>)`. Built prompt template:

```
Discover decision-maker contacts for:
  company_id: <ID>
  domain: <company.domain>
  company_name: <company.name>

Follow your system prompt procedure. Return the JSON verdict per spec.
```

`>=2` stale rows -> hand the capped `stale[]` array (from the stale-query Bash call) to the `Workflow` tool as `args` -- an actual JSON value, not a file path or a re-stringified blob. The snippet's `typeof args === 'string'` guard covers the runtime-stringifies case either way.

The discover logic is saved at `.claude/workflows/lead-contacts-find.js` so INVOKE BY NAME -- `Workflow({name: 'lead-contacts-find', args: <array>})` -- do not re-paste the body. The block below is the §V.73 spec-of-record mirror of that saved file (self-contained, runnable as authored); the body below `meta` MUST stay byte-identical to the saved file (the saved `meta` adds registry-only fields -- `whenToUse`, a fuller `description`):

```js
export const meta = {
  name: 'lead-contacts-find',
  description: 'Concurrently discover + verify decision-maker contacts for stale companies',
  phases: [{title: 'Discover', detail: 'contact-finder agents, 3 in flight'}],
}

// `stale` source: the discover set per V.96 -- companies w/ profile IS NOT NULL
// and < 5 existing contacts (count includes disabled rows so memoization holds),
// captured by the skill's stale-query and handed in via Workflow `args`. The
// runtime delivers `args` as a JSON string, so parse it (guard the already-parsed
// case). To paste rows directly instead, replace this line with an inline
// literal: const stale = [{...}, ...].
const stale = typeof args === 'string' ? JSON.parse(args) : args

const CONTACT_RESULT_SCHEMA = {
  type: 'object',
  required: ['company_id', 'domain', 'status'],
  properties: {
    company_id: {type: 'string'},
    domain: {type: 'string'},
    status: {enum: ['seeded', 'skipped', 'failed']},
    contacts_created: {type: 'integer'},
    flagged: {type: 'integer'},
    reason: {type: 'string'},
  },
}

function buildPrompt(c) {
  return [
    'Discover decision-maker contacts for:',
    `  company_id: ${c.id}`,
    `  domain: ${c.domain}`,
    `  company_name: ${c.name}`,
    '',
    'Follow your system prompt procedure. Return the JSON verdict per spec.',
  ].join('\n')
}

phase('Discover')

// Chunk into batches of 3 so at most 3 contact-finder agents run at once -- this
// (not the runtime cap) is what honors the concurrency-3 budget (V.96/V.73). A
// bare parallel(stale.map(...)) would submit all stale.length at once, bounded
// only by the runtime cap min(16, cores-2).
const results = []
for (let i = 0; i < stale.length; i += 3) {
  const batch = stale.slice(i, i + 3)
  const batchResults = await parallel(batch.map(c => () =>
    agent(buildPrompt(c), {
      label: `contacts:${c.domain}`,
      agentType: 'contact-finder',
      schema: CONTACT_RESULT_SCHEMA,
    })
  ))
  results.push(...batchResults)
}
return results.filter(Boolean)
```

The batch loop caps in-flight finders at 3 per `parallel()` call -- the chunking, not any runtime setting, enforces the concurrency-3 budget per §V.73. The Workflow runtime's own per-call cap = `min(16, cores-2)` is a separate, higher ceiling.

## Risk policy (admit-all, §V.96)

- Every discovered + Bouncer-verified email -> seeded as a `contact`. Low Bouncer score never drops a row.
- `email_confidence` <- Bouncer score (0-100); Bouncer `status="unknown"` -> persisted `NULL` (no signal, unbilled) per §V.95.
- Flag (surface in the run summary for operator review) any seeded row where Bouncer `status != "deliverable"` OR score `< 70` OR score is `NULL`. The flag threshold is pinned at 70 -- a high-risk row stays persisted + queryable, never silently dropped.

Why admit-all: dropping a bad/unknown email means re-discovering + re-verifying it next cycle (wasted vendor credits). Persisting it instead memoizes the verdict; idempotent re-run skips it via `contact.email` UNIQUE (§V.90). Review the flagged rows later with `mailpilot contact list --max-email-confidence 70`.

## Vendor keys

The `contact-finder` agent (not this skill, not app code) calls three vendors -- Hunter, TheOrg, Bouncer. Keys live in the operator's `pass` store, env-only per §V.96 (NOT in `settings.py`, NOT in telemetry):

- `pass search THEORG_API_KEY` (and `HUNTER_API_KEY`, `BOUNCER_API_KEY`) locates each entry.
- The agent injects each key inline at call time via `$(pass show <NAME>)` -- never echoed, never persisted by app code.

This skill itself needs no vendor key; it only queries the local DB and dispatches agents.

## Run summary

After all stages, emit one aggregate JSON: `{"seeded": N, "skipped": N, "failed": N, "flagged": N, "results": [...], "ok": true}` -- `seeded` = companies w/ `>=1` new contact, `flagged` = total high-risk rows surfaced for review, `results` = the per-company verdicts. When the batch gate (per §Stage: batch gate) caps below the stale-count -- operator picks `First 10`/`First 25` over a larger N, or `--limit N` < stale-count -- append `"deferred": <stale-count - dispatched>` (the stale companies the stale query found minus the capped count actually dispatched to discover) so the operator sees how many companies were left under the `< 5`-contact threshold for a follow-up run per §V.97; all stale dispatched -> `deferred: 0` or omit the field. A bare `seeded` count is never the sole remainder signal. On scoped runs, include a `"skipped": [{"input": ..., "reason": "no_profile" | "contact_cap" | "not_found"}]` detail so the operator sees which args were not dispatched.

## Conventions

- ASCII-only project artifacts per §C. Math-glyph encoding admitted for skill prose per `/sdd:glyph`.
- All `mailpilot` commands run via `uv run mailpilot`.
- Envelope shape per §V.4: `list|search|...` -> `{"<plural>": [...], "ok": true}`; `view|create|...` -> `{"<singular>": {...}, "ok": true}`. Extract through the wrap.
- Capture stdout only (`2>/dev/null`) before any JSON parse. Every `uv run mailpilot` command writes an always-on operator-log line to stderr (`HH:MM:SS event=... k=v`); the JSON envelope -- including the `{"error":"duplicate_key", ...}` failure case from `contact create` -- is on stdout. Do not `2>&1` into a JSON parser: the leading stderr line corrupts the parse while the command actually succeeded.

## Prerequisites

- `mailpilot` installed locally w/ a working DB (`mailpilot config get database_url`).
- `pass` on PATH with `HUNTER_API_KEY`, `THEORG_API_KEY`, `BOUNCER_API_KEY` entries (the `contact-finder` agent reads them).
- `curl` on PATH (the agent's vendor transport).
- Anthropic credentials reachable (Sonnet finders).
- `>=1` company already enriched (`profile IS NOT NULL`) -- run `/lead-companies` first.

## Rendering

§V.3 JSON-stdout invariant untouched. After `mailpilot contact view <id>` returns the JSON envelope, the skill MAY surface `title` + `email_confidence` inline for operator review. No CLI change.

## OUTPUT -- "Next" block

Heading `## Next`; 1-5 atomic items (one sentence each, no `Reply` prefix); positional dispatch.

Canonical examples -- after a discovery run:

```
## Next

1. mailpilot contact list --max-email-confidence 70 -- review the high-risk flagged rows
2. /lead-contacts -- re-run for companies still under 5 contacts
3. mailpilot contact list --company-id <ID> -- spot-check one company's seeded contacts
```

After a stale-query w/ zero enriched companies:

```
## Next

1. /lead-companies -- enrich company profiles first (lead-contacts needs profile IS NOT NULL)
2. mailpilot company list --has-profile -- confirm which companies are enrichable targets
```

## Why this skill exists

`/lead-companies` distills a company-level profile; cold outreach still needs a PERSON + a verified email. This skill closes company -> people -> verified `contact` row. Vendor split is deliberate: Hunter Domain Search returns observed emails in one call, TheOrg adds decision-maker role precision for thin-web-footprint companies, Bouncer is the sole verification authority. Admit-all + Bouncer-scored `email_confidence` (§V.95/§V.96) keeps risk queryable instead of dropping rows -- so re-runs converge without re-burning vendor credits on known-bad addresses.
