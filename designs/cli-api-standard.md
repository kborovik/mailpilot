# CLI API standard

## Problem

The CLI grammar is half-codified. §I.cli fixes the `mailpilot <noun> <verb> [args]` shape, the JSON envelope, and the closed noun and verb sets (§V.1-5). Below that line, per-entity conventions drifted — verb choice, target arguments, filter flags, and error codes each grew ad-hoc. Observed drift in `cli.py`:

Verb and argument drift:

- `add` names creation for tag, note, and enrollment; `create` names it everywhere else, with no written rule (`cli.py:1799`, `cli.py:2060`, `cli.py:3053` versus `cli.py:617`).
- `task retry` takes a `--task-id` option (`cli.py:3500`), while every other single-entity verb takes a positional `<id>` argument.
- `activity create` requires a parent (`--contact-id` or `--company-id`) yet uses `create`, not the parent-scoped `add` (`cli.py:1693`).

Filter-flag drift:

- `--since` bounds a different column per entity (`created_at`; `updated_at` enrollment `cli.py:3313`; `scheduled_at` task `cli.py:3432`). Its `help=` text names the column inconsistently, and no `--until` mirrors it.
- The direction axis uses two flag names for one inbound-outbound axis: `--direction` (email `cli.py:1431`, template) versus `--type` mapping to `workflow_type` (workflow `cli.py:2455`), per §V.46.
- `--route-method` (`cli.py:1448`) is a free string despite a closed 7-value enum (§V.20, §I, §V.88). A typo produces a silent `[]`. Peer enum filters use `click.Choice`.
- Text-match modes are inconsistent and undocumented: `--company-domain` exact (`cli.py:1211`), `--title` case-insensitive substring (`cli.py:1216`), `--from` and `--to` exact-email.
- The presence filter is ad-hoc: company exposes `--has-profile` and `--no-profile` as a two-flag tri-state with a manual XOR and `validation_error` (`cli.py:768-814`).
- `--include-disabled` is consistent across company, contact, and tag (§V.10, §V.114, §V.96) but uncodified.
- `--limit` is repeated 16 times verbatim. No `--offset` or `--order-by` exists.

Error-code drift:

- Domain error codes are assigned inline per command (`not_found`, `invalid_state`, `missing_filter`, `send_failed`, and more) with no closed vocabulary. §V.54 fixes only the psycopg-constraint and Pydantic mappings.

## Proposal

A **CLI API standard** fixes one convention per axis — verbs, target identification, filter flags, and output — so every current and future command draws from a shared vocabulary. Each axis is realized as a rule plus, where it pays, a reusable Click decorator, so the code enforces the convention. The standard extends §I.cli and §V.1-5 rather than replacing them; it documents the conventions those rows leave implicit and resolves the drift above.

## Command grammar

Every command is `mailpilot <noun> <verb> [args]` (§I.cli). The noun set is closed (`account company contact email activity tag note workflow enrollment template task db`); the verb set is closed (listed under Verbs). A new noun or verb is a spec change, not a local invention. Three top-level commands stand outside the noun-verb grid: `run` (the sync and task loop), `status`, and `config get` / `config set`. Global options are `--version`, `--debug`, `--completion`, and `--skill`.

## Verbs

Every command picks one verb from the closed set (§I.cli). The verbs group by role:

- **Read** — `list`, `search`, `view`. `view` is the single-entity read for entity nouns; `config get` and `config set` are the one exception, the top-level settings accessor.
- **Mutate** — `create`, `add`, `remove`, `update`, `disable`. `create` brings a new entity into existence; `add` and `remove` link and unlink an entity to an owner; `disable` soft-retires an entity. No `delete` verb exists and no command hard-deletes a row (§V.10, §V.114).
- **Lifecycle** — `start`, `stop`, `cancel`, `retry`, and enrollment `run`. These move an entity between states or invoke it. They are not `update --status`, because each carries its own state guard (§V.109, `invalid_state`).
- **Action** — `send`, `reply`, `sync`. These perform an external Gmail effect and return the affected row.
- **Schema and catalog** — `init`, `migrate`, `check` under `db`; `export` and `import` for round-trips.

**create, add, and remove.** `create` brings a new entity into existence from its own fields, so its subject is the entity (account, company, contact, workflow, and a `tag` vocabulary entry). `add` links an existing entity to an owner, so its subject is the link, not a new standalone thing (`tag add` links a tag to a company or contact; `note add` and `activity add` attach a row to an owner; `enrollment add` links a contact to a workflow). `remove` is the inverse of `add`, unlinking without retiring either side. The rule is currently unwritten and has one outlier: `activity create` attaches an event to an owner yet uses `create`. The standard reconciles it to `activity add`.

## Target identification

Every entity reference — a single-entity verb's target, a Scope or owner option, an account reference — names an entity by its **natural key** where §V.90 defines one, by **UUID** otherwise. Resolution is **polymorphic**: a value matching the UUIDv7 shape resolves by id, any other value resolves by the natural key. The two never collide — a UUIDv7 is `8-4-4-4-12` hex, a domain carries dots, an email carries an at-sign — so the form is unambiguous. The natural key is the canonical, documented form, shown on every map line and in `--help`; the UUID stays accepted so a foreign key or the `id` field from one command's output feeds straight into the next. A flag named for a key (`--company-domain`) therefore also accepts a UUID. Resolution runs the existing helpers (`get_account_by_email`, `get_company_by_domain`, `get_contact_by_email`) case-insensitively per the natural key (§V.90); an unknown key returns `not_found` with no write (§V.94).

- **Keyed entities** carry one natural key: account (email), company (domain), contact (email), tag (name). They are addressed by it.
- **Keyless entities** — email, note, task, workflow, enrollment — have no single natural key (email's `gmail_message_id` is nullable; workflow and enrollment keys are composite), so they are addressed by UUID alone.

- **Single entity** — a positional argument names the target, carrying the natural key or the UUID: `company view <domain>`, `contact view <email>`, `task cancel <task_id>`. The target is never a `--<entity>-id` option. This fixes `task retry`, which takes `--task-id` today.
- **Search** — a positional `<query>` argument plus `--limit`: `contact search <query>`.
- **Filters** — options only, drawn from the six families below.
- **Parent scope** — `add` and parent-scoped reads name the owner with a Scope option named for the owner's natural key (`--contact-email` or `--company-domain`, exactly one where the entity allows either).
- **Account reference** — account-requiring commands take one `--account-email`; the §V.107 pair collapses, since a single polymorphic flag resolves an email or a UUID. `account sync` makes it optional — at most one when given, all accounts when omitted — since sync defaults to the full account set.

## Filter flags (six families)

Every `list` filter flag belongs to exactly one of six families, each with a fixed naming and semantics rule.

1. **Scope** — named for the parent's natural key where it has one (`--company-domain`, `--contact-email`), `--<singular-noun>-id <UUID>` for a keyless parent (`--workflow-id`). Narrow to children of a parent by its foreign key (FK). The value resolves polymorphically (natural key or UUID) per Target identification; validation runs the matching `get_<entity>` helper and returns a `not_found` envelope when the parent is absent.
2. **Enum** — `--<axis> <value>` with `type=click.Choice(<schema enum>)`. Any column whose value set is a schema CHECK enum must be a `Choice` mirroring it, never a free string (§V.88). The `--status` value set is entity-local. Fixes `--route-method`.
3. **Range** — `--min-<field>` and `--max-<field>`. Numeric or ordinal. Both bounds are offered, both inclusive and composable. NULL-inclusive where the column is nullable and meaningful (§V.95).
4. **Presence** — `--has-<field>/--no-<field>`, a single tri-state with `default=None`. Marks a nullable column as has or hasn't. One Click option replaces the two-flag form and its manual XOR. The two sides need no shared stem: Click derives the parameter name (`has_<field>`) from the positive side, so `--no-<field>` is a clean off-switch with no `--no-has-<field>` artifact.
5. **Text-match** — field-named (`--title`, `--from`, `--to`). Exact match only on `list`. Case-folding follows the column's natural-key semantics (§V.90 — email is case-insensitive). Substring or fuzzy match belongs to the `search` verb, never a `list` filter. Note `--company-domain` is no longer a Text-match member: it is the Scope reference to a company (family 1), so an unknown domain errors `not_found` rather than returning an empty list.
6. **Lifecycle** — `--include-disabled` (is_flag, default False) plus `--since` and `--until <ISO>`. A soft-deletable entity hides disabled rows by default and opts in via `--include-disabled` (§V.10, §V.114). The time window is a closed interval `[since, until]`, both bounds inclusive, mirroring Range, over one declared column per entity (`created_at` default; `updated_at` enrollment; `scheduled_at` task). The `help=` text always names the bound column. Every list command offers `--since` and `--until`.

Result controls sit alongside the filters but are not filters: `--limit <int>` (default 100) appears on every list and search command, the only result control. No `--offset`, `--order-by`, or `--desc`.

### Direction unification

`--direction` becomes the single canonical name for the inbound-outbound axis across email, workflow, and template. Workflow's `--type` is renamed to `--direction` with no back-compat alias, since no scripts exist yet. The internal param `workflow_type` may stay; only the operator-facing flag normalizes.

### Code realization (shared decorators)

A filter-option vocabulary lives at the top of `cli.py` or in `_filters.py`, composed in fixed order:

```
@limit_option                        # --limit, default 100
@time_window_options("created_at")   # --since / --until, column-labelled help
@include_disabled_option             # --include-disabled (soft-delete entities only)
@scope_option("company")             # --company-domain, polymorphic + get_company_by_domain validation -> not_found
@enum_option("status", EMAIL_STATUS) # --status, Choice(schema enum)
@range_options("contacts")           # --min-contacts / --max-contacts
@presence_option("profile")          # --has-profile/--no-profile tri-state
```

This collapses the 16 repeated `--limit` flags and the per-entity copy-paste to one source of truth. Asking whether a flag is in the standard is the same as asking whether it came from a vocabulary decorator.

## Output and errors

- **Envelope** — stdout is strict JSON on every command and flag, including `--debug`; operator lines and errors go to stderr (§V.3). A `list`, `search`, `sync`, `export`, or `import` returns `{"<plural>": [...], "ok": true}`; every single-entity verb returns `{"<singular>": {...}, "ok": true}`; an error returns `{"error": CODE, "message": TEXT, "ok": false}` and exits 1 (§V.4, §I.cli). Three helpers produce these: `output`, `output_entity`, and `output_error`.
- **Projection** — list and view rows carry parent denormalization joined at fetch, not a second query (§V.5). The singular `{"<singular>": {...}}` field set is verb-invariant across view, create, update, and disable (§V.8). A view model that omits a base column silently strips it, so the field set is test-tracked.
- **Error codes** — a closed vocabulary, one code per failure class. The constraint and validation codes are fixed by §V.54 (`duplicate_key`, `foreign_key_violation`, `not_null_violation`, `check_violation`, `database_error`, `validation_error`). The domain codes (`not_found`, `invalid_state`, `missing_filter`, `already_exists`, `schema_migration_pending`, `schema_drift`, and peers) are enumerated here; a new code is a spec change. FK validation precedes every mutation, so a missing parent returns `not_found` with no partial write (§V.94).
- **Hygiene** — flags are kebab-case. Output is ASCII-only (§C). Every rendered `--help` is free of `§[VTB].<n>` (§V.111). Each command loads settings first and lazy-imports heavy dependencies inside the command function (§V.1, §V.2).

## Tags as controlled vocabulary

Tags are an operator-maintained enum, not free text. Two tables split the concept:

- **`tag`** — the vocabulary. One row per defined tag, with `name` globally unique and soft-delete via `disabled_reason`. The enum lives here.
- **`tag_assignment`** — the link. One row per tag-and-owner pair, the owner being a company or a contact, mirroring the contact-or-company owner that notes and activities already carry.

One rule makes it an enum: `tag add` errors when the tag is undefined (`not_found`) and never creates the tag as a side effect. This applies the Enum filter family's rule to data — an unknown value errors, never appears silently. The vocabulary differs from a schema CHECK enum (§V.88) only in being operator-maintained rows extended at runtime, not a migration.

The verbs split across two lifecycles:

- `tag create <name>` defines an enum value; `tag disable <name>` retires one.
- `tag add --tag <name> --company-domain <domain>` (or `--contact-email <email>`) links an existing tag to an owner; `tag remove` unlinks.
- `tag list` lists the vocabulary with a projected `usage_count` per tag and needs no owner, so it no longer requires the caller to name a company or contact. `tag list --company-domain <domain>` lists the tags on one owner.
- A link names its tag by `--tag <name>`, resolved through the unique name the way `--account-email` resolves to an account id (§V.107). Operators never paste tag ids.

Reporting follows from the split: `company list --tag <name>` and `contact list --tag <name>` return every entity carrying a curated tag, an Enum-family filter over the assignment join. The closed vocabulary keeps the report axis trustworthy; free strings could not.

A single negative companion `--no-tag <name>` returns every entity NOT carrying the named tag — the same Enum-family membership filter, negated. It is the one controlled exception to the intersection-only filter rule (see Out of scope). It exists for memoization. The lead-contacts pipeline marks a company `no-contacts-found` after a discovery cycle finds nobody, then drops it from the next discover set with `--no-tag no-contacts-found`. This retires the current `company disable --reason no_contacts_found:<date>` abuse, which hides an enriched company from every `company list` default to store a per-pipeline cache fact. `--no-tag` resolves through the vocabulary the way `--tag` does, so an undefined name errors `not_found`. It composes with `--tag` as an intersection — carries A, not B — and no further negation is admitted.

Tags carry `name` only for now. A `category` or `description` is deferred (see Out of scope).

## CLI API map

This map is normative — the full surface as this standard defines it. A few entries differ from current `cli.py`; each such entry is marked *(standard)* and the change is listed under Effect on in-flight SPEC items.

Notation: a bare option is required, a bracketed `[--option]` is optional, and `(--a | --b)` means exactly one of the group is required. A value set is shown as `{a|b|c}`. A positional argument is `<name>`. An entity reference shows its canonical natural key (`<domain>`, `<email>`, `<name>`, `--company-domain`, `--contact-email`) and resolves polymorphically — a UUID is also accepted (see Target identification). Every `list` command carries the universal controls `--limit <int>` (default 100), `--since <iso>`, and `--until <iso>`; the time bound is `created_at` except enrollment (`updated_at`) and task (`scheduled_at`). Those three are omitted from the per-command lines below.

### Global and top-level

- `mailpilot --version` — print the version and exit.
- `mailpilot --debug` — route debug logging to stderr; valid on every command.
- `mailpilot --completion {bash|zsh|fish}` — print the shell completion script and exit.
- `mailpilot --skill` — print the SKILL.md body and exit.
- `mailpilot status` — print the application-state envelope (no options).
- `mailpilot run` — start the sync and task loop in the foreground (no options).

### db

- `mailpilot db init` — provision an empty database (no options).
- `mailpilot db migrate` — apply pending migrations in order (no options).
- `mailpilot db check` — print the schema-verdict report (no options).

### config

- `mailpilot config get [<key>]` — print one setting, or all settings when `<key>` is omitted.
- `mailpilot config set <key> <value>` — write one setting.

### account

- `account create --email <addr> [--display-name <text>]`
- `account view <email>`
- `account update <email> [--display-name <text>]`
- `account sync [--account-email <addr>] [--since <iso>]` — one-shot Gmail sync; all accounts when no selector is given, the one resolved account when given. `--since` bounds the initial full-INBOX backfill (a Gmail `after:` query on the full-listing path); incremental history-based syncs ignore it.
- `account list` — filters: none beyond the universal controls.

### company

- `company create --domain <domain> [--name <text>] [--note <text>]`
- `company update <domain> [--name <text>] [--profile-json <json>]`
- `company disable <domain> --reason <text>`
- `company view <domain>`
- `company search <query>`
- `company export [--file <path>]` — write the company catalog as JSON.
- `company import [--file <path>]` — load a company catalog from JSON.
- `company list` — filters: `[--has-profile/--no-profile]` *(standard; collapses today's two-flag `--has-profile` plus `--no-profile` into one tri-state)*, `[--min-contacts <int>]`, `[--max-contacts <int>]`, `[--tag <name>]` *(standard)*, `[--no-tag <name>]` *(standard)*, `[--include-disabled]`. Projects `contact_count`.

### contact

- `contact create --email <addr> [--first-name <text>] [--last-name <text>] [--company-domain <domain>] [--title <text>] [--email-confidence <int>] [--note <text>]`
- `contact update <email> [--email <addr>] [--first-name <text>] [--last-name <text>] [--company-domain <domain>] [--title <text>] [--email-confidence <int>]`
- `contact disable <email> --reason <text>`
- `contact view <email>`
- `contact search <query>`
- `contact export [--file <path>]` — write the contact catalog as JSON.
- `contact import [--file <path>]` — load a contact catalog from JSON.
- `contact list` — filters: `[--company-domain <domain>]` *(standard; the former `--company-id` Scope and `--company-domain` Text-match collapse into one polymorphic Scope reference — unknown domain errors `not_found`)*, `[--title <text>]` (exact) *(standard; today substring)*, `[--min-email-confidence <int>]`, `[--max-email-confidence <int>]`, `[--tag <name>]` *(standard)*, `[--no-tag <name>]` *(standard)*, `[--include-disabled]`. Projects `title` and `company_domain`.

### email

- `email view <email_id>`
- `email search <query>`
- `email send --account-email <addr> --to <addr> [--to <addr> ...] --subject <text> --body <text> [--workflow-id <id>] [--cc <list>] [--bcc <list>]` — `--to` repeats; `--cc` and `--bcc` are comma-separated.
- `email reply --account-email <addr> --email-id <id> --body <text> [--workflow-id <id>] [--cc <list>] [--bcc <list>]`
- `email list` — filters: `[--contact-email <email>]`, `[--account-email <addr>]`, `[--thread-id <id>]`, `[--workflow-id <id>]`, `[--direction {inbound|outbound}]`, `[--status {sent|received|bounced}]`, `[--from <addr>]` (exact), `[--to <addr>]` (exact), `[--route-method {classified|thread_match|rfc_message_id_match|skipped_outside_window|skipped_no_workflows|skipped_predates_workflows|skipped_no_inbound_workflows}]` *(standard; today a free string)*. Projects `gmail_thread_id`, `is_routed`, `route_method`.

### activity

- `activity add --contact-email <email> [--company-domain <domain>] --type {<activity-type>} --summary <text> [--detail <json>]` *(standard; today `activity create`)*. The type set is the `_ACTIVITY_TYPES` enum (`email_sent`, `email_received`, `note_added`, `tag_added`, `tag_removed`, `tag_disabled`, `status_changed`, `enrollment_added`, `enrollment_completed`, `enrollment_failed`, `enrollment_paused`, `enrollment_resumed`, `enrollment_disabled`).
- `activity list (--contact-email <email> | --company-domain <domain>) [--type {<activity-type>}]` — one owner is required (`missing_filter` otherwise).

### tag *(redesigned — see Tags as controlled vocabulary)*

- `tag create <name>` — define a vocabulary entry.
- `tag view <name>` — read one vocabulary entry.
- `tag disable <name> --reason <text>` — retire a vocabulary entry.
- `tag add --tag <name> (--company-domain <domain> | --contact-email <email>)` — link a defined tag to an owner; errors `not_found` when the tag is undefined.
- `tag remove --tag <name> (--company-domain <domain> | --contact-email <email>)` — unlink.
- `tag search <query>` — search the vocabulary by name.
- `tag list [--company-domain <domain> | --contact-email <email>]` — the vocabulary with `usage_count` when no owner is given; one owner's tags when given.

### note

- `note add (--contact-email <email> | --company-domain <domain>) --body <text>`
- `note view <note_id>`
- `note list (--contact-email <email> | --company-domain <domain>)` — one owner is required.

### workflow

- `workflow create --name <text> --template {outbound-general|inbound-general|inbound-google-drive} --account-email <addr> [--objective <text>] [--instructions <text> | --instructions-file <path>] [--theme <name>] [--draft]`
- `workflow update <workflow_id> [--name <text>] [--objective <text>] [--instructions <text> | --instructions-file <path>] [--theme <name>]`
- `workflow view <workflow_id>`
- `workflow start <workflow_id>` — draft becomes active.
- `workflow stop <workflow_id>` — active becomes paused.
- `workflow search <query>`
- `workflow export --account-email <addr> --out-dir <dir>` — one `*.toml` per workflow plus a JSON status envelope on stdout (§V.103).
- `workflow import --account-email <addr> --file <path>` — `<path>` is one `.toml` file or a directory globbed for `*.toml`.
- `workflow list` — filters: `[--account-email <addr>]`, `[--status {draft|active|paused}]`, `[--direction {inbound|outbound}]` *(standard; today `--type`)*, `[--template {outbound-general|inbound-general|inbound-google-drive}]`. Projects `account_email`.

### template

- `template list [--direction {inbound|outbound}]` — read-only, code-defined registry.
- `template view <name>`

### enrollment

- `enrollment add --workflow-id <id> --contact-email <email> [--scheduled-at <iso>]`
- `enrollment run <enrollment_id>` — invoke the workflow agent synchronously.
- `enrollment disable <enrollment_id> --reason <text>`
- `enrollment view <enrollment_id>`
- `enrollment update <enrollment_id> --status {active|paused} [--reason <text>]`
- `enrollment list` — filters: `[--workflow-id <id>]`, `[--contact-email <email>]`, `[--status {active|paused|disabled}]`. Projects `workflow_name`, `contact_email`, `contact_name`.

### task

- `task view <task_id>`
- `task cancel <task_id>`
- `task retry <task_id>` *(standard; today `--task-id`)*
- `task list` — filters: `[--workflow-id <id>]`, `[--contact-email <email>]`, `[--status {pending|completed|failed|cancelled}]`.

## Effect on in-flight SPEC items

- A new §V (proposed) codifies the six-family filter taxonomy: the naming rules, the mandate that a `Choice` mirrors each schema enum, list text filters exact-only (substring moves to `search`), the `--since` and `--until` closed interval over one declared column, `--limit` as the only result control, and canonical `--direction`.
- §I.cli is amended to document the create-add-remove rule and to make `task retry` take a positional `<task_id>`. The noun set is unchanged; the verb set gains `remove`. `activity create` becomes `activity add`.
- A new §V codifies the tag vocabulary: a `tag` table with globally unique names, a `tag_assignment` join over company or contact, `tag add` erroring on an undefined tag, and a `usage_count` projection on `tag list`. The same §V admits `--no-tag <name>` as the single negative membership filter, the bounded exception to intersection-only composition, so memoization drops a tagged company from a `list` without `disable`.
- §V.90 changes for tags: name uniqueness moves from per-owner active rows to globally unique vocabulary rows, and an assignment is unique per tag-and-owner.
- §V.10 changes for tags: `tag disable` retires a vocabulary entry rather than a per-owner row; detaching a link is the new `tag remove` verb.
- A new §T migrates existing tags forward (§V.108): each distinct name becomes one vocabulary row, and each existing tag row becomes one assignment.
- §V.54 is unchanged: the error-code standard cites it as the constraint-mapping half and enumerates the domain codes around it.
- §V.20 and §V.88 narrow in effect: `route_method`, and any schema-enum filter, must surface as a `click.Choice` mirroring the authoritative schema set.
- §V.95 and §V.96 are subsumed with unchanged behavior: `--min-email-confidence` and `--max-email-confidence`, plus `--min-contacts` and `--max-contacts`, become Range exemplars, where NULL-inclusion follows the Range rule.
- §V.10 and §V.114 are subsumed: `--include-disabled` is the Lifecycle rule.
- §V.107 generalizes from an account-only convention to the whole CLI: every keyed entity (account, company, contact, tag) is addressed by its natural key, resolved polymorphically through the existing `get_<entity>_by_<key>` helpers, with the UUID still accepted. The two-flag `(--account-id | --account-email)` pair collapses to a single `--account-email`; `--company-id` and `--contact-id` become `--company-domain` and `--contact-email` everywhere they name a target or owner. Keyless entities (email, note, task, workflow, enrollment) stay UUID-addressed.
- §V.90 is promoted from a storage invariant to the CLI identifier: the natural keys it declares (account email, company domain, contact email, tag name) become the canonical command-line handles, case-insensitive per the same rule, with an unknown key returning `not_found` (§V.94).
- §V.107 also gains `account sync` as an optional selector: `--account-email` is optional (all accounts when omitted, the one resolved account when given). A new control, `account sync --since <iso>`, bounds the initial full-INBOX backfill via a Gmail `after:` query; it is an Action-verb parameter, not the Lifecycle list-filter `--since`, and the full-INBOX listing path (`sync.py`, first sync or 404 fallback) gains the bound while incremental history syncs are unaffected.
- §V.5 still applies, with a role change: the `company_domain` LEFT JOIN denormalization now backs the `--company-domain` Scope reference (resolve-then-scope), no longer an exact Text-match filter, and it keeps every FK projection feed-able by natural key.
- §V.111 constrains the implementation: every generated `help=` string stays free of `§[VTB].<n>`.
- One behavior change needs a retrofit: `contact list --title` flips from case-insensitive substring to exact. Title substring discovery moves to `contact search`; search-side title coverage is a follow-up (see Out of scope).
- §V.4, §V.8, and the §I.cli envelope are otherwise unchanged.
- A new §T retrofits all 11 list commands onto the filter decorators in one pass: rename `--type` to `--direction`, convert `--route-method` to a `Choice`, flip `--title` to exact, add `--until` everywhere, and convert company presence to a tri-state. A sibling §T applies the verb and target fixes: rename `task retry` to a positional target and `activity create` to `activity add`. A third §T sweeps the natural-key migration: positional targets and owner options on keyed entities accept the natural key (UUID still resolved), the account-ref pair collapses to `--account-email`, and `--company-domain` moves from Text-match to Scope. TDD.
- A new §T sweeps the lead-pipeline skills (`/lead-companies`, `/lead-contacts`) and `scripts/seed_companies.py` onto the standard, in step with the `cli.py` migration. Domain resolution moves off the fuzzy `company search "<arg>" --limit 1` (and the `fetch_owner_name` client-side exact-domain filter) onto exact polymorphic `company view <domain>`, which errors `not_found` on a miss rather than risking a wrong-row substring match. The renamed flags propagate into skill prose: the lead-contacts Next block's `contact list --company-id <ID>` becomes `--company-domain`, and any `--type` becomes `--direction`. Skill prose sits outside pytest, so a grep over the skill tree verifies the sweep. A follow-up §T may migrate the lead-contacts negative-verdict memoization (§V.96) from `company disable` onto a `no-contacts-found` tag plus the `--no-tag` filter; that changes the §V.96 mechanism, so it is a spec change beyond this mechanical sweep.

## Design decisions

- **Decision:** Document the create-add-remove rule rather than retire any verb. **Why:** §I.cli already blesses `create` and `add`; the split carries a real meaning — `create` names an entity, `add` and `remove` link and unlink it to an owner — and tags exercise both halves cleanly.
- **Decision:** Tags are a two-table controlled vocabulary, not free strings. **Why:** A curated, globally unique set is the only way `company list --tag X` and usage reports stay trustworthy; free per-owner strings fragment on typos.
- **Decision:** `tag add` errors on an undefined tag and never auto-creates it. **Why:** Auto-create is what turns a vocabulary back into free text; this is the Enum filter family's rule applied to data.
- **Decision:** Add the `add` and `remove` pair; `disable` retires a vocabulary entry. **Why:** Linking and retiring are different lifecycles, and overloading `disable` for detach is what makes today's tag model confusing.
- **Decision:** Tags attach to both companies and contacts, and carry `name` only for now. **Why:** Notes and activities already own contact-or-company, so tags match for consistency; `category` waits until a report needs grouping (YAGNI).
- **Decision:** Admit one negative filter, `--no-tag <name>`, against the intersection-only rule. **Why:** Memoization needs it. The lead-contacts pipeline must drop a company from the discover set once a finder cycle returns nobody. Today it abuses `company disable`, which hides an enriched company from every `company list` default to store a per-pipeline cache fact. A `no-contacts-found` tag plus `--no-tag` expresses the same exclusion and keeps `disable` meaning retired. The exception stays bounded — tags are a closed vocabulary, so an undefined name errors, and only a single negation is admitted, no general `--not` or `--or`.
- **Decision:** Every single-entity verb takes a positional target; the target is never a `--<entity>-id` option. **Why:** Positional targets read uniformly and free the option namespace for filters; `task retry --task-id` is the lone deviation.
- **Decision:** The natural key is the canonical CLI identifier wherever §V.90 defines one (account email, company domain, contact email, tag name); the UUID stays accepted, resolved polymorphically. **Why:** This is an agent-operated, operator-facing CRM — operators and agents reason in domains and emails, not UUIDs, and §V.107 already proved the pattern (`--account-email`). Generalizing it makes command traces legible (`tag add --tag vip --company-domain acme.com`) and the resolvers already exist (`get_account_by_email`, `get_company_by_domain`, `get_contact_by_email`). Keeping the UUID accepted preserves round-trip: the `id` field and the foreign keys (`contact_id`, `company_id`) in one command's output feed straight into the next. Disambiguation is free — a UUIDv7 (`8-4-4-4-12` hex) never collides with a domain (dots) or an email (at-sign). Keyless entities (email, note, task, workflow, enrollment) have no single natural key, so they stay UUID-addressed; the rule reads "natural key where one exists, UUID otherwise."
- **Decision:** Collapse the §V.107 account-ref pair `(--account-id | --account-email)` to one `--account-email`. **Why:** A single polymorphic flag resolves an email or a UUID, so the two-flag "exactly one" form is redundant; one flag is simpler and the UUID path survives.
- **Decision:** `--company-domain` is reclassified from a Text-match filter to the company Scope reference. **Why:** Resolving the domain to a company and scoping by FK is the same result as matching the denormalized projection, but an unknown domain now errors `not_found` instead of returning a silent empty list — the Scope rule the doc already prefers — and it removes the only Text-match exception that was also a reference.
- **Decision:** Error codes are a closed vocabulary; a new code is a spec change. **Why:** A closed set lets a script branch on `error` reliably, the same reason the filter families are closed; §V.54 already fixes the constraint half.
- **Decision:** A Scope filter with a missing parent (by id or natural key) returns `not_found`. **Why:** This matches existing email, workflow, enrollment, and task validation (`cli.py:1478`); a silent-empty result hides operator typos.
- **Decision:** List text filters are exact only; substring moves to `search`. **Why:** This keeps `list` semantics predictable and indexable; substring and fuzzy match are the search verb's purpose. `--title` case-insensitive substring is the lone deviation, fixed by the retrofit.
- **Decision:** Rename `--type` to `--direction` with no alias. **Why:** No scripts depend on `--type` yet; one canonical name beats the cost of carrying a deprecated alias.
- **Decision:** The Presence tri-state is named `--has-<field>/--no-<field>`, not `--<field>/--no-<field>`. **Why:** `--has-profile` reads as "has a profile" where bare `--profile` is ambiguous; Click does not require matching stems and derives the parameter name from the positive side, so the negative stays the clean `--no-profile` (no `--no-has-profile`). The derived `has_profile` param also matches the existing `list_companies` argument and the projected `CompanySummary.has_profile`, so no downstream rename.
- **Decision:** `--limit` is the only result control; no `--offset` or `--order-by`. **Why:** YAGNI — no paging or sort demand yet.
- **Decision:** Add `--until` to every list command now. **Why:** A symmetric closed-interval window everywhere beats per-entity asymmetry, and a shared decorator makes it low-cost.
- **Decision:** One §T retrofits all 11 list commands in a single pass. **Why:** A half-applied convention is worse than none; the decorators make the sweep mechanical.
- **Decision (default, not user-gated):** `--since` and `--until` are both inclusive (closed interval). **Why:** This keeps taxonomy consistency with Range's inclusive `--min` and `--max`.
- **Decision:** `account sync --since` reuses the `--since` name for the initial-backfill window, not the Lifecycle list-filter family. **Why:** The meaning is the same inclusive lower time bound, so a second name would be drift; only the target differs — `account sync` bounds the Gmail full-INBOX fetch (an `after:` query), where a `list` filter bounds a DB column. `account sync` is an Action verb, not a `list`, so it carries no `--until` companion and no `--limit`. The bound applies only on the full-listing path; an incremental history-based sync is keyed on `gmail_history_id` and ignores it.
- **Decision:** `account sync` makes the `--account-email` selector optional. **Why:** Sync defaults to all accounts, so requiring the selector would break the no-argument all-accounts behavior; the same §V.107 resolver runs, but the flag is optional and resolves at most one account.

## Success criterion

- Every command is `mailpilot <noun> <verb> [args]` with a noun and verb from the closed sets; no command invents a verb outside them.
- Every single-entity verb takes a positional target; a grep over the command tree finds no `--<entity>-id` option naming a verb's own target.
- Every keyed entity (account, company, contact, tag) is addressed by its natural key as the canonical form, with the UUID still accepted; a grep over the command tree finds no `--account-id`, `--company-id`, or `--contact-id` option, and `email view` output's `contact_id` UUID still resolves through `contact view`.
- Every `list` filter flag in `cli.py` maps to exactly one family. A grep over list-command `@click.option` finds nothing outside the vocabulary.
- No schema-enum filter is a free string (`--route-method` is a `Choice`). A typo'd enum value errors, never returns `[]`.
- One flag name `--direction` covers inbound and outbound across email, workflow, and template; `--type` is gone.
- Every error path emits a code from the closed vocabulary; no command invents an inline code.
- Every command emits the §I.cli envelope on stdout and nothing else; `json.load` over any command output succeeds.
- A tag name exists once globally; `tag add` against an undefined name errors and never creates it.
- `company list --tag <name>` and `contact list --tag <name>` return entities by curated tag, `company list --no-tag <name>` returns entities lacking it, and `tag list` projects `usage_count`.
- For tags, `add` and `remove` manage links while `disable` retires vocabulary entries; the two lifecycles never overlap.
- A new command is written by composing vocabulary decorators and picking a verb from the set, with zero re-invention.

## Out of scope

- The `search` verb's full-text search (FTS) semantics beyond the shared `--limit`; whether `contact search` indexes `title` (follow-up for the substring affordance displaced from `--title`).
- Import and export format unification — companies and contacts stay JSON, workflows stay TOML (§V.103).
- Output projection beyond the §V.5 and §V.8 rules; no global `--format` or field-selection flag.
- Filter composition beyond intersection — no `--or`, `--not`, or `--exclude`. The lone exception is `--no-tag <name>`, a single negative tag-membership filter for memoization (see Tags as controlled vocabulary); no other negation, disjunction, or boolean composition is admitted.
- Tag attributes beyond `name` (category, description, color), and tag hierarchy or aliases.
- Cursor, keyset, or offset pagination, and sort flags.
