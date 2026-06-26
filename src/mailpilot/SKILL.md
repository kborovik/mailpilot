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
`add`, `remove`, `reply`, `send`, `start`, `stop`, `cancel`, `retry`, `run`,
`sync`, `export`, `import`, `init`, `migrate`, `check`. Not every verb applies
to every noun -- use
`mailpilot <noun> --help` to enumerate. `config` exposes the `get` and `set`
subverbs for reading and writing persistent configuration.

## JSON envelope

Every noun-verb command writes a single JSON document to stdout. Operator
diagnostics go to stderr and never to stdout.

- `list`, `search`, `sync`, `export`, `import`:
  `{"<plural>": [...], "ok": true}`
- `view`, `create`, `update`, `disable`, `enable`, `add`, `remove`,
  `reply`, `send`, `start`, `stop`, `cancel`, `retry`, `init`, `migrate`,
  `check`:
  `{"<singular>": {...}, "ok": true}`
- error: `{"error": "<code>", "message": "<text>", "ok": false}`

Plural keys mirror the noun (`accounts`, `companies`, `contacts`,
`workflows`, `enrollments`, `tasks`, `emails`, `activities`, `tags`, `notes`,
`templates`). Singular keys are the noun itself (`account`, `company`, ...).

Soft-disable verbs such as `contact disable`, `enrollment disable`, and
`tag disable` return the full updated entity under the singular envelope
since the row is retained.

## Exit codes

- `0` -- success. `ok: true` payload.
- `1` -- failure. `ok: false` payload on stdout, plus stderr diagnostic.

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
`MAILPILOT_ANTHROPIC_API_KEY`, `MAILPILOT_RUN_INTERVAL`). Priority is
constructor kwargs (tests only), then `MAILPILOT_*` env vars, then the
config file, then field defaults.

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

```
mailpilot company create --domain example.com --name "Example Co"
mailpilot contact create --email lead@example.com \
    --first-name "Ada" --last-name "Lovelace" --company-domain <COMPANY_REF>
```

Soft-disable a contact (preserves audit history) with:

```
mailpilot contact disable <CONTACT_REF> --reason "left company"
```

### Define a workflow declaratively

Workflow definitions are one TOML file per workflow. Export/import is TOML-only
and idempotent; round-trip is keyed on `(account_id, name)`.

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

```
mailpilot tag create vip
mailpilot tag add --tag vip --contact-email <ADDR>
mailpilot tag remove --tag vip --contact-email <ADDR>
mailpilot tag disable vip --reason "<text>"
mailpilot note add --contact-email <ADDR> --body "Met at conf 2026."
mailpilot activity list --contact-email <ADDR>
```

Tags are a controlled vocabulary: `tag create` defines a name, `tag add`
links it to a contact or company, `tag remove` unlinks, and `tag disable`
retires the name. A note attaches to exactly one of `contact_id` or
`company_id`. Activities may attach to either, both, or neither.

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
