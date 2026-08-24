"""Tag vocabulary and owner assignments."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, Literal

import psycopg
from psycopg.sql import SQL, Composed, Identifier
from psycopg.types.json import Json

from mailpilot.database._common import (
    _new_id,
)
from mailpilot.models import (
    Tag,
    TagAssignment,
    TagSummary,
)

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
) -> Tag | None:
    """Define a tag in the controlled vocabulary (§V.116).

    The name is normalized via ``_normalize_tag_name`` then inserted into the
    ``tag`` vocabulary table. ``name`` is globally unique (§V.90), so a
    second create of the same name uses ON CONFLICT DO NOTHING and returns
    ``None`` (the caller surfaces ``already_exists``).

    Raises:
        ValueError: If the tag name fails normalization.
    """
    normalized = _normalize_tag_name(name)
    row = connection.execute(
        """\
        INSERT INTO tag (id, name)
        VALUES (%(id)s, %(name)s)
        ON CONFLICT (name) DO NOTHING
        RETURNING *
        """,
        {"id": _new_id(), "name": normalized},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Tag.model_validate(row)


def get_tag(
    connection: psycopg.Connection[dict[str, Any]],
    tag_id: str,
) -> Tag | None:
    """Resolve a vocabulary tag by id (§V.107 polymorphic UUID branch).

    Operators address tags by name; this id branch keeps a tag id round-trip
    from one command's output into the next. Returns ``None`` when absent.
    """
    row = connection.execute("SELECT * FROM tag WHERE id = %s", (tag_id,)).fetchone()
    if row is None:
        return None
    return Tag.model_validate(row)


def get_tag_by_name(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
) -> Tag | None:
    """Resolve a vocabulary tag by its globally unique name (§V.90/§V.107).

    The name is normalized first. Returns ``None`` when no tag is defined --
    the caller surfaces ``not_found``. Used to resolve ``--tag <name>`` to a
    tag id without the operator pasting ids.

    Raises:
        ValueError: If the tag name fails normalization.
    """
    normalized = _normalize_tag_name(name)
    row = connection.execute(
        "SELECT * FROM tag WHERE name = %s", (normalized,)
    ).fetchone()
    if row is None:
        return None
    return Tag.model_validate(row)


def get_tag_summary_by_name(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
) -> TagSummary | None:
    """Resolve a vocabulary tag by name with its ``usage_count`` (§V.116).

    Backs ``tag view``: the vocabulary row plus the number of owners carrying
    it (``tag_assignment`` count). Returns ``None`` when no tag is defined.

    Raises:
        ValueError: If the tag name fails normalization.
    """
    normalized = _normalize_tag_name(name)
    row = connection.execute(
        """\
        SELECT t.id, t.name, t.disabled_reason, t.created_at,
               COUNT(a.id) AS usage_count
        FROM tag t
        LEFT JOIN tag_assignment a ON a.tag_id = t.id
        WHERE t.name = %s
        GROUP BY t.id
        """,
        (normalized,),
    ).fetchone()
    if row is None:
        return None
    return TagSummary.model_validate(row)


_TAG_SUMMARY_COLUMNS = SQL(
    "t.id, t.name, t.disabled_reason, t.created_at, "
    "(SELECT COUNT(*) FROM tag_assignment a WHERE a.tag_id = t.id) "
    "AS usage_count"
)


def list_tags(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str | None = None,
    company_id: str | None = None,
    limit: int = 100,
    since: str | None = None,
    until: str | None = None,
    include_disabled: bool = False,
) -> list[TagSummary]:
    """List vocabulary tags with a projected ``usage_count`` (§V.116).

    Owner-free (neither ``contact_id`` nor ``company_id``): returns the whole
    vocabulary. With an owner: returns only the tags assigned to that contact
    or company. ``usage_count`` is always the tag's global assignment count, so
    the owner-scoped view still reports how widely each tag is used.

    Disabled vocabulary rows (``disabled_reason IS NOT NULL``) are excluded by
    default; pass ``include_disabled=True`` to include them (§V.10). ``since`` /
    ``until`` bound the vocabulary row's ``created_at``.

    Raises:
        ValueError: If both ``contact_id`` and ``company_id`` are set.
    """
    if contact_id is not None and company_id is not None:
        raise ValueError("at most one of contact_id or company_id may be set")
    params: dict[str, object] = {"limit": limit}
    conditions: list[Composed | SQL] = []
    if since is not None:
        conditions.append(SQL("t.created_at >= %(since)s"))
        params["since"] = since
    if until is not None:
        conditions.append(SQL("t.created_at <= %(until)s"))
        params["until"] = until
    if not include_disabled:
        conditions.append(SQL("t.disabled_reason IS NULL"))

    owner_join = SQL("")
    if contact_id is not None:
        owner_join = SQL(
            "JOIN tag_assignment owner ON owner.tag_id = t.id "
            "AND owner.contact_id = %(contact_id)s"
        )
        params["contact_id"] = contact_id
    elif company_id is not None:
        owner_join = SQL(
            "JOIN tag_assignment owner ON owner.tag_id = t.id "
            "AND owner.company_id = %(company_id)s"
        )
        params["company_id"] = company_id

    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT {cols} FROM tag t {owner_join} {where} ORDER BY t.name LIMIT %(limit)s"
    ).format(cols=_TAG_SUMMARY_COLUMNS, owner_join=owner_join, where=where)
    rows = connection.execute(query, params).fetchall()
    return [TagSummary.model_validate(row) for row in rows]


def search_tags(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    limit: int = 100,
    include_disabled: bool = False,
) -> list[TagSummary]:
    """Search the vocabulary by name substring, with ``usage_count`` (§V.116).

    Substring/fuzzy matching is the ``search`` verb's job (§V.115); ``tag
    list`` does exact/owner filtering. Disabled rows are excluded by default.

    Args:
        connection: Open database connection.
        name: Substring to LIKE-match against the vocabulary ``name``.
        limit: Maximum number of results.
        include_disabled: When ``True`` include disabled rows.
    """
    pattern = f"%{name.strip().lower()}%"
    params: dict[str, object] = {"pattern": pattern, "limit": limit}
    disabled_filter = (
        SQL("") if include_disabled else SQL("AND t.disabled_reason IS NULL")
    )
    query = SQL(
        "SELECT {cols} "
        "FROM tag t WHERE t.name LIKE %(pattern)s {disabled_filter} "
        "ORDER BY t.name LIMIT %(limit)s"
    ).format(cols=_TAG_SUMMARY_COLUMNS, disabled_filter=disabled_filter)
    rows = connection.execute(query, params).fetchall()
    return [TagSummary.model_validate(row) for row in rows]


def disable_tag(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    reason: str,
) -> Tag | None:
    """Soft-retire a vocabulary tag (§V.10/§V.116).

    Flips ``disabled_reason`` on the matching active vocabulary row
    (``disabled_reason IS NULL`` gate blocks double-disable). A retired tag
    stays linked to its owners but drops out of the default ``tag list``.
    Returns ``None`` when no active tag with the name exists (undefined or
    already disabled) -- the caller distinguishes the two. This is a vocabulary
    lifecycle, not a per-owner event, so it writes no activity (an activity
    needs a contact or company owner, §V.17).

    Raises:
        ValueError: If the tag name fails normalization.
    """
    normalized = _normalize_tag_name(name)
    row = connection.execute(
        """\
        UPDATE tag
        SET disabled_reason = %(reason)s
        WHERE name = %(name)s AND disabled_reason IS NULL
        RETURNING *
        """,
        {"name": normalized, "reason": reason},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Tag.model_validate(row)


def enable_tag(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
) -> Tag | None:
    """Re-enable a retired vocabulary tag by clearing ``disabled_reason``.

    Mirror of ``disable_tag``. A ``disabled_reason IS NOT NULL`` gate blocks
    enabling an already-active tag, so the call returns ``None`` when no
    disabled tag with the name exists (undefined or already active) -- the
    caller distinguishes the two. Being owner-free, the vocabulary lifecycle
    writes no activity (an activity needs a contact or company owner).

    Raises:
        ValueError: If the tag name fails normalization.
    """
    normalized = _normalize_tag_name(name)
    row = connection.execute(
        """\
        UPDATE tag
        SET disabled_reason = NULL
        WHERE name = %(name)s AND disabled_reason IS NOT NULL
        RETURNING *
        """,
        {"name": normalized},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Tag.model_validate(row)


def _tag_owner_activity_ids(
    connection: psycopg.Connection[dict[str, Any]],
    owner_col: Literal["contact_id", "company_id"],
    owner_id: str,
) -> tuple[str | None, str | None]:
    """Return ``(activity_contact_id, activity_company_id)`` for a tag owner.

    Contact owners carry the parent company so the activity is multi-target
    (§V.17). Company owners write company-only activity.
    """
    if owner_col == "contact_id":
        owner_row = connection.execute(
            "SELECT company_id FROM contact WHERE id = %s", (owner_id,)
        ).fetchone()
        if owner_row is None:
            raise ValueError(f"contact not found: {owner_id}")
        return owner_id, owner_row["company_id"]
    if (
        connection.execute(
            "SELECT 1 FROM company WHERE id = %s", (owner_id,)
        ).fetchone()
        is None
    ):
        raise ValueError(f"company not found: {owner_id}")
    return None, owner_id


def _assign_tag(
    connection: psycopg.Connection[dict[str, Any]],
    owner_col: Literal["contact_id", "company_id"],
    tag_id: str,
    owner_id: str,
    *,
    commit: bool = True,
) -> TagAssignment | None:
    """Link a vocabulary tag to a contact or company and emit ``tag_added``.

    INSERT and ``tag_added`` commit together when ``commit`` is true.
    Returns ``None`` if the link already exists (ON CONFLICT DO NOTHING) --
    no activity in that case.
    """
    activity_contact_id, activity_company_id = _tag_owner_activity_ids(
        connection, owner_col, owner_id
    )
    insert_contact_id = owner_id if owner_col == "contact_id" else None
    insert_company_id = owner_id if owner_col == "company_id" else None
    tag_row = connection.execute(
        "SELECT name FROM tag WHERE id = %s", (tag_id,)
    ).fetchone()
    if tag_row is None:
        raise ValueError(f"tag not found: {tag_id}")
    assignment_row = connection.execute(
        """\
        INSERT INTO tag_assignment (id, tag_id, contact_id, company_id)
        VALUES (%(id)s, %(tag_id)s, %(contact_id)s, %(company_id)s)
        ON CONFLICT DO NOTHING
        RETURNING *
        """,
        {
            "id": _new_id(),
            "tag_id": tag_id,
            "contact_id": insert_contact_id,
            "company_id": insert_company_id,
        },
    ).fetchone()
    if assignment_row is None:
        if commit:
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
            "contact_id": activity_contact_id,
            "company_id": activity_company_id,
            "summary": f"Tagged as {tag_row['name']}",
            "detail": Json({"tag": tag_row["name"]}),
        },
    )
    if commit:
        connection.commit()
    return TagAssignment.model_validate(assignment_row)


def _remove_tag(
    connection: psycopg.Connection[dict[str, Any]],
    owner_col: Literal["contact_id", "company_id"],
    tag_id: str,
    owner_id: str,
    *,
    commit: bool = True,
) -> TagAssignment | None:
    """Unlink a vocabulary tag from a contact or company and emit ``tag_removed``.

    Deletes the link and appends ``tag_removed`` in one transaction when
    ``commit`` is true, retiring neither the tag nor the owner. Returns
    ``None`` when no such link exists.
    """
    activity_contact_id, activity_company_id = _tag_owner_activity_ids(
        connection, owner_col, owner_id
    )
    deleted_row = connection.execute(
        SQL(
            "DELETE FROM tag_assignment "
            "WHERE tag_id = %(tag_id)s AND {} = %(owner_id)s "
            "RETURNING *"
        ).format(Identifier(owner_col)),
        {"tag_id": tag_id, "owner_id": owner_id},
    ).fetchone()
    if deleted_row is None:
        if commit:
            connection.commit()
        return None
    tag_row = connection.execute(
        "SELECT name FROM tag WHERE id = %s", (tag_id,)
    ).fetchone()
    tag_name = tag_row["name"] if tag_row is not None else tag_id
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
            "contact_id": activity_contact_id,
            "company_id": activity_company_id,
            "summary": f"Untagged {tag_name}",
            "detail": Json({"tag": tag_name}),
        },
    )
    if commit:
        connection.commit()
    return TagAssignment.model_validate(deleted_row)


def assign_tag_to_contact(
    connection: psycopg.Connection[dict[str, Any]],
    tag_id: str,
    contact_id: str,
    *,
    commit: bool = True,
) -> TagAssignment | None:
    """Link a vocabulary tag to a contact and emit ``tag_added`` (§V.91/§V.116).

    The assignment INSERT and the ``tag_added`` activity commit in one
    transaction (§V.91) unless ``commit=False``. Returns ``None`` if the link
    already exists (ON CONFLICT DO NOTHING) -- no activity is written in that
    case. The activity carries the contact's company so it surfaces on the
    company timeline too (§V.17 multi-target).

    Raises:
        ValueError: If the contact does not exist.
    """
    return _assign_tag(connection, "contact_id", tag_id, contact_id, commit=commit)


def assign_tag_to_company(
    connection: psycopg.Connection[dict[str, Any]],
    tag_id: str,
    company_id: str,
    *,
    commit: bool = True,
) -> TagAssignment | None:
    """Link a vocabulary tag to a company and emit ``tag_added`` (§V.91/§V.116).

    Mirrors ``assign_tag_to_contact``. Returns ``None`` if the link already
    exists.

    Raises:
        ValueError: If the company does not exist.
    """
    return _assign_tag(connection, "company_id", tag_id, company_id, commit=commit)


def remove_tag_from_contact(
    connection: psycopg.Connection[dict[str, Any]],
    tag_id: str,
    contact_id: str,
    *,
    commit: bool = True,
) -> TagAssignment | None:
    """Unlink a vocabulary tag from a contact and emit ``tag_removed`` (§V.116).

    Inverse of ``assign_tag_to_contact``: deletes the link and appends a
    ``tag_removed`` activity in one transaction (§V.91), retiring neither the
    tag vocabulary nor the contact. Returns ``None`` when no such link exists
    (the caller surfaces ``not_found``).

    Raises:
        ValueError: If the contact does not exist.
    """
    return _remove_tag(connection, "contact_id", tag_id, contact_id, commit=commit)


def remove_tag_from_company(
    connection: psycopg.Connection[dict[str, Any]],
    tag_id: str,
    company_id: str,
    *,
    commit: bool = True,
) -> TagAssignment | None:
    """Unlink a vocabulary tag from a company and emit ``tag_removed`` (§V.116).

    Mirrors ``remove_tag_from_contact``. Returns ``None`` when no such link
    exists.

    Raises:
        ValueError: If the company does not exist.
    """
    return _remove_tag(connection, "company_id", tag_id, company_id, commit=commit)


def _tag_names_by_id(
    connection: psycopg.Connection[dict[str, Any]],
    tag_ids: Sequence[str],
) -> dict[str, str]:
    """Map tag ids to vocabulary names; missing ids are omitted."""
    if not tag_ids:
        return {}
    rows = connection.execute(
        "SELECT id, name FROM tag WHERE id = ANY(%s)",
        (list(tag_ids),),
    ).fetchall()
    return {str(row["id"]): str(row["name"]) for row in rows}


def _set_owner_tags(
    connection: psycopg.Connection[dict[str, Any]],
    owner_col: Literal["contact_id", "company_id"],
    owner_id: str,
    tag_ids: Sequence[str],
) -> list[str]:
    """Replace an owner's full tag set."""
    # Existence via the same lookup the writers use, so a missing owner
    # raises before any link mutation (empty ``tag_ids`` still validates).
    _tag_owner_activity_ids(connection, owner_col, owner_id)
    desired_ids = list(dict.fromkeys(tag_ids))
    name_by_id = _tag_names_by_id(connection, desired_ids)
    missing = [tid for tid in desired_ids if tid not in name_by_id]
    if missing:
        raise ValueError(f"tag not found: {missing[0]}")
    current_rows = connection.execute(
        SQL("SELECT tag_id FROM tag_assignment WHERE {} = %s").format(
            Identifier(owner_col)
        ),
        (owner_id,),
    ).fetchall()
    current_ids = {str(row["tag_id"]) for row in current_rows}
    desired_set = set(desired_ids)
    to_add = [tid for tid in desired_ids if tid not in current_ids]
    to_remove = sorted(current_ids - desired_set)
    for tag_id in to_remove:
        _remove_tag(connection, owner_col, tag_id, owner_id, commit=False)
    for tag_id in to_add:
        _assign_tag(connection, owner_col, tag_id, owner_id, commit=False)
    connection.commit()
    if owner_col == "contact_id":
        assigned = list_tags(
            connection,
            contact_id=owner_id,
            limit=1_000_000,
            include_disabled=True,
        )
    else:
        assigned = list_tags(
            connection,
            company_id=owner_id,
            limit=1_000_000,
            include_disabled=True,
        )
    return [t.name for t in assigned]


def set_company_tags(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
    tag_ids: Sequence[str],
) -> list[str]:
    """Replace a company's full tag assignment set in one transaction (§V.141).

    Adds missing links, removes extras, and writes ``tag_added`` /
    ``tag_removed`` activity per change (§V.14). Empty ``tag_ids`` clears every
    assignment. Caller must pre-resolve vocabulary names so undefined tags never
    reach this function (zero partial writes on undefined). Returns the final
    assigned tag names sorted.

    Raises:
        ValueError: If the company does not exist, or a ``tag_id`` is unknown.
    """
    return _set_owner_tags(connection, "company_id", company_id, tag_ids)


def set_contact_tags(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
    tag_ids: Sequence[str],
) -> list[str]:
    """Replace a contact's full tag assignment set in one transaction (§V.141).

    Mirrors ``set_company_tags``: add missing, remove extras, activity per
    change (§V.14), empty ``tag_ids`` clears. Returns final tag names sorted.

    Raises:
        ValueError: If the contact does not exist, or a ``tag_id`` is unknown.
    """
    return _set_owner_tags(connection, "contact_id", contact_id, tag_ids)
