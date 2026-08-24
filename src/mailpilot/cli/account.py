"""Gmail account commands."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import click

from mailpilot._filters import (
    include_disabled_option,
    limit_option,
    time_window_options,
)
from mailpilot.cli.main import (
    _db,
    _resolve_account,
    main,
    output,
    output_entity,
    output_error,
)

# -- Account commands ----------------------------------------------------------


@main.group()
def account() -> None:
    """Manage Gmail accounts."""


def _validate_signature_website(website: str | None) -> str | None:
    """Validate signature website as absolute http(s) URL (§V.151).

    Empty string clears the field (returned as empty for caller to store NULL).
    Non-empty values must be absolute ``http://`` or ``https://``; no auto-prefix.
    """
    if website is None:
        return None
    if website == "":
        return ""
    from urllib.parse import urlsplit

    parts = urlsplit(website)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        output_error(
            f"signature website must be an absolute http(s) URL (got {website!r})",
            "validation_error",
        )
    return website


@account.command("create")
@click.option("--email", required=True, help="Gmail address.")
@click.option("--display-name", default="", help="Display name (From header only).")
@click.option(
    "--signature-full-name",
    default=None,
    help="Signature full name (optional).",
)
@click.option(
    "--signature-title",
    default=None,
    help="Signature title (optional).",
)
@click.option(
    "--signature-website",
    default=None,
    help="Signature website absolute http(s) URL (optional).",
)
@click.option(
    "--signature-phone",
    default=None,
    help="Signature phone (optional).",
)
def account_create(
    email: str,
    display_name: str,
    signature_full_name: str | None,
    signature_title: str | None,
    signature_website: str | None,
    signature_phone: str | None,
) -> None:
    """Create a new Gmail account."""
    from mailpilot.database import create_account
    from mailpilot.operator_log import cli_mutation, operator_event

    if not email.strip():
        output_error("email cannot be empty", "validation_error")
    signature_website = _validate_signature_website(signature_website)
    with (
        _db(mutate=True) as connection,
        cli_mutation("account", "create", email=email),
    ):
        created = create_account(
            connection,
            email=email,
            display_name=display_name,
            signature_full_name=signature_full_name,
            signature_title=signature_title,
            signature_website=signature_website,
            signature_phone=signature_phone,
        )
        if created is None:
            output_error(
                f"account with email={email!r} already exists",
                "duplicate_key",
            )
        changed = ["email", "display_name"]
        for field in (
            "signature_full_name",
            "signature_title",
            "signature_website",
            "signature_phone",
        ):
            if getattr(created, field) is not None:
                changed.append(field)
        operator_event(
            "account.create",
            entity_id=created.id,
            email=created.email,
            changed=changed,
        )
        output_entity("account", created)


@account.command("list")
@include_disabled_option
@time_window_options("created_at")
@limit_option
def account_list(
    limit: int, since: str | None, until: str | None, include_disabled: bool
) -> None:
    """List Gmail accounts as summaries."""
    from mailpilot.database import list_accounts

    with _db() as connection:
        accounts = list_accounts(
            connection,
            limit=limit,
            since=since,
            until=until,
            include_disabled=include_disabled,
        )
        output({"accounts": [a.model_dump(mode="json") for a in accounts]})


@account.command("view")
@click.argument("account_ref")
def account_view(account_ref: str) -> None:
    """Show a Gmail account by email or ID."""
    with _db() as connection:
        output_entity("account", _resolve_account(connection, account_ref))


@account.command("update")
@click.argument("account_ref")
@click.option(
    "--display-name",
    default=None,
    help="Display name (From header only).",
)
@click.option(
    "--signature-full-name",
    default=None,
    help="Signature full name (empty string clears).",
)
@click.option(
    "--signature-title",
    default=None,
    help="Signature title (empty string clears).",
)
@click.option(
    "--signature-website",
    default=None,
    help="Signature website absolute http(s) URL (empty string clears).",
)
@click.option(
    "--signature-phone",
    default=None,
    help="Signature phone (empty string clears).",
)
def account_update(
    account_ref: str,
    display_name: str | None,
    signature_full_name: str | None,
    signature_title: str | None,
    signature_website: str | None,
    signature_phone: str | None,
) -> None:
    """Update a Gmail account (addressed by email or ID).

    Signature flags are field-selective: omit leaves the field unchanged;
    empty string clears it. Website must be absolute http(s) when non-empty.
    """
    from mailpilot.database import update_account
    from mailpilot.operator_log import cli_mutation, operator_event

    signature_website = _validate_signature_website(signature_website)
    with _db(mutate=True) as connection:
        before = _resolve_account(connection, account_ref)
        account_id = before.id
        fields: dict[str, object] = {}
        if display_name is not None:
            fields["display_name"] = display_name
        if signature_full_name is not None:
            fields["signature_full_name"] = signature_full_name
        if signature_title is not None:
            fields["signature_title"] = signature_title
        if signature_website is not None:
            fields["signature_website"] = signature_website
        if signature_phone is not None:
            fields["signature_phone"] = signature_phone
        with cli_mutation("account", "update", entity_id=account_id):
            updated = update_account(connection, account_id, **fields)
            if updated is None:
                output_error(f"account not found: {account_id}", "not_found")
            changed = [
                field
                for field in (
                    "display_name",
                    "signature_full_name",
                    "signature_title",
                    "signature_website",
                    "signature_phone",
                )
                if getattr(before, field) != getattr(updated, field)
            ]
            operator_event(
                "account.update",
                entity_id=account_id,
                changed=changed,
            )
            output_entity("account", updated)


@account.command("disable")
@click.argument("account_ref")
@click.option(
    "--reason",
    required=True,
    help="Explanation written to disabled_reason.",
)
def account_disable(account_ref: str, reason: str) -> None:
    """Soft-disable a Gmail account by writing disabled_reason.

    A disabled account is hidden from `account list` unless `--include-disabled`
    is passed, and is gated out of every Gmail-touching path: the sync loop,
    `account sync` all-accounts mode, watch renewal, and send/reply. Disable is
    reversible -- re-enable with `account enable`. Disabling an already-disabled
    account is rejected.
    """
    from mailpilot.database import disable_account
    from mailpilot.operator_log import cli_mutation, operator_event

    if reason.strip() == "":
        output_error("reason cannot be empty", "validation_error")
    with _db(mutate=True) as connection:
        before = _resolve_account(connection, account_ref)
        account_id = before.id
        if before.disabled_reason is not None:
            output_error(
                f"account {account_id} is already disabled "
                f"(reason: {before.disabled_reason})",
                "validation_error",
            )
        with cli_mutation("account", "disable", entity_id=account_id):
            updated = disable_account(connection, account_id, reason)
            if updated is None:
                output_error(
                    f"account {account_id} is already disabled",
                    "validation_error",
                )
            operator_event(
                "account.disable",
                entity_id=account_id,
                changed=["disabled_reason"],
            )
            output_entity("account", updated)


@account.command("enable")
@click.argument("account_ref")
def account_enable(account_ref: str) -> None:
    """Re-enable a soft-disabled Gmail account by clearing disabled_reason.

    The account reappears in the default `account list` and resumes syncing.
    Enabling an account that is not disabled is rejected.
    """
    from mailpilot.database import enable_account
    from mailpilot.operator_log import cli_mutation, operator_event

    with _db(mutate=True) as connection:
        before = _resolve_account(connection, account_ref)
        account_id = before.id
        if before.disabled_reason is None:
            output_error(
                f"account {account_id} is not disabled",
                "validation_error",
            )
        with cli_mutation("account", "enable", entity_id=account_id):
            updated = enable_account(connection, account_id)
            if updated is None:
                output_error(
                    f"account {account_id} is not disabled",
                    "validation_error",
                )
            operator_event(
                "account.enable",
                entity_id=account_id,
                changed=["disabled_reason"],
            )
            output_entity("account", updated)


@account.command("sync")
@click.option(
    "--account-email",
    default=None,
    help="Sync only the given account (email or ID); omit to sync all accounts.",
)
@click.option(
    "--since",
    default=None,
    help=(
        "ISO datetime lower bound on the initial full-INBOX backfill "
        "(Gmail 'after:'); ignored once incremental history sync takes over."
    ),
)
def account_sync(account_email: str | None, since: str | None) -> None:
    """Run a one-shot Gmail sync for one or all accounts."""
    from datetime import datetime

    import logfire

    from mailpilot.database import get_account, list_accounts
    from mailpilot.gmail import GmailClient
    from mailpilot.google_auth import has_google_credentials
    from mailpilot.settings import get_settings
    from mailpilot.sync import (
        _poll_account_calendar,  # pyright: ignore[reportPrivateUsage]
        sync_account,
    )

    backfill_since: datetime | None = None
    if since is not None:
        try:
            backfill_since = datetime.fromisoformat(since)
        except ValueError as exc:
            output_error(f"invalid --since value: {exc}", "validation_error")

    settings = get_settings()
    with _db(mutate=True) as connection:
        if account_email is not None:
            accounts = [_resolve_account(connection, account_email)]
        else:
            summaries = list_accounts(connection, limit=1000)
            accounts = [
                full
                for full in (get_account(connection, s.id) for s in summaries)
                if full is not None
            ]

        rows: list[dict[str, object]] = []
        total_stored = 0
        poll_calendars = has_google_credentials()
        with logfire.span("cli.account.sync", account_count=len(accounts)) as span:
            for acc in accounts:
                row: dict[str, object] = {
                    "account_id": acc.id,
                    "email": acc.email,
                }
                try:
                    client = GmailClient(acc.email)
                    stored = sync_account(
                        connection,
                        acc,
                        client,
                        settings,
                        backfill_since=backfill_since,
                    )
                    row["stored"] = stored
                    total_stored += stored
                    # §V.126: ingest the account's upcoming calendar events in
                    # the same pass. The helper isolates its own transport
                    # fault (never raised), so a calendar failure is recorded
                    # on the row but never aborts the Gmail sync.
                    if poll_calendars:
                        calendar_error = _poll_account_calendar(connection, acc)
                        if calendar_error is not None:
                            row["calendar_error"] = calendar_error
                except Exception as exc:
                    from mailpilot.sync import sync_errors

                    sync_errors.add(
                        1,
                        attributes={
                            "account_id": acc.id,
                            "reason": "cli_sync_exception",
                        },
                    )
                    logfire.exception(
                        "cli.account.sync.failed",
                        account_id=acc.id,
                        email=acc.email,
                    )
                    row["error"] = str(exc)
                rows.append(row)
            account_succeeded = sum(1 for r in rows if "error" not in r)
            account_failed = sum(1 for r in rows if "error" in r)
            span.set_attribute("total_stored", total_stored)
            span.set_attribute("account_succeeded", account_succeeded)
            span.set_attribute("account_failed", account_failed)
        output({"accounts": rows})
