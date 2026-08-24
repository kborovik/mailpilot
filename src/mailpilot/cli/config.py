"""Application config commands."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json

import click

from mailpilot.cli.main import (
    _database_url,
    _db,
    main,
    output,
    output_error,
)

# -- Config commands -----------------------------------------------------------


@main.group()
def config() -> None:
    """Manage configuration."""


@config.command("get")
@click.argument("key", required=False)
def config_get(key: str | None) -> None:
    """Show config (all or single key)."""
    from mailpilot.settings import load_settings

    with _db() as connection:
        settings = load_settings(connection=connection, database_url=_database_url())
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
    """Set a config value on the ``app_config`` row."""
    from pydantic import ValidationError

    from mailpilot.settings import (
        APP_CONFIG_KEYS,
        DERIVED_KEYS,
        Settings,
        set_setting,
    )

    if (
        key == "database_url"
        or key in DERIVED_KEYS
        or key not in Settings.model_fields
        or key not in APP_CONFIG_KEYS
    ):
        output_error(f"unknown config key: {key}", "invalid_key")

    field_info = Settings.model_fields[key]
    annotation = field_info.annotation

    if key == "google_application_credentials":
        if value.strip().lower() == "null":
            parsed_value: object = None
        else:
            try:
                parsed: object = json.loads(value)
            except json.JSONDecodeError as exc:
                output_error(f"invalid JSON: {exc}", "validation_error")
            if parsed is not None and not isinstance(parsed, dict):
                output_error(
                    "google_application_credentials must be a JSON object",
                    "validation_error",
                )
            parsed_value = parsed
    elif annotation is int or annotation == (int | None):
        try:
            parsed_value = int(value)
        except ValueError:
            output_error(f"invalid integer: {value}", "validation_error")
    else:
        parsed_value = value

    with _db(mutate=True) as connection:
        try:
            updated = set_setting(connection, key, parsed_value)
        except KeyError:
            output_error(f"unknown config key: {key}", "invalid_key")
        except ValidationError as exc:
            output_error(str(exc), "validation_error")
    dumped = updated.model_dump(mode="json")
    output({"key": key, "value": dumped.get(key, parsed_value)})
