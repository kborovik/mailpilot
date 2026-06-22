---
name: lead-contacts
description: |
  Discover decision-maker contacts for already-enriched companies and seed
  verified contact rows. For every company with a profile and fewer than 5
  contacts, find up to 5 decision-makers and seed each as a verified contact.
  Single free-form invocation -- no sub-commands. Triggers on "/lead-contacts",
  "find contacts", "discover decision-makers for enriched companies".
argument-hint: [<company-id>... | <domain>...] [--limit N]
allowed-tools: Bash(uv run mailpilot company *), Bash(uv run mailpilot contact *), Bash(uv run mailpilot tag *), Bash(python3 *), Read, Task, Workflow, AskUserQuestion
model: opus
---

# lead-contacts

Turn enriched company rows into verified decision-maker `contact` rows. For each company w/ a `profile` and `< 5` contacts, a Sonnet `contact-finder` agent discovers people (Hunter + TheOrg), picks `<= 5` decision-makers, finds + verifies their emails (Hunter Email Finder + Bouncer), and seeds them via `mailpilot contact create`.

Spec: §V.96 (discover set + admit-all + idempotent positive memoization via `contact.email` UNIQUE + dual-class negative memoization on the typed `reason_code`), §V.116 (controlled tag vocabulary -- `no-contacts-found` + `contacts-exhausted` company memoization, `email-unverified` contact flag, `company|contact list --tag`/repeatable `--no-tag` filters), §V.95 (`contact.title` + `contact.email_confidence` lead-metadata columns), §V.73 (`lead-contacts-find.js` body byte-identical to the skill-mirror snippet below).

Sibling skill `/lead-companies` seeds + enriches the company rows this skill consumes -- run it first.

## Pipeline

One implicit pipeline -- no sub-command dispatch. Classify free-form args, run every applicable stage in order:

| Args look like | Stages |
|---|---|
| bare invocation | stale -> gate -> discover |
| UUID token(s) | stale (scoped to those company rows) -> discover |
| domain/URL token(s) | stale (resolve domain -> company row) -> discover |

Classification rules:

- Resolve each arg to its company row via `mailpilot company view <arg>` -- one polymorphic resolve (§V.107): a UUID matches by id, a domain by its natural key (§V.90). No fuzzy `company search`; an unknown arg -> `not_found` envelope.
- A company that is not yet enriched (`profile IS NULL`) or already has `>= 5` contacts -> record `{"skipped": ..., "reason": "no_profile" | "contact_cap"}`, do not dispatch.
- `--limit N` pre-answers the batch gate (no AskUserQuestion fires).

Semantics: idempotent "ensure this company's decision-makers are discovered + verified". A previously seeded email is skipped on re-run (`contact.email` UNIQUE §V.90) -- so re-running the same invocation converges and never re-discovers a known-bad address (§V.96 memoization).

## Fast path (consolidated execution)

Minimize tool calls -- run the two stages each as ONE tool call:

- **Stale query** -> ONE Bash call (`python3` heredoc below) that emits the exact `[{id, domain, name}]` discover set the Workflow consumes -- not a per-company `contact list` loop in skill prose.
- **Discover (>=2 stale rows)** -> ONE Workflow call BY NAME: `Workflow({name: 'lead-contacts-find', args: <stale array>})`, not pasting the inline snippet. Single stale row -> direct `Task(subagent_type="contact-finder", ...)` per §Stage: discover.

So full pipeline = 1 Bash (stale query) + 1 Workflow (discover) + the batch-gate `AskUserQuestion` when `>9` stale and not `--limit`.

## Stage: stale query

Discover set per §V.96 = companies w/ `profile IS NOT NULL` AND contact-count `< 5`, EXCLUDING companies tagged `no-contacts-found` OR `contacts-exhausted` (§V.116). `company list` projects `contact_count` (LEFT JOIN COUNT, INCLUDING disabled contact rows so a company whose addresses later bounce still memoizes out), so `company list --has-profile --max-contacts 4 --no-tag no-contacts-found --no-tag contacts-exhausted` expresses the entire discover set in ONE call -- `--max-contacts 4` is `contact_count <= 4` (i.e. `< 5`), and the two `--no-tag` flags drop the memoized companies (§V.116, the repeatable negated-membership filter -- each `--no-tag` is one intersected predicate). This replaces the old per-company `contact list` N+1 probe (§V.96). A company marked by the negative-verdict memoization stage (tagged `no-contacts-found` for a zero-decision-maker cycle, `contacts-exhausted` for an all-already-seeded one) is excluded by `--no-tag` and so never re-enters the discover set.

The same Bash call FIRST ensures the three vocabulary tags exist -- `no-contacts-found` + `contacts-exhausted` (company memoization) and `email-unverified` (the contact-finder's flag mark, §Risk policy). `tag add` errors `not_found` on an undefined tag (§V.116 never auto-creates), so the skill defines all three up front with idempotent `tag create` (`already_exists` is success) BEFORE any discover dispatch, so the agent's `tag add --tag email-unverified` and the memoization stage's `tag add` always resolve.

ONE Bash call (stdout-only JSON; see §Conventions on the always-on stderr operator-log line):

```
python3 - <<'PY'
import json, subprocess

# Ensure the lead-pipeline vocabulary exists (idempotent; already_exists = ok).
for name in ("no-contacts-found", "contacts-exhausted", "email-unverified"):
    subprocess.run(["uv", "run", "mailpilot", "tag", "create", name],
                   capture_output=True, text=True)

out = subprocess.run(
    ["uv", "run", "mailpilot", "company", "list",
     "--has-profile", "--max-contacts", "4",
     "--no-tag", "no-contacts-found", "--no-tag", "contacts-exhausted",
     "--limit", "100"],
    capture_output=True, text=True).stdout
companies = json.loads(out)["companies"]
stale = [{"id": c["id"], "domain": c["domain"], "name": c["name"]}
         for c in companies]
print(json.dumps(stale))
PY
```

The printed JSON array is the `stale` value handed to the discover stage.

Scoped runs (UUID/domain args): run the same idempotent `tag create` preamble, then resolve each arg to its company row; drop rows that are `profile IS NULL` (`no_profile`), already at `>= 5` contacts (`contact_cap`), or tagged `no-contacts-found` OR `contacts-exhausted` (`memoized`) -- detect those marks with ONE `company list --no-tag no-contacts-found --no-tag contacts-exhausted` lookup (a survivor appears in the result, a memoized company does not); pass only the survivors as the `stale` array.

## Stage: batch gate

Shared gate mechanics (`--limit`, the `>9` `AskUserQuestion`, the First-9 / First-24 / All-N options with the distinct-batch suppression rule, the 1-9 proceed rule) -> see `.claude/skills/lead-companies/references/lead-pipeline-conventions.md` (Batch gate). `<rows>` = the `stale[]` array from the stale query. This skill's per-skill gate parameters:

- empty-set run summary (`len(stale) == 0`): `{"seeded": 0, "skipped": 0, "failed": 0, "results": [], "ok": true}`.
- **question**: `"<N> enriched companies need contacts. How many should the finder process this run?"`
- **header**: `"Find batch"`

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
    reason_code: {enum: ['no_decision_makers', 'all_already_seeded', 'transient']},
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

## Stage: negative-verdict memoization (§V.96/§V.116)

At run end -- after the discover verdicts are collected -- mark every company whose finder added ZERO new contacts, so it drops out of the discover set (the stale query's `--no-tag no-contacts-found --no-tag contacts-exhausted`) and the next run does not re-burn Hunter/TheOrg/Bouncer credits on it (closes §B.95, §B.101). This is the negative mirror of the positive memoization (a persisted `contact` row stops re-discovery via `contact.email` UNIQUE §V.90): a finder that ran and added nobody leaves no new row, so the empty verdict MUST be memoized on the company instead.

Branch on the verdict's typed `reason_code` field, NOT the free-text `reason` prefix -- a prose-prefix match is brittle and was the §B.101 recurrence. Walk each verdict from the discover stage:

- `status == "skipped"` AND `reason_code == "no_decision_makers"` -> the finder found no reachable decision-makers at all. Mark it `no-contacts-found`:
  ```
  uv run mailpilot tag add --tag no-contacts-found --company-domain <company_id>
  ```
- `status == "skipped"` AND `reason_code == "all_already_seeded"` AND `contacts_created == 0` -> the finder re-found people but every discovered email was already a `contact` row, so the run added nothing new. Mark it `contacts-exhausted`:
  ```
  uv run mailpilot tag add --tag contacts-exhausted --company-domain <company_id>
  ```
- `status == "failed"` (`reason_code == "transient"`) -> a TRANSIENT vendor/transport fault. NEVER tag -- the company stays in the discover set and is retried next run. Tagging here would strand a retryable company.
- `status == "seeded"` -> the company gained a contact; do NOT tag.

`--company-domain` resolves the company by id or domain (§V.107). Each tag carries `name` only (§V.116) -- no date payload; the `tag_assignment` row records when. The next stale query (`company list --has-profile --max-contacts 4 --no-tag no-contacts-found --no-tag contacts-exhausted`) then skips both memoized classes.

The trigger is the finder VERDICT `reason_code`, never a blanket `contact_count == 0` sweep: a company sits at zero contacts simply because no finder has run yet. Both marks are reversible -- `mailpilot tag remove --tag no-contacts-found --company-domain <id>` (or `--tag contacts-exhausted`) re-admits the company to the discover set (unlike a bounced contact, a company with no discoverable contacts this cycle may have some next), and `mailpilot company list --tag no-contacts-found` (or `--tag contacts-exhausted`) surfaces the memoized rows for review.

## Risk policy (admit-all, §V.96)

- Every discovered + Bouncer-verified email -> seeded as a `contact`. Low Bouncer score never drops a row.
- `email_confidence` <- Bouncer score (0-100); Bouncer `status="unknown"` -> persisted `NULL` (no signal, unbilled) per §V.95.
- Flag any seeded row where Bouncer `status != "deliverable"` OR score `< 70` OR score is `NULL`. The contact-finder MARKS each flagged row with the `email-unverified` tag at seed time AND counts it in its verdict's `flagged`; the skill aggregates the count into the run summary. The flag threshold is pinned at 70 -- a high-risk row stays persisted, tagged, and queryable, never silently dropped.

Why admit-all: dropping a bad/unknown email means re-discovering + re-verifying it next cycle (wasted vendor credits). Persisting it instead memoizes the verdict; idempotent re-run skips it via `contact.email` UNIQUE (§V.90). Review the flagged rows later with `mailpilot contact list --tag email-unverified` (§V.116) -- an exact membership filter, unlike `--max-email-confidence 70` which also surfaces unrelated NULL-confidence rows never run through Bouncer.

## Vendor keys

The `contact-finder` agent (not this skill, not app code) calls three vendors -- Hunter, TheOrg, Bouncer. Keys are sourced from the repo-root `.env` file, env-only per §V.96 (NOT in `settings.py`, NOT in telemetry):

- `.env` is gitignored and holds one `KEY=value` line each for `HUNTER_API_KEY`, `THEORG_API_KEY`, `BOUNCER_API_KEY`.
- The agent (running from the repo root) reads each key inline at call time by sourcing `.env` in a subshell -- `$(. ./.env; printf '%s' "$HUNTER_API_KEY")` -- never echoed, never persisted by app code.
- Back the file up encrypted with `make env-backup` (gpg -> `.env.gpg`, committable); restore by decrypting `.env.gpg` back to `.env`.

This skill itself needs no vendor key; it only queries the local DB and dispatches agents.

## Run summary

After all stages, emit one aggregate JSON: `{"seeded": N, "skipped": N, "failed": N, "flagged": N, "memoized": N, "results": [...], "ok": true}` -- `seeded` = companies w/ `>=1` new contact, `flagged` = total high-risk rows marked `email-unverified` for review, `memoized` = companies tagged this run by the negative-verdict stage, counting BOTH classes (`no-contacts-found` for a zero-decision-maker cycle plus `contacts-exhausted` for an all-already-seeded one), `results` = the per-company verdicts. When the batch gate (per §Stage: batch gate) caps below the stale-count -- operator picks `First 9`/`First 24` over a larger N, or `--limit N` < stale-count -- append `"deferred": <stale-count - dispatched>` (the stale companies the stale query found minus the capped count actually dispatched to discover) so the operator sees how many companies were left under the `< 5`-contact threshold for a follow-up run per §V.97; all stale dispatched -> `deferred: 0` or omit the field. A bare `seeded` count is never the sole remainder signal. On scoped runs, include a `"skipped": [{"input": ..., "reason": "no_profile" | "contact_cap" | "not_found"}]` detail so the operator sees which args were not dispatched.

## Conventions

Shared across the lead-pipeline siblings (§V.100 single-source) -> see `.claude/skills/lead-companies/references/lead-pipeline-conventions.md` (Conventions: ASCII / `uv run mailpilot` / §V.4 envelope-unwrap / JSON-via-`python3` + `printf` / outcome-split capture -- success envelope on stdout, error envelope on stderr past the always-on operator-log line, per §V.3). The `{"error":"duplicate_key", ...}` failure case here is `contact create`; it exits 1 with the envelope on stderr, not stdout.

## Prerequisites

- `mailpilot` installed locally w/ a working DB (`mailpilot config get database_url`).
- A repo-root `.env` (gitignored) with `HUNTER_API_KEY`, `THEORG_API_KEY`, `BOUNCER_API_KEY` (`KEY=value` per line); the `contact-finder` agent sources it at call time. Confirm with `. ./.env && [ -n "$HUNTER_API_KEY" ] && echo ok` before a run -- a missing key 401s every vendor call. Back up encrypted via `make env-backup` -> `.env.gpg`.
- `curl` on PATH (the agent's vendor transport).
- Anthropic credentials reachable (Sonnet finders).
- `>=1` company already enriched (`profile IS NOT NULL`) -- run `/lead-companies` first.
- Shared Conventions / batch-gate / Next-block prose lives in the sibling skill's `.claude/skills/lead-companies/references/lead-pipeline-conventions.md` (§V.100 single-source); this skill cites that path, so it goes stale if `/lead-companies` is moved or packaged alone.

## Rendering

§V.3 JSON-stdout invariant untouched. After `mailpilot contact view <id>` returns the JSON envelope, the skill MAY surface `title` + `email_confidence` inline for operator review. No CLI change.

## OUTPUT -- "Next" block

Next-block format (heading, 1-5 atomic items, positional dispatch) -> see `.claude/skills/lead-companies/references/lead-pipeline-conventions.md` (OUTPUT -- "Next" block).

Canonical examples -- after a discovery run:

```
## Next

1. mailpilot contact list --tag email-unverified -- review the high-risk flagged rows
2. mailpilot company list --tag no-contacts-found -- review companies memoized with no discoverable decision-makers
3. mailpilot company list --tag contacts-exhausted -- review companies whose discoverable contacts are all already seeded
4. /lead-contacts -- re-run for companies still under 5 contacts
4. mailpilot contact list --company-domain <domain> -- spot-check one company's seeded contacts
```

After a stale-query w/ zero enriched companies:

```
## Next

1. /lead-companies -- enrich company profiles first (lead-contacts needs profile IS NOT NULL)
2. mailpilot company list --has-profile -- confirm which companies are enrichable targets
```

## Why this skill exists

`/lead-companies` distills a company-level profile; cold outreach still needs a PERSON + a verified email. This skill closes company -> people -> verified `contact` row. Vendor split is deliberate: Hunter Domain Search returns observed emails in one call, TheOrg adds decision-maker role precision for thin-web-footprint companies, Bouncer is the sole verification authority. Admit-all + Bouncer-scored `email_confidence` (§V.95/§V.96) keeps risk queryable instead of dropping rows -- so re-runs converge without re-burning vendor credits on known-bad addresses.
