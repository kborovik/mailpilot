"""Contact CRUD and list/search."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Literal

import psycopg
from psycopg.sql import SQL, Composable, Composed, Placeholder
from psycopg.types.json import Json

from mailpilot.database._common import (
    _build_update,
    _new_id,
)
from mailpilot.database.company import (
    _CONTACT_TAGS_SQL,
    _normalize_tag_ids,
    _tag_assignment_conditions,
)
from mailpilot.models import (
    Contact,
    ContactSummary,
)

# -- Contact -------------------------------------------------------------------


def create_contact(
    connection: psycopg.Connection[dict[str, Any]],
    email: str,
    company_id: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    title: str | None = None,
    email_confidence: int | None = None,
    verification_meta: dict[str, Any] | None = None,
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
        verification_meta: Optional operator-only verification audit object
            (§V.144). Never injected into agent prompts.

    Returns:
        Created contact, or ``None`` if a contact with this email already
        existed.
    """
    # Canonicalize the natural key lowercase before insert (§V.90). The
    # case-sensitive ``email`` UNIQUE would otherwise let a recased local-part
    # (Outlook/Exchange) mint a duplicate row past ON CONFLICT (§B.121).
    email = email.lower()
    row = connection.execute(
        """\
        INSERT INTO contact (id, email, company_id, first_name, last_name,
                             title, email_confidence, verification_meta)
        VALUES (%(id)s, %(email)s, %(company_id)s,
                %(first_name)s, %(last_name)s,
                %(title)s, %(email_confidence)s, %(verification_meta)s)
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
            "verification_meta": (
                Json(verification_meta) if verification_meta is not None else None
            ),
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
    # Match against the lowercase natural key (§V.90) so a mixed-case lookup
    # resolves the canonical row.
    email = email.lower()
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
    # Canonicalize the natural key lowercase (§V.90) so a recased sender resolves
    # the enrolled row rather than minting a bare duplicate (§B.121). The
    # delegated ``get_contact_by_email`` / ``create_contact`` also lowercase; the
    # explicit normalization keeps the error message and re-fetch key canonical.
    email = email.lower()
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
        Mapping from lowercase email to Contact for every input email that has
        an existing row. Keys are the canonical lowercase natural key (§V.90),
        so a mixed-case input resolves under its lowercase form. Missing emails
        are simply absent from the dict.
    """
    # Canonicalize and dedupe on the lowercase natural key (§V.90) so case
    # variants collapse to one lookup value and resolve the canonical row.
    unique = list({email.lower() for email in emails})
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
        Mapping from lowercase email to Contact for every input email. Keys are
        the canonical lowercase natural key (§V.90); case-variant inputs collapse
        to a single row.
    """
    # Canonicalize and dedupe on the lowercase natural key (§V.90) before insert
    # so case variants share one row and ON CONFLICT never mints a duplicate
    # (§B.121).
    unique = list({email.lower() for email in emails})
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


def _enrollment_coverage_conditions(
    enrollment: Literal["unenrolled", "enrolled"] | None,
) -> list[SQL]:
    """Enrollment-row EXISTS / NOT EXISTS predicates (empty when unset)."""
    if enrollment == "unenrolled":
        return [
            SQL("NOT EXISTS (SELECT 1 FROM enrollment e WHERE e.contact_id = c.id)")
        ]
    if enrollment == "enrolled":
        return [SQL("EXISTS (SELECT 1 FROM enrollment e WHERE e.contact_id = c.id)")]
    return []


def list_contacts(
    connection: psycopg.Connection[dict[str, Any]],
    limit: int | None = 100,
    company_id: str | None = None,
    company_ids: Sequence[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    include_disabled: bool = False,
    max_email_confidence: int | None = None,
    min_email_confidence: int | None = None,
    title: str | None = None,
    tag: str | Sequence[str] | None = None,
    exclude_tags: Sequence[str] | None = None,
    enrollment: Literal["unenrolled", "enrolled"] | None = None,
) -> list[ContactSummary]:
    """List contacts as summaries with optional filters.

    Joins ``company`` once (LEFT JOIN) so each summary carries
    ``company_domain`` without an N+1 lookup per §V.5; ``company_domain``
    is NULL when ``company_id`` is NULL. Every row projects ``tags``
    (assigned names, empty ok), mirroring ``search_contacts`` / view.

    Args:
        connection: Open database connection.
        limit: Maximum results. ``None`` omits LIMIT (preview expand).
        company_id: Filter by company ID.
        company_ids: Filter by a batch of company IDs (``company_id = ANY``).
            Takes precedence over ``company_id`` when both are set.
        since: ISO datetime inclusive lower bound on ``created_at``.
        until: ISO datetime inclusive upper bound on ``created_at``.
        include_disabled: When False (default), only contacts with
            ``disabled_reason IS NULL`` are returned.
        max_email_confidence: When set, surfaces rows with
            ``email_confidence <= N OR email_confidence IS NULL`` for
            cross-run operator review of low-score (high-risk) leads
            (§V.95). NULL = Bouncer-unknown = unverified = high risk, so
            it is surfaced for review, never skipped; admit-all (§V.96)
            must not drop unknowns (§B.76).
        min_email_confidence: When set, surfaces only rows with
            ``email_confidence >= N``; composes with
            ``max_email_confidence`` into a closed range. NULL-score rows
            are excluded by the lower bound (§V.95).
        title: When set, a case-insensitive exact match on ``contact.title``.
            Substring/fuzzy title matching is the ``contact search`` verb's
            job, never the ``list`` filter (§V.115 family 5).
        tag: When set (one resolved tag id or a sequence of ids), returns
            only contacts carrying every named tag -- AND-compose over
            ``tag_assignment`` (§V.116). Composes with ``exclude_tags`` as
            an intersection.
        exclude_tags: When set (resolved tag ids), returns only contacts
            carrying NONE of the given tags -- one ``NOT EXISTS`` predicate per
            tag, all intersected (§V.116).
        enrollment: When ``unenrolled``, only contacts with zero enrollment
            rows (any workflow, any status). When ``enrolled``, only contacts
            with at least one enrollment row any status. Disabled enrollments
            still count as enrolled. ``None`` applies no enrollment-row
            filter. Composes with ``tag`` / ``exclude_tags`` /
            ``include_disabled``.

    Returns:
        List of contact summaries ordered by email.
    """
    conditions: list[Composed | SQL] = []
    params: dict[str, object] = {}
    if limit is not None:
        params["limit"] = limit
    if company_ids:
        conditions.append(SQL("c.company_id = ANY(%(company_ids)s)"))
        params["company_ids"] = list(company_ids)
    elif company_id is not None:
        conditions.append(SQL("c.company_id = %(company_id)s"))
        params["company_id"] = company_id
    if since is not None:
        conditions.append(SQL("c.created_at >= %(since)s"))
        params["since"] = since
    if until is not None:
        conditions.append(SQL("c.created_at <= %(until)s"))
        params["until"] = until
    if not include_disabled:
        conditions.append(SQL("c.disabled_reason IS NULL"))
    if max_email_confidence is not None:
        conditions.append(
            SQL(
                "(c.email_confidence <= %(max_email_confidence)s "
                "OR c.email_confidence IS NULL)"
            )
        )
        params["max_email_confidence"] = max_email_confidence
    if min_email_confidence is not None:
        conditions.append(SQL("c.email_confidence >= %(min_email_confidence)s"))
        params["min_email_confidence"] = min_email_confidence
    if title is not None:
        conditions.append(SQL("LOWER(c.title) = LOWER(%(title)s)"))
        params["title"] = title
    conditions.extend(
        _tag_assignment_conditions(_normalize_tag_ids(tag), "contact_id", params)
    )
    conditions.extend(
        _tag_assignment_conditions(exclude_tags, "contact_id", params, negate=True)
    )
    conditions.extend(_enrollment_coverage_conditions(enrollment))
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    limit_sql = SQL(" LIMIT %(limit)s") if limit is not None else SQL("")
    query = SQL(
        "SELECT c.id, c.email, c.first_name, c.last_name, c.title, "
        "c.company_id, co.domain AS company_domain, "
        "c.email_confidence, c.disabled_reason, c.created_at, "
        "{tags} "
        "FROM contact c LEFT JOIN company co ON c.company_id = co.id "
        "{where} ORDER BY c.email{limit}"
    ).format(tags=SQL(_CONTACT_TAGS_SQL), where=where, limit=limit_sql)
    rows = connection.execute(query, params).fetchall()
    return [ContactSummary.model_validate(row) for row in rows]


def search_contacts(
    connection: psycopg.Connection[dict[str, Any]],
    query: str,
    limit: int = 100,
) -> list[ContactSummary]:
    """Search contacts by email, name, or title (§V.158).

    Args:
        connection: Open database connection.
        query: Search term (single-token or multi-token).
        limit: Maximum number of results.

    Returns:
        Matching contact summaries ordered by email. Each carries
        ``title`` + ``company_domain`` (LEFT JOIN company per §V.5) and
        ``tags`` (assigned names, empty ok), mirroring ``list_contacts``.

    Match rules (§V.158):
        - Single-token: per-field LIKE on email / first_name / last_name /
          title (status quo).
        - Full-name: order-preserving match on
          ``TRIM(first_name || ' ' || last_name)`` for the whole query string.
        - Multi-token (whitespace-split): every token AND-matches at least one
          of the same fields (no flood from partial noise).
        - Disabled contacts remain searchable (forensics).
    """
    tokens = [t for t in query.strip().split() if t]
    if not tokens:
        return []

    # Whole-query full-name pattern (covers "David Drouin" as one string).
    params: dict[str, object] = {
        "full_pattern": f"%{query.strip()}%",
        "limit": limit,
    }
    token_clauses: list[Composable] = []
    for i, token in enumerate(tokens):
        key = f"tok_{i}"
        params[key] = f"%{token}%"
        ph = Placeholder(key)
        token_clauses.append(
            SQL(
                "(LOWER(c.email) LIKE LOWER({})"
                " OR LOWER(COALESCE(c.first_name, '')) LIKE LOWER({})"
                " OR LOWER(COALESCE(c.last_name, '')) LIKE LOWER({})"
                " OR LOWER(COALESCE(c.title, '')) LIKE LOWER({}))"
            ).format(ph, ph, ph, ph)
        )
    multi_and = SQL(" AND ").join(token_clauses)
    full_name = SQL(
        "LOWER(TRIM(COALESCE(c.first_name, '') || ' ' || COALESCE(c.last_name, ''))) "
        "LIKE LOWER(%(full_pattern)s)"
    )
    where = SQL("({}) OR ({})").format(full_name, multi_and)
    query_sql = SQL(
        """\
        SELECT c.id, c.email, c.first_name, c.last_name, c.title,
               c.company_id, co.domain AS company_domain,
               c.email_confidence, c.disabled_reason, c.created_at,
               {tags}
        FROM contact c
        LEFT JOIN company co ON c.company_id = co.id
        WHERE {where}
        ORDER BY c.email
        LIMIT %(limit)s
        """
    ).format(tags=SQL(_CONTACT_TAGS_SQL), where=where)
    rows = connection.execute(query_sql, params).fetchall()
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
    if "verification_meta" in updates and updates["verification_meta"] is not None:
        updates["verification_meta"] = Json(updates["verification_meta"])
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


def enable_contact(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
) -> Contact | None:
    """Clear a contact's global block by clearing ``disabled_reason``.

    Clears any reason regardless of prefix -- the operator owns consent, so a
    ``"bounced:"`` or ``"unsubscribed:"`` block re-enables the same way (no
    unsubscribe carve-out). This is operator-only; the agent disables a contact
    on bounce or unsubscribe but never re-enables it. A ``disabled_reason IS
    NOT NULL`` gate blocks enabling an already-active contact (returns
    ``None``).

    Args:
        connection: Open database connection.
        contact_id: Contact ID.

    Returns:
        Updated contact, or ``None`` when no disabled contact with that id
        exists -- i.e. missing or already active.
    """
    row = connection.execute(
        """\
        UPDATE contact
        SET disabled_reason = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %(id)s
          AND disabled_reason IS NOT NULL
        RETURNING *
        """,
        {"id": contact_id},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Contact.model_validate(row)
