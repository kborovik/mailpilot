"""PostgreSQL database for CRM persistence.

Single flat module with section headers per entity. All CRUD functions follow
consistent signatures and return domain models from ``models.py``.

Convention:
    create_X(connection, ...) -> X
    get_X(connection, id) -> X | None
    list_X(connection, ...) -> list[X]
    update_X(connection, id, ...) -> X
"""

import re
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import logfire
import psycopg
from psycopg.rows import dict_row
from psycopg.sql import SQL, Composed, Identifier, Placeholder
from psycopg.types.json import Json

from mailpilot.models import (
    Account,
    AccountSummary,
    Activity,
    ActivitySummary,
    Company,
    CompanySummary,
    Contact,
    ContactSummary,
    Email,
    EmailSummary,
    Enrollment,
    EnrollmentSummary,
    Note,
    NoteSummary,
    SyncStatus,
    Tag,
    Task,
    TaskSummary,
    Workflow,
    WorkflowSummary,
)

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
        raise SystemExit(f"database connection failed: {hint}") from None
    # Skip the schema apply when the database is already initialized.
    # schema.sql contains DROP TRIGGER + CREATE TRIGGER on the task table
    # which takes AccessExclusiveLock and deadlocks against the sync loop's
    # INSERT INTO task (RowExclusiveLock). New columns/tables added to the
    # schema still flow through the canonical `make clean` workflow, which
    # drops everything and re-applies on an empty database.
    probe = connection.execute("SELECT to_regclass('account') AS oid").fetchone()  # type: ignore[union-attr]
    if probe is None or probe.get("oid") is None:
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
    with logfire.span("db.status.counts"):
        row = connection.execute(
            """\
            SELECT
                (SELECT COUNT(*) FROM account) AS accounts,
                (SELECT COUNT(*) FROM company) AS companies,
                (SELECT COUNT(*) FROM contact) AS contacts,
                (SELECT COUNT(*) FROM workflow) AS workflows,
                (SELECT COUNT(*) FROM email) AS emails,
                (SELECT COUNT(*) FROM activity) AS activities,
                (SELECT COUNT(*) FROM tag) AS tags,
                (SELECT COUNT(*) FROM note) AS notes
            FROM (SELECT 1) AS _dummy
            """
        ).fetchone()
        return {
            "accounts": row["accounts"],  # type: ignore[index]
            "companies": row["companies"],  # type: ignore[index]
            "contacts": row["contacts"],  # type: ignore[index]
            "workflows": row["workflows"],  # type: ignore[index]
            "emails": row["emails"],  # type: ignore[index]
            "activities": row["activities"],  # type: ignore[index]
            "tags": row["tags"],  # type: ignore[index]
            "notes": row["notes"],  # type: ignore[index]
        }


# -- Account -------------------------------------------------------------------


def create_account(
    connection: psycopg.Connection[dict[str, Any]],
    email: str,
    display_name: str = "",
) -> Account:
    """Create a new account.

    Args:
        connection: Open database connection.
        email: Gmail address.
        display_name: Display name for the account.

    Returns:
        Created account.
    """
    row = connection.execute(
        """\
        INSERT INTO account (id, email, display_name)
        VALUES (%(id)s, %(email)s, %(display_name)s)
        RETURNING *
        """,
        {"id": _new_id(), "email": email, "display_name": display_name},
    ).fetchone()
    connection.commit()
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
) -> Company:
    """Create a new company.

    Args:
        connection: Open database connection.
        name: Company name.
        domain: Primary domain.

    Returns:
        Created company.
    """
    row = connection.execute(
        """\
        INSERT INTO company (id, name, domain)
        VALUES (%(id)s, %(name)s, %(domain)s)
        RETURNING *
        """,
        {"id": _new_id(), "name": name, "domain": domain},
    ).fetchone()
    connection.commit()
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
) -> list[CompanySummary]:
    """List companies as summaries.

    Args:
        connection: Open database connection.
        limit: Maximum results.
        since: ISO datetime lower bound on ``created_at``.

    Returns:
        List of company summaries ordered by name.
    """
    conditions: list[Composed | SQL] = []
    params: dict[str, object] = {"limit": limit}
    if since is not None:
        conditions.append(SQL("created_at >= %(since)s"))
        params["since"] = since
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT id, name, domain, industry, employee_count, created_at "
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
        SELECT id, name, domain, industry, employee_count, created_at
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

    Args:
        connection: Open database connection.
        company_id: Company ID.
        **fields: Fields to update (must be valid Company field names).

    Returns:
        Updated company, or None if not found.
    """
    allowed = set(Company.model_fields) - {"id", "created_at"}
    updates = {k: v for k, v in fields.items() if k in allowed}
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
    domain: str,
    company_id: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> Contact:
    """Create a new contact.

    Args:
        connection: Open database connection.
        email: Contact email address.
        domain: Email domain.
        company_id: Optional company FK.
        first_name: Optional first name.
        last_name: Optional last name.

    Returns:
        Created contact.
    """
    row = connection.execute(
        """\
        INSERT INTO contact (id, email, domain, company_id, first_name, last_name)
        VALUES (%(id)s, %(email)s, %(domain)s, %(company_id)s,
                %(first_name)s, %(last_name)s)
        RETURNING *
        """,
        {
            "id": _new_id(),
            "email": email,
            "domain": domain,
            "company_id": company_id,
            "first_name": first_name,
            "last_name": last_name,
        },
    ).fetchone()
    connection.commit()
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
    domain = email.split("@", 1)[1] if "@" in email else ""
    return create_contact(
        connection,
        email=email,
        domain=domain,
        first_name=first_name,
        last_name=last_name,
    )


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
    domains = [email.split("@", 1)[1] if "@" in email else "" for email in unique]
    rows = connection.execute(
        """\
        INSERT INTO contact (id, email, domain)
        SELECT id, email, domain
        FROM unnest(%(ids)s::text[], %(emails)s::text[], %(domains)s::text[])
             AS t(id, email, domain)
        ON CONFLICT (email) DO NOTHING
        RETURNING *
        """,
        {"ids": ids, "emails": unique, "domains": domains},
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
    domain: str | None = None,
    company_id: str | None = None,
    status: str | None = None,
    since: str | None = None,
) -> list[ContactSummary]:
    """List contacts as summaries with optional filters.

    Args:
        connection: Open database connection.
        limit: Maximum results.
        domain: Filter by domain.
        company_id: Filter by company ID.
        status: Filter by contact status ("active", "bounced", "unsubscribed").
        since: ISO datetime lower bound on ``created_at``.

    Returns:
        List of contact summaries ordered by email.
    """
    conditions: list[SQL] = []
    params: dict[str, object] = {"limit": limit}
    if domain is not None:
        conditions.append(SQL("domain = %(domain)s"))
        params["domain"] = domain
    if company_id is not None:
        conditions.append(SQL("company_id = %(company_id)s"))
        params["company_id"] = company_id
    if status is not None:
        conditions.append(SQL("status = %(status)s"))
        params["status"] = status
    if since is not None:
        conditions.append(SQL("created_at >= %(since)s"))
        params["since"] = since
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT id, email, first_name, last_name, company_id, status, created_at "
        "FROM contact {} ORDER BY email LIMIT %(limit)s"
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [ContactSummary.model_validate(row) for row in rows]


def search_contacts(
    connection: psycopg.Connection[dict[str, Any]],
    query: str,
    limit: int = 100,
) -> list[ContactSummary]:
    """Search contacts by email, name, or domain.

    Args:
        connection: Open database connection.
        query: Search term.
        limit: Maximum number of results.

    Returns:
        Matching contact summaries ordered by email.
    """
    pattern = f"%{query}%"
    rows = connection.execute(
        """\
        SELECT id, email, first_name, last_name, company_id, status, created_at
        FROM contact
        WHERE LOWER(email) LIKE LOWER(%(pattern)s)
           OR LOWER(COALESCE(first_name, '')) LIKE LOWER(%(pattern)s)
           OR LOWER(COALESCE(last_name, '')) LIKE LOWER(%(pattern)s)
           OR LOWER(domain) LIKE LOWER(%(pattern)s)
        ORDER BY email
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
    status: str,
    status_reason: str,
) -> Contact | None:
    """Set a global block on a contact (bounced or unsubscribed).

    This is a hard block across all workflows. The send_email tool checks
    contact.status before sending.

    Args:
        connection: Open database connection.
        contact_id: Contact ID.
        status: New status ("bounced" or "unsubscribed").
        status_reason: Explanation for the block.

    Returns:
        Updated contact, or None if not found.
    """
    row = connection.execute(
        """\
        UPDATE contact
        SET status = %(status)s,
            status_reason = %(status_reason)s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %(id)s
        RETURNING *
        """,
        {"id": contact_id, "status": status, "status_reason": status_reason},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Contact.model_validate(row)


# -- Workflow ------------------------------------------------------------------


def create_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    workflow_type: str,
    account_id: str,
    theme: str = "blue",
) -> Workflow:
    """Create a new workflow.

    Args:
        connection: Open database connection.
        name: Workflow name.
        workflow_type: "inbound" or "outbound".
        account_id: Account FK.
        theme: Email color theme (default "blue").

    Returns:
        Created workflow.
    """
    row = connection.execute(
        """\
        INSERT INTO workflow (id, name, type, account_id, theme)
        VALUES (%(id)s, %(name)s, %(type)s, %(account_id)s, %(theme)s)
        RETURNING *
        """,
        {
            "id": _new_id(),
            "name": name,
            "type": workflow_type,
            "account_id": account_id,
            "theme": theme,
        },
    ).fetchone()
    connection.commit()
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
        "SELECT * FROM workflow WHERE id = %(id)s",
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
    limit: int = 100,
    since: str | None = None,
) -> list[WorkflowSummary]:
    """List workflows as summaries with optional account, status, and type filters.

    Args:
        connection: Open database connection.
        account_id: Filter by account ID.
        status: Filter by workflow status (e.g., "active").
        workflow_type: Filter by workflow type ("inbound" or "outbound").
        limit: Maximum results.
        since: ISO datetime lower bound on ``created_at``.

    Returns:
        List of workflow summaries ordered by creation time.
    """
    conditions: list[SQL] = []
    params: dict[str, object] = {"limit": limit}
    if account_id is not None:
        conditions.append(SQL("account_id = %(account_id)s"))
        params["account_id"] = account_id
    if status is not None:
        conditions.append(SQL("status = %(status)s"))
        params["status"] = status
    if workflow_type is not None:
        conditions.append(SQL("type = %(workflow_type)s"))
        params["workflow_type"] = workflow_type
    if since is not None:
        conditions.append(SQL("created_at >= %(since)s"))
        params["since"] = since
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT id, name, type, account_id, status, created_at "
        "FROM workflow {} ORDER BY created_at LIMIT %(limit)s"
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [WorkflowSummary.model_validate(row) for row in rows]


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
        SELECT id, name, type, account_id, status, created_at
        FROM workflow
        WHERE LOWER(name) LIKE LOWER(%(pattern)s)
           OR LOWER(objective) LIKE LOWER(%(pattern)s)
        ORDER BY LOWER(name)
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
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_workflow(connection, workflow_id)
    updates["id"] = workflow_id
    query = _build_update("workflow", updates, SQL("id = %(id)s"))
    row = connection.execute(query, updates).fetchone()
    connection.commit()
    if row is None:
        return None
    return Workflow.model_validate(row)


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
        UPDATE workflow
        SET status = 'active', updated_at = CURRENT_TIMESTAMP
        WHERE id = %(id)s
        RETURNING *
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
        UPDATE workflow
        SET status = 'paused', updated_at = CURRENT_TIMESTAMP
        WHERE id = %(id)s
        RETURNING *
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
    exists (same pattern as ``create_email``).

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        contact_id: Contact FK.

    Returns:
        Created enrollment, or None if it already existed.
    """
    row = connection.execute(
        """\
        INSERT INTO enrollment (workflow_id, contact_id)
        VALUES (%(workflow_id)s, %(contact_id)s)
        ON CONFLICT (workflow_id, contact_id) DO NOTHING
        RETURNING *
        """,
        {"workflow_id": workflow_id, "contact_id": contact_id},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Enrollment.model_validate(row)


def update_enrollment(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
    **fields: object,
) -> Enrollment | None:
    """Update an enrollment.

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        contact_id: Contact FK.
        **fields: Fields to update (status, reason).

    Returns:
        Updated enrollment, or None if not found.
    """
    allowed = {"status", "reason"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_enrollment(connection, workflow_id, contact_id)
    updates["workflow_id"] = workflow_id
    updates["contact_id"] = contact_id
    where = SQL("workflow_id = %(workflow_id)s AND contact_id = %(contact_id)s")
    query = _build_update("enrollment", updates, where)
    row = connection.execute(query, updates).fetchone()
    connection.commit()
    if row is None:
        return None
    return Enrollment.model_validate(row)


def get_enrollment(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> Enrollment | None:
    """Get an enrollment by composite key.

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        contact_id: Contact FK.

    Returns:
        Enrollment if found, None otherwise.
    """
    row = connection.execute(
        """\
        SELECT * FROM enrollment
        WHERE workflow_id = %(workflow_id)s AND contact_id = %(contact_id)s
        """,
        {"workflow_id": workflow_id, "contact_id": contact_id},
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
        status_filter = SQL("AND status = %(status)s")
        params["status"] = status
    query = SQL(
        "SELECT * FROM enrollment "
        "WHERE workflow_id = %(workflow_id)s {} "
        "ORDER BY created_at"
    ).format(status_filter)
    rows = connection.execute(query, params).fetchall()
    return [Enrollment.model_validate(row) for row in rows]


def delete_enrollment(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> bool:
    """Remove an enrollment.

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        contact_id: Contact FK.

    Returns:
        True if the row was deleted, False if not found.
    """
    cursor = connection.execute(
        """\
        DELETE FROM enrollment
        WHERE workflow_id = %(workflow_id)s AND contact_id = %(contact_id)s
        """,
        {"workflow_id": workflow_id, "contact_id": contact_id},
    )
    connection.commit()
    return cursor.rowcount > 0


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
        "SELECT e.workflow_id, e.contact_id, e.status, e.updated_at, "
        "c.email AS contact_email, "
        "TRIM(COALESCE(c.first_name, '') || ' ' || COALESCE(c.last_name, '')) "
        "AS contact_name "
        "FROM enrollment e "
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
            contact_id, workflow_id, status, is_routed,
            received_at, sent_at, labels, rfc2822_message_id,
            in_reply_to, references_header,
            sender, recipients)
        VALUES (%(id)s, %(account_id)s, %(direction)s,
            %(subject)s, %(body_text)s, %(gmail_message_id)s,
            %(gmail_thread_id)s, %(contact_id)s, %(workflow_id)s,
            %(status)s, %(is_routed)s, %(received_at)s, %(sent_at)s,
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

    Returns:
        List of email summaries ordered by ``COALESCE(sent_at, received_at)``
        descending -- the same expression used by the ``since`` filter, so
        operators can page newest-first using a timestamp visible in
        ``EmailSummary``.
    """
    conditions: list[SQL] = []
    params: dict[str, object] = {"limit": limit}
    if contact_id is not None:
        conditions.append(SQL("contact_id = %(contact_id)s"))
        params["contact_id"] = contact_id
    if account_id is not None:
        conditions.append(SQL("account_id = %(account_id)s"))
        params["account_id"] = account_id
    if since is not None:
        conditions.append(SQL("COALESCE(sent_at, received_at) >= %(since)s"))
        params["since"] = since
    if thread_id is not None:
        conditions.append(SQL("gmail_thread_id = %(thread_id)s"))
        params["thread_id"] = thread_id
    if direction is not None:
        conditions.append(SQL("direction = %(direction)s"))
        params["direction"] = direction
    if workflow_id is not None:
        conditions.append(SQL("workflow_id = %(workflow_id)s"))
        params["workflow_id"] = workflow_id
    if status is not None:
        conditions.append(SQL("status = %(status)s"))
        params["status"] = status
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
        "subject, sender, status, sent_at, received_at "
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
        "subject, sender, status, sent_at, received_at "
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
    workflow_id: str,
    contact_id: str,
    description: str,
    scheduled_at: str,
    context: dict[str, object] | None = None,
    email_id: str | None = None,
) -> Task:
    """Create a deferred task.

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        contact_id: Contact FK (every task targets a contact).
        description: What the agent should do.
        scheduled_at: When to execute (ISO timestamp).
        context: Arbitrary JSON context for the agent.
        email_id: Optional triggering email FK.

    Returns:
        Created task.
    """
    row = connection.execute(
        """\
        INSERT INTO task (id, workflow_id, contact_id, email_id,
            description, context, scheduled_at)
        VALUES (%(id)s, %(workflow_id)s, %(contact_id)s, %(email_id)s,
                %(description)s, %(context)s, %(scheduled_at)s)
        RETURNING *
        """,
        {
            "id": _new_id(),
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
        "SELECT id, workflow_id, contact_id, email_id, description, "
        "scheduled_at, status "
        "FROM task {} ORDER BY scheduled_at DESC LIMIT %(limit)s"
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [TaskSummary.model_validate(row) for row in rows]


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
    row, and creates a task with scheduled_at=now() for each.

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
        SELECT e.id, e.workflow_id, e.contact_id FROM email e
        JOIN workflow w ON w.id = e.workflow_id
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
) -> Activity:
    """Create an activity event.

    At least one of ``contact_id`` or ``company_id`` must be set.
    Structured FK columns (``email_id``, ``workflow_id``, ``task_id``)
    let reports join activity to source records without parsing
    ``detail`` JSON.

    Raises:
        ValueError: If neither contact_id nor company_id is provided.
    """
    if contact_id is None and company_id is None:
        raise ValueError("at least one of contact_id or company_id is required")
    row = connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, email_id, workflow_id, task_id,
            type, summary, detail
        )
        VALUES (
            %(id)s, %(contact_id)s, %(company_id)s, %(email_id)s,
            %(workflow_id)s, %(task_id)s,
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
        "SELECT id, contact_id, company_id, type, summary, created_at "
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


def delete_tag(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    contact_id: str | None = None,
    company_id: str | None = None,
) -> bool:
    """Remove a tag from a contact or company.

    Raises:
        ValueError: If neither or both of contact_id/company_id are set.
    """
    if (contact_id is None) == (company_id is None):
        raise ValueError("exactly one of contact_id or company_id is required")
    normalized = _normalize_tag_name(name)
    if contact_id is not None:
        cursor = connection.execute(
            "DELETE FROM tag WHERE contact_id = %(contact_id)s AND name = %(name)s",
            {"contact_id": contact_id, "name": normalized},
        )
    else:
        cursor = connection.execute(
            "DELETE FROM tag WHERE company_id = %(company_id)s AND name = %(name)s",
            {"company_id": company_id, "name": normalized},
        )
    connection.commit()
    return cursor.rowcount > 0


def list_tags(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str | None = None,
    company_id: str | None = None,
    limit: int = 100,
    since: str | None = None,
) -> list[Tag]:
    """List tags on a contact or company.

    Tag has no Summary projection -- the full row already matches the
    summary contract.

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
    query = SQL("SELECT * FROM tag {} ORDER BY name LIMIT %(limit)s").format(where)
    rows = connection.execute(query, params).fetchall()
    return [Tag.model_validate(row) for row in rows]


def list_contacts_by_tag(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    limit: int = 100,
) -> list[str]:
    """Return contact IDs with the given tag (normalized)."""
    normalized = _normalize_tag_name(name)
    rows = connection.execute(
        """\
        SELECT contact_id FROM tag
        WHERE contact_id IS NOT NULL AND name = %(name)s
        ORDER BY created_at
        LIMIT %(limit)s
        """,
        {"name": normalized, "limit": limit},
    ).fetchall()
    return [row["contact_id"] for row in rows]


def list_companies_by_tag(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    limit: int = 100,
) -> list[str]:
    """Return company IDs with the given tag (normalized)."""
    normalized = _normalize_tag_name(name)
    rows = connection.execute(
        """\
        SELECT company_id FROM tag
        WHERE company_id IS NOT NULL AND name = %(name)s
        ORDER BY created_at
        LIMIT %(limit)s
        """,
        {"name": normalized, "limit": limit},
    ).fetchall()
    return [row["company_id"] for row in rows]


def search_tags(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    owner: str | None = None,
    limit: int = 100,
) -> list[Tag]:
    """Search tags by name pattern with optional owner filter.

    Args:
        owner: ``"contact"`` to limit to contact tags, ``"company"`` to
            limit to company tags, ``None`` for both.
    """
    if owner not in (None, "contact", "company"):
        raise ValueError("owner must be 'contact', 'company', or None")
    pattern = f"%{name.strip().lower()}%"
    params: dict[str, object] = {"pattern": pattern, "limit": limit}
    owner_filter = SQL("")
    if owner == "contact":
        owner_filter = SQL("AND contact_id IS NOT NULL")
    elif owner == "company":
        owner_filter = SQL("AND company_id IS NOT NULL")
    query = SQL(
        "SELECT * FROM tag WHERE name LIKE %(pattern)s {} "
        "ORDER BY name LIMIT %(limit)s"
    ).format(owner_filter)
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


def remove_contact_tag(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
    name: str,
) -> bool:
    """Remove a tag from a contact and emit a `tag_removed` activity atomically."""
    normalized = _normalize_tag_name(name)
    contact_row = connection.execute(
        "SELECT company_id FROM contact WHERE id = %s", (contact_id,)
    ).fetchone()
    if contact_row is None:
        raise ValueError(f"contact not found: {contact_id}")
    cursor = connection.execute(
        "DELETE FROM tag WHERE contact_id = %s AND name = %s",
        (contact_id, normalized),
    )
    if cursor.rowcount == 0:
        connection.commit()
        return False
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, type, summary, detail
        )
        VALUES (
            %(id)s, %(contact_id)s, %(company_id)s,
            'tag_removed', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "contact_id": contact_id,
            "company_id": contact_row["company_id"],
            "summary": f"Removed tag {normalized}",
            "detail": Json({"tag": normalized}),
        },
    )
    connection.commit()
    return True


def remove_company_tag(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
    name: str,
) -> bool:
    """Remove a tag from a company and emit a `tag_removed` activity atomically."""
    normalized = _normalize_tag_name(name)
    cursor = connection.execute(
        "DELETE FROM tag WHERE company_id = %s AND name = %s",
        (company_id, normalized),
    )
    if cursor.rowcount == 0:
        connection.commit()
        return False
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, type, summary, detail
        )
        VALUES (
            %(id)s, NULL, %(company_id)s,
            'tag_removed', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "company_id": company_id,
            "summary": f"Removed tag {normalized}",
            "detail": Json({"tag": normalized}),
        },
    )
    connection.commit()
    return True


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
