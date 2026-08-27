# MailPilot

Agent-operated CRM with Gmail as the communication layer.

**[See it in action](https://lab5.ca/mailpilot//)**

## Overview

MailPilot manages contacts, companies, and communication workflows through Gmail API.
It is designed to be operated by AI agents -- Claude Code as the strategic orchestrator and an internal Pydantic AI agent for real-time reactive work.

### Two-Layer Intelligence

1. **Claude Code** -- strategic orchestrator.
   Creates workflows, assigns contacts, reviews outcomes, generates reports.
   Operates the system via CLI.
2. **Internal Pydantic AI agent** -- subordinate tactical executor.
   Handles inbound email classification, auto-replies, and follow-up scheduling within workflows.

### Key Capabilities

- **Contact and company management** -- track relationships, tag for segmentation, annotate with notes
- **Activity timeline** -- unified chronological log of all interactions per contact
- **Email workflows** -- inbound auto-reply and outbound campaigns via Gmail API with service account delegation
- **Task scheduling** -- deferred agent work with scheduled execution for long-running processes
- **Reporting** -- Claude Code queries the database and generates activity summaries, relationship health, and campaign effectiveness reports

## Install

MailPilot ships on PyPI as [`mailpilot-crm`](https://pypi.org/project/mailpilot-crm/).
The package installs the `mailpilot` command.
Python 3.14 or later is required.

Install with uv:

```bash
uv tool install mailpilot-crm
```

Or install with pip:

```bash
pip install mailpilot-crm
```

Optional read-only TUI (`mailpilot tui`): install the `tui` extra
(`pip install 'mailpilot-crm[tui]'` or `uv tool install 'mailpilot-crm[tui]'`).

Verify the install:

```bash
mailpilot --version
```

## Quick Start

MailPilot needs PostgreSQL 18 and a Google service account with domain-wide delegation for the Gmail API.

Create the database.
MailPilot provisions the schema on first connection:

```bash
createdb mailpilot
```

Point MailPilot at the database (optional; default is
`postgresql://localhost/mailpilot`). The URL is bootstrap-only -- env
`MAILPILOT_DATABASE_URL` or a cwd `.env` -- not `config set`:

```bash
export MAILPILOT_DATABASE_URL=postgresql://localhost/mailpilot
```

Set the Google service account JSON (or omit for Application Default
Credentials):

```bash
mailpilot config set google_application_credentials "$(cat /path/to/service-account.json)"
```

Set the xAI API key (default provider):

```bash
mailpilot config set xai_api_key xai-...
```

Create the Gmail account MailPilot operates:

```bash
mailpilot account create --email user@example.com --display-name "User Name"
```

Sync the inbox:

```bash
mailpilot account sync
```

Start the event-driven sync loop:

```bash
mailpilot run
```

## Release

Human release notes live in root [`CHANGELOG.md`](CHANGELOG.md) (Keep a Changelog).
During development, append user-facing work under `## Unreleased` in `### Added` / `### Changed` / `### Fixed`.
Empty Unreleased (no bullets) hard-fails the release - nothing to ship.

```bash
make release patch   # or minor | major
```

`make release` is the sole release path (never local `gh release create`):

1. `make check` (ruff, basedpyright, pytest)
2. Fail if `## Unreleased` has no bullets
3. Bump `pyproject.toml` version (`major` | `minor` | `patch`)
4. Promote Unreleased body to `## [vX.Y.Z] - YYYY-MM-DD`, leave an empty `## Unreleased`
5. Commit `CHANGELOG.md` + `pyproject.toml` + `uv.lock` together, tag `vX.Y.Z`, push

GitHub Actions on tag `v*` re-runs CI, builds sdist+wheel, publishes to PyPI (OIDC), and creates a GitHub Release whose notes are the promoted CHANGELOG section for that tag (plus the artifacts).

## License

Copyright 2026 Konstantin Borovik.

Licensed under the Apache License, Version 2.0.
See [LICENSE](LICENSE) for the full text.
