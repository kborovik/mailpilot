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
`email`, `activity`, `tag`, `note`, `template`.

Verbs: `list`, `search`, `view`, `create`, `update`, `disable`, `add`,
`remove`, `reply`, `send`, `start`, `stop`, `cancel`, `retry`, `run`, `sync`,
`export`, `import`. Not every verb applies to every noun -- use
`mailpilot <noun> --help` to enumerate. `config` exposes the `get` and `set`
subverbs for reading and writing persistent configuration.

## JSON envelope

Every noun-verb command writes a single JSON document to stdout. Operator
diagnostics go to stderr and never to stdout.

- `list`, `search`, `sync`, `export`, `import`:
  `{"<plural>": [...], "ok": true}`
- `view`, `create`, `update`, `disable`, `add`, `remove`, `reply`, `send`,
  `start`, `stop`, `cancel`, `retry`: `{"<singular>": {...}, "ok": true}`
- error: `{"error": "<code>", "message": "<text>", "ok": false}`

Plural keys mirror the noun (`accounts`, `companies`, `contacts`,
`workflows`, `enrollments`, `tasks`, `emails`, `activities`, `tags`, `notes`,
`templates`). Singular keys are the noun itself (`account`, `company`, ...).

`remove` returns the removed entity's composite-key fields (or natural
identifier) under the singular envelope -- mirror of `add`. Soft-disable
verbs such as `contact disable` return the full updated entity since the
row is retained.

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
mailpilot workflow list --account-id <ID>
mailpilot enrollment list --workflow-id <ID>
mailpilot task list --status pending
mailpilot email list --account-id <ID> --limit 50
```

### Onboard an account

```
mailpilot account create --email outbound@example.com --display-name "Outbound"
mailpilot account sync --account-id <ACCOUNT_ID>
```

`account sync` performs a one-shot Gmail sync; omit `--account-id` to sync
every account. The long-running `mailpilot run` loop handles ongoing
Pub/Sub deltas.

### Create a contact and company

```
mailpilot company create --domain example.com --name "Example Co"
mailpilot contact create --account-id <ID> --email lead@example.com \
    --first-name "Ada" --last-name "Lovelace" --company-id <COMPANY_ID>
```

Soft-disable a contact (preserves audit history) with:

```
mailpilot contact disable --contact-id <CID> --reason "left company"
```

### Define a workflow declaratively

Workflows are reproducible via export/import. Round-trip is keyed on
`(account_id, name)` and is idempotent on unchanged input.

```
mailpilot workflow export --account-id <ID> > workflows.json
mailpilot workflow import --account-id <ID> --file workflows.json
# or via stdin:
cat workflows.json | mailpilot workflow import --account-id <ID>
```

Each row in the JSON array carries `name`, `template`, `objective`,
`instructions`, `theme`. Available templates:

```
mailpilot template list
mailpilot template view <NAME>
```

`template` is immutable on update -- changing it requires deleting and
recreating the workflow. Import reports a per-row `template_immutable` error
when the value differs and continues with the rest of the batch.

### Enroll a contact

`enrollment add` constructs the binding from `--workflow-id` + `--contact-id`
and returns the freshly-minted scalar `id`; every other verb takes that id
as a single positional argument.

```
mailpilot enrollment add --workflow-id <WID> --contact-id <CID>
mailpilot enrollment run <ENROLLMENT_ID>                            # manual kick
mailpilot enrollment view <ENROLLMENT_ID>
mailpilot enrollment update <ENROLLMENT_ID> --status paused
mailpilot enrollment remove <ENROLLMENT_ID>
```

Pass `--scheduled-at <ISO>` on `enrollment add` against an outbound workflow
to queue a first-touch send for that time; the run loop dispatches it when
due.

Enrollment status is `active` or `paused`. Terminal outcomes
(`completed`, `failed`) are recorded as activity-log entries by the agent,
not as enrollment status changes.

### Send and reply by hand

```
mailpilot email send --account-id <ID> --to lead@example.com \
    --subject "Hello" --body "..."
mailpilot email reply --account-id <ID> --email-id <EMAIL_ID> --body "..."
```

### Tag, note, and audit

```
mailpilot tag add --contact-id <CID> --name vip
mailpilot note add --contact-id <CID> --body "Met at conf 2026."
mailpilot activity list --contact-id <CID>
```

Tags and notes attach to exactly one of `contact_id` or `company_id`.
Activities may attach to either, both, or neither.

### Task queue

```
mailpilot task list --status pending
mailpilot task view --task-id <TID>
mailpilot task cancel --task-id <TID>
mailpilot task retry --task-id <TID>     # only on failed or cancelled rows
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
