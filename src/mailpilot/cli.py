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

if TYPE_CHECKING:
    from logfire import ScrubMatch

# Keep in sync with ActivityType in models.py and CHECK constraint in schema.sql.
_ACTIVITY_TYPES = [
    "email_sent",
    "email_received",
    "note_added",
    "tag_added",
    "tag_removed",
    "tag_disabled",
    "status_changed",
    "enrollment_added",
    "enrollment_completed",
    "enrollment_failed",
    "enrollment_paused",
    "enrollment_resumed",
    "enrollment_disabled",
]


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


def output_error(message: str, code: str) -> NoReturn:
    """Print structured JSON error to stderr and exit."""
    from opentelemetry import trace

    payload: dict[str, object] = {"error": code, "message": message, "ok": False}
    current = trace.get_current_span()
    ctx = current.get_span_context() if current else None
    if ctx is not None and ctx.is_valid:
        payload["trace_id"] = format(ctx.trace_id, "032x")
    click.echo(json.dumps(payload, indent=2, ensure_ascii=False), err=True)
    raise SystemExit(1)


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
    connection = initialize_database(_database_url())
    try:
        start_sync_loop(connection, settings)
    finally:
        connection.close()


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
    connection = initialize_database(_database_url())
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
@click.option("--limit", default=100, help="Maximum results.")
@click.option("--since", default=None, help="ISO datetime lower bound on created_at.")
def account_list(limit: int, since: str | None) -> None:
    """List Gmail accounts as summaries."""
    from mailpilot.database import initialize_database, list_accounts

    connection = initialize_database(_database_url())
    try:
        accounts = list_accounts(connection, limit=limit, since=since)
        output({"accounts": [a.model_dump(mode="json") for a in accounts]})
    finally:
        connection.close()


@account.command("view")
@click.argument("account_id")
def account_view(account_id: str) -> None:
    """Show a Gmail account by ID."""
    from mailpilot.database import get_account, initialize_database

    connection = initialize_database(_database_url())
    try:
        found = get_account(connection, account_id)
        if found is None:
            output_error(f"account not found: {account_id}", "not_found")
        output_entity("account", found)
    finally:
        connection.close()


@account.command("update")
@click.argument("account_id")
@click.option("--display-name", default=None, help="Display name.")
def account_update(account_id: str, display_name: str | None) -> None:
    """Update a Gmail account."""
    from mailpilot.database import get_account, initialize_database, update_account
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url())
    try:
        before = get_account(connection, account_id)
        if before is None:
            output_error(f"account not found: {account_id}", "not_found")
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


@account.command("sync")
@click.option(
    "--account-id",
    default=None,
    help="Sync only the given account; omit to sync all accounts.",
)
def account_sync(account_id: str | None) -> None:
    """Run a one-shot Gmail sync for one or all accounts."""
    import logfire

    from mailpilot.database import get_account, initialize_database, list_accounts
    from mailpilot.gmail import GmailClient
    from mailpilot.settings import get_settings
    from mailpilot.sync import sync_account

    settings = get_settings()
    connection = initialize_database(_database_url())
    try:
        if account_id is not None:
            single = get_account(connection, account_id)
            if single is None:
                output_error(f"account not found: {account_id}", "not_found")
            accounts = [single]
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
                    stored = sync_account(connection, acc, client, settings)
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
    connection = initialize_database(_database_url())
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
@click.argument("company_id")
@click.option("--name", default=None, help="Company name.")
@click.option(
    "--profile-json",
    default=None,
    help="JSON object validated against CompanyProfile (§V.72).",
)
def company_update(company_id: str, name: str | None, profile_json: str | None) -> None:
    """Update a company."""
    import json

    from mailpilot.database import get_company, initialize_database, update_company
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url())
    try:
        before = get_company(connection, company_id)
        if before is None:
            output_error(f"company not found: {company_id}", "not_found")
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
@click.option("--limit", default=100, help="Maximum results.")
@click.option("--since", default=None, help="ISO datetime lower bound on created_at.")
@click.option(
    "--has-profile",
    is_flag=True,
    default=False,
    help="Return only companies with a non-NULL profile (§V.72).",
)
@click.option(
    "--no-profile",
    is_flag=True,
    default=False,
    help="Return only companies with a NULL profile (§V.72).",
)
def company_list(
    limit: int, since: str | None, has_profile: bool, no_profile: bool
) -> None:
    """List companies as summaries."""
    from mailpilot.database import initialize_database, list_companies

    if has_profile and no_profile:
        output_error(
            "--has-profile and --no-profile are mutually exclusive",
            "validation_error",
        )
    profile_filter: bool | None = None
    if has_profile:
        profile_filter = True
    elif no_profile:
        profile_filter = False

    connection = initialize_database(_database_url())
    try:
        companies = list_companies(
            connection, limit=limit, since=since, has_profile=profile_filter
        )
        output({"companies": [c.model_dump(mode="json") for c in companies]})
    finally:
        connection.close()


@company.command("view")
@click.argument("company_id")
def company_view(company_id: str) -> None:
    """Show a company by ID with inlined notes."""
    from mailpilot.database import initialize_database, load_company_view

    connection = initialize_database(_database_url())
    try:
        found = load_company_view(connection, company_id)
        if found is None:
            output_error(f"company not found: {company_id}", "not_found")
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
    """Export all companies as a declarative JSON payload (§V.4)."""
    import pathlib

    from mailpilot.database import get_company, initialize_database, list_companies

    connection = initialize_database(_database_url())
    try:
        summaries = list_companies(connection, limit=100_000)
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
    """Import companies from a declarative JSON array (§V.63 batch-error pattern).

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

    connection = initialize_database(_database_url())
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
@click.option("--company-id", default=None, help="Company ID.")
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
    company_id: str | None,
    title: str | None,
    email_confidence: int | None,
    note: str | None,
) -> None:
    """Create a new contact."""
    from mailpilot.database import (
        add_contact_note,
        create_contact,
        get_company,
        initialize_database,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url())
    try:
        if company_id is not None and get_company(connection, company_id) is None:
            output_error(f"company not found: {company_id}", "not_found")
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
@click.argument("contact_id")
@click.option("--email", default=None, help="Email address.")
@click.option("--first-name", default=None, help="First name.")
@click.option("--last-name", default=None, help="Last name.")
@click.option("--company-id", default=None, help="Company ID.")
@click.option("--title", default=None, help="Role label (lead-metadata).")
@click.option(
    "--email-confidence",
    type=int,
    default=None,
    help="Deliverability score 0-100; low = high risk (lead-metadata).",
)
def contact_update(
    contact_id: str,
    email: str | None,
    first_name: str | None,
    last_name: str | None,
    company_id: str | None,
    title: str | None,
    email_confidence: int | None,
) -> None:
    """Update a contact."""
    from mailpilot.database import get_contact, initialize_database, update_contact
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url())
    try:
        before = get_contact(connection, contact_id)
        if before is None:
            output_error(f"contact not found: {contact_id}", "not_found")
        fields: dict[str, object] = {}
        if email is not None:
            fields["email"] = email
        if first_name is not None:
            fields["first_name"] = first_name
        if last_name is not None:
            fields["last_name"] = last_name
        if company_id is not None:
            fields["company_id"] = company_id
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
@click.argument("contact_id")
@click.option(
    "--reason",
    required=True,
    help="Explanation written to disabled_reason.",
)
def contact_disable(contact_id: str, reason: str) -> None:
    """Soft-disable a contact by writing disabled_reason."""
    from mailpilot.database import disable_contact, get_contact, initialize_database
    from mailpilot.operator_log import cli_mutation, operator_event

    if reason.strip() == "":
        output_error("reason cannot be empty", "validation_error")
    connection = initialize_database(_database_url())
    try:
        before = get_contact(connection, contact_id)
        if before is None:
            output_error(f"contact not found: {contact_id}", "not_found")
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
@click.option("--limit", default=100, help="Maximum results.")
@click.option("--company-id", default=None, help="Filter by company ID.")
@click.option("--since", default=None, help="ISO datetime lower bound on created_at.")
@click.option(
    "--include-disabled",
    is_flag=True,
    default=False,
    help="Also list contacts with a non-null disabled_reason (default: hide).",
)
@click.option(
    "--max-email-confidence",
    type=int,
    default=None,
    help="Surface only rows with email_confidence <= N (low-score lead review).",
)
def contact_list(
    limit: int,
    company_id: str | None,
    since: str | None,
    include_disabled: bool,
    max_email_confidence: int | None,
) -> None:
    """List contacts as summaries."""
    from mailpilot.database import get_company, initialize_database, list_contacts

    connection = initialize_database(_database_url())
    try:
        if company_id is not None and get_company(connection, company_id) is None:
            output_error(f"company not found: {company_id}", "not_found")
        contacts = list_contacts(
            connection,
            limit=limit,
            company_id=company_id,
            since=since,
            include_disabled=include_disabled,
            max_email_confidence=max_email_confidence,
        )
        output({"contacts": [c.model_dump(mode="json") for c in contacts]})
    finally:
        connection.close()


@contact.command("view")
@click.argument("contact_id")
def contact_view(contact_id: str) -> None:
    """Show a contact by ID with inlined notes (own + parent company)."""
    from mailpilot.database import initialize_database, load_contact_view

    connection = initialize_database(_database_url())
    try:
        found = load_contact_view(connection, contact_id)
        if found is None:
            output_error(f"contact not found: {contact_id}", "not_found")
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
    """Export all contacts as a declarative JSON payload (§V.4)."""
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
    """Import contacts from a declarative JSON array (§V.63 batch-error pattern).

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

    connection = initialize_database(_database_url())
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
@click.option("--limit", default=100, help="Maximum number of results.")
@click.option("--contact-id", default=None, help="Filter by contact ID.")
@click.option("--account-id", default=None, help="Filter by account ID.")
@click.option("--since", default=None, help="ISO datetime lower bound.")
@click.option("--thread-id", default=None, help="Filter by Gmail thread ID.")
@click.option(
    "--direction",
    default=None,
    type=click.Choice(["inbound", "outbound"]),
    help="Filter by direction.",
)
@click.option("--workflow-id", default=None, help="Filter by workflow ID.")
@click.option(
    "--status",
    default=None,
    type=click.Choice(["sent", "received", "bounced"]),
    help="Filter by email status.",
)
@click.option("--from", "sender", default=None, help="Filter by sender email address.")
@click.option(
    "--to", "recipient", default=None, help="Filter by recipient email address."
)
@click.option(
    "--route-method",
    "route_method",
    default=None,
    help="Filter by persisted routing decision (e.g. classified, thread_match).",
)
def email_list(
    limit: int,
    contact_id: str | None,
    account_id: str | None,
    since: str | None,
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
        get_account,
        get_contact,
        get_workflow,
        initialize_database,
        list_emails,
    )

    connection = initialize_database(_database_url())
    try:
        if contact_id is not None and get_contact(connection, contact_id) is None:
            output_error(f"contact not found: {contact_id}", "not_found")
        if account_id is not None and get_account(connection, account_id) is None:
            output_error(f"account not found: {account_id}", "not_found")
        if workflow_id is not None and get_workflow(connection, workflow_id) is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        emails = list_emails(
            connection,
            limit=limit,
            contact_id=contact_id,
            account_id=account_id,
            since=since,
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
@click.option("--account-id", required=True, help="Sending account ID.")
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
    account_id: str,
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
    from mailpilot.database import get_account, get_workflow, initialize_database
    from mailpilot.gmail import GmailClient
    from mailpilot.settings import get_settings

    if not subject.strip():
        output_error("subject cannot be empty", "validation_error")
    if not body.strip():
        output_error("body cannot be empty", "validation_error")

    to_joined = ",".join(to)
    settings = get_settings()
    connection = initialize_database(_database_url())
    try:
        account = get_account(connection, account_id)
        if account is None:
            output_error(f"account not found: {account_id}", "not_found")
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
@click.option("--account-id", required=True, help="Sending account ID.")
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
    account_id: str,
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
    from mailpilot.database import get_account, get_workflow, initialize_database
    from mailpilot.gmail import GmailClient
    from mailpilot.settings import get_settings

    if not body.strip():
        output_error("body cannot be empty", "validation_error")

    settings = get_settings()
    connection = initialize_database(_database_url())
    try:
        account = get_account(connection, account_id)
        if account is None:
            output_error(f"account not found: {account_id}", "not_found")
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


@activity.command("create")
@click.option("--contact-id", default=None, help="Contact ID.")
@click.option("--company-id", default=None, help="Optional company ID.")
@click.option(
    "--type",
    "activity_type",
    required=True,
    type=click.Choice(_ACTIVITY_TYPES),
    help="Activity type.",
)
@click.option("--summary", required=True, help="One-line description.")
@click.option("--detail", default=None, help="JSON detail payload.")
def activity_create(
    contact_id: str | None,
    company_id: str | None,
    activity_type: str,
    summary: str,
    detail: str | None,
) -> None:
    """Create an activity event. At least one of --contact-id / --company-id."""
    from mailpilot.database import (
        create_activity,
        get_company,
        get_contact,
        initialize_database,
    )

    if not summary.strip():
        output_error("summary cannot be empty", "validation_error")
    if contact_id is None and company_id is None:
        output_error(
            "at least one of --contact-id or --company-id is required",
            "validation_error",
        )
    detail_dict: dict[str, object] = json.loads(detail) if detail else {}
    connection = initialize_database(_database_url())
    try:
        if contact_id is not None and get_contact(connection, contact_id) is None:
            output_error(f"contact not found: {contact_id}", "not_found")
        if company_id is not None and get_company(connection, company_id) is None:
            output_error(f"company not found: {company_id}", "not_found")
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
@click.option("--contact-id", default=None, help="Filter by contact ID.")
@click.option("--company-id", default=None, help="Filter by company ID.")
@click.option(
    "--type",
    "activity_type",
    default=None,
    type=click.Choice(_ACTIVITY_TYPES),
    help="Filter by activity type.",
)
@click.option("--limit", default=100, help="Maximum results.")
@click.option("--since", default=None, help="ISO datetime lower bound.")
def activity_list(
    contact_id: str | None,
    company_id: str | None,
    activity_type: str | None,
    limit: int,
    since: str | None,
) -> None:
    """List activities (requires --contact-id or --company-id)."""
    from mailpilot.database import (
        get_company,
        get_contact,
        initialize_database,
        list_activities,
    )

    if contact_id is None and company_id is None:
        output_error(
            "at least one of --contact-id or --company-id is required",
            "missing_filter",
        )
    connection = initialize_database(_database_url())
    try:
        if contact_id is not None and get_contact(connection, contact_id) is None:
            output_error(f"contact not found: {contact_id}", "not_found")
        if company_id is not None and get_company(connection, company_id) is None:
            output_error(f"company not found: {company_id}", "not_found")
        activities = list_activities(
            connection,
            contact_id=contact_id,
            company_id=company_id,
            activity_type=activity_type,
            limit=limit,
            since=since,
        )
        output({"activities": [a.model_dump(mode="json") for a in activities]})
    finally:
        connection.close()


# -- Tag commands --------------------------------------------------------------


@main.group()
def tag() -> None:
    """Manage tags on contacts and companies."""


@tag.command("add")
@click.option("--contact-id", default=None, help="Contact ID.")
@click.option("--company-id", default=None, help="Company ID.")
@click.argument("name")
def tag_add(contact_id: str | None, company_id: str | None, name: str) -> None:
    """Add a tag to a contact or company."""
    from mailpilot.database import (
        _normalize_tag_name,  # pyright: ignore[reportPrivateUsage]
        add_company_tag,
        add_contact_tag,
        get_company,
        get_contact,
        initialize_database,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    if not name.strip():
        output_error("tag name cannot be empty", "validation_error")
    if (contact_id is None) == (company_id is None):
        output_error(
            "exactly one of --contact-id or --company-id is required",
            "validation_error",
        )
    connection = initialize_database(_database_url())
    try:
        if contact_id is not None:
            if get_contact(connection, contact_id) is None:
                output_error(f"contact {contact_id} not found", "not_found")
            owner = ("contact", contact_id)
        else:
            assert company_id is not None
            if get_company(connection, company_id) is None:
                output_error(f"company {company_id} not found", "not_found")
            owner = ("company", company_id)
        with cli_mutation(
            "tag", "add", name=name, owner_type=owner[0], owner_id=owner[1]
        ):
            if contact_id is not None:
                try:
                    created = add_contact_tag(
                        connection, contact_id=contact_id, name=name
                    )
                except ValueError as exc:
                    output_error(str(exc), "validation_error")
            else:
                assert company_id is not None
                try:
                    created = add_company_tag(
                        connection, company_id=company_id, name=name
                    )
                except ValueError as exc:
                    output_error(str(exc), "validation_error")
            if created is None:
                normalized = _normalize_tag_name(name)
                output_error(
                    f"tag '{normalized}' already exists on {owner[0]} {owner[1]}",
                    "already_exists",
                )
            operator_event(
                "tag.add",
                name=created.name,
                owner_type=owner[0],
                owner_id=owner[1],
                changed=["name"],
            )
            output_entity("tag", created)
    finally:
        connection.close()


@tag.command("disable")
@click.option("--contact-id", default=None, help="Contact ID.")
@click.option("--company-id", default=None, help="Company ID.")
@click.argument("name")
@click.option(
    "--reason",
    required=True,
    help="Explanation written to disabled_reason and the tag_disabled activity.",
)
def tag_disable(
    contact_id: str | None, company_id: str | None, name: str, reason: str
) -> None:
    """Soft-disable a tag on a contact or company (§V.10 tag coverage).

    Flips ``disabled_reason`` on the active tag row and appends a
    ``tag_disabled`` activity carrying the reason. Disabled is terminal --
    re-adding the same name means creating a fresh row via ``tag add``.
    """
    from mailpilot.database import (
        _normalize_tag_name,  # pyright: ignore[reportPrivateUsage]
        disable_company_tag,
        disable_contact_tag,
        get_company,
        get_contact,
        initialize_database,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    if reason.strip() == "":
        output_error("reason cannot be empty", "validation_error")
    if (contact_id is None) == (company_id is None):
        output_error(
            "exactly one of --contact-id or --company-id is required",
            "validation_error",
        )
    connection = initialize_database(_database_url())
    try:
        if contact_id is not None:
            if get_contact(connection, contact_id) is None:
                output_error(f"contact {contact_id} not found", "not_found")
            owner = ("contact", contact_id)
        else:
            assert company_id is not None
            if get_company(connection, company_id) is None:
                output_error(f"company {company_id} not found", "not_found")
            owner = ("company", company_id)
        with cli_mutation(
            "tag", "disable", entity_id=name, owner_type=owner[0], owner_id=owner[1]
        ):
            if contact_id is not None:
                try:
                    updated = disable_contact_tag(
                        connection,
                        contact_id=contact_id,
                        name=name,
                        reason=reason,
                    )
                except ValueError as exc:
                    output_error(str(exc), "validation_error")
            else:
                assert company_id is not None
                try:
                    updated = disable_company_tag(
                        connection,
                        company_id=company_id,
                        name=name,
                        reason=reason,
                    )
                except ValueError as exc:
                    output_error(str(exc), "validation_error")
            if updated is None:
                normalized = _normalize_tag_name(name)
                output_error(
                    f"tag '{normalized}' not found on {owner[0]} {owner[1]}",
                    "not_found",
                )
            operator_event(
                "tag.disable",
                entity_id=updated.name,
                owner_type=owner[0],
                owner_id=owner[1],
                changed=["disabled_reason"],
            )
            output_entity("tag", updated)
    finally:
        connection.close()


@tag.command("list")
@click.option("--contact-id", default=None, help="Contact ID.")
@click.option("--company-id", default=None, help="Company ID.")
@click.option("--limit", default=100, help="Maximum results.")
@click.option("--since", default=None, help="ISO datetime lower bound on created_at.")
@click.option(
    "--include-disabled",
    is_flag=True,
    default=False,
    help="Include rows whose disabled_reason is set (default: active only).",
)
def tag_list(
    contact_id: str | None,
    company_id: str | None,
    limit: int,
    since: str | None,
    include_disabled: bool,
) -> None:
    """List tags on a contact or company."""
    from mailpilot.database import (
        get_company,
        get_contact,
        initialize_database,
        list_tags,
    )

    if (contact_id is None) == (company_id is None):
        output_error(
            "exactly one of --contact-id or --company-id is required",
            "validation_error",
        )
    connection = initialize_database(_database_url())
    try:
        if contact_id is not None:
            if get_contact(connection, contact_id) is None:
                output_error(f"contact {contact_id} not found", "not_found")
            tags = list_tags(
                connection,
                contact_id=contact_id,
                limit=limit,
                since=since,
                include_disabled=include_disabled,
            )
        else:
            assert company_id is not None
            if get_company(connection, company_id) is None:
                output_error(f"company {company_id} not found", "not_found")
            tags = list_tags(
                connection,
                company_id=company_id,
                limit=limit,
                since=since,
                include_disabled=include_disabled,
            )
        output({"tags": [t.model_dump(mode="json") for t in tags]})
    finally:
        connection.close()


@tag.command("search")
@click.argument("name")
@click.option(
    "--type",
    "owner",
    default=None,
    type=click.Choice(["contact", "company"]),
    help="Filter by owner type.",
)
@click.option("--limit", default=100, help="Maximum results.")
@click.option(
    "--include-disabled",
    is_flag=True,
    default=False,
    help="Include rows whose disabled_reason is set (default: active only).",
)
def tag_search(
    name: str, owner: str | None, limit: int, include_disabled: bool
) -> None:
    """Search tags by name."""
    from mailpilot.database import initialize_database, search_tags

    connection = initialize_database(_database_url())
    try:
        tags = search_tags(
            connection,
            name=name,
            owner=owner,
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
@click.option("--contact-id", default=None, help="Contact ID.")
@click.option("--company-id", default=None, help="Company ID.")
@click.option("--body", required=True, help="Note text.")
def note_add(contact_id: str | None, company_id: str | None, body: str) -> None:
    """Add a note to a contact or company."""
    from mailpilot.database import (
        add_company_note,
        add_contact_note,
        get_company,
        get_contact,
        initialize_database,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    if not body.strip():
        output_error("note body cannot be empty", "validation_error")
    if (contact_id is None) == (company_id is None):
        output_error(
            "exactly one of --contact-id or --company-id is required",
            "validation_error",
        )
    connection = initialize_database(_database_url())
    try:
        if contact_id is not None:
            if get_contact(connection, contact_id) is None:
                output_error(f"contact {contact_id} not found", "not_found")
            owner = ("contact", contact_id)
        else:
            assert company_id is not None
            if get_company(connection, company_id) is None:
                output_error(f"company {company_id} not found", "not_found")
            owner = ("company", company_id)
        with cli_mutation("note", "add", owner_type=owner[0], owner_id=owner[1]):
            if contact_id is not None:
                created = add_contact_note(connection, contact_id=contact_id, body=body)
            else:
                assert company_id is not None
                created = add_company_note(connection, company_id=company_id, body=body)
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
@click.option("--contact-id", default=None, help="Contact ID.")
@click.option("--company-id", default=None, help="Company ID.")
@click.option("--limit", default=100, help="Maximum results.")
@click.option("--since", default=None, help="ISO datetime lower bound.")
def note_list(
    contact_id: str | None, company_id: str | None, limit: int, since: str | None
) -> None:
    """List notes on a contact or company."""
    from mailpilot.database import (
        get_company,
        get_contact,
        initialize_database,
        list_notes,
    )

    if (contact_id is None) == (company_id is None):
        output_error(
            "exactly one of --contact-id or --company-id is required",
            "validation_error",
        )
    connection = initialize_database(_database_url())
    try:
        if contact_id is not None:
            if get_contact(connection, contact_id) is None:
                output_error(f"contact {contact_id} not found", "not_found")
            notes = list_notes(
                connection, contact_id=contact_id, limit=limit, since=since
            )
        else:
            assert company_id is not None
            if get_company(connection, company_id) is None:
                output_error(f"company {company_id} not found", "not_found")
            notes = list_notes(
                connection, company_id=company_id, limit=limit, since=since
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
@click.option("--account-id", required=True, help="Owning Gmail account ID.")
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
    account_id: str,
    objective: str | None,
    instructions: str | None,
    instructions_file: str | None,
    theme: str | None,
    draft: bool,
) -> None:
    """Create a new workflow."""
    from mailpilot.database import get_account, initialize_database
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
    connection = initialize_database(_database_url())
    try:
        if get_account(connection, account_id) is None:
            output_error(f"account not found: {account_id}", "not_found")
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
    connection = initialize_database(_database_url())
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
@click.option("--account-id", default=None, help="Filter by account ID.")
@click.option(
    "--status",
    default=None,
    type=click.Choice(["draft", "active", "paused"]),
    help="Filter by workflow status.",
)
@click.option(
    "--type",
    "workflow_type",
    default=None,
    type=click.Choice(["inbound", "outbound"]),
    help="Filter by workflow direction.",
)
@click.option(
    "--template",
    default=None,
    type=click.Choice(["outbound-general", "inbound-general", "inbound-google-drive"]),
    help="Filter by workflow template.",
)
@click.option("--limit", default=100, help="Maximum results.")
@click.option("--since", default=None, help="ISO datetime lower bound on created_at.")
def workflow_list(
    account_id: str | None,
    status: str | None,
    workflow_type: str | None,
    template: str | None,
    limit: int,
    since: str | None,
) -> None:
    """List workflows as summaries."""
    from mailpilot.database import get_account, initialize_database, list_workflows

    connection = initialize_database(_database_url())
    try:
        if account_id is not None and get_account(connection, account_id) is None:
            output_error(f"account not found: {account_id}", "not_found")
        workflows = list_workflows(
            connection,
            account_id=account_id,
            status=status,
            workflow_type=workflow_type,
            template=template,
            limit=limit,
            since=since,
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

    connection = initialize_database(_database_url())
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

    connection = initialize_database(_database_url())
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


_WORKFLOW_EXPORT_FIELDS = ("name", "template", "objective", "instructions", "theme")


@workflow.command("export")
@click.option("--account-id", required=True, help="Owning Gmail account ID.")
def workflow_export(account_id: str) -> None:
    """Export workflows for an account as a declarative JSON payload."""
    from mailpilot.database import (
        get_account,
        initialize_database,
        list_workflows_full,
    )

    connection = initialize_database(_database_url())
    try:
        if get_account(connection, account_id) is None:
            output_error(f"account not found: {account_id}", "not_found")
        workflows = list_workflows_full(connection, account_id)
        payload = [
            {field: getattr(w, field) for field in _WORKFLOW_EXPORT_FIELDS}
            for w in workflows
        ]
        output({"workflows": payload})
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


@workflow.command("import")
@click.option("--account-id", required=True, help="Owning Gmail account ID.")
@click.option(
    "--file",
    "file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to JSON payload. If omitted, read from stdin.",
)
def workflow_import(account_id: str, file: str | None) -> None:
    """Import workflows for an account from a declarative JSON payload (§V.63).

    The payload is the same shape produced by ``workflow export``: a list of
    objects with ``name``, ``template``, ``objective``, ``instructions``,
    ``theme``. Upsert is keyed on ``(account_id, name)``. Workflows absent
    from the DB are created (and activated when both ``objective`` and
    ``instructions`` are non-empty). Workflows already present are updated
    in-place for changed fields only; ``template`` differences emit a per-row
    ``template_immutable`` error and the batch continues. ``status`` is never
    written by import -- it remains operational state owned by start/stop.
    """
    import pathlib
    import sys

    from mailpilot.database import (
        get_account,
        initialize_database,
        list_workflows_full,
    )
    from mailpilot.operator_log import cli_mutation

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
            "payload must be a JSON array of workflow objects", "validation_error"
        )

    connection = initialize_database(_database_url())
    try:
        if get_account(connection, account_id) is None:
            output_error(f"account not found: {account_id}", "not_found")
        with cli_mutation(
            "workflow",
            "import",
            account_id=account_id,
            row_count=len(entries),
        ):
            existing = {w.name: w for w in list_workflows_full(connection, account_id)}
            results = [
                _import_workflow_row(connection, account_id, existing, entry)
                for entry in entries
            ]
            output({"workflows": results})
    finally:
        connection.close()


# -- Template commands ---------------------------------------------------------


@main.group()
def template() -> None:
    """Inspect built-in workflow templates (read-only, code-defined)."""


@template.command("list")
@click.option(
    "--direction",
    default=None,
    type=click.Choice(["inbound", "outbound"]),
    help="Filter by template direction.",
)
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
@click.option("--contact-id", required=True, help="Contact ID.")
@click.option(
    "--scheduled-at",
    "scheduled_at",
    default=None,
    help=(
        "ISO 8601 timestamp for scheduled first reach-out (outbound workflows "
        "only). Inserts a pending task drained by the run loop per SPEC §V.32."
    ),
)
def enrollment_add(workflow_id: str, contact_id: str, scheduled_at: str | None) -> None:
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
        get_contact,
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

    connection = initialize_database(_database_url())
    try:
        workflow = get_workflow(connection, workflow_id)
        if workflow is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        if scheduled_iso is not None and workflow.type != "outbound":
            output_error(
                "--scheduled-at only valid for outbound workflows",
                "invalid_state",
            )
        contact = get_contact(connection, contact_id)
        if contact is None:
            output_error(f"contact not found: {contact_id}", "not_found")
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
    connection = initialize_database(_database_url())
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
    """Soft-disable an enrollment via terminal lifecycle exit (§V.10, §V.15).

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
    connection = initialize_database(_database_url())
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
@click.option("--workflow-id", default=None, help="Filter by workflow ID.")
@click.option("--contact-id", default=None, help="Filter by contact ID.")
@click.option(
    "--status",
    default=None,
    type=click.Choice(["active", "paused", "disabled"]),
    help="Filter by enrollment status.",
)
@click.option("--limit", default=100, help="Maximum results.")
@click.option("--since", default=None, help="ISO datetime lower bound on updated_at.")
def enrollment_list(
    workflow_id: str | None,
    contact_id: str | None,
    status: str | None,
    limit: int,
    since: str | None,
) -> None:
    """List enrollments as summaries. Filter by workflow, contact, or both."""
    from mailpilot.database import (
        get_contact,
        get_workflow,
        initialize_database,
        list_enrollments_detailed,
    )

    connection = initialize_database(_database_url())
    try:
        if workflow_id is not None and get_workflow(connection, workflow_id) is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        if contact_id is not None and get_contact(connection, contact_id) is None:
            output_error(f"contact not found: {contact_id}", "not_found")
        rows = list_enrollments_detailed(
            connection,
            workflow_id=workflow_id,
            contact_id=contact_id,
            status=status,
            limit=limit,
            since=since,
        )
        output({"enrollments": [r.model_dump(mode="json") for r in rows]})
    finally:
        connection.close()


@enrollment.command("update")
@click.argument("enrollment_id")
@click.option(
    "--status",
    required=True,
    type=click.Choice(["active", "paused"]),
    help="New enrollment status (active or paused).",
)
@click.option("--reason", default=None, help="Status reason.")
def enrollment_update(enrollment_id: str, status: str, reason: str | None) -> None:
    """Update enrollment operational status (active or paused).

    Outcomes (completed, failed) are recorded as activity by the agent
    via record_enrollment_outcome -- not via this command.
    """
    from mailpilot.database import (
        create_activity,
        get_contact,
        get_enrollment_by_id,
        initialize_database,
        update_enrollment,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url())
    try:
        before = get_enrollment_by_id(connection, enrollment_id)
        if before is None:
            output_error("enrollment not found", "not_found")
        fields: dict[str, object] = {"status": status}
        if reason is not None:
            fields["reason"] = reason
        with cli_mutation("enrollment", "update", entity_id=enrollment_id):
            updated = update_enrollment(connection, enrollment_id, **fields)
            if updated is None:
                output_error("enrollment not found", "not_found")
            if before.status != status:
                contact = get_contact(connection, before.contact_id)
                activity_type = (
                    "enrollment_paused" if status == "paused" else "enrollment_resumed"
                )
                create_activity(
                    connection,
                    contact_id=before.contact_id,
                    activity_type=activity_type,
                    summary=reason or f"Enrollment {status}",
                    detail={"reason": reason or ""},
                    company_id=contact.company_id if contact is not None else None,
                    workflow_id=before.workflow_id,
                    enrollment_id=before.id,
                )
            changed = [
                field
                for field in ("status", "reason")
                if getattr(before, field) != getattr(updated, field)
            ]
            operator_event(
                "enrollment.update",
                entity_id=enrollment_id,
                changed=changed,
            )
            output_entity("enrollment", updated)
    finally:
        connection.close()


# -- Task commands -------------------------------------------------------------


@main.group()
def task() -> None:
    """Manage deferred agent tasks."""


@task.command("list")
@click.option("--workflow-id", default=None, help="Filter by workflow ID.")
@click.option("--contact-id", default=None, help="Filter by contact ID.")
@click.option(
    "--status",
    default=None,
    type=click.Choice(["pending", "completed", "failed", "cancelled"]),
    help="Filter by task status.",
)
@click.option("--limit", default=100, help="Maximum results.")
@click.option("--since", default=None, help="ISO datetime lower bound on scheduled_at.")
def task_list(
    workflow_id: str | None,
    contact_id: str | None,
    status: str | None,
    limit: int,
    since: str | None,
) -> None:
    """List tasks as summaries with optional filters."""
    from mailpilot.database import (
        get_contact,
        get_workflow,
        initialize_database,
        list_tasks,
    )

    connection = initialize_database(_database_url())
    try:
        if workflow_id is not None and get_workflow(connection, workflow_id) is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        if contact_id is not None and get_contact(connection, contact_id) is None:
            output_error(f"contact not found: {contact_id}", "not_found")
        tasks = list_tasks(
            connection,
            workflow_id=workflow_id,
            contact_id=contact_id,
            status=status,
            limit=limit,
            since=since,
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

    connection = initialize_database(_database_url())
    try:
        cancelled = cancel_task(connection, task_id)
        if cancelled is None:
            output_error(f"task not found or not pending: {task_id}", "not_found")
        output_entity("task", cancelled)
    finally:
        connection.close()


@task.command("retry")
@click.option("--task-id", required=True, help="Task ID to retry.")
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

    connection = initialize_database(_database_url())
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
