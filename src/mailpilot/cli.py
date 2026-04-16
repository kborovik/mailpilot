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


@click.group()
@click.version_option()
@click.option("--debug", is_flag=True, help="Enable debug logging.")
@click.option("--completion", type=str, default=None, hidden=True)
@click.pass_context
def main(ctx: click.Context, debug: bool, completion: str | None) -> None:
    """MailPilot -- CRM for cold email outreach via Gmail."""
    if completion:
        from click.shell_completion import get_completion_class

        comp_cls = get_completion_class(completion)
        if comp_cls:
            click.echo(comp_cls(main, {}, "mailpilot", "_MAILPILOT_COMPLETE").source())
        raise SystemExit(0)
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
    from mailpilot.settings import Settings, get_settings, save_settings

    settings = get_settings()
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

    data = settings.model_dump(mode="json")
    data[key] = parsed_value
    updated = Settings(**{k: v for k, v in data.items() if k in Settings.model_fields})
    save_settings(updated)
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


# -- Contact commands ----------------------------------------------------------


@main.group()
def contact() -> None:
    """Manage contacts."""


# -- Email commands ------------------------------------------------------------


@main.group()
def email() -> None:
    """Manage emails."""
