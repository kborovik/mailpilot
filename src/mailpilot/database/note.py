"""Note CRUD."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.sql import SQL, Composed
from psycopg.types.json import Json

from mailpilot.database._common import (
    _new_id,
)
from mailpilot.models import (
    Note,
    NoteSummary,
)

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
    until: str | None = None,
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
    if until is not None:
        where_parts.append(SQL("created_at <= %(until)s"))
        params["until"] = until
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
    *,
    commit: bool = True,
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
    if commit:
        connection.commit()
    return note


def delete_note(
    connection: psycopg.Connection[dict[str, Any]],
    note_id: str,
) -> bool:
    """Delete a single note by id; return whether a row was deleted.

    Single-id hard-delete path per §V.14 -- removes one `note` row only, never
    its owner's other notes. The `note_added` activity trail stays append-only
    and intact (§V.91). Operator-only, never an agent tool.

    Args:
        connection: Open database connection.
        note_id: Note ID to delete.

    Returns:
        True if a note row was deleted, False if no note had that id.
    """
    cursor = connection.execute("DELETE FROM note WHERE id = %(id)s", {"id": note_id})
    connection.commit()
    return cursor.rowcount > 0


def delete_notes(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str | None = None,
    company_id: str | None = None,
) -> list[str]:
    """Delete every note owned by a contact or company; return deleted ids.

    Owner-bulk hard-delete path per §V.14 -- one transaction, note rows only.
    The `note_added` activity trail stays append-only and intact. Zero notes
    is a no-op returning ``[]``. Operator-only, never an agent tool.

    Args:
        connection: Open database connection.
        contact_id: Delete all notes on this contact (XOR with company_id).
        company_id: Delete all notes on this company (XOR with contact_id).

    Returns:
        Deleted note ids (order undefined / DB-dependent), empty when none.

    Raises:
        ValueError: If neither or both of contact_id/company_id are set.
    """
    if (contact_id is None) == (company_id is None):
        raise ValueError("exactly one of contact_id or company_id is required")
    if contact_id is not None:
        rows = connection.execute(
            "DELETE FROM note WHERE contact_id = %(id)s RETURNING id",
            {"id": contact_id},
        ).fetchall()
    else:
        rows = connection.execute(
            "DELETE FROM note WHERE company_id = %(id)s RETURNING id",
            {"id": company_id},
        ).fetchall()
    connection.commit()
    return [str(row["id"]) for row in rows]
