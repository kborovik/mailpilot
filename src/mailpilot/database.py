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
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import logfire
import psycopg
from psycopg.rows import dict_row
from psycopg.sql import SQL, Composed, Identifier, Placeholder
from psycopg.types.json import Json

from mailpilot.models import (
    Account,
    Company,
    Contact,
    Email,
    SyncStatus,
    Task,
    Workflow,
    WorkflowContact,
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
    with logfire.span("db.account.create", email=email) as span:
        row = connection.execute(
            """\
            INSERT INTO account (id, email, display_name)
            VALUES (%(id)s, %(email)s, %(display_name)s)
            RETURNING *
            """,
            {"id": _new_id(), "email": email, "display_name": display_name},
        ).fetchone()
        connection.commit()
        account = Account.model_validate(row)
        span.set_attribute("account_id", account.id)
        return account


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
    with logfire.span("db.account.get", account_id=account_id) as span:
        row = connection.execute(
            "SELECT * FROM account WHERE id = %(id)s",
            {"id": account_id},
        ).fetchone()
        span.set_attribute("hit", row is not None)
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
    with logfire.span("db.account.list") as span:
        rows = connection.execute(
            "SELECT * FROM account ORDER BY created_at"
        ).fetchall()
        accounts = [Account.model_validate(row) for row in rows]
        span.set_attribute("account_count", len(accounts))
        return accounts


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
    with logfire.span(
        "db.account.update",
        account_id=account_id,
        updated_fields=sorted(updates.keys()),
    ) as span:
        if not updates:
            existing = get_account(connection, account_id)
            span.set_attribute("hit", existing is not None)
            return existing
        updates["id"] = account_id
        query = _build_update("account", updates, SQL("id = %(id)s"))
        row = connection.execute(query, updates).fetchone()
        connection.commit()
        span.set_attribute("hit", row is not None)
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
    with logfire.span("db.company.create", domain=domain) as span:
        row = connection.execute(
            """\
            INSERT INTO company (id, name, domain)
            VALUES (%(id)s, %(name)s, %(domain)s)
            RETURNING *
            """,
            {"id": _new_id(), "name": name, "domain": domain},
        ).fetchone()
        connection.commit()
        company = Company.model_validate(row)
        span.set_attribute("company_id", company.id)
        return company


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
    with logfire.span("db.company.get", company_id=company_id) as span:
        row = connection.execute(
            "SELECT * FROM company WHERE id = %(id)s",
            {"id": company_id},
        ).fetchone()
        span.set_attribute("hit", row is not None)
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
    with logfire.span("db.company.list", limit=limit) as span:
        rows = connection.execute(
            "SELECT * FROM company ORDER BY LOWER(name) LIMIT %(limit)s",
            {"limit": limit},
        ).fetchall()
        companies = [Company.model_validate(row) for row in rows]
        span.set_attribute("company_count", len(companies))
        return companies


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
    with logfire.span("db.company.search", query=query, limit=limit) as span:
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
        companies = [Company.model_validate(row) for row in rows]
        span.set_attribute("company_count", len(companies))
        return companies


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
    with logfire.span(
        "db.company.update",
        company_id=company_id,
        updated_fields=sorted(updates.keys()),
    ) as span:
        if not updates:
            existing = get_company(connection, company_id)
            span.set_attribute("hit", existing is not None)
            return existing
        updates["id"] = company_id
        query = _build_update("company", updates, SQL("id = %(id)s"))
        row = connection.execute(query, updates).fetchone()
        connection.commit()
        span.set_attribute("hit", row is not None)
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
    with logfire.span(
        "db.contact.create",
        email=email,
        domain=domain,
        company_id=company_id,
    ) as span:
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
        contact = Contact.model_validate(row)
        span.set_attribute("contact_id", contact.id)
        return contact


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
    with logfire.span("db.contact.get", contact_id=contact_id) as span:
        row = connection.execute(
            "SELECT * FROM contact WHERE id = %(id)s",
            {"id": contact_id},
        ).fetchone()
        span.set_attribute("hit", row is not None)
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
    with logfire.span("db.contact.get_by_email", email=email) as span:
        row = connection.execute(
            "SELECT * FROM contact WHERE email = %(email)s",
            {"email": email},
        ).fetchone()
        span.set_attribute("hit", row is not None)
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
    with logfire.span("db.contact.create_or_get", email=email) as span:
        existing = get_contact_by_email(connection, email)
        if existing is not None:
            span.set_attribute("created", False)
            span.set_attribute("contact_id", existing.id)
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
        created = create_contact(
            connection,
            email=email,
            domain=domain,
            first_name=first_name,
            last_name=last_name,
        )
        span.set_attribute("created", True)
        span.set_attribute("contact_id", created.id)
        return created


def list_contacts(
    connection: psycopg.Connection[dict[str, Any]],
    limit: int = 100,
    domain: str | None = None,
    company_id: str | None = None,
) -> list[Contact]:
    """List contacts with optional filters.

    Args:
        connection: Open database connection.
        limit: Maximum number of contacts to return.
        domain: Filter by domain.
        company_id: Filter by company ID.

    Returns:
        List of contacts ordered by email.
    """
    with logfire.span(
        "db.contact.list",
        limit=limit,
        domain=domain,
        company_id=company_id,
    ) as span:
        conditions: list[SQL] = []
        params: dict[str, object] = {"limit": limit}
        if domain is not None:
            conditions.append(SQL("domain = %(domain)s"))
            params["domain"] = domain
        if company_id is not None:
            conditions.append(SQL("company_id = %(company_id)s"))
            params["company_id"] = company_id
        where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
        query = SQL("SELECT * FROM contact {} ORDER BY email LIMIT %(limit)s").format(
            where
        )
        rows = connection.execute(query, params).fetchall()
        contacts = [Contact.model_validate(row) for row in rows]
        span.set_attribute("contact_count", len(contacts))
        return contacts


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
    with logfire.span("db.contact.search", query=query, limit=limit) as span:
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
        contacts = [Contact.model_validate(row) for row in rows]
        span.set_attribute("contact_count", len(contacts))
        return contacts


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
    with logfire.span(
        "db.contact.update",
        contact_id=contact_id,
        updated_fields=sorted(updates.keys()),
    ) as span:
        if not updates:
            existing = get_contact(connection, contact_id)
            span.set_attribute("hit", existing is not None)
            return existing
        updates["id"] = contact_id
        query = _build_update("contact", updates, SQL("id = %(id)s"))
        row = connection.execute(query, updates).fetchone()
        connection.commit()
        span.set_attribute("hit", row is not None)
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
    with logfire.span(
        "db.contact.disable",
        contact_id=contact_id,
        status=status,
    ) as span:
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
        span.set_attribute("hit", row is not None)
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
    with logfire.span(
        "db.workflow.create",
        account_id=account_id,
        workflow_type=workflow_type,
    ) as span:
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
        workflow = Workflow.model_validate(row)
        span.set_attribute("workflow_id", workflow.id)
        return workflow


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
    with logfire.span("db.workflow.get", workflow_id=workflow_id) as span:
        row = connection.execute(
            "SELECT * FROM workflow WHERE id = %(id)s",
            {"id": workflow_id},
        ).fetchone()
        span.set_attribute("hit", row is not None)
        if row is None:
            return None
        return Workflow.model_validate(row)


def list_workflows(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str | None = None,
    status: str | None = None,
) -> list[Workflow]:
    """List workflows with optional account and status filters.

    Args:
        connection: Open database connection.
        account_id: Filter by account ID.
        status: Filter by workflow status (e.g., "active").

    Returns:
        List of workflows ordered by creation time.
    """
    with logfire.span(
        "db.workflow.list",
        account_id=account_id,
        status=status,
    ) as span:
        conditions: list[SQL] = []
        params: dict[str, object] = {}
        if account_id is not None:
            conditions.append(SQL("account_id = %(account_id)s"))
            params["account_id"] = account_id
        if status is not None:
            conditions.append(SQL("status = %(status)s"))
            params["status"] = status
        where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
        query = SQL("SELECT * FROM workflow {} ORDER BY created_at").format(where)
        rows = connection.execute(query, params).fetchall()
        workflows = [Workflow.model_validate(row) for row in rows]
        span.set_attribute("workflow_count", len(workflows))
        return workflows


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
    with logfire.span("db.workflow.search", query=query, limit=limit) as span:
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
        workflows = [Workflow.model_validate(row) for row in rows]
        span.set_attribute("workflow_count", len(workflows))
        return workflows


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
    with logfire.span(
        "db.workflow.update",
        workflow_id=workflow_id,
        updated_fields=sorted(updates.keys()),
    ) as span:
        if not updates:
            existing = get_workflow(connection, workflow_id)
            span.set_attribute("hit", existing is not None)
            return existing
        updates["id"] = workflow_id
        query = _build_update("workflow", updates, SQL("id = %(id)s"))
        row = connection.execute(query, updates).fetchone()
        connection.commit()
        span.set_attribute("hit", row is not None)
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
    with logfire.span("db.workflow.activate", workflow_id=workflow_id) as span:
        workflow = get_workflow(connection, workflow_id)
        if workflow is None:
            raise ValueError(f"workflow {workflow_id} not found")
        if workflow.status == "active":
            raise ValueError("workflow is already active")
        if not workflow.objective.strip():
            raise ValueError("objective must be non-empty to activate")
        if not workflow.instructions.strip():
            raise ValueError("instructions must be non-empty to activate")
        span.set_attribute("account_id", workflow.account_id)
        span.set_attribute("prior_status", workflow.status)
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
        span.set_attribute("result", "success")
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
    with logfire.span("db.workflow.pause", workflow_id=workflow_id) as span:
        workflow = get_workflow(connection, workflow_id)
        if workflow is None:
            raise ValueError(f"workflow {workflow_id} not found")
        if workflow.status != "active":
            raise ValueError(f"cannot pause workflow in status '{workflow.status}'")
        span.set_attribute("account_id", workflow.account_id)
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
        span.set_attribute("result", "success")
        return Workflow.model_validate(row)


# -- Workflow Contact ----------------------------------------------------------


def create_workflow_contact(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> WorkflowContact:
    """Add a contact to a workflow.

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        contact_id: Contact FK.

    Returns:
        Created workflow-contact link.
    """
    with logfire.span(
        "db.workflow_contact.create",
        workflow_id=workflow_id,
        contact_id=contact_id,
    ):
        row = connection.execute(
            """\
            INSERT INTO workflow_contact (workflow_id, contact_id)
            VALUES (%(workflow_id)s, %(contact_id)s)
            RETURNING *
            """,
            {"workflow_id": workflow_id, "contact_id": contact_id},
        ).fetchone()
        connection.commit()
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
    with logfire.span(
        "db.workflow_contact.update",
        workflow_id=workflow_id,
        contact_id=contact_id,
        updated_fields=sorted(updates.keys()),
    ) as span:
        if not updates:
            existing = get_workflow_contact(connection, workflow_id, contact_id)
            span.set_attribute("hit", existing is not None)
            return existing
        updates["workflow_id"] = workflow_id
        updates["contact_id"] = contact_id
        where = SQL("workflow_id = %(workflow_id)s AND contact_id = %(contact_id)s")
        query = _build_update("workflow_contact", updates, where)
        row = connection.execute(query, updates).fetchone()
        connection.commit()
        span.set_attribute("hit", row is not None)
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
    with logfire.span(
        "db.workflow_contact.get",
        workflow_id=workflow_id,
        contact_id=contact_id,
    ) as span:
        row = connection.execute(
            """\
            SELECT * FROM workflow_contact
            WHERE workflow_id = %(workflow_id)s AND contact_id = %(contact_id)s
            """,
            {"workflow_id": workflow_id, "contact_id": contact_id},
        ).fetchone()
        span.set_attribute("hit", row is not None)
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
    with logfire.span(
        "db.workflow_contact.list",
        workflow_id=workflow_id,
        status=status,
    ) as span:
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
        links = [WorkflowContact.model_validate(row) for row in rows]
        span.set_attribute("contact_count", len(links))
        return links


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
        labels: Gmail label IDs attached to the message.

    Returns:
        Created email, or None if another worker already stored a row with
        the same ``gmail_message_id``.
    """
    with logfire.span(
        "db.email.create",
        account_id=account_id,
        direction=direction,
        contact_id=contact_id,
        workflow_id=workflow_id,
    ) as span:
        row = connection.execute(
            """\
            INSERT INTO email (id, account_id, direction, subject,
                body_text, gmail_message_id, gmail_thread_id,
                contact_id, workflow_id, status, is_routed,
                received_at, labels)
            VALUES (%(id)s, %(account_id)s, %(direction)s,
                %(subject)s, %(body_text)s, %(gmail_message_id)s,
                %(gmail_thread_id)s, %(contact_id)s, %(workflow_id)s,
                %(status)s, %(is_routed)s, %(received_at)s, %(labels)s)
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
                "labels": Json(labels or []),
            },
        ).fetchone()
        connection.commit()
        if row is None:
            span.set_attribute("result", "conflict_skipped")
            return None
        email = Email.model_validate(row)
        span.set_attribute("email_id", email.id)
        return email


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
    with logfire.span("db.email.get", email_id=email_id) as span:
        row = connection.execute(
            "SELECT * FROM email WHERE id = %(id)s",
            {"id": email_id},
        ).fetchone()
        span.set_attribute("hit", row is not None)
        if row is None:
            return None
        return Email.model_validate(row)


def list_emails(
    connection: psycopg.Connection[dict[str, Any]],
    limit: int = 100,
    contact_id: str | None = None,
    account_id: str | None = None,
) -> list[Email]:
    """List emails with optional filters.

    Args:
        connection: Open database connection.
        limit: Maximum number of emails to return.
        contact_id: Filter by contact ID.
        account_id: Filter by account ID.

    Returns:
        List of emails ordered by creation time descending.
    """
    with logfire.span(
        "db.email.list",
        limit=limit,
        contact_id=contact_id,
        account_id=account_id,
    ) as span:
        conditions: list[SQL] = []
        params: dict[str, object] = {"limit": limit}
        if contact_id is not None:
            conditions.append(SQL("contact_id = %(contact_id)s"))
            params["contact_id"] = contact_id
        if account_id is not None:
            conditions.append(SQL("account_id = %(account_id)s"))
            params["account_id"] = account_id
        where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
        query = SQL(
            "SELECT * FROM email {} ORDER BY created_at DESC LIMIT %(limit)s"
        ).format(where)
        rows = connection.execute(query, params).fetchall()
        emails = [Email.model_validate(row) for row in rows]
        span.set_attribute("email_count", len(emails))
        return emails


def search_emails(
    connection: psycopg.Connection[dict[str, Any]],
    query: str,
    limit: int = 100,
) -> list[Email]:
    """Search emails by subject or body text.

    Args:
        connection: Open database connection.
        query: Search term.
        limit: Maximum number of results.

    Returns:
        Matching emails ordered by creation time descending.
    """
    with logfire.span("db.email.search", query=query, limit=limit) as span:
        pattern = f"%{query}%"
        rows = connection.execute(
            """\
            SELECT * FROM email
            WHERE LOWER(subject) LIKE LOWER(%(pattern)s)
               OR LOWER(body_text) LIKE LOWER(%(pattern)s)
            ORDER BY created_at DESC
            LIMIT %(limit)s
            """,
            {"pattern": pattern, "limit": limit},
        ).fetchall()
        emails = [Email.model_validate(row) for row in rows]
        span.set_attribute("email_count", len(emails))
        return emails


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
    with logfire.span(
        "db.email.get_by_gmail_message_id",
        gmail_message_id=gmail_message_id,
    ) as span:
        row = connection.execute(
            "SELECT * FROM email WHERE gmail_message_id = %(gmail_message_id)s",
            {"gmail_message_id": gmail_message_id},
        ).fetchone()
        span.set_attribute("hit", row is not None)
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
    with logfire.span(
        "db.email.get_by_gmail_thread_id",
        gmail_thread_id=gmail_thread_id,
    ) as span:
        rows = connection.execute(
            """\
            SELECT * FROM email
            WHERE gmail_thread_id = %(gmail_thread_id)s
            ORDER BY created_at
            """,
            {"gmail_thread_id": gmail_thread_id},
        ).fetchall()
        emails = [Email.model_validate(row) for row in rows]
        span.set_attribute("email_count", len(emails))
        return emails


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
    with logfire.span(
        "db.email.update",
        email_id=email_id,
        updated_fields=sorted(updates.keys()),
    ) as span:
        if not updates:
            existing = get_email(connection, email_id)
            span.set_attribute("hit", existing is not None)
            return existing
        updates["id"] = email_id
        # email table has no updated_at column -- use raw SQL instead of _build_update
        set_parts = [
            SQL("{} = {}").format(Identifier(k), Placeholder(k))
            for k in updates
            if k != "id"
        ]
        set_clause = SQL(", ").join(set_parts)
        query = SQL("UPDATE email SET {} WHERE id = %(id)s RETURNING *").format(
            set_clause
        )
        row = connection.execute(query, updates).fetchone()
        connection.commit()
        span.set_attribute("hit", row is not None)
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
    with logfire.span(
        "db.task.create",
        workflow_id=workflow_id,
        contact_id=contact_id,
        email_id=email_id,
    ) as span:
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
        task = Task.model_validate(row)
        span.set_attribute("task_id", task.id)
        return task


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
    with logfire.span("db.task.get", task_id=task_id) as span:
        row = connection.execute(
            "SELECT * FROM task WHERE id = %(id)s",
            {"id": task_id},
        ).fetchone()
        span.set_attribute("hit", row is not None)
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
    with logfire.span("db.task.list_pending") as span:
        rows = connection.execute(
            """\
            SELECT * FROM task
            WHERE scheduled_at <= CURRENT_TIMESTAMP AND status = 'pending'
            ORDER BY scheduled_at
            """
        ).fetchall()
        tasks = [Task.model_validate(row) for row in rows]
        span.set_attribute("task_count", len(tasks))
        return tasks


def complete_task(
    connection: psycopg.Connection[dict[str, Any]],
    task_id: str,
    status: str = "completed",
) -> Task | None:
    """Mark a task as completed or failed.

    Args:
        connection: Open database connection.
        task_id: Task ID.
        status: "completed" or "failed".

    Returns:
        Updated task, or None if not found.
    """
    with logfire.span("db.task.complete", task_id=task_id, status=status) as span:
        row = connection.execute(
            """\
            UPDATE task SET status = %(status)s, completed_at = CURRENT_TIMESTAMP
            WHERE id = %(id)s RETURNING *
            """,
            {"id": task_id, "status": status},
        ).fetchone()
        connection.commit()
        span.set_attribute("hit", row is not None)
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
    with logfire.span("db.task.cancel", task_id=task_id) as span:
        row = connection.execute(
            """\
            UPDATE task SET status = 'cancelled', completed_at = CURRENT_TIMESTAMP
            WHERE id = %(id)s AND status = 'pending'
            RETURNING *
            """,
            {"id": task_id},
        ).fetchone()
        connection.commit()
        span.set_attribute("hit", row is not None)
        if row is None:
            return None
        return Task.model_validate(row)


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
    with logfire.span("db.sync_status.upsert", pid=pid):
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
    with logfire.span("db.sync_status.get") as span:
        row = connection.execute(
            "SELECT * FROM sync_status WHERE id = 'singleton'"
        ).fetchone()
        span.set_attribute("hit", row is not None)
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
    with logfire.span("db.sync_status.delete"):
        connection.execute("DELETE FROM sync_status WHERE id = 'singleton'")
        connection.commit()


def update_sync_heartbeat(
    connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Update the heartbeat timestamp to signal liveness.

    Args:
        connection: Open database connection.
    """
    with logfire.span("db.sync_status.heartbeat"):
        connection.execute(
            """\
            UPDATE sync_status
            SET heartbeat_at = CURRENT_TIMESTAMP
            WHERE id = 'singleton'
            """
        )
        connection.commit()
