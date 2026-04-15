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
    """Show application state summary."""
    configure_logging()

    from mailpilot.database import get_status_counts, initialize_database

    connection = initialize_database(_database_url())
    try:
        counts = get_status_counts(connection)
        output({"status": counts})
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
