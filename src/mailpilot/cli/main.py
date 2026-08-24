"""CLI interface for MailPilot.

Startup-critical: only ``click`` is imported at module level. All heavy
dependencies (logfire, psycopg, httpx, pydantic, mailpilot.database,
mailpilot.settings) are lazy-imported inside command functions so that
``--help`` / ``--version`` stay fast (~50 ms).
When adding new commands, keep imports inside the function body.
"""
# pyright: reportPrivateUsage=false, reportUnusedFunction=false

from __future__ import annotations

import json
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Literal, NoReturn, TypedDict, overload

import click

if TYPE_CHECKING:
    from logfire import ScrubMatch

    from mailpilot.models import (
        Account,
        Company,
        Contact,
        Workflow,
    )

# Hex digit set for the UUID-shape probe in _looks_like_uuid (natural-key vs id).
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _database_url() -> str:
    """Bootstrap-only database URL (env / .env / default). Not ``app_config``."""
    from mailpilot.settings import bootstrap_database_url

    return bootstrap_database_url()


@contextmanager
def _db(*, mutate: bool = False) -> Generator[Any]:
    """Yield a CLI database connection.

    Lazy-imports ``initialize_database`` so ``--help`` stays click-only.
    ``mutate=True`` is the write-path schema gate. Successful mutate
    blocks commit before close; exceptions and ``output_error`` skip
    commit so the transaction rolls back.
    """
    from mailpilot import cli as _cli
    from mailpilot.database import initialize_database

    connection = initialize_database(
        _cli._database_url(),
        require_current_schema=mutate,
    )
    try:
        yield connection
        if mutate:
            connection.commit()
    finally:
        connection.close()


def scrub_tool_response_callback(match: ScrubMatch) -> Any:
    """Exempt agent tool-return payloads from default Logfire scrubbing.

    Pydantic-AI ``execute_tool`` spans carry the structured tool return value
    under the ``gen_ai.tool.call.result`` attribute (instrumentation format 5;
    named ``tool_response`` before pydantic-ai v2). Without this exemption the
    default substring matcher redacts strings like ``"authorized"`` inside KB
    markdown, making §V.57 grounding regressions unverifiable from traces
    alone. Per §V.55, agent tool outputs are non-sensitive by design.
    """
    if match.path[:2] == ("attributes", "gen_ai.tool.call.result"):
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
        environment="development" if settings.environment == "dev" else "production",
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


def _record_count(data: dict[str, Any]) -> int:
    """Count the records a payload displays (§V.4).

    An array-bearing payload -- exactly one top-level key whose value is a
    list (`{"accounts": [...]}`) -- counts its array length. Every other
    shape (single entity, aggregate `stats`/`check`, `status`, `db` status
    objects, multi-key payloads like `config get`) counts as one record.
    The single-key gate keeps a list-typed config *value* under a multi-key
    payload from being miscounted as an array payload.
    """
    if len(data) == 1:
        sole_value = next(iter(data.values()))
        if isinstance(sole_value, list):
            return len(sole_value)
    return 1


def output(data: dict[str, Any], *, record_count: int | None = None) -> None:
    r"""Print structured JSON response to stdout.

    Always RFC 8259 compliant: control characters (\n, \r, \t, etc.) inside
    string values are escaped, so downstream `json.loads` / `jq` callers never
    trip on raw control bytes. `ensure_ascii=False` keeps non-ASCII glyphs
    (em-dashes, accented characters) readable instead of `\uXXXX`-encoded.
    Every ok:true envelope carries top-level `record_count` per §V.4.
    ``record_count`` overrides the inferred count for multi-key payloads whose
    displayed records live in one array key (e.g. ``workflow import`` carries
    ``applied``/``rejected`` beside ``workflows`` per §V.103).
    """
    click.echo(
        json.dumps(
            {
                **data,
                "record_count": (
                    record_count if record_count is not None else _record_count(data)
                ),
                "ok": True,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def output_entity(key: str, model: Any, **extra: object) -> None:
    """Emit a single entity wrapped under its singular key.

    Per SPEC §V.4: `<entity> view|create|update` -> `{"<singular>": {...}, "ok": true}`.
    Symmetric with `output({"<plural>": [...]})` used by list commands.
    ``extra`` merges top-level fields (e.g. ``created`` for §V.147 upsert).
    """
    output({key: model.model_dump(mode="json"), **extra})


def _output_company_create_entity(model: Any, *, created: bool) -> None:
    """Emit company create/upsert envelope with ``has_profile`` (§V.167)."""
    payload = model.model_dump(mode="json")
    payload["has_profile"] = payload.get("profile") is not None
    output({"company": payload, "created": created})


def _emit_formatted(
    envelope_key: str,
    payload: dict[str, Any],
    *,
    rows: list[dict[str, Any]],
    output_format: str,
    out_path: str | None,
) -> None:
    """Emit JSON envelope (default) or opt-in table/csv/ndjson (§V.156).

    ``csv`` / ``ndjson`` prefer ``--out`` (file write + status envelope on
    stdout). Without ``--out``, stream rows to stdout. ``table`` is always
    human columns on stdout.
    """
    fmt = output_format.lower()
    if fmt == "json":
        output({envelope_key: payload})
        return
    if fmt == "table":
        if not rows:
            click.echo("(no rows)")
            return
        headers = list(rows[0].keys())
        click.echo("\t".join(headers))
        for row in rows:
            click.echo(
                "\t".join(
                    "" if row.get(h) is None else str(row.get(h)) for h in headers
                )
            )
        return
    # csv / ndjson
    import csv
    import pathlib

    if fmt == "ndjson":
        lines = [json.dumps(r, default=str, ensure_ascii=False) for r in rows]
        body = "\n".join(lines) + ("\n" if lines else "")
    elif not rows:
        body = ""
    else:
        headers = list(rows[0].keys())
        from io import StringIO

        buf = StringIO()
        writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {h: "" if row.get(h) is None else row.get(h) for h in headers}
            )
        body = buf.getvalue()
    if out_path is not None:
        path = pathlib.Path(out_path)
        path.write_text(body, encoding="utf-8")
        output(
            {
                envelope_key: {
                    "path": str(path),
                    "format": fmt,
                    "record_count": len(rows),
                }
            },
            record_count=len(rows),
        )
        return
    click.echo(body, nl=False)


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


@overload
def _resolve[Row](
    connection: Any,
    ref: str,
    *,
    get_id: Callable[[Any, str], Row | None],
    get_key: Callable[[Any, str], Row | None],
    noun: str,
    missing: Literal["error"] = "error",
) -> Row: ...


@overload
def _resolve[Row](
    connection: Any,
    ref: str,
    *,
    get_id: Callable[[Any, str], Row | None],
    get_key: Callable[[Any, str], Row | None],
    noun: str,
    missing: Literal["none"],
) -> Row | None: ...


def _resolve[Row](
    connection: Any,
    ref: str,
    *,
    get_id: Callable[[Any, str], Row | None],
    get_key: Callable[[Any, str], Row | None],
    noun: str,
    missing: Literal["error", "none"] = "error",
) -> Row | None:
    """Polymorphic natural-key or UUID lookup (§V.107).

    UUID-shaped refs resolve via ``get_id``; every other value via ``get_key``.
    Hard miss (``missing="error"``) exits ``not_found``; soft miss
    (``missing="none"``) returns ``None``. Soft vs hard differs only on miss.
    """
    row = get_id(connection, ref) if _looks_like_uuid(ref) else get_key(connection, ref)
    if row is not None:
        return row
    if missing == "none":
        return None
    output_error(f"{noun} not found: {ref}", "not_found")


def _resolve_account(connection: Any, account_ref: str | None) -> Account:
    """Resolve an account reference (email or UUID) to its full row (§V.107).

    A UUID-shaped ref resolves by id; any other value resolves by the email
    natural key (§V.90), case-insensitively via ``get_account_by_email``. A
    None ref (the flag was omitted) exits ``validation_error``; an unknown key
    exits ``not_found`` per §V.94.
    """
    from mailpilot.database import get_account, get_account_by_email

    if account_ref is None:
        output_error("--account-email is required", "validation_error")
    return _resolve(
        connection,
        account_ref,
        get_id=get_account,
        get_key=get_account_by_email,
        noun="account",
    )


@overload
def _resolve_company(
    connection: Any, company_ref: str, *, missing: Literal["error"] = "error"
) -> Company: ...


@overload
def _resolve_company(
    connection: Any, company_ref: str, *, missing: Literal["none"]
) -> Company | None: ...


def _resolve_company(
    connection: Any,
    company_ref: str,
    *,
    missing: Literal["error", "none"] = "error",
) -> Company | None:
    """Resolve a company reference (domain or UUID) to its full row (§V.107).

    A UUID-shaped ref resolves by id; any other value resolves by the domain
    natural key (§V.90) via ``get_company_by_domain``. Hard miss exits
    ``not_found`` per §V.94; soft miss (``missing="none"``) returns ``None``.
    """
    from mailpilot.database import get_company, get_company_by_domain

    return _resolve(
        connection,
        company_ref,
        get_id=get_company,
        get_key=get_company_by_domain,
        noun="company",
        missing=missing,
    )


@overload
def _resolve_contact(
    connection: Any, contact_ref: str, *, missing: Literal["error"] = "error"
) -> Contact: ...


@overload
def _resolve_contact(
    connection: Any, contact_ref: str, *, missing: Literal["none"]
) -> Contact | None: ...


def _resolve_contact(
    connection: Any,
    contact_ref: str,
    *,
    missing: Literal["error", "none"] = "error",
) -> Contact | None:
    """Resolve a contact reference (email or UUID) to its full row (§V.107).

    A UUID-shaped ref resolves by id; any other value resolves by the email
    natural key (§V.90) via ``get_contact_by_email``. Hard miss exits
    ``not_found`` per §V.94; soft miss (``missing="none"``) returns ``None``.
    """
    from mailpilot.database import get_contact, get_contact_by_email

    return _resolve(
        connection,
        contact_ref,
        get_id=get_contact,
        get_key=get_contact_by_email,
        noun="contact",
        missing=missing,
    )


def _resolve_company_id(connection: Any, company_ref: str) -> str:
    """Resolve a company domain or UUID to its id; miss → ``not_found``."""
    return _resolve_company(connection, company_ref).id


def _resolve_contact_id(connection: Any, contact_ref: str) -> str:
    """Resolve a contact email or UUID to its id; miss → ``not_found``."""
    return _resolve_contact(connection, contact_ref).id


def _resolve_workflow(connection: Any, workflow_ref: str) -> Workflow:
    """Resolve a workflow name or UUID to its full row; miss → ``not_found``."""
    from mailpilot.database import get_workflow, get_workflow_by_name

    return _resolve(
        connection,
        workflow_ref,
        get_id=get_workflow,
        get_key=get_workflow_by_name,
        noun="workflow",
    )


def _resolve_workflow_id(connection: Any, workflow_ref: str) -> str:
    """Resolve a workflow name or UUID to its id; miss → ``not_found``."""
    return _resolve_workflow(connection, workflow_ref).id


def _resolve_task_scope(
    connection: Any,
    workflow_id: str | None,
    contact_email: str | None,
) -> tuple[str | None, str | None]:
    """Resolve optional workflow + contact filters to ids (§V.180)."""
    resolved_workflow_id = (
        _resolve_workflow_id(connection, workflow_id)
        if workflow_id is not None
        else None
    )
    contact_id = (
        _resolve_contact(connection, contact_email).id
        if contact_email is not None
        else None
    )
    return resolved_workflow_id, contact_id


def _resolve_tag(connection: Any, tag_ref: str) -> Any:
    """Resolve a tag reference (name or UUID) to its vocabulary row (§V.116).

    Tags are addressed by their globally unique name (operators never paste
    tag ids); a UUID-shaped ref still resolves by id so a tag id round-trips
    from one command's output into the next (§V.107). An undefined or malformed
    name exits ``not_found`` (§V.94) -- ``tag add`` never auto-creates the tag.
    """
    from mailpilot.database import get_tag, get_tag_by_name

    def _get_id(conn: Any, ref: str) -> Any:
        try:
            return get_tag(conn, ref)
        except ValueError:
            return None

    def _get_key(conn: Any, ref: str) -> Any:
        try:
            return get_tag_by_name(conn, ref)
        except ValueError:
            return None

    return _resolve(
        connection,
        tag_ref,
        get_id=_get_id,
        get_key=_get_key,
        noun="tag",
    )


def _resolve_tag_ids(connection: Any, tag_refs: tuple[str, ...]) -> list[str]:
    """Resolve repeatable ``--tag`` refs to vocabulary ids (§V.116)."""
    return [_resolve_tag(connection, name).id for name in tag_refs]


class _CompanyCohortKwargs(TypedDict):
    """Resolved ``--tag``/``--no-tag`` ids and include-disabled."""

    tag: list[str] | None
    exclude_tags: list[str]
    include_disabled: bool


def _company_cohort_kwargs(
    connection: Any,
    tag: tuple[str, ...],
    no_tag: tuple[str, ...],
    include_disabled: bool,
    status: str | None,
) -> _CompanyCohortKwargs:
    """Resolve ``--tag``/``--no-tag`` ids and include-disabled."""
    return {
        "tag": _resolve_tag_ids(connection, tag) or None,
        "exclude_tags": _resolve_tag_ids(connection, no_tag),
        "include_disabled": include_disabled or status == "disabled",
    }


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


def _print_skill_help(ctx: click.Context, param: click.Parameter, value: bool) -> None:
    """Eager callback: emit the packaged SKILL.md body as top-level --help.

    Replaces Click's default root help. Runs before Click validates that a
    subcommand was given, so ``mailpilot --help`` works alone. Hard-fails
    with a stderr diagnostic when the package data is missing. Subcommand
    and verb ``--help`` stay Click-rendered (this callback is only on root).
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


def _version() -> str:
    """Render the CLI version, marking editable installs as dev builds.

    A PEP 610 direct_url.json with dir_info.editable true means the package
    was installed with `pip/uv install -e` from a checkout, so the running
    code can differ from the released wheel; render `<version>+dev (<path>)`
    to keep dev output from masquerading as a release. Wheel installs carry
    no direct_url.json (or editable is absent) and render plain `<version>`.
    """
    from mailpilot import cli as _cli

    dist = _cli.distribution("mailpilot-crm")
    raw = dist.read_text("direct_url.json")
    if raw is not None:
        direct = json.loads(raw)
        if direct.get("dir_info", {}).get("editable"):
            checkout = direct.get("url", "").removeprefix("file://")
            return f"{dist.version}+dev ({checkout})"
    return dist.version


@click.group(add_help_option=False)
@click.version_option(version=_version(), prog_name="mailpilot")
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
    "--help",
    is_flag=True,
    default=False,
    is_eager=True,
    expose_value=False,
    callback=_print_skill_help,
    help="Print the packaged SKILL.md body and exit.",
)
@click.pass_context
def main(ctx: click.Context, debug: bool) -> None:
    """MailPilot -- CRM for cold email outreach via Gmail."""
    import sys

    ctx.ensure_object(dict)
    ctx.obj["debug"] = debug
    if ctx.invoked_subcommand is None:
        return
    # ``db init|migrate|check`` bootstrap the URL only; --help/--version
    # skip settings load entirely (§V.1).
    if ctx.invoked_subcommand == "db":
        return
    if "--help" in sys.argv or "--version" in sys.argv:
        return
    from mailpilot import cli as _cli

    _cli.configure_logging(debug=debug)


# -- Status command ------------------------------------------------------------


@main.command()
def status() -> None:
    """Show application state summary including sync loop status."""
    from mailpilot.database import get_status_payload
    from mailpilot.settings import get_settings

    settings = get_settings()
    with _db() as connection:
        output({"status": get_status_payload(connection, settings)})


@main.command()
def run() -> None:
    """Start the sync loop (Pub/Sub + task runner, foreground)."""
    from mailpilot.settings import get_settings
    from mailpilot.sync import start_sync_loop

    settings = get_settings()
    with _db(mutate=True) as connection:
        start_sync_loop(connection, settings)
