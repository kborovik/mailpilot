"""Shared database helpers, paths, and constants."""
# pyright: reportPrivateUsage=false, reportUnusedFunction=false

from __future__ import annotations

import re
import uuid
from importlib.metadata import version as _pkg_version
from pathlib import Path

from psycopg.sql import SQL, Composed, Identifier, Placeholder

# Distribution name is mailpilot-crm; the import package stays mailpilot.
_MAILPILOT_VERSION = _pkg_version("mailpilot-crm")

_INLINE_NOTES_CAP = 10

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = _PACKAGE_ROOT / "schema.sql"
MIGRATIONS_PATH = _PACKAGE_ROOT / "migrations"

# Filename grammar for forward-only migrations: NNN_snake_description.sql with a
# monotonic integer prefix (§V.108). Files that do not match (e.g. README, a
# stray .sql scratch file) are ignored by discovery.
_MIGRATION_FILENAME_RE = re.compile(r"^(\d+)_([a-z0-9_]+)\.sql$")

# The migration ledger is the machinery's own bookkeeping table. It is also
# declared in schema.sql for fresh-DB builds; both definitions MUST match (the
# init==migrations identity test enforces it, §V.108).

_ENSURE_MIGRATIONS_LEDGER_SQL = """\
CREATE TABLE IF NOT EXISTS schema_migrations (
    version            INTEGER PRIMARY KEY,
    name               TEXT NOT NULL,
    applied_at         TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    mailpilot_version  TEXT NOT NULL
);"""


def _new_id() -> str:
    """Generate a UUIDv7 string for use as a primary key."""
    return str(uuid.uuid7())


def _build_update(
    table: str,
    updates: dict[str, object],
    where: Composed | SQL,
) -> Composed:
    """Build a dynamic UPDATE ... SET ... WHERE ... RETURNING * query.

    Args:
        table: Table name.
        updates: Column-name to value mapping for SET clause.
        where: WHERE clause (psycopg.sql fragment).

    Returns:
        Composed SQL query ready for execute().
    """
    set_parts = [SQL("{} = {}").format(Identifier(k), Placeholder(k)) for k in updates]
    set_clause = SQL(", ").join([*set_parts, SQL("updated_at = CURRENT_TIMESTAMP")])
    return SQL("UPDATE {} SET {} WHERE {} RETURNING *").format(
        Identifier(table), set_clause, where
    )


def _empty_to_none(value: str | None) -> str | None:
    """Map empty strings to NULL for optional TEXT columns."""
    if value is None or value == "":
        return None
    return value
