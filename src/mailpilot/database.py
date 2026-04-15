"""PostgreSQL database for CRM persistence."""

from pathlib import Path
from typing import Any, cast

import psycopg
from psycopg.rows import dict_row

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def initialize_database(database_url: str) -> psycopg.Connection[dict[str, Any]]:
    """Open a PostgreSQL connection and apply the schema.

    Args:
        database_url: PostgreSQL connection URL.

    Returns:
        Open database connection with schema applied.
    """
    try:
        connection = cast(
            psycopg.Connection[dict[str, Any]],
            psycopg.connect(database_url, row_factory=dict_row, autocommit=True),  # type: ignore[arg-type]
        )
    except psycopg.OperationalError as exc:
        db_name = database_url.rsplit("/", 1)[-1]
        message = str(exc)
        if "does not exist" in message:
            hint = f"run 'createdb {db_name}' to create it"
        elif "Connection refused" in message:
            hint = "is PostgreSQL running? check your system's service manager"
        else:
            hint = "check your database_url setting"
        raise SystemExit(f"database connection failed: {hint}") from None
    schema_sql = SCHEMA_PATH.read_text()
    connection.execute(schema_sql)  # type: ignore[arg-type]
    connection.autocommit = False
    return connection


# -- Status --------------------------------------------------------------------


def get_status_counts(
    connection: psycopg.Connection[dict[str, Any]],
) -> dict[str, object]:
    """Get summary counts for the status command.

    Args:
        connection: Open database connection.

    Returns:
        Dict with accounts, companies, contacts, workflows, emails counts.
    """
    row = connection.execute(
        """\
        SELECT
            (SELECT COUNT(*) FROM account) AS accounts,
            (SELECT COUNT(*) FROM company) AS companies,
            (SELECT COUNT(*) FROM contact) AS contacts,
            (SELECT COUNT(*) FROM workflow) AS workflows,
            (SELECT COUNT(*) FROM email) AS emails
        FROM (SELECT 1) AS _dummy
        """
    ).fetchone()
    return {
        "accounts": row["accounts"],  # type: ignore[index]
        "companies": row["companies"],  # type: ignore[index]
        "contacts": row["contacts"],  # type: ignore[index]
        "workflows": row["workflows"],  # type: ignore[index]
        "emails": row["emails"],  # type: ignore[index]
    }
