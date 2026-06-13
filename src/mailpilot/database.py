"""PostgreSQL database for CRM persistence.

Single flat module with section headers per entity. All CRUD functions follow
consistent signatures and return domain models from ``models.py``.

Convention:
    create_X(connection, ...) -> X
    get_X(connection, id) -> X | None
    list_X(connection, ...) -> list[X]
    update_X(connection, id, ...) -> X
"""

import hashlib
import re
import urllib.parse
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any, cast

import logfire
import psycopg
from psycopg.rows import dict_row
from psycopg.sql import SQL, Composable, Composed, Identifier, Placeholder
from psycopg.types.json import Json

from mailpilot.models import (
    Account,
    AccountSummary,
    Activity,
    ActivitySummary,
    Company,
    CompanyProfile,
    CompanySummary,
    CompanyView,
    Contact,
    ContactSummary,
    ContactView,
    Email,
    EmailSummary,
    Enrollment,
    EnrollmentSummary,
    EnrollmentWithOutcome,
    Note,
    NoteSummary,
    SchemaMetadata,
    SyncStatus,
    Tag,
    Task,
    TaskSummary,
    Workflow,
    WorkflowSummary,
)
from mailpilot.operator_log import operator_event
from mailpilot.settings import SECRET_KEYS, Settings

_MAILPILOT_VERSION = _pkg_version("mailpilot")

_INLINE_NOTES_CAP = 10

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


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


def _compute_schema_hash(sql: str) -> str:
    """Hash schema.sql modulo comments and whitespace (§V.19).

    Strips `--` line comments, collapses whitespace runs to single spaces,
    then takes a sha256. Reformatting (added comments, blank-line shuffles)
    leaves the hash stable; column / table changes flip it.
    """
    normalized = re.sub(r"--[^\n]*", "", sql)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _read_schema_metadata(
    connection: psycopg.Connection[dict[str, Any]],
) -> SchemaMetadata | None:
    """Return the singleton `schema_metadata` row, or None if missing.

    Row-missing and table-missing both collapse to None per §V.18 — both
    are "drift" branches from the caller's view.
    """
    try:
        row = connection.execute(
            "SELECT mailpilot_version, schema_hash, applied_at "
            "FROM schema_metadata WHERE id = 1"
        ).fetchone()
    except psycopg.errors.UndefinedTable:
        # Even under autocommit=True, psycopg3 surfaces a failed-transaction
        # state on the following query unless the rolled-back state is cleared.
        connection.rollback()
        return None
    if row is None:
        return None
    return SchemaMetadata.model_validate(row)


def initialize_database(database_url: str) -> psycopg.Connection[dict[str, Any]]:
    """Open a PostgreSQL connection and apply the schema.

    Args:
        database_url: PostgreSQL connection URL.

    Returns:
        Open database connection with schema applied.
    """
    db_name = database_url.rsplit("/", 1)[-1]
    try:
        connection = cast(
            psycopg.Connection[dict[str, Any]],
            psycopg.connect(database_url, row_factory=dict_row, autocommit=True),  # type: ignore[arg-type]
        )
    except psycopg.OperationalError as exc:
        message = str(exc)
        if "does not exist" in message:
            hint = f"run 'createdb {db_name}' to create it"
        elif "Connection refused" in message:
            hint = "is PostgreSQL running? check your system's service manager"
        else:
            hint = "check your database_url setting"
        logfire.exception("database connection failed", database=db_name, hint=hint)
        operator_event("error", source="database.connect", message=str(exc))
        raise SystemExit(f"database connection failed: {hint}") from None
    # Skip the schema apply when the database is already initialized.
    # schema.sql contains DROP TRIGGER + CREATE TRIGGER on the task table
    # which takes AccessExclusiveLock and deadlocks against the sync loop's
    # INSERT INTO task (RowExclusiveLock). New columns/tables added to the
    # schema still flow through the canonical `make clean` workflow, which
    # drops everything and re-applies on an empty database.
    schema_sql = SCHEMA_PATH.read_text()
    current_hash = _compute_schema_hash(schema_sql)
    probe = connection.execute("SELECT to_regclass('account') AS oid").fetchone()  # type: ignore[union-attr]
    if probe is None or probe.get("oid") is None:
        connection.execute(schema_sql)  # type: ignore[arg-type]
        connection.execute(
            "INSERT INTO schema_metadata (mailpilot_version, schema_hash) "
            "VALUES (%s, %s)",
            (_MAILPILOT_VERSION, current_hash),
        )
    else:
        recorded = _read_schema_metadata(connection)
        if recorded is None or recorded.schema_hash != current_hash:
            recorded_version = recorded.mailpilot_version if recorded else None
            recorded_hash = recorded.schema_hash if recorded else None
            logfire.warn(
                "schema drift detected",
                recorded_version=recorded_version,
                current_version=_MAILPILOT_VERSION,
                recorded_hash=recorded_hash,
                current_hash=current_hash,
            )
            operator_event(
                "schema.drift",
                recorded_version=recorded_version,
                current_version=_MAILPILOT_VERSION,
                recorded_hash=recorded_hash,
                current_hash=current_hash,
            )
    connection.autocommit = False
    return connection


# -- Status --------------------------------------------------------------------


def _scrub_database_url(url: str) -> str:
    """Reduce a PostgreSQL URL to ``scheme://host[:port]/db``, dropping creds.

    Uses ``urllib.parse.urlsplit`` so a passwordless URL round-trips unchanged
    and a credentialed URL loses its userinfo netloc segment. Path is preserved
    verbatim (the leading ``/`` is part of urlsplit's ``path``).
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port is not None else host
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _schema_block(
    connection: psycopg.Connection[dict[str, Any]],
) -> dict[str, object]:
    """Return the `schema` block shape per §V.18: {hash, applied_at, drift}.

    Drift branches (row missing, table missing, hash mismatch) all surface
    ``drift: true`` with whatever recorded values exist (or ``null``).
    """
    current_hash = _compute_schema_hash(SCHEMA_PATH.read_text())
    recorded = _read_schema_metadata(connection)
    if recorded is None:
        return {"hash": None, "applied_at": None, "drift": True}
    drift = recorded.schema_hash != current_hash
    return {
        "hash": recorded.schema_hash,
        "applied_at": recorded.applied_at.isoformat(),
        "drift": drift,
    }


def _sync_loop_block(
    connection: psycopg.Connection[dict[str, Any]],
) -> dict[str, object] | None:
    """Return the `sync_loop` block per §V.11, or None when not running."""
    row = connection.execute(
        """\
        SELECT
            pid,
            started_at,
            heartbeat_at,
            EXTRACT(EPOCH FROM (now() - heartbeat_at))::int AS heartbeat_age_seconds
        FROM sync_status WHERE id = 'singleton'
        """
    ).fetchone()
    if row is None:
        return None
    return {
        "running": True,
        "pid": row["pid"],  # type: ignore[index]
        "started_at": row["started_at"].isoformat(),  # type: ignore[index]
        "heartbeat_at": row["heartbeat_at"].isoformat(),  # type: ignore[index]
        "heartbeat_age_seconds": row["heartbeat_age_seconds"],  # type: ignore[index]
    }


def _accounts_block(
    connection: psycopg.Connection[dict[str, Any]],
) -> list[dict[str, object]]:
    """Return per-account status rows per §V.11.

    Server-computed ages (``last_synced_age_seconds``, ``watch_expires_in_hours``)
    avoid Python clock skew; null inputs → null ages.
    """
    rows = connection.execute(
        """\
        SELECT
            id,
            email,
            (disabled_reason IS NOT NULL) AS disabled,
            gmail_history_id,
            watch_expiration,
            last_synced_at,
            EXTRACT(EPOCH FROM (now() - last_synced_at))::int
                AS last_synced_age_seconds,
            (EXTRACT(EPOCH FROM (watch_expiration - now()))::int / 3600)
                AS watch_expires_in_hours
        FROM account
        ORDER BY email
        """
    ).fetchall()
    accounts: list[dict[str, object]] = []
    for row in rows:
        watch_expiration = row["watch_expiration"]
        last_synced_at = row["last_synced_at"]
        accounts.append(
            {
                "id": row["id"],
                "email": row["email"],
                "disabled": row["disabled"],
                "last_synced_at": (
                    last_synced_at.isoformat() if last_synced_at is not None else None
                ),
                "last_synced_age_seconds": row["last_synced_age_seconds"],
                "gmail_history_id": row["gmail_history_id"],
                "watch_expiration": (
                    watch_expiration.isoformat()
                    if watch_expiration is not None
                    else None
                ),
                "watch_expires_in_hours": row["watch_expires_in_hours"],
            }
        )
    return accounts


def _tasks_block(
    connection: psycopg.Connection[dict[str, Any]],
) -> dict[str, object]:
    """Return task-queue aggregates per §V.11.

    Single SQL statement: pending counts split by due-vs-future, oldest
    pending age (due-only), max attempt_count among pending, and failed_24h.
    """
    row = connection.execute(
        """\
        SELECT
            (SELECT count(*) FROM task WHERE status = 'pending') AS pending,
            (SELECT count(*) FROM task
             WHERE status = 'pending' AND scheduled_at > now())
                AS scheduled_future,
            (SELECT EXTRACT(EPOCH FROM (now() - min(scheduled_at)))::int
             FROM task
             WHERE status = 'pending' AND scheduled_at <= now())
                AS oldest_pending_age_seconds,
            (SELECT max(attempt_count) FROM task WHERE status = 'pending')
                AS max_attempt_count_pending,
            (SELECT count(*) FROM task
             WHERE status = 'failed'
               AND completed_at >= now() - interval '24 hours')
                AS failed_24h
        """
    ).fetchone()
    return {
        "pending": row["pending"],  # type: ignore[index]
        "failed_24h": row["failed_24h"],  # type: ignore[index]
        "scheduled_future": row["scheduled_future"],  # type: ignore[index]
        "oldest_pending_age_seconds": row["oldest_pending_age_seconds"],  # type: ignore[index]
        "max_attempt_count_pending": row["max_attempt_count_pending"],  # type: ignore[index]
    }


def _counts_block(
    connection: psycopg.Connection[dict[str, Any]],
) -> dict[str, object]:
    """Return entity counts (sanity tail per §V.11). ``accounts`` excluded."""
    row = connection.execute(
        """\
        SELECT
            (SELECT COUNT(*) FROM company) AS companies,
            (SELECT COUNT(*) FROM contact) AS contacts,
            (SELECT COUNT(*) FROM workflow) AS workflows,
            (SELECT COUNT(*) FROM email) AS emails,
            (SELECT COUNT(*) FROM activity) AS activities,
            (SELECT COUNT(*) FROM tag) AS tags,
            (SELECT COUNT(*) FROM note) AS notes
        """
    ).fetchone()
    return {
        "companies": row["companies"],  # type: ignore[index]
        "contacts": row["contacts"],  # type: ignore[index]
        "workflows": row["workflows"],  # type: ignore[index]
        "emails": row["emails"],  # type: ignore[index]
        "activities": row["activities"],  # type: ignore[index]
        "tags": row["tags"],  # type: ignore[index]
        "notes": row["notes"],  # type: ignore[index]
    }


def _config_block(settings: Settings) -> dict[str, object]:
    """Return the `config` block per §V.11.

    Secret keys collapsed to ``*_set: bool`` so values never reach agent
    transcripts or Logfire spans. ``database_url`` is scrubbed of userinfo
    rather than dropped entirely so operators can still see host/db/port.
    """
    assert "anthropic_api_key" in SECRET_KEYS
    assert "logfire_token" in SECRET_KEYS
    assert "database_url" in SECRET_KEYS
    return {
        "anthropic_api_key_set": bool(settings.anthropic_api_key),
        "anthropic_model": settings.anthropic_model,
        "logfire_environment": settings.logfire_environment,
        "logfire_token_set": bool(settings.logfire_token),
        "google_pubsub_topic": settings.google_pubsub_topic,
        "google_pubsub_subscription": settings.google_pubsub_subscription,
        "database_url": _scrub_database_url(str(settings.database_url)),
    }


def get_status_payload(
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
) -> dict[str, object]:
    """Build the full ``mailpilot status`` payload per §V.11.

    Top-level blocks (``version``, ``schema``, ``sync_loop``, ``accounts``,
    ``tasks``, ``config``, ``counts``) are layout-stable for LLM-agent
    troubleshooting; secrets are collapsed to booleans and the database URL
    is scrubbed (§V.11).

    Args:
        connection: Open database connection.
        settings: Loaded settings (callers pass ``get_settings()``).

    Returns:
        Dict matching the §V.11 envelope, ready to wrap as
        ``{"status": <payload>, "ok": true}``.
    """
    with logfire.span("db.status.payload"):
        return {
            "version": _MAILPILOT_VERSION,
            "schema": _schema_block(connection),
            "sync_loop": _sync_loop_block(connection),
            "accounts": _accounts_block(connection),
            "tasks": _tasks_block(connection),
            "config": _config_block(settings),
            "counts": _counts_block(connection),
        }


# -- Account -------------------------------------------------------------------


def create_account(
    connection: psycopg.Connection[dict[str, Any]],
    email: str,
    display_name: str = "",
) -> Account | None:
    """Create a new account.

    Uses ``ON CONFLICT (email) DO NOTHING`` per §V.16(+) so callers can
    safely re-invoke without catching ``UniqueViolation``. Returns ``None``
    when the row already exists.

    Args:
        connection: Open database connection.
        email: Gmail address.
        display_name: Display name for the account.

    Returns:
        Created account, or ``None`` if an account with this email already
        existed (concurrent worker won or operator re-create).
    """
    row = connection.execute(
        """\
        INSERT INTO account (id, email, display_name)
        VALUES (%(id)s, %(email)s, %(display_name)s)
        ON CONFLICT (email) DO NOTHING
        RETURNING *
        """,
        {"id": _new_id(), "email": email, "display_name": display_name},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Account.model_validate(row)


def get_account(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
) -> Account | None:
    """Get an account by ID.

    Args:
        connection: Open database connection.
        account_id: Account ID.

    Returns:
        Account if found, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM account WHERE id = %(id)s",
        {"id": account_id},
    ).fetchone()
    if row is None:
        return None
    return Account.model_validate(row)


def list_accounts(
    connection: psycopg.Connection[dict[str, Any]],
    limit: int = 100,
    since: str | None = None,
) -> list[AccountSummary]:
    """List accounts as summaries (identify/filter/order fields only).

    Internal callers needing the full record (e.g. ``gmail_history_id``,
    ``watch_expiration``) must hydrate via ``get_account()`` per id.

    Args:
        connection: Open database connection.
        limit: Maximum results.
        since: ISO datetime lower bound on ``created_at``.

    Returns:
        List of account summaries ordered by creation time.
    """
    conditions: list[Composed | SQL] = []
    params: dict[str, object] = {"limit": limit}
    if since is not None:
        conditions.append(SQL("created_at >= %(since)s"))
        params["since"] = since
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT id, email, display_name, last_synced_at, created_at "
        "FROM account {where} ORDER BY created_at LIMIT %(limit)s"
    ).format(where=where)
    rows = connection.execute(query, params).fetchall()
    return [AccountSummary.model_validate(row) for row in rows]


def get_account_by_email(
    connection: psycopg.Connection[dict[str, Any]],
    email: str,
) -> Account | None:
    """Get an account by email address (case-insensitive).

    Args:
        connection: Open database connection.
        email: Email address to look up.

    Returns:
        Account if found, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM account WHERE LOWER(email) = LOWER(%(email)s)",
        {"email": email},
    ).fetchone()
    if row is None:
        return None
    return Account.model_validate(row)


def update_account(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    **fields: object,
) -> Account | None:
    """Update an account by ID.

    Args:
        connection: Open database connection.
        account_id: Account ID.
        **fields: Fields to update (must be valid Account field names).

    Returns:
        Updated account, or None if not found.
    """
    allowed = set(Account.model_fields) - {"id", "created_at"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_account(connection, account_id)
    updates["id"] = account_id
    query = _build_update("account", updates, SQL("id = %(id)s"))
    row = connection.execute(query, updates).fetchone()
    connection.commit()
    if row is None:
        return None
    return Account.model_validate(row)


# -- Company -------------------------------------------------------------------


def create_company(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    domain: str,
) -> Company | None:
    """Create a new company.

    Uses ``ON CONFLICT (domain) DO NOTHING`` per §V.16(+) so callers can
    safely re-invoke without catching ``UniqueViolation``. Returns ``None``
    when the row already exists.

    Args:
        connection: Open database connection.
        name: Company name.
        domain: Primary domain.

    Returns:
        Created company, or ``None`` if a company with this domain already
        existed.
    """
    row = connection.execute(
        """\
        INSERT INTO company (id, name, domain)
        VALUES (%(id)s, %(name)s, %(domain)s)
        ON CONFLICT (domain) DO NOTHING
        RETURNING *
        """,
        {"id": _new_id(), "name": name, "domain": domain},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Company.model_validate(row)


def get_company(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
) -> Company | None:
    """Get a company by ID.

    Args:
        connection: Open database connection.
        company_id: Company ID.

    Returns:
        Company if found, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM company WHERE id = %(id)s",
        {"id": company_id},
    ).fetchone()
    if row is None:
        return None
    return Company.model_validate(row)


def list_companies(
    connection: psycopg.Connection[dict[str, Any]],
    limit: int = 100,
    since: str | None = None,
    has_profile: bool | None = None,
) -> list[CompanySummary]:
    """List companies as summaries.

    Args:
        connection: Open database connection.
        limit: Maximum results.
        since: ISO datetime lower bound on ``created_at``.
        has_profile: ``True`` returns only rows where ``profile IS NOT NULL``;
            ``False`` returns only rows where ``profile IS NULL``; ``None``
            (default) returns all rows. Per §V.72 operator filter surface.

    Returns:
        List of company summaries ordered by name.
    """
    conditions: list[Composed | SQL] = []
    params: dict[str, object] = {"limit": limit}
    if since is not None:
        conditions.append(SQL("created_at >= %(since)s"))
        params["since"] = since
    if has_profile is True:
        conditions.append(SQL("profile IS NOT NULL"))
    elif has_profile is False:
        conditions.append(SQL("profile IS NULL"))
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT id, name, domain, (profile IS NOT NULL) AS has_profile, created_at "
        "FROM company {where} ORDER BY LOWER(name) LIMIT %(limit)s"
    ).format(where=where)
    rows = connection.execute(query, params).fetchall()
    return [CompanySummary.model_validate(row) for row in rows]


def search_companies(
    connection: psycopg.Connection[dict[str, Any]],
    query: str,
    limit: int = 100,
) -> list[CompanySummary]:
    """Search companies by name or domain.

    Args:
        connection: Open database connection.
        query: Search term (matched against name and domain).
        limit: Maximum number of results.

    Returns:
        Matching company summaries ordered by name.
    """
    pattern = f"%{query}%"
    rows = connection.execute(
        """\
        SELECT id, name, domain, (profile IS NOT NULL) AS has_profile, created_at
        FROM company
        WHERE LOWER(name) LIKE LOWER(%(pattern)s)
           OR LOWER(domain) LIKE LOWER(%(pattern)s)
        ORDER BY LOWER(name)
        LIMIT %(limit)s
        """,
        {"pattern": pattern, "limit": limit},
    ).fetchall()
    return [CompanySummary.model_validate(row) for row in rows]


def get_company_by_domain(
    connection: psycopg.Connection[dict[str, Any]],
    domain: str,
) -> Company | None:
    """Get a company by primary domain.

    Args:
        connection: Open database connection.
        domain: Company domain (exact match on UNIQUE column).

    Returns:
        Company if found, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM company WHERE domain = %(domain)s",
        {"domain": domain},
    ).fetchone()
    if row is None:
        return None
    return Company.model_validate(row)


def update_company(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
    **fields: object,
) -> Company | None:
    """Update a company by ID.

    ``profile`` (if present and non-None) is validated via
    ``CompanyProfile.model_validate`` per §V.72 and persisted as JSONB; an
    invalid payload raises ``pydantic.ValidationError`` which the
    ``cli_mutation`` boundary translates to a ``validation_error`` envelope.

    Args:
        connection: Open database connection.
        company_id: Company ID.
        **fields: Fields to update (must be valid Company field names).

    Returns:
        Updated company, or None if not found.
    """
    allowed = set(Company.model_fields) - {"id", "created_at"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if "profile" in updates and updates["profile"] is not None:
        validated = CompanyProfile.model_validate(updates["profile"])
        updates["profile"] = Json(validated.model_dump(exclude_unset=True))
    if not updates:
        return get_company(connection, company_id)
    updates["id"] = company_id
    query = _build_update("company", updates, SQL("id = %(id)s"))
    row = connection.execute(query, updates).fetchone()
    connection.commit()
    if row is None:
        return None
    return Company.model_validate(row)


# -- Contact -------------------------------------------------------------------


def create_contact(
    connection: psycopg.Connection[dict[str, Any]],
    email: str,
    company_id: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    title: str | None = None,
    email_confidence: int | None = None,
) -> Contact | None:
    """Create a new contact.

    Uses ``ON CONFLICT (email) DO NOTHING`` per §V.16(+) so callers can
    safely re-invoke without catching ``UniqueViolation``. Returns ``None``
    when the row already exists (sync-path callers re-fetch via
    ``get_contact_by_email``).

    Args:
        connection: Open database connection.
        email: Contact email address.
        company_id: Optional company FK.
        first_name: Optional first name.
        last_name: Optional last name.
        title: Optional role label (lead-metadata, §V.95).
        email_confidence: Optional deliverability score 0-100; ``None`` =
            Bouncer-unknown (§V.95). Schema CHECK enforces the range.

    Returns:
        Created contact, or ``None`` if a contact with this email already
        existed.
    """
    row = connection.execute(
        """\
        INSERT INTO contact (id, email, company_id, first_name, last_name,
                             title, email_confidence)
        VALUES (%(id)s, %(email)s, %(company_id)s,
                %(first_name)s, %(last_name)s,
                %(title)s, %(email_confidence)s)
        ON CONFLICT (email) DO NOTHING
        RETURNING *
        """,
        {
            "id": _new_id(),
            "email": email,
            "company_id": company_id,
            "first_name": first_name,
            "last_name": last_name,
            "title": title,
            "email_confidence": email_confidence,
        },
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Contact.model_validate(row)


def get_contact(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
) -> Contact | None:
    """Get a contact by ID.

    Args:
        connection: Open database connection.
        contact_id: Contact ID.

    Returns:
        Contact if found, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM contact WHERE id = %(id)s",
        {"id": contact_id},
    ).fetchone()
    if row is None:
        return None
    return Contact.model_validate(row)


def get_contact_by_email(
    connection: psycopg.Connection[dict[str, Any]],
    email: str,
) -> Contact | None:
    """Get a contact by email address.

    Args:
        connection: Open database connection.
        email: Contact email address.

    Returns:
        Contact if found, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM contact WHERE email = %(email)s",
        {"email": email},
    ).fetchone()
    if row is None:
        return None
    return Contact.model_validate(row)


def create_or_get_contact_by_email(
    connection: psycopg.Connection[dict[str, Any]],
    email: str,
    first_name: str | None = None,
    last_name: str | None = None,
) -> Contact:
    """Return an existing contact by email, creating one if missing.

    If the contact already exists, backfills ``first_name`` / ``last_name``
    only when the stored value is NULL and the caller provided one. Existing
    non-null names are never overwritten.

    Used during inbound sync to resolve a ``From`` header to a contact row
    without forcing callers to branch on existence.

    Args:
        connection: Open database connection.
        email: Contact email address.
        first_name: Optional first name (from From header display name).
        last_name: Optional last name (from From header display name).

    Returns:
        Existing or newly created contact.
    """
    existing = get_contact_by_email(connection, email)
    if existing is not None:
        backfill: dict[str, object] = {}
        if existing.first_name is None and first_name is not None:
            backfill["first_name"] = first_name
        if existing.last_name is None and last_name is not None:
            backfill["last_name"] = last_name
        if not backfill:
            return existing
        updated = update_contact(connection, existing.id, **backfill)
        return updated if updated is not None else existing
    created = create_contact(
        connection,
        email=email,
        first_name=first_name,
        last_name=last_name,
    )
    if created is not None:
        return created
    # Concurrent worker won the race per §V.16(+); re-fetch the existing row.
    racer = get_contact_by_email(connection, email)
    assert racer is not None, (
        f"create_contact returned None for {email!r} but no row was found on re-fetch"
    )
    return racer


def get_contacts_by_emails(
    connection: psycopg.Connection[dict[str, Any]],
    emails: Iterable[str],
) -> dict[str, Contact]:
    """Fetch contacts for a batch of email addresses in one round-trip.

    Used by the sync pipeline to eliminate per-message contact lookups. The
    caller should feed in the set of distinct sender addresses from a batch
    of Gmail messages.

    Args:
        connection: Open database connection.
        emails: Email addresses to look up. Duplicates are tolerated.

    Returns:
        Mapping from email to Contact for every input email that has an
        existing row. Missing emails are simply absent from the dict.
    """
    unique = list(set(emails))
    if not unique:
        return {}
    rows = connection.execute(
        "SELECT * FROM contact WHERE email = ANY(%(emails)s)",
        {"emails": unique},
    ).fetchall()
    return {row["email"]: Contact.model_validate(row) for row in rows}


def create_contacts_bulk(
    connection: psycopg.Connection[dict[str, Any]],
    emails: Iterable[str],
) -> dict[str, Contact]:
    """Ensure a contact row exists for every input email, in one round-trip.

    Inserts any missing rows with ``ON CONFLICT (email) DO NOTHING``, then
    re-reads every requested email so the returned mapping covers rows
    that were already present (either pre-existing or inserted by a
    concurrent transaction). Safe to run in parallel from multiple sync
    workers; no ``UniqueViolation`` can escape.

    Args:
        connection: Open database connection.
        emails: Email addresses to ensure. Duplicates are tolerated.

    Returns:
        Mapping from email to Contact for every input email.
    """
    unique = list(set(emails))
    if not unique:
        return {}
    ids = [_new_id() for _ in unique]
    rows = connection.execute(
        """\
        INSERT INTO contact (id, email)
        SELECT id, email
        FROM unnest(%(ids)s::text[], %(emails)s::text[])
             AS t(id, email)
        ON CONFLICT (email) DO NOTHING
        RETURNING *
        """,
        {"ids": ids, "emails": unique},
    ).fetchall()
    connection.commit()
    inserted = {row["email"]: Contact.model_validate(row) for row in rows}
    # Re-fetch any row that was not inserted by this transaction. These
    # cover both pre-existing rows and rows inserted by a concurrent
    # worker (ON CONFLICT DO NOTHING swallows those silently).
    remaining = [email for email in unique if email not in inserted]
    if remaining:
        existing = get_contacts_by_emails(connection, remaining)
        inserted.update(existing)
    return inserted


def list_contacts(
    connection: psycopg.Connection[dict[str, Any]],
    limit: int = 100,
    company_id: str | None = None,
    since: str | None = None,
    include_disabled: bool = False,
    max_email_confidence: int | None = None,
    min_email_confidence: int | None = None,
    company_domain: str | None = None,
    title: str | None = None,
) -> list[ContactSummary]:
    """List contacts as summaries with optional filters.

    Joins ``company`` once (LEFT JOIN) so each summary carries
    ``company_domain`` without an N+1 lookup per §V.5; ``company_domain``
    is NULL when ``company_id`` is NULL.

    Args:
        connection: Open database connection.
        limit: Maximum results.
        company_id: Filter by company ID.
        since: ISO datetime lower bound on ``created_at``.
        include_disabled: When False (default), only contacts with
            ``disabled_reason IS NULL`` are returned.
        max_email_confidence: When set, surfaces only rows with
            ``email_confidence <= N`` for cross-run operator review of
            low-score (high-risk) leads (§V.95); NULL-score rows are
            excluded (no signal to review).
        min_email_confidence: When set, surfaces only rows with
            ``email_confidence >= N``; composes with
            ``max_email_confidence`` into a closed range. NULL-score rows
            are excluded by the lower bound (§V.95).
        company_domain: When set, matches the joined ``company.domain``.
        title: When set, a case-insensitive substring (ILIKE) match on
            ``contact.title``.

    Returns:
        List of contact summaries ordered by email.
    """
    conditions: list[SQL] = []
    params: dict[str, object] = {"limit": limit}
    if company_id is not None:
        conditions.append(SQL("c.company_id = %(company_id)s"))
        params["company_id"] = company_id
    if since is not None:
        conditions.append(SQL("c.created_at >= %(since)s"))
        params["since"] = since
    if not include_disabled:
        conditions.append(SQL("c.disabled_reason IS NULL"))
    if max_email_confidence is not None:
        conditions.append(SQL("c.email_confidence <= %(max_email_confidence)s"))
        params["max_email_confidence"] = max_email_confidence
    if min_email_confidence is not None:
        conditions.append(SQL("c.email_confidence >= %(min_email_confidence)s"))
        params["min_email_confidence"] = min_email_confidence
    if company_domain is not None:
        conditions.append(SQL("co.domain = %(company_domain)s"))
        params["company_domain"] = company_domain
    if title is not None:
        conditions.append(SQL("c.title ILIKE %(title)s"))
        params["title"] = f"%{title}%"
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT c.id, c.email, c.first_name, c.last_name, c.title, "
        "c.company_id, co.domain AS company_domain, "
        "c.email_confidence, c.disabled_reason, c.created_at "
        "FROM contact c LEFT JOIN company co ON c.company_id = co.id "
        "{} ORDER BY c.email LIMIT %(limit)s"
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [ContactSummary.model_validate(row) for row in rows]


def search_contacts(
    connection: psycopg.Connection[dict[str, Any]],
    query: str,
    limit: int = 100,
) -> list[ContactSummary]:
    """Search contacts by email or name.

    Args:
        connection: Open database connection.
        query: Search term.
        limit: Maximum number of results.

    Returns:
        Matching contact summaries ordered by email. Each carries
        ``title`` + ``company_domain`` (LEFT JOIN company per §V.5),
        mirroring ``list_contacts``.
    """
    pattern = f"%{query}%"
    rows = connection.execute(
        """\
        SELECT c.id, c.email, c.first_name, c.last_name, c.title,
               c.company_id, co.domain AS company_domain,
               c.email_confidence, c.disabled_reason, c.created_at
        FROM contact c
        LEFT JOIN company co ON c.company_id = co.id
        WHERE LOWER(c.email) LIKE LOWER(%(pattern)s)
           OR LOWER(COALESCE(c.first_name, '')) LIKE LOWER(%(pattern)s)
           OR LOWER(COALESCE(c.last_name, '')) LIKE LOWER(%(pattern)s)
        ORDER BY c.email
        LIMIT %(limit)s
        """,
        {"pattern": pattern, "limit": limit},
    ).fetchall()
    return [ContactSummary.model_validate(row) for row in rows]


def update_contact(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
    **fields: object,
) -> Contact | None:
    """Update a contact by ID.

    Args:
        connection: Open database connection.
        contact_id: Contact ID.
        **fields: Fields to update (must be valid Contact field names).

    Returns:
        Updated contact, or None if not found.
    """
    allowed = set(Contact.model_fields) - {"id", "created_at"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_contact(connection, contact_id)
    updates["id"] = contact_id
    query = _build_update("contact", updates, SQL("id = %(id)s"))
    row = connection.execute(query, updates).fetchone()
    connection.commit()
    if row is None:
        return None
    return Contact.model_validate(row)


def disable_contact(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
    reason: str,
) -> Contact | None:
    """Set a global block on a contact.

    This is a hard block across all workflows. ``send_email`` and
    ``reply_email`` refuse contacts whose ``disabled_reason`` is non-null.
    Any non-empty reason string disables the contact; conventional values
    include ``"bounced: <detail>"`` and ``"unsubscribed: <detail>"``.

    Args:
        connection: Open database connection.
        contact_id: Contact ID.
        reason: Explanation for the block (stored verbatim in
            ``disabled_reason``).

    Returns:
        Updated contact, or None if not found.
    """
    row = connection.execute(
        """\
        UPDATE contact
        SET disabled_reason = %(reason)s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %(id)s
        RETURNING *
        """,
        {"id": contact_id, "reason": reason},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Contact.model_validate(row)


# -- Workflow ------------------------------------------------------------------


def create_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    template: str,
    account_id: str,
    theme: str = "blue",
) -> Workflow | None:
    """Create a new workflow.

    The workflow's ``type`` (``inbound`` / ``outbound``) is derived from
    the template's declared direction -- callers do not pass ``type``.

    Uses ``ON CONFLICT (account_id, name) DO NOTHING`` per §V.16(+) so
    callers can safely re-invoke without catching ``UniqueViolation``.
    Returns ``None`` when a workflow already exists for ``(account_id,
    name)``.

    Args:
        connection: Open database connection.
        name: Workflow name.
        template: Template name (e.g. ``outbound-general``). Drives both
            the agent shape and the workflow's direction.
        account_id: Account FK.
        theme: Email color theme (default "blue").

    Returns:
        Created workflow, or ``None`` if a workflow with this
        ``(account_id, name)`` pair already existed.
    """
    from mailpilot.agent.templates import TEMPLATES

    if template not in TEMPLATES:
        raise ValueError(
            f"unknown workflow template {template!r}; valid: {sorted(TEMPLATES.keys())}"
        )
    direction = TEMPLATES[template].direction  # pyright: ignore[reportArgumentType]
    row = connection.execute(
        """\
        WITH inserted AS (
            INSERT INTO workflow (id, name, template, type, account_id, theme)
            VALUES (
                %(id)s, %(name)s, %(template)s, %(type)s, %(account_id)s, %(theme)s
            )
            ON CONFLICT (account_id, name) DO NOTHING
            RETURNING *
        )
        SELECT inserted.*, account.email AS account_email
        FROM inserted JOIN account ON account.id = inserted.account_id
        """,
        {
            "id": _new_id(),
            "name": name,
            "template": template,
            "type": direction,
            "account_id": account_id,
            "theme": theme,
        },
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Workflow.model_validate(row)


def get_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> Workflow | None:
    """Get a workflow by ID.

    Args:
        connection: Open database connection.
        workflow_id: Workflow ID.

    Returns:
        Workflow if found, None otherwise.
    """
    row = connection.execute(
        """\
        SELECT workflow.*, account.email AS account_email
        FROM workflow JOIN account ON account.id = workflow.account_id
        WHERE workflow.id = %(id)s
        """,
        {"id": workflow_id},
    ).fetchone()
    if row is None:
        return None
    return Workflow.model_validate(row)


def list_workflows(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str | None = None,
    status: str | None = None,
    workflow_type: str | None = None,
    template: str | None = None,
    limit: int = 100,
    since: str | None = None,
) -> list[WorkflowSummary]:
    """List workflows as summaries with optional filters.

    Args:
        connection: Open database connection.
        account_id: Filter by account ID.
        status: Filter by workflow status (e.g., "active").
        workflow_type: Filter by workflow type ("inbound" or "outbound").
        template: Filter by template name.
        limit: Maximum results.
        since: ISO datetime lower bound on ``created_at``.

    Returns:
        List of workflow summaries ordered by creation time.
    """
    conditions: list[SQL] = []
    params: dict[str, object] = {"limit": limit}
    if account_id is not None:
        conditions.append(SQL("workflow.account_id = %(account_id)s"))
        params["account_id"] = account_id
    if status is not None:
        conditions.append(SQL("workflow.status = %(status)s"))
        params["status"] = status
    if workflow_type is not None:
        conditions.append(SQL("workflow.type = %(workflow_type)s"))
        params["workflow_type"] = workflow_type
    if template is not None:
        conditions.append(SQL("workflow.template = %(template)s"))
        params["template"] = template
    if since is not None:
        conditions.append(SQL("workflow.created_at >= %(since)s"))
        params["since"] = since
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT workflow.id, workflow.name, workflow.template, workflow.type, "
        "workflow.account_id, account.email AS account_email, "
        "workflow.status, workflow.created_at "
        "FROM workflow JOIN account ON account.id = workflow.account_id "
        "{} ORDER BY workflow.created_at LIMIT %(limit)s"
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [WorkflowSummary.model_validate(row) for row in rows]


def list_workflows_full(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
) -> list[Workflow]:
    """List all workflows for an account as full rows ordered by name.

    Used by ``workflow export`` to emit a declarative payload keyed on
    ``(account_id, name)`` per §V.63. Ordering by ``name`` makes the
    export output deterministic for diffs and round-trip testing.

    Args:
        connection: Open database connection.
        account_id: Owning account ID.

    Returns:
        Full ``Workflow`` rows ordered by ``name``.
    """
    rows = connection.execute(
        """\
        SELECT workflow.*, account.email AS account_email
        FROM workflow JOIN account ON account.id = workflow.account_id
        WHERE workflow.account_id = %(account_id)s
        ORDER BY workflow.name
        """,
        {"account_id": account_id},
    ).fetchall()
    return [Workflow.model_validate(row) for row in rows]


def search_workflows(
    connection: psycopg.Connection[dict[str, Any]],
    query: str,
    limit: int = 100,
) -> list[WorkflowSummary]:
    """Search workflows by name or objective.

    Args:
        connection: Open database connection.
        query: Search term (matched against name and objective).
        limit: Maximum number of results.

    Returns:
        Matching workflow summaries ordered by name.
    """
    pattern = f"%{query}%"
    rows = connection.execute(
        """\
        SELECT workflow.id, workflow.name, workflow.template, workflow.type,
               workflow.account_id, account.email AS account_email,
               workflow.status, workflow.created_at
        FROM workflow JOIN account ON account.id = workflow.account_id
        WHERE LOWER(workflow.name) LIKE LOWER(%(pattern)s)
           OR LOWER(workflow.objective) LIKE LOWER(%(pattern)s)
        ORDER BY LOWER(workflow.name)
        LIMIT %(limit)s
        """,
        {"pattern": pattern, "limit": limit},
    ).fetchall()
    return [WorkflowSummary.model_validate(row) for row in rows]


def update_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    **fields: object,
) -> Workflow | None:
    """Update a workflow by ID.

    Only ``name``, ``objective``, and ``instructions`` are updatable.
    Status transitions use ``activate_workflow()`` / ``pause_workflow()``.
    ``type`` and ``account_id`` are immutable after creation.

    Args:
        connection: Open database connection.
        workflow_id: Workflow ID.
        **fields: Fields to update.

    Returns:
        Updated workflow, or None if not found.
    """
    allowed = {"name", "objective", "instructions", "theme"}
    if "template" in fields:
        raise ValueError(
            "workflow.template is immutable; "
            "delete and recreate the workflow to change template"
        )
    if "type" in fields:
        raise ValueError(
            "workflow.type is derived from the template at create time "
            "and cannot be updated"
        )
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_workflow(connection, workflow_id)
    updates["id"] = workflow_id
    query = _build_update("workflow", updates, SQL("id = %(id)s"))
    row = connection.execute(query, updates).fetchone()
    connection.commit()
    if row is None:
        return None
    return get_workflow(connection, workflow_id)


def activate_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> Workflow:
    """Transition a workflow to active status.

    Valid transitions: ``draft -> active``, ``paused -> active``.
    Guards: ``objective`` and ``instructions`` must be non-empty.

    Args:
        connection: Open database connection.
        workflow_id: Workflow ID.

    Returns:
        Updated workflow.

    Raises:
        ValueError: If workflow not found, already active, or missing
            objective/instructions.
    """
    workflow = get_workflow(connection, workflow_id)
    if workflow is None:
        raise ValueError(f"workflow {workflow_id} not found")
    if workflow.status == "active":
        raise ValueError("workflow is already active")
    if not workflow.objective.strip():
        raise ValueError("objective must be non-empty to activate")
    if not workflow.instructions.strip():
        raise ValueError("instructions must be non-empty to activate")
    row = connection.execute(
        """\
        WITH updated AS (
            UPDATE workflow
            SET status = 'active', updated_at = CURRENT_TIMESTAMP
            WHERE id = %(id)s
            RETURNING *
        )
        SELECT updated.*, account.email AS account_email
        FROM updated JOIN account ON account.id = updated.account_id
        """,
        {"id": workflow_id},
    ).fetchone()
    connection.commit()
    return Workflow.model_validate(row)


def pause_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> Workflow:
    """Transition a workflow to paused status.

    Valid transition: ``active -> paused``.

    Args:
        connection: Open database connection.
        workflow_id: Workflow ID.

    Returns:
        Updated workflow.

    Raises:
        ValueError: If workflow not found or not active.
    """
    workflow = get_workflow(connection, workflow_id)
    if workflow is None:
        raise ValueError(f"workflow {workflow_id} not found")
    if workflow.status != "active":
        raise ValueError(f"cannot pause workflow in status '{workflow.status}'")
    row = connection.execute(
        """\
        WITH updated AS (
            UPDATE workflow
            SET status = 'paused', updated_at = CURRENT_TIMESTAMP
            WHERE id = %(id)s
            RETURNING *
        )
        SELECT updated.*, account.email AS account_email
        FROM updated JOIN account ON account.id = updated.account_id
        """,
        {"id": workflow_id},
    ).fetchone()
    connection.commit()
    return Workflow.model_validate(row)


# -- Enrollment ----------------------------------------------------------------


def create_enrollment(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> Enrollment | None:
    """Enroll a contact in a workflow.

    Uses ON CONFLICT DO NOTHING so callers can safely re-invoke without
    catching unique-constraint errors. Returns None when the row already
    exists (same pattern as ``create_email``). ``id`` is minted client-side
    per §V.12 (UUIDv7).

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        contact_id: Contact FK.

    Returns:
        Created enrollment, or None if it already existed.
    """
    row = connection.execute(
        """\
        WITH inserted AS (
            INSERT INTO enrollment (id, workflow_id, contact_id)
            VALUES (%(id)s, %(workflow_id)s, %(contact_id)s)
            ON CONFLICT (workflow_id, contact_id) DO NOTHING
            RETURNING *
        )
        SELECT
            inserted.*,
            workflow.name AS workflow_name,
            contact.email AS contact_email,
            TRIM(
                COALESCE(contact.first_name, '')
                || ' '
                || COALESCE(contact.last_name, '')
            ) AS contact_name
        FROM inserted
        JOIN workflow ON workflow.id = inserted.workflow_id
        JOIN contact ON contact.id = inserted.contact_id
        """,
        {"id": _new_id(), "workflow_id": workflow_id, "contact_id": contact_id},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Enrollment.model_validate(row)


def update_enrollment(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
    **fields: object,
) -> Enrollment | None:
    """Update an enrollment by scalar id.

    Args:
        connection: Open database connection.
        enrollment_id: Enrollment ID.
        **fields: Fields to update (status, reason).

    Returns:
        Updated enrollment, or None if not found.
    """
    allowed = {"status", "reason"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_enrollment_by_id(connection, enrollment_id)
    updates["id"] = enrollment_id
    where = SQL("id = %(id)s")
    query = _build_update("enrollment", updates, where)
    row = connection.execute(query, updates).fetchone()
    connection.commit()
    if row is None:
        return None
    return get_enrollment_by_id(connection, enrollment_id)


def get_enrollment(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> Enrollment | None:
    """Get an enrollment by composite ``(workflow_id, contact_id)`` key.

    The ``(workflow_id, contact_id)`` pair remains a UNIQUE constraint
    post-migration to scalar ``id`` so composite-key lookups stay valid for
    the inbound routing and run-loop call sites that already carry the pair.

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        contact_id: Contact FK.

    Returns:
        Enrollment if found, None otherwise.
    """
    row = connection.execute(
        """\
        SELECT
            enrollment.*,
            workflow.name AS workflow_name,
            contact.email AS contact_email,
            TRIM(
                COALESCE(contact.first_name, '')
                || ' '
                || COALESCE(contact.last_name, '')
            ) AS contact_name
        FROM enrollment
        JOIN workflow ON workflow.id = enrollment.workflow_id
        JOIN contact ON contact.id = enrollment.contact_id
        WHERE enrollment.workflow_id = %(workflow_id)s
          AND enrollment.contact_id = %(contact_id)s
        """,
        {"workflow_id": workflow_id, "contact_id": contact_id},
    ).fetchone()
    if row is None:
        return None
    return Enrollment.model_validate(row)


def get_enrollment_by_id(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
) -> Enrollment | None:
    """Get an enrollment by scalar id (§V.12).

    Args:
        connection: Open database connection.
        enrollment_id: Enrollment ID.

    Returns:
        Enrollment if found, None otherwise.
    """
    row = connection.execute(
        """\
        SELECT
            enrollment.*,
            workflow.name AS workflow_name,
            contact.email AS contact_email,
            TRIM(
                COALESCE(contact.first_name, '')
                || ' '
                || COALESCE(contact.last_name, '')
            ) AS contact_name
        FROM enrollment
        JOIN workflow ON workflow.id = enrollment.workflow_id
        JOIN contact ON contact.id = enrollment.contact_id
        WHERE enrollment.id = %(id)s
        """,
        {"id": enrollment_id},
    ).fetchone()
    if row is None:
        return None
    return Enrollment.model_validate(row)


def list_enrollments(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    status: str | None = None,
) -> list[Enrollment]:
    """List enrollments in a workflow with optional status filter.

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        status: Filter by enrollment status.

    Returns:
        List of enrollments.
    """
    params: dict[str, object] = {"workflow_id": workflow_id}
    status_filter = SQL("")
    if status is not None:
        status_filter = SQL("AND enrollment.status = %(status)s")
        params["status"] = status
    query = SQL(
        "SELECT enrollment.*, "
        "workflow.name AS workflow_name, "
        "contact.email AS contact_email, "
        "TRIM(COALESCE(contact.first_name, '') || ' ' "
        "|| COALESCE(contact.last_name, '')) AS contact_name "
        "FROM enrollment "
        "JOIN workflow ON workflow.id = enrollment.workflow_id "
        "JOIN contact ON contact.id = enrollment.contact_id "
        "WHERE enrollment.workflow_id = %(workflow_id)s {} "
        "ORDER BY enrollment.created_at"
    ).format(status_filter)
    rows = connection.execute(query, params).fetchall()
    return [Enrollment.model_validate(row) for row in rows]


def list_enrollments_with_outcomes(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> list[EnrollmentWithOutcome]:
    """List enrollments in a workflow with their latest outcome activity.

    Outcomes (`completed` / `failed`) are timeline-only and do not change
    `enrollment.status` (§V.15). This helper LEFT JOINs the most recent
    `enrollment_completed` / `enrollment_failed` activity per row so the
    agent can answer "has this objective already been satisfied for any
    contact in this workflow?" in a single query.

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.

    Returns:
        List of `EnrollmentWithOutcome`, ordered by enrollment `created_at`.
    """
    rows = connection.execute(
        """\
        SELECT
            e.id,
            e.workflow_id,
            e.contact_id,
            e.status,
            e.reason,
            e.created_at,
            e.updated_at,
            CASE a.type
                WHEN 'enrollment_completed' THEN 'completed'
                WHEN 'enrollment_failed' THEN 'failed'
            END AS latest_outcome,
            COALESCE(a.detail->>'reason', a.summary) AS latest_outcome_reason,
            a.created_at AS latest_outcome_at
        FROM enrollment e
        LEFT JOIN LATERAL (
            SELECT type, summary, detail, created_at
            FROM activity
            WHERE activity.contact_id = e.contact_id
              AND activity.workflow_id = e.workflow_id
              AND activity.type IN ('enrollment_completed', 'enrollment_failed')
            ORDER BY created_at DESC
            LIMIT 1
        ) a ON TRUE
        WHERE e.workflow_id = %(workflow_id)s
        ORDER BY e.created_at
        """,
        {"workflow_id": workflow_id},
    ).fetchall()
    return [EnrollmentWithOutcome.model_validate(row) for row in rows]


def disable_enrollment(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
    reason: str,
) -> Enrollment | None:
    """Soft-disable an enrollment via terminal lifecycle exit (§V.10, §V.15).

    Single transaction: flips ``status='disabled'`` + writes ``disabled_reason``,
    then appends an ``enrollment_disabled`` activity carrying the reason. The
    coupling CHECK on ``enrollment`` rejects empty reasons at the schema level;
    callers MUST validate ``reason.strip() != ""`` upstream for a friendlier
    error envelope.

    Returns the updated row with denormalised parent identifiers (workflow
    name, contact email/name) so the CLI envelope can ship the full
    Enrollment model unchanged. Returns ``None`` when no row matches the id.

    Args:
        connection: Open database connection.
        enrollment_id: Enrollment ID.
        reason: Operator-supplied explanation written to ``disabled_reason``
            and inlined into the ``enrollment_disabled`` activity row.

    Returns:
        Updated ``Enrollment`` (status='disabled'), or ``None`` if not found.
    """
    row = connection.execute(
        """\
        WITH updated AS (
            UPDATE enrollment
            SET status = 'disabled',
                disabled_reason = %(reason)s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %(id)s
            RETURNING *
        )
        SELECT
            updated.*,
            workflow.name AS workflow_name,
            contact.email AS contact_email,
            contact.company_id AS contact_company_id,
            TRIM(
                COALESCE(contact.first_name, '')
                || ' '
                || COALESCE(contact.last_name, '')
            ) AS contact_name
        FROM updated
        JOIN workflow ON workflow.id = updated.workflow_id
        JOIN contact ON contact.id = updated.contact_id
        """,
        {"id": enrollment_id, "reason": reason},
    ).fetchone()
    if row is None:
        connection.commit()
        return None
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, workflow_id, enrollment_id,
            type, summary, detail
        )
        VALUES (
            %(id)s, %(contact_id)s, %(company_id)s, %(workflow_id)s,
            %(enrollment_id)s, 'enrollment_disabled', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "contact_id": row["contact_id"],
            "company_id": row["contact_company_id"],
            "workflow_id": row["workflow_id"],
            "enrollment_id": row["id"],
            "summary": reason,
            "detail": Json({"reason": reason}),
        },
    )
    connection.commit()
    row.pop("contact_company_id", None)
    return Enrollment.model_validate(row)


def list_enrollments_detailed(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str | None = None,
    contact_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    since: str | None = None,
) -> list[EnrollmentSummary]:
    """List enrollments with denormalised contact info as summaries.

    JOINs the contact table to include email and name. Separate from
    ``list_enrollments`` to avoid breaking agent tools which expect
    ``list[Enrollment]``. Both ``workflow_id`` and ``contact_id`` are
    optional independent filters; either or both can be supplied.

    Args:
        connection: Open database connection.
        workflow_id: Optional workflow FK filter.
        contact_id: Optional contact FK filter.
        status: Filter by enrollment status.
        limit: Maximum results.
        since: ISO datetime lower bound on ``e.updated_at``.

    Returns:
        List of enrollment summaries.
    """
    params: dict[str, object] = {"limit": limit}
    where_parts: list[Composed | SQL] = []
    if workflow_id is not None:
        where_parts.append(SQL("e.workflow_id = %(workflow_id)s"))
        params["workflow_id"] = workflow_id
    if contact_id is not None:
        where_parts.append(SQL("e.contact_id = %(contact_id)s"))
        params["contact_id"] = contact_id
    if status is not None:
        where_parts.append(SQL("e.status = %(status)s"))
        params["status"] = status
    if since is not None:
        where_parts.append(SQL("e.updated_at >= %(since)s"))
        params["since"] = since
    where_clause = (
        SQL("WHERE ") + SQL(" AND ").join(where_parts) if where_parts else SQL("")
    )
    query = SQL(
        "SELECT e.id, e.workflow_id, w.name AS workflow_name, "
        "e.contact_id, e.status, e.updated_at, "
        "c.email AS contact_email, "
        "TRIM(COALESCE(c.first_name, '') || ' ' || COALESCE(c.last_name, '')) "
        "AS contact_name "
        "FROM enrollment e "
        "JOIN workflow w ON w.id = e.workflow_id "
        "JOIN contact c ON c.id = e.contact_id "
        "{} "
        "ORDER BY e.updated_at "
        "LIMIT %(limit)s"
    ).format(where_clause)
    rows = connection.execute(query, params).fetchall()
    return [EnrollmentSummary.model_validate(row) for row in rows]


# -- Email ---------------------------------------------------------------------


def create_email(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    direction: str,
    subject: str = "",
    body_text: str = "",
    gmail_message_id: str | None = None,
    gmail_thread_id: str | None = None,
    contact_id: str | None = None,
    workflow_id: str | None = None,
    status: str = "received",
    is_routed: bool = False,
    route_method: str | None = None,
    received_at: datetime | None = None,
    sent_at: datetime | None = None,
    labels: list[str] | None = None,
    rfc2822_message_id: str | None = None,
    in_reply_to: str | None = None,
    references_header: str | None = None,
    sender: str = "",
    recipients: dict[str, list[str]] | None = None,
) -> Email | None:
    """Create a new email record, or return None on gmail_message_id conflict.

    Insert is atomic via ``ON CONFLICT (gmail_message_id) DO NOTHING``, so two
    concurrent workers attempting to store the same Gmail message will never
    raise ``UniqueViolation``: one wins and returns the row, the other
    returns ``None``. Outbound rows with ``gmail_message_id=NULL`` never
    trigger the conflict (NULLs are distinct under a UNIQUE constraint).

    Args:
        connection: Open database connection.
        account_id: Account FK.
        direction: "inbound" or "outbound".
        subject: Email subject.
        body_text: Plain text body.
        gmail_message_id: Gmail message ID.
        gmail_thread_id: Gmail thread ID.
        contact_id: Optional contact FK.
        workflow_id: Optional workflow FK.
        status: Email status ("sent" or "received").
        is_routed: Whether the routing pipeline has processed this email.
        route_method: Persisted routing decision (per §I email projection;
            e.g. ``thread_match``, ``classified``, ``skipped_outside_window``).
        received_at: When Gmail reports the message arrived (UTC datetime).
        sent_at: When the outbound message was handed to Gmail (UTC datetime).
        labels: Gmail label IDs attached to the message.
        rfc2822_message_id: RFC 2822 Message-ID header value.
        in_reply_to: RFC 2822 In-Reply-To header value (parent message id).
        references_header: RFC 2822 References header value (full
            whitespace-separated chain of ancestor message ids). Stored as
            ``references_header`` because ``references`` is a reserved SQL
            keyword.
        sender: Sender email address (lowercase).
        recipients: Recipient addresses grouped by type
            (``{"to": [...], "cc": [...], "bcc": [...]}``)

    Returns:
        Created email, or None if another worker already stored a row with
        the same ``gmail_message_id``.
    """
    row = connection.execute(
        """\
        INSERT INTO email (id, account_id, direction, subject,
            body_text, gmail_message_id, gmail_thread_id,
            contact_id, workflow_id, status, is_routed, route_method,
            received_at, sent_at, labels, rfc2822_message_id,
            in_reply_to, references_header,
            sender, recipients)
        VALUES (%(id)s, %(account_id)s, %(direction)s,
            %(subject)s, %(body_text)s, %(gmail_message_id)s,
            %(gmail_thread_id)s, %(contact_id)s, %(workflow_id)s,
            %(status)s, %(is_routed)s, %(route_method)s,
            %(received_at)s, %(sent_at)s,
            %(labels)s, %(rfc2822_message_id)s,
            %(in_reply_to)s, %(references_header)s,
            %(sender)s, %(recipients)s)
        ON CONFLICT (gmail_message_id) DO NOTHING
        RETURNING *
        """,
        {
            "id": _new_id(),
            "account_id": account_id,
            "direction": direction,
            "subject": subject,
            "body_text": body_text,
            "gmail_message_id": gmail_message_id,
            "gmail_thread_id": gmail_thread_id,
            "contact_id": contact_id,
            "workflow_id": workflow_id,
            "status": status,
            "is_routed": is_routed,
            "route_method": route_method,
            "received_at": received_at,
            "sent_at": sent_at,
            "labels": Json(labels or []),
            "rfc2822_message_id": rfc2822_message_id,
            "in_reply_to": in_reply_to,
            "references_header": references_header,
            "sender": sender,
            "recipients": Json(recipients or {}),
        },
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Email.model_validate(row)


def get_email(
    connection: psycopg.Connection[dict[str, Any]],
    email_id: str,
) -> Email | None:
    """Get an email by ID.

    Args:
        connection: Open database connection.
        email_id: Email ID.

    Returns:
        Email if found, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM email WHERE id = %(id)s",
        {"id": email_id},
    ).fetchone()
    if row is None:
        return None
    return Email.model_validate(row)


def list_emails(
    connection: psycopg.Connection[dict[str, Any]],
    limit: int = 100,
    contact_id: str | None = None,
    account_id: str | None = None,
    since: str | None = None,
    thread_id: str | None = None,
    direction: str | None = None,
    workflow_id: str | None = None,
    status: str | None = None,
    sender: str | None = None,
    recipient: str | None = None,
    route_method: str | None = None,
) -> list[EmailSummary]:
    """List emails as summaries with optional filters.

    Args:
        connection: Open database connection.
        limit: Maximum results.
        contact_id: Filter by contact ID.
        account_id: Filter by account ID.
        since: ISO datetime lower bound for COALESCE(sent_at, received_at).
        thread_id: Filter by Gmail thread ID.
        direction: Filter by direction ("inbound" or "outbound").
        workflow_id: Filter by workflow ID.
        status: Filter by email status ("sent", "received", "bounced").
        sender: Filter by sender email address (case-insensitive).
        recipient: Filter by recipient address in recipients JSONB
            (case-insensitive, matches to/cc/bcc).
        route_method: Filter by persisted routing decision (per
            §I email projection).

    Returns:
        List of email summaries ordered by ``COALESCE(sent_at, received_at)``
        descending -- the same expression used by the ``since`` filter, so
        operators can page newest-first using a timestamp visible in
        ``EmailSummary``.
    """
    conditions: list[Composable] = []
    params: dict[str, object] = {"limit": limit}
    # Simple equality filters keyed (param_name -> (column_name, value)).
    equality_filters: dict[str, tuple[str, str | None]] = {
        "contact_id": ("contact_id", contact_id),
        "account_id": ("account_id", account_id),
        "thread_id": ("gmail_thread_id", thread_id),
        "direction": ("direction", direction),
        "workflow_id": ("workflow_id", workflow_id),
        "status": ("status", status),
        "route_method": ("route_method", route_method),
    }
    for param_name, (column, value) in equality_filters.items():
        if value is None:
            continue
        conditions.append(
            SQL("{} = {}").format(Identifier(column), Placeholder(param_name))
        )
        params[param_name] = value
    if since is not None:
        conditions.append(SQL("COALESCE(sent_at, received_at) >= %(since)s"))
        params["since"] = since
    if sender is not None:
        conditions.append(SQL("LOWER(sender) = LOWER(%(sender)s)"))
        params["sender"] = sender
    if recipient is not None:
        conditions.append(
            SQL("LOWER(recipients::text) LIKE LOWER(%(recipient_pattern)s)")
        )
        params["recipient_pattern"] = f"%{recipient}%"
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT id, account_id, contact_id, workflow_id, direction, "
        "subject, sender, status, is_routed, route_method, "
        "gmail_thread_id, sent_at, received_at "
        "FROM email {} "
        "ORDER BY COALESCE(sent_at, received_at) DESC LIMIT %(limit)s"
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [EmailSummary.model_validate(row) for row in rows]


def search_emails(
    connection: psycopg.Connection[dict[str, Any]],
    query: str,
    limit: int = 100,
    account_id: str | None = None,
) -> list[EmailSummary]:
    """Search emails by subject, body text, sender, or recipients.

    Args:
        connection: Open database connection.
        query: Search term.
        limit: Maximum number of results.
        account_id: Filter by account ID.

    Returns:
        Matching email summaries ordered by creation time descending.
    """
    pattern = f"%{query}%"
    params: dict[str, object] = {"pattern": pattern, "limit": limit}
    account_filter = SQL("")
    if account_id is not None:
        account_filter = SQL("AND account_id = %(account_id)s")
        params["account_id"] = account_id
    query_sql = SQL(
        "SELECT id, account_id, contact_id, workflow_id, direction, "
        "subject, sender, status, is_routed, route_method, "
        "gmail_thread_id, sent_at, received_at "
        "FROM email "
        "WHERE (LOWER(subject) LIKE LOWER(%(pattern)s) "
        "   OR LOWER(body_text) LIKE LOWER(%(pattern)s) "
        "   OR LOWER(sender) LIKE LOWER(%(pattern)s) "
        "   OR LOWER(recipients::text) LIKE LOWER(%(pattern)s)) "
        "{} "
        "ORDER BY created_at DESC "
        "LIMIT %(limit)s"
    ).format(account_filter)
    rows = connection.execute(query_sql, params).fetchall()
    return [EmailSummary.model_validate(row) for row in rows]


def get_email_by_gmail_message_id(
    connection: psycopg.Connection[dict[str, Any]],
    gmail_message_id: str,
) -> Email | None:
    """Get an email by Gmail message ID.

    Args:
        connection: Open database connection.
        gmail_message_id: Gmail message ID (unique).

    Returns:
        Email if found, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM email WHERE gmail_message_id = %(gmail_message_id)s",
        {"gmail_message_id": gmail_message_id},
    ).fetchone()
    if row is None:
        return None
    return Email.model_validate(row)


def get_emails_by_gmail_thread_id(
    connection: psycopg.Connection[dict[str, Any]],
    gmail_thread_id: str,
) -> list[Email]:
    """Get all emails in a Gmail thread.

    Args:
        connection: Open database connection.
        gmail_thread_id: Gmail thread ID.

    Returns:
        Emails in the thread ordered by creation time.
    """
    rows = connection.execute(
        """\
        SELECT * FROM email
        WHERE gmail_thread_id = %(gmail_thread_id)s
        ORDER BY created_at
        """,
        {"gmail_thread_id": gmail_thread_id},
    ).fetchall()
    return [Email.model_validate(row) for row in rows]


def get_latest_email_in_thread(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    gmail_thread_id: str,
) -> Email | None:
    """Get the most recently created email in a Gmail thread for an account.

    Used when sending a reply into an existing thread to pull the prior
    message's ``rfc2822_message_id`` for the outgoing ``In-Reply-To`` /
    ``References`` headers. Scoping by ``account_id`` keeps the lookup
    deterministic when the same Gmail thread ID is observed on multiple
    delegated mailboxes (e.g. sender + recipient on the same domain).

    Args:
        connection: Open database connection.
        account_id: Account FK.
        gmail_thread_id: Gmail thread ID.

    Returns:
        Most recently created email in the thread, or None if the thread
        has no rows for this account.
    """
    row = connection.execute(
        """\
        SELECT * FROM email
        WHERE account_id = %(account_id)s
          AND gmail_thread_id = %(gmail_thread_id)s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"account_id": account_id, "gmail_thread_id": gmail_thread_id},
    ).fetchone()
    if row is None:
        return None
    return Email.model_validate(row)


def find_email_by_rfc2822_message_id(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    message_ids: list[str],
) -> Email | None:
    """Find the most recent email matching any of the given RFC 2822 message ids.

    Used by the routing pipeline as a fallback when ``gmail_thread_id`` no
    longer joins inbound replies to their outbound parents (Gmail re-threads
    on the recipient side, producing a different ``threadId`` for the same
    conversation). Restricted to a single ``account_id`` so cross-account
    collisions on a shared Message-ID cannot leak workflow assignments.

    Args:
        connection: Open database connection.
        account_id: Account scope -- only rows belonging to this account
            are considered.
        message_ids: Candidate RFC 2822 Message-ID values, typically the
            inbound email's ``In-Reply-To`` plus every entry in its
            ``References`` chain.

    Returns:
        The most-recently created matching email, or ``None`` when no row
        in this account stores any of ``message_ids`` in its
        ``rfc2822_message_id`` column.
    """
    if not message_ids:
        return None
    row = connection.execute(
        """\
        SELECT * FROM email
        WHERE account_id = %(account_id)s
          AND rfc2822_message_id = ANY(%(message_ids)s)
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"account_id": account_id, "message_ids": message_ids},
    ).fetchone()
    if row is None:
        return None
    return Email.model_validate(row)


def get_last_cold_outbound(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    contact_id: str,
    workflow_id: str,
) -> Email | None:
    """Get the most recent cold outbound email to a contact within a workflow.

    A cold outbound email is the first outbound message in its Gmail
    thread (no prior outbound in the same thread). This distinguishes
    initial outreach from follow-up replies within an existing
    conversation. Used by the ``send_email`` agent tool for cooldown
    enforcement. Scoped to a single workflow so that independent
    campaigns can each send their first outreach independently.

    Args:
        connection: Open database connection.
        account_id: Sending account.
        contact_id: Recipient contact.
        workflow_id: Workflow scope for cooldown.

    Returns:
        Most recent cold outbound email, or None if none exists.
    """
    row = connection.execute(
        """\
        SELECT e.* FROM email e
        WHERE e.account_id = %(account_id)s
          AND e.contact_id = %(contact_id)s
          AND e.workflow_id = %(workflow_id)s
          AND e.direction = 'outbound'
          AND NOT EXISTS (
              SELECT 1 FROM email prior
              WHERE prior.gmail_thread_id = e.gmail_thread_id
                AND prior.gmail_thread_id IS NOT NULL
                AND prior.account_id = e.account_id
                AND prior.direction = 'outbound'
                AND prior.created_at < e.created_at
          )
        ORDER BY e.created_at DESC
        LIMIT 1
        """,
        {
            "account_id": account_id,
            "contact_id": contact_id,
            "workflow_id": workflow_id,
        },
    ).fetchone()
    if row is None:
        return None
    return Email.model_validate(row)


def update_email(
    connection: psycopg.Connection[dict[str, Any]],
    email_id: str,
    **fields: object,
) -> Email | None:
    """Update an email by ID.

    Args:
        connection: Open database connection.
        email_id: Email ID.
        **fields: Fields to update (must be valid Email field names).

    Returns:
        Updated email, or None if not found.
    """
    allowed = {
        "workflow_id",
        "is_routed",
        "route_method",
        "status",
        "contact_id",
        "rfc2822_message_id",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_email(connection, email_id)
    updates["id"] = email_id
    # email table has no updated_at column -- use raw SQL instead of _build_update
    set_parts = [
        SQL("{} = {}").format(Identifier(k), Placeholder(k))
        for k in updates
        if k != "id"
    ]
    set_clause = SQL(", ").join(set_parts)
    query = SQL("UPDATE email SET {} WHERE id = %(id)s RETURNING *").format(set_clause)
    row = connection.execute(query, updates).fetchone()
    connection.commit()
    if row is None:
        return None
    return Email.model_validate(row)


# -- Task ----------------------------------------------------------------------


def create_task(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
    workflow_id: str,
    contact_id: str,
    description: str,
    scheduled_at: str,
    context: dict[str, object] | None = None,
    email_id: str | None = None,
) -> Task:
    """Create a deferred task.

    Per §V.28: every task row belongs to an enrollment. ``enrollment_id``
    is NOT NULL at the schema level; callers resolve it from the
    ``(workflow_id, contact_id)`` UNIQUE pair before invoking this fn.
    The denormalised ``workflow_id`` + ``contact_id`` columns stay for
    filter-path and dashboard compat.

    Args:
        connection: Open database connection.
        enrollment_id: Enrollment FK (NOT NULL per schema).
        workflow_id: Workflow FK (denormalised from enrollment row).
        contact_id: Contact FK (denormalised from enrollment row).
        description: What the agent should do.
        scheduled_at: When to execute (ISO timestamp).
        context: Arbitrary JSON context for the agent.
        email_id: Optional triggering email FK.

    Returns:
        Created task.
    """
    row = connection.execute(
        """\
        INSERT INTO task (id, enrollment_id, workflow_id, contact_id, email_id,
            description, context, scheduled_at)
        VALUES (%(id)s, %(enrollment_id)s, %(workflow_id)s, %(contact_id)s,
                %(email_id)s, %(description)s, %(context)s, %(scheduled_at)s)
        RETURNING *
        """,
        {
            "id": _new_id(),
            "enrollment_id": enrollment_id,
            "workflow_id": workflow_id,
            "contact_id": contact_id,
            "email_id": email_id,
            "description": description,
            "context": Json(context or {}),
            "scheduled_at": scheduled_at,
        },
    ).fetchone()
    connection.commit()
    return Task.model_validate(row)


def get_task(
    connection: psycopg.Connection[dict[str, Any]],
    task_id: str,
) -> Task | None:
    """Get a task by ID.

    Args:
        connection: Open database connection.
        task_id: Task ID.

    Returns:
        Task if found, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM task WHERE id = %(id)s",
        {"id": task_id},
    ).fetchone()
    if row is None:
        return None
    return Task.model_validate(row)


def list_pending_tasks(
    connection: psycopg.Connection[dict[str, Any]],
) -> list[Task]:
    """List tasks due for execution.

    Args:
        connection: Open database connection.

    Returns:
        Pending tasks where scheduled_at <= now(), ordered by scheduled_at.
    """
    rows = connection.execute(
        """\
        SELECT * FROM task
        WHERE scheduled_at <= CURRENT_TIMESTAMP AND status = 'pending'
        ORDER BY scheduled_at
        """
    ).fetchall()
    return [Task.model_validate(row) for row in rows]


def find_pending_first_touch_task(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
) -> Task | None:
    """Return a pending first-touch task for ``enrollment_id`` if any.

    A first-touch task is the CLI-scheduled initial outbound send per §V.32:
    ``email_id IS NULL`` (not tied to a triggering inbound email) and
    ``status='pending'`` (not yet drained, not cancelled, not failed). Used
    by ``mailpilot enrollment add --scheduled-at ...`` to skip a duplicate
    insert when the operator re-runs against an enrollment that already has
    one queued. Keyed on scalar ``enrollment_id`` per §V.32 post-migration.
    """
    row = connection.execute(
        """\
        SELECT * FROM task
        WHERE enrollment_id = %(enrollment_id)s
          AND email_id IS NULL
          AND status = 'pending'
        ORDER BY scheduled_at
        LIMIT 1
        """,
        {"enrollment_id": enrollment_id},
    ).fetchone()
    if row is None:
        return None
    return Task.model_validate(row)


def complete_task(
    connection: psycopg.Connection[dict[str, Any]],
    task_id: str,
    status: str = "completed",
    result: dict[str, object] | None = None,
) -> Task | None:
    """Mark a task as completed or failed, optionally storing a result.

    Args:
        connection: Open database connection.
        task_id: Task ID.
        status: "completed" or "failed".
        result: Agent reasoning and outcome to persist.

    Returns:
        Updated task, or None if not found.
    """
    result_json = result or {}
    row = connection.execute(
        """\
        UPDATE task
        SET status = %(status)s,
            result = %(result)s,
            completed_at = CURRENT_TIMESTAMP
        WHERE id = %(id)s RETURNING *
        """,
        {
            "id": task_id,
            "status": status,
            "result": Json(result_json),
        },
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Task.model_validate(row)


def cancel_task(
    connection: psycopg.Connection[dict[str, Any]],
    task_id: str,
) -> Task | None:
    """Cancel a pending task.

    Only cancels tasks with status 'pending'. Already completed or failed
    tasks are not affected.

    Args:
        connection: Open database connection.
        task_id: Task ID.

    Returns:
        Cancelled task, or None if not found or not pending.
    """
    row = connection.execute(
        """\
        UPDATE task SET status = 'cancelled', completed_at = CURRENT_TIMESTAMP
        WHERE id = %(id)s AND status = 'pending'
        RETURNING *
        """,
        {"id": task_id},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Task.model_validate(row)


def list_tasks(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str | None = None,
    contact_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    since: str | None = None,
) -> list[TaskSummary]:
    """List tasks as summaries with optional filters.

    Args:
        connection: Open database connection.
        workflow_id: Filter by workflow ID.
        contact_id: Filter by contact ID.
        status: Filter by task status.
        limit: Maximum results.
        since: ISO datetime lower bound on ``scheduled_at``.

    Returns:
        List of task summaries ordered by scheduled_at descending.
    """
    conditions: list[SQL] = []
    params: dict[str, object] = {"limit": limit}
    if workflow_id is not None:
        conditions.append(SQL("workflow_id = %(workflow_id)s"))
        params["workflow_id"] = workflow_id
    if contact_id is not None:
        conditions.append(SQL("contact_id = %(contact_id)s"))
        params["contact_id"] = contact_id
    if status is not None:
        conditions.append(SQL("status = %(status)s"))
        params["status"] = status
    if since is not None:
        conditions.append(SQL("scheduled_at >= %(since)s"))
        params["since"] = since
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT id, enrollment_id, workflow_id, contact_id, email_id, "
        "description, scheduled_at, status, attempt_count "
        "FROM task {} ORDER BY scheduled_at DESC LIMIT %(limit)s"
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [TaskSummary.model_validate(row) for row in rows]


def reschedule_task_for_retry(
    connection: psycopg.Connection[dict[str, Any]],
    task_id: str,
    backoff_seconds: int,
    exc: BaseException,
) -> Task | None:
    """Reschedule a transient-failure task for another attempt.

    Status remains ``pending``. ``attempt_count`` is incremented and
    ``scheduled_at`` is advanced by ``backoff_seconds``. The row's
    ``result`` JSON captures a summary of the last failure so an
    operator inspecting the row mid-retry-loop sees what's been tried.

    Args:
        connection: Open database connection.
        task_id: Task ID.
        backoff_seconds: Delay to add before the next attempt fires.
        exc: Exception from the failed attempt; used to populate the
            ``result.last_error`` summary.

    Returns:
        Updated task, or ``None`` if the row does not exist.
    """
    summary = {
        "last_error": {
            "type": type(exc).__name__,
            "message": str(exc),
        },
    }
    row = connection.execute(
        """\
        UPDATE task
        SET attempt_count = attempt_count + 1,
            scheduled_at = CURRENT_TIMESTAMP + (%(delay)s || ' seconds')::interval,
            result = %(result)s
        WHERE id = %(id)s
        RETURNING *
        """,
        {
            "id": task_id,
            "delay": str(backoff_seconds),
            "result": Json(summary),
        },
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Task.model_validate(row)


def reschedule_task_for_lock_contention(
    connection: psycopg.Connection[dict[str, Any]],
    task_id: str,
    backoff_seconds: int,
) -> Task | None:
    """Push ``scheduled_at`` forward without bumping ``attempt_count``.

    Used when the agent advisory lock was held by another worker (§V.25).
    Lock contention is not a retry: the task ran nothing, side-effect
    budget is untouched, ``attempt_count`` stays put. Bumping
    ``scheduled_at`` fires the ``task_pending_trigger`` ``UPDATE`` notify
    (§V.49 trigger extension) so the drain loop wakes again instead of
    leaving the task ``pending`` with no signal (§B.42).

    Args:
        connection: Open database connection.
        task_id: Task ID.
        backoff_seconds: Delay before the next attempt fires.

    Returns:
        Updated task, or ``None`` if the row does not exist.
    """
    row = connection.execute(
        """\
        UPDATE task
        SET scheduled_at = CURRENT_TIMESTAMP + (%(delay)s || ' seconds')::interval
        WHERE id = %(id)s
        RETURNING *
        """,
        {
            "id": task_id,
            "delay": str(backoff_seconds),
        },
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Task.model_validate(row)


def manual_retry_task(
    connection: psycopg.Connection[dict[str, Any]],
    task_id: str,
) -> Task | None:
    """Reset a terminal task row for a fresh retry, operator-initiated.

    Allowed only on rows with status ``failed`` or ``cancelled``.
    Refuses ``completed`` rows (tools already fired - retry would
    duplicate side-effects) and ``pending`` rows (already queued, no-op).

    Resets ``status='pending'``, ``attempt_count=0``, ``scheduled_at=now()``,
    and clears ``completed_at``. The row's ``UPDATE`` of ``status`` and
    ``scheduled_at`` fires ``pg_notify('task_pending')`` via
    ``task_pending_trigger`` so the run loop wakes immediately.

    Args:
        connection: Open database connection.
        task_id: Task ID.

    Returns:
        Reset task, or ``None`` if the row does not exist or is not in
        a retryable state.
    """
    row = connection.execute(
        """\
        UPDATE task
        SET status = 'pending',
            attempt_count = 0,
            scheduled_at = CURRENT_TIMESTAMP,
            completed_at = NULL
        WHERE id = %(id)s AND status IN ('failed', 'cancelled')
        RETURNING *
        """,
        {"id": task_id},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Task.model_validate(row)


def get_unprocessed_inbound_email(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> Email | None:
    """Return the most recent inbound email for a contact+workflow without a task.

    Uses the same filtering logic as ``create_tasks_for_routed_emails`` but
    scoped to a single contact and returning at most one email.

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        contact_id: Contact FK.

    Returns:
        The most recent unprocessed inbound email, or None.
    """
    row = connection.execute(
        """\
        SELECT e.* FROM email e
        JOIN workflow w ON w.id = e.workflow_id
        WHERE e.direction = 'inbound'
          AND e.workflow_id = %(workflow_id)s
          AND e.contact_id = %(contact_id)s
          AND e.created_at >= w.created_at
          AND NOT EXISTS (SELECT 1 FROM task t WHERE t.email_id = e.id)
        ORDER BY e.created_at DESC
        LIMIT 1
        """,
        {"workflow_id": workflow_id, "contact_id": contact_id},
    ).fetchone()
    if row is None:
        return None
    return Email.model_validate(row)


def create_tasks_for_routed_emails(
    connection: psycopg.Connection[dict[str, Any]],
) -> list[Task]:
    """Create immediate tasks for routed inbound emails without tasks.

    Finds inbound emails with workflow_id set but no corresponding task
    row, and creates a task with scheduled_at=now() for each. Joins
    ``enrollment`` so ``task.enrollment_id`` is populated per §V.28 --
    the enrollment row is guaranteed present because
    ``routing._ensure_enrollment`` runs earlier in the inbound pipeline.

    Uses ``e.created_at`` (DB insert time) rather than ``e.received_at``
    (Gmail timestamp) to filter historical emails. An email can be received
    by Gmail before a workflow exists but synced into our DB after -- using
    ``received_at`` would incorrectly skip such emails.

    Args:
        connection: Open database connection.

    Returns:
        List of newly created tasks.
    """
    unmatched = connection.execute(
        """\
        SELECT e.id, e.workflow_id, e.contact_id, en.id AS enrollment_id
        FROM email e
        JOIN workflow w ON w.id = e.workflow_id
        JOIN enrollment en
          ON en.workflow_id = e.workflow_id AND en.contact_id = e.contact_id
        WHERE e.direction = 'inbound'
          AND e.contact_id IS NOT NULL
          AND e.created_at >= w.created_at
          AND NOT EXISTS (SELECT 1 FROM task t WHERE t.email_id = e.id)
        ORDER BY e.created_at
        """
    ).fetchall()
    tasks: list[Task] = []
    for email_row in unmatched:
        now = datetime.now(UTC).isoformat()
        t = create_task(
            connection,
            enrollment_id=email_row["enrollment_id"],
            workflow_id=email_row["workflow_id"],
            contact_id=email_row["contact_id"],
            description="handle inbound email",
            scheduled_at=now,
            email_id=email_row["id"],
        )
        tasks.append(t)
    return tasks


# -- Activity ------------------------------------------------------------------


def create_activity(
    connection: psycopg.Connection[dict[str, Any]],
    activity_type: str,
    summary: str = "",
    detail: dict[str, object] | None = None,
    contact_id: str | None = None,
    company_id: str | None = None,
    email_id: str | None = None,
    workflow_id: str | None = None,
    task_id: str | None = None,
    enrollment_id: str | None = None,
) -> Activity:
    """Create an activity event.

    At least one of ``contact_id`` or ``company_id`` must be set.
    Structured FK columns (``email_id``, ``workflow_id``, ``task_id``,
    ``enrollment_id``) let reports join activity to source records without
    parsing ``detail`` JSON. ``enrollment_id`` is nullable -- non-enrollment
    activity types (``email_sent``, ``note_added``, etc.) leave it null;
    enrollment-lifecycle types (``enrollment_added`` / ``enrollment_completed``
    / etc.) populate it.

    Raises:
        ValueError: If neither contact_id nor company_id is provided.
    """
    if contact_id is None and company_id is None:
        raise ValueError("at least one of contact_id or company_id is required")
    row = connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, email_id, workflow_id, task_id,
            enrollment_id, type, summary, detail
        )
        VALUES (
            %(id)s, %(contact_id)s, %(company_id)s, %(email_id)s,
            %(workflow_id)s, %(task_id)s, %(enrollment_id)s,
            %(type)s, %(summary)s, %(detail)s
        )
        RETURNING *
        """,
        {
            "id": _new_id(),
            "contact_id": contact_id,
            "company_id": company_id,
            "email_id": email_id,
            "workflow_id": workflow_id,
            "task_id": task_id,
            "enrollment_id": enrollment_id,
            "type": activity_type,
            "summary": summary,
            "detail": Json(detail or {}),
        },
    ).fetchone()
    connection.commit()
    return Activity.model_validate(row)


def list_activities(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str | None = None,
    company_id: str | None = None,
    activity_type: str | None = None,
    limit: int = 100,
    since: str | None = None,
) -> list[ActivitySummary]:
    """List activities as summaries with required contact or company filter.

    At least one of ``contact_id`` or ``company_id`` must be provided.

    Args:
        connection: Open database connection.
        contact_id: Filter by contact ID.
        company_id: Filter by company ID.
        activity_type: Filter by activity type.
        limit: Maximum number of results.
        since: ISO datetime lower bound for created_at.

    Returns:
        Activity summaries ordered by created_at descending.

    Raises:
        ValueError: If neither contact_id nor company_id is provided.
    """
    if contact_id is None and company_id is None:
        raise ValueError("at least one of contact_id or company_id is required")
    conditions: list[SQL] = []
    params: dict[str, object] = {"limit": limit}
    if contact_id is not None:
        conditions.append(SQL("contact_id = %(contact_id)s"))
        params["contact_id"] = contact_id
    if company_id is not None:
        conditions.append(SQL("company_id = %(company_id)s"))
        params["company_id"] = company_id
    if activity_type is not None:
        conditions.append(SQL("type = %(activity_type)s"))
        params["activity_type"] = activity_type
    if since is not None:
        conditions.append(SQL("created_at >= %(since)s"))
        params["since"] = since
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT id, contact_id, company_id, email_id, workflow_id, task_id, "
        "enrollment_id, type, summary, created_at "
        "FROM activity {} ORDER BY created_at DESC LIMIT %(limit)s"
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [ActivitySummary.model_validate(row) for row in rows]


# -- Tag -----------------------------------------------------------------------


_TAG_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


def _normalize_tag_name(name: str) -> str:
    """Normalize a tag name to lowercase hyphenated form.

    Strips whitespace, lowercases, replaces whitespace and underscores
    with hyphens, collapses repeated hyphens, trims leading/trailing
    hyphens, and validates against ``[a-z0-9][a-z0-9-]*``.

    Raises:
        ValueError: If the result is empty or contains disallowed
        characters.
    """
    cleaned = name.strip().lower()
    cleaned = re.sub(r"[\s_]+", "-", cleaned)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    if not _TAG_NAME_RE.fullmatch(cleaned):
        raise ValueError(f"invalid tag name: {name!r} (normalized to {cleaned!r})")
    return cleaned


def create_tag(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    contact_id: str | None = None,
    company_id: str | None = None,
) -> Tag | None:
    """Create a tag on a contact or company.

    Exactly one of ``contact_id`` or ``company_id`` must be provided.
    The name is normalized via ``_normalize_tag_name``. Uses ON CONFLICT
    DO NOTHING -- returns None if the tag already exists.

    Raises:
        ValueError: If neither or both of contact_id/company_id are set,
        or if the tag name fails normalization.
    """
    if (contact_id is None) == (company_id is None):
        raise ValueError("exactly one of contact_id or company_id is required")
    normalized = _normalize_tag_name(name)
    row = connection.execute(
        """\
        INSERT INTO tag (id, contact_id, company_id, name)
        VALUES (%(id)s, %(contact_id)s, %(company_id)s, %(name)s)
        ON CONFLICT DO NOTHING
        RETURNING *
        """,
        {
            "id": _new_id(),
            "contact_id": contact_id,
            "company_id": company_id,
            "name": normalized,
        },
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Tag.model_validate(row)


def list_tags(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str | None = None,
    company_id: str | None = None,
    limit: int = 100,
    since: str | None = None,
    include_disabled: bool = False,
) -> list[Tag]:
    """List tags on a contact or company.

    Tag has no Summary projection -- the full row already matches the
    summary contract. Disabled rows (``disabled_reason IS NOT NULL``) are
    excluded by default; pass ``include_disabled=True`` to include them
    (§V.10 tag-coverage clause).

    Raises:
        ValueError: If neither or both of contact_id/company_id are set.
    """
    if (contact_id is None) == (company_id is None):
        raise ValueError("exactly one of contact_id or company_id is required")
    params: dict[str, object] = {"limit": limit}
    where_parts: list[Composed | SQL] = []
    if contact_id is not None:
        where_parts.append(SQL("contact_id = %(contact_id)s"))
        params["contact_id"] = contact_id
    else:
        where_parts.append(SQL("company_id = %(company_id)s"))
        params["company_id"] = company_id
    if since is not None:
        where_parts.append(SQL("created_at >= %(since)s"))
        params["since"] = since
    if not include_disabled:
        where_parts.append(SQL("disabled_reason IS NULL"))
    where = SQL("WHERE ") + SQL(" AND ").join(where_parts)
    query = SQL("SELECT * FROM tag {} ORDER BY name LIMIT %(limit)s").format(where)
    rows = connection.execute(query, params).fetchall()
    return [Tag.model_validate(row) for row in rows]


def list_contacts_by_tag(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    limit: int = 100,
    include_disabled: bool = False,
) -> list[str]:
    """Return contact IDs with the given tag (normalized).

    Excludes disabled tag rows by default (§V.10 tag-coverage clause).
    """
    normalized = _normalize_tag_name(name)
    params: dict[str, object] = {"name": normalized, "limit": limit}
    disabled_filter = (
        SQL("") if include_disabled else SQL("AND disabled_reason IS NULL")
    )
    query = SQL(
        """\
        SELECT contact_id FROM tag
        WHERE contact_id IS NOT NULL AND name = %(name)s {}
        ORDER BY created_at
        LIMIT %(limit)s
        """
    ).format(disabled_filter)
    rows = connection.execute(query, params).fetchall()
    return [row["contact_id"] for row in rows]


def list_companies_by_tag(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    limit: int = 100,
    include_disabled: bool = False,
) -> list[str]:
    """Return company IDs with the given tag (normalized).

    Excludes disabled tag rows by default (§V.10 tag-coverage clause).
    """
    normalized = _normalize_tag_name(name)
    params: dict[str, object] = {"name": normalized, "limit": limit}
    disabled_filter = (
        SQL("") if include_disabled else SQL("AND disabled_reason IS NULL")
    )
    query = SQL(
        """\
        SELECT company_id FROM tag
        WHERE company_id IS NOT NULL AND name = %(name)s {}
        ORDER BY created_at
        LIMIT %(limit)s
        """
    ).format(disabled_filter)
    rows = connection.execute(query, params).fetchall()
    return [row["company_id"] for row in rows]


def search_tags(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    owner: str | None = None,
    limit: int = 100,
    include_disabled: bool = False,
) -> list[Tag]:
    """Search tags by name pattern with optional owner filter.

    Excludes disabled tag rows by default (§V.10 tag-coverage clause).

    Args:
        connection: Open database connection.
        name: Pattern to LIKE-match against tag ``name``.
        owner: ``"contact"`` to limit to contact tags, ``"company"`` to
            limit to company tags, ``None`` for both.
        limit: Maximum number of results.
        include_disabled: When ``True`` include disabled rows.
    """
    if owner not in (None, "contact", "company"):
        raise ValueError("owner must be 'contact', 'company', or None")
    pattern = f"%{name.strip().lower()}%"
    params: dict[str, object] = {"pattern": pattern, "limit": limit}
    extra_filters: list[SQL] = []
    if owner == "contact":
        extra_filters.append(SQL("AND contact_id IS NOT NULL"))
    elif owner == "company":
        extra_filters.append(SQL("AND company_id IS NOT NULL"))
    if not include_disabled:
        extra_filters.append(SQL("AND disabled_reason IS NULL"))
    extra = SQL(" ").join(extra_filters) if extra_filters else SQL("")
    query = SQL(
        "SELECT * FROM tag WHERE name LIKE %(pattern)s {} ORDER BY name LIMIT %(limit)s"
    ).format(extra)
    rows = connection.execute(query, params).fetchall()
    return [Tag.model_validate(row) for row in rows]


def add_contact_tag(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
    name: str,
) -> Tag | None:
    """Add a tag to a contact and emit a `tag_added` activity atomically.

    The two writes share one transaction. Returns ``None`` if the tag
    already exists -- in that case no activity is written.
    """
    normalized = _normalize_tag_name(name)
    contact_row = connection.execute(
        "SELECT company_id FROM contact WHERE id = %s", (contact_id,)
    ).fetchone()
    if contact_row is None:
        raise ValueError(f"contact not found: {contact_id}")
    tag_row = connection.execute(
        """\
        INSERT INTO tag (id, contact_id, company_id, name)
        VALUES (%(id)s, %(contact_id)s, NULL, %(name)s)
        ON CONFLICT DO NOTHING
        RETURNING *
        """,
        {"id": _new_id(), "contact_id": contact_id, "name": normalized},
    ).fetchone()
    if tag_row is None:
        connection.commit()
        return None
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, type, summary, detail
        )
        VALUES (
            %(id)s, %(contact_id)s, %(company_id)s,
            'tag_added', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "contact_id": contact_id,
            "company_id": contact_row["company_id"],
            "summary": f"Tagged as {normalized}",
            "detail": Json({"tag": normalized}),
        },
    )
    connection.commit()
    return Tag.model_validate(tag_row)


def add_company_tag(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
    name: str,
) -> Tag | None:
    """Add a tag to a company and emit a `tag_added` company activity atomically."""
    normalized = _normalize_tag_name(name)
    if (
        connection.execute(
            "SELECT 1 FROM company WHERE id = %s", (company_id,)
        ).fetchone()
        is None
    ):
        raise ValueError(f"company not found: {company_id}")
    tag_row = connection.execute(
        """\
        INSERT INTO tag (id, contact_id, company_id, name)
        VALUES (%(id)s, NULL, %(company_id)s, %(name)s)
        ON CONFLICT DO NOTHING
        RETURNING *
        """,
        {"id": _new_id(), "company_id": company_id, "name": normalized},
    ).fetchone()
    if tag_row is None:
        connection.commit()
        return None
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, type, summary, detail
        )
        VALUES (
            %(id)s, NULL, %(company_id)s,
            'tag_added', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "company_id": company_id,
            "summary": f"Tagged as {normalized}",
            "detail": Json({"tag": normalized}),
        },
    )
    connection.commit()
    return Tag.model_validate(tag_row)


def disable_contact_tag(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
    name: str,
    reason: str,
) -> Tag | None:
    """Soft-disable a contact-tag and emit a `tag_disabled` activity (§V.10).

    Single transaction: flips ``disabled_reason`` on the matching active tag
    row (``disabled_reason IS NULL`` gate ensures already-disabled rows are
    not double-disabled), then appends a ``tag_disabled`` activity carrying
    the reason. Returns the updated ``Tag`` row, or ``None`` when no active
    tag with the given name exists on the contact.

    Raises:
        ValueError: If the contact does not exist or the name fails
            normalization.
    """
    normalized = _normalize_tag_name(name)
    contact_row = connection.execute(
        "SELECT company_id FROM contact WHERE id = %s", (contact_id,)
    ).fetchone()
    if contact_row is None:
        raise ValueError(f"contact not found: {contact_id}")
    updated_row = connection.execute(
        """\
        UPDATE tag
        SET disabled_reason = %(reason)s
        WHERE contact_id = %(contact_id)s
          AND name = %(name)s
          AND disabled_reason IS NULL
        RETURNING *
        """,
        {"contact_id": contact_id, "name": normalized, "reason": reason},
    ).fetchone()
    if updated_row is None:
        connection.commit()
        return None
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, type, summary, detail
        )
        VALUES (
            %(id)s, %(contact_id)s, %(company_id)s,
            'tag_disabled', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "contact_id": contact_id,
            "company_id": contact_row["company_id"],
            "summary": f"Disabled tag {normalized}",
            "detail": Json({"tag": normalized, "reason": reason}),
        },
    )
    connection.commit()
    return Tag.model_validate(updated_row)


def disable_company_tag(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
    name: str,
    reason: str,
) -> Tag | None:
    """Soft-disable a company-tag and emit a `tag_disabled` activity (§V.10).

    Mirrors ``disable_contact_tag`` shape. Returns the updated ``Tag`` row,
    or ``None`` when no active tag with the given name exists on the
    company.
    """
    normalized = _normalize_tag_name(name)
    updated_row = connection.execute(
        """\
        UPDATE tag
        SET disabled_reason = %(reason)s
        WHERE company_id = %(company_id)s
          AND name = %(name)s
          AND disabled_reason IS NULL
        RETURNING *
        """,
        {"company_id": company_id, "name": normalized, "reason": reason},
    ).fetchone()
    if updated_row is None:
        connection.commit()
        return None
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, type, summary, detail
        )
        VALUES (
            %(id)s, NULL, %(company_id)s,
            'tag_disabled', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "company_id": company_id,
            "summary": f"Disabled tag {normalized}",
            "detail": Json({"tag": normalized, "reason": reason}),
        },
    )
    connection.commit()
    return Tag.model_validate(updated_row)


# -- Note ----------------------------------------------------------------------


def create_note(
    connection: psycopg.Connection[dict[str, Any]],
    body: str,
    contact_id: str | None = None,
    company_id: str | None = None,
) -> Note:
    """Create a freeform note on a contact or company.

    Raises:
        ValueError: If neither or both of contact_id/company_id are set.
    """
    if (contact_id is None) == (company_id is None):
        raise ValueError("exactly one of contact_id or company_id is required")
    row = connection.execute(
        """\
        INSERT INTO note (id, contact_id, company_id, body)
        VALUES (%(id)s, %(contact_id)s, %(company_id)s, %(body)s)
        RETURNING *
        """,
        {
            "id": _new_id(),
            "contact_id": contact_id,
            "company_id": company_id,
            "body": body,
        },
    ).fetchone()
    connection.commit()
    return Note.model_validate(row)


def list_notes(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str | None = None,
    company_id: str | None = None,
    limit: int = 100,
    since: str | None = None,
) -> list[NoteSummary]:
    """List notes on a contact or company as summaries with body previews.

    The full body is replaced by ``body_preview`` -- the first 80 characters
    with a trailing ellipsis when the body is longer.

    Raises:
        ValueError: If neither or both of contact_id/company_id are set.
    """
    if (contact_id is None) == (company_id is None):
        raise ValueError("exactly one of contact_id or company_id is required")
    params: dict[str, object] = {"limit": limit}
    where_parts: list[Composed | SQL] = []
    if contact_id is not None:
        where_parts.append(SQL("contact_id = %(contact_id)s"))
        params["contact_id"] = contact_id
    else:
        where_parts.append(SQL("company_id = %(company_id)s"))
        params["company_id"] = company_id
    if since is not None:
        where_parts.append(SQL("created_at >= %(since)s"))
        params["since"] = since
    where = SQL("WHERE ") + SQL(" AND ").join(where_parts)
    query = SQL(
        "SELECT id, contact_id, company_id, "
        "CASE WHEN LENGTH(body) > 80 THEN LEFT(body, 80) || '...' "
        "ELSE body END AS body_preview, "
        "created_at "
        "FROM note {} ORDER BY created_at DESC LIMIT %(limit)s"
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [NoteSummary.model_validate(row) for row in rows]


def get_note(
    connection: psycopg.Connection[dict[str, Any]],
    note_id: str,
) -> Note | None:
    """Get a note by ID.

    Args:
        connection: Open database connection.
        note_id: Note ID.

    Returns:
        Note if found, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM note WHERE id = %(id)s",
        {"id": note_id},
    ).fetchone()
    if row is None:
        return None
    return Note.model_validate(row)


def add_contact_note(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
    body: str,
) -> Note:
    """Add a note to a contact and emit a `note_added` activity atomically."""
    contact_row = connection.execute(
        "SELECT company_id FROM contact WHERE id = %s", (contact_id,)
    ).fetchone()
    if contact_row is None:
        raise ValueError(f"contact not found: {contact_id}")
    note_row = connection.execute(
        """\
        INSERT INTO note (id, contact_id, company_id, body)
        VALUES (%(id)s, %(contact_id)s, NULL, %(body)s)
        RETURNING *
        """,
        {"id": _new_id(), "contact_id": contact_id, "body": body},
    ).fetchone()
    note = Note.model_validate(note_row)
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, type, summary, detail
        )
        VALUES (
            %(id)s, %(contact_id)s, %(company_id)s,
            'note_added', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "contact_id": contact_id,
            "company_id": contact_row["company_id"],
            "summary": "Note added",
            "detail": Json({"note_id": note.id}),
        },
    )
    connection.commit()
    return note


def add_company_note(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
    body: str,
) -> Note:
    """Add a note to a company and emit a `note_added` company activity atomically."""
    if (
        connection.execute(
            "SELECT 1 FROM company WHERE id = %s", (company_id,)
        ).fetchone()
        is None
    ):
        raise ValueError(f"company not found: {company_id}")
    note_row = connection.execute(
        """\
        INSERT INTO note (id, contact_id, company_id, body)
        VALUES (%(id)s, NULL, %(company_id)s, %(body)s)
        RETURNING *
        """,
        {"id": _new_id(), "company_id": company_id, "body": body},
    ).fetchone()
    note = Note.model_validate(note_row)
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, type, summary, detail
        )
        VALUES (
            %(id)s, NULL, %(company_id)s,
            'note_added', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "company_id": company_id,
            "summary": "Note added",
            "detail": Json({"note_id": note.id}),
        },
    )
    connection.commit()
    return note


# -- Composite View Loaders ----------------------------------------------------


def _load_notes_for_owner(
    connection: psycopg.Connection[dict[str, Any]],
    owner_column: str,
    owner_id: str,
) -> tuple[list[Note], int]:
    """Fetch latest notes for a single owner column plus the total row count.

    Two queries (no JOIN) per §V.8: ``LIMIT _INLINE_NOTES_CAP ORDER BY
    created_at DESC`` for the inline list, ``COUNT(*)`` for the total.
    """
    if owner_column not in {"contact_id", "company_id"}:
        raise ValueError(f"unsupported owner column: {owner_column}")
    list_query = SQL(
        "SELECT * FROM note WHERE {col} = %s ORDER BY created_at DESC LIMIT %s"
    ).format(col=Identifier(owner_column))
    rows = connection.execute(list_query, (owner_id, _INLINE_NOTES_CAP)).fetchall()
    notes = [Note.model_validate(row) for row in rows]
    count_query = SQL("SELECT COUNT(*) AS total FROM note WHERE {col} = %s").format(
        col=Identifier(owner_column)
    )
    count_row = connection.execute(count_query, (owner_id,)).fetchone()
    total = int(count_row["total"]) if count_row is not None else 0
    return notes, total


def load_contact_view(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
) -> ContactView | None:
    """Load a contact with inlined notes (own + parent company) per §V.8.

    Returns ``None`` when the contact does not exist. ``notes`` and
    ``company_notes`` are capped at ``_INLINE_NOTES_CAP`` rows each, ordered
    by ``created_at`` DESC, full body verbatim. Totals reflect the actual row
    count, not the cap. ``company_notes`` is always ``[]`` when the contact
    has no parent company.
    """
    contact = get_contact(connection, contact_id)
    if contact is None:
        return None
    notes, notes_total = _load_notes_for_owner(connection, "contact_id", contact_id)
    if contact.company_id is not None:
        company_notes, company_notes_total = _load_notes_for_owner(
            connection, "company_id", contact.company_id
        )
    else:
        company_notes = []
        company_notes_total = 0
    return ContactView(
        **contact.model_dump(),
        notes=notes,
        notes_total=notes_total,
        company_notes=company_notes,
        company_notes_total=company_notes_total,
    )


def load_company_view(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
) -> CompanyView | None:
    """Load a company with inlined own notes per §V.8.

    Returns ``None`` when the company does not exist. ``notes`` capped at
    ``_INLINE_NOTES_CAP`` rows, ordered by ``created_at`` DESC, full body
    verbatim. ``notes_total`` reflects the actual row count.
    """
    company = get_company(connection, company_id)
    if company is None:
        return None
    notes, notes_total = _load_notes_for_owner(connection, "company_id", company_id)
    return CompanyView(
        **company.model_dump(),
        notes=notes,
        notes_total=notes_total,
    )


# -- Sync Status ---------------------------------------------------------------


def upsert_sync_status(
    connection: psycopg.Connection[dict[str, Any]],
    pid: int,
) -> SyncStatus:
    """Insert or update the singleton sync status row.

    Args:
        connection: Open database connection.
        pid: Process ID of the running sync loop.

    Returns:
        Current sync status.
    """
    row = connection.execute(
        """\
        INSERT INTO sync_status (id, pid)
        VALUES ('singleton', %(pid)s)
        ON CONFLICT (id) DO UPDATE
            SET pid = %(pid)s,
                started_at = CURRENT_TIMESTAMP,
                heartbeat_at = CURRENT_TIMESTAMP
        RETURNING *
        """,
        {"pid": pid},
    ).fetchone()
    connection.commit()
    return SyncStatus.model_validate(row)


def get_sync_status(
    connection: psycopg.Connection[dict[str, Any]],
) -> SyncStatus | None:
    """Get the current sync status.

    Args:
        connection: Open database connection.

    Returns:
        SyncStatus if sync is registered, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM sync_status WHERE id = 'singleton'"
    ).fetchone()
    if row is None:
        return None
    return SyncStatus.model_validate(row)


def delete_sync_status(
    connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Delete the sync status row (called on clean shutdown).

    Args:
        connection: Open database connection.
    """
    connection.execute("DELETE FROM sync_status WHERE id = 'singleton'")
    connection.commit()


def update_sync_heartbeat(
    connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Update the heartbeat timestamp to signal liveness.

    Args:
        connection: Open database connection.
    """
    connection.execute(
        """\
        UPDATE sync_status
        SET heartbeat_at = CURRENT_TIMESTAMP
        WHERE id = 'singleton'
        """
    )
    connection.commit()
