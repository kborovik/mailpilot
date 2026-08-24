# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- `email reply --subject` overrides the default `Re: original`
  subject. Omit it and the reply still prefixes the original.

### Changed

- Mechanical out-of-office inbound does not create a
  `handle inbound email` agent task. Resume is scheduled once during
  routing. Language-only OOO still runs the agent. Agent `noop` still
  resumes after invoke.
- `workflow status` wording comes from `check_workflow_wording`. An
  empty catalog classifies live rows as `orphaned`. The field is never
  hardcoded `unknown`. CLI verbs and envelope keys stay.

## [v0.31.0] - 2026-08-22

### Fixed

- `config set` commits the `app_config` write before the connection
  closes. A successful envelope is visible to a later `config get` and
  to `mailpilot run`. CLI write commands share that commit on
  `_db(mutate=True)` success; errors still roll back.

### Changed

- Persistent settings live in the `app_config` database singleton, not
  `~/.mailpilot/config.json`. `config get` / `config set` read and write
  that row.
- `database_url` is bootstrap-only (`MAILPILOT_DATABASE_URL`, cwd `.env`,
  or `postgresql://localhost/mailpilot`). `config set database_url`
  returns `invalid_key`. Other `MAILPILOT_*` env vars are not sources.
- `google_application_credentials` is a JSON service-account document.
  `null` uses Application Default Credentials. A file path is no longer
  accepted.
- A missing active-provider API key skips that `mailpilot run` tick
  (process stays up; zero due tasks claimed). The error names
  `mailpilot config set xai_api_key` or
  `mailpilot config set anthropic_api_key`.
- `make clean` drops and recreates the databases, then restores empty
  `app_config` keys from `pass mailpilot/`. The `db-backup`,
  `config-backup`, and `env-backup` targets are gone.

## [v0.30.1] - 2026-08-21

### Fixed

- Recording an enrollment terminal outcome (including `do_not_contact`)
  bumps `enrollment.updated_at` in the same transaction as the activity.
  `enrollment list --full --disposition do_not_contact --since --until`
  windows DNC applied in that date range without `contact view --timeline`.
  Enrollment status stays `active` or `disabled`; there is no
  `disposition_updated_at` column.



- A past-tense last-day auto-reply ("last day was", "last day with … was")
  is left-company, not an out-of-office pause. Automatic reply and
  Auto-Submitted no longer year-roll that date. Named successors without
  emails stay in the note.

- Out-of-office bodies that name an event week and a later return date
  resume on the return date this year. A past "week of August 17th" is
  not a resume date and does not year-roll. "Fully back online on
  Monday, August 24th" schedules 2026-08-24, not 2027-08-17.

- `workflow import` `in_sync` now hashes the live written row with the
  same SHA-256 as `workflow check`. Each applied row includes
  `catalog_hash` and `row_hash`. When hashes still differ, `changed`
  lists the remaining def-field delta, not only the fields just written.
  A successful import of a drifted TOML leaves `workflow check --file`
  in sync without a second import. Incomplete cadence (`touches` without
  `touch_interval_days`) persists as single-touch. Stored columns hash
  as-is (an empty `theme` is not re-defaulted to `blue`).

## [v0.30.0] - 2026-08-20

### Added

- `environment` (`dev` or `prd`, default `dev`) is the operator knob for
  target env. Pub/Sub topic and subscription names are derived from it
  and cannot be set independently. Existing config files that only store
  `logfire_environment` still load.

### Changed

- Live campaign-test and reply-test preflight gate on `environment=dev`
  instead of `logfire_environment=development`.
- `config get` and `mailpilot status` no longer project
  `logfire_environment`. Logfire's deployment environment is mapped
  internally at configure (`dev` -> `development`, `prd` -> `production`).

## [v0.29.0] - 2026-08-19

### Fixed

- `mailpilot run` aborts before draining tasks when the active LLM
  provider API key is missing. The error names `MAILPILOT_XAI_API_KEY`
  or `MAILPILOT_ANTHROPIC_API_KEY` and does not prescribe
  `mailpilot config set` as the only fix.

### Added

- `task retry` accepts the same filters as `task list` plus repeatable
  `--touch N` (or `T<n>`). One call retries matching failed (default)
  or cancelled tasks and returns `retried_count`, `ids`, `scheduled_at`,
  and `companies`. `--scheduled-at` applies to the selected set.
  `--dry-run` previews ids and companies with no writes.
  `task retry <id>` is unchanged unless `--dry-run`.

- `workflow review <slug|all> --since --until` returns one dated
  campaign envelope: funnel, failed/overdue/pending counts, window
  emails with snippet, window activities including inbound
  `email_received` with snippet, failed tasks with `contact_email` and
  `result.reason`, and enrollments not capped below the live enrolled
  count. `all` reviews every active workflow.

- `task cancel` accepts the same filters as `task list` plus repeatable
  `--touch N` (or `T<n>`). One call cancels matching pending tasks and
  returns `cancelled_count`, `ids`, and `leftover_pending_by_touch`.
  `task list --touch` shares the filter. `task cancel <id>` is unchanged.

## [v0.28.0] - 2026-08-17

### Added

- `email list` and `email search` project `snippet` (first 500 characters
  of `body_text`) so inbound out-of-office / left-company / referral
  classification does not need `email view`.
- `task list` projects `result.reason` on failed rows so campaign-review
  can classify fail cause without `task view`.

### Fixed

- Out-of-office year-less return dates stay in the current year. A
  week-range containing today resumes the day after the range end. A
  weekday-month-day leave-start months in the past is unparseable
  (cadence fallback), not next year. Explicit year still wins.

## [v0.27.0] - 2026-08-14

### Added

- `contact view`, `contact list`, and `contact search` project `tags[]`
  (assigned names, empty array ok). `company view --full` lean contacts
  include the same `tags[]` shape.

- `enrollment add --file` or `--tag` plus `--scheduled-at` enrolls a
  reviewed batch in one envelope. `--limit` is a hard cap, or a soft
  cap with `--company-atomic` (same calendar day per domain; last
  company may exceed the cap). `--exclude-peer` drops seats already
  active in another workflow. Tag apply never restamps seats already
  enrolled in this workflow. Dry-run packing flags reuse the same pack.

## [v0.26.0] - 2026-08-14

### Added

- `task retry --scheduled-at` parks a failed or cancelled task on a
  future instant. Omit the flag to keep a still-future stored time, or
  requeue at now when the stored time is past.

### Changed

- Fallback acknowledgement on a terminal inbound failure is first-person
  singular (`I`). The previous we / our-team wording is gone.

### Fixed

- `tag remove` accepts repeatable `--company-domain` or `--contact-email`
  (same owner-kind XOR as `tag add`). One invocation unlinks every listed
  owner. Multiple owners return a `results` envelope; an already-unlinked
  row is an ok skip.

- Out-of-office inbound no longer counts as a reply for the touch
  pre-flight cancel. Retrying a cancelled T2 after OOO no longer
  re-cancels immediately.

## [v0.25.1] - 2026-08-13

### Changed

- `show queue --tz` defaults to the host local IANA timezone (`TZ` env or
  OS zoneinfo). An unresolvable host zone falls back to UTC. Explicit
  `--tz` still overrides.
- `show queue` table and JSON `next_at` are a full ISO datetime in `--tz`
  (offset included). JSON no longer emits stored UTC.

## [v0.25.0] - 2026-08-13

### Fixed

- `enrollment add --scheduled-at` on an existing never-sent first-reach
  enrollment updates the pending task time in place. A re-run at the same
  instant stays a no-op. Later touches and already-sent enrollments are
  not moved.
- `workflow stats` `touches.1.pending` and `show queue --detail` treat
  `enrollment_schedule` tasks with no `context.touch` as T1.

### Changed

- `show queue` workflow-grain columns are `workflow_name`, `status`,
  `t1`, `t2`, `t3`, `t4p`, `next_at`. Table `next_at` is a full ISO
  datetime in `--tz`; JSON keeps the stored ISO.
- `show queue --detail` columns are `workflow_name`, `company_domain`,
  `contact`, `email`, `touch`, `attempts`, `next_at`. Relative `when`,
  `trigger`, and `state` are gone. Table `next_at` is a full ISO datetime
  in `--tz`; JSON keeps the stored ISO.

## [v0.24.0] - 2026-08-13

### Added

- `company view --full` embeds `contacts[]` with existing `tags[]` and
  `notes[]` on one company envelope. Lean `company view` is unchanged.
  Pass `--include-meta` to project `verification_meta` on those contacts.

- `workflow check --account-email` plus `--file` restores that account's
  full wording envelope, including orphaned rows.

- `workflow import --file` recurses `**/*.toml`. Each applied row includes
  `in_sync` and a short `changed` excerpt so `workflow view` is not
  required to confirm ready-copy.

- `enrollment add --tag --dry-run` matches company tags or contact tags
  (union, unique by contact). Preview rows include title, company tags,
  contact tags, email confidence, and peer-workflow names, grouped by
  company.

- `enrollment list --touch 1` matches never-sent enrollments that have a
  scheduled first send. `--full` projects `next_touch=1` on those rows.
  `enrollment add --scheduled-at` writes `context.touch` as `1`.

- `company create` accepts profile flags and repeatable `--tag` in one
  call. Combined with `--upsert`, a second identical call exits 0,
  updates the profile when flags are present, and does not duplicate
  tags. Invalid profile or undefined tag writes nothing.

### Changed

- Out-of-office automatic replies on an outbound enrollment no longer
  send the fallback acknowledgement or burn a touch. The next touch
  resumes on the stated return date when that date is parseable, or
  after the cadence interval (three days when cadence is unset).
  Address-change and left-company auto-replies stay a hard stop.

- `company list` and `contact list` `--tag` is repeatable and AND-composes
  (the row must carry every named tag). `--help` documents AND, matching
  `--no-tag`.

- `company merge` accepts a disabled source and a disabled survivor. The
  survivor keeps its `disabled_reason`; the source is recorded as an
  alias with `merged:into <survivor.domain>`.

- `workflow check --file` always path-scopes to discovered TOML files
  (file or directory; directories recurse `**/*.toml`). A directory is
  no longer a full-catalog check. Other account workflows no longer
  appear as orphaned.

## [v0.23.1] - 2026-08-13

### Changed

- `mailpilot show queue` table `next_at` is a date (`YYYY-MM-DD` in
  `--tz`), not a full ISO timestamp. JSON still emits the ISO datetime.
- `mailpilot show queue` column and filter are `workflow_name` /
  `--workflow-name` (name or UUID). The old `--workflow-id` flag and
  `workflow` column are gone.

### Added

- `mailpilot show queue` operator report hub. Default ASCII table; pass
  `--format json` for the `queue` envelope (`record_count` is the row
  count). `--detail` lists pending tasks in queue order. `--workflow-name`
  accepts a workflow name or UUID.

## [v0.23.0] - 2026-08-12

### Added

- `email list --workflow-id` accepts a workflow name or UUID, matching
  enrollment / task / activity list.
- Keep-a-Changelog release notes. `scripts/changelog` checks Unreleased,
  promotes it to `## [vX.Y.Z] - YYYY-MM-DD`, and extracts a version
  section for GitHub Release notes. `make release` hard-fails when
  Unreleased has no bullets, then commits `CHANGELOG.md` with the
  version bump. Tag `v*` CI publishes to PyPI (OIDC) and creates the
  GitHub Release from that section (`--notes-file`), not `--generate-notes`.

### Changed

- Address-change / "update your records" / hard-redirect auto-replies
  conclude `do_not_contact` (cancel follow-ups, disable the old contact)
  and never enroll the new address. Distinct from OOO auto-reply (noop).
- Bounce handler concludes every active outbound enrollment as
  `do_not_contact` and cancels pending follow-ups. Already-terminal
  enrollments are skipped; contact disable is unchanged.
- Inbound on an existing outbound thread binds the enrolled contact when
  From uses a different local-part. The alias is not minted or enrolled.
  Left-company / retired auto-replies still conclude the original enrollment.
- `campaign-test` default workflow is `var-sales-coclose`. `--workflow-file`
  override is unchanged.
- `campaign-test` and `reply-test` preflight fail closed unless
  `logfire_environment=development`, before any CRM or Gmail mutate.

### Fixed

- `context.touch` values like `T2` no longer crash `workflow stats`,
  `report`, `status`, or `enrollment --full` / `--touch`. Unparseable
  values become NULL; new OOO-resume tasks write a numeric touch.

