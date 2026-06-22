"""CLI interface for MailPilot.

Startup-critical: only ``click`` is imported at module level. All heavy
dependencies (logfire, psycopg, httpx, pydantic, mailpilot.database,
mailpilot.settings) are lazy-imported inside command functions so that
``--help`` / ``--version`` stay fast (~50 ms).
When adding new commands, keep imports inside the function body.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, NoReturn

import click

from mailpilot._filters import (
    DIRECTIONS,
    enum_option,
    include_disabled_option,
    limit_option,
    presence_option,
    range_options,
    scope_option,
    tag_filter_options,
    time_window_options,
)

if TYPE_CHECKING:
    import pathlib

    from logfire import ScrubMatch

    from mailpilot.models import Account, Company, Contact

# Hex digit set for the UUID-shape probe in _looks_like_uuid (natural-key vs id).
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

# Keep in sync with ActivityType in models.py and CHECK constraint in schema.sql.
_ACTIVITY_TYPES = [
    "email_sent",
    "email_received",
    "note_added",
    "tag_added",
    "tag_removed",
    "status_changed",
    "enrollment_added",
    "enrollment_completed",
    "enrollment_failed",
    "enrollment_paused",
    "enrollment_resumed",
    "enrollment_disabled",
    "enrollment_enabled",
]

# Persisted email.route_method values; mirrors the schema CHECK set and the
# email projection enum (the 7 routing decisions an operator can filter on).
_ROUTE_METHODS = [
    "classified",
    "thread_match",
    "rfc_message_id_match",
    "skipped_outside_window",
    "skipped_no_workflows",
    "skipped_predates_workflows",
    "skipped_no_inbound_workflows",
]

# workflow.status / enrollment.status / task.status CHECK sets, mirrored from
# schema.sql so the Choice options reject out-of-set values at parse time.
_WORKFLOW_STATUSES = ["draft", "active", "paused"]
_WORKFLOW_TEMPLATES = ["outbound-general", "inbound-general", "inbound-google-drive"]
_EMAIL_STATUSES = ["sent", "received", "bounced"]
_ENROLLMENT_STATUSES = ["active", "disabled"]
_TASK_STATUSES = ["pending", "completed", "failed", "cancelled"]


def _database_url() -> str:
    """Resolve the database URL from settings at call time (not import time)."""
    from mailpilot.settings import get_settings

    return str(get_settings().database_url)


def scrub_tool_response_callback(match: ScrubMatch) -> Any:
    """Exempt agent ``tool_response`` payloads from default Logfire scrubbing.

    Pydantic-AI ``running tool`` spans carry the structured tool return value
    under the ``tool_response`` attribute. Without this exemption the default
    substring matcher redacts strings like ``"authorized"`` inside KB markdown,
    making §V.57 grounding regressions unverifiable from traces alone. Per
    §V.55, agent tool outputs are non-sensitive by design.
    """
    if match.path[:2] == ("attributes", "tool_response"):
        return match.value
    return None


def configure_logging(debug: bool = False) -> None:
    """Configure Logfire from settings."""
    import sys

    import logfire

    from mailpilot.settings import get_settings

    settings = get_settings()
    logfire.configure(
        service_name="mailpilot",
        environment=settings.logfire_environment,
        token=settings.logfire_token or None,
        console=logfire.ConsoleOptions(
            min_log_level="debug" if debug else "warn",
            show_project_link=False,
            # §V.3: console exporter ! target stderr. Default output=None
            # routes to stdout, where warn/debug lines corrupt the JSON
            # envelope ahead of `output()` (see §B.73).
            output=sys.stderr,
        ),
        send_to_logfire="if-token-present",
        inspect_arguments=False,
        metrics=logfire.MetricsOptions(collect_in_spans=True),
        scrubbing=logfire.ScrubbingOptions(callback=scrub_tool_response_callback),
    )
    logfire.instrument_pydantic_ai()


# -- JSON output pattern -------------------------------------------------------


def output(data: dict[str, Any]) -> None:
    r"""Print structured JSON response to stdout.

    Always RFC 8259 compliant: control characters (\n, \r, \t, etc.) inside
    string values are escaped, so downstream `json.loads` / `jq` callers never
    trip on raw control bytes. `ensure_ascii=False` keeps non-ASCII glyphs
    (em-dashes, accented characters) readable instead of `\uXXXX`-encoded.
    """
    click.echo(json.dumps({**data, "ok": True}, indent=2, ensure_ascii=False))


def output_entity(key: str, model: Any) -> None:
    """Emit a single entity wrapped under its singular key.

    Per SPEC §V.4: `<entity> view|create|update` -> `{"<singular>": {...}, "ok": true}`.
    Symmetric with `output({"<plural>": [...]})` used by list commands.
    """
    click.echo(
        json.dumps(
            {key: model.model_dump(mode="json"), "ok": True},
            indent=2,
            ensure_ascii=False,
        )
    )


def output_error(
    message: str, code: str, extra: dict[str, object] | None = None
) -> NoReturn:
    """Print structured JSON error to stderr and exit.

    ``extra`` merges additional fields into the envelope -- e.g. ``db check``
    inlines its schema report alongside the error code per §I.cli / §V.109.
    """
    from opentelemetry import trace

    payload: dict[str, object] = {"error": code, "message": message, "ok": False}
    if extra:
        payload.update(extra)
    current = trace.get_current_span()
    ctx = current.get_span_context() if current else None
    if ctx is not None and ctx.is_valid:
        payload["trace_id"] = format(ctx.trace_id, "032x")
    click.echo(json.dumps(payload, indent=2, ensure_ascii=False), err=True)
    raise SystemExit(1)


def _looks_like_uuid(value: str) -> bool:
    """Return True when ``value`` has the 8-4-4-4-12 hex shape of a UUID (§V.107).

    Natural keys never collide with this shape: a domain carries dots, an
    email an at-sign, a tag name is free text. So a value of this shape is an
    id and anything else is a natural key -- the basis for polymorphic
    resolution.
    """
    parts = value.split("-")
    if len(parts) != 5:
        return False
    expected_lengths = (8, 4, 4, 4, 12)
    return all(
        len(part) == length and all(ch in _HEX_DIGITS for ch in part)
        for part, length in zip(parts, expected_lengths, strict=True)
    )


def _resolve_account(connection: Any, account_ref: str | None) -> Account:
    """Resolve an account reference (email or UUID) to its full row (§V.107).

    A UUID-shaped ref resolves by id; any other value resolves by the email
    natural key (§V.90), case-insensitively via ``get_account_by_email``. A
    None ref (the flag was omitted) exits ``validation_error``; an unknown key
    exits ``not_found`` per §V.94.

    Args:
        connection: Open database connection.
        account_ref: Value of ``--account-email`` (email | UUID | None).

    Returns:
        The resolved ``Account`` row.
    """
    from mailpilot.database import get_account, get_account_by_email

    if account_ref is None:
        output_error("--account-email is required", "validation_error")
    account = (
        get_account(connection, account_ref)
        if _looks_like_uuid(account_ref)
        else get_account_by_email(connection, account_ref)
    )
    if account is None:
        output_error(f"account not found: {account_ref}", "not_found")
    return account


def _resolve_company(connection: Any, company_ref: str) -> Company:
    """Resolve a company reference (domain or UUID) to its full row (§V.107).

    A UUID-shaped ref resolves by id; any other value resolves by the domain
    natural key (§V.90) via ``get_company_by_domain``. An unknown key exits
    ``not_found`` per §V.94.

    Args:
        connection: Open database connection.
        company_ref: A company domain or UUID.

    Returns:
        The resolved ``Company`` row.
    """
    from mailpilot.database import get_company, get_company_by_domain

    company = (
        get_company(connection, company_ref)
        if _looks_like_uuid(company_ref)
        else get_company_by_domain(connection, company_ref)
    )
    if company is None:
        output_error(f"company not found: {company_ref}", "not_found")
    return company


def _resolve_contact(connection: Any, contact_ref: str) -> Contact:
    """Resolve a contact reference (email or UUID) to its full row (§V.107).

    A UUID-shaped ref resolves by id; any other value resolves by the email
    natural key (§V.90) via ``get_contact_by_email``. An unknown key exits
    ``not_found`` per §V.94.

    Args:
        connection: Open database connection.
        contact_ref: A contact email or UUID.

    Returns:
        The resolved ``Contact`` row.
    """
    from mailpilot.database import get_contact, get_contact_by_email

    contact = (
        get_contact(connection, contact_ref)
        if _looks_like_uuid(contact_ref)
        else get_contact_by_email(connection, contact_ref)
    )
    if contact is None:
        output_error(f"contact not found: {contact_ref}", "not_found")
    return contact


def _resolve_company_id(connection: Any, company_ref: str) -> str:
    """Resolve a company reference to its id, deferring existence to the caller.

    A UUID-shaped ref passes through unfetched (the caller's own ``load``/
    ``get`` validates existence); a domain resolves via the natural key,
    exiting ``not_found`` when unknown (§V.94, §V.107).
    """
    if _looks_like_uuid(company_ref):
        return company_ref
    from mailpilot.database import get_company_by_domain

    company = get_company_by_domain(connection, company_ref)
    if company is None:
        output_error(f"company not found: {company_ref}", "not_found")
    return company.id


def _resolve_contact_id(connection: Any, contact_ref: str) -> str:
    """Resolve a contact reference to its id, deferring existence to the caller.

    A UUID-shaped ref passes through unfetched (the caller's own ``load``/
    ``get`` validates existence); an email resolves via the natural key,
    exiting ``not_found`` when unknown (§V.94, §V.107).
    """
    if _looks_like_uuid(contact_ref):
        return contact_ref
    from mailpilot.database import get_contact_by_email

    contact = get_contact_by_email(connection, contact_ref)
    if contact is None:
        output_error(f"contact not found: {contact_ref}", "not_found")
    return contact.id


def _resolve_tag(connection: Any, tag_ref: str) -> Any:
    """Resolve a tag reference (name or UUID) to its vocabulary row (§V.116).

    Tags are addressed by their globally unique name (operators never paste
    tag ids); a UUID-shaped ref still resolves by id so a tag id round-trips
    from one command's output into the next (§V.107). An undefined or malformed
    name exits ``not_found`` (§V.94) -- ``tag add`` never auto-creates the tag.
    """
    from mailpilot.database import get_tag, get_tag_by_name

    try:
        tag = (
            get_tag(connection, tag_ref)
            if _looks_like_uuid(tag_ref)
            else get_tag_by_name(connection, tag_ref)
        )
    except ValueError:
        tag = None
    if tag is None:
        output_error(f"tag not found: {tag_ref}", "not_found")
    return tag


# -- Main CLI ------------------------------------------------------------------


def _print_completion(
    ctx: click.Context, param: click.Parameter, value: str | None
) -> None:
    """Eager callback: emit the shell completion script and exit.

    Runs before Click validates that a subcommand was given, so
    ``mailpilot --completion zsh`` works without supplying a subcommand.
    """
    if not value or ctx.resilient_parsing:
        return
    from click.shell_completion import get_completion_class

    comp_cls = get_completion_class(value)
    if comp_cls is None:
        click.echo(f"unsupported shell: {value}", err=True)
        ctx.exit(1)
    click.echo(comp_cls(ctx.command, {}, "mailpilot", "_MAILPILOT_COMPLETE").source())
    ctx.exit(0)


def _print_skill(ctx: click.Context, param: click.Parameter, value: bool) -> None:
    """Eager callback: emit the packaged SKILL.md body verbatim and exit.

    Runs before Click validates that a subcommand was given, so
    ``mailpilot --skill`` works without supplying a subcommand. Hard-fails
    with a stderr diagnostic when the package data is missing.
    """
    if not value or ctx.resilient_parsing:
        return
    from importlib.resources import files

    skill_path = files("mailpilot").joinpath("SKILL.md")
    try:
        body = skill_path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, OSError) as exc:
        click.echo(f"mailpilot: SKILL.md missing from package: {exc}", err=True)
        ctx.exit(1)
        return
    click.echo(body, nl=False)
    ctx.exit(0)


@click.group()
@click.version_option()
@click.option("--debug", is_flag=True, help="Enable debug logging.")
@click.option(
    "--completion",
    type=click.Choice(["bash", "zsh", "fish"]),
    default=None,
    is_eager=True,
    expose_value=False,
    callback=_print_completion,
    help="Print shell completion script and exit.",
)
@click.option(
    "--skill",
    is_flag=True,
    default=False,
    is_eager=True,
    expose_value=False,
    callback=_print_skill,
    help="Print the packaged SKILL.md body and exit.",
)
@click.pass_context
def main(ctx: click.Context, debug: bool) -> None:
    """MailPilot -- CRM for cold email outreach via Gmail."""
    ctx.ensure_object(dict)
    ctx.obj["debug"] = debug
    if ctx.invoked_subcommand is not None:
        configure_logging(debug=debug)


# -- Status command ------------------------------------------------------------


@main.command()
def status() -> None:
    """Show application state summary including sync loop status."""
    from mailpilot.database import get_status_payload, initialize_database
    from mailpilot.settings import get_settings

    settings = get_settings()
    connection = initialize_database(str(settings.database_url))
    try:
        output({"status": get_status_payload(connection, settings)})
    finally:
        connection.close()


# -- Run command ---------------------------------------------------------------


@main.command()
def run() -> None:
    """Start the sync loop (Pub/Sub + task runner, foreground)."""
    from mailpilot.database import initialize_database
    from mailpilot.settings import get_settings
    from mailpilot.sync import start_sync_loop

    settings = get_settings()
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        start_sync_loop(connection, settings)
    finally:
        connection.close()


# -- DB schema commands --------------------------------------------------------


@main.group()
def db() -> None:
    """Provision and migrate the database schema, off the connection hot path."""


@db.command("init")
def db_init() -> None:
    """Provision an empty database from schema.sql.

    Refuses to touch a populated database -- no --force footgun; idempotent
    no-op-with-message when the schema is already current.
    """
    from mailpilot.database import provision_database

    report = provision_database(_database_url())
    if report["provisioned"]:
        output({"db": {**report, "message": "database provisioned"}})
        return
    if report["verdict"] == "current":
        output({"db": {**report, "message": "database already initialized"}})
        return
    if report["verdict"] == "pending":
        output_error(
            "database already initialized; run 'mailpilot db migrate' to advance it",
            "already_initialized",
            {"report": report},
        )
    output_error(
        "database already initialized; schema drift detected -- "
        "investigate divergence (no migration path)",
        "already_initialized",
        {"report": report},
    )


@db.command("migrate")
def db_migrate() -> None:
    """Apply pending forward migrations in version order.

    Each migration runs in its own transaction and is recorded in
    ``schema_migrations``; a no-op when nothing is pending.
    """
    from mailpilot.database import initialize_database, migrate_database

    connection = initialize_database(_database_url())
    try:
        applied = migrate_database(connection)
    finally:
        connection.close()
    output({"db": {"applied": applied, "count": len(applied)}})


@db.command("check")
def db_check() -> None:
    """Report the schema verdict; exit 1 on pending or drift.

    A scriptable deploy gate: ``current`` -> ok envelope + exit 0;
    ``pending``/``drift`` -> ``schema_migration_pending``/``schema_drift``
    error envelope with the report inlined + exit 1.
    """
    from mailpilot.database import determine_schema_verdict, initialize_database

    connection = initialize_database(_database_url())
    try:
        status = determine_schema_verdict(connection)
    finally:
        connection.close()
    report: dict[str, object] = {
        "recorded_hash": status.recorded_hash,
        "current_hash": status.current_hash,
        "applied": status.applied,
        "pending": status.pending,
        "verdict": status.verdict,
    }
    if status.verdict == "current":
        output({"db": report})
        return
    if status.verdict == "pending":
        output_error(
            f"{status.pending} schema migration(s) pending; run 'mailpilot db migrate'",
            "schema_migration_pending",
            {"report": report},
        )
    output_error(
        "schema drift detected; investigate divergence -- no migration path",
        "schema_drift",
        {"report": report},
    )


# -- Config commands -----------------------------------------------------------


@main.group()
def config() -> None:
    """Manage configuration."""


@config.command("get")
@click.argument("key", required=False)
def config_get(key: str | None) -> None:
    """Show config (all or single key)."""
    from mailpilot.settings import get_settings

    settings = get_settings()
    data = settings.model_dump(mode="json")

    if key:
        if key not in data:
            output_error(f"unknown config key: {key}", "invalid_key")
        output({"key": key, "value": data[key]})
    else:
        output({"config": data})


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Set a config value."""
    from mailpilot.settings import Settings, set_setting

    if key not in Settings.model_fields:
        output_error(f"unknown config key: {key}", "invalid_key")

    field_info = Settings.model_fields[key]
    annotation = field_info.annotation

    if annotation is int or annotation == (int | None):
        parsed_value: object = int(value)
    elif annotation == list[str]:
        parsed_value = json.loads(value) if value.startswith("[") else [value]
    elif annotation == list[int]:
        parsed_value = json.loads(value) if value.startswith("[") else [int(value)]
    else:
        parsed_value = value

    set_setting(key, parsed_value)
    output({"key": key, "value": parsed_value})


# -- Account commands ----------------------------------------------------------


@main.group()
def account() -> None:
    """Manage Gmail accounts."""


@account.command("create")
@click.option("--email", required=True, help="Gmail address.")
@click.option("--display-name", default="", help="Display name.")
def account_create(email: str, display_name: str) -> None:
    """Create a new Gmail account."""
    from mailpilot.database import create_account, initialize_database
    from mailpilot.operator_log import cli_mutation, operator_event

    if not email.strip():
        output_error("email cannot be empty", "validation_error")
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        with cli_mutation("account", "create", email=email):
            created = create_account(connection, email=email, display_name=display_name)
            if created is None:
                output_error(
                    f"account with email={email!r} already exists",
                    "duplicate_key",
                )
            operator_event(
                "account.create",
                entity_id=created.id,
                email=created.email,
                changed=["email", "display_name"],
            )
            output_entity("account", created)
    finally:
        connection.close()


@account.command("list")
@include_disabled_option
@time_window_options("created_at")
@limit_option
def account_list(
    limit: int, since: str | None, until: str | None, include_disabled: bool
) -> None:
    """List Gmail accounts as summaries."""
    from mailpilot.database import initialize_database, list_accounts

    connection = initialize_database(_database_url())
    try:
        accounts = list_accounts(
            connection,
            limit=limit,
            since=since,
            until=until,
            include_disabled=include_disabled,
        )
        output({"accounts": [a.model_dump(mode="json") for a in accounts]})
    finally:
        connection.close()


@account.command("view")
@click.argument("account_ref")
def account_view(account_ref: str) -> None:
    """Show a Gmail account by email or ID."""
    from mailpilot.database import initialize_database

    connection = initialize_database(_database_url())
    try:
        output_entity("account", _resolve_account(connection, account_ref))
    finally:
        connection.close()


@account.command("update")
@click.argument("account_ref")
@click.option("--display-name", default=None, help="Display name.")
def account_update(account_ref: str, display_name: str | None) -> None:
    """Update a Gmail account (addressed by email or ID)."""
    from mailpilot.database import initialize_database, update_account
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        before = _resolve_account(connection, account_ref)
        account_id = before.id
        fields: dict[str, object] = {}
        if display_name is not None:
            fields["display_name"] = display_name
        with cli_mutation("account", "update", entity_id=account_id):
            updated = update_account(connection, account_id, **fields)
            if updated is None:
                output_error(f"account not found: {account_id}", "not_found")
            changed = [
                field
                for field in ("display_name",)
                if getattr(before, field) != getattr(updated, field)
            ]
            operator_event(
                "account.update",
                entity_id=account_id,
                changed=changed,
            )
            output_entity("account", updated)
    finally:
        connection.close()


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
    from mailpilot.database import disable_account, initialize_database
    from mailpilot.operator_log import cli_mutation, operator_event

    if reason.strip() == "":
        output_error("reason cannot be empty", "validation_error")
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
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
    finally:
        connection.close()


@account.command("enable")
@click.argument("account_ref")
def account_enable(account_ref: str) -> None:
    """Re-enable a soft-disabled Gmail account by clearing disabled_reason.

    The account reappears in the default `account list` and resumes syncing.
    Enabling an account that is not disabled is rejected.
    """
    from mailpilot.database import enable_account, initialize_database
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
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
    finally:
        connection.close()


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

    from mailpilot.database import get_account, initialize_database, list_accounts
    from mailpilot.gmail import GmailClient
    from mailpilot.settings import get_settings
    from mailpilot.sync import sync_account

    backfill_since: datetime | None = None
    if since is not None:
        try:
            backfill_since = datetime.fromisoformat(since)
        except ValueError as exc:
            output_error(f"invalid --since value: {exc}", "validation_error")

    settings = get_settings()
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
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
    finally:
        connection.close()


# -- Company commands ----------------------------------------------------------


@main.group()
def company() -> None:
    """Manage target companies."""


@company.command("create")
@click.option("--domain", required=True, help="Primary domain.")
@click.option("--name", default="", help="Company name.")
@click.option(
    "--note",
    default=None,
    help="Optional first note body. Appended atomically as a `note` row.",
)
def company_create(domain: str, name: str, note: str | None) -> None:
    """Create a new company."""
    from mailpilot.database import add_company_note, create_company, initialize_database
    from mailpilot.operator_log import cli_mutation, operator_event

    if not domain.strip():
        output_error("domain cannot be empty", "validation_error")
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        with cli_mutation("company", "create", domain=domain):
            created = create_company(connection, name=name, domain=domain)
            if created is None:
                output_error(
                    f"company with domain={domain!r} already exists",
                    "duplicate_key",
                )
            changed = ["name", "domain"]
            if note:
                add_company_note(connection, created.id, note)
                changed.append("note")
            operator_event(
                "company.create",
                entity_id=created.id,
                domain=created.domain,
                changed=changed,
            )
            output_entity("company", created)
    finally:
        connection.close()


@company.command("update")
@click.argument("company_ref")
@click.option("--name", default=None, help="Company name.")
@click.option(
    "--profile-json",
    default=None,
    help="JSON object validated against CompanyProfile.",
)
def company_update(
    company_ref: str, name: str | None, profile_json: str | None
) -> None:
    """Update a company (addressed by domain or ID)."""
    import json

    from mailpilot.database import initialize_database, update_company
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        before = _resolve_company(connection, company_ref)
        company_id = before.id
        fields: dict[str, object] = {}
        if name is not None:
            fields["name"] = name
        if profile_json is not None:
            try:
                fields["profile"] = json.loads(profile_json)
            except json.JSONDecodeError as exc:
                output_error(f"invalid JSON: {exc}", "validation_error")
        with cli_mutation("company", "update", entity_id=company_id):
            updated = update_company(connection, company_id, **fields)
            if updated is None:
                output_error(f"company not found: {company_id}", "not_found")
            changed = [
                field
                for field in ("name", "profile")
                if getattr(before, field) != getattr(updated, field)
            ]
            operator_event(
                "company.update",
                entity_id=company_id,
                changed=changed,
            )
            output_entity("company", updated)
    finally:
        connection.close()


@company.command("disable")
@click.argument("company_ref")
@click.option(
    "--reason",
    required=True,
    help="Explanation written to disabled_reason.",
)
def company_disable(company_ref: str, reason: str) -> None:
    """Soft-disable a company by writing disabled_reason.

    A disabled company is hidden from `company list` unless `--include-disabled`
    is passed. Disable is reversible -- re-enable with `company enable`.
    Disabling an already-disabled company is rejected.
    """
    from mailpilot.database import disable_company, initialize_database
    from mailpilot.operator_log import cli_mutation, operator_event

    if reason.strip() == "":
        output_error("reason cannot be empty", "validation_error")
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        before = _resolve_company(connection, company_ref)
        company_id = before.id
        if before.disabled_reason is not None:
            output_error(
                f"company {company_id} is already disabled "
                f"(reason: {before.disabled_reason})",
                "validation_error",
            )
        with cli_mutation("company", "disable", entity_id=company_id):
            updated = disable_company(connection, company_id, reason)
            if updated is None:
                output_error(
                    f"company {company_id} is already disabled",
                    "validation_error",
                )
            operator_event(
                "company.disable",
                entity_id=company_id,
                changed=["disabled_reason"],
            )
            output_entity("company", updated)
    finally:
        connection.close()


@company.command("enable")
@click.argument("company_ref")
def company_enable(company_ref: str) -> None:
    """Re-enable a soft-disabled company by clearing disabled_reason.

    The company reappears in the default `company list`. Enabling a company
    that is not disabled is rejected.
    """
    from mailpilot.database import enable_company, initialize_database
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        before = _resolve_company(connection, company_ref)
        company_id = before.id
        if before.disabled_reason is None:
            output_error(
                f"company {company_id} is not disabled",
                "validation_error",
            )
        with cli_mutation("company", "enable", entity_id=company_id):
            updated = enable_company(connection, company_id)
            if updated is None:
                output_error(
                    f"company {company_id} is not disabled",
                    "validation_error",
                )
            operator_event(
                "company.enable",
                entity_id=company_id,
                changed=["disabled_reason"],
            )
            output_entity("company", updated)
    finally:
        connection.close()


@company.command("search")
@click.argument("query")
@click.option("--limit", default=100, help="Maximum results.")
def company_search(query: str, limit: int) -> None:
    """Search companies by name or domain."""
    from mailpilot.database import initialize_database, search_companies

    connection = initialize_database(_database_url())
    try:
        companies = search_companies(connection, query, limit=limit)
        output({"companies": [c.model_dump(mode="json") for c in companies]})
    finally:
        connection.close()


@company.command("list")
@presence_option("profile", "Filter on presence of a company profile.")
@range_options(
    "contacts",
    "Return only companies with contact_count >= N (composes with --max).",
    "Return only companies with contact_count <= N (inclusive).",
)
@tag_filter_options
@include_disabled_option
@time_window_options("created_at")
@limit_option
def company_list(
    limit: int,
    since: str | None,
    until: str | None,
    has_profile: bool | None,
    max_contacts: int | None,
    min_contacts: int | None,
    include_disabled: bool,
    tag: str | None,
    no_tag: tuple[str, ...],
) -> None:
    """List companies as summaries."""
    from mailpilot.database import initialize_database, list_companies

    connection = initialize_database(_database_url())
    try:
        tag_id = _resolve_tag(connection, tag).id if tag is not None else None
        exclude_tag_ids = [_resolve_tag(connection, name).id for name in no_tag]
        companies = list_companies(
            connection,
            limit=limit,
            since=since,
            until=until,
            has_profile=has_profile,
            max_contacts=max_contacts,
            min_contacts=min_contacts,
            include_disabled=include_disabled,
            tag=tag_id,
            exclude_tags=exclude_tag_ids,
        )
        output({"companies": [c.model_dump(mode="json") for c in companies]})
    finally:
        connection.close()


@company.command("view")
@click.argument("company_ref")
def company_view(company_ref: str) -> None:
    """Show a company by domain or ID with inlined notes."""
    from mailpilot.database import initialize_database, load_company_view

    connection = initialize_database(_database_url())
    try:
        company_id = _resolve_company_id(connection, company_ref)
        found = load_company_view(connection, company_id)
        if found is None:
            output_error(f"company not found: {company_ref}", "not_found")
        output_entity("company", found)
    finally:
        connection.close()


@company.command("export")
@click.option(
    "--file",
    "file",
    default=None,
    type=click.Path(dir_okay=False),
    help="Optional path to also write the JSON array. Stdout still emits envelope.",
)
def company_export(file: str | None) -> None:
    """Export all companies as a declarative JSON payload."""
    import pathlib

    from mailpilot.database import get_company, initialize_database, list_companies

    connection = initialize_database(_database_url())
    try:
        summaries = list_companies(connection, limit=100_000, include_disabled=True)
        full = [get_company(connection, s.id) for s in summaries]
        data = [c.model_dump(mode="json") for c in full if c is not None]
        if file is not None:
            pathlib.Path(file).write_text(json.dumps(data, indent=2))
        output({"companies": data})
    finally:
        connection.close()


@company.command("import")
@click.option(
    "--file",
    "file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to JSON array of company objects. If omitted, read from stdin.",
)
def company_import(file: str | None) -> None:
    """Import companies from a declarative JSON array.

    Each row resolves to either ``{"name": ..., "action": "created"}`` or
    ``{"name": ..., "error": CODE, "message": ...}``; per-row failures do not
    abort the batch. Existing ``domain`` (UNIQUE) yields ``error="duplicate"``.
    """
    import pathlib
    import sys

    from mailpilot.database import (
        create_company,
        initialize_database,
        list_companies,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    if file:
        raw = pathlib.Path(file).read_text()
    else:
        if sys.stdin.isatty():
            output_error(
                "no input: provide --file PATH or pipe JSON via stdin",
                "validation_error",
            )
        raw = sys.stdin.read()
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        output_error(f"malformed JSON: {exc}", "validation_error")
    if not isinstance(entries, list):
        output_error(
            "payload must be a JSON array of company objects", "validation_error"
        )

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        with cli_mutation("company", "import", row_count=len(entries)):
            existing = {c.domain for c in list_companies(connection, limit=100_000)}
            results: list[dict[str, object]] = []
            for entry in entries:
                name = entry.get("name") if isinstance(entry, dict) else None
                domain = entry.get("domain") if isinstance(entry, dict) else None
                if not isinstance(name, str) or not isinstance(domain, str):
                    results.append(
                        {
                            "name": name if isinstance(name, str) else "",
                            "error": "validation_error",
                            "message": "row missing required 'name' or 'domain'",
                        }
                    )
                    operator_event(
                        "company.import",
                        name=name if isinstance(name, str) else "",
                        changed=[],
                    )
                    continue
                if domain in existing:
                    results.append(
                        {
                            "name": name,
                            "error": "duplicate",
                            "message": (
                                f"company with domain {domain!r} already exists"
                            ),
                        }
                    )
                    operator_event("company.import", name=name, changed=[])
                    continue
                created = create_company(connection, name=name, domain=domain)
                if created is None:
                    # Pre-fetch ``existing`` set is an optimization, not
                    # source-of-truth per §V.16(+); race lost -> duplicate row.
                    results.append(
                        {
                            "name": name,
                            "error": "duplicate",
                            "message": (
                                f"company with domain {domain!r} already exists"
                            ),
                        }
                    )
                    operator_event("company.import", name=name, changed=[])
                    continue
                existing.add(domain)
                results.append({"name": name, "action": "created"})
                operator_event(
                    "company.import",
                    name=name,
                    domain=domain,
                    changed=["name", "domain"],
                )
            output({"companies": results})
    finally:
        connection.close()


# -- Contact commands ----------------------------------------------------------


@main.group()
def contact() -> None:
    """Manage contacts."""


@contact.command("create")
@click.option("--email", required=True, help="Email address.")
@click.option("--first-name", default=None, help="First name.")
@click.option("--last-name", default=None, help="Last name.")
@click.option("--company-domain", default=None, help="Owning company (domain or ID).")
@click.option("--title", default=None, help="Role label (lead-metadata).")
@click.option(
    "--email-confidence",
    type=int,
    default=None,
    help="Deliverability score 0-100; low = high risk (lead-metadata).",
)
@click.option(
    "--note",
    default=None,
    help="Optional first note body. Appended atomically as a `note` row.",
)
def contact_create(
    email: str,
    first_name: str | None,
    last_name: str | None,
    company_domain: str | None,
    title: str | None,
    email_confidence: int | None,
    note: str | None,
) -> None:
    """Create a new contact."""
    from mailpilot.database import (
        add_contact_note,
        create_contact,
        initialize_database,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        company_id = (
            _resolve_company(connection, company_domain).id
            if company_domain is not None
            else None
        )
        with cli_mutation("contact", "create", email=email, company_id=company_id):
            created = create_contact(
                connection,
                email=email,
                first_name=first_name,
                last_name=last_name,
                company_id=company_id,
                title=title,
                email_confidence=email_confidence,
            )
            if created is None:
                output_error(
                    f"contact with email={email!r} already exists",
                    "duplicate_key",
                )
            changed = ["email", "first_name", "last_name", "company_id"]
            if title is not None:
                changed.append("title")
            if email_confidence is not None:
                changed.append("email_confidence")
            if note:
                add_contact_note(connection, created.id, note)
                changed.append("note")
            operator_event(
                "contact.create",
                entity_id=created.id,
                email=created.email,
                company_id=company_id,
                changed=changed,
            )
            output_entity("contact", created)
    finally:
        connection.close()


@contact.command("update")
@click.argument("contact_ref")
@click.option("--email", default=None, help="Email address.")
@click.option("--first-name", default=None, help="First name.")
@click.option("--last-name", default=None, help="Last name.")
@click.option("--company-domain", default=None, help="Owning company (domain or ID).")
@click.option("--title", default=None, help="Role label (lead-metadata).")
@click.option(
    "--email-confidence",
    type=int,
    default=None,
    help="Deliverability score 0-100; low = high risk (lead-metadata).",
)
def contact_update(
    contact_ref: str,
    email: str | None,
    first_name: str | None,
    last_name: str | None,
    company_domain: str | None,
    title: str | None,
    email_confidence: int | None,
) -> None:
    """Update a contact (addressed by email or ID)."""
    from mailpilot.database import initialize_database, update_contact
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        before = _resolve_contact(connection, contact_ref)
        contact_id = before.id
        fields: dict[str, object] = {}
        if email is not None:
            fields["email"] = email
        if first_name is not None:
            fields["first_name"] = first_name
        if last_name is not None:
            fields["last_name"] = last_name
        if company_domain is not None:
            fields["company_id"] = _resolve_company(connection, company_domain).id
        if title is not None:
            fields["title"] = title
        if email_confidence is not None:
            fields["email_confidence"] = email_confidence
        with cli_mutation("contact", "update", entity_id=contact_id):
            updated = update_contact(connection, contact_id, **fields)
            if updated is None:
                output_error(f"contact not found: {contact_id}", "not_found")
            changed = [
                field
                for field in (
                    "email",
                    "first_name",
                    "last_name",
                    "company_id",
                    "title",
                    "email_confidence",
                )
                if getattr(before, field) != getattr(updated, field)
            ]
            operator_event(
                "contact.update",
                entity_id=contact_id,
                changed=changed,
            )
            output_entity("contact", updated)
    finally:
        connection.close()


@contact.command("disable")
@click.argument("contact_ref")
@click.option(
    "--reason",
    required=True,
    help="Explanation written to disabled_reason.",
)
def contact_disable(contact_ref: str, reason: str) -> None:
    """Soft-disable a contact by writing disabled_reason (addressed by email or ID)."""
    from mailpilot.database import disable_contact, initialize_database
    from mailpilot.operator_log import cli_mutation, operator_event

    if reason.strip() == "":
        output_error("reason cannot be empty", "validation_error")
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        before = _resolve_contact(connection, contact_ref)
        contact_id = before.id
        with cli_mutation("contact", "disable", entity_id=contact_id):
            updated = disable_contact(connection, contact_id, reason)
            if updated is None:
                output_error(f"contact not found: {contact_id}", "not_found")
            changed = (
                ["disabled_reason"]
                if before.disabled_reason != updated.disabled_reason
                else []
            )
            operator_event(
                "contact.disable",
                entity_id=contact_id,
                changed=changed,
            )
            output_entity("contact", updated)
    finally:
        connection.close()


@contact.command("enable")
@click.argument("contact_ref")
def contact_enable(contact_ref: str) -> None:
    """Re-enable a disabled contact by clearing disabled_reason.

    Clears any reason, including a `bounced:` or `unsubscribed:` block -- the
    operator owns consent. Enabling a contact that is not disabled is rejected.
    Addressed by email or ID.
    """
    from mailpilot.database import enable_contact, initialize_database
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        before = _resolve_contact(connection, contact_ref)
        contact_id = before.id
        if before.disabled_reason is None:
            output_error(
                f"contact {contact_id} is not disabled",
                "validation_error",
            )
        with cli_mutation("contact", "enable", entity_id=contact_id):
            updated = enable_contact(connection, contact_id)
            if updated is None:
                output_error(
                    f"contact {contact_id} is not disabled",
                    "validation_error",
                )
            operator_event(
                "contact.enable",
                entity_id=contact_id,
                changed=["disabled_reason"],
            )
            output_entity("contact", updated)
    finally:
        connection.close()


@contact.command("search")
@click.argument("query")
@click.option("--limit", default=100, help="Maximum results.")
def contact_search(query: str, limit: int) -> None:
    """Search contacts by email, name, or domain."""
    from mailpilot.database import initialize_database, search_contacts

    connection = initialize_database(_database_url())
    try:
        contacts = search_contacts(connection, query, limit=limit)
        output({"contacts": [c.model_dump(mode="json") for c in contacts]})
    finally:
        connection.close()


@contact.command("list")
@scope_option(
    "--company-domain",
    "company_domain",
    "Filter by owning company (domain or ID).",
)
@range_options(
    "email-confidence",
    "Surface only rows with email_confidence >= N (composes with --max).",
    "Surface only rows with email_confidence <= N (low-score lead review).",
)
@click.option(
    "--title",
    default=None,
    help="Filter by title (case-insensitive exact match; use search for substring).",
)
@tag_filter_options
@include_disabled_option
@time_window_options("created_at")
@limit_option
def contact_list(
    limit: int,
    company_domain: str | None,
    since: str | None,
    until: str | None,
    include_disabled: bool,
    max_email_confidence: int | None,
    min_email_confidence: int | None,
    title: str | None,
    tag: str | None,
    no_tag: tuple[str, ...],
) -> None:
    """List contacts as summaries."""
    from mailpilot.database import initialize_database, list_contacts

    connection = initialize_database(_database_url())
    try:
        company_id = (
            _resolve_company(connection, company_domain).id
            if company_domain is not None
            else None
        )
        tag_id = _resolve_tag(connection, tag).id if tag is not None else None
        exclude_tag_ids = [_resolve_tag(connection, name).id for name in no_tag]
        contacts = list_contacts(
            connection,
            limit=limit,
            company_id=company_id,
            since=since,
            until=until,
            include_disabled=include_disabled,
            max_email_confidence=max_email_confidence,
            min_email_confidence=min_email_confidence,
            title=title,
            tag=tag_id,
            exclude_tags=exclude_tag_ids,
        )
        output({"contacts": [c.model_dump(mode="json") for c in contacts]})
    finally:
        connection.close()


@contact.command("view")
@click.argument("contact_ref")
def contact_view(contact_ref: str) -> None:
    """Show a contact by email or ID with inlined notes (own + parent company)."""
    from mailpilot.database import initialize_database, load_contact_view

    connection = initialize_database(_database_url())
    try:
        contact_id = _resolve_contact_id(connection, contact_ref)
        found = load_contact_view(connection, contact_id)
        if found is None:
            output_error(f"contact not found: {contact_ref}", "not_found")
        output_entity("contact", found)
    finally:
        connection.close()


@contact.command("export")
@click.option(
    "--file",
    "file",
    default=None,
    type=click.Path(dir_okay=False),
    help="Optional path to also write the JSON array. Stdout still emits envelope.",
)
def contact_export(file: str | None) -> None:
    """Export all contacts as a declarative JSON payload."""
    import pathlib

    from mailpilot.database import get_contact, initialize_database, list_contacts

    connection = initialize_database(_database_url())
    try:
        summaries = list_contacts(connection, limit=100_000)
        full = [get_contact(connection, s.id) for s in summaries]
        data = [c.model_dump(mode="json") for c in full if c is not None]
        if file is not None:
            pathlib.Path(file).write_text(json.dumps(data, indent=2))
        output({"contacts": data})
    finally:
        connection.close()


@contact.command("import")
@click.option(
    "--file",
    "file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to JSON array of contact objects. If omitted, read from stdin.",
)
def contact_import(file: str | None) -> None:
    """Import contacts from a declarative JSON array.

    Each row resolves to either ``{"email": ..., "action": "created"}`` or
    ``{"email": ..., "error": CODE, "message": ...}``; per-row failures do not
    abort the batch. Existing ``email`` (UNIQUE) yields ``error="duplicate"``.
    """
    import pathlib
    import sys

    from mailpilot.database import (
        create_contact,
        initialize_database,
        list_contacts,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    if file:
        raw = pathlib.Path(file).read_text()
    else:
        if sys.stdin.isatty():
            output_error(
                "no input: provide --file PATH or pipe JSON via stdin",
                "validation_error",
            )
        raw = sys.stdin.read()
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        output_error(f"malformed JSON: {exc}", "validation_error")
    if not isinstance(entries, list):
        output_error(
            "payload must be a JSON array of contact objects", "validation_error"
        )

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        with cli_mutation("contact", "import", row_count=len(entries)):
            existing = {c.email for c in list_contacts(connection, limit=100_000)}
            results: list[dict[str, object]] = []
            for entry in entries:
                email = entry.get("email") if isinstance(entry, dict) else None
                if not isinstance(email, str):
                    results.append(
                        {
                            "email": "",
                            "error": "validation_error",
                            "message": "row missing required 'email'",
                        }
                    )
                    operator_event("contact.import", email="", changed=[])
                    continue
                if email in existing:
                    results.append(
                        {
                            "email": email,
                            "error": "duplicate",
                            "message": (f"contact with email {email!r} already exists"),
                        }
                    )
                    operator_event("contact.import", email=email, changed=[])
                    continue
                created = create_contact(
                    connection,
                    email=email,
                    first_name=entry.get("first_name"),
                    last_name=entry.get("last_name"),
                    company_id=entry.get("company_id"),
                )
                if created is None:
                    # Pre-fetch ``existing`` set is an optimization, not
                    # source-of-truth per §V.16(+); race lost -> duplicate row.
                    results.append(
                        {
                            "email": email,
                            "error": "duplicate",
                            "message": (f"contact with email {email!r} already exists"),
                        }
                    )
                    operator_event("contact.import", email=email, changed=[])
                    continue
                existing.add(email)
                results.append({"email": email, "action": "created"})
                operator_event(
                    "contact.import",
                    email=email,
                    changed=[
                        "email",
                        "first_name",
                        "last_name",
                        "company_id",
                    ],
                )
            output({"contacts": results})
    finally:
        connection.close()


# -- Email commands ------------------------------------------------------------


@main.group()
def email() -> None:
    """Manage emails."""


@email.command("search")
@click.argument("query")
@click.option("--limit", default=100, help="Maximum number of results.")
def email_search(query: str, limit: int) -> None:
    """Search emails by subject or body."""
    from mailpilot.database import initialize_database, search_emails

    connection = initialize_database(_database_url())
    try:
        emails = search_emails(connection, query, limit=limit)
        output({"emails": [e.model_dump(mode="json") for e in emails]})
    finally:
        connection.close()


@email.command("list")
@scope_option("--contact-email", "contact_email", "Filter by contact (email or ID).")
@scope_option("--account-email", "account_email", "Filter by account (email or ID).")
@scope_option("--workflow-id", "workflow_id", "Filter by workflow ID.")
@scope_option("--thread-id", "thread_id", "Filter by Gmail thread ID.")
@enum_option("--direction", "direction", DIRECTIONS, "Filter by direction.")
@enum_option("--status", "status", _EMAIL_STATUSES, "Filter by email status.")
@enum_option(
    "--route-method",
    "route_method",
    _ROUTE_METHODS,
    "Filter by persisted routing decision.",
)
@click.option("--from", "sender", default=None, help="Filter by sender email address.")
@click.option(
    "--to", "recipient", default=None, help="Filter by recipient email address."
)
@time_window_options("COALESCE(sent_at, received_at)")
@limit_option
def email_list(
    limit: int,
    contact_email: str | None,
    account_email: str | None,
    since: str | None,
    until: str | None,
    thread_id: str | None,
    direction: str | None,
    workflow_id: str | None,
    status: str | None,
    sender: str | None,
    recipient: str | None,
    route_method: str | None,
) -> None:
    """List emails with optional filters."""
    from mailpilot.database import (
        get_workflow,
        initialize_database,
        list_emails,
    )

    connection = initialize_database(_database_url())
    try:
        contact_id = (
            _resolve_contact(connection, contact_email).id
            if contact_email is not None
            else None
        )
        account_id = (
            _resolve_account(connection, account_email).id
            if account_email is not None
            else None
        )
        if workflow_id is not None and get_workflow(connection, workflow_id) is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        emails = list_emails(
            connection,
            limit=limit,
            contact_id=contact_id,
            account_id=account_id,
            since=since,
            until=until,
            thread_id=thread_id,
            direction=direction,
            workflow_id=workflow_id,
            status=status,
            sender=sender,
            recipient=recipient,
            route_method=route_method,
        )
        output({"emails": [e.model_dump(mode="json") for e in emails]})
    finally:
        connection.close()


@email.command("view")
@click.argument("email_id")
def email_view(email_id: str) -> None:
    """View a single email by ID."""
    from mailpilot.database import get_email, initialize_database

    connection = initialize_database(_database_url())
    try:
        found = get_email(connection, email_id)
        if found is None:
            output_error(f"email not found: {email_id}", "not_found")
        output_entity("email", found)
    finally:
        connection.close()


@email.command("send")
@click.option(
    "--account-email",
    default=None,
    help="Sending account (email or ID).",
)
@click.option(
    "--to",
    "to",
    required=True,
    multiple=True,
    help="Recipient email address (repeatable).",
)
@click.option("--subject", required=True, help="Email subject.")
@click.option("--body", required=True, help="Plain text body.")
@click.option("--workflow-id", default=None, help="Link to a workflow.")
@click.option("--cc", default=None, help="CC recipient(s), comma-separated.")
@click.option("--bcc", default=None, help="BCC recipient(s), comma-separated.")
def email_send(
    account_email: str | None,
    to: tuple[str, ...],
    subject: str,
    body: str,
    workflow_id: str | None,
    cc: str | None,
    bcc: str | None,
) -> None:
    """Send a new outbound email via the given account's Gmail mailbox.

    Use ``email reply`` to continue an existing thread.
    """
    import logfire

    from mailpilot import email_ops
    from mailpilot.database import get_workflow, initialize_database
    from mailpilot.gmail import GmailClient
    from mailpilot.settings import get_settings

    if not subject.strip():
        output_error("subject cannot be empty", "validation_error")
    if not body.strip():
        output_error("body cannot be empty", "validation_error")

    to_joined = ",".join(to)
    settings = get_settings()
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        account = _resolve_account(connection, account_email)
        if workflow_id is not None and get_workflow(connection, workflow_id) is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        client = GmailClient(account.email)
        try:
            sent = email_ops.send_email(
                connection,
                account=account,
                gmail_client=client,
                settings=settings,
                to=to_joined,
                subject=subject,
                body=body,
                workflow_id=workflow_id,
                cc=cc,
                bcc=bcc,
            )
        except email_ops.EmailOpsError as exc:
            output_error(str(exc), exc.code)
        except Exception as exc:
            logfire.exception(
                "cli.email.send.failed",
                account_id=account.id,
                to=to,
            )
            output_error(str(exc), "send_failed")
        output_entity("email", sent)
    finally:
        connection.close()


@email.command("reply")
@click.option(
    "--account-email",
    default=None,
    help="Sending account (email or ID).",
)
@click.option(
    "--email-id",
    required=True,
    help="ID of the email being replied to.",
)
@click.option("--body", required=True, help="Reply body (plain text).")
@click.option("--workflow-id", default=None, help="Link to a workflow.")
@click.option("--cc", default=None, help="CC recipient(s), comma-separated.")
@click.option("--bcc", default=None, help="BCC recipient(s), comma-separated.")
def email_reply(
    account_email: str | None,
    email_id: str,
    body: str,
    workflow_id: str | None,
    cc: str | None,
    bcc: str | None,
) -> None:
    """Reply to an existing email in-thread.

    Auto-derives recipient, subject (with "Re: " prefix), thread, and
    In-Reply-To from the original. No cooldown applied.
    """
    import logfire

    from mailpilot import email_ops
    from mailpilot.database import get_workflow, initialize_database
    from mailpilot.gmail import GmailClient
    from mailpilot.settings import get_settings

    if not body.strip():
        output_error("body cannot be empty", "validation_error")

    settings = get_settings()
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        account = _resolve_account(connection, account_email)
        if workflow_id is not None and get_workflow(connection, workflow_id) is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        client = GmailClient(account.email)
        try:
            sent = email_ops.reply_email(
                connection,
                account=account,
                gmail_client=client,
                settings=settings,
                email_id=email_id,
                body=body,
                workflow_id=workflow_id,
                cc=cc,
                bcc=bcc,
            )
        except email_ops.EmailOpsError as exc:
            output_error(str(exc), exc.code)
        except Exception as exc:
            logfire.exception(
                "cli.email.reply.failed",
                account_id=account.id,
                email_id=email_id,
            )
            output_error(str(exc), "send_failed")
        output_entity("email", sent)
    finally:
        connection.close()


# -- Activity commands ---------------------------------------------------------


@main.group()
def activity() -> None:
    """Manage activity timeline events."""


@activity.command("add")
@click.option("--contact-email", default=None, help="Owner contact (email or ID).")
@click.option("--company-domain", default=None, help="Owner company (domain or ID).")
@click.option(
    "--type",
    "activity_type",
    required=True,
    type=click.Choice(_ACTIVITY_TYPES),
    help="Activity type.",
)
@click.option("--summary", required=True, help="One-line description.")
@click.option("--detail", default=None, help="JSON detail payload.")
def activity_add(
    contact_email: str | None,
    company_domain: str | None,
    activity_type: str,
    summary: str,
    detail: str | None,
) -> None:
    """Attach an activity event to a contact or company.

    At least one of --contact-email / --company-domain is required.
    """
    from mailpilot.database import (
        create_activity,
        initialize_database,
    )

    if not summary.strip():
        output_error("summary cannot be empty", "validation_error")
    if contact_email is None and company_domain is None:
        output_error(
            "at least one of --contact-email or --company-domain is required",
            "validation_error",
        )
    detail_dict: dict[str, object] = json.loads(detail) if detail else {}
    connection = initialize_database(_database_url())
    try:
        contact_id = (
            _resolve_contact(connection, contact_email).id
            if contact_email is not None
            else None
        )
        company_id = (
            _resolve_company(connection, company_domain).id
            if company_domain is not None
            else None
        )
        created = create_activity(
            connection,
            contact_id=contact_id,
            company_id=company_id,
            activity_type=activity_type,
            summary=summary,
            detail=detail_dict,
        )
        output_entity("activity", created)
    finally:
        connection.close()


@activity.command("list")
@scope_option("--contact-email", "contact_email", "Filter by contact (email or ID).")
@scope_option("--company-domain", "company_domain", "Filter by company (domain or ID).")
@enum_option("--type", "activity_type", _ACTIVITY_TYPES, "Filter by activity type.")
@time_window_options("created_at")
@limit_option
def activity_list(
    contact_email: str | None,
    company_domain: str | None,
    activity_type: str | None,
    limit: int,
    since: str | None,
    until: str | None,
) -> None:
    """List activities (requires --contact-email or --company-domain)."""
    from mailpilot.database import (
        initialize_database,
        list_activities,
    )

    if contact_email is None and company_domain is None:
        output_error(
            "at least one of --contact-email or --company-domain is required",
            "missing_filter",
        )
    connection = initialize_database(_database_url())
    try:
        contact_id = (
            _resolve_contact(connection, contact_email).id
            if contact_email is not None
            else None
        )
        company_id = (
            _resolve_company(connection, company_domain).id
            if company_domain is not None
            else None
        )
        activities = list_activities(
            connection,
            contact_id=contact_id,
            company_id=company_id,
            activity_type=activity_type,
            limit=limit,
            since=since,
            until=until,
        )
        output({"activities": [a.model_dump(mode="json") for a in activities]})
    finally:
        connection.close()


# -- Tag commands --------------------------------------------------------------


@main.group()
def tag() -> None:
    """Manage the controlled tag vocabulary and its assignments."""


@tag.command("create")
@click.argument("name")
def tag_create(name: str) -> None:
    """Define a tag in the controlled vocabulary.

    Creates a vocabulary entry; linking owners to it is the `tag add` verb. A
    name already defined is rejected (names are globally unique).
    """
    from mailpilot.database import (
        _normalize_tag_name,  # pyright: ignore[reportPrivateUsage]
        create_tag,
        initialize_database,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    if not name.strip():
        output_error("tag name cannot be empty", "validation_error")
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        with cli_mutation("tag", "create", name=name):
            try:
                created = create_tag(connection, name=name)
            except ValueError as exc:
                output_error(str(exc), "validation_error")
            if created is None:
                normalized = _normalize_tag_name(name)
                output_error(f"tag '{normalized}' already exists", "already_exists")
            operator_event("tag.create", name=created.name, changed=["name"])
            output_entity("tag", created)
    finally:
        connection.close()


@tag.command("view")
@click.argument("name")
def tag_view(name: str) -> None:
    """Show a vocabulary tag by name with its usage_count."""
    from mailpilot.database import get_tag_summary_by_name, initialize_database

    connection = initialize_database(_database_url())
    try:
        try:
            found = get_tag_summary_by_name(connection, name)
        except ValueError:
            found = None
        if found is None:
            output_error(f"tag not found: {name}", "not_found")
        output_entity("tag", found)
    finally:
        connection.close()


@tag.command("disable")
@click.argument("name")
@click.option(
    "--reason",
    required=True,
    help="Explanation written to disabled_reason.",
)
def tag_disable(name: str, reason: str) -> None:
    """Soft-retire a tag in the controlled vocabulary.

    Flips disabled_reason on the vocabulary row, so the tag drops out of the
    default `tag list` but stays linked to its owners. Re-enable by clearing
    disabled_reason. Disabling an already-disabled tag is rejected.
    """
    from mailpilot.database import disable_tag, initialize_database
    from mailpilot.operator_log import cli_mutation, operator_event

    if reason.strip() == "":
        output_error("reason cannot be empty", "validation_error")
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        before = _resolve_tag(connection, name)
        if before.disabled_reason is not None:
            output_error(
                f"tag '{before.name}' is already disabled "
                f"(reason: {before.disabled_reason})",
                "validation_error",
            )
        with cli_mutation("tag", "disable", entity_id=before.name):
            updated = disable_tag(connection, name=before.name, reason=reason)
            if updated is None:
                output_error(
                    f"tag '{before.name}' is already disabled",
                    "validation_error",
                )
            operator_event(
                "tag.disable",
                entity_id=updated.name,
                changed=["disabled_reason"],
            )
            output_entity("tag", updated)
    finally:
        connection.close()


@tag.command("enable")
@click.argument("name")
def tag_enable(name: str) -> None:
    """Re-enable a retired tag by clearing disabled_reason.

    The tag reappears in the default `tag list`. Enabling a tag that is not
    disabled is rejected.
    """
    from mailpilot.database import enable_tag, initialize_database
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        before = _resolve_tag(connection, name)
        if before.disabled_reason is None:
            output_error(
                f"tag '{before.name}' is not disabled",
                "validation_error",
            )
        with cli_mutation("tag", "enable", entity_id=before.name):
            updated = enable_tag(connection, name=before.name)
            if updated is None:
                output_error(
                    f"tag '{before.name}' is not disabled",
                    "validation_error",
                )
            operator_event(
                "tag.enable",
                entity_id=updated.name,
                changed=["disabled_reason"],
            )
            output_entity("tag", updated)
    finally:
        connection.close()


@tag.command("add")
@click.option(
    "--tag", "tag_name", required=True, help="Defined tag to link (name or ID)."
)
@click.option("--contact-email", default=None, help="Owner contact (email or ID).")
@click.option("--company-domain", default=None, help="Owner company (domain or ID).")
def tag_add(
    tag_name: str, contact_email: str | None, company_domain: str | None
) -> None:
    """Link a defined tag to a contact or company.

    Errors `not_found` when the tag is undefined -- `tag add` never creates the
    tag as a side effect (define vocabulary with `tag create`).
    """
    from mailpilot.database import (
        assign_tag_to_company,
        assign_tag_to_contact,
        initialize_database,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    if (contact_email is None) == (company_domain is None):
        output_error(
            "exactly one of --contact-email or --company-domain is required",
            "validation_error",
        )
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        tag_row = _resolve_tag(connection, tag_name)
        if contact_email is not None:
            owner = ("contact", _resolve_contact(connection, contact_email).id)
        else:
            assert company_domain is not None
            owner = ("company", _resolve_company(connection, company_domain).id)
        with cli_mutation(
            "tag", "add", name=tag_row.name, owner_type=owner[0], owner_id=owner[1]
        ):
            if owner[0] == "contact":
                created = assign_tag_to_contact(
                    connection, tag_id=tag_row.id, contact_id=owner[1]
                )
            else:
                created = assign_tag_to_company(
                    connection, tag_id=tag_row.id, company_id=owner[1]
                )
            if created is None:
                output_error(
                    f"tag '{tag_row.name}' already on {owner[0]} {owner[1]}",
                    "already_exists",
                )
            operator_event(
                "tag.add",
                name=tag_row.name,
                owner_type=owner[0],
                owner_id=owner[1],
                changed=["tag_id"],
            )
            output_entity("tag_assignment", created)
    finally:
        connection.close()


@tag.command("remove")
@click.option(
    "--tag", "tag_name", required=True, help="Defined tag to unlink (name or ID)."
)
@click.option("--contact-email", default=None, help="Owner contact (email or ID).")
@click.option("--company-domain", default=None, help="Owner company (domain or ID).")
def tag_remove(
    tag_name: str, contact_email: str | None, company_domain: str | None
) -> None:
    """Unlink a tag from a contact or company (inverse of `tag add`).

    Removes only the link; the tag vocabulary entry and the owner both survive.
    """
    from mailpilot.database import (
        initialize_database,
        remove_tag_from_company,
        remove_tag_from_contact,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    if (contact_email is None) == (company_domain is None):
        output_error(
            "exactly one of --contact-email or --company-domain is required",
            "validation_error",
        )
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        tag_row = _resolve_tag(connection, tag_name)
        if contact_email is not None:
            owner = ("contact", _resolve_contact(connection, contact_email).id)
        else:
            assert company_domain is not None
            owner = ("company", _resolve_company(connection, company_domain).id)
        with cli_mutation(
            "tag", "remove", name=tag_row.name, owner_type=owner[0], owner_id=owner[1]
        ):
            if owner[0] == "contact":
                removed = remove_tag_from_contact(
                    connection, tag_id=tag_row.id, contact_id=owner[1]
                )
            else:
                removed = remove_tag_from_company(
                    connection, tag_id=tag_row.id, company_id=owner[1]
                )
            if removed is None:
                output_error(
                    f"tag '{tag_row.name}' not on {owner[0]} {owner[1]}",
                    "not_found",
                )
            operator_event(
                "tag.remove",
                name=tag_row.name,
                owner_type=owner[0],
                owner_id=owner[1],
                changed=["tag_id"],
            )
            output_entity("tag_assignment", removed)
    finally:
        connection.close()


@tag.command("list")
@scope_option(
    "--contact-email", "contact_email", "List one contact's tags (email or ID)."
)
@scope_option(
    "--company-domain", "company_domain", "List one company's tags (domain or ID)."
)
@include_disabled_option
@time_window_options("created_at")
@limit_option
def tag_list(
    contact_email: str | None,
    company_domain: str | None,
    limit: int,
    since: str | None,
    until: str | None,
    include_disabled: bool,
) -> None:
    """List the tag vocabulary with usage_count, or one owner's tags.

    With no owner option, lists the whole vocabulary (each row carrying its
    global usage_count). With --contact-email or --company-domain, lists the
    tags assigned to that owner.
    """
    from mailpilot.database import initialize_database, list_tags

    if contact_email is not None and company_domain is not None:
        output_error(
            "pass at most one of --contact-email or --company-domain",
            "validation_error",
        )
    connection = initialize_database(_database_url())
    try:
        contact_id = (
            _resolve_contact(connection, contact_email).id
            if contact_email is not None
            else None
        )
        company_id = (
            _resolve_company(connection, company_domain).id
            if company_domain is not None
            else None
        )
        tags = list_tags(
            connection,
            contact_id=contact_id,
            company_id=company_id,
            limit=limit,
            since=since,
            until=until,
            include_disabled=include_disabled,
        )
        output({"tags": [t.model_dump(mode="json") for t in tags]})
    finally:
        connection.close()


@tag.command("search")
@click.argument("name")
@include_disabled_option
@limit_option
def tag_search(name: str, limit: int, include_disabled: bool) -> None:
    """Search the tag vocabulary by name substring."""
    from mailpilot.database import initialize_database, search_tags

    connection = initialize_database(_database_url())
    try:
        tags = search_tags(
            connection,
            name=name,
            limit=limit,
            include_disabled=include_disabled,
        )
        output({"tags": [t.model_dump(mode="json") for t in tags]})
    finally:
        connection.close()


# -- Note commands -------------------------------------------------------------


@main.group()
def note() -> None:
    """Manage notes on contacts and companies."""


@note.command("add")
@click.option("--contact-email", default=None, help="Owner contact (email or ID).")
@click.option("--company-domain", default=None, help="Owner company (domain or ID).")
@click.option("--body", required=True, help="Note text.")
def note_add(contact_email: str | None, company_domain: str | None, body: str) -> None:
    """Add a note to a contact or company."""
    from mailpilot.database import (
        add_company_note,
        add_contact_note,
        initialize_database,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    if not body.strip():
        output_error("note body cannot be empty", "validation_error")
    if (contact_email is None) == (company_domain is None):
        output_error(
            "exactly one of --contact-email or --company-domain is required",
            "validation_error",
        )
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        if contact_email is not None:
            owner = ("contact", _resolve_contact(connection, contact_email).id)
        else:
            assert company_domain is not None
            owner = ("company", _resolve_company(connection, company_domain).id)
        with cli_mutation("note", "add", owner_type=owner[0], owner_id=owner[1]):
            if owner[0] == "contact":
                created = add_contact_note(connection, contact_id=owner[1], body=body)
            else:
                created = add_company_note(connection, company_id=owner[1], body=body)
            operator_event(
                "note.add",
                entity_id=created.id,
                owner_type=owner[0],
                owner_id=owner[1],
                changed=["body"],
            )
            output_entity("note", created)
    finally:
        connection.close()


@note.command("list")
@scope_option("--contact-email", "contact_email", "Filter by contact (email or ID).")
@scope_option("--company-domain", "company_domain", "Filter by company (domain or ID).")
@time_window_options("created_at")
@limit_option
def note_list(
    contact_email: str | None,
    company_domain: str | None,
    limit: int,
    since: str | None,
    until: str | None,
) -> None:
    """List notes on a contact or company."""
    from mailpilot.database import (
        initialize_database,
        list_notes,
    )

    if (contact_email is None) == (company_domain is None):
        output_error(
            "exactly one of --contact-email or --company-domain is required",
            "validation_error",
        )
    connection = initialize_database(_database_url())
    try:
        if contact_email is not None:
            notes = list_notes(
                connection,
                contact_id=_resolve_contact(connection, contact_email).id,
                limit=limit,
                since=since,
                until=until,
            )
        else:
            assert company_domain is not None
            notes = list_notes(
                connection,
                company_id=_resolve_company(connection, company_domain).id,
                limit=limit,
                since=since,
                until=until,
            )
        output({"notes": [n.model_dump(mode="json") for n in notes]})
    finally:
        connection.close()


@note.command("view")
@click.argument("note_id")
def note_view(note_id: str) -> None:
    """View a note by ID."""
    from mailpilot.database import get_note, initialize_database

    connection = initialize_database(_database_url())
    try:
        found = get_note(connection, note_id)
        if found is None:
            output_error(f"note {note_id} not found", "not_found")
        output_entity("note", found)
    finally:
        connection.close()


# -- Workflow commands ---------------------------------------------------------


@main.group()
def workflow() -> None:
    """Manage workflows (inbound + outbound)."""


def _resolve_instructions(
    instructions: str | None, instructions_file: str | None
) -> str | None:
    """Return final instructions text from inline or file source."""
    import pathlib

    if instructions_file is not None:
        return pathlib.Path(instructions_file).read_text()
    return instructions


def _validate_theme(theme: str) -> None:
    """Exit with validation_error if theme is not a recognized name."""
    from mailpilot.email_renderer import THEME_NAMES

    if theme not in THEME_NAMES:
        output_error(
            f"invalid theme '{theme}', must be one of: "
            f"{', '.join(sorted(THEME_NAMES))}",
            "validation_error",
        )


def _create_and_populate_workflow(
    connection: Any,
    *,
    name: str,
    template: str,
    account_id: str,
    theme: str | None,
    objective: str | None,
    resolved_instructions: str | None,
    activate: bool,
) -> tuple[Any, list[str]] | None:
    """Run the §V.54 mutation sequence: create -> update extras -> optional activate.

    Returns the populated workflow row and the list of fields written, or
    ``None`` when ``create_workflow`` collided on the ``(account_id, name)``
    unique constraint per §V.16(+).
    """
    from mailpilot.database import activate_workflow, create_workflow, update_workflow

    created = create_workflow(
        connection,
        name=name,
        template=template,
        account_id=account_id,
        theme=theme or "blue",
    )
    if created is None:
        return None
    extras: dict[str, object] = {}
    if objective is not None:
        extras["objective"] = objective
    if resolved_instructions is not None:
        extras["instructions"] = resolved_instructions
    if extras:
        created = update_workflow(connection, created.id, **extras) or created
    if activate:
        created = activate_workflow(connection, created.id)
    changed = ["name", "template", "account_id", "theme"]
    if objective is not None:
        changed.append("objective")
    if resolved_instructions is not None:
        changed.append("instructions")
    if activate:
        changed.append("status")
    return created, changed


@workflow.command("create")
@click.option("--name", required=True, help="Workflow name.")
@click.option(
    "--template",
    required=True,
    type=click.Choice(["outbound-general", "inbound-general", "inbound-google-drive"]),
    help=(
        "Workflow template. Owns the agent's tool set and protocol. "
        "Immutable after creation; direction is derived from the template."
    ),
)
@click.option(
    "--account-email",
    default=None,
    help="Owning Gmail account (email or ID).",
)
@click.option("--objective", default=None, help="Workflow objective.")
@click.option(
    "--instructions",
    default=None,
    help="Workflow instructions (inline text).",
)
@click.option(
    "--instructions-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a file with the workflow instructions (system prompt).",
)
@click.option(
    "--theme",
    default=None,
    help="Email color theme (blue, green, orange, purple, red, slate).",
)
@click.option(
    "--draft",
    is_flag=True,
    default=False,
    help="Keep workflow in draft status.",
)
def workflow_create(
    name: str,
    template: str,
    account_email: str | None,
    objective: str | None,
    instructions: str | None,
    instructions_file: str | None,
    theme: str | None,
    draft: bool,
) -> None:
    """Create a new workflow."""
    from mailpilot.database import initialize_database
    from mailpilot.operator_log import cli_mutation, operator_event

    if not name.strip():
        output_error("workflow name cannot be empty", "validation_error")
    if theme is not None:
        _validate_theme(theme)
    if instructions is not None and instructions_file is not None:
        output_error(
            "--instructions and --instructions-file are mutually exclusive",
            "validation_error",
        )
    has_objective = objective is not None
    has_instructions = instructions is not None or instructions_file is not None
    if not draft and not (has_objective and has_instructions):
        output_error(
            "cannot activate workflow without objective and instructions. "
            "Use --draft to create without them.",
            "validation_error",
        )
    resolved = _resolve_instructions(instructions, instructions_file)
    activate = not draft and has_objective and has_instructions
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        account_id = _resolve_account(connection, account_email).id
        with cli_mutation(
            "workflow",
            "create",
            account_id=account_id,
            template=template,
        ):
            result = _create_and_populate_workflow(
                connection,
                name=name,
                template=template,
                account_id=account_id,
                theme=theme,
                objective=objective,
                resolved_instructions=resolved,
                activate=activate,
            )
            if result is None:
                output_error(
                    f"workflow {name!r} already exists for account {account_id}",
                    "duplicate_key",
                )
            created, changed = result
            operator_event(
                "workflow.create",
                entity_id=created.id,
                account_id=account_id,
                template=template,
                changed=changed,
            )
            output_entity("workflow", created)
    finally:
        connection.close()


@workflow.command("update")
@click.argument("workflow_id")
@click.option("--name", default=None, help="Workflow name.")
@click.option("--objective", default=None, help="Workflow objective.")
@click.option(
    "--instructions",
    default=None,
    help="Workflow instructions (inline text).",
)
@click.option(
    "--instructions-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a file with the workflow instructions (system prompt).",
)
@click.option(
    "--theme",
    default=None,
    help="Email color theme (blue, green, orange, purple, red, slate).",
)
def workflow_update(
    workflow_id: str,
    name: str | None,
    objective: str | None,
    instructions: str | None,
    instructions_file: str | None,
    theme: str | None,
) -> None:
    """Update a workflow."""
    from mailpilot.database import get_workflow, initialize_database, update_workflow
    from mailpilot.operator_log import cli_mutation, operator_event

    if theme is not None:
        _validate_theme(theme)
    if instructions is not None and instructions_file is not None:
        output_error(
            "--instructions and --instructions-file are mutually exclusive",
            "validation_error",
        )
    resolved = _resolve_instructions(instructions, instructions_file)
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        before = get_workflow(connection, workflow_id)
        if before is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        fields: dict[str, object] = {}
        if name is not None:
            fields["name"] = name
        if objective is not None:
            fields["objective"] = objective
        if resolved is not None:
            fields["instructions"] = resolved
        if theme is not None:
            fields["theme"] = theme
        with cli_mutation("workflow", "update", entity_id=workflow_id):
            updated = update_workflow(connection, workflow_id, **fields)
            if updated is None:
                output_error(f"workflow not found: {workflow_id}", "not_found")
            changed = [
                field
                for field in ("name", "objective", "instructions", "theme")
                if getattr(before, field) != getattr(updated, field)
            ]
            operator_event(
                "workflow.update",
                entity_id=workflow_id,
                changed=changed,
            )
            output_entity("workflow", updated)
    finally:
        connection.close()


@workflow.command("search")
@click.argument("query")
@click.option("--limit", default=100, help="Maximum results.")
def workflow_search(query: str, limit: int) -> None:
    """Search workflows by name or objective."""
    from mailpilot.database import initialize_database, search_workflows

    connection = initialize_database(_database_url())
    try:
        workflows = search_workflows(connection, query, limit=limit)
        output({"workflows": [w.model_dump(mode="json") for w in workflows]})
    finally:
        connection.close()


@workflow.command("list")
@scope_option("--account-email", "account_email", "Filter by account (email or ID).")
@enum_option("--status", "status", _WORKFLOW_STATUSES, "Filter by workflow status.")
@enum_option(
    "--direction", "workflow_type", DIRECTIONS, "Filter by workflow direction."
)
@enum_option(
    "--template", "template", _WORKFLOW_TEMPLATES, "Filter by workflow template."
)
@time_window_options("created_at")
@limit_option
def workflow_list(
    account_email: str | None,
    status: str | None,
    workflow_type: str | None,
    template: str | None,
    limit: int,
    since: str | None,
    until: str | None,
) -> None:
    """List workflows as summaries."""
    from mailpilot.database import initialize_database, list_workflows

    connection = initialize_database(_database_url())
    try:
        account_id = (
            _resolve_account(connection, account_email).id
            if account_email is not None
            else None
        )
        workflows = list_workflows(
            connection,
            account_id=account_id,
            status=status,
            workflow_type=workflow_type,
            template=template,
            limit=limit,
            since=since,
            until=until,
        )
        output({"workflows": [w.model_dump(mode="json") for w in workflows]})
    finally:
        connection.close()


@workflow.command("view")
@click.argument("workflow_id")
def workflow_view(workflow_id: str) -> None:
    """Show a workflow by ID."""
    from mailpilot.database import get_workflow, initialize_database

    connection = initialize_database(_database_url())
    try:
        found = get_workflow(connection, workflow_id)
        if found is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        output_entity("workflow", found)
    finally:
        connection.close()


@workflow.command("start")
@click.argument("workflow_id")
def workflow_start(workflow_id: str) -> None:
    """Start a workflow (requires non-empty objective and instructions)."""
    from mailpilot.database import activate_workflow, initialize_database
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        with cli_mutation("workflow", "start", entity_id=workflow_id):
            try:
                activated = activate_workflow(connection, workflow_id)
            except ValueError as exc:
                message = str(exc)
                if "objective" in message:
                    output_error(
                        f"cannot start: objective is empty. "
                        f'Run: workflow update {workflow_id} --objective "..."',
                        "invalid_state",
                    )
                if "instructions" in message:
                    output_error(
                        f"cannot start: instructions are empty. "
                        f'Run: workflow update {workflow_id} --instructions "..."',
                        "invalid_state",
                    )
                output_error(message, "invalid_state")
            operator_event(
                "workflow.start",
                entity_id=workflow_id,
                changed=["status"],
            )
            output_entity("workflow", activated)
    finally:
        connection.close()


@workflow.command("stop")
@click.argument("workflow_id")
def workflow_stop(workflow_id: str) -> None:
    """Stop an active workflow."""
    from mailpilot.database import initialize_database, pause_workflow
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        with cli_mutation("workflow", "stop", entity_id=workflow_id):
            try:
                paused = pause_workflow(connection, workflow_id)
            except ValueError as exc:
                output_error(str(exc), "invalid_state")
            operator_event(
                "workflow.stop",
                entity_id=workflow_id,
                changed=["status"],
            )
            output_entity("workflow", paused)
    finally:
        connection.close()


def _toml_basic_string(value: str) -> str:
    r"""Quote ``value`` as a TOML basic string with the minimal escaping.

    Single-line def fields (``name``, ``template``, ``theme``, ``objective``)
    round-trip through this; ``\``, ``"`` and control bytes are escaped so the
    emitted file re-parses to the original value.
    """
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _workflow_slug(name: str) -> str:
    """Derive a filesystem-safe ``*.toml`` stem from a workflow ``name``.

    Import keys on the in-file ``name`` field, not the filename, so the slug is
    purely cosmetic -- it only needs to be deterministic for stable diffs.
    """
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "workflow"


def _workflow_to_toml(workflow: Any) -> str:
    """Serialize a ``Workflow`` row to a one-workflow TOML catalog entry (§V.103).

    Emits the def fields ``{name, template, theme, objective, instructions}`` in
    a fixed order; ``instructions`` uses a multi-line literal string so pipes and
    quotes survive verbatim. The leading newline after the opening ``'''`` is
    trimmed by the TOML parser, so the value re-parses byte-identically.
    """
    return (
        f"name = {_toml_basic_string(workflow.name)}\n"
        f"template = {_toml_basic_string(workflow.template)}\n"
        f"theme = {_toml_basic_string(workflow.theme)}\n"
        f"objective = {_toml_basic_string(workflow.objective)}\n"
        f"instructions = '''\n{workflow.instructions}'''\n"
    )


@workflow.command("export")
@click.option(
    "--account-email",
    default=None,
    help="Owning Gmail account (email or ID).",
)
@click.option(
    "--out-dir",
    "out_dir",
    required=True,
    type=click.Path(file_okay=False),
    help="Directory to write one '*.toml' per workflow. Created if absent.",
)
def workflow_export(account_email: str | None, out_dir: str) -> None:
    """Export an account's workflows as one TOML file each.

    TOML-only: writes one ``*.toml`` per workflow into ``--out-dir`` (def fields
    ``{name, template, theme, objective, instructions}``, name-sorted) and prints
    a JSON status envelope listing the paths written. TOML never reaches stdout
    -- stdout stays strict JSON. ``export -> dir -> import`` round-trips
    idempotently.
    """
    import pathlib

    from mailpilot.database import (
        initialize_database,
        list_workflows_full,
    )

    connection = initialize_database(_database_url())
    try:
        account_id = _resolve_account(connection, account_email).id
        workflows = list_workflows_full(connection, account_id)
        directory = pathlib.Path(out_dir)
        directory.mkdir(parents=True, exist_ok=True)
        written: list[dict[str, str]] = []
        for current in workflows:
            path = directory / f"{_workflow_slug(current.name)}.toml"
            path.write_text(_workflow_to_toml(current))
            written.append({"name": current.name, "path": str(path)})
        output({"workflows": written})
    finally:
        connection.close()


_WORKFLOW_IMPORT_UPDATABLE = ("objective", "instructions", "theme")


def _import_workflow_create(
    connection: Any, account_id: str, name: str, template: str, entry: dict[str, Any]
) -> dict[str, object]:
    from mailpilot.database import activate_workflow, create_workflow, update_workflow
    from mailpilot.operator_log import operator_event

    theme = entry.get("theme") or "blue"
    created = create_workflow(
        connection,
        name=name,
        template=template,
        account_id=account_id,
        theme=theme,
    )
    if created is None:
        # Concurrent worker won the race per §V.16(+). Emit the same per-row
        # ``duplicate`` shape used elsewhere in this importer.
        operator_event(
            "workflow.import",
            account_id=account_id,
            name=name,
            changed=[],
        )
        return {
            "name": name,
            "error": "duplicate",
            "message": (f"workflow {name!r} already exists for account {account_id}"),
        }
    extras: dict[str, object] = {}
    objective = entry.get("objective")
    instructions = entry.get("instructions")
    if objective:
        extras["objective"] = objective
    if instructions:
        extras["instructions"] = instructions
    if extras:
        update_workflow(connection, created.id, **extras)
    activated = bool(objective and instructions)
    if activated:
        activate_workflow(connection, created.id)
    changed = ["name", "template", "account_id", "theme"]
    if objective:
        changed.append("objective")
    if instructions:
        changed.append("instructions")
    if activated:
        changed.append("status")
    operator_event(
        "workflow.import",
        entity_id=created.id,
        account_id=account_id,
        name=name,
        changed=changed,
    )
    return {"name": name, "action": "created"}


def _import_workflow_update(
    connection: Any, current: Any, entry: dict[str, Any]
) -> dict[str, object]:
    from mailpilot.database import update_workflow
    from mailpilot.operator_log import operator_event

    diff: dict[str, object] = {}
    for field in _WORKFLOW_IMPORT_UPDATABLE:
        if field not in entry:
            continue
        payload_value = entry[field] if entry[field] is not None else ""
        if getattr(current, field) != payload_value:
            diff[field] = payload_value
    if not diff:
        operator_event(
            "workflow.import",
            entity_id=current.id,
            account_id=current.account_id,
            name=current.name,
            changed=[],
        )
        return {"name": current.name, "action": "unchanged"}
    update_workflow(connection, current.id, **diff)
    operator_event(
        "workflow.import",
        entity_id=current.id,
        account_id=current.account_id,
        name=current.name,
        changed=list(diff.keys()),
    )
    return {"name": current.name, "action": "updated"}


def _import_workflow_row(
    connection: Any, account_id: str, existing: dict[str, Any], entry: dict[str, Any]
) -> dict[str, object]:
    from mailpilot.operator_log import operator_event

    name = entry.get("name")
    template = entry.get("template")
    if not isinstance(name, str) or not isinstance(template, str):
        operator_event(
            "workflow.import",
            account_id=account_id,
            name=name if isinstance(name, str) else "",
            changed=[],
        )
        return {
            "name": name if isinstance(name, str) else "",
            "error": "validation_error",
            "message": "row missing required 'name' or 'template'",
        }
    current = existing.get(name)
    if current is None:
        return _import_workflow_create(connection, account_id, name, template, entry)
    if current.template != template:
        operator_event(
            "workflow.import",
            entity_id=current.id,
            account_id=account_id,
            name=name,
            changed=[],
        )
        return {
            "name": name,
            "error": "template_immutable",
            "message": (
                f"workflow.template is immutable; existing "
                f"{current.template!r}, payload {template!r}"
            ),
        }
    return _import_workflow_update(connection, current, entry)


def _parse_toml_catalog_dir(
    path: pathlib.Path,
) -> tuple[list[dict[str, Any]], list[dict[str, object]]]:
    """Glob ``*.toml`` in a catalog dir; collect each as an entry (§V.103).

    A file that fails to parse becomes a per-row error so the rest of the
    catalog still imports (§V.63).
    """
    import tomllib

    entries: list[dict[str, Any]] = []
    pre_errors: list[dict[str, object]] = []
    for toml_path in sorted(path.glob("*.toml")):
        try:
            with toml_path.open("rb") as handle:
                entries.append(tomllib.load(handle))
        except tomllib.TOMLDecodeError as exc:
            pre_errors.append(
                {
                    "name": toml_path.name,
                    "error": "validation_error",
                    "message": f"malformed TOML in {toml_path.name}: {exc}",
                }
            )
    return entries, pre_errors


def _load_workflow_import_entries(
    file: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, object]]]:
    """Parse a ``workflow import`` source into entries + per-row pre-errors (§V.103).

    TOML-only per §V.103, §V.63 (no JSON, no stdin). Dispatch by shape: a
    directory globs ``*.toml`` (catalog batch, per-file parse errors become
    per-row pre-errors) and a single ``.toml`` file parses to one entry. A
    missing ``--file`` or a non-TOML path exits via ``output_error`` with
    ``validation_error``.
    """
    import pathlib
    import tomllib

    if file is None:
        output_error(
            "no input: provide --file PATH (a '.toml' file or a directory)",
            "validation_error",
        )
    path = pathlib.Path(file)
    if path.is_dir():
        return _parse_toml_catalog_dir(path)
    if path.suffix == ".toml":
        try:
            with path.open("rb") as handle:
                return [tomllib.load(handle)], []
        except tomllib.TOMLDecodeError as exc:
            output_error(f"malformed TOML: {exc}", "validation_error")
    output_error(
        "unsupported workflow source: expected a '.toml' file or a directory",
        "validation_error",
    )


@workflow.command("import")
@click.option(
    "--account-email",
    default=None,
    help="Owning Gmail account (email or ID).",
)
@click.option(
    "--file",
    "file",
    default=None,
    type=click.Path(exists=True),
    help=(
        "Workflow source (TOML only): a '.toml' file imports one workflow "
        "(catalog entry); a directory imports every '*.toml' in it."
    ),
)
def workflow_import(account_email: str | None, file: str | None) -> None:
    """Import workflows for an account from TOML catalog files.

    TOML-only -- no JSON, no stdin. Dispatch is by ``--file`` shape:

    * ``--file X.toml`` -- one workflow as pure TOML; ``instructions`` may use a
      multi-line literal string.
    * ``--file <dir>`` -- every ``*.toml`` in the directory (catalog batch); a
      file that fails to parse becomes a per-row error and the batch continues.

    Each parsed entry feeds the same upsert (keyed on ``(account_id, name)``):
    workflows absent from the DB are created (and activated when both
    ``objective`` and ``instructions`` are non-empty), present workflows are
    updated for changed fields only, ``template`` differences emit a per-row
    ``template_immutable`` error, and ``status`` is never written by import.
    """
    from mailpilot.database import (
        initialize_database,
        list_workflows_full,
    )
    from mailpilot.operator_log import cli_mutation

    entries, pre_errors = _load_workflow_import_entries(file)

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        account_id = _resolve_account(connection, account_email).id
        with cli_mutation(
            "workflow",
            "import",
            account_id=account_id,
            row_count=len(entries) + len(pre_errors),
        ):
            existing = {w.name: w for w in list_workflows_full(connection, account_id)}
            results: list[dict[str, object]] = [*pre_errors]
            results.extend(
                _import_workflow_row(connection, account_id, existing, entry)
                for entry in entries
            )
            output({"workflows": results})
    finally:
        connection.close()


# -- Template commands ---------------------------------------------------------


@main.group()
def template() -> None:
    """Inspect built-in workflow templates (read-only, code-defined)."""


@template.command("list")
@enum_option("--direction", "direction", DIRECTIONS, "Filter by template direction.")
def template_list(direction: str | None) -> None:
    """List all workflow templates as summaries."""
    from mailpilot.agent.templates import TEMPLATES
    from mailpilot.models import WorkflowTemplateSummary

    summaries: list[WorkflowTemplateSummary] = []
    for tpl in TEMPLATES.values():
        if direction is not None and tpl.direction != direction:
            continue
        summaries.append(
            WorkflowTemplateSummary(
                name=tpl.name,
                direction=tpl.direction,
                description=tpl.description,
                tool_count=len(tpl.tools),
            )
        )
    output({"templates": [s.model_dump(mode="json") for s in summaries]})


@template.command("view")
@click.argument("name")
def template_view(name: str) -> None:
    """Show full template record (tools + protocol)."""
    from mailpilot.agent.templates import TEMPLATES
    from mailpilot.models import WorkflowTemplateRecord

    tpl = TEMPLATES.get(name)  # pyright: ignore[reportArgumentType]
    if tpl is None:
        output_error(f"template not found: {name}", "not_found")
    record = WorkflowTemplateRecord(
        name=tpl.name,
        direction=tpl.direction,
        description=tpl.description,
        tools=[t.name for t in tpl.tools],
        protocol=tpl.protocol,
    )
    output_entity("template", record)


# -- Enrollment commands -------------------------------------------------------


@main.group()
def enrollment() -> None:
    """Manage contact enrollments in workflows."""


def _reject_enrollment_self_loop(
    account: Any,
    contact: Any,
    workflow_name: str,
) -> None:
    """Reject enrollment when contact.email matches workflow's account email.

    Per SPEC §V.33 -- semantic self-loop (agent notionally emails itself).
    Compare case-insensitively (Gmail addresses are case-insensitive). When
    ``account`` is ``None`` (defensive: FK-orphaned workflow), no rejection.
    """
    if account is not None and account.email.lower() == contact.email.lower():
        output_error(
            f"cannot enroll contact {contact.email} in workflow "
            f"{workflow_name}: contact email matches workflow's account email",
            "self_loop",
        )


def _maybe_schedule_first_touch(
    connection: Any,
    enrollment_id: str,
    workflow_id: str,
    contact_id: str,
    scheduled_iso: str | None,
    changed: list[str],
) -> None:
    """Insert a pending first-touch task per §V.32 unless one already exists.

    Idempotent: if ``find_pending_first_touch_task`` returns a row for the
    enrollment, no task is created and ``changed`` is left alone. On insert,
    ``changed`` gains ``"scheduled_first_send"`` so §V.54 operator events
    carry an accurate diff list.
    """
    if scheduled_iso is None:
        return
    from mailpilot.database import (
        create_task,
        find_pending_first_touch_task,
    )

    if find_pending_first_touch_task(connection, enrollment_id) is not None:
        return
    create_task(
        connection,
        enrollment_id=enrollment_id,
        workflow_id=workflow_id,
        contact_id=contact_id,
        description="scheduled first reach-out",
        scheduled_at=scheduled_iso,
        context={"trigger": "enrollment_schedule"},
        email_id=None,
    )
    changed.append("scheduled_first_send")


@enrollment.command("add")
@click.option("--workflow-id", required=True, help="Workflow ID.")
@click.option("--contact-email", required=True, help="Contact (email or ID).")
@click.option(
    "--scheduled-at",
    "scheduled_at",
    default=None,
    help=(
        "ISO 8601 timestamp for scheduled first reach-out (outbound workflows "
        "only). Inserts a pending task drained by the run loop."
    ),
)
def enrollment_add(
    workflow_id: str, contact_email: str, scheduled_at: str | None
) -> None:
    """Enroll a contact in a workflow.

    When ``--scheduled-at`` is given on an outbound workflow, a pending
    task is inserted so the run loop dispatches the initial outbound
    message at that time. Re-running against an enrollment that already
    has a pending first-touch task is a no-op (idempotent). Inbound
    workflows reject ``--scheduled-at`` -- inbound is reactive.
    """
    from datetime import datetime

    from mailpilot.database import (
        create_activity,
        create_enrollment,
        get_account,
        get_enrollment,
        get_workflow,
        initialize_database,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    scheduled_iso: str | None = None
    if scheduled_at is not None:
        try:
            scheduled_iso = datetime.fromisoformat(scheduled_at).isoformat()
        except ValueError as exc:
            output_error(f"invalid --scheduled-at value: {exc}", "validation_error")

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        workflow = get_workflow(connection, workflow_id)
        if workflow is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        if scheduled_iso is not None and workflow.type != "outbound":
            output_error(
                "--scheduled-at only valid for outbound workflows",
                "invalid_state",
            )
        contact = _resolve_contact(connection, contact_email)
        contact_id = contact.id
        account = get_account(connection, workflow.account_id)
        _reject_enrollment_self_loop(account, contact, workflow.name)
        mutation_attrs: dict[str, Any] = {
            "workflow_id": workflow_id,
            "contact_id": contact_id,
        }
        if scheduled_iso is not None:
            mutation_attrs["scheduled_at"] = scheduled_iso
        with cli_mutation("enrollment", "add", **mutation_attrs):
            created = create_enrollment(connection, workflow_id, contact_id)
            if created is not None:
                create_activity(
                    connection,
                    contact_id=contact_id,
                    activity_type="enrollment_added",
                    summary=f"Assigned to {workflow.name}",
                    detail={"workflow_name": workflow.name},
                    company_id=contact.company_id,
                    workflow_id=workflow_id,
                    enrollment_id=created.id,
                )
                target = created
                changed = ["status"]
            else:
                existing = get_enrollment(connection, workflow_id, contact_id)
                if existing is None:
                    return
                target = existing
                changed = []
            _maybe_schedule_first_touch(
                connection,
                target.id,
                workflow_id,
                contact_id,
                scheduled_iso,
                changed,
            )
            event_fields: dict[str, Any] = {
                "enrollment_id": target.id,
                "workflow_id": workflow_id,
                "contact_id": contact_id,
            }
            if scheduled_iso is not None:
                event_fields["scheduled_at"] = scheduled_iso
            event_fields["changed"] = changed
            operator_event("enrollment.add", **event_fields)
            output_entity("enrollment", target)
    finally:
        connection.close()


@enrollment.command("run")
@click.argument("enrollment_id")
def enrollment_run(enrollment_id: str) -> None:
    """Invoke the workflow agent for an enrollment synchronously.

    Manual runs invoke the agent directly. Going through ``create_task``
    would fire ``pg_notify('task_pending')``, which a parallel ``mailpilot
    run`` listener thread translates into a competing drain of the same
    row. Tasks are for deferred work; CLI runs are immediate.
    """
    from mailpilot.agent import invoke_workflow_agent
    from mailpilot.database import (
        get_contact,
        get_enrollment_by_id,
        get_unprocessed_inbound_email,
        get_workflow,
        initialize_database,
    )
    from mailpilot.settings import get_settings

    settings = get_settings()
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        record = get_enrollment_by_id(connection, enrollment_id)
        if record is None:
            output_error(f"enrollment not found: {enrollment_id}", "not_found")
        wf = get_workflow(connection, record.workflow_id)
        if wf is None:
            output_error(f"workflow not found: {record.workflow_id}", "not_found")
        if wf.status != "active":
            output_error(
                f"workflow is not active (status={wf.status})", "invalid_state"
            )
        contact = get_contact(connection, record.contact_id)
        if contact is None:
            output_error(f"contact not found: {record.contact_id}", "not_found")
        if record.status != "active":
            output_error(
                f"enrollment is not active (status={record.status})",
                "invalid_state",
            )
        email = None
        if wf.type == "inbound":
            email = get_unprocessed_inbound_email(connection, wf.id, contact.id)
        envelope: dict[str, object] = {
            "enrollment_id": record.id,
            "workflow_id": wf.id,
            "contact_id": contact.id,
        }
        try:
            # §V.30: prompt framing comes from `trigger`, not a synthesised
            # task_description. enrollment_run is an initial reach-out, not
            # resumed deferred work.
            result = invoke_workflow_agent(
                connection,
                settings,
                wf,
                contact,
                email=email,
                trigger="enrollment_run",
            )
        except Exception as exc:
            envelope["status"] = "failed"
            envelope["result"] = {"reason": str(exc)}
            output(envelope)
            return
        if result is None:
            envelope["status"] = "skipped"
            envelope["result"] = {"reason": "agent lock held"}
            output(envelope)
            return
        envelope["status"] = "completed"
        envelope["result"] = {
            "reasoning": result.get("reasoning", ""),
            "tool_calls": result.get("tool_calls", 0),
        }
        output(envelope)
    finally:
        connection.close()


@enrollment.command("disable")
@click.argument("enrollment_id")
@click.option(
    "--reason",
    required=True,
    help="Explanation written to disabled_reason and the enrollment_disabled activity.",
)
def enrollment_disable(enrollment_id: str, reason: str) -> None:
    """Soft-disable an enrollment via terminal lifecycle exit.

    Flips ``status='disabled'``, writes ``disabled_reason``, and appends an
    ``enrollment_disabled`` activity carrying the reason. Disabled is
    terminal -- re-enrolling means creating a fresh enrollment via
    ``enrollment add``.
    """
    from mailpilot.database import (
        disable_enrollment,
        get_enrollment_by_id,
        initialize_database,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    if reason.strip() == "":
        output_error("reason cannot be empty", "validation_error")
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        before = get_enrollment_by_id(connection, enrollment_id)
        if before is None:
            output_error(f"enrollment not found: {enrollment_id}", "not_found")
        with cli_mutation("enrollment", "disable", entity_id=enrollment_id):
            updated = disable_enrollment(connection, enrollment_id, reason)
            if updated is None:
                output_error(f"enrollment not found: {enrollment_id}", "not_found")
            changed = [
                field
                for field in ("status", "disabled_reason")
                if getattr(before, field) != getattr(updated, field)
            ]
            operator_event(
                "enrollment.disable",
                entity_id=enrollment_id,
                changed=changed,
            )
            output_entity("enrollment", updated)
    finally:
        connection.close()


@enrollment.command("enable")
@click.argument("enrollment_id")
def enrollment_enable(enrollment_id: str) -> None:
    """Re-enable a disabled enrollment by flipping status back to active.

    Clears disabled_reason and resumes the enrollment. Enabling an enrollment
    that is not disabled is rejected.
    """
    from mailpilot.database import (
        enable_enrollment,
        get_enrollment_by_id,
        initialize_database,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        before = get_enrollment_by_id(connection, enrollment_id)
        if before is None:
            output_error(f"enrollment not found: {enrollment_id}", "not_found")
        if before.status != "disabled":
            output_error(
                f"enrollment {enrollment_id} is not disabled",
                "validation_error",
            )
        with cli_mutation("enrollment", "enable", entity_id=enrollment_id):
            updated = enable_enrollment(connection, enrollment_id)
            if updated is None:
                output_error(
                    f"enrollment {enrollment_id} is not disabled",
                    "validation_error",
                )
            changed = [
                field
                for field in ("status", "disabled_reason")
                if getattr(before, field) != getattr(updated, field)
            ]
            operator_event(
                "enrollment.enable",
                entity_id=enrollment_id,
                changed=changed,
            )
            output_entity("enrollment", updated)
    finally:
        connection.close()


@enrollment.command("view")
@click.argument("enrollment_id")
def enrollment_view(enrollment_id: str) -> None:
    """View an enrollment by id."""
    from mailpilot.database import get_enrollment_by_id, initialize_database

    connection = initialize_database(_database_url())
    try:
        record = get_enrollment_by_id(connection, enrollment_id)
        if record is None:
            output_error("enrollment not found", "not_found")
        output_entity("enrollment", record)
    finally:
        connection.close()


@enrollment.command("list")
@scope_option("--workflow-id", "workflow_id", "Filter by workflow ID.")
@scope_option("--contact-email", "contact_email", "Filter by contact (email or ID).")
@enum_option("--status", "status", _ENROLLMENT_STATUSES, "Filter by enrollment status.")
@time_window_options("updated_at")
@limit_option
def enrollment_list(
    workflow_id: str | None,
    contact_email: str | None,
    status: str | None,
    limit: int,
    since: str | None,
    until: str | None,
) -> None:
    """List enrollments as summaries. Filter by workflow, contact, or both."""
    from mailpilot.database import (
        get_workflow,
        initialize_database,
        list_enrollments_detailed,
    )

    connection = initialize_database(_database_url())
    try:
        if workflow_id is not None and get_workflow(connection, workflow_id) is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        contact_id = (
            _resolve_contact(connection, contact_email).id
            if contact_email is not None
            else None
        )
        rows = list_enrollments_detailed(
            connection,
            workflow_id=workflow_id,
            contact_id=contact_id,
            status=status,
            limit=limit,
            since=since,
            until=until,
        )
        output({"enrollments": [r.model_dump(mode="json") for r in rows]})
    finally:
        connection.close()


# -- Task commands -------------------------------------------------------------


@main.group()
def task() -> None:
    """Manage deferred agent tasks."""


@task.command("list")
@scope_option("--workflow-id", "workflow_id", "Filter by workflow ID.")
@scope_option("--contact-email", "contact_email", "Filter by contact (email or ID).")
@enum_option("--status", "status", _TASK_STATUSES, "Filter by task status.")
@time_window_options("scheduled_at")
@limit_option
def task_list(
    workflow_id: str | None,
    contact_email: str | None,
    status: str | None,
    limit: int,
    since: str | None,
    until: str | None,
) -> None:
    """List tasks as summaries with optional filters."""
    from mailpilot.database import (
        get_workflow,
        initialize_database,
        list_tasks,
    )

    connection = initialize_database(_database_url())
    try:
        if workflow_id is not None and get_workflow(connection, workflow_id) is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        contact_id = (
            _resolve_contact(connection, contact_email).id
            if contact_email is not None
            else None
        )
        tasks = list_tasks(
            connection,
            workflow_id=workflow_id,
            contact_id=contact_id,
            status=status,
            limit=limit,
            since=since,
            until=until,
        )
        output({"tasks": [t.model_dump(mode="json") for t in tasks]})
    finally:
        connection.close()


@task.command("view")
@click.argument("task_id")
def task_view(task_id: str) -> None:
    """Show a task by ID."""
    from mailpilot.database import get_task, initialize_database

    connection = initialize_database(_database_url())
    try:
        found = get_task(connection, task_id)
        if found is None:
            output_error(f"task not found: {task_id}", "not_found")
        output_entity("task", found)
    finally:
        connection.close()


@task.command("cancel")
@click.argument("task_id")
def task_cancel(task_id: str) -> None:
    """Cancel a pending task."""
    from mailpilot.database import cancel_task, initialize_database

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        cancelled = cancel_task(connection, task_id)
        if cancelled is None:
            output_error(f"task not found or not pending: {task_id}", "not_found")
        output_entity("task", cancelled)
    finally:
        connection.close()


@task.command("retry")
@click.argument("task_id")
def task_retry(task_id: str) -> None:
    """Reset a failed or cancelled task for a fresh attempt.

    Refuses ``completed`` rows (tools already fired -- replay risks
    duplicate side-effects) and ``pending`` rows (already queued).
    """
    from mailpilot.database import (
        get_task,
        initialize_database,
        manual_retry_task,
    )

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        existing = get_task(connection, task_id)
        if existing is None:
            output_error(f"task not found: {task_id}", "not_found")
        reset = manual_retry_task(connection, task_id)
        if reset is None:
            output_error(
                f"task not retryable in status {existing.status!r}: {task_id}",
                "invalid_state",
            )
        output_entity("task", reset)
    finally:
        connection.close()
