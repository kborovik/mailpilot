# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- `enrollment add --tag --dry-run` matches company tags or contact tags
  (union, unique by contact). Preview rows include title, company tags,
  contact tags, email confidence, and peer-workflow names, grouped by
  company.

- `company create` accepts profile flags and repeatable `--tag` in one
  call. Combined with `--upsert`, a second identical call exits 0,
  updates the profile when flags are present, and does not duplicate
  tags. Invalid profile or undefined tag writes nothing.

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

