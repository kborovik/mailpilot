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
    )


# -- JSON output pattern -------------------------------------------------------


def output(data: dict[str, Any]) -> None:
    """Print structured JSON response to stdout."""
    click.echo(json.dumps({**data, "ok": True}, indent=2))


def output_error(message: str, code: str) -> NoReturn:
    """Print structured JSON error to stderr and exit."""
    click.echo(
        json.dumps({"error": code, "message": message, "ok": False}, indent=2),
        err=True,
    )
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


# -- Status command ------------------------------------------------------------


@main.command()
def status() -> None:
    """Show application state summary including sync loop status."""
    configure_logging()

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
@click.pass_context
def run(ctx: click.Context) -> None:
    """Start the sync loop (foreground, managed by systemd)."""
    configure_logging(debug=ctx.obj.get("debug", False))

    from mailpilot.database import initialize_database
    from mailpilot.sync import start_sync_loop

    connection = initialize_database(_database_url())
    try:
        start_sync_loop(connection)
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
    from mailpilot.database import create_contact, initialize_database

    domain = email.rsplit("@", maxsplit=1)[-1]
    connection = initialize_database(_database_url())
    try:
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
def contact_list(limit: int, domain: str | None, company_id: str | None) -> None:
    """List contacts."""
    from mailpilot.database import initialize_database, list_contacts

    connection = initialize_database(_database_url())
    try:
        contacts = list_contacts(
            connection, limit=limit, domain=domain, company_id=company_id
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
