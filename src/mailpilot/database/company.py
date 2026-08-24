"""Company CRUD, aliases, merge, and list/export."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg.sql import SQL, Composed, Identifier, Placeholder
from psycopg.types.json import Json

from mailpilot.database._common import (
    _build_update,
    _new_id,
)
from mailpilot.models import (
    Company,
    CompanyProfile,
    CompanySummary,
)

# -- Company -------------------------------------------------------------------


def _normalize_company_domain(domain: str) -> str:
    """Lowercase + strip a company domain natural key (§V.90 / §V.142)."""
    return domain.strip().lower()


def _merged_into_reason(into_domain: str) -> str:
    """Structured soft-disable reason written by ``merge_companies`` (§V.143)."""
    return f"merged:into {_normalize_company_domain(into_domain)}"


def _tombstone_merged_domain(company_id: str) -> str:
    """Unique domain left on an absorbed company after merge (§V.142 space)."""
    return f"__merged__.{company_id}"


def domain_in_use(
    connection: psycopg.Connection[dict[str, Any]],
    domain: str,
) -> bool:
    """True when *domain* is a canonical company.domain or an alias (§V.142)."""
    normalized = _normalize_company_domain(domain)
    if not normalized:
        return False
    row = connection.execute(
        """\
        SELECT EXISTS (
            SELECT 1 FROM company WHERE domain = %(domain)s
            UNION ALL
            SELECT 1 FROM company_alias WHERE domain = %(domain)s
        ) AS taken
        """,
        {"domain": normalized},
    ).fetchone()
    return bool(row and row["taken"])


def list_company_aliases(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
) -> list[str]:
    """Return sorted lowercased alias domains for a company (§V.142)."""
    rows = connection.execute(
        """\
        SELECT domain FROM company_alias
        WHERE company_id = %(company_id)s
        ORDER BY domain
        """,
        {"company_id": company_id},
    ).fetchall()
    return [str(r["domain"]) for r in rows]


def add_company_alias(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
    domain: str,
    *,
    commit: bool = True,
) -> bool:
    """Register one alias domain for a company (§V.142).

    Returns ``True`` when a row was inserted, ``False`` when the alias already
    pointed at this company (idempotent skip). Raises ``ValueError`` when the
    domain collides with another company.domain or another owner's alias.
    """
    normalized = _normalize_company_domain(domain)
    if not normalized:
        raise ValueError("alias domain cannot be empty")
    company = get_company(connection, company_id)
    if company is None:
        raise ValueError(f"company not found: {company_id}")
    if normalized == company.domain:
        raise ValueError(f"alias {normalized!r} equals company domain")
    existing = connection.execute(
        "SELECT company_id FROM company_alias WHERE domain = %(domain)s",
        {"domain": normalized},
    ).fetchone()
    if existing is not None:
        if existing["company_id"] == company_id:
            return False
        raise ValueError(
            f"domain {normalized!r} is already an alias of another company"
        )
    if (
        connection.execute(
            "SELECT 1 FROM company WHERE domain = %(domain)s",
            {"domain": normalized},
        ).fetchone()
        is not None
    ):
        raise ValueError(f"domain {normalized!r} is already a company domain")
    connection.execute(
        """\
        INSERT INTO company_alias (domain, company_id)
        VALUES (%(domain)s, %(company_id)s)
        """,
        {"domain": normalized, "company_id": company_id},
    )
    if commit:
        connection.commit()
    return True


def create_company(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    domain: str,
    *,
    aliases: Sequence[str] | None = None,
    commit: bool = True,
) -> Company | None:
    """Create a new company, optionally with alias domains (§V.142).

    Uses ``ON CONFLICT (domain) DO NOTHING`` per §V.16(+) so callers can
    safely re-invoke without catching ``UniqueViolation``. Returns ``None``
    when the canonical domain already exists as a company row or is already
    an alias (shared domain space). Alias domains are lowercased and
    registered in the same transaction.

    Args:
        connection: Open database connection.
        name: Company name.
        domain: Primary domain.
        aliases: Optional alternate domains (repeatable CLI ``--alias``).
        commit: When False, leave the insert uncommitted for a caller txn
            (§V.167 oneshot).

    Returns:
        Created company, or ``None`` if the domain space was already taken.
    """
    normalized = _normalize_company_domain(domain)
    if not normalized:
        return None
    alias_list = sorted(
        {
            _normalize_company_domain(a)
            for a in (aliases or ())
            if _normalize_company_domain(a)
        }
    )
    if normalized in alias_list:
        return None
    if domain_in_use(connection, normalized):
        return None
    for alias in alias_list:
        if domain_in_use(connection, alias):
            return None
    company_id = _new_id()
    row = connection.execute(
        """\
        INSERT INTO company (id, name, domain)
        VALUES (%(id)s, %(name)s, %(domain)s)
        ON CONFLICT (domain) DO NOTHING
        RETURNING *
        """,
        {"id": company_id, "name": name, "domain": normalized},
    ).fetchone()
    if row is None:
        if commit:
            connection.commit()
        return None
    for alias in alias_list:
        connection.execute(
            """\
            INSERT INTO company_alias (domain, company_id)
            VALUES (%(domain)s, %(company_id)s)
            """,
            {"domain": alias, "company_id": company_id},
        )
    if commit:
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


def _normalize_tag_ids(tag: str | Sequence[str] | None) -> list[str]:
    """Coerce a single tag id or a sequence into a list (§V.116).

    A bare ``str`` is one id (enrollment preview still passes ``tag.id``).
    ``str`` is checked first so a string is not treated as a sequence of
    characters.
    """
    if tag is None:
        return []
    if isinstance(tag, str):
        return [tag]
    return [item for item in tag if item]


def _tag_assignment_conditions(
    tags: Sequence[str] | None,
    owner_column: str,
    params: dict[str, object],
    *,
    negate: bool = False,
) -> list[Composed]:
    """Build one ``EXISTS`` (or ``NOT EXISTS``) predicate per tag (§V.116/§V.178).

    ``--tag`` AND-composes: the row must carry every named tag. ``--no-tag``
    AND-composes the negation: the row must carry none. Each tag is its own
    intersected subquery over ``tag_assignment`` on ``owner_column``.
    ``negate=True`` is ``--no-tag`` (``NOT EXISTS``, ``exclude_tag_id_*``
    placeholders); default is ``--tag``.
    """
    conditions: list[Composed] = []
    if not tags:
        return conditions
    prefix = "exclude_tag_id" if negate else "include_tag_id"
    exists = SQL("NOT EXISTS") if negate else SQL("EXISTS")
    for index, tag_id in enumerate(tags):
        param_name = f"{prefix}_{index}"
        conditions.append(
            SQL(
                "{} (SELECT 1 FROM tag_assignment ta "
                "WHERE ta.{} = c.id AND ta.tag_id = {})"
            ).format(exists, Identifier(owner_column), Placeholder(param_name))
        )
        params[param_name] = tag_id
    return conditions


_COMPANY_TAGS_SQL = (
    "COALESCE("
    "(SELECT array_agg(t.name ORDER BY t.name) "
    "FROM tag_assignment ta JOIN tag t ON t.id = ta.tag_id "
    "WHERE ta.company_id = c.id), "
    "ARRAY[]::text[]) AS tags"
)
"""Correlated assigned-tag names for company list/search rows (§V.8 / §V.116)."""

_CONTACT_TAGS_SQL = (
    "COALESCE("
    "(SELECT array_agg(t.name ORDER BY t.name) "
    "FROM tag_assignment ta JOIN tag t ON t.id = ta.tag_id "
    "WHERE ta.contact_id = c.id), "
    "ARRAY[]::text[]) AS tags"
)
"""Correlated assigned-tag names for contact list/search rows (§V.8 / §V.116)."""

_COMPANY_SORT_SQL: dict[str, SQL] = {
    "name": SQL("LOWER(c.name)"),
    "domain": SQL("LOWER(c.domain)"),
    "created_at": SQL("c.created_at"),
    "contact_count": SQL("COUNT(ct.id)"),
}
"""Company list|search ORDER BY expressions keyed by ``--sort`` Choice."""


def _company_order_by(sort: str, desc: bool) -> Composed:
    """Build ``ORDER BY <sort> ASC|DESC, LOWER(c.name) ASC`` for stable pages."""
    col = _COMPANY_SORT_SQL.get(sort, _COMPANY_SORT_SQL["name"])
    direction = SQL("DESC") if desc else SQL("ASC")
    return SQL("ORDER BY {} {}, LOWER(c.name) ASC").format(col, direction)


def _company_pipeline_status_predicates(
    status: str | None,
    include_disabled: bool,
) -> tuple[list[SQL], list[SQL]]:
    """Build WHERE/HAVING predicates for the company pipeline cohort filter.

    Args:
        status: Pipeline cohort name (``ready`` / ``needs_contacts`` /
            ``needs_profile`` / ``disabled``) or ``None``.
        include_disabled: When ``status`` is unset, controls the default
            soft-disable hide (§V.114).

    Returns:
        ``(conditions, having)`` SQL fragments. Active cohort buckets force
        not-disabled; ``disabled`` selects only disabled rows and overrides
        the default hide (§V.138).
    """
    conditions: list[SQL] = []
    having: list[SQL] = []
    if status == "ready":
        conditions.append(SQL("c.profile IS NOT NULL"))
        conditions.append(SQL("c.disabled_reason IS NULL"))
        having.append(SQL("COUNT(ct.id) >= 1"))
    elif status == "needs_contacts":
        conditions.append(SQL("c.profile IS NOT NULL"))
        conditions.append(SQL("c.disabled_reason IS NULL"))
        having.append(SQL("COUNT(ct.id) = 0"))
    elif status == "needs_profile":
        conditions.append(SQL("c.profile IS NULL"))
        conditions.append(SQL("c.disabled_reason IS NULL"))
    elif status == "disabled":
        conditions.append(SQL("c.disabled_reason IS NOT NULL"))
    elif not include_disabled:
        conditions.append(SQL("c.disabled_reason IS NULL"))
    return conditions, having


def _company_scope_clauses(
    params: dict[str, object],
    *,
    has_profile: bool | None = None,
    max_contacts: int | None = None,
    min_contacts: int | None = None,
    include_disabled: bool = False,
    tag: str | Sequence[str] | None = None,
    exclude_tags: Sequence[str] | None = None,
    status: str | None = None,
) -> tuple[list[Composed | SQL], list[SQL]]:
    """Build has_profile, pipeline, contact-count, and tag predicates.

    Mutates ``params`` with tag and contact-count placeholders.
    """
    conditions: list[Composed | SQL] = []
    having: list[SQL] = []
    if has_profile is True:
        conditions.append(SQL("c.profile IS NOT NULL"))
    elif has_profile is False:
        conditions.append(SQL("c.profile IS NULL"))
    status_conditions, status_having = _company_pipeline_status_predicates(
        status, include_disabled
    )
    conditions.extend(status_conditions)
    having.extend(status_having)
    if max_contacts is not None:
        having.append(SQL("COUNT(ct.id) <= %(max_contacts)s"))
        params["max_contacts"] = max_contacts
    if min_contacts is not None:
        having.append(SQL("COUNT(ct.id) >= %(min_contacts)s"))
        params["min_contacts"] = min_contacts
    conditions.extend(
        _tag_assignment_conditions(_normalize_tag_ids(tag), "company_id", params)
    )
    conditions.extend(
        _tag_assignment_conditions(exclude_tags, "company_id", params, negate=True)
    )
    return conditions, having


def list_companies(
    connection: psycopg.Connection[dict[str, Any]],
    limit: int = 100,
    offset: int = 0,
    sort: str = "name",
    desc: bool = False,
    since: str | None = None,
    until: str | None = None,
    has_profile: bool | None = None,
    max_contacts: int | None = None,
    min_contacts: int | None = None,
    include_disabled: bool = False,
    tag: str | Sequence[str] | None = None,
    exclude_tags: Sequence[str] | None = None,
    full: bool = False,
    status: str | None = None,
) -> list[CompanySummary]:
    """List companies as summaries.

    Joins ``contact`` once (LEFT JOIN) so each summary carries
    ``contact_count`` (child cardinality, **including disabled** rows per
    §V.96) without an N+1 probe; the count tracks the discovery-memoization
    rule, so disabled contacts are counted, not the active-only set.

    Disabled companies (``disabled_reason IS NOT NULL``) are hidden by default
    (§V.114) -- a company memoized as having no discoverable contacts drops
    out of the listing and so out of the lead-contacts discover set (§V.96).
    Pass ``include_disabled=True`` to surface them.

    Every row projects ``tags`` (assigned names, empty ok) and
    ``disabled_reason`` (null when enabled). Pass ``full=True`` to embed
    lean ``profile.summary`` only — never products/target_customers/sources
    on the list path (§V.8).

    Args:
        connection: Open database connection.
        limit: Maximum results.
        offset: Rows to skip before the page (default 0).
        sort: Order key in {name, domain, created_at, contact_count}.
        desc: When ``True``, sort descending; default ascending.
        since: ISO datetime inclusive lower bound on ``created_at``.
        until: ISO datetime inclusive upper bound on ``created_at``.
        has_profile: ``True`` returns only rows where ``profile IS NOT NULL``;
            ``False`` returns only rows where ``profile IS NULL``; ``None``
            (default) returns all rows. Per §V.72 operator filter surface.
        max_contacts: When set, returns only companies whose ``contact_count``
            is ``<= N`` (inclusive upper bound). Mirrors
            ``--max-email-confidence`` (§V.95); ``--has-profile --max-contacts
            4`` expresses the lead-contacts discover set in one query (§V.96).
        min_contacts: When set, returns only companies whose ``contact_count``
            is ``>= N`` (inclusive lower bound); composes with ``max_contacts``
            into a closed range.
        include_disabled: When ``True``, includes disabled companies; the
            default (``False``) hides them (§V.114).
        tag: When set (one resolved tag id or a sequence of ids), returns
            only companies carrying every named tag -- AND-compose over
            ``tag_assignment`` (§V.116). Composes with ``exclude_tags`` as
            an intersection.
        exclude_tags: When set (resolved tag ids), returns only companies
            carrying NONE of the given tags -- one ``NOT EXISTS`` predicate per
            tag, all intersected (§V.116). The repeatable negated membership
            filter, for memoization (drop a memoized company from the discover
            set without ``company disable``); the lead-contacts discover set
            excludes both ``no-contacts-found`` and ``contacts-exhausted``
            (§V.96).
        full: When ``True``, embeds ``profile`` as ``{"summary": ...}`` (or
            null when the company has no profile). Default lean list leaves
            ``profile`` null (§V.8).
        status: Pipeline cohort filter (§V.138). One of ``ready`` (profile +
            contact_count >= 1 + not disabled), ``needs_contacts`` (profile +
            contact_count = 0 + not disabled), ``needs_profile`` (no profile +
            not disabled), ``disabled`` (disabled_reason set; overrides the
            default hide). AND-composes with the other filters. ``None``
            (default) applies no cohort predicate.

    Returns:
        List of company summaries ordered by ``sort`` (default name).
    """
    conditions: list[Composed | SQL] = []
    params: dict[str, object] = {"limit": limit, "offset": offset}
    if since is not None:
        conditions.append(SQL("c.created_at >= %(since)s"))
        params["since"] = since
    if until is not None:
        conditions.append(SQL("c.created_at <= %(until)s"))
        params["until"] = until
    scope_conditions, having = _company_scope_clauses(
        params,
        has_profile=has_profile,
        max_contacts=max_contacts,
        min_contacts=min_contacts,
        include_disabled=include_disabled,
        tag=tag,
        exclude_tags=exclude_tags,
        status=status,
    )
    conditions.extend(scope_conditions)
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    having_clause = SQL("HAVING ") + SQL(" AND ").join(having) if having else SQL("")
    profile_select = (
        SQL(
            ", CASE WHEN c.profile IS NULL THEN NULL "
            "ELSE jsonb_build_object('summary', c.profile->>'summary') "
            "END AS profile"
        )
        if full
        else SQL("")
    )
    order_by = _company_order_by(sort, desc)
    query = SQL(
        "SELECT c.id, c.name, c.domain, (c.profile IS NOT NULL) AS has_profile, "
        "c.disabled_reason, c.created_at, COUNT(ct.id) AS contact_count, "
        "{tags}{profile} "
        "FROM company c LEFT JOIN contact ct ON ct.company_id = c.id "
        "{where} GROUP BY c.id {having} {order} "
        "LIMIT %(limit)s OFFSET %(offset)s"
    ).format(
        tags=SQL(_COMPANY_TAGS_SQL),
        profile=profile_select,
        where=where,
        having=having_clause,
        order=order_by,
    )
    rows = connection.execute(query, params).fetchall()
    return [CompanySummary.model_validate(row) for row in rows]


def search_companies(
    connection: psycopg.Connection[dict[str, Any]],
    query: str,
    limit: int = 100,
    offset: int = 0,
    sort: str = "name",
    desc: bool = False,
) -> list[CompanySummary]:
    """Search companies by name or domain.

    Args:
        connection: Open database connection.
        query: Search term (matched against name and domain).
        limit: Maximum number of results.
        offset: Rows to skip before the page (default 0).
        sort: Order key in {name, domain, created_at, contact_count}.
        desc: When ``True``, sort descending; default ascending.

    Returns:
        Matching company summaries ordered by ``sort``. Each carries
        ``contact_count`` (LEFT JOIN contact COUNT, incl. disabled per §V.96)
        and ``tags`` (assigned names, empty ok), mirroring ``list_companies``.
    """
    pattern = f"%{query}%"
    order_by = _company_order_by(sort, desc)
    params: dict[str, object] = {
        "pattern": pattern,
        "limit": limit,
        "offset": offset,
    }
    # Search returns disabled when matched (§I); skip the default hide.
    conditions, having = _company_scope_clauses(params, include_disabled=True)
    conditions.append(
        SQL(
            "("
            "LOWER(c.name) LIKE LOWER(%(pattern)s) "
            "OR LOWER(c.domain) LIKE LOWER(%(pattern)s) "
            "OR EXISTS ("
            "  SELECT 1 FROM company_alias a "
            "  WHERE a.company_id = c.id "
            "    AND LOWER(a.domain) LIKE LOWER(%(pattern)s)"
            ")"
            ")"
        )
    )
    where = SQL("WHERE ") + SQL(" AND ").join(conditions)
    having_clause = SQL("HAVING ") + SQL(" AND ").join(having) if having else SQL("")
    sql = SQL(
        "SELECT c.id, c.name, c.domain, (c.profile IS NOT NULL) AS has_profile, "
        "c.disabled_reason, c.created_at, COUNT(ct.id) AS contact_count, "
        "{tags} "
        "FROM company c "
        "LEFT JOIN contact ct ON ct.company_id = c.id "
        "{where} "
        "GROUP BY c.id "
        "{having} "
        "{order} "
        "LIMIT %(limit)s OFFSET %(offset)s"
    ).format(
        tags=SQL(_COMPANY_TAGS_SQL),
        where=where,
        having=having_clause,
        order=order_by,
    )
    rows = connection.execute(sql, params).fetchall()
    return [CompanySummary.model_validate(row) for row in rows]


def export_companies(
    connection: psycopg.Connection[dict[str, Any]],
    has_profile: bool | None = None,
    max_contacts: int | None = None,
    min_contacts: int | None = None,
    include_disabled: bool = False,
    tag: str | Sequence[str] | None = None,
    exclude_tags: Sequence[str] | None = None,
    status: str | None = None,
    full: bool = False,
) -> list[dict[str, Any]]:
    """Export companies as tracker NDJSON-ready dicts (§V.145).

    Stable keys: ``domain``, ``name``, ``tags``, ``has_profile``,
    ``contact_count``, ``disabled_reason``. Domains are lowercased; tags are
    sorted; rows ordered by domain ASC. No result-limit (unlike ``list``).
    Filters match the company list family (§V.138/§V.116/§V.114/§V.96).
    Pass ``full=True`` to embed the full ``profile`` object (or null).

    Args:
        connection: Open database connection.
        has_profile: Presence filter; ``None`` means no filter.
        max_contacts: Inclusive upper bound on contact_count.
        min_contacts: Inclusive lower bound on contact_count.
        include_disabled: When ``True``, includes disabled companies.
        tag: One resolved tag id or a sequence; AND-compose membership.
        exclude_tags: Resolved tag ids excluded via NOT EXISTS.
        status: Pipeline cohort filter (§V.138).
        full: When ``True``, embed full profile JSON (or null).

    Returns:
        List of tracker-shaped dicts ordered by domain ASC.
    """
    params: dict[str, object] = {}
    conditions, having = _company_scope_clauses(
        params,
        has_profile=has_profile,
        max_contacts=max_contacts,
        min_contacts=min_contacts,
        include_disabled=include_disabled,
        tag=tag,
        exclude_tags=exclude_tags,
        status=status,
    )
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    having_clause = SQL("HAVING ") + SQL(" AND ").join(having) if having else SQL("")
    profile_select = SQL(", c.profile") if full else SQL("")
    query = SQL(
        "SELECT LOWER(c.domain) AS domain, c.name, "
        "(c.profile IS NOT NULL) AS has_profile, "
        "c.disabled_reason, COUNT(ct.id) AS contact_count, "
        "{tags}{profile} "
        "FROM company c LEFT JOIN contact ct ON ct.company_id = c.id "
        "{where} GROUP BY c.id {having} ORDER BY LOWER(c.domain)"
    ).format(
        tags=SQL(_COMPANY_TAGS_SQL),
        profile=profile_select,
        where=where,
        having=having_clause,
    )
    rows = connection.execute(query, params).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        entry: dict[str, Any] = {
            "domain": row["domain"],
            "name": row["name"],
            "tags": list(row["tags"] or []),
            "has_profile": bool(row["has_profile"]),
            "contact_count": int(row["contact_count"]),
            "disabled_reason": row["disabled_reason"],
        }
        if full:
            entry["profile"] = row["profile"]
        results.append(entry)
    return results


def company_import_diff(
    connection: psycopg.Connection[dict[str, Any]],
    file_domains: set[str],
    has_profile: bool | None = None,
    max_contacts: int | None = None,
    min_contacts: int | None = None,
    include_disabled: bool = False,
    tag: str | Sequence[str] | None = None,
    exclude_tags: Sequence[str] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Compare tracker file domains to CRM scope (dry-run only, §V.146).

    CRM side is filtered with the same list-family flags as export. Bucket
    lists are sorted lowercased domains. ``record_count`` is the size of the
    union of file domains and CRM-scope domains.

    Args:
        connection: Open database connection.
        file_domains: Lowercased domains from the tracker NDJSON file.
        has_profile: Presence filter on CRM scope.
        max_contacts: Inclusive upper bound on contact_count.
        min_contacts: Inclusive lower bound on contact_count.
        include_disabled: When ``True``, includes disabled CRM companies.
        tag: One resolved tag id or a sequence; AND-compose membership.
        exclude_tags: Resolved tag ids excluded via NOT EXISTS.
        status: Pipeline cohort filter (§V.138).

    Returns:
        Diff dict with ``missing_in_crm``, ``missing_profile``,
        ``zero_contacts``, ``disabled``, ``extra_in_crm``, and
        ``record_count``.
    """
    crm_rows = export_companies(
        connection,
        has_profile=has_profile,
        max_contacts=max_contacts,
        min_contacts=min_contacts,
        include_disabled=include_disabled,
        tag=tag,
        exclude_tags=exclude_tags,
        status=status,
        full=False,
    )
    crm_by_domain = {str(row["domain"]).lower(): row for row in crm_rows}
    crm_domains = set(crm_by_domain)
    file_set = {d.lower() for d in file_domains}

    missing_in_crm = sorted(file_set - crm_domains)
    extra_in_crm = sorted(crm_domains - file_set)
    missing_profile = sorted(
        domain for domain, row in crm_by_domain.items() if not row["has_profile"]
    )
    zero_contacts = sorted(
        domain for domain, row in crm_by_domain.items() if row["contact_count"] == 0
    )
    disabled = sorted(
        domain
        for domain, row in crm_by_domain.items()
        if row["disabled_reason"] is not None
    )
    return {
        "missing_in_crm": missing_in_crm,
        "missing_profile": missing_profile,
        "zero_contacts": zero_contacts,
        "disabled": disabled,
        "extra_in_crm": extra_in_crm,
        "record_count": len(file_set | crm_domains),
    }


def get_company_by_domain_exact(
    connection: psycopg.Connection[dict[str, Any]],
    domain: str,
) -> Company | None:
    """Get a company by canonical domain only (no alias resolve).

    Used by merge ``--from`` so an already-absorbed brand alias is not
    mistaken for a live source row (§V.143 idempotent path).
    """
    normalized = _normalize_company_domain(domain)
    if not normalized:
        return None
    row = connection.execute(
        "SELECT * FROM company WHERE domain = %(domain)s",
        {"domain": normalized},
    ).fetchone()
    if row is None:
        return None
    return Company.model_validate(row)


def get_company_by_domain(
    connection: psycopg.Connection[dict[str, Any]],
    domain: str,
) -> Company | None:
    """Get a company by primary domain or alias (§V.142).

    Args:
        connection: Open database connection.
        domain: Company domain or registered alias (case-insensitive).

    Returns:
        Canonical company if found, None otherwise.
    """
    normalized = _normalize_company_domain(domain)
    if not normalized:
        return None
    row = connection.execute(
        "SELECT * FROM company WHERE domain = %(domain)s",
        {"domain": normalized},
    ).fetchone()
    if row is not None:
        return Company.model_validate(row)
    row = connection.execute(
        """\
        SELECT c.*
        FROM company_alias a
        JOIN company c ON c.id = a.company_id
        WHERE a.domain = %(domain)s
        """,
        {"domain": normalized},
    ).fetchone()
    if row is None:
        return None
    return Company.model_validate(row)


def merge_companies(
    connection: psycopg.Connection[dict[str, Any]],
    from_company_id: str,
    into_company_id: str,
    *,
    move_contacts: bool = False,
    original_from_domain: str | None = None,
) -> Company | None:
    """Absorb *from* into *into* (§V.143).

    Records ``original_from_domain`` (or the source's current domain) as an
    alias on the survivor, soft-disables the source with
    ``merged:into <into.domain>`` (overwriting any prior source reason),
    and rewrites the source domain to a tombstone so the shared domain
    space stays unique (§V.142). Optional contact reassignment runs in the
    same transaction. Disabled source and disabled survivor are allowed;
    the survivor's ``disabled_reason`` is never cleared (§V.143 / §V.114).

    Idempotent when the source is already disabled with the matching reason
    and the original domain is already an alias of the survivor.

    Returns:
        The survivor company, or ``None`` if either id is missing.
    """
    if from_company_id == into_company_id:
        raise ValueError("cannot merge a company into itself")
    source = get_company(connection, from_company_id)
    survivor = get_company(connection, into_company_id)
    if source is None or survivor is None:
        return None
    absorbed_domain = _normalize_company_domain(
        original_from_domain if original_from_domain is not None else source.domain
    )
    expected_reason = _merged_into_reason(survivor.domain)
    existing_alias = connection.execute(
        """\
        SELECT company_id FROM company_alias
        WHERE domain = %(domain)s
        """,
        {"domain": absorbed_domain},
    ).fetchone()
    if (
        source.disabled_reason == expected_reason
        and existing_alias is not None
        and existing_alias["company_id"] == survivor.id
    ):
        return survivor
    tombstone = _tombstone_merged_domain(source.id)
    # Free the canonical domain before inserting the alias (shared space).
    if source.domain == absorbed_domain or not source.domain.startswith("__merged__."):
        connection.execute(
            """\
            UPDATE company
            SET domain = %(tombstone)s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %(id)s
            """,
            {"tombstone": tombstone, "id": source.id},
        )
    if existing_alias is None:
        connection.execute(
            """\
            INSERT INTO company_alias (domain, company_id)
            VALUES (%(domain)s, %(company_id)s)
            """,
            {"domain": absorbed_domain, "company_id": survivor.id},
        )
    elif existing_alias["company_id"] != survivor.id:
        raise ValueError(
            f"domain {absorbed_domain!r} is already an alias of another company"
        )
    connection.execute(
        """\
        UPDATE company
        SET disabled_reason = %(reason)s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %(id)s
        """,
        {"reason": expected_reason, "id": source.id},
    )
    if move_contacts:
        connection.execute(
            """\
            UPDATE contact
            SET company_id = %(into_id)s,
                updated_at = CURRENT_TIMESTAMP
            WHERE company_id = %(from_id)s
            """,
            {"into_id": survivor.id, "from_id": source.id},
        )
    connection.commit()
    return get_company(connection, survivor.id)


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
    return write_company_fields(connection, company_id, fields, commit=True)


def write_company_fields(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
    fields: dict[str, object],
    *,
    commit: bool = True,
) -> Company | None:
    """Apply a company field map; ``commit=False`` defers for a caller txn."""
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
    if commit:
        connection.commit()
    if row is None:
        return None
    return Company.model_validate(row)


def disable_company(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
    reason: str,
) -> Company | None:
    """Soft-disable a company by writing ``disabled_reason``.

    A ``disabled_reason IS NULL`` gate blocks double-disable: an already
    disabled company does not match, so the call returns ``None`` without
    overwriting an earlier reason. Disable is reversible -- ``enable_company``
    clears ``disabled_reason`` to re-enable the company (a company with no
    discoverable contacts this cycle may have some next).

    Args:
        connection: Open database connection.
        company_id: Company ID.
        reason: Explanation written to ``disabled_reason`` (stored verbatim);
            operator-facing (out-of-business / not-a-fit). The lead-contacts
            negative-verdict memoization no longer disables a company -- it
            tags it ``no-contacts-found`` or ``contacts-exhausted`` instead
            (§V.96, §V.116).

    Returns:
        Updated company, or ``None`` when no active (not-yet-disabled) company
        with that id exists -- i.e. missing or already disabled.
    """
    row = connection.execute(
        """\
        UPDATE company
        SET disabled_reason = %(reason)s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %(id)s
          AND disabled_reason IS NULL
        RETURNING *
        """,
        {"id": company_id, "reason": reason},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Company.model_validate(row)


def enable_company(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
) -> Company | None:
    """Re-enable a soft-disabled company by clearing ``disabled_reason``.

    Mirror of ``disable_company``. A ``disabled_reason IS NOT NULL`` gate
    blocks enabling an already-active company: an active company does not
    match, so the call returns ``None``. A re-enabled company reappears in the
    default ``company list``.

    Raises ``ValueError`` when this company's domain is registered as an
    alias of a different company (§V.143 — cannot revive a domain that
    still belongs to a survivor's alias set).

    Args:
        connection: Open database connection.
        company_id: Company ID.

    Returns:
        Updated company, or ``None`` when no disabled company with that id
        exists -- i.e. missing or already active.
    """
    current = get_company(connection, company_id)
    if current is None:
        return None
    alias_owner = connection.execute(
        "SELECT company_id FROM company_alias WHERE domain = %(domain)s",
        {"domain": current.domain},
    ).fetchone()
    if alias_owner is not None and alias_owner["company_id"] != company_id:
        raise ValueError(
            f"company domain {current.domain!r} is an alias of another company"
        )
    row = connection.execute(
        """\
        UPDATE company
        SET disabled_reason = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %(id)s
          AND disabled_reason IS NOT NULL
        RETURNING *
        """,
        {"id": company_id},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Company.model_validate(row)
