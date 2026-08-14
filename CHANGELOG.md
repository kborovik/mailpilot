# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- `task retry --scheduled-at` parks a failed or cancelled task on a
  future instant. Omit the flag to keep a still-future stored time, or
  requeue at now when the stored time is past.

### Changed

- Fallback acknowledgement on a terminal inbound failure is first-person
  singular (`I`). The previous we / our-team wording is gone.

### Fixed

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

