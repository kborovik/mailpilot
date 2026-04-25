"""CLI interface for MailPilot.

Startup-critical: only ``click`` is imported at module level. All heavy
dependencies (logfire, psycopg, httpx, pydantic, mailpilot.database,
mailpilot.settings) are lazy-imported inside command functions so that
``--help`` / ``--version`` stay fast (~50 ms).
When adding new commands, keep imports inside the function body.
"""

from __future__ import annotations

import json
from typing import Any, NoReturn

import click

# Keep in sync with ActivityType in models.py and CHECK constraint in schema.sql.
_ACTIVITY_TYPES = [
    "email_sent",
    "email_received",
    "note_added",
    "tag_added",
    "tag_removed",
    "status_changed",
    "workflow_assigned",
    "workflow_completed",
    "workflow_failed",
]


def _database_url() -> str:
    """Resolve the database URL from settings at call time (not import time)."""
    from mailpilot.settings import get_settings

    return str(get_settings().database_url)


def configure_logging(debug: bool = False) -> None:
    """Configure Logfire from settings."""
    import logfire

    from mailpilot.settings import get_settings

    settings = get_settings()
    logfire.configure(
        service_name="mailpilot",
        environment=settings.logfire_environment,
        token=settings.logfire_token or None,
        console=logfire.ConsoleOptions(
            min_log_level="debug" if debug else "warn",
            show_project_link=False,
        ),
        send_to_logfire="if-token-present",
        inspect_arguments=False,
        metrics=logfire.MetricsOptions(collect_in_spans=True),
    )
    logfire.instrument_pydantic_ai()


# -- JSON output pattern -------------------------------------------------------


def output(data: dict[str, Any]) -> None:
    """Print structured JSON response to stdout."""
    click.echo(json.dumps({**data, "ok": True}, indent=2))


def output_error(message: str, code: str) -> NoReturn:
    """Print structured JSON error to stderr and exit."""
    from opentelemetry import trace

    payload: dict[str, object] = {"error": code, "message": message, "ok": False}
    current = trace.get_current_span()
    ctx = current.get_span_context() if current else None
    if ctx is not None and ctx.is_valid:
        payload["trace_id"] = format(ctx.trace_id, "032x")
    click.echo(json.dumps(payload, indent=2), err=True)
    raise SystemExit(1)


# -- Main CLI ------------------------------------------------------------------


def _print_completion(
    ctx: click.Context, param: click.Parameter, value: str | None
) -> None:
    """Eager callback: emit the shell completion script and exit.

    Runs before Click validates that a subcommand was given, so
    ``mailpilot --completion zsh`` works without supplying a subcommand.
    """
    if not value or ctx.resilient_parsing:
        return
    from click.shell_completion import get_completion_class

    comp_cls = get_completion_class(value)
    if comp_cls is None:
        click.echo(f"unsupported shell: {value}", err=True)
        ctx.exit(1)
    click.echo(comp_cls(ctx.command, {}, "mailpilot", "_MAILPILOT_COMPLETE").source())
    ctx.exit(0)


@click.group()
@click.version_option()
@click.option("--debug", is_flag=True, help="Enable debug logging.")
@click.option(
    "--completion",
    type=click.Choice(["bash", "zsh", "fish"]),
    default=None,
    is_eager=True,
    expose_value=False,
    callback=_print_completion,
    help="Print shell completion script and exit.",
)
@click.pass_context
def main(ctx: click.Context, debug: bool) -> None:
    """MailPilot -- CRM for cold email outreach via Gmail."""
    ctx.ensure_object(dict)
    ctx.obj["debug"] = debug
    if ctx.invoked_subcommand is not None:
        configure_logging(debug=debug)


# -- Status command ------------------------------------------------------------


@main.command()
def status() -> None:
    """Show application state summary including sync loop status."""
    from mailpilot.database import (
        get_status_counts,
        get_sync_status,
        initialize_database,
    )

    connection = initialize_database(_database_url())
    try:
        counts = get_status_counts(connection)
        sync = get_sync_status(connection)
        sync_info: dict[str, object]
        if sync is None:
            sync_info = {"running": False}
        else:
            sync_info = {
                "running": True,
                "pid": sync.pid,
                "started_at": sync.started_at.isoformat(),
                "heartbeat_at": sync.heartbeat_at.isoformat(),
            }
        output({"status": counts, "sync": sync_info})
    finally:
        connection.close()


# -- Run command ---------------------------------------------------------------


@main.command()
def run() -> None:
    """Start the sync loop (Pub/Sub + task runner, foreground)."""
    from mailpilot.database import initialize_database
    from mailpilot.settings import get_settings
    from mailpilot.sync import start_sync_loop

    settings = get_settings()
    connection = initialize_database(_database_url())
    try:
        start_sync_loop(connection, settings)
    finally:
        connection.close()


# -- Config commands -----------------------------------------------------------


@main.group()
def config() -> None:
    """Manage configuration."""


@config.command("get")
@click.argument("key", required=False)
def config_get(key: str | None) -> None:
    """Show config (all or single key)."""
    from mailpilot.settings import get_settings

    settings = get_settings()
    data = settings.model_dump(mode="json")

    if key:
        if key not in data:
            output_error(f"unknown config key: {key}", "invalid_key")
        output({"key": key, "value": data[key]})
    else:
        output({"config": data})


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Set a config value."""
    from mailpilot.settings import Settings, set_setting

    if key not in Settings.model_fields:
        output_error(f"unknown config key: {key}", "invalid_key")

    field_info = Settings.model_fields[key]
    annotation = field_info.annotation

    if annotation is int or annotation == (int | None):
        parsed_value: object = int(value)
    elif annotation == list[str]:
        parsed_value = json.loads(value) if value.startswith("[") else [value]
    elif annotation == list[int]:
        parsed_value = json.loads(value) if value.startswith("[") else [int(value)]
    else:
        parsed_value = value

    set_setting(key, parsed_value)
    output({"key": key, "value": parsed_value})


# -- Account commands ----------------------------------------------------------


@main.group()
def account() -> None:
    """Manage Gmail accounts."""


@account.command("create")
@click.option("--email", required=True, help="Gmail address.")
@click.option("--display-name", default="", help="Display name.")
def account_create(email: str, display_name: str) -> None:
    """Create a new Gmail account."""
    from mailpilot.database import create_account, initialize_database

    if not email.strip():
        output_error("email cannot be empty", "validation_error")
    connection = initialize_database(_database_url())
    try:
        created = create_account(connection, email=email, display_name=display_name)
        output(created.model_dump(mode="json"))
    finally:
        connection.close()


@account.command("list")
def account_list() -> None:
    """List all Gmail accounts."""
    from mailpilot.database import initialize_database, list_accounts

    connection = initialize_database(_database_url())
    try:
        accounts = list_accounts(connection)
        output({"accounts": [a.model_dump(mode="json") for a in accounts]})
    finally:
        connection.close()


@account.command("view")
@click.argument("account_id")
def account_view(account_id: str) -> None:
    """Show a Gmail account by ID."""
    from mailpilot.database import get_account, initialize_database

    connection = initialize_database(_database_url())
    try:
        found = get_account(connection, account_id)
        if found is None:
            output_error(f"account not found: {account_id}", "not_found")
        output(found.model_dump(mode="json"))
    finally:
        connection.close()


@account.command("update")
@click.argument("account_id")
@click.option("--display-name", default=None, help="Display name.")
def account_update(account_id: str, display_name: str | None) -> None:
    """Update a Gmail account."""
    from mailpilot.database import initialize_database, update_account

    connection = initialize_database(_database_url())
    try:
        fields: dict[str, object] = {}
        if display_name is not None:
            fields["display_name"] = display_name
        updated = update_account(connection, account_id, **fields)
        if updated is None:
            output_error(f"account not found: {account_id}", "not_found")
        output(updated.model_dump(mode="json"))
    finally:
        connection.close()


@account.command("sync")
@click.option(
    "--account-id",
    default=None,
    help="Sync only the given account; omit to sync all accounts.",
)
def account_sync(account_id: str | None) -> None:
    """Run a one-shot Gmail sync for one or all accounts."""
    import logfire

    from mailpilot.database import get_account, initialize_database, list_accounts
    from mailpilot.gmail import GmailClient
    from mailpilot.settings import get_settings
    from mailpilot.sync import sync_account

    settings = get_settings()
    connection = initialize_database(_database_url())
    try:
        if account_id is not None:
            single = get_account(connection, account_id)
            if single is None:
                output_error(f"account not found: {account_id}", "not_found")
            accounts = [single]
        else:
            accounts = list_accounts(connection)

        results: list[dict[str, object]] = []
        total_stored = 0
        with logfire.span("cli.account.sync", account_count=len(accounts)) as span:
            for acc in accounts:
                row: dict[str, object] = {
                    "account_id": acc.id,
                    "email": acc.email,
                }
                try:
                    client = GmailClient(acc.email)
                    stored = sync_account(connection, acc, client, settings)
                    row["stored"] = stored
                    total_stored += stored
                except Exception as exc:
                    from mailpilot.sync import sync_errors

                    sync_errors.add(
                        1,
                        attributes={
                            "account_id": acc.id,
                            "reason": "cli_sync_exception",
                        },
                    )
                    logfire.exception(
                        "cli.account.sync.failed",
                        account_id=acc.id,
                        email=acc.email,
                    )
                    row["error"] = str(exc)
                results.append(row)
            account_succeeded = sum(1 for r in results if "error" not in r)
            account_failed = sum(1 for r in results if "error" in r)
            span.set_attribute("total_stored", total_stored)
            span.set_attribute("account_succeeded", account_succeeded)
            span.set_attribute("account_failed", account_failed)
        output({"results": results, "total_stored": total_stored})
    finally:
        connection.close()


# -- Company commands ----------------------------------------------------------


@main.group()
def company() -> None:
    """Manage target companies."""


@company.command("create")
@click.option("--domain", required=True, help="Primary domain.")
@click.option("--name", default="", help="Company name.")
def company_create(domain: str, name: str) -> None:
    """Create a new company."""
    from mailpilot.database import create_company, initialize_database

    if not domain.strip():
        output_error("domain cannot be empty", "validation_error")
    connection = initialize_database(_database_url())
    try:
        created = create_company(connection, name=name, domain=domain)
        output(created.model_dump(mode="json"))
    finally:
        connection.close()


@company.command("update")
@click.argument("company_id")
@click.option("--name", default=None, help="Company name.")
def company_update(company_id: str, name: str | None) -> None:
    """Update a company."""
    from mailpilot.database import initialize_database, update_company

    connection = initialize_database(_database_url())
    try:
        fields: dict[str, object] = {}
        if name is not None:
            fields["name"] = name
        updated = update_company(connection, company_id, **fields)
        if updated is None:
            output_error(f"company not found: {company_id}", "not_found")
        output(updated.model_dump(mode="json"))
    finally:
        connection.close()


@company.command("search")
@click.argument("query")
@click.option("--limit", default=100, help="Maximum results.")
def company_search(query: str, limit: int) -> None:
    """Search companies by name or domain."""
    from mailpilot.database import initialize_database, search_companies

    connection = initialize_database(_database_url())
    try:
        companies = search_companies(connection, query, limit=limit)
        output({"companies": [c.model_dump(mode="json") for c in companies]})
    finally:
        connection.close()


@company.command("list")
@click.option("--limit", default=100, help="Maximum results.")
def company_list(limit: int) -> None:
    """List all companies."""
    from mailpilot.database import initialize_database, list_companies

    connection = initialize_database(_database_url())
    try:
        companies = list_companies(connection, limit=limit)
        output({"companies": [c.model_dump(mode="json") for c in companies]})
    finally:
        connection.close()


@company.command("view")
@click.argument("company_id")
def company_view(company_id: str) -> None:
    """Show a company by ID."""
    from mailpilot.database import get_company, initialize_database

    connection = initialize_database(_database_url())
    try:
        found = get_company(connection, company_id)
        if found is None:
            output_error(f"company not found: {company_id}", "not_found")
        output(found.model_dump(mode="json"))
    finally:
        connection.close()


@company.command("export")
@click.argument("file", type=click.Path())
def company_export(file: str) -> None:
    """Export all companies to a JSON file."""
    import pathlib

    from mailpilot.database import initialize_database, list_companies

    connection = initialize_database(_database_url())
    try:
        companies = list_companies(connection)
        data = [c.model_dump(mode="json") for c in companies]
        pathlib.Path(file).write_text(json.dumps(data, indent=2))
        output({"exported": len(data), "file": file})
    finally:
        connection.close()


@company.command("import")
@click.argument("file", type=click.Path(exists=True))
def company_import(file: str) -> None:
    """Import companies from a JSON file."""
    import pathlib

    from mailpilot.database import create_company, initialize_database

    connection = initialize_database(_database_url())
    try:
        entries = json.loads(pathlib.Path(file).read_text())
        count = 0
        for entry in entries:
            create_company(connection, name=entry["name"], domain=entry["domain"])
            count += 1
        output({"imported": count, "file": file})
    finally:
        connection.close()


# -- Contact commands ----------------------------------------------------------


@main.group()
def contact() -> None:
    """Manage contacts."""


@contact.command("create")
@click.option("--email", required=True, help="Email address.")
@click.option("--first-name", default=None, help="First name.")
@click.option("--last-name", default=None, help="Last name.")
@click.option("--company-id", default=None, help="Company ID.")
def contact_create(
    email: str,
    first_name: str | None,
    last_name: str | None,
    company_id: str | None,
) -> None:
    """Create a new contact."""
    from mailpilot.database import (
        create_contact,
        get_company,
        initialize_database,
    )

    domain = email.rsplit("@", maxsplit=1)[-1]
    connection = initialize_database(_database_url())
    try:
        if company_id is not None and get_company(connection, company_id) is None:
            output_error(f"company not found: {company_id}", "not_found")
        created = create_contact(
            connection,
            email=email,
            domain=domain,
            first_name=first_name,
            last_name=last_name,
            company_id=company_id,
        )
        output(created.model_dump(mode="json"))
    finally:
        connection.close()


@contact.command("update")
@click.argument("contact_id")
@click.option("--email", default=None, help="Email address.")
@click.option("--first-name", default=None, help="First name.")
@click.option("--last-name", default=None, help="Last name.")
@click.option("--company-id", default=None, help="Company ID.")
def contact_update(
    contact_id: str,
    email: str | None,
    first_name: str | None,
    last_name: str | None,
    company_id: str | None,
) -> None:
    """Update a contact."""
    from mailpilot.database import initialize_database, update_contact

    connection = initialize_database(_database_url())
    try:
        fields: dict[str, object] = {}
        if email is not None:
            fields["email"] = email
            fields["domain"] = email.split("@")[-1]
        if first_name is not None:
            fields["first_name"] = first_name
        if last_name is not None:
            fields["last_name"] = last_name
        if company_id is not None:
            fields["company_id"] = company_id
        updated = update_contact(connection, contact_id, **fields)
        if updated is None:
            output_error(f"contact not found: {contact_id}", "not_found")
        output(updated.model_dump(mode="json"))
    finally:
        connection.close()


@contact.command("search")
@click.argument("query")
@click.option("--limit", default=100, help="Maximum results.")
def contact_search(query: str, limit: int) -> None:
    """Search contacts by email, name, or domain."""
    from mailpilot.database import initialize_database, search_contacts

    connection = initialize_database(_database_url())
    try:
        contacts = search_contacts(connection, query, limit=limit)
        output({"contacts": [c.model_dump(mode="json") for c in contacts]})
    finally:
        connection.close()


@contact.command("list")
@click.option("--limit", default=100, help="Maximum results.")
@click.option("--domain", default=None, help="Filter by domain.")
@click.option("--company-id", default=None, help="Filter by company ID.")
@click.option(
    "--status",
    default=None,
    type=click.Choice(["active", "bounced", "unsubscribed"]),
    help="Filter by contact status.",
)
def contact_list(
    limit: int, domain: str | None, company_id: str | None, status: str | None
) -> None:
    """List contacts."""
    from mailpilot.database import get_company, initialize_database, list_contacts

    connection = initialize_database(_database_url())
    try:
        if company_id is not None and get_company(connection, company_id) is None:
            output_error(f"company not found: {company_id}", "not_found")
        contacts = list_contacts(
            connection, limit=limit, domain=domain, company_id=company_id, status=status
        )
        output({"contacts": [c.model_dump(mode="json") for c in contacts]})
    finally:
        connection.close()


@contact.command("view")
@click.argument("contact_id")
def contact_view(contact_id: str) -> None:
    """Show a contact by ID."""
    from mailpilot.database import get_contact, initialize_database

    connection = initialize_database(_database_url())
    try:
        found = get_contact(connection, contact_id)
        if found is None:
            output_error(f"contact not found: {contact_id}", "not_found")
        output(found.model_dump(mode="json"))
    finally:
        connection.close()


@contact.command("export")
@click.argument("file", type=click.Path())
def contact_export(file: str) -> None:
    """Export all contacts to a JSON file."""
    import pathlib

    from mailpilot.database import initialize_database, list_contacts

    connection = initialize_database(_database_url())
    try:
        contacts = list_contacts(connection)
        data = [c.model_dump(mode="json") for c in contacts]
        pathlib.Path(file).write_text(json.dumps(data, indent=2))
        output({"exported": len(data), "file": file})
    finally:
        connection.close()


@contact.command("import")
@click.argument("file", type=click.Path(exists=True))
def contact_import(file: str) -> None:
    """Import contacts from a JSON file."""
    import pathlib

    from mailpilot.database import create_contact, initialize_database

    connection = initialize_database(_database_url())
    try:
        entries = json.loads(pathlib.Path(file).read_text())
        count = 0
        for entry in entries:
            email = entry["email"]
            domain = entry.get("domain") or email.split("@")[-1]
            create_contact(
                connection,
                email=email,
                domain=domain,
                first_name=entry.get("first_name"),
                last_name=entry.get("last_name"),
                company_id=entry.get("company_id"),
            )
            count += 1
        output({"imported": count, "file": file})
    finally:
        connection.close()


# -- Email commands ------------------------------------------------------------


@main.group()
def email() -> None:
    """Manage emails."""


@email.command("search")
@click.argument("query")
@click.option("--limit", default=100, help="Maximum number of results.")
def email_search(query: str, limit: int) -> None:
    """Search emails by subject or body."""
    from mailpilot.database import initialize_database, search_emails

    connection = initialize_database(_database_url())
    try:
        emails = search_emails(connection, query, limit=limit)
        output({"emails": [e.model_dump(mode="json") for e in emails]})
    finally:
        connection.close()


@email.command("list")
@click.option("--limit", default=100, help="Maximum number of results.")
@click.option("--contact-id", default=None, help="Filter by contact ID.")
@click.option("--account-id", default=None, help="Filter by account ID.")
@click.option("--since", default=None, help="ISO datetime lower bound.")
@click.option("--thread-id", default=None, help="Filter by Gmail thread ID.")
@click.option(
    "--direction",
    default=None,
    type=click.Choice(["inbound", "outbound"]),
    help="Filter by direction.",
)
@click.option("--workflow-id", default=None, help="Filter by workflow ID.")
@click.option(
    "--status",
    default=None,
    type=click.Choice(["sent", "received", "bounced"]),
    help="Filter by email status.",
)
@click.option("--from", "sender", default=None, help="Filter by sender email address.")
@click.option(
    "--to", "recipient", default=None, help="Filter by recipient email address."
)
def email_list(
    limit: int,
    contact_id: str | None,
    account_id: str | None,
    since: str | None,
    thread_id: str | None,
    direction: str | None,
    workflow_id: str | None,
    status: str | None,
    sender: str | None,
    recipient: str | None,
) -> None:
    """List emails with optional filters."""
    from mailpilot.database import (
        get_account,
        get_contact,
        get_workflow,
        initialize_database,
        list_emails,
    )

    connection = initialize_database(_database_url())
    try:
        if contact_id is not None and get_contact(connection, contact_id) is None:
            output_error(f"contact not found: {contact_id}", "not_found")
        if account_id is not None and get_account(connection, account_id) is None:
            output_error(f"account not found: {account_id}", "not_found")
        if workflow_id is not None and get_workflow(connection, workflow_id) is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        emails = list_emails(
            connection,
            limit=limit,
            contact_id=contact_id,
            account_id=account_id,
            since=since,
            thread_id=thread_id,
            direction=direction,
            workflow_id=workflow_id,
            status=status,
            sender=sender,
            recipient=recipient,
        )
        output({"emails": [e.model_dump(mode="json") for e in emails]})
    finally:
        connection.close()


@email.command("view")
@click.argument("email_id")
def email_view(email_id: str) -> None:
    """View a single email by ID."""
    from mailpilot.database import get_email, initialize_database

    connection = initialize_database(_database_url())
    try:
        found = get_email(connection, email_id)
        if found is None:
            output_error(f"email not found: {email_id}", "not_found")
        output(found.model_dump(mode="json"))
    finally:
        connection.close()


@email.command("send")
@click.option("--account-id", required=True, help="Sending account ID.")
@click.option(
    "--to",
    "to",
    required=True,
    multiple=True,
    help="Recipient email address (repeatable).",
)
@click.option("--subject", required=True, help="Email subject.")
@click.option("--body", required=True, help="Plain text body.")
@click.option("--contact-id", default=None, help="Link to an existing contact.")
@click.option("--workflow-id", default=None, help="Link to a workflow.")
@click.option("--thread-id", default=None, help="Gmail thread ID for replies.")
@click.option("--cc", default=None, help="CC recipient(s), comma-separated.")
@click.option("--bcc", default=None, help="BCC recipient(s), comma-separated.")
def email_send(
    account_id: str,
    to: tuple[str, ...],
    subject: str,
    body: str,
    contact_id: str | None,
    workflow_id: str | None,
    thread_id: str | None,
    cc: str | None,
    bcc: str | None,
) -> None:
    """Send an outbound email via the given account's Gmail mailbox."""
    import logfire

    from mailpilot.database import get_account, initialize_database
    from mailpilot.gmail import GmailClient
    from mailpilot.settings import get_settings
    from mailpilot.sync import send_email

    to_joined = ",".join(to)
    settings = get_settings()
    connection = initialize_database(_database_url())
    try:
        account = get_account(connection, account_id)
        if account is None:
            output_error(f"account not found: {account_id}", "not_found")
        client = GmailClient(account.email)
        try:
            sent = send_email(
                connection,
                account=account,
                gmail_client=client,
                settings=settings,
                to=to_joined,
                subject=subject,
                body=body,
                contact_id=contact_id,
                workflow_id=workflow_id,
                thread_id=thread_id,
                cc=cc,
                bcc=bcc,
            )
        except Exception as exc:
            logfire.exception(
                "cli.email.send.failed",
                account_id=account.id,
                to=to,
            )
            output_error(str(exc), "send_failed")
        output(sent.model_dump(mode="json"))
    finally:
        connection.close()


# -- Activity commands ---------------------------------------------------------


@main.group()
def activity() -> None:
    """Manage activity timeline events."""


@activity.command("create")
@click.option("--contact-id", required=True, help="Contact ID.")
@click.option(
    "--type",
    "activity_type",
    required=True,
    type=click.Choice(_ACTIVITY_TYPES),
    help="Activity type.",
)
@click.option("--summary", required=True, help="One-line description.")
@click.option("--detail", default=None, help="JSON detail payload.")
@click.option("--company-id", default=None, help="Optional company ID.")
def activity_create(
    contact_id: str,
    activity_type: str,
    summary: str,
    detail: str | None,
    company_id: str | None,
) -> None:
    """Create an activity event."""
    from mailpilot.database import (
        create_activity,
        get_company,
        get_contact,
        initialize_database,
    )

    if not summary.strip():
        output_error("summary cannot be empty", "validation_error")
    detail_dict: dict[str, object] = json.loads(detail) if detail else {}
    connection = initialize_database(_database_url())
    try:
        if get_contact(connection, contact_id) is None:
            output_error(f"contact not found: {contact_id}", "not_found")
        if company_id is not None and get_company(connection, company_id) is None:
            output_error(f"company not found: {company_id}", "not_found")
        created = create_activity(
            connection,
            contact_id=contact_id,
            activity_type=activity_type,
            summary=summary,
            detail=detail_dict,
            company_id=company_id,
        )
        output(created.model_dump(mode="json"))
    finally:
        connection.close()


@activity.command("list")
@click.option("--contact-id", default=None, help="Filter by contact ID.")
@click.option("--company-id", default=None, help="Filter by company ID.")
@click.option(
    "--type",
    "activity_type",
    default=None,
    type=click.Choice(_ACTIVITY_TYPES),
    help="Filter by activity type.",
)
@click.option("--limit", default=100, help="Maximum results.")
@click.option("--since", default=None, help="ISO datetime lower bound.")
def activity_list(
    contact_id: str | None,
    company_id: str | None,
    activity_type: str | None,
    limit: int,
    since: str | None,
) -> None:
    """List activities (requires --contact-id or --company-id)."""
    from mailpilot.database import (
        get_company,
        get_contact,
        initialize_database,
        list_activities,
    )

    if contact_id is None and company_id is None:
        output_error(
            "at least one of --contact-id or --company-id is required",
            "missing_filter",
        )
    connection = initialize_database(_database_url())
    try:
        if contact_id is not None and get_contact(connection, contact_id) is None:
            output_error(f"contact not found: {contact_id}", "not_found")
        if company_id is not None and get_company(connection, company_id) is None:
            output_error(f"company not found: {company_id}", "not_found")
        activities = list_activities(
            connection,
            contact_id=contact_id,
            company_id=company_id,
            activity_type=activity_type,
            limit=limit,
            since=since,
        )
        output({"activities": [a.model_dump(mode="json") for a in activities]})
    finally:
        connection.close()


# -- Tag commands --------------------------------------------------------------


@main.group()
def tag() -> None:
    """Manage tags on contacts and companies."""


def _resolve_entity(contact_id: str | None, company_id: str | None) -> tuple[str, str]:
    """Return (entity_type, entity_id) or call output_error."""
    if contact_id and company_id:
        output_error("specify only one of --contact-id or --company-id", "invalid_args")
    if contact_id:
        return ("contact", contact_id)
    if company_id:
        return ("company", company_id)
    output_error("one of --contact-id or --company-id is required", "missing_filter")


@tag.command("add")
@click.option("--contact-id", default=None, help="Contact ID.")
@click.option("--company-id", default=None, help="Company ID.")
@click.argument("name")
def tag_add(contact_id: str | None, company_id: str | None, name: str) -> None:
    """Add a tag to a contact or company."""
    from mailpilot.database import (
        create_activity,
        create_tag,
        get_company,
        get_contact,
        initialize_database,
    )

    if not name.strip():
        output_error("tag name cannot be empty", "validation_error")
    entity_type, entity_id = _resolve_entity(contact_id, company_id)
    connection = initialize_database(_database_url())
    try:
        contact = None
        if entity_type == "contact":
            contact = get_contact(connection, entity_id)
            if contact is None:
                output_error(
                    f"contact {entity_id} not found",
                    "not_found",
                )
        else:
            company = get_company(connection, entity_id)
            if company is None:
                output_error(
                    f"company {entity_id} not found",
                    "not_found",
                )
        created = create_tag(
            connection,
            entity_type=entity_type,
            entity_id=entity_id,
            name=name,
        )
        if created is None:
            normalized = name.strip().lower()
            output_error(
                f"tag '{normalized}' already exists on {entity_type} {entity_id}",
                "already_exists",
            )
        if entity_type == "contact" and contact is not None:
            create_activity(
                connection,
                contact_id=entity_id,
                activity_type="tag_added",
                summary=f"Tagged as {created.name}",
                detail={"tag": created.name},
                company_id=contact.company_id,
            )
        output(created.model_dump(mode="json"))
    finally:
        connection.close()


@tag.command("remove")
@click.option("--contact-id", default=None, help="Contact ID.")
@click.option("--company-id", default=None, help="Company ID.")
@click.argument("name")
def tag_remove(contact_id: str | None, company_id: str | None, name: str) -> None:
    """Remove a tag from a contact or company."""
    from mailpilot.database import (
        create_activity,
        delete_tag,
        get_company,
        get_contact,
        initialize_database,
    )

    entity_type, entity_id = _resolve_entity(contact_id, company_id)
    connection = initialize_database(_database_url())
    try:
        contact = None
        if entity_type == "contact":
            contact = get_contact(connection, entity_id)
            if contact is None:
                output_error(f"contact {entity_id} not found", "not_found")
        elif get_company(connection, entity_id) is None:
            output_error(f"company {entity_id} not found", "not_found")
        deleted = delete_tag(
            connection,
            entity_type=entity_type,
            entity_id=entity_id,
            name=name,
        )
        normalized = name.strip().lower()
        if not deleted:
            output_error(
                f"tag '{normalized}' not found on {entity_type} {entity_id}",
                "not_found",
            )
        if entity_type == "contact" and contact is not None:
            create_activity(
                connection,
                contact_id=entity_id,
                activity_type="tag_removed",
                summary=f"Removed tag {normalized}",
                detail={"tag": normalized},
                company_id=contact.company_id,
            )
        output({"removed": True, "tag": normalized, "entity_type": entity_type})
    finally:
        connection.close()


@tag.command("list")
@click.option("--contact-id", default=None, help="Contact ID.")
@click.option("--company-id", default=None, help="Company ID.")
def tag_list(contact_id: str | None, company_id: str | None) -> None:
    """List tags on a contact or company."""
    from mailpilot.database import (
        get_company,
        get_contact,
        initialize_database,
        list_tags,
    )

    entity_type, entity_id = _resolve_entity(contact_id, company_id)
    connection = initialize_database(_database_url())
    try:
        if entity_type == "contact":
            if get_contact(connection, entity_id) is None:
                output_error(f"contact {entity_id} not found", "not_found")
        elif get_company(connection, entity_id) is None:
            output_error(f"company {entity_id} not found", "not_found")
        tags = list_tags(connection, entity_type=entity_type, entity_id=entity_id)
        output({"tags": [t.model_dump(mode="json") for t in tags]})
    finally:
        connection.close()


@tag.command("search")
@click.argument("name")
@click.option(
    "--type",
    "entity_type",
    default=None,
    type=click.Choice(["contact", "company"]),
    help="Filter by entity type.",
)
@click.option("--limit", default=100, help="Maximum results.")
def tag_search(name: str, entity_type: str | None, limit: int) -> None:
    """Search tags by name."""
    from mailpilot.database import initialize_database, search_tags

    connection = initialize_database(_database_url())
    try:
        tags = search_tags(connection, name=name, entity_type=entity_type, limit=limit)
        output({"tags": [t.model_dump(mode="json") for t in tags]})
    finally:
        connection.close()


# -- Note commands -------------------------------------------------------------


@main.group()
def note() -> None:
    """Manage notes on contacts and companies."""


@note.command("add")
@click.option("--contact-id", default=None, help="Contact ID.")
@click.option("--company-id", default=None, help="Company ID.")
@click.option("--body", required=True, help="Note text.")
def note_add(contact_id: str | None, company_id: str | None, body: str) -> None:
    """Add a note to a contact or company."""
    from mailpilot.database import (
        create_activity,
        create_note,
        get_company,
        get_contact,
        initialize_database,
    )

    if not body.strip():
        output_error("note body cannot be empty", "validation_error")
    entity_type, entity_id = _resolve_entity(contact_id, company_id)
    connection = initialize_database(_database_url())
    try:
        contact = None
        if entity_type == "contact":
            contact = get_contact(connection, entity_id)
            if contact is None:
                output_error(
                    f"contact {entity_id} not found",
                    "not_found",
                )
        else:
            company = get_company(connection, entity_id)
            if company is None:
                output_error(
                    f"company {entity_id} not found",
                    "not_found",
                )
        created = create_note(
            connection,
            entity_type=entity_type,
            entity_id=entity_id,
            body=body,
        )
        if entity_type == "contact" and contact is not None:
            create_activity(
                connection,
                contact_id=entity_id,
                activity_type="note_added",
                summary="Note added",
                detail={"note_id": created.id},
                company_id=contact.company_id,
            )
        output(created.model_dump(mode="json"))
    finally:
        connection.close()


@note.command("list")
@click.option("--contact-id", default=None, help="Contact ID.")
@click.option("--company-id", default=None, help="Company ID.")
@click.option("--limit", default=100, help="Maximum results.")
@click.option("--since", default=None, help="ISO datetime lower bound.")
def note_list(
    contact_id: str | None, company_id: str | None, limit: int, since: str | None
) -> None:
    """List notes on a contact or company."""
    from mailpilot.database import (
        get_company,
        get_contact,
        initialize_database,
        list_notes,
    )

    entity_type, entity_id = _resolve_entity(contact_id, company_id)
    connection = initialize_database(_database_url())
    try:
        if entity_type == "contact":
            if get_contact(connection, entity_id) is None:
                output_error(f"contact {entity_id} not found", "not_found")
        elif get_company(connection, entity_id) is None:
            output_error(f"company {entity_id} not found", "not_found")
        notes = list_notes(
            connection,
            entity_type=entity_type,
            entity_id=entity_id,
            limit=limit,
            since=since,
        )
        output({"notes": [n.model_dump(mode="json") for n in notes]})
    finally:
        connection.close()


@note.command("view")
@click.argument("note_id")
def note_view(note_id: str) -> None:
    """View a note by ID."""
    from mailpilot.database import get_note, initialize_database

    connection = initialize_database(_database_url())
    try:
        found = get_note(connection, note_id)
        if found is None:
            output_error(f"note {note_id} not found", "not_found")
        output(found.model_dump(mode="json"))
    finally:
        connection.close()


# -- Workflow commands ---------------------------------------------------------


@main.group()
def workflow() -> None:
    """Manage workflows (inbound + outbound)."""


def _resolve_instructions(
    instructions: str | None, instructions_file: str | None
) -> str | None:
    """Return final instructions text from inline or file source."""
    import pathlib

    if instructions_file is not None:
        return pathlib.Path(instructions_file).read_text()
    return instructions


def _validate_theme(theme: str) -> None:
    """Exit with validation_error if theme is not a recognized name."""
    from mailpilot.email_renderer import THEME_NAMES

    if theme not in THEME_NAMES:
        output_error(
            f"invalid theme '{theme}', must be one of: "
            f"{', '.join(sorted(THEME_NAMES))}",
            "validation_error",
        )


@workflow.command("create")
@click.option("--name", required=True, help="Workflow name.")
@click.option(
    "--type",
    "workflow_type",
    required=True,
    type=click.Choice(["inbound", "outbound"]),
    help="Workflow direction. Immutable after creation.",
)
@click.option("--account-id", required=True, help="Owning Gmail account ID.")
@click.option("--objective", default=None, help="Workflow objective.")
@click.option(
    "--instructions",
    default=None,
    help="Workflow instructions (inline text).",
)
@click.option(
    "--instructions-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a file with the workflow instructions (system prompt).",
)
@click.option(
    "--theme",
    default=None,
    help="Email color theme (blue, green, orange, purple, red, slate).",
)
@click.option(
    "--draft",
    is_flag=True,
    default=False,
    help="Keep workflow in draft status.",
)
def workflow_create(
    name: str,
    workflow_type: str,
    account_id: str,
    objective: str | None,
    instructions: str | None,
    instructions_file: str | None,
    theme: str | None,
    draft: bool,
) -> None:
    """Create a new workflow."""
    from mailpilot.database import (
        activate_workflow,
        create_workflow,
        get_account,
        initialize_database,
        update_workflow,
    )

    if not name.strip():
        output_error("workflow name cannot be empty", "validation_error")
    if theme is not None:
        _validate_theme(theme)
    if instructions is not None and instructions_file is not None:
        output_error(
            "--instructions and --instructions-file are mutually exclusive",
            "validation_error",
        )
    has_objective = objective is not None
    has_instructions = instructions is not None or instructions_file is not None
    if not draft and not (has_objective and has_instructions):
        output_error(
            "cannot activate workflow without objective and instructions. "
            "Use --draft to create without them.",
            "validation_error",
        )
    resolved = _resolve_instructions(instructions, instructions_file)
    connection = initialize_database(_database_url())
    try:
        if get_account(connection, account_id) is None:
            output_error(f"account not found: {account_id}", "not_found")
        created = create_workflow(
            connection,
            name=name,
            workflow_type=workflow_type,
            account_id=account_id,
            theme=theme or "blue",
        )
        extras: dict[str, object] = {}
        if objective is not None:
            extras["objective"] = objective
        if resolved is not None:
            extras["instructions"] = resolved
        if extras:
            created = update_workflow(connection, created.id, **extras) or created
        if not draft and has_objective and has_instructions:
            created = activate_workflow(connection, created.id)
        output(created.model_dump(mode="json"))
    finally:
        connection.close()


@workflow.command("update")
@click.argument("workflow_id")
@click.option("--name", default=None, help="Workflow name.")
@click.option("--objective", default=None, help="Workflow objective.")
@click.option(
    "--instructions",
    default=None,
    help="Workflow instructions (inline text).",
)
@click.option(
    "--instructions-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a file with the workflow instructions (system prompt).",
)
@click.option(
    "--theme",
    default=None,
    help="Email color theme (blue, green, orange, purple, red, slate).",
)
def workflow_update(
    workflow_id: str,
    name: str | None,
    objective: str | None,
    instructions: str | None,
    instructions_file: str | None,
    theme: str | None,
) -> None:
    """Update a workflow."""
    from mailpilot.database import initialize_database, update_workflow

    if theme is not None:
        _validate_theme(theme)
    if instructions is not None and instructions_file is not None:
        output_error(
            "--instructions and --instructions-file are mutually exclusive",
            "validation_error",
        )
    resolved = _resolve_instructions(instructions, instructions_file)
    connection = initialize_database(_database_url())
    try:
        fields: dict[str, object] = {}
        if name is not None:
            fields["name"] = name
        if objective is not None:
            fields["objective"] = objective
        if resolved is not None:
            fields["instructions"] = resolved
        if theme is not None:
            fields["theme"] = theme
        updated = update_workflow(connection, workflow_id, **fields)
        if updated is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        output(updated.model_dump(mode="json"))
    finally:
        connection.close()


@workflow.command("search")
@click.argument("query")
@click.option("--limit", default=100, help="Maximum results.")
def workflow_search(query: str, limit: int) -> None:
    """Search workflows by name or objective."""
    from mailpilot.database import initialize_database, search_workflows

    connection = initialize_database(_database_url())
    try:
        workflows = search_workflows(connection, query, limit=limit)
        output({"workflows": [w.model_dump(mode="json") for w in workflows]})
    finally:
        connection.close()


@workflow.command("list")
@click.option("--account-id", default=None, help="Filter by account ID.")
@click.option(
    "--status",
    default=None,
    type=click.Choice(["draft", "active", "paused"]),
    help="Filter by workflow status.",
)
@click.option(
    "--type",
    "workflow_type",
    default=None,
    type=click.Choice(["inbound", "outbound"]),
    help="Filter by workflow type.",
)
def workflow_list(
    account_id: str | None, status: str | None, workflow_type: str | None
) -> None:
    """List workflows."""
    from mailpilot.database import get_account, initialize_database, list_workflows

    connection = initialize_database(_database_url())
    try:
        if account_id is not None and get_account(connection, account_id) is None:
            output_error(f"account not found: {account_id}", "not_found")
        workflows = list_workflows(
            connection,
            account_id=account_id,
            status=status,
            workflow_type=workflow_type,
        )
        output({"workflows": [w.model_dump(mode="json") for w in workflows]})
    finally:
        connection.close()


@workflow.command("view")
@click.argument("workflow_id")
def workflow_view(workflow_id: str) -> None:
    """Show a workflow by ID."""
    from mailpilot.database import get_workflow, initialize_database

    connection = initialize_database(_database_url())
    try:
        found = get_workflow(connection, workflow_id)
        if found is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        output(found.model_dump(mode="json"))
    finally:
        connection.close()


@workflow.command("start")
@click.argument("workflow_id")
def workflow_start(workflow_id: str) -> None:
    """Start a workflow (requires non-empty objective and instructions)."""
    from mailpilot.database import activate_workflow, initialize_database

    connection = initialize_database(_database_url())
    try:
        try:
            activated = activate_workflow(connection, workflow_id)
        except ValueError as exc:
            message = str(exc)
            if "objective" in message:
                output_error(
                    f"cannot start: objective is empty. "
                    f'Run: workflow update {workflow_id} --objective "..."',
                    "invalid_state",
                )
            if "instructions" in message:
                output_error(
                    f"cannot start: instructions are empty. "
                    f'Run: workflow update {workflow_id} --instructions "..."',
                    "invalid_state",
                )
            output_error(message, "invalid_state")
        output(activated.model_dump(mode="json"))
    finally:
        connection.close()


@workflow.command("stop")
@click.argument("workflow_id")
def workflow_stop(workflow_id: str) -> None:
    """Stop an active workflow."""
    from mailpilot.database import initialize_database, pause_workflow

    connection = initialize_database(_database_url())
    try:
        try:
            paused = pause_workflow(connection, workflow_id)
        except ValueError as exc:
            output_error(str(exc), "invalid_state")
        output(paused.model_dump(mode="json"))
    finally:
        connection.close()


@workflow.command("run")
@click.option("--workflow-id", required=True, help="Workflow ID.")
@click.option("--contact-id", required=True, help="Contact ID.")
def workflow_run(workflow_id: str, contact_id: str) -> None:
    """Invoke the workflow agent for a single contact synchronously.

    Manual runs invoke the agent directly. Going through ``create_task``
    would fire ``pg_notify('task_pending')``, which a parallel ``mailpilot
    run`` listener thread translates into a competing drain of the same
    row. Tasks are for deferred work; CLI runs are immediate.
    """
    from mailpilot.agent import invoke_workflow_agent
    from mailpilot.database import (
        get_contact,
        get_unprocessed_inbound_email,
        get_workflow,
        get_workflow_contact,
        initialize_database,
    )
    from mailpilot.settings import get_settings

    settings = get_settings()
    connection = initialize_database(_database_url())
    try:
        wf = get_workflow(connection, workflow_id)
        if wf is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        if wf.status != "active":
            output_error(
                f"workflow is not active (status={wf.status})", "invalid_state"
            )
        contact = get_contact(connection, contact_id)
        if contact is None:
            output_error(f"contact not found: {contact_id}", "not_found")
        if get_workflow_contact(connection, workflow_id, contact.id) is None:
            output_error(
                f"contact {contact_id} is not enrolled in workflow {workflow_id}",
                "not_found",
            )
        email = None
        if wf.type == "inbound":
            email = get_unprocessed_inbound_email(connection, wf.id, contact.id)
        description = f"manual {wf.type} run"
        envelope: dict[str, object] = {
            "workflow_id": wf.id,
            "contact_id": contact.id,
        }
        try:
            result = invoke_workflow_agent(
                connection,
                settings,
                wf,
                contact,
                email=email,
                task_description=description,
            )
        except Exception as exc:
            envelope["status"] = "failed"
            envelope["result"] = {"reason": str(exc)}
            output(envelope)
            return
        if result is None:
            envelope["status"] = "skipped"
            envelope["result"] = {"reason": "agent lock held"}
            output(envelope)
            return
        envelope["status"] = "completed"
        envelope["result"] = {
            "reasoning": result.get("reasoning", ""),
            "tool_calls": result.get("tool_calls", 0),
        }
        output(envelope)
    finally:
        connection.close()


# -- Workflow Contact subgroup -------------------------------------------------


@workflow.group("contact")
def workflow_contact() -> None:
    """Manage contact enrollment in workflows."""


@workflow_contact.command("add")
@click.option("--workflow-id", required=True, help="Workflow ID.")
@click.option("--contact-id", required=True, help="Contact ID.")
def workflow_contact_add(workflow_id: str, contact_id: str) -> None:
    """Enroll a contact in a workflow."""
    from mailpilot.database import (
        create_workflow_contact,
        get_contact,
        get_workflow,
        get_workflow_contact,
        initialize_database,
    )

    connection = initialize_database(_database_url())
    try:
        if get_workflow(connection, workflow_id) is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        if get_contact(connection, contact_id) is None:
            output_error(f"contact not found: {contact_id}", "not_found")
        created = create_workflow_contact(connection, workflow_id, contact_id)
        if created is not None:
            output(created.model_dump(mode="json"))
            return
        existing = get_workflow_contact(connection, workflow_id, contact_id)
        if existing is not None:
            output(existing.model_dump(mode="json"))
            return
    finally:
        connection.close()


@workflow_contact.command("remove")
@click.option("--workflow-id", required=True, help="Workflow ID.")
@click.option("--contact-id", required=True, help="Contact ID.")
def workflow_contact_remove(workflow_id: str, contact_id: str) -> None:
    """Remove a contact from a workflow."""
    from mailpilot.database import delete_workflow_contact, initialize_database

    connection = initialize_database(_database_url())
    try:
        deleted = delete_workflow_contact(connection, workflow_id, contact_id)
        if not deleted:
            output_error("workflow-contact not found", "not_found")
        output({"workflow_id": workflow_id, "contact_id": contact_id})
    finally:
        connection.close()


@workflow_contact.command("list")
@click.option("--workflow-id", required=True, help="Workflow ID.")
@click.option(
    "--status",
    default=None,
    type=click.Choice(["pending", "active", "completed", "failed"]),
    help="Filter by enrollment status.",
)
@click.option("--limit", default=100, help="Maximum results.")
def workflow_contact_list(workflow_id: str, status: str | None, limit: int) -> None:
    """List contacts enrolled in a workflow."""
    from mailpilot.database import (
        get_workflow,
        initialize_database,
        list_workflow_contacts_enriched,
    )

    connection = initialize_database(_database_url())
    try:
        if get_workflow(connection, workflow_id) is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        contacts = list_workflow_contacts_enriched(
            connection, workflow_id, status=status, limit=limit
        )
        output({"contacts": [c.model_dump(mode="json") for c in contacts]})
    finally:
        connection.close()


@workflow_contact.command("update")
@click.option("--workflow-id", required=True, help="Workflow ID.")
@click.option("--contact-id", required=True, help="Contact ID.")
@click.option(
    "--status",
    required=True,
    type=click.Choice(["pending", "active", "completed", "failed"]),
    help="New enrollment status.",
)
@click.option("--reason", default=None, help="Status reason.")
def workflow_contact_update(
    workflow_id: str, contact_id: str, status: str, reason: str | None
) -> None:
    """Update enrollment status and reason."""
    from mailpilot.database import initialize_database, update_workflow_contact

    connection = initialize_database(_database_url())
    try:
        fields: dict[str, object] = {"status": status}
        if reason is not None:
            fields["reason"] = reason
        updated = update_workflow_contact(connection, workflow_id, contact_id, **fields)
        if updated is None:
            output_error("workflow-contact not found", "not_found")
        output(updated.model_dump(mode="json"))
    finally:
        connection.close()


# -- Task commands -------------------------------------------------------------


@main.group()
def task() -> None:
    """Manage deferred agent tasks."""


@task.command("list")
@click.option("--workflow-id", default=None, help="Filter by workflow ID.")
@click.option("--contact-id", default=None, help="Filter by contact ID.")
@click.option(
    "--status",
    default=None,
    type=click.Choice(["pending", "completed", "failed", "cancelled"]),
    help="Filter by task status.",
)
@click.option("--limit", default=100, help="Maximum results.")
def task_list(
    workflow_id: str | None,
    contact_id: str | None,
    status: str | None,
    limit: int,
) -> None:
    """List tasks with optional filters."""
    from mailpilot.database import (
        get_contact,
        get_workflow,
        initialize_database,
        list_tasks,
    )

    connection = initialize_database(_database_url())
    try:
        if workflow_id is not None and get_workflow(connection, workflow_id) is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        if contact_id is not None and get_contact(connection, contact_id) is None:
            output_error(f"contact not found: {contact_id}", "not_found")
        tasks = list_tasks(
            connection,
            workflow_id=workflow_id,
            contact_id=contact_id,
            status=status,
            limit=limit,
        )
        output({"tasks": [t.model_dump(mode="json") for t in tasks]})
    finally:
        connection.close()


@task.command("view")
@click.argument("task_id")
def task_view(task_id: str) -> None:
    """Show a task by ID."""
    from mailpilot.database import get_task, initialize_database

    connection = initialize_database(_database_url())
    try:
        found = get_task(connection, task_id)
        if found is None:
            output_error(f"task not found: {task_id}", "not_found")
        output(found.model_dump(mode="json"))
    finally:
        connection.close()


@task.command("cancel")
@click.argument("task_id")
def task_cancel(task_id: str) -> None:
    """Cancel a pending task."""
    from mailpilot.database import cancel_task, initialize_database

    connection = initialize_database(_database_url())
    try:
        cancelled = cancel_task(connection, task_id)
        if cancelled is None:
            output_error(f"task not found or not pending: {task_id}", "not_found")
        output(cancelled.model_dump(mode="json"))
    finally:
        connection.close()
