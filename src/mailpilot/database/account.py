"""Account CRUD."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.sql import SQL, Composed

from mailpilot.database._common import (
    _build_update,
    _empty_to_none,
    _new_id,
)
from mailpilot.models import (
    Account,
    AccountSummary,
)


def create_account(
    connection: psycopg.Connection[dict[str, Any]],
    email: str,
    display_name: str = "",
    *,
    signature_full_name: str | None = None,
    signature_title: str | None = None,
    signature_website: str | None = None,
    signature_phone: str | None = None,
) -> Account | None:
    """Create a new account.

    Uses ``ON CONFLICT (email) DO NOTHING`` per §V.16(+) so callers can
    safely re-invoke without catching ``UniqueViolation``. Returns ``None``
    when the row already exists.

    Signature fields (§V.151) are optional; empty strings store as NULL.
    ``display_name`` is From-header only and is not aliased to
    ``signature_full_name``.

    Args:
        connection: Open database connection.
        email: Gmail address.
        display_name: Display name for the account (From header).
        signature_full_name: Signature name line (optional).
        signature_title: Signature title line (optional).
        signature_website: Signature website absolute http(s) URL (optional).
        signature_phone: Signature phone line (optional).

    Returns:
        Created account, or ``None`` if an account with this email already
        existed (concurrent worker won or operator re-create).
    """
    row = connection.execute(
        """\
        INSERT INTO account (
            id, email, display_name,
            signature_full_name, signature_title,
            signature_website, signature_phone
        )
        VALUES (
            %(id)s, %(email)s, %(display_name)s,
            %(signature_full_name)s, %(signature_title)s,
            %(signature_website)s, %(signature_phone)s
        )
        ON CONFLICT (email) DO NOTHING
        RETURNING *
        """,
        {
            "id": _new_id(),
            "email": email,
            "display_name": display_name,
            "signature_full_name": _empty_to_none(signature_full_name),
            "signature_title": _empty_to_none(signature_title),
            "signature_website": _empty_to_none(signature_website),
            "signature_phone": _empty_to_none(signature_phone),
        },
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
    until: str | None = None,
    include_disabled: bool = False,
) -> list[AccountSummary]:
    """List accounts as summaries (identify/filter/order fields only).

    Internal callers needing the full record (e.g. ``gmail_history_id``,
    ``watch_expiration``) must hydrate via ``get_account()`` per id.

    Disabled accounts (``disabled_reason IS NOT NULL``) are hidden by default
    (§V.118) -- the sync loop full sweep, ``account sync`` all-accounts mode,
    and ``renew_watches`` all read this default-excluding listing, so a
    disabled account drops out of every Gmail-touching path at once. Pass
    ``include_disabled=True`` to surface them.

    Args:
        connection: Open database connection.
        limit: Maximum results.
        since: ISO datetime inclusive lower bound on ``created_at``.
        until: ISO datetime inclusive upper bound on ``created_at``.
        include_disabled: When ``True``, includes disabled accounts; the
            default (``False``) hides them (§V.118).

    Returns:
        List of account summaries ordered by creation time.
    """
    conditions: list[Composed | SQL] = []
    params: dict[str, object] = {"limit": limit}
    if since is not None:
        conditions.append(SQL("created_at >= %(since)s"))
        params["since"] = since
    if until is not None:
        conditions.append(SQL("created_at <= %(until)s"))
        params["until"] = until
    if not include_disabled:
        conditions.append(SQL("disabled_reason IS NULL"))
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT id, email, display_name, last_synced_at, disabled_reason, "
        "signature_full_name, signature_title, signature_website, "
        "signature_phone, created_at "
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

    Signature fields (§V.151) accept empty string to clear (stored as NULL).
    ``display_name`` is From-header only and is not aliased to signature name.

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
    for key in (
        "signature_full_name",
        "signature_title",
        "signature_website",
        "signature_phone",
    ):
        if key in updates and isinstance(updates[key], str):
            updates[key] = _empty_to_none(updates[key])  # type: ignore[arg-type]
    updates["id"] = account_id
    query = _build_update("account", updates, SQL("id = %(id)s"))
    row = connection.execute(query, updates).fetchone()
    connection.commit()
    if row is None:
        return None
    return Account.model_validate(row)


def disable_account(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    reason: str,
) -> Account | None:
    """Soft-disable an account by writing ``disabled_reason``.

    A ``disabled_reason IS NULL`` gate blocks double-disable: an already
    disabled account does not match, so the call returns ``None`` without
    overwriting an earlier reason (mirror of ``disable_company`` per §V.118).
    A disabled account is gated out of every Gmail-touching path -- the sync
    loop, ``account sync`` all-accounts mode, ``renew_watches``, and send/reply
    (§V.79). Disable is reversible via ``enable_account``.

    Args:
        connection: Open database connection.
        account_id: Account ID.
        reason: Explanation written to ``disabled_reason`` (stored verbatim).

    Returns:
        Updated account, or ``None`` when no active (not-yet-disabled) account
        with that id exists -- i.e. missing or already disabled.
    """
    row = connection.execute(
        """\
        UPDATE account
        SET disabled_reason = %(reason)s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %(id)s
          AND disabled_reason IS NULL
        RETURNING *
        """,
        {"id": account_id, "reason": reason},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Account.model_validate(row)


def enable_account(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
) -> Account | None:
    """Re-enable a soft-disabled account by clearing ``disabled_reason``.

    Mirror of ``disable_account``. A ``disabled_reason IS NOT NULL`` gate
    blocks enabling an already-active account: an active account does not
    match, so the call returns ``None``. A re-enabled account reappears in the
    default ``account list`` and resumes syncing (§V.118).

    Args:
        connection: Open database connection.
        account_id: Account ID.

    Returns:
        Updated account, or ``None`` when no disabled account with that id
        exists -- i.e. missing or already active.
    """
    row = connection.execute(
        """\
        UPDATE account
        SET disabled_reason = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %(id)s
          AND disabled_reason IS NOT NULL
        RETURNING *
        """,
        {"id": account_id},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Account.model_validate(row)
