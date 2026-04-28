# MailPilot

Agent-operated CRM with Gmail as the communication layer.

**[See it in action](https://lab5.ca/demo/)**

## Overview

MailPilot manages contacts, companies, and communication workflows through Gmail API. It is designed to be operated by AI agents -- Claude Code as the strategic orchestrator and an internal Pydantic AI agent for real-time reactive work.

### Two-Layer Intelligence

1. **Claude Code** -- strategic orchestrator. Creates workflows, assigns contacts, reviews outcomes, generates reports. Operates the system via CLI.
2. **Internal Pydantic AI agent** -- subordinate tactical executor. Handles inbound email classification, auto-replies, and follow-up scheduling within workflows.

### Key Capabilities

- **Contact and company management** -- track relationships, tag for segmentation, annotate with notes
- **Activity timeline** -- unified chronological log of all interactions per contact
- **Email workflows** -- inbound auto-reply and outbound campaigns via Gmail API with service account delegation
- **Task scheduling** -- deferred agent work with scheduled execution for long-running processes
- **Reporting** -- Claude Code queries the database and generates activity summaries, relationship health, and campaign effectiveness reports

## Architecture

- **CLI-first** -- JSON output, meaningful exit codes, actionable errors. Designed for LLM agent consumption.
- **PostgreSQL** -- contacts, companies, workflows, emails, activities, tags, notes. Raw SQL via psycopg, no ORM.
- **Gmail API** -- service account domain-wide delegation. Pub/Sub for real-time notifications. History API for incremental sync.
- **Pydantic AI** -- stateless agent invocations with tool access. Per-contact advisory locks for concurrency.
- **Observability** -- Pydantic Logfire (OpenTelemetry-based) for tracing and logging.

## Tech Stack

- Python 3.14
- PostgreSQL 18
- Gmail API (`google-api-python-client`)
- Pydantic AI (agent framework)
- Pydantic Logfire (observability)
- Click (CLI)
- basedpyright (strict type checking)
- ruff (formatting and linting)
- pytest (testing)

## Quick Start

```bash
# Install dependencies
uv sync

# Configure
mailpilot config set database_url postgresql://localhost/mailpilot
mailpilot config set google_application_credentials /path/to/service-account.json
mailpilot config set anthropic_api_key sk-ant-...

# Create an account
mailpilot account create --email user@example.com --display-name "User Name"

# Sync emails
mailpilot account sync

# Start the sync loop
mailpilot run
```

## Development

```bash
make check    # lint + tests
make lint     # ruff format + ruff check + basedpyright
make py-test  # pytest -x
```

## Documentation

- [Gmail Sync Architecture](docs/adr-01-gmail-api-integration.md)
- [Email Body Storage](docs/adr-02-email-body-storage-strategy.md)
- [Workflow Model](docs/adr-03-workflow-model.md)
- [Email Routing](docs/adr-04-email-routing.md)
- [Schema Migration](docs/adr-05-schema-migration-strategy.md)
- [Workflow Field Definitions](docs/adr-06-workflow-field-definitions.md)
- [Observability with Logfire](docs/adr-07-observability-with-logfire.md)
- [CRM Design](docs/adr-08-crm-design.md)
- [Email Flow](docs/email-flow.md)

## License

Private.
