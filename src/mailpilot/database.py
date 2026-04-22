"""PostgreSQL database for CRM persistence.

Single flat module with section headers per entity. All CRUD functions follow
consistent signatures and return domain models from ``models.py``.

Convention:
    create_X(connection, ...) -> X
    get_X(connection, id) -> X | None
    list_X(connection, ...) -> list[X]
    update_X(connection, id, ...) -> X
"""

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
    Activity,
    Company,
    Contact,
    Email,
    Note,
    SyncStatus,
    Tag,
    Task,
    Workflow,
    WorkflowContact,
    WorkflowContactDetail,
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
    with logfire.span("db.schema.apply", database=db_name) as span:
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
        logfire.instrument_psycopg(connection)
        schema_sql = SCHEMA_PATH.read_text()
        connection.execute(schema_sql)  # type: ignore[arg-type]
        connection.autocommit = False
        span.set_attribute("schema_applied", True)
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
) -> list[Account]:
    """List all accounts.

    Args:
        connection: Open database connection.

    Returns:
        List of accounts ordered by creation time.
    """
    rows = connection.execute("SELECT * FROM account ORDER BY created_at").fetchall()
    return [Account.model_validate(row) for row in rows]


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
) -> list[Company]:
    """List companies.

    Args:
        connection: Open database connection.
        limit: Maximum number of companies to return.

    Returns:
        List of companies ordered by name.
    """
    rows = connection.execute(
        "SELECT * FROM company ORDER BY LOWER(name) LIMIT %(limit)s",
        {"limit": limit},
    ).fetchall()
    return [Company.model_validate(row) for row in rows]


def search_companies(
    connection: psycopg.Connection[dict[str, Any]],
    query: str,
    limit: int = 100,
) -> list[Company]:
    """Search companies by name or domain.

    Args:
        connection: Open database connection.
        query: Search term (matched against name and domain).
        limit: Maximum number of results.

    Returns:
        Matching companies ordered by name.
    """
    pattern = f"%{query}%"
    rows = connection.execute(
        """\
        SELECT * FROM company
        WHERE LOWER(name) LIKE LOWER(%(pattern)s)
           OR LOWER(domain) LIKE LOWER(%(pattern)s)
        ORDER BY LOWER(name)
        LIMIT %(limit)s
        """,
        {"pattern": pattern, "limit": limit},
    ).fetchall()
    return [Company.model_validate(row) for row in rows]


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
) -> list[Contact]:
    """List contacts with optional filters.

    Args:
        connection: Open database connection.
        limit: Maximum number of contacts to return.
        domain: Filter by domain.
        company_id: Filter by company ID.
        status: Filter by contact status ("active", "bounced", "unsubscribed").

    Returns:
        List of contacts ordered by email.
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
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL("SELECT * FROM contact {} ORDER BY email LIMIT %(limit)s").format(where)
    rows = connection.execute(query, params).fetchall()
    return [Contact.model_validate(row) for row in rows]


def search_contacts(
    connection: psycopg.Connection[dict[str, Any]],
    query: str,
    limit: int = 100,
) -> list[Contact]:
    """Search contacts by email, name, or domain.

    Args:
        connection: Open database connection.
        query: Search term.
        limit: Maximum number of results.

    Returns:
        Matching contacts ordered by email.
    """
    pattern = f"%{query}%"
    rows = connection.execute(
        """\
        SELECT * FROM contact
        WHERE LOWER(email) LIKE LOWER(%(pattern)s)
           OR LOWER(COALESCE(first_name, '')) LIKE LOWER(%(pattern)s)
           OR LOWER(COALESCE(last_name, '')) LIKE LOWER(%(pattern)s)
           OR LOWER(domain) LIKE LOWER(%(pattern)s)
        ORDER BY email
        LIMIT %(limit)s
        """,
        {"pattern": pattern, "limit": limit},
    ).fetchall()
    return [Contact.model_validate(row) for row in rows]


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
) -> Workflow:
    """Create a new workflow.

    Args:
        connection: Open database connection.
        name: Workflow name.
        workflow_type: "inbound" or "outbound".
        account_id: Account FK.

    Returns:
        Created workflow.
    """
    row = connection.execute(
        """\
        INSERT INTO workflow (id, name, type, account_id)
        VALUES (%(id)s, %(name)s, %(type)s, %(account_id)s)
        RETURNING *
        """,
        {
            "id": _new_id(),
            "name": name,
            "type": workflow_type,
            "account_id": account_id,
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
) -> list[Workflow]:
    """List workflows with optional account, status, and type filters.

    Args:
        connection: Open database connection.
        account_id: Filter by account ID.
        status: Filter by workflow status (e.g., "active").
        workflow_type: Filter by workflow type ("inbound" or "outbound").

    Returns:
        List of workflows ordered by creation time.
    """
    conditions: list[SQL] = []
    params: dict[str, object] = {}
    if account_id is not None:
        conditions.append(SQL("account_id = %(account_id)s"))
        params["account_id"] = account_id
    if status is not None:
        conditions.append(SQL("status = %(status)s"))
        params["status"] = status
    if workflow_type is not None:
        conditions.append(SQL("type = %(workflow_type)s"))
        params["workflow_type"] = workflow_type
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL("SELECT * FROM workflow {} ORDER BY created_at").format(where)
    rows = connection.execute(query, params).fetchall()
    return [Workflow.model_validate(row) for row in rows]


def search_workflows(
    connection: psycopg.Connection[dict[str, Any]],
    query: str,
    limit: int = 100,
) -> list[Workflow]:
    """Search workflows by name or objective.

    Args:
        connection: Open database connection.
        query: Search term (matched against name and objective).
        limit: Maximum number of results.

    Returns:
        Matching workflows ordered by name.
    """
    pattern = f"%{query}%"
    rows = connection.execute(
        """\
        SELECT * FROM workflow
        WHERE LOWER(name) LIKE LOWER(%(pattern)s)
           OR LOWER(objective) LIKE LOWER(%(pattern)s)
        ORDER BY LOWER(name)
        LIMIT %(limit)s
        """,
        {"pattern": pattern, "limit": limit},
    ).fetchall()
    return [Workflow.model_validate(row) for row in rows]


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
    allowed = {"name", "objective", "instructions"}
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


# -- Workflow Contact ----------------------------------------------------------


def create_workflow_contact(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> WorkflowContact | None:
    """Add a contact to a workflow.

    Uses ON CONFLICT DO NOTHING so callers can safely re-invoke without
    catching unique-constraint errors. Returns None when the row already
    exists (same pattern as ``create_email``).

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        contact_id: Contact FK.

    Returns:
        Created workflow-contact link, or None if it already existed.
    """
    row = connection.execute(
        """\
        INSERT INTO workflow_contact (workflow_id, contact_id)
        VALUES (%(workflow_id)s, %(contact_id)s)
        ON CONFLICT (workflow_id, contact_id) DO NOTHING
        RETURNING *
        """,
        {"workflow_id": workflow_id, "contact_id": contact_id},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return WorkflowContact.model_validate(row)


def update_workflow_contact(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
    **fields: object,
) -> WorkflowContact | None:
    """Update a workflow-contact link.

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        contact_id: Contact FK.
        **fields: Fields to update (status, reason).

    Returns:
        Updated workflow-contact, or None if not found.
    """
    allowed = {"status", "reason"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_workflow_contact(connection, workflow_id, contact_id)
    updates["workflow_id"] = workflow_id
    updates["contact_id"] = contact_id
    where = SQL("workflow_id = %(workflow_id)s AND contact_id = %(contact_id)s")
    query = _build_update("workflow_contact", updates, where)
    row = connection.execute(query, updates).fetchone()
    connection.commit()
    if row is None:
        return None
    return WorkflowContact.model_validate(row)


def get_workflow_contact(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> WorkflowContact | None:
    """Get a workflow-contact link.

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        contact_id: Contact FK.

    Returns:
        WorkflowContact if found, None otherwise.
    """
    row = connection.execute(
        """\
        SELECT * FROM workflow_contact
        WHERE workflow_id = %(workflow_id)s AND contact_id = %(contact_id)s
        """,
        {"workflow_id": workflow_id, "contact_id": contact_id},
    ).fetchone()
    if row is None:
        return None
    return WorkflowContact.model_validate(row)


def list_workflow_contacts(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    status: str | None = None,
) -> list[WorkflowContact]:
    """List contacts in a workflow with optional status filter.

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        status: Filter by contact outcome status.

    Returns:
        List of workflow-contact links.
    """
    params: dict[str, object] = {"workflow_id": workflow_id}
    status_filter = SQL("")
    if status is not None:
        status_filter = SQL("AND status = %(status)s")
        params["status"] = status
    query = SQL(
        "SELECT * FROM workflow_contact "
        "WHERE workflow_id = %(workflow_id)s {} "
        "ORDER BY created_at"
    ).format(status_filter)
    rows = connection.execute(query, params).fetchall()
    return [WorkflowContact.model_validate(row) for row in rows]


def delete_workflow_contact(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> bool:
    """Remove a contact from a workflow.

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        contact_id: Contact FK.

    Returns:
        True if the row was deleted, False if not found.
    """
    cursor = connection.execute(
        """\
        DELETE FROM workflow_contact
        WHERE workflow_id = %(workflow_id)s AND contact_id = %(contact_id)s
        """,
        {"workflow_id": workflow_id, "contact_id": contact_id},
    )
    connection.commit()
    return cursor.rowcount > 0


def list_workflow_contacts_enriched(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    status: str | None = None,
    limit: int = 100,
) -> list[WorkflowContactDetail]:
    """List contacts in a workflow with enriched contact info.

    JOINs the contact table to include email and name. Separate from
    ``list_workflow_contacts`` to avoid breaking agent tools which
    expect ``list[WorkflowContact]``.

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        status: Filter by contact outcome status.
        limit: Maximum results (default 100).

    Returns:
        List of enriched workflow-contact details.
    """
    params: dict[str, object] = {"workflow_id": workflow_id, "limit": limit}
    status_filter = SQL("")
    if status is not None:
        status_filter = SQL("AND wc.status = %(status)s")
        params["status"] = status
    query = SQL(
        "SELECT wc.*, c.email AS contact_email, "
        "TRIM(COALESCE(c.first_name, '') || ' ' || COALESCE(c.last_name, '')) "
        "AS contact_name "
        "FROM workflow_contact wc "
        "JOIN contact c ON c.id = wc.contact_id "
        "WHERE wc.workflow_id = %(workflow_id)s {} "
        "ORDER BY wc.created_at "
        "LIMIT %(limit)s"
    ).format(status_filter)
    rows = connection.execute(query, params).fetchall()
    return [WorkflowContactDetail.model_validate(row) for row in rows]


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

    Returns:
        Created email, or None if another worker already stored a row with
        the same ``gmail_message_id``.
    """
    row = connection.execute(
        """\
        INSERT INTO email (id, account_id, direction, subject,
            body_text, gmail_message_id, gmail_thread_id,
            contact_id, workflow_id, status, is_routed,
            received_at, sent_at, labels)
        VALUES (%(id)s, %(account_id)s, %(direction)s,
            %(subject)s, %(body_text)s, %(gmail_message_id)s,
            %(gmail_thread_id)s, %(contact_id)s, %(workflow_id)s,
            %(status)s, %(is_routed)s, %(received_at)s, %(sent_at)s,
            %(labels)s)
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
) -> list[Email]:
    """List emails with optional filters.

    Args:
        connection: Open database connection.
        limit: Maximum number of emails to return.
        contact_id: Filter by contact ID.
        account_id: Filter by account ID.
        since: ISO datetime lower bound for COALESCE(sent_at, received_at).
        thread_id: Filter by Gmail thread ID.
        direction: Filter by direction ("inbound" or "outbound").
        workflow_id: Filter by workflow ID.
        status: Filter by email status ("sent", "received", "bounced").

    Returns:
        List of emails ordered by creation time descending.
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
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT * FROM email {} ORDER BY created_at DESC LIMIT %(limit)s"
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [Email.model_validate(row) for row in rows]


def search_emails(
    connection: psycopg.Connection[dict[str, Any]],
    query: str,
    limit: int = 100,
    account_id: str | None = None,
) -> list[Email]:
    """Search emails by subject or body text.

    Args:
        connection: Open database connection.
        query: Search term.
        limit: Maximum number of results.
        account_id: Filter by account ID.

    Returns:
        Matching emails ordered by creation time descending.
    """
    pattern = f"%{query}%"
    params: dict[str, object] = {"pattern": pattern, "limit": limit}
    account_filter = SQL("")
    if account_id is not None:
        account_filter = SQL("AND account_id = %(account_id)s")
        params["account_id"] = account_id
    query_sql = SQL(
        "SELECT * FROM email "
        "WHERE (LOWER(subject) LIKE LOWER(%(pattern)s) "
        "   OR LOWER(body_text) LIKE LOWER(%(pattern)s)) "
        "{} "
        "ORDER BY created_at DESC "
        "LIMIT %(limit)s"
    ).format(account_filter)
    rows = connection.execute(query_sql, params).fetchall()
    return [Email.model_validate(row) for row in rows]


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


def get_last_cold_outbound(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    contact_id: str,
) -> Email | None:
    """Get the most recent cold outbound email to a contact.

    A cold outbound email is the first outbound message in its Gmail
    thread (no prior outbound in the same thread). This distinguishes
    initial outreach from follow-up replies within an existing
    conversation. Used by the ``send_email`` agent tool for cooldown
    enforcement.

    Args:
        connection: Open database connection.
        account_id: Sending account.
        contact_id: Recipient contact.

    Returns:
        Most recent cold outbound email, or None if none exists.
    """
    row = connection.execute(
        """\
        SELECT e.* FROM email e
        WHERE e.account_id = %(account_id)s
          AND e.contact_id = %(contact_id)s
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
        {"account_id": account_id, "contact_id": contact_id},
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
    allowed = {"workflow_id", "is_routed", "status", "contact_id"}
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
) -> list[Task]:
    """List tasks with optional filters.

    Args:
        connection: Open database connection.
        workflow_id: Filter by workflow ID.
        contact_id: Filter by contact ID.
        status: Filter by task status.
        limit: Maximum number of tasks to return.

    Returns:
        List of tasks ordered by scheduled_at descending.
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
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT * FROM task {} ORDER BY scheduled_at DESC LIMIT %(limit)s"
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [Task.model_validate(row) for row in rows]


def create_tasks_for_routed_emails(
    connection: psycopg.Connection[dict[str, Any]],
) -> list[Task]:
    """Create immediate tasks for routed inbound emails without tasks.

    Finds inbound emails with workflow_id set but no corresponding task
    row, and creates a task with scheduled_at=now() for each.

    Args:
        connection: Open database connection.

    Returns:
        List of newly created tasks.
    """
    unmatched = connection.execute(
        """\
        SELECT e.id, e.workflow_id, e.contact_id FROM email e
        WHERE e.workflow_id IS NOT NULL
          AND e.direction = 'inbound'
          AND e.contact_id IS NOT NULL
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
    contact_id: str,
    activity_type: str,
    summary: str = "",
    detail: dict[str, object] | None = None,
    company_id: str | None = None,
) -> Activity:
    """Create an activity event in a contact's timeline.

    Args:
        connection: Open database connection.
        contact_id: Contact FK (every activity relates to a contact).
        activity_type: Activity type (email_sent, tag_added, etc.).
        summary: One-line human-readable description.
        detail: Type-specific JSON data (email_id, tag name, etc.).
        company_id: Optional company FK for company-level views.

    Returns:
        Created activity.
    """
    row = connection.execute(
        """\
        INSERT INTO activity (id, contact_id, company_id, type, summary, detail)
        VALUES (%(id)s, %(contact_id)s, %(company_id)s, %(type)s,
                %(summary)s, %(detail)s)
        RETURNING *
        """,
        {
            "id": _new_id(),
            "contact_id": contact_id,
            "company_id": company_id,
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
) -> list[Activity]:
    """List activities with required contact or company filter.

    At least one of ``contact_id`` or ``company_id`` must be provided.

    Args:
        connection: Open database connection.
        contact_id: Filter by contact ID.
        company_id: Filter by company ID.
        activity_type: Filter by activity type.
        limit: Maximum number of results.
        since: ISO datetime lower bound for created_at.

    Returns:
        Activities ordered by created_at descending.

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
        "SELECT * FROM activity {} ORDER BY created_at DESC LIMIT %(limit)s"
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [Activity.model_validate(row) for row in rows]


# -- Tag -----------------------------------------------------------------------


def create_tag(
    connection: psycopg.Connection[dict[str, Any]],
    entity_type: str,
    entity_id: str,
    name: str,
) -> Tag | None:
    """Create a tag on a contact or company.

    Normalizes the tag name to lowercase with leading/trailing whitespace
    stripped. Uses ON CONFLICT DO NOTHING so duplicate tags are silently
    ignored.

    Args:
        connection: Open database connection.
        entity_type: "contact" or "company".
        entity_id: ID of the tagged entity.
        name: Tag name (normalized to lowercase).

    Returns:
        Created tag, or None if the tag already exists.
    """
    normalized = name.strip().lower()
    row = connection.execute(
        """\
        INSERT INTO tag (id, entity_type, entity_id, name)
        VALUES (%(id)s, %(entity_type)s, %(entity_id)s, %(name)s)
        ON CONFLICT (entity_type, entity_id, name) DO NOTHING
        RETURNING *
        """,
        {
            "id": _new_id(),
            "entity_type": entity_type,
            "entity_id": entity_id,
            "name": normalized,
        },
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Tag.model_validate(row)


def delete_tag(
    connection: psycopg.Connection[dict[str, Any]],
    entity_type: str,
    entity_id: str,
    name: str,
) -> bool:
    """Remove a tag from a contact or company.

    Args:
        connection: Open database connection.
        entity_type: "contact" or "company".
        entity_id: ID of the tagged entity.
        name: Tag name (normalized to lowercase for matching).

    Returns:
        True if the tag was deleted, False if it was not found.
    """
    normalized = name.strip().lower()
    cursor = connection.execute(
        """\
        DELETE FROM tag
        WHERE entity_type = %(entity_type)s
          AND entity_id = %(entity_id)s
          AND name = %(name)s
        """,
        {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "name": normalized,
        },
    )
    connection.commit()
    return cursor.rowcount > 0


def list_tags(
    connection: psycopg.Connection[dict[str, Any]],
    entity_type: str,
    entity_id: str,
) -> list[Tag]:
    """List all tags on a contact or company.

    Args:
        connection: Open database connection.
        entity_type: "contact" or "company".
        entity_id: ID of the tagged entity.

    Returns:
        Tags ordered by name.
    """
    rows = connection.execute(
        """\
        SELECT * FROM tag
        WHERE entity_type = %(entity_type)s AND entity_id = %(entity_id)s
        ORDER BY name
        """,
        {"entity_type": entity_type, "entity_id": entity_id},
    ).fetchall()
    return [Tag.model_validate(row) for row in rows]


def list_entities_by_tag(
    connection: psycopg.Connection[dict[str, Any]],
    entity_type: str,
    name: str,
    limit: int = 100,
) -> list[str]:
    """Find all entities with a given tag.

    Args:
        connection: Open database connection.
        entity_type: "contact" or "company".
        name: Tag name to search for.
        limit: Maximum number of entity IDs to return.

    Returns:
        Entity IDs matching the tag.
    """
    normalized = name.strip().lower()
    rows = connection.execute(
        """\
        SELECT entity_id FROM tag
        WHERE entity_type = %(entity_type)s AND name = %(name)s
        ORDER BY created_at
        LIMIT %(limit)s
        """,
        {"entity_type": entity_type, "name": normalized, "limit": limit},
    ).fetchall()
    return [row["entity_id"] for row in rows]


def search_tags(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    entity_type: str | None = None,
    limit: int = 100,
) -> list[Tag]:
    """Search tags by name with optional entity type filter.

    Args:
        connection: Open database connection.
        name: Tag name pattern (LIKE match).
        entity_type: Optional filter by "contact" or "company".
        limit: Maximum number of results.

    Returns:
        Matching tags ordered by name.
    """
    pattern = f"%{name.strip().lower()}%"
    params: dict[str, object] = {"pattern": pattern, "limit": limit}
    type_filter = SQL("")
    if entity_type is not None:
        type_filter = SQL("AND entity_type = %(entity_type)s")
        params["entity_type"] = entity_type
    query = SQL(
        "SELECT * FROM tag WHERE name LIKE %(pattern)s {} ORDER BY name LIMIT %(limit)s"
    ).format(type_filter)
    rows = connection.execute(query, params).fetchall()
    return [Tag.model_validate(row) for row in rows]


# -- Note ----------------------------------------------------------------------


def create_note(
    connection: psycopg.Connection[dict[str, Any]],
    entity_type: str,
    entity_id: str,
    body: str,
) -> Note:
    """Create a freeform text note on a contact or company.

    Args:
        connection: Open database connection.
        entity_type: "contact" or "company".
        entity_id: ID of the annotated entity.
        body: Note text.

    Returns:
        Created note.
    """
    row = connection.execute(
        """\
        INSERT INTO note (id, entity_type, entity_id, body)
        VALUES (%(id)s, %(entity_type)s, %(entity_id)s, %(body)s)
        RETURNING *
        """,
        {
            "id": _new_id(),
            "entity_type": entity_type,
            "entity_id": entity_id,
            "body": body,
        },
    ).fetchone()
    connection.commit()
    return Note.model_validate(row)


def list_notes(
    connection: psycopg.Connection[dict[str, Any]],
    entity_type: str,
    entity_id: str,
    limit: int = 100,
    since: str | None = None,
) -> list[Note]:
    """List notes on a contact or company.

    Args:
        connection: Open database connection.
        entity_type: "contact" or "company".
        entity_id: ID of the annotated entity.
        limit: Maximum number of notes to return.
        since: ISO datetime lower bound for created_at.

    Returns:
        Notes ordered by created_at descending.
    """
    conditions: list[SQL] = [
        SQL("entity_type = %(entity_type)s"),
        SQL("entity_id = %(entity_id)s"),
    ]
    params: dict[str, object] = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "limit": limit,
    }
    if since is not None:
        conditions.append(SQL("created_at >= %(since)s"))
        params["since"] = since
    where = SQL("WHERE ") + SQL(" AND ").join(conditions)
    query = SQL(
        "SELECT * FROM note {} ORDER BY created_at DESC LIMIT %(limit)s"
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [Note.model_validate(row) for row in rows]


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
