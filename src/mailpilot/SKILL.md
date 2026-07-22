# mailpilot CLI skill

External LLM-agent reference for the `mailpilot` CLI. Audience: agents that
have `mailpilot` installed as a dependency and need to drive it from a shell.
Scope: command grammar, JSON envelope shape, exit codes, common task recipes,
settings. Out of scope: database schema, internal agent / template wiring.

## Grammar

```
mailpilot <noun> <verb> [args]
mailpilot run | status | config get|set
mailpilot --version | --help | --completion <shell> | --skill | --debug
```

Nouns: `account`, `company`, `contact`, `workflow`, `enrollment`, `task`,
`email`, `activity`, `tag`, `note`, `template`, `db`.

Verbs: `list`, `search`, `view`, `create`, `update`, `disable`, `enable`,
`add`, `remove`, `set`, `merge`, `reply`, `send`, `start`, `stop`, `cancel`,
`retry`, `run`, `sync`, `export`, `import`, `init`, `migrate`, `check`. Not
every verb applies to every noun -- use
`mailpilot <noun> --help` to enumerate. `config` exposes the `get` and `set`
subverbs for reading and writing persistent configuration.

## JSON envelope

Every noun-verb command writes a single JSON document to stdout. Operator
diagnostics go to stderr and never to stdout.

- `list`, `search`, `sync`, `export`, `import`:
  `{"<plural>": [...], "record_count": <int>, "ok": true}`
- `view`, `create`, `update`, `disable`, `enable`, `add`, `remove`,
  `merge`, `reply`, `send`, `start`, `stop`, `cancel`, `retry`, `init`,
  `migrate`, `check`:
  `{"<singular>": {...}, "record_count": 1, "ok": true}`
- error: `{"error": "<code>", "message": "<text>", "ok": false}`

Every `ok: true` envelope carries a top-level integer `record_count`: the
array length for array payloads, `1` for single-object payloads. Error
envelopes omit it.

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

The top-level `--skill`, `--version`, `--help`, `--completion` flags emit
plain text (not JSON) and exit `0`.

## Settings

Persistent config lives in `~/.mailpilot/config.json`. Read and write with:

```
mailpilot config get [KEY]
mailpilot config set KEY VALUE
```

`config get` with no key returns `{"config": {...}, "ok": true}`. With a key
it returns `{"key": ..., "value": ..., "ok": true}` or an `invalid_key`
error envelope.

Every key may also be overridden via an environment variable of the form
`MAILPILOT_<UPPERCASE_KEY>` (for example, `MAILPILOT_DATABASE_URL`,
`MAILPILOT_ANTHROPIC_API_KEY`, `MAILPILOT_RUN_INTERVAL`). A cwd `.env` file
with the same `MAILPILOT_*` keys is auto-read at load (folder-local override).
Priority is constructor kwargs (tests only), then process `MAILPILOT_*` env
vars, then cwd `.env`, then the config file, then field defaults. Missing
`.env` is a no-op. Non-`MAILPILOT_*` keys in `.env` are ignored.

Keys:

- `database_url` -- PostgreSQL DSN. Default `postgresql://localhost/mailpilot`.
- `anthropic_api_key` -- required for agent invocations.
- `anthropic_model` -- e.g. `claude-sonnet-4-6`.
- `anthropic_base_url` -- Anthropic-compatible API endpoint. Default
  `https://api.anthropic.com`; point it at e.g. `https://api.novita.ai/anthropic`
  to route the same call to another vendor.
- `anthropic_thinking` -- workflow-agent extended thinking. Default `adaptive`
  (on); set to empty to turn it off. Classifier never reads this key.
- `anthropic_effort` -- workflow-agent reasoning effort. Default `high`; one of
  `low`, `medium`, `high`, `xhigh`, `max`, or empty to send no effort key.
  `xhigh` needs Opus 4.7 or newer. Classifier never reads this key.
- `anthropic_max_tokens` -- workflow-agent output-token budget. Default `16384`;
  always sent so default-active thinking cannot exhaust the provider-default
  budget before any reply text. Classifier never reads this key.
- `google_application_credentials` -- path to service-account JSON. Optional
  when running on a platform that exposes Application Default Credentials (GCE
  attached service account, GKE Workload Identity, Cloud Run identity); leave
  unset to use ADC. Set explicitly otherwise. Domain-wide delegation works in
  both modes; ADC mode signs JWTs via the IAM Credentials API and requires the
  active service account to hold `roles/iam.serviceAccountTokenCreator` on
  itself.
- `google_pubsub_topic` -- default `mailpilot-topic-dev`.
- `google_pubsub_subscription` -- default `mailpilot-sub-dev`.
- `logfire_token` -- optional. Enables cloud telemetry.
- `logfire_environment` -- `development` or `production`.
- `run_interval` -- fallback poll interval for the sync loop, in seconds.
  Default `60`.
- `max_concurrent_tasks` -- bound on the worker pool that drains the task
  queue. Default `10`.

## Recipes

### Inspect state

```
mailpilot status
mailpilot account list
mailpilot workflow list --account-email <ACCOUNT_REF>
mailpilot enrollment list --workflow-id <ID>
mailpilot task list --status pending
mailpilot email list --account-email <ACCOUNT_REF> --limit 50
```

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
registers missing `--alias` values only — it never wipes profile.

```
mailpilot company create --domain example.com --name "Example Co" --upsert
mailpilot contact create --email lead@example.com \
    --first-name "Ada" --last-name "Lovelace" \
    --company-domain <COMPANY_REF> --title "VP Sales" --upsert
```

Soft-disable a contact (preserves audit history) with:

```
mailpilot contact disable <CONTACT_REF> --reason "left company"
```

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
reason `merged:into <survivor.domain>`. Pass `--move-contacts` to reassign
contacts; omit it to leave contacts on the disabled source. Re-running the
same merge is an ok no-op.

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

`company list` lean rows always project `domain`, `name`, `has_profile`,
`contact_count`, `tags` (assigned names, empty array ok), and
`disabled_reason` (null when enabled; value when the row is returned via
`--include-disabled` or `--status disabled`). One call is enough for tag /
disable / contact-count triage — no per-domain `company view` loop.

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
mailpilot company list --tag acumatica-var --status ready
mailpilot company list --tag acumatica-var --status needs_contacts
mailpilot company list --tag acumatica-var --status needs_profile
mailpilot company list --tag acumatica-var --status disabled
mailpilot company list --include-disabled
mailpilot company list --full
mailpilot company view <DOMAIN_OR_ID>
```

`--full` embeds `profile.summary` only (`null` when the company has no
profile); default list never ships products, target_customers, or sources.
`company view` projects the same `tags` shape as list, plus `aliases`
(sorted alternate domains), full profile, and inlined notes.

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
import` takes a single `.toml` file or a directory of them (`*.toml` glob). Each
file carries `name`, `template`, `goal`, `instructions`, `theme`, with
`instructions` as a TOML multi-line literal string. Available templates:

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

### Enroll a contact

`enrollment add` constructs the binding from `--workflow-id` + `--contact-email`
and returns the freshly-minted scalar `id`; every other verb takes that id
as a single positional argument.

```
mailpilot enrollment add --workflow-id <WID> --contact-email <CONTACT_REF>
mailpilot enrollment run <ENROLLMENT_ID>                            # manual kick
mailpilot enrollment view <ENROLLMENT_ID>
mailpilot enrollment disable <ENROLLMENT_ID> --reason "left company"
mailpilot enrollment enable <ENROLLMENT_ID>
```

Pass `--scheduled-at <ISO>` on `enrollment add` against an outbound workflow
to queue a first-touch send for that time; the run loop dispatches it when
due.

Enrollment status is `active` or `disabled`. `disabled` is the operator halt
(set via `enrollment disable`, reversed via `enrollment enable`); the agent
never re-enables an enrollment. Terminal outcomes (`completed`, `failed`) are
recorded as activity-log entries by the agent, not as enrollment status
changes.

### Send and reply by hand

```
mailpilot email send --account-email <ADDR> --to lead@example.com \
    --subject "Hello" --body "..."
mailpilot email reply --account-email <ADDR> --email-id <EMAIL_ID> --body "..."
```

### Tag, note, and audit

Tags are a controlled vocabulary: `tag create` defines a name first;
`tag add` never auto-creates. `tag add` links one defined tag to one or
more owners (repeatable `--company-domain` or repeatable `--contact-email`,
owner-kind XOR). One owner returns a `tag_assignment` entity; multiple
owners return a `results` batch envelope (already-linked multi rows are
ok skips; exit 0 only when every row is ok).

```
mailpilot tag create vip
mailpilot tag create acumatica-var
mailpilot tag create dynamics-365-var
mailpilot tag add --tag vip --contact-email <ADDR>
mailpilot tag add --tag acumatica-var \
  --company-domain a.com --company-domain b.com
mailpilot tag remove --tag vip --contact-email <ADDR>
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
mailpilot activity list --contact-email <ADDR>
```

A note attaches to exactly one of `contact_id` or `company_id`. Activities
may attach to either, both, or neither.

### Task queue

```
mailpilot task list --status pending
mailpilot task view <TID>
mailpilot task cancel <TID>
mailpilot task retry <TID>     # only on failed or cancelled rows
```

### Run the sync loop

```
mailpilot run
```

Foreground process. Drives Gmail Pub/Sub delivery, runs queued tasks,
invokes the agent on routed inbound mail. Stderr emits one
`HH:MM:SS event=<name> ...` line per operator event. `Ctrl-C` stops cleanly.

## Discovery

Every command supports `--help`. The top-level `--help` lists noun groups;
`mailpilot <noun> --help` lists verbs; `mailpilot <noun> <verb> --help`
lists flags. When uncertain, prefer `--help` over guessing.
