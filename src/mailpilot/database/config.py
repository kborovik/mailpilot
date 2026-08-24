"""App config singleton (§V.181)."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.sql import SQL, Identifier
from psycopg.types.json import Json

from mailpilot.settings import APP_CONFIG_KEYS, Settings

# -- App config singleton (§V.181) ---------------------------------------------


def get_or_insert_app_config(
    connection: psycopg.Connection[dict[str, Any]],
) -> dict[str, Any]:
    """Return the ``app_config`` singleton, inserting column defaults if missing.

    Args:
        connection: Open database connection.

    Returns:
        The singleton row as a dict (includes ``id``).
    """
    connection.execute(
        "INSERT INTO app_config (id) VALUES ('singleton') ON CONFLICT (id) DO NOTHING"
    )
    row = connection.execute(
        "SELECT * FROM app_config WHERE id = 'singleton'"
    ).fetchone()
    assert row is not None
    return dict(row)


def upsert_app_config_from_settings(
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
) -> dict[str, Any]:
    """Write every persistable Settings field onto the singleton row.

    Used at ``mailpilot run`` start so the first tick re-SELECT matches the
    process snapshot (tests pass in-memory Settings; production loaded the
    same row).

    Args:
        connection: Open database connection.
        settings: Settings to persist.

    Returns:
        The updated singleton row.
    """
    get_or_insert_app_config(connection)
    assignments = SQL(", ").join(
        SQL("{} = %s").format(Identifier(key)) for key in APP_CONFIG_KEYS
    )
    values: list[object] = []
    for key in APP_CONFIG_KEYS:
        value: object = getattr(settings, key)
        if key == "google_application_credentials" and value is not None:
            value = Json(value)
        values.append(value)
    row = connection.execute(
        SQL("UPDATE app_config SET {} WHERE id = 'singleton' RETURNING *").format(
            assignments
        ),
        values,
    ).fetchone()
    assert row is not None
    return dict(row)


def update_app_config_key(
    connection: psycopg.Connection[dict[str, Any]],
    key: str,
    value: object,
) -> dict[str, Any]:
    """UPDATE one persistable ``app_config`` column and return the row.

    Args:
        connection: Open database connection.
        key: Column name; must be in ``APP_CONFIG_KEYS``.
        value: Bound parameter (JSONB dicts are wrapped in ``Json``).

    Returns:
        The updated singleton row.

    Raises:
        KeyError: ``key`` is not an ``app_config`` column.
    """
    if key not in APP_CONFIG_KEYS:
        raise KeyError(key)
    bound: object = value
    if key == "google_application_credentials" and value is not None:
        bound = Json(value)
    row = connection.execute(
        SQL("UPDATE app_config SET {} = %s WHERE id = 'singleton' RETURNING *").format(
            Identifier(key)
        ),
        (bound,),
    ).fetchone()
    assert row is not None
    return dict(row)
