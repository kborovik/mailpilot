# mailpilot CLI skill

External LLM-agent reference for the `mailpilot` CLI. Audience: agents that
have `mailpilot` installed as a dependency and need to drive it from a shell.
Scope: command grammar, JSON envelope shape, exit codes, common task recipes,
settings. Out of scope: database schema, internal agent / template wiring.

## Grammar

```
mailpilot <noun> <verb> [args]
mailpilot run | status | config get|set | show queue
mailpilot --version | --help | --completion <shell> | --debug
```

Top-level `--help` prints this skill body (agent reference). There is no
`--skill` flag. Subcommand and verb `--help` stay Click-rendered for
command-scoped flag discovery.

Nouns: `account`, `company`, `contact`, `workflow`, `enrollment`, `task`,
`email`, `activity`, `tag`, `note`, `template`, `db`.

Verbs: `list`, `search`, `view`, `stats`, `report`, `review`, `status`,
`create`, `update`, `disable`, `enable`, `add`, `remove`, `set`, `merge`,
`reply`, `send`, `start`, `stop`, `cancel`, `retry`, `run`, `sync`,
`export`, `import`, `init`, `migrate`, `check`. Not every verb applies to
every noun -- use `mailpilot <noun> --help` to enumerate. `config` exposes
the `get` and `set` subverbs for reading and writing persistent
configuration.

## JSON envelope

Every noun-verb command writes a single JSON document to stdout. Operator
diagnostics go to stderr and never to stdout. `show queue` is the exception:
it defaults to an ASCII table; pass `--format json` for the `queue` envelope.

- `list`, `search`, `sync`, `export`, `import`:
  `{"<plural>": [...], "record_count": <int>, "ok": true}`
- `view`, `create`, `update`, `disable`, `enable`, `add`, `remove`,
  `merge`, `reply`, `send`, `start`, `stop`, `cancel`, `retry`, `init`,
  `migrate`, `check`:
  `{"<singular>": {...}, "record_count": 1, "ok": true}`
- error: `{"error": "<code>", "message": "<text>", "ok": false}`
- `task cancel` with filters (no TASK_ID):
  `{"task_cancel": {"cancelled_count": N, "ids": [...], "leftover_pending_by_touch": {"1": N}}, "record_count": N, "ok": true}`
  (`record_count` is `cancelled_count`; zero match is an ok no-op)
- `task retry` with filters (no TASK_ID), or `--dry-run` (including with TASK_ID):
  `{"task_retry": {"retried_count": N, "ids": [...], "scheduled_at": "...", "companies": [{"domain": "...", "count": N}], "dry_run": false}, "record_count": N, "ok": true}`
  (`record_count` is `retried_count`; zero match is an ok no-op; `scheduled_at` is the override or null)
- `workflow review`:
  `{"workflow_review": {"since": "...", "until": "...", "reviews": [...]}, "record_count": N, "ok": true}`
  (`record_count` is the number of workflow reviews; `all` is every active)
- `workflow stats` / `workflow status`:
  one slug: `{"workflow_stats": {...}, "record_count": 1, "ok": true}`
  `all`: `{"workflow_stats": [...], "record_count": N, "ok": true}`
  (same shape for `workflow_status`; `all` is every active workflow)

Every `ok: true` envelope carries a top-level integer `record_count`: the
array length for array payloads, `1` for single-object payloads,
`cancelled_count` for filter-mode `task cancel`, `retried_count` for
filter-mode `task retry`, review count for `workflow review`, array length
for `workflow stats all` / `workflow status all`. Error envelopes omit it.

Plural keys mirror the noun (`accounts`, `companies`, `contacts`,
`workflows`, `enrollments`, `tasks`, `emails`, `activities`, `tags`, `notes`,
`templates`). Singular keys are the noun itself (`account`, `company`, ...).

Soft-disable verbs such as `contact disable`, `enrollment disable`, and
`tag disable` return the full updated entity under the singular envelope
since the row is retained.

## Exit codes

- `0` -- success. `ok: true` payload on stdout.
- `1` -- failure. For most commands: `ok: false` envelope on stderr;
  stdout stays empty. For stdin batch mutations (`company disable --stdin`,
  `contact create --stdin`): exit 1 when any row has `status: "error"`,
  but the full `{"results": [...], "ok": true, "record_count": N}`
  envelope still lands on stdout (partial success is reportable).

The top-level `--help`, `--version`, and `--completion` flags emit plain
text (not JSON) and exit `0`. Top-level `--help` is this document.

## Settings

Persistent config lives in the `app_config` database singleton (not a JSON
file). Read and write with:

```
mailpilot config get [KEY]
mailpilot config set KEY VALUE
```

`config get` with no key returns `{"config": {...}, "ok": true}`. With a key
it returns `{"key": ..., "value": ..., "ok": true}` or an `invalid_key`
error envelope.

`database_url` is bootstrap-only: kwargs, then env `MAILPILOT_DATABASE_URL`,
then cwd `.env` (`MAILPILOT_DATABASE_URL=` only), then
`postgresql://localhost/mailpilot`. It is not an `app_config` column.
`config set database_url` returns `invalid_key`. Other `MAILPILOT_*` env
vars and `~/.mailpilot/config.json` are not sources. Missing `.env` is a
no-op.

Keys (row, unless noted):

- `database_url` -- PostgreSQL DSN. Bootstrap only. Default
  `postgresql://localhost/mailpilot`.
- `llm_provider` -- `xai` (default) or `anthropic`.
- `anthropic_api_key` -- required when `llm_provider=anthropic`.
- `anthropic_model` -- e.g. `claude-sonnet-5`.
- `anthropic_base_url` -- Anthropic-compatible API endpoint. Default
  `https://api.anthropic.com`; point it at e.g. `https://api.novita.ai/anthropic`
  to route the same call to another vendor.
- `anthropic_thinking` -- workflow-agent extended thinking. Default `adaptive`
  (on); set to empty to turn it off. Classifier never reads this key.
- `anthropic_effort` -- workflow-agent reasoning effort. Default `high`; one of
  `low`, `medium`, `high`, `xhigh`, `max`, or empty to send no effort key.
  `xhigh` needs Opus 4.7 or newer. Classifier never reads this key.
- `anthropic_max_tokens` -- workflow-agent output-token budget. Default `32768`;
  always sent so default-active thinking cannot exhaust the provider-default
  budget before any reply text. Classifier never reads this key.
- `xai_api_key` -- required when `llm_provider=xai` (the default).
- `xai_model` -- default `grok-4.5`.
- `xai_reasoning_effort` -- `low`, `medium` (default), or `high`.
- `xai_max_tokens` -- workflow-agent output-token budget. Default `32768`.
- `google_application_credentials` -- JSON service-account document (not a
  file path). `config set` VALUE is JSON text; `null` clears to Application
  Default Credentials (GCE, Workload Identity, Cloud Run). Invalid JSON
  returns `validation_error`. Domain-wide delegation works in both modes;
  ADC mode signs JWTs via the IAM Credentials API and requires the active
  service account to hold `roles/iam.serviceAccountTokenCreator` on itself.
- `environment` -- target env. `dev` or `prd`. Default `dev`. Set with
  `config set environment`.
- `logfire_token` -- optional. Enables cloud telemetry.
- `run_interval` -- fallback poll interval for the sync loop, in seconds.
  Default `60`.
- `max_concurrent_tasks` -- bound on the worker pool that drains the task
  queue. Default `10`.

A missing active-provider API key skips that `run` tick (process stays up;
zero due tasks claimed). The error names `mailpilot config set xai_api_key`
or `mailpilot config set anthropic_api_key`.

`config get` and `mailpilot status` also project derived Pub/Sub names
(not settable; `config set` returns `invalid_key`):

- `google_pubsub_topic` -- `mailpilot-topic-{environment}`
- `google_pubsub_subscription` -- `mailpilot-sub-{environment}`

Logfire's deployment environment is mapped internally at configure
(`dev` → `development`, `prd` → `production`). It is not a setting
and is not projected by `config get` or `mailpilot status`. Changing
`environment` or `logfire_token` while `mailpilot run` is up reconfigures
Logfire and restarts the Pub/Sub subscriber on the next tick.

`MAILPILOT_GOOGLE_PUBSUB_TOPIC`, `MAILPILOT_GOOGLE_PUBSUB_SUBSCRIPTION`,
and `MAILPILOT_LOGFIRE_ENVIRONMENT` are not sources. Persist `environment`
only.

## Recipes

### Inspect state

```
mailpilot status
mailpilot account list
mailpilot workflow list --account-email <ACCOUNT_REF>
mailpilot enrollment list --workflow-id <NAME_OR_ID>
mailpilot enrollment list --workflow-id <NAME_OR_ID> --full
mailpilot workflow stats <NAME_OR_ID>
mailpilot task list --workflow-id <NAME_OR_ID> --status pending
mailpilot task stats --workflow-id <NAME_OR_ID>
mailpilot email list --account-email <ACCOUNT_REF> --limit 50
```

### Queue report

Human hub for "what is due?". Default is an ASCII table (not JSON). One row
per workflow (draft, active, paused) with pending-task counts by resolved
touch (`t1`, `t2`, `t3`, `t4p` for touch 4+), `failed` (failed-unsent task
count), `stuck` (stuck-enrollment count), and next send as a full ISO
datetime in `--tz` (offset included) for table and JSON. `next_at` is the
earliest pending send; it is empty when there is no pending task even if
`failed` or `stuck` is greater than 0. Omit `--tz` to
use the host local IANA timezone (`TZ` env or OS zoneinfo); an
unresolvable host zone falls back to UTC. Explicit `--tz` overrides.

`failed` counts tasks with status failed, no send (`email_id` null), an
active enrollment, and no terminal disposition -- including
`attempt_count` 0. Those rows stay in `failed` until `task retry` or the
enrollment is concluded or disabled. `stuck` counts active enrollments
with no next send and no terminal outcome whose latest task failed or
that still await a first touch. This is not the enrollment/report
`--stuck` 24h SLA filter.

`--detail` switches to task grain: pending + failed-unsent + stuck
(oldest pending first, then failed, then stuck). Each row has `kind`
(`pending` / `failed` / `stuck`) and `reason` (empty on pending; stored
`result.reason` on failed; latest failed-task reason on stuck when
present). `--limit` is per kind (default 100 each), so pending filling
100 does not hide failed or stuck. `--detail --failed` lists
failed-unsent only. `--detail --stuck` lists stuck enrollments only
(touch, attempts, next_at, and reason come from the latest failed task
when present). `--overdue` is pending-kind only (`scheduled_at` in the
past). `--overdue`, `--failed`, and `--stuck` are exclusive on
`--detail`; without `--detail` they are ignored.

Detail columns: `workflow_name`, `company_domain`, `contact`, `email`,
`touch`, `attempts`, `next_at`, `kind`, `reason`. `touch` is `T<n>`;
first-reach rows (`enrollment_schedule` with no `touch`) print `T1`.
Table and JSON `next_at` is a full ISO datetime in `--tz` (offset
required). `--workflow-name` accepts name or UUID and matches the
`workflow_name` table/JSON column. Empty prints `(no rows)` and exits 0.
Read-only; no LLM.

For "why is the queue empty / where did scheduled emails go", one call.
Do not follow with `workflow review` for that diagnosis.

For send-window outage classify (failed stack / why failed+stuck), one
call. Do not follow with `task list --status failed`.

```
mailpilot show queue
mailpilot show queue --workflow-name <NAME_OR_ID>
mailpilot show queue --detail
mailpilot show queue --detail --format json
mailpilot show queue --detail --overdue --limit 50
mailpilot show queue --detail --failed
mailpilot show queue --detail --stuck
mailpilot show queue --format json --tz America/Toronto
```

### Campaign enrollment triage

Default `enrollment list` rows stay lean (email, name, status, updated_at).
Pass `--full` for company, touch progress, next send, and disposition:

```
mailpilot enrollment list --workflow-id acumatica-var-outbound --full
mailpilot enrollment list --workflow-id acumatica-var-outbound --full \
  --has-pending-task --touch 2 --sort next_scheduled_at
mailpilot enrollment list --workflow-id acumatica-var-outbound \
  --disposition do_not_contact
mailpilot enrollment list --workflow-id acumatica-var-outbound \
  --disposition contact_later --full
mailpilot enrollment list --workflow-id acumatica-var-outbound --full \
  --disposition do_not_contact \
  --since 2026-08-18T09:36:15-04:00 --until 2026-08-20T09:36:15-04:00
```

`--full` fields: `company_domain`, `company_name`, `emails_sent`, `last_touch`,
`next_scheduled_at`, `next_touch`, `disposition`, `created_at`. Filters
`--has-pending-task` / `--no-pending-task`, `--touch N`, and `--disposition`
(`meeting_booked` | `do_not_contact` | `contact_later`) compose with
workflow/contact/status scopes. `--since` / `--until` filter `updated_at`.
A terminal outcome (including `do_not_contact`) bumps `updated_at`, so a
dated `--full --disposition do_not_contact --since --until` window is
enough to see DNC applied in that window. Do not follow with
`contact view --timeline`. `--touch 1` matches never-sent rows that
have `next_scheduled_at` set (`emails_sent=0`), even when `next_touch` was
null; `--full` projects `next_touch=1` on those rows. Unknown disposition →
`validation_error` with allowed set. Sort keys: `updated_at` (default),
`next_scheduled_at`.

Verify a just-scheduled first-touch batch:

```
mailpilot enrollment list --workflow-id acumatica-outreach --full \
  --has-pending-task --touch 1
```

### Workflow stats (funnel + touch slices)

```
mailpilot workflow stats acumatica-var-outbound
mailpilot workflow stats all
```

Envelope key `workflow_stats`. One slug is an object; `all` is an array of
those objects (`record_count` is the array length). `all` is every active
workflow (same set as `workflow review all`). Zero active is an ok empty
array. Eight enrollment-grain stages (enrolled, sent, bounced, replied,
meeting_booked, contact_later, do_not_contact, active) plus `touches` map
(`"1"/{sent,pending}`, ... for def `touches`), `awaiting_first_touch`,
and `disabled`. Pure SQL; no LLM.

### Campaign stack health (one call)

One call for current funnel plus ops on listed workflows. Do not loop
`workflow stats` and `workflow status` after `workflow list`.

```
mailpilot workflow list --health
```

Each row embeds `funnel` (the same object as `workflow stats`) and `ops`
(`wording`, `run_loop`, `overdue_tasks`, `failed_tasks_24h`). Lean list
(no flag) is unchanged. `--health` composes with existing list filters
(`--status`, `--direction`, `--account-email`).

Fan-out without a list join (every active workflow, same set as
`workflow review all`):

```
mailpilot workflow stats all
mailpilot workflow status all
```

Per-slug verbs stay: `workflow stats <NAME_OR_ID>` and
`workflow status <NAME_OR_ID>` still return a single object. Envelope
keys stay `workflow_stats` / `workflow_status` -- object for a slug,
array for `all`. Zero active is ok (`record_count` 0).

### Workflow report and status

```
mailpilot workflow report <NAME_OR_ID>
mailpilot workflow report <NAME_OR_ID> --stuck --touch 2 --status active
mailpilot workflow report <NAME_OR_ID> --format table
mailpilot workflow report <NAME_OR_ID> --format csv --out /tmp/report.csv
mailpilot workflow status <NAME_OR_ID>
mailpilot workflow status all
```

`workflow report` returns funnel + task aggregate + enrollment matrix under
`workflow_report` (pure SQL). `--stuck` applies stuck heuristics (never-sent
past 24h SLA, bounced without disposition, high-attempt failed tasks).

`workflow status` is ops health (not funnel): wording, run_loop, overdue_tasks,
failed_tasks_24h, enrollments_never_sent, funnel_active pointer. One slug is
an object; `all` is an array of those objects.

Default output is JSON. Optional `--format table|csv|ndjson` on report and
enrollment list; `csv`/`ndjson` prefer `--out` (file + status envelope).

### Stuck and overdue

```
mailpilot task list --overdue
mailpilot enrollment list --workflow-id <NAME_OR_ID> --stuck
mailpilot workflow report <NAME_OR_ID> --stuck
```

`--overdue` = pending tasks with `scheduled_at` in the past. Stuck filters are
read-only; mutate with `task retry` / `enrollment disable`.

### Campaign activity and email timeline

```
mailpilot activity list --workflow-id <NAME_OR_ID> --since 2026-01-01T00:00:00Z
mailpilot email list --workflow-id <NAME_OR_ID> --direction outbound
```

`--workflow-id` on `activity list` and `email list` accepts workflow name or UUID
(same polymorphic resolve as enrollment/task list).

`activity list` requires at least one of `--contact-email`, `--company-domain`,
or `--workflow-id`. `email list` requires at least one scope or filter (no
unbounded full-table dump). Each `email list` / `email search` row carries
`snippet` (first 500 characters of the body; empty ok). Full `body_text`
stays on `email view`.

### Campaign review (one call)

One dated collect for a slug or every active workflow. Do not loop
`workflow stats`, `workflow report`, `enrollment list`, `task list`,
`activity list`, or `email list`. Do not follow with `email view` or
`task view`.

```
mailpilot workflow review <NAME_OR_ID> --since <ISO> --until <ISO>
mailpilot workflow review all --since <ISO> --until <ISO>
```

`--since` and `--until` are required ISO datetimes. Envelope key
`workflow_review`. `record_count` is the number of reviews (1 for a
slug; active-workflow count for `all`). Each review carries funnel,
`task_counts` (`failed` / `overdue` / `pending`), window emails with
`snippet`, window activities including inbound `email_received` with
`snippet`, failed tasks with `contact_email` and `result.reason`, and
every enrollment (not capped below the live enrolled count).

Window email `snippet` is the first 500 characters of the body (empty
ok) and is enough to classify out-of-office, left-company, and
referral. Failed-task `result.reason` is a string or null. Full
`body_text`, `result`, and `context` stay on `email view` / `task view`.

### Provision and migrate the schema

```
mailpilot db init
mailpilot db migrate
mailpilot db check
```

`db init` provisions an empty database from the bundled schema; it refuses a
populated database (no destructive re-init) and is an idempotent no-op once
current. `db migrate` applies pending forward migrations, one transaction each.
`db check` reports the schema verdict and exits non-zero on `pending`/`drift`,
so it doubles as a deploy gate.

### Onboard an account

```
mailpilot account create --email outbound@example.com --display-name "Outbound"
mailpilot account sync --account-email <ACCOUNT_REF>
```

`account sync` performs a one-shot Gmail sync; omit `--account-email` to sync
every account. The long-running `mailpilot run` loop handles ongoing
Pub/Sub deltas.

### Create a contact and company

Preferred agent path uses `--upsert` so a second create on the same natural
key updates allowed fields and exits 0 (no view→create→update loop). Without
`--upsert`, duplicate email returns `duplicate_key` and duplicate company
domain/alias returns `already_exists` (unchanged).

Success envelopes include top-level `created: true` (insert) or
`created: false` (update). Contact upsert updates only flags present on the
call (`title`, `email_confidence`, `company_domain`, `--meta-json`); omitted
fields are not clobbered. Company upsert updates non-empty `--name` and
registers missing `--alias` values only — it never wipes profile unless
profile flags are also passed.

Company create is oneshot: the same invocation accepts profile write flags
(`--profile-file` / `--profile -` / `--profile-json` or field-patch
`--summary` / `--product` / `--source` / `--timezone` /
`--target-customers`) plus repeatable `--tag`. One transaction writes the
company, optional profile, and additive tag links. Invalid profile returns
`validation_error` and writes nothing. Undefined `--tag` returns
`not_found` and never auto-creates the tag. `--tag` is additive (already
linked is an ok skip, not a replace). Success company payload includes
`has_profile` and `tags`. A second identical `--upsert` call exits 0,
updates the profile when flags are present, and does not duplicate tags.

```
mailpilot company create --domain example.com --name "Example Co" --upsert \
  --profile-file /tmp/profile.json \
  --tag sage-var --tag acumatica-var
mailpilot contact create --email lead@example.com \
    --first-name "Ada" --last-name "Lovelace" \
    --company-domain <COMPANY_REF> --title "VP Sales" --upsert
```

Soft-disable a contact (preserves audit history) with ``--reason`` or
``--reason-file`` (exactly one; long reasons from a file avoid shell
quoting). Same XOR on ``company disable`` (single-entity; ``--stdin`` batch
still supplies reason per NDJSON line):

```
mailpilot contact disable <CONTACT_REF> --reason "left company"
mailpilot contact disable <CONTACT_REF> --reason-file /tmp/reason.txt
mailpilot company disable example.com --reason-file /tmp/reason.txt
```

### Contact search (full name and multi-token)

```
mailpilot contact search alice
mailpilot contact search "David Drouin"
mailpilot contact search "VP Engineering"
mailpilot contact list
mailpilot contact view lead@example.com
```

`contact list`, `contact search`, and `contact view` project `tags`
(assigned names, empty array ok) — same shape as company `tags`. One call
is enough to read seats without a separate `tag list`.

Single-token queries substring-match email, first_name, last_name, or title.
A quoted full name matches `first last` in order (e.g. first=David,
last=Drouin). Multi-word queries require every token to match at least one
of those fields (AND), so partial noise does not flood results. Disabled
contacts stay searchable for forensics.

### Unenrolled contacts (one call)

One call for live contacts with no enrollment in any workflow. Do not
list contacts then list enrollments and diff.

`--unenrolled` keeps contacts with zero enrollment rows (any workflow,
any status). `--enrolled` keeps contacts with at least one enrollment
row, including disabled enrollments. The two flags are exclusive (both
together returns `validation_error`). They compose with `--tag` /
`--no-tag` / `--include-disabled`. Default still excludes disabled
contacts. Lean `contacts` envelope is unchanged. `record_count` is the
page length; raise `--limit` when it equals the cap, same as other
lists.

```
mailpilot contact list --unenrolled
mailpilot contact list --unenrolled --tag sales-seat
mailpilot contact list --unenrolled --no-tag skip
mailpilot contact list --unenrolled --include-disabled
mailpilot contact list --enrolled
mailpilot contact list --unenrolled --limit 500
```

### Contact view timeline (dossier)

```
mailpilot contact view lead@example.com
mailpilot contact view lead@example.com --timeline
mailpilot contact view lead@example.com --timeline --limit 20
```

Default `contact view` returns notes only (agent prompt budget). `--timeline`
adds a bounded dossier in one envelope: enrollments (status, disposition,
last/next touch), recent emails, and recent activities. Default 10 rows per
section; `--limit` raises the cap (hard max 50). Works for disabled /
do_not_contact contacts (forensics). Timeline keys are absent without the
flag.

### Contact verification meta (operator-only)

Store Bouncer status, source, and other verification trails as structured
meta — never as a contact note, and never in the workflow agent prompt.

Agent-facing contact fields (prompt allowlist): `name` / first+last,
`title`, `email`, `email_confidence`, company profile, lean notes. Meta is
outside that allowlist.

```
mailpilot contact create --email lead@example.com \
  --email-confidence 98 \
  --meta-json '{"bouncer_status":"deliverable","source":"hunter_pattern"}'
mailpilot contact update lead@example.com \
  --meta-json '{"bouncer_status":"risky","source":"manual"}'
mailpilot contact view lead@example.com
mailpilot contact view lead@example.com --include-meta
```

Default `contact view` (and the agent context builder) omit
`verification_meta`. Pass `--include-meta` for operator audit. Stdin batch
create accepts optional object field `meta` with the same shape.

### Company domain aliases and merge

Register alternate domains when creating a company (repeatable `--alias`).
Every domain string is either a canonical `company.domain` or an alias —
never both, never two owners. View and contact create resolve aliases to the
canonical company. Create of a domain that is already a company domain or an
alias fails with `already_exists` (no silent second firm).

```
mailpilot company create --domain sva.com --name "SVA Consulting" \
  --alias consulting.sva.com
mailpilot company view consulting.sva.com
mailpilot contact create --email lead@consulting.sva.com \
  --company-domain consulting.sva.com
```

`company view` projects `aliases` (sorted lowercased domains, empty array
ok). Lean `company list` omits aliases.

Absorb an absorbed brand into a survivor with `company merge`. The source
domain becomes an alias on the survivor; the source is soft-disabled with
reason `merged:into <survivor.domain>`. Source and survivor may already be
disabled — no prior `company enable` is required. A disabled survivor
keeps its existing `disabled_reason` (merge never re-enables it). Pass
`--move-contacts` to reassign contacts; omit it to leave contacts on the
disabled source. Re-running the same merge is an ok no-op.

```
mailpilot company merge --from nexvue.com --into netatwork.com
mailpilot company merge --from nexvue.com --into netatwork.com --move-contacts
mailpilot company view nexvue.com
```

### Write a company profile

Prefer file or stdin over inline JSON (avoids shell-escape footguns). Profile
objects must include non-empty `summary`, `products`, `target_customers`, and
`sources`; `timezone` is optional. Invalid profiles fail with
`validation_error` and write nothing.

Prefer oneshot `company create --upsert --profile-file --tag ...` when the
company may not exist yet. Use `company update` to patch an existing
company without a create/upsert.

Full replace (exclusive: one of `--profile-file`, `--profile -`, or short
`--profile-json`):

```
mailpilot company update example.com --profile-file /tmp/profile.json
mailpilot company update example.com --profile - < /tmp/profile.json
```

Field patch merges into the existing profile (or builds a base when profile
is null, then validates the full object). Multi flags replace their list:

```
mailpilot company update example.com \
  --summary "ERP reseller focused on mid-market manufacturers." \
  --product "Acumatica" --product "Dynamics BC" \
  --source "https://example.com/" --source "lab5-leads tracker" \
  --timezone America/Chicago \
  --target-customers "Mid-market manufacturers."
```

Full-replace and field-patch flags are exclusive. Keep `--name` available
with either path. Success returns the full company entity including profile.

### Batch disable companies (stdin NDJSON)

One shell call for many soft-disables. Each stdin line is one JSON object
with `domain` and `reason`. `--stdin` is exclusive with a company positional
target and with `--reason`. Re-disabling an already-disabled company is an
ok no-op. Missing domain, missing reason, unknown company, or bad JSON
become per-row errors; the batch never aborts mid-stream without reporting
prior rows.

```
printf '%s\n' \
  '{"domain":"a.com","reason":"absorbed-brand"}' \
  '{"domain":"b.com","reason":"not-a-fit"}' \
  | mailpilot company disable --stdin
```

Envelope (always `ok: true` on stdout):

```json
{
  "results": [
    {"ref": "a.com", "status": "ok"},
    {"ref": "b.com", "status": "error", "error": "not_found", "message": "..."}
  ],
  "record_count": 2,
  "ok": true
}
```

Exit 0 when every row is ok; exit 1 if any row has `status: "error"`
(still emit the full results JSON above).

### Batch create contacts (stdin NDJSON)

Each stdin line is one JSON object with contact create fields. `email` is
required; `first_name`, `last_name`, `company_domain`, `title`,
`email_confidence`, `meta` (JSON object), `note`, and `upsert` (boolean)
are optional. `--stdin` is exclusive with single-entity create options. A
duplicate email natural key is an ok skip unless the line sets
`"upsert": true` (then field-selective update of supplied fields).

```
printf '%s\n' \
  '{"email":"ada@example.com","first_name":"Ada","company_domain":"example.com","upsert":true}' \
  '{"email":"grace@example.com","title":"CTO","meta":{"source":"hunter"},"upsert":true}' \
  | mailpilot contact create --stdin
```

Same `results` / `record_count` / exit policy as batch company disable.

### Company list triage

`company list` and `company search` lean rows always project `domain`,
`name`, `has_profile`, `contact_count`, `tags` (assigned names, empty
array ok), and `disabled_reason` (null when enabled; value when the row is
returned — list needs `--include-disabled` or `--status disabled` for
disabled rows; search returns disabled when the match hits). One call is
enough for tag / disable / contact-count triage — no per-domain
`company view` loop.

Result controls (company list and search only):

| flag | default | notes |
| --- | --- | --- |
| `--limit` | `500` | tag-cohort sized (other nouns keep 100) |
| `--offset` | `0` | page start; `record_count` is page length only |
| `--sort` | `name` | `name` \| `domain` \| `created_at` \| `contact_count` |
| `--desc` | off | descending; default ascending |

Pipeline cohort filter `--status` (AND-composes with `--tag`, `--no-tag`,
`--min-contacts`, `--max-contacts`, `--has-profile`, `--include-disabled`):

| status | rule |
| --- | --- |
| `ready` | has profile, contact_count at least 1, not disabled |
| `needs_contacts` | has profile, contact_count is 0, not disabled |
| `needs_profile` | no profile, not disabled |
| `disabled` | `disabled_reason` is set (forces include of disabled rows) |

```
mailpilot company list --tag acumatica-var
mailpilot company list --tag sage-var --tag linkedin-pass-done
mailpilot company list --tag acumatica-var --status ready
mailpilot company list --tag acumatica-var --status needs_contacts
mailpilot company list --tag acumatica-var --status needs_profile
mailpilot company list --tag acumatica-var --status disabled
mailpilot company list --sort domain --desc --limit 100 --offset 0
mailpilot company search acme --sort contact_count --desc
mailpilot company list --include-disabled
mailpilot company list --full
mailpilot company view <DOMAIN_OR_ID>
```

Repeatable `--tag` is AND: the row must carry every named tag. Repeatable
`--no-tag` is AND: the row must carry none of the named tags. Same AND
rule on `contact list`.

`--full` embeds `profile.summary` only (`null` when the company has no
profile); default list never ships products, target_customers, or sources.
`company view` projects the same `tags` shape as list, plus `aliases`
(sorted alternate domains), full profile, and inlined notes.

Pass `company view --full` to inspect one firm in a single call: the
company envelope embeds `contacts[]` (lean contact fields including
`tags[]`, including disabled), plus the existing company `tags[]` and
`notes[]`. Distinct from `company list --full`. Omit `verification_meta`
unless `--include-meta`. Lean `company view` (no `--full`) is unchanged.

```
mailpilot company view example.com --full
mailpilot company view example.com --full --include-meta
```

### Company tracker export and dry-run import

Export a filterable company cohort as NDJSON for external trackers (not the
`db export` CRM snapshot). Stable keys per line: `domain`, `name`, `tags`,
`has_profile`, `contact_count`, `disabled_reason`. Domains are lowercased;
tags sorted; rows ordered by domain. Pass `--full` to embed the full
`profile` object (or `null`). Filters match `company list`
(`--tag` / `--no-tag` / `--status` / `--include-disabled` / `--has-profile`
/ `--min-contacts` / `--max-contacts`).

```
mailpilot company export --tag acumatica-var --status ready
mailpilot company export --full --out /tmp/companies.jsonl
```

Without `--out`, NDJSON lines stream on stdout (no JSON envelope — pipe-friendly).
With `--out`, the file is written and stdout carries:

```json
{
  "company_export": {
    "path": "/tmp/companies.jsonl",
    "format": "jsonl",
    "record_count": 12
  },
  "record_count": 12,
  "ok": true
}
```

Compare a tracker file to CRM domains with dry-run import (no apply writes):

```
mailpilot company import --from /tmp/companies.jsonl --dry-run
mailpilot company import --from /tmp/companies.jsonl --dry-run \
  --tag acumatica-var --include-disabled
```

`--dry-run` is required. Optional filters scope the CRM side the same way
as export. Envelope:

```json
{
  "company_import_diff": {
    "missing_in_crm": ["newco.com"],
    "missing_profile": ["partial.com"],
    "zero_contacts": ["lonely.com"],
    "disabled": ["gone.com"],
    "extra_in_crm": ["stale.com"]
  },
  "record_count": 5,
  "ok": true
}
```

Buckets: `missing_in_crm` (file not CRM), `missing_profile` (CRM, no
profile), `zero_contacts` (CRM contact_count 0), `disabled` (CRM
disabled_reason set — needs `--include-disabled` or `--status disabled` to
surface), `extra_in_crm` (CRM scope not in file). `record_count` is the
union size of file domains and CRM-scope domains. Missing file →
`not_found`; invalid NDJSON line → `validation_error`.

### Define a workflow declaratively

Workflow definitions are one TOML file per workflow. Export/import is TOML-only
and idempotent; round-trip is keyed on the globally unique `name`.

```
mailpilot workflow export --account-email <ACCOUNT_REF> --out-dir workflows/
mailpilot workflow import --account-email <ACCOUNT_REF> --file workflows/
```

`workflow export` writes one `*.toml` per workflow into `--out-dir` and prints a
JSON status envelope of the paths written (TOML never goes to stdout). `workflow
import` takes a single `.toml` file or a directory of them (`**/*.toml`
recurse). Each file carries `name`, `template`, `goal`, `instructions`, `theme`,
with `instructions` as a TOML multi-line literal string. Available templates:

```
mailpilot template list
mailpilot template view <NAME>
```

`template` is immutable on update -- changing it requires deleting and
recreating the workflow. Import reports a per-row `template_immutable` error
when the value differs and continues with the rest of the batch.

Every import envelope carries top-level integer `applied` and `rejected`
counts beside the `workflows` rows. An import that applies zero rows (every
row rejected, or no `*.toml` found) fails loudly: `import_failed` error
envelope on stderr with the per-row rows inlined, exit 1. A partial import
stays `ok: true` (exit 0) with per-row errors inline, so check `rejected`
before trusting a batch.

### Import campaign workflows (one call)

`--file` recurses `**/*.toml`, so a campaigns monorepo is one import. Do
not run one import per slug directory, and do not follow with
`workflow view` to confirm wording.

```
mailpilot workflow import --account-email <ACCOUNT_REF> --file campaigns/
```

Each applied row carries `action` (`created` / `updated` / `unchanged`),
`in_sync` (live-row wording hash vs the file, same hash as
`workflow check`), `catalog_hash`, `row_hash`, and `changed`. When
`in_sync` is true, `changed` is the fields just written (instruction
excerpts keep the tail so ready-copy is visible). When `in_sync` is
false, `changed` is the remaining def fields that still differ — not
only the fields just written. Do not follow with `workflow check` or
`workflow view`. One import is the verify.

### Check workflow wording (one call)

`--file` lists only workflows whose TOML files sit under that path (file
or directory; directories recurse). Other live workflows do not appear as
orphaned. One call covers a campaigns tree -- do not run one `--file` per
slug directory.

```
mailpilot workflow check --file campaigns/
mailpilot workflow check --file campaigns/<slug>/workflows/
```

Add `--account-email` when you want that account's full envelope (orphaned
rows included):

```
mailpilot workflow check --account-email <ACCOUNT_REF> --file campaigns/
```

### Enroll a contact

`enrollment add` constructs the binding from `--workflow-id` + `--contact-email`
and returns the freshly-minted scalar `id`; every other verb takes that id
as a single positional argument. `--workflow-id` accepts workflow name or UUID.

```
mailpilot enrollment add --workflow-id <WID_OR_NAME> --contact-email <CONTACT_REF>
mailpilot enrollment run <ENROLLMENT_ID>                            # manual kick
mailpilot enrollment view <ENROLLMENT_ID>
mailpilot enrollment disable <ENROLLMENT_ID> --reason "left company"
mailpilot enrollment enable <ENROLLMENT_ID>
```

Pass `--scheduled-at <ISO>` on `enrollment add` against an outbound workflow
to queue a first-touch send for that time; the run loop dispatches it when
due. Re-run the same command on an active never-sent enrollment to move
the pending first-reach time. The task is updated in place; no second
enrollment is created. A re-run at the same instant is a no-op. Later
touches and already-sent enrollments are not moved.

`--contact-email --exclude-peer` is one call. Do not run `enrollment list`
first. Same skip as the tag/file pack: if the contact is already active on
another workflow, nothing is written (`enrollment_batch` `source=contact`,
`count` 0, `excluded.peer` 1). Otherwise the seat enrolls (`count` 1,
`action` created / scheduled_first_send / unchanged). Without
`--exclude-peer` the envelope stays the singular `enrollment` entity.
`--limit` and `--company-atomic` still need `--file` or `--tag`.

```
mailpilot enrollment add --workflow-id <WID_OR_NAME> \
  --contact-email <CONTACT_REF> --scheduled-at <ISO> --exclude-peer
```

Enrollment status is `active` or `disabled`. `disabled` is the operator halt
(set via `enrollment disable`, reversed via `enrollment enable`); the agent
never re-enables an enrollment. Terminal outcomes (`completed`, `failed`) are
recorded as activity-log entries by the agent, not as enrollment status
changes.

### Preview a tag-cohort enrollment (dry-run)

One call for a scheduled-batch preview. `--tag` matches a company tag or a
contact tag (union, unique by contact id) — `sales-seat` and
`leadership-seat` work the same way as a firm tag such as `acumatica-var`.
Dry-run only (no apply writes). Requires `--tag` and `--dry-run` together.
Optional `--min-contacts N` filters companies before expand (company tag)
or the contact's company contact count (contact tag). Disabled companies
are excluded and counted under `excluded.disabled_companies`. Candidates
drop already-enrolled contacts for this workflow, self-loop contacts
(email matches the workflow account), and disabled contacts.

Do not walk `tag list` + `contact list --tag` + `company list --tag` +
`enrollment list` to assemble the same set. This preview is the one-call
recipe: title, company tags, contact tags, email_confidence, and
`peer_workflows` (other workflows with an active enrollment) land on each
row. Rows are grouped by `company_domain` then email.

```
mailpilot enrollment add --workflow-id acumatica-outreach \
  --tag sales-seat --dry-run
mailpilot enrollment add --workflow-id acumatica-outreach \
  --tag acumatica-var --dry-run --min-contacts 1
```

Optional packing flags on dry-run reuse the apply pack (still no writes):
`--limit N`, `--company-atomic`, `--exclude-peer`. Packed contacts stay
in `contacts`; dropped seats increment `excluded.peer` / `excluded.over_limit`.
With `--company-atomic`, each packed contact also carries `scheduled_at`
(resolved first-touch instant, or null when none) and
`aligned_to_existing_t1` (true when snapped to a live same-domain
never-sent first-touch sibling).

```
mailpilot enrollment add --workflow-id acumatica-outreach \
  --tag sales-seat --dry-run --limit 20 --company-atomic --exclude-peer
```

Envelope (no writes):

```json
{
  "enrollment_preview": {
    "workflow": "acumatica-outreach",
    "tag": "sales-seat",
    "count": 2,
    "contacts": [
      {
        "email": "ada@a.com",
        "title": "VP Sales",
        "company_domain": "a.com",
        "company_tags": ["acumatica-var"],
        "contact_tags": ["sales-seat"],
        "email_confidence": 98,
        "peer_workflows": [],
        "scheduled_at": null,
        "aligned_to_existing_t1": false
      },
      {
        "email": "grace@b.com",
        "title": "Director",
        "company_domain": "b.com",
        "company_tags": [],
        "contact_tags": ["sales-seat"],
        "email_confidence": 90,
        "peer_workflows": ["other-outbound"],
        "scheduled_at": null,
        "aligned_to_existing_t1": false
      }
    ],
    "excluded": {
      "disabled_companies": 1,
      "already_enrolled": 0,
      "self_loop": 0,
      "disabled_contacts": 0
    }
  },
  "record_count": 2,
  "ok": true
}
```

Undefined tag → `not_found`. Zero candidates → ok empty (`record_count` 0).
`--tag` without `--dry-run` and without `--scheduled-at` → `validation_error`.
`--dry-run` without `--tag` or `--file` → `validation_error`.

### Enroll a scheduled batch (one call)

After reviewing the dry-run, apply in one call. Do not loop
`enrollment add --contact-email`. Do not list T1 then loop per-email reschedules.
One `--file` or `--tag` apply with `--company-atomic` `--scheduled-at` is enough.

```
mailpilot enrollment add --workflow-id acumatica-outreach \
  --tag sales-seat --scheduled-at 2026-08-17T09:00:00-04:00 \
  --limit 20 --company-atomic --exclude-peer
mailpilot enrollment add --workflow-id acumatica-outreach \
  --file /tmp/emails.json --scheduled-at 2026-08-17T09:00:00-04:00 \
  --limit 20 --company-atomic
```

`--file` is a JSON array of email strings or `{email, scheduled_at}`
objects. Per-row `scheduled_at` overrides the flag. `--company-atomic`
keeps every included seat on a domain on the same calendar day,
including live never-sent first-touch siblings already on this workflow:
new seats inherit that day and clock (over `--scheduled-at` and per-row
times). Live siblings already split across days is `validation_error`
(zero writes). May exceed `--limit` to fit the last company. `--limit`
without `--company-atomic` is a hard cap (first N by company_domain then
email). `--exclude-peer` drops contacts already active in another
workflow. Tag apply never restamps seats already enrolled in this
workflow. File apply last-write-wins an existing never-sent first-reach
unless `--company-atomic` snaps it to the sibling instant.

Envelope:

```json
{
  "enrollment_batch": {
    "workflow": "acumatica-outreach",
    "scheduled_at": "2026-08-17T09:00:00-04:00",
    "source": "tag",
    "tag": "sales-seat",
    "limit": 20,
    "company_atomic": true,
    "count": 2,
    "enrolled": [
      {
        "email": "ada@a.com",
        "company_domain": "a.com",
        "enrollment_id": "...",
        "scheduled_at": "2026-08-17T09:00:00-04:00",
        "action": "created"
      }
    ],
    "excluded": {
      "disabled_companies": 0,
      "already_enrolled": 0,
      "self_loop": 0,
      "disabled_contacts": 0,
      "peer": 0,
      "over_limit": 0,
      "not_found": 0
    }
  },
  "record_count": 2,
  "ok": true
}
```

`action` is `created`, `scheduled_first_send`, or `unchanged`. Missing
`--file` path or unknown file email → `not_found` (zero writes). Bad
JSON → `validation_error`. `--limit` below 1 → `validation_error`.

### Send and reply by hand

`--workflow-id` on `email send` and `email reply` accepts workflow name or UUID
(same polymorphic resolve as enrollment/task list). Unknown UUID-shaped ids return `not_found`.

```
mailpilot email send --account-email <ADDR> --to lead@example.com \
    --subject "Hello" --body "..." --workflow-id <NAME_OR_ID>
mailpilot email reply --account-email <ADDR> --email-id <EMAIL_ID> --body "..." \
    --workflow-id <NAME_OR_ID>
```

### Tag, note, and audit

Tags are a controlled vocabulary: `tag create` defines a name first;
`tag add` never auto-creates. `tag add` and `tag remove` share the same
owner flags: one defined tag to one or more owners (repeatable
`--company-domain` or repeatable `--contact-email`, owner-kind XOR). One
owner returns a `tag_assignment` entity; multiple owners return a
`results` batch envelope. Already-linked multi `add` rows and
already-unlinked multi `remove` rows are ok skips; exit 0 only when every
row is ok.

```
mailpilot tag create vip
mailpilot tag create acumatica-var
mailpilot tag create dynamics-365-var
mailpilot tag add --tag vip --contact-email <ADDR>
mailpilot tag add --tag acumatica-var \
  --company-domain a.com --company-domain b.com
mailpilot tag remove --tag vip --contact-email <ADDR>
mailpilot tag remove --tag acumatica-var \
  --company-domain a.com --company-domain b.com
mailpilot tag disable vip --reason "<text>"
```

Replace a company's full tag set with `tag set` (empty `--tags` clears):

```
mailpilot tag set --company-domain a.com \
  --tags acumatica-var,dynamics-365-var
mailpilot tag set --company-domain a.com --tags ""
mailpilot company view a.com
```

Company success for `tag set` returns the company entity including final
`tags` (same shape as `company list` / `company view`). Undefined names
error `not_found` with zero writes.

```
mailpilot note add --contact-email <ADDR> --body "Met at conf 2026."
mailpilot note list --company-domain example.com
mailpilot note list --contact-email <ADDR>
mailpilot note remove <NOTE_ID>
mailpilot note remove --company-domain example.com --yes
mailpilot note remove --contact-email <ADDR> --yes
mailpilot activity list --contact-email <ADDR>
```

A note attaches to exactly one of `contact_id` or `company_id`. Activities
may attach to either, both, or neither.

`note list` is the note surface for a company or contact (no full entity-view
scrape). `note remove <NOTE_ID>` deletes one note. Owner bulk remove needs
exactly one of `--company-domain` / `--contact-email` plus required `--yes`
(confirmation gate). Bulk success envelope:

```json
{
  "notes_removed": {
    "owner": {"company_domain": "example.com"},
    "removed_count": 2,
    "note_ids": ["...", "..."]
  },
  "record_count": 2,
  "ok": true
}
```

Zero notes is an ok no-op (`record_count` 0). Deletes write no activity;
prior `note_added` rows stay as the audit trail. Operator-only — never an
agent tool.

### Task queue

`--workflow-id` on `task list` and `task stats` accepts workflow name or UUID
(same polymorphic resolve as enrollment/activity list).

```
mailpilot task list --status pending
mailpilot task list --workflow-id <NAME_OR_ID>
mailpilot task list --workflow-id <NAME_OR_ID> --overdue
mailpilot task list --workflow-id <NAME_OR_ID> --status failed
mailpilot task list --workflow-id <NAME_OR_ID> --touch 2
mailpilot task list --workflow-id <NAME_OR_ID> --touch 2 --touch 3
mailpilot task stats --workflow-id <NAME_OR_ID>
mailpilot task stats --workflow-id <NAME_OR_ID> --trigger enrollment_schedule
mailpilot task view <TID>
mailpilot task cancel <TID>
mailpilot task cancel --workflow-id <NAME_OR_ID> --touch 2
mailpilot task retry <TID>
mailpilot task retry <TID> --scheduled-at 2026-08-17T13:01:49-04:00
mailpilot task retry --status failed --dry-run
mailpilot task retry --status failed
mailpilot task retry --workflow-id <NAME_OR_ID> --touch 1 --dry-run
mailpilot task retry --workflow-id <NAME_OR_ID> --status failed \
  --touch 1 --scheduled-at 2026-08-24T09:00:00-04:00
```

Failed `task list` rows carry `result.reason` (string or null). Do not
loop `task view` to classify fail cause; full `result` and `context`
stay on view.

`task list --touch` is repeatable and reads resolved `context.touch`
(N or `T<n>`), never description text.

### Cancel pending tasks (one call)

Do not list then loop `task cancel <id>`. One call cancels every matching
pending row and returns the join.

Filter-mode needs at least one of `--touch`, `--workflow-id`,
`--contact-email`, `--trigger`, or `--overdue`. `--status` defaults to
pending; any other status is rejected. TASK_ID and filters are exclusive.

```
mailpilot task cancel --workflow-id <NAME_OR_ID> --touch 3
mailpilot task cancel --workflow-id <NAME_OR_ID> --touch 2 --touch 3
mailpilot task cancel --workflow-id <NAME_OR_ID> --overdue
```

Envelope:

```json
{
  "task_cancel": {
    "cancelled_count": 2,
    "ids": ["...", "..."],
    "leftover_pending_by_touch": {"1": 4}
  },
  "record_count": 2,
  "ok": true
}
```

Zero match is an ok no-op (`cancelled_count` 0).
`leftover_pending_by_touch` is remaining pending in the same scope,
grouped by resolved touch, after the cancel. `task cancel <TID>` still
returns the single task entity.

### Retry failed tasks (one call)

Do not list then loop `task retry <id>`. Do not retry once per
workflow. One call retries every matching failed (default) or cancelled
row and returns the join.

Filter-mode needs at least one of `--touch`, `--workflow-id`,
`--contact-email`, `--trigger`, or `--status`. `--status failed` or
`--status cancelled` with no other scope retries every matching row.
`--status` defaults to failed; only failed and cancelled are allowed.
TASK_ID and filters are exclusive. `--scheduled-at` applies the same
instant to every selected row. Omit it to keep a still-future stored
time, or now when the stored time is past. `--dry-run` previews ids
and companies with no writes. Touch is read from task context,
never description text. Distinct from `task cancel`.

```
mailpilot task retry --status failed --dry-run
mailpilot task retry --status failed
mailpilot task retry --status failed --scheduled-at 2026-08-24T09:00:00-04:00
mailpilot task retry --status cancelled
mailpilot task retry --workflow-id <NAME_OR_ID> --touch 1 --dry-run
mailpilot task retry --workflow-id <NAME_OR_ID> --status failed \
  --touch 1 --scheduled-at 2026-08-24T09:00:00-04:00
mailpilot task retry --workflow-id <NAME_OR_ID> --touch 2 --touch 3
mailpilot task retry <TID> --dry-run
```

Envelope:

```json
{
  "task_retry": {
    "retried_count": 2,
    "ids": ["...", "..."],
    "scheduled_at": "2026-08-24T09:00:00-04:00",
    "companies": [{"domain": "a.com", "count": 2}],
    "dry_run": false
  },
  "record_count": 2,
  "ok": true
}
```

Zero match is an ok no-op (`retried_count` 0). `task retry <TID>` without
`--dry-run` still returns the single task entity.

`task retry <TID>` is one call for a failed or cancelled row. Omit
`--scheduled-at` to keep a still-future stored time (resume T2 on the
original date after an out-of-office cancel). Pass `--scheduled-at` to
park it on a later instant. A past override is rejected. Then confirm
with `enrollment list --full --has-pending-task --touch 2`.

### Run the sync loop

```
mailpilot run
```

Foreground process. Drives Gmail Pub/Sub delivery, runs queued tasks,
invokes the agent on routed inbound mail. Stderr emits one
`HH:MM:SS event=<name> ...` line per operator event. `Ctrl-C` stops cleanly.

## Discovery

Top-level `mailpilot --help` prints this skill document (grammar, envelope,
exit codes, settings, recipes). Subcommand help stays Click-rendered:
`mailpilot <noun> --help` lists verbs; `mailpilot <noun> <verb> --help`
lists flags. When uncertain, prefer `--help` over guessing.
