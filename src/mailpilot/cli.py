"""CLI interface for MailPilot.

Startup-critical: only ``click`` is imported at module level. All heavy
dependencies (logfire, psycopg, httpx, pydantic, mailpilot.database,
mailpilot.settings) are lazy-imported inside command functions so that
``--help`` / ``--version`` stay fast (~50 ms).
When adding new commands, keep imports inside the function body.
"""

from __future__ import annotations

import json
from importlib.metadata import distribution
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
# Caller-path taxonomy stored in task.context->>'trigger' (§V.26); shared by
# `task list --trigger` and `task stats --trigger`.
_TASK_TRIGGERS = ["enrollment_run", "enrollment_schedule", "task", "email", "manual"]
_MEETING_STATUSES = ["scheduled", "completed", "cancelled", "no_show"]
# Company list pipeline cohort filter (§V.138); derived, not a schema CHECK.
_COMPANY_PIPELINE_STATUSES = [
    "ready",
    "needs_contacts",
    "needs_profile",
    "disabled",
]


def _database_url() -> str:
    """Resolve the database URL from settings at call time (not import time)."""
    from mailpilot.settings import get_settings

    return str(get_settings().database_url)


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


def _batch_ok(ref: str) -> dict[str, object]:
    """One successful row for a §V.139 stdin batch envelope."""
    return {"ref": ref, "status": "ok"}


def _batch_error(ref: str, code: str, message: str) -> dict[str, object]:
    """One error row for a §V.139 stdin batch envelope."""
    return {"ref": ref, "status": "error", "error": code, "message": message}


def _emit_batch_results(results: list[dict[str, object]]) -> None:
    """Emit the §V.139 results envelope; exit 1 when any row is an error.

    Always writes the full stream to stdout with ``ok: true`` (partial success
    still reports every prior row). Exit 0 iff zero error rows; exit 1 if any
    error, without aborting mid-batch.
    """
    output({"results": results}, record_count=len(results))
    if any(row.get("status") == "error" for row in results):
        raise SystemExit(1)


def _read_stdin_ndjson_lines() -> list[tuple[int, str]]:
    """Read non-empty stdin lines as (1-based line number, stripped text)."""
    import sys

    lines: list[tuple[int, str]] = []
    for line_number, raw in enumerate(sys.stdin, start=1):
        stripped = raw.strip()
        if stripped:
            lines.append((line_number, stripped))
    return lines


def _parse_ndjson_object(
    line_number: int, line: str
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    """Parse one NDJSON line into an object, or a batch error row.

    Returns ``(payload, None)`` on success, ``(None, error_row)`` on failure.
    """
    try:
        parsed: object = json.loads(line)
    except json.JSONDecodeError as exc:
        return None, _batch_error(
            f"line:{line_number}",
            "validation_error",
            f"invalid JSON: {exc}",
        )
    if not isinstance(parsed, dict):
        return None, _batch_error(
            f"line:{line_number}",
            "validation_error",
            "NDJSON line must be a JSON object",
        )
    return parsed, None


def _lookup_company_soft(connection: Any, company_ref: str) -> Company | None:
    """Resolve company by domain or UUID without exiting on miss (§V.139 batch)."""
    from mailpilot.database import get_company, get_company_by_domain

    if _looks_like_uuid(company_ref):
        return get_company(connection, company_ref)
    return get_company_by_domain(connection, company_ref)


def _lookup_contact_soft(connection: Any, contact_ref: str) -> Contact | None:
    """Resolve contact by email or UUID without exiting on miss (§V.139 batch)."""
    from mailpilot.database import get_contact, get_contact_by_email

    if _looks_like_uuid(contact_ref):
        return get_contact(connection, contact_ref)
    return get_contact_by_email(connection, contact_ref)


def _required_nonempty_str(
    payload: dict[str, object], key: str, line_number: int
) -> tuple[str | None, str, dict[str, object] | None]:
    """Read a required non-empty string field from an NDJSON object.

    Returns ``(value, ref, None)`` on success or ``(None, ref, error_row)``
    on failure. ``ref`` prefers the field value when present, else ``line:N``.
    """
    raw = payload.get(key)
    ref = (
        str(raw).strip()
        if isinstance(raw, str) and raw.strip()
        else f"line:{line_number}"
    )
    if not isinstance(raw, str) or not raw.strip():
        return None, ref, _batch_error(ref, "validation_error", f"{key} is required")
    return raw.strip(), ref, None


def _optional_str_fields(
    payload: dict[str, object], keys: tuple[str, ...], ref: str
) -> tuple[dict[str, str | None] | None, dict[str, object] | None]:
    """Read optional string fields; first type error becomes a batch error row."""
    values: dict[str, str | None] = {}
    for key in keys:
        raw = payload.get(key)
        if raw is None:
            values[key] = None
            continue
        if not isinstance(raw, str):
            return None, _batch_error(
                ref, "validation_error", f"{key} must be a string"
            )
        values[key] = raw
    return values, None


def _company_disable_stdin_row(
    connection: Any, line_number: int, line: str
) -> dict[str, object]:
    """Process one ``company disable --stdin`` NDJSON line into a result row."""
    from mailpilot.database import disable_company
    from mailpilot.operator_log import operator_event

    payload, parse_error = _parse_ndjson_object(line_number, line)
    if parse_error is not None:
        return parse_error
    assert payload is not None
    domain, ref, domain_error = _required_nonempty_str(payload, "domain", line_number)
    if domain_error is not None:
        return domain_error
    assert domain is not None
    reason, _, reason_error = _required_nonempty_str(payload, "reason", line_number)
    if reason_error is not None:
        # Prefer domain as ref when reason is the only missing field.
        return _batch_error(ref, "validation_error", "reason is required")
    assert reason is not None
    company = _lookup_company_soft(connection, domain)
    if company is None:
        return _batch_error(domain, "not_found", f"company not found: {domain}")
    if company.disabled_reason is None:
        updated = disable_company(connection, company.id, reason)
        if updated is not None:
            operator_event(
                "company.disable",
                entity_id=company.id,
                changed=["disabled_reason"],
            )
    # Active disable, already-disabled no-op, and race no-op all report ok.
    return _batch_ok(domain)


def _run_company_disable_stdin() -> None:
    """Drive ``company disable --stdin`` NDJSON batch (§V.139)."""
    from mailpilot.database import initialize_database
    from mailpilot.operator_log import cli_mutation

    lines = _read_stdin_ndjson_lines()
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        with cli_mutation("company", "disable", mode="stdin", row_count=len(lines)):
            results = [
                _company_disable_stdin_row(connection, line_number, line)
                for line_number, line in lines
            ]
            _emit_batch_results(results)
    finally:
        connection.close()


def _parse_contact_create_fields(
    payload: dict[str, object], line_number: int
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    """Validate a contact-create NDJSON object into create kwargs + ref.

    Returns ``(fields, None)`` where fields includes ``ref`` and create args,
    or ``(None, error_row)``.
    """
    email, ref, email_error = _required_nonempty_str(payload, "email", line_number)
    if email_error is not None:
        return None, email_error
    assert email is not None
    ref = email.lower()
    optional, opt_error = _optional_str_fields(
        payload,
        ("first_name", "last_name", "title", "note", "company_domain"),
        ref,
    )
    if opt_error is not None:
        return None, opt_error
    assert optional is not None
    confidence_raw = payload.get("email_confidence")
    confidence: int | None
    if confidence_raw is None:
        confidence = None
    elif isinstance(confidence_raw, int) and not isinstance(confidence_raw, bool):
        confidence = confidence_raw
    else:
        return None, _batch_error(
            ref, "validation_error", "email_confidence must be an integer"
        )
    meta_raw = payload.get("meta")
    meta: dict[str, object] | None
    if meta_raw is None:
        meta = None
    elif isinstance(meta_raw, dict):
        meta = meta_raw
    else:
        return None, _batch_error(
            ref, "validation_error", "meta must be a JSON object"
        )
    upsert_raw = payload.get("upsert")
    if upsert_raw is None:
        upsert = False
    elif isinstance(upsert_raw, bool):
        upsert = upsert_raw
    else:
        return None, _batch_error(
            ref, "validation_error", "upsert must be a boolean"
        )
    return {
        "ref": ref,
        "email": email,
        "first_name": optional["first_name"],
        "last_name": optional["last_name"],
        "title": optional["title"],
        "note": optional["note"],
        "company_domain": optional["company_domain"],
        "email_confidence": confidence,
        "meta": meta,
        "upsert": upsert,
    }, None


def _contact_upsert_fields(
    *,
    title: str | None,
    email_confidence: int | None,
    company_id: str | None,
    company_domain_set: bool,
    verification_meta: dict[str, object] | None,
    meta_set: bool,
) -> dict[str, object]:
    """Build field-selective contact update kwargs for §V.147 upsert.

    Only supplied create flags are included — omitted fields are never
    clobbered. ``first_name`` / ``last_name`` are insert-only.
    """
    fields: dict[str, object] = {}
    if title is not None:
        fields["title"] = title
    if email_confidence is not None:
        fields["email_confidence"] = email_confidence
    if company_domain_set:
        fields["company_id"] = company_id
    if meta_set:
        fields["verification_meta"] = verification_meta
    return fields


def _contact_create_stdin_row(
    connection: Any, line_number: int, line: str
) -> dict[str, object]:
    """Process one ``contact create --stdin`` NDJSON line into a result row."""
    from mailpilot.database import (
        add_contact_note,
        create_contact,
        get_contact_by_email,
        update_contact,
    )
    from mailpilot.operator_log import operator_event

    payload, parse_error = _parse_ndjson_object(line_number, line)
    if parse_error is not None:
        return parse_error
    assert payload is not None
    fields, field_error = _parse_contact_create_fields(payload, line_number)
    if field_error is not None:
        return field_error
    assert fields is not None
    ref = str(fields["ref"])
    company_domain = fields["company_domain"]
    company_id: str | None = None
    company_domain_set = isinstance(company_domain, str)
    if company_domain_set:
        company = _lookup_company_soft(connection, str(company_domain))
        if company is None:
            return _batch_error(
                ref, "not_found", f"company not found: {company_domain}"
            )
        company_id = company.id
    meta = fields["meta"] if isinstance(fields["meta"], dict) else None
    meta_set = fields["meta"] is not None
    do_upsert = bool(fields["upsert"])
    created = create_contact(
        connection,
        email=str(fields["email"]),
        first_name=fields["first_name"]
        if isinstance(fields["first_name"], str)
        else None,
        last_name=fields["last_name"] if isinstance(fields["last_name"], str) else None,
        company_id=company_id,
        title=fields["title"] if isinstance(fields["title"], str) else None,
        email_confidence=(
            fields["email_confidence"]
            if isinstance(fields["email_confidence"], int)
            else None
        ),
        verification_meta=meta,
    )
    if created is None:
        if not do_upsert:
            # Safe-idempotent: duplicate natural key -> ok skip (§V.139).
            return _batch_ok(ref)
        existing = get_contact_by_email(connection, str(fields["email"]))
        if existing is None:
            return _batch_error(
                ref, "duplicate_key", f"contact with email={ref!r} already exists"
            )
        update_fields = _contact_upsert_fields(
            title=fields["title"] if isinstance(fields["title"], str) else None,
            email_confidence=(
                fields["email_confidence"]
                if isinstance(fields["email_confidence"], int)
                else None
            ),
            company_id=company_id,
            company_domain_set=company_domain_set,
            verification_meta=meta,
            meta_set=meta_set,
        )
        if update_fields:
            updated = update_contact(connection, existing.id, **update_fields)
            if updated is None:
                return _batch_error(ref, "not_found", f"contact not found: {ref}")
            operator_event(
                "contact.upsert",
                entity_id=updated.id,
                email=updated.email,
                company_id=company_id,
                created=False,
                changed=sorted(update_fields),
            )
        return _batch_ok(ref)
    changed = ["email", "first_name", "last_name", "company_id"]
    title = fields["title"]
    if isinstance(title, str):
        changed.append("title")
    if isinstance(fields["email_confidence"], int):
        changed.append("email_confidence")
    if meta is not None:
        changed.append("verification_meta")
    note = fields["note"]
    if isinstance(note, str) and note:
        add_contact_note(connection, created.id, note)
        changed.append("note")
    operator_event(
        "contact.create",
        entity_id=created.id,
        email=created.email,
        company_id=company_id,
        changed=changed,
    )
    return _batch_ok(ref)


def _run_contact_create_stdin() -> None:
    """Drive ``contact create --stdin`` NDJSON batch (§V.139)."""
    from mailpilot.database import initialize_database
    from mailpilot.operator_log import cli_mutation

    lines = _read_stdin_ndjson_lines()
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        with cli_mutation("contact", "create", mode="stdin", row_count=len(lines)):
            results = [
                _contact_create_stdin_row(connection, line_number, line)
                for line_number, line in lines
            ]
            _emit_batch_results(results)
    finally:
        connection.close()


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


def _resolve_workflow_id(connection: Any, workflow_ref: str) -> str:
    """Resolve a workflow reference (name or UUID) to its id (§V.107, §V.90).

    Workflow is a keyed entity addressed by its globally unique ``name``
    (§V.103). A UUID-shaped ref passes through unfetched (the caller's own
    ``get``/lifecycle call validates existence); any other value resolves via
    the ``name`` natural key, case-insensitively, exiting ``not_found`` when
    unknown (§V.94).
    """
    if _looks_like_uuid(workflow_ref):
        return workflow_ref
    from mailpilot.database import get_workflow_by_name

    workflow = get_workflow_by_name(connection, workflow_ref)
    if workflow is None:
        output_error(f"workflow not found: {workflow_ref}", "not_found")
    return workflow.id


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


def _version() -> str:
    """Render the CLI version, marking editable installs as dev builds.

    A PEP 610 direct_url.json with dir_info.editable true means the package
    was installed with `pip/uv install -e` from a checkout, so the running
    code can differ from the released wheel; render `<version>+dev (<path>)`
    to keep dev output from masquerading as a release. Wheel installs carry
    no direct_url.json (or editable is absent) and render plain `<version>`.
    """
    dist = distribution("mailpilot-crm")
    raw = dist.read_text("direct_url.json")
    if raw is not None:
        direct = json.loads(raw)
        if direct.get("dir_info", {}).get("editable"):
            checkout = direct.get("url", "").removeprefix("file://")
            return f"{dist.version}+dev ({checkout})"
    return dist.version


@click.group()
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


@db.command("export")
@click.option(
    "--file",
    "file",
    required=True,
    type=click.Path(dir_okay=False),
    help="Path to write the JSON snapshot bundle. Stdout emits the status envelope.",
)
def db_export(file: str) -> None:
    """Write a database snapshot bundle to disk.

    The bundle carries the tag vocabulary plus the company and contact tables;
    emails, activities, notes, workflows, enrollments, tasks, and accounts are
    excluded. Read-only and drift-tolerant, like `db check`: the bundle file
    lands on disk and stdout carries a JSON status envelope with the row counts.
    """
    import pathlib

    from mailpilot.database import export_snapshot, initialize_database

    connection = initialize_database(_database_url())
    try:
        bundle = export_snapshot(connection)
    finally:
        connection.close()
    pathlib.Path(file).write_text(json.dumps(bundle, indent=2, ensure_ascii=False))
    output(
        {
            "db": {
                "path": file,
                "companies": len(bundle["companies"]),
                "contacts": len(bundle["contacts"]),
                "tags": len(bundle["tags"]),
            }
        }
    )


@db.command("import")
@click.option(
    "--file",
    "file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to a JSON snapshot bundle to restore. Stdout emits the status envelope.",
)
def db_import(file: str) -> None:
    """Restore a database snapshot bundle in dependency order.

    A mutation: it dead-stops on a drifted or pending schema before any write
    lands. Restores the tag vocabulary, then companies, then contacts,
    re-linking every row by natural key (company domain, contact email, tag
    name). A row that cannot resolve its foreign key records a per-row error
    and the batch continues.
    """
    import pathlib

    from mailpilot.database import import_snapshot, initialize_database
    from mailpilot.operator_log import cli_mutation, operator_event

    raw = pathlib.Path(file).read_text()
    try:
        bundle = json.loads(raw)
    except json.JSONDecodeError as exc:
        output_error(f"malformed JSON: {exc}", "validation_error")
    if not isinstance(bundle, dict):
        output_error("snapshot bundle must be a JSON object", "validation_error")

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        with cli_mutation("db", "import", file=file):
            result = import_snapshot(connection, bundle)
            operator_event(
                "db.import",
                path=file,
                companies=result["companies"],
                contacts=result["contacts"],
                tags=result["tags"],
                errors=len(result["errors"]),
            )
            output(
                {
                    "db": {
                        "path": file,
                        "companies": result["companies"],
                        "contacts": result["contacts"],
                        "tags": result["tags"],
                        "errors": result["errors"],
                    }
                }
            )
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
    from mailpilot.gmail import GmailClient, has_google_credentials
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
    "--alias",
    "aliases",
    multiple=True,
    help=(
        "Alternate domain that resolves to this company (repeatable). "
        "Shared domain space: cannot match another company domain or alias."
    ),
)
@click.option(
    "--note",
    default=None,
    help="Optional first note body. Appended atomically as a `note` row.",
)
@click.option(
    "--upsert",
    is_flag=True,
    default=False,
    help=(
        "On natural-key conflict, update name when non-empty and register "
        "missing aliases only (never wipe profile). Without this flag, "
        "duplicate domain returns already_exists. Preferred agent path."
    ),
)
def company_create(
    domain: str,
    name: str,
    aliases: tuple[str, ...],
    note: str | None,
    upsert: bool,
) -> None:
    """Create a new company, optionally with alias domains."""
    from mailpilot.database import (
        add_company_alias,
        add_company_note,
        create_company,
        get_company_by_domain_exact,
        initialize_database,
        load_company_view,
        update_company,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    if not domain.strip():
        output_error("domain cannot be empty", "validation_error")
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        with cli_mutation("company", "create", domain=domain, upsert=upsert):
            created_row = create_company(
                connection,
                name=name,
                domain=domain,
                aliases=list(aliases) if aliases else None,
            )
            if created_row is None:
                if not upsert:
                    output_error(
                        f"company domain or alias already exists: {domain!r}",
                        "already_exists",
                    )
                # Canonical domain only — alias-of-other stays already_exists
                # (never move ownership, §V.147 / §V.142).
                existing = get_company_by_domain_exact(connection, domain)
                if existing is None:
                    output_error(
                        f"company domain or alias already exists: {domain!r}",
                        "already_exists",
                    )
                changed: list[str] = []
                if name:
                    updated = update_company(connection, existing.id, name=name)
                    if updated is not None:
                        existing = updated
                        changed.append("name")
                for alias in aliases:
                    try:
                        if add_company_alias(
                            connection, existing.id, alias, commit=True
                        ):
                            if "aliases" not in changed:
                                changed.append("aliases")
                    except ValueError as exc:
                        output_error(str(exc), "already_exists")
                # Bare upsert never touches profile (§V.147 / §V.140).
                operator_event(
                    "company.upsert",
                    entity_id=existing.id,
                    domain=existing.domain,
                    created=False,
                    changed=changed or ["none"],
                )
                viewed = load_company_view(connection, existing.id)
                output_entity(
                    "company",
                    viewed if viewed is not None else existing,
                    created=False,
                )
                return
            changed = ["name", "domain"]
            if aliases:
                changed.append("aliases")
            if note:
                add_company_note(connection, created_row.id, note)
                changed.append("note")
            operator_event(
                "company.create",
                entity_id=created_row.id,
                domain=created_row.domain,
                changed=changed,
            )
            # View projection includes aliases[] (§V.8 / §V.142).
            viewed = load_company_view(connection, created_row.id)
            output_entity(
                "company",
                viewed if viewed is not None else created_row,
                created=True,
            )
    finally:
        connection.close()


def _parse_company_profile_json(text: str) -> dict[str, object]:
    """Parse full-replace profile JSON text into a dict.

    Invalid JSON or a non-object root becomes ``validation_error`` (no DB write).
    """
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError as exc:
        output_error(f"invalid JSON: {exc}", "validation_error")
    if not isinstance(parsed, dict):
        output_error("profile must be a JSON object", "validation_error")
    return parsed


def _parse_verification_meta_json(text: str) -> dict[str, object]:
    """Parse operator-only verification meta JSON into a dict (§V.144).

    Invalid JSON or a non-object root becomes ``validation_error`` (no DB write).
    """
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError as exc:
        output_error(f"invalid JSON: {exc}", "validation_error")
    if not isinstance(parsed, dict):
        output_error("meta must be a JSON object", "validation_error")
    return parsed


def _merge_company_profile_patch(
    existing: dict[str, Any] | None,
    *,
    summary: str | None,
    products: tuple[str, ...],
    sources: tuple[str, ...],
    timezone: str | None,
    target_customers: str | None,
) -> dict[str, object]:
    """Field-merge patch flags into an existing profile (or empty base).

    Multi flags replace their list when at least one value is supplied.
    Empty ``--timezone`` clears optional timezone to null. Result is not
    validated here — ``update_company`` applies CompanyProfile validation.
    """
    base: dict[str, object] = dict(existing) if existing else {}
    if summary is not None:
        base["summary"] = summary
    if products:
        base["products"] = list(products)
    if sources:
        base["sources"] = list(sources)
    if timezone is not None:
        base["timezone"] = timezone if timezone else None
    if target_customers is not None:
        base["target_customers"] = target_customers
    return base


@company.command("update")
@click.argument("company_ref")
@click.option("--name", default=None, help="Company name.")
@click.option(
    "--profile-json",
    default=None,
    help="Full-replace profile as an inline JSON object (prefer --profile-file or --profile -).",
)
@click.option(
    "--profile-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Full-replace profile from a JSON file path.",
)
@click.option(
    "--profile",
    default=None,
    help="Full-replace profile; pass '-' to read a JSON object from stdin.",
)
@click.option("--summary", default=None, help="Patch profile.summary (merge).")
@click.option(
    "--product",
    multiple=True,
    help="Patch profile.products (repeatable; replaces the products list).",
)
@click.option(
    "--source",
    multiple=True,
    help="Patch profile.sources (repeatable; replaces the sources list).",
)
@click.option(
    "--timezone",
    default=None,
    help="Patch profile.timezone (empty string clears to null).",
)
@click.option(
    "--target-customers",
    default=None,
    help="Patch profile.target_customers (merge).",
)
def company_update(
    company_ref: str,
    name: str | None,
    profile_json: str | None,
    profile_file: str | None,
    profile: str | None,
    summary: str | None,
    product: tuple[str, ...],
    source: tuple[str, ...],
    timezone: str | None,
    target_customers: str | None,
) -> None:
    """Update a company (addressed by domain or ID).

    Profile writes: full-replace via exclusive --profile-json / --profile-file /
    --profile - (stdin), or field-patch merge via --summary / --product /
    --source / --timezone / --target-customers. Full-replace and field-patch
    are exclusive. Invalid profiles fail with validation_error and no write.
    """
    import pathlib
    import sys

    from mailpilot.database import initialize_database, update_company
    from mailpilot.operator_log import cli_mutation, operator_event

    replace_flags: list[str] = []
    if profile_json is not None:
        replace_flags.append("--profile-json")
    if profile_file is not None:
        replace_flags.append("--profile-file")
    if profile is not None:
        replace_flags.append("--profile")
    has_patch = any(
        (
            summary is not None,
            bool(product),
            bool(source),
            timezone is not None,
            target_customers is not None,
        )
    )
    if len(replace_flags) > 1:
        output_error(
            "full-replace profile options are exclusive: "
            + ", ".join(replace_flags),
            "validation_error",
        )
    if replace_flags and has_patch:
        output_error(
            "full-replace profile options are exclusive with field-patch flags",
            "validation_error",
        )
    if profile is not None and profile != "-":
        output_error(
            "--profile only accepts '-' for stdin; use --profile-file for a path",
            "validation_error",
        )

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        before = _resolve_company(connection, company_ref)
        company_id = before.id
        fields: dict[str, object] = {}
        if name is not None:
            fields["name"] = name
        if profile_json is not None:
            fields["profile"] = _parse_company_profile_json(profile_json)
        elif profile_file is not None:
            raw = pathlib.Path(profile_file).read_text(encoding="utf-8")
            fields["profile"] = _parse_company_profile_json(raw)
        elif profile == "-":
            fields["profile"] = _parse_company_profile_json(sys.stdin.read())
        elif has_patch:
            existing = before.profile if isinstance(before.profile, dict) else None
            fields["profile"] = _merge_company_profile_patch(
                existing,
                summary=summary,
                products=product,
                sources=source,
                timezone=timezone,
                target_customers=target_customers,
            )
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
@click.argument("company_ref", required=False, default=None)
@click.option(
    "--reason",
    default=None,
    help="Explanation written to disabled_reason (single-entity mode).",
)
@click.option(
    "--stdin",
    "from_stdin",
    is_flag=True,
    default=False,
    help=(
        "Batch mode: read NDJSON from stdin, one object per line with "
        "domain and reason. Exclusive with COMPANY_REF / --reason. "
        "Re-disable of an already-disabled company is an ok no-op. "
        "Exit 0 when every row is ok; exit 1 if any row errors "
        "(full results JSON still on stdout)."
    ),
)
def company_disable(
    company_ref: str | None, reason: str | None, from_stdin: bool
) -> None:
    """Soft-disable a company by writing disabled_reason.

    A disabled company is hidden from `company list` unless `--include-disabled`
    is passed. Disable is reversible -- re-enable with `company enable`.
    Single-entity mode rejects an already-disabled company; ``--stdin`` batch
    mode treats re-disable as an ok no-op so a lead pass can re-run safely.
    """
    from mailpilot.database import disable_company, initialize_database
    from mailpilot.operator_log import cli_mutation, operator_event

    if from_stdin:
        if company_ref is not None:
            output_error(
                "--stdin is exclusive with a company positional target",
                "validation_error",
            )
        if reason is not None:
            output_error(
                "--stdin is exclusive with --reason (supply reason per NDJSON line)",
                "validation_error",
            )
        _run_company_disable_stdin()
        return

    if company_ref is None:
        output_error(
            "COMPANY_REF is required (or pass --stdin)",
            "validation_error",
        )
    if reason is None or reason.strip() == "":
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
    that is not disabled is rejected. Enabling a company whose domain is an
    alias of another company is rejected (`invalid_state`).
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
            try:
                updated = enable_company(connection, company_id)
            except ValueError as exc:
                output_error(str(exc), "invalid_state")
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


@company.command("merge")
@click.option(
    "--from",
    "from_ref",
    required=True,
    help="Source company to absorb (domain or ID).",
)
@click.option(
    "--into",
    "into_ref",
    required=True,
    help="Survivor company (domain or ID).",
)
@click.option(
    "--move-contacts",
    is_flag=True,
    default=False,
    help="Reassign all contacts from the source company to the survivor.",
)
def company_merge(from_ref: str, into_ref: str, move_contacts: bool) -> None:
    """Absorb a source company into a survivor brand.

    Records the source domain as an alias on the survivor, soft-disables the
    source with reason `merged:into <survivor.domain>`, and optionally moves
    contacts. Re-running the same merge is an ok no-op.
    """
    from mailpilot.database import (
        get_company,
        get_company_by_domain,
        get_company_by_domain_exact,
        initialize_database,
        load_company_view,
        merge_companies,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        # Survivor resolves aliases (canonical firm).
        into_company = _resolve_company(connection, into_ref)
        if into_company.disabled_reason is not None:
            output_error(
                f"survivor company is disabled (reason: {into_company.disabled_reason})",
                "invalid_state",
            )

        # Source: exact domain first so an already-merged alias is not treated
        # as a second live firm. UUID still resolves by id.
        original_from_domain: str | None = None
        if _looks_like_uuid(from_ref):
            from_company = get_company(connection, from_ref)
            if from_company is None:
                output_error(f"company not found: {from_ref}", "not_found")
            original_from_domain = from_company.domain
            if from_company.domain.startswith("__merged__."):
                # After merge the row keeps a tombstone domain; absorb key is
                # recovered from the survivor alias list when possible.
                original_from_domain = None
        else:
            from_company = get_company_by_domain_exact(connection, from_ref)
            if from_company is None:
                # Idempotent: --from is already an alias of the survivor.
                alias_hit = get_company_by_domain(connection, from_ref)
                if alias_hit is not None and alias_hit.id == into_company.id:
                    viewed = load_company_view(connection, into_company.id)
                    output_entity(
                        "company", viewed if viewed is not None else into_company
                    )
                    return
                output_error(f"company not found: {from_ref}", "not_found")
            original_from_domain = from_ref

        if from_company.id == into_company.id:
            output_error("cannot merge a company into itself", "invalid_state")

        with cli_mutation(
            "company",
            "merge",
            entity_id=into_company.id,
            from_id=from_company.id,
        ):
            try:
                merged = merge_companies(
                    connection,
                    from_company.id,
                    into_company.id,
                    move_contacts=move_contacts,
                    original_from_domain=original_from_domain,
                )
            except ValueError as exc:
                message = str(exc)
                code = (
                    "invalid_state"
                    if "disabled" in message or "itself" in message
                    else "validation_error"
                )
                output_error(message, code)
            if merged is None:
                output_error("merge failed: company not found", "not_found")
            operator_event(
                "company.merge",
                entity_id=merged.id,
                from_id=from_company.id,
                move_contacts=move_contacts,
                changed=["aliases", "disabled_reason"]
                + (["contacts"] if move_contacts else []),
            )
            viewed = load_company_view(connection, merged.id)
            output_entity("company", viewed if viewed is not None else merged)
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
@enum_option(
    "--status",
    "status",
    _COMPANY_PIPELINE_STATUSES,
    "Pipeline cohort: ready (profile + >=1 contact), needs_contacts "
    "(profile + 0 contacts), needs_profile (no profile), disabled "
    "(has disabled_reason; forces include). Composes with other filters.",
)
@presence_option("profile", "Filter on presence of a company profile.")
@range_options(
    "contacts",
    "Return only companies with contact_count >= N (composes with --max).",
    "Return only companies with contact_count <= N (inclusive).",
)
@tag_filter_options
@include_disabled_option
@time_window_options("created_at")
@click.option(
    "--full",
    is_flag=True,
    default=False,
    help="Embed profile.summary on each row (null when no profile).",
)
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
    full: bool,
    status: str | None,
) -> None:
    """List companies as summaries.

    Lean rows project domain, name, has_profile, contact_count, tags,
    disabled_reason. Pass --full to embed profile.summary for triage without
    N company view calls.

    --status filters a pipeline cohort: ready (profile + at least one contact,
    not disabled), needs_contacts (profile + zero contacts, not disabled),
    needs_profile (no profile, not disabled), disabled (disabled_reason set;
    overrides the default hide). Status AND-composes with --tag, --no-tag,
    --min/max-contacts, --has-profile, and --include-disabled.
    """
    from mailpilot.database import initialize_database, list_companies

    connection = initialize_database(_database_url())
    try:
        tag_id = _resolve_tag(connection, tag).id if tag is not None else None
        exclude_tag_ids = [_resolve_tag(connection, name).id for name in no_tag]
        # --status disabled overrides the default hide of disabled rows.
        effective_include_disabled = include_disabled or status == "disabled"
        companies = list_companies(
            connection,
            limit=limit,
            since=since,
            until=until,
            has_profile=has_profile,
            max_contacts=max_contacts,
            min_contacts=min_contacts,
            include_disabled=effective_include_disabled,
            tag=tag_id,
            exclude_tags=exclude_tag_ids,
            full=full,
            status=status,
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
@enum_option(
    "--status",
    "status",
    _COMPANY_PIPELINE_STATUSES,
    "Pipeline cohort filter (same rules as company list --status).",
)
@presence_option("profile", "Filter on presence of a company profile.")
@range_options(
    "contacts",
    "Return only companies with contact_count >= N (composes with --max).",
    "Return only companies with contact_count <= N (inclusive).",
)
@tag_filter_options
@include_disabled_option
@click.option(
    "--full",
    is_flag=True,
    default=False,
    help="Embed full profile object on each row (null when no profile).",
)
@click.option(
    "--format",
    "export_format",
    type=click.Choice(["jsonl"]),
    default="jsonl",
    show_default=True,
    help="Output format (jsonl only).",
)
@click.option(
    "--out",
    "out_path",
    default=None,
    type=click.Path(dir_okay=False),
    help=(
        "Write NDJSON to this path; stdout emits a JSON status envelope. "
        "Omit to stream NDJSON lines on stdout."
    ),
)
def company_export(
    has_profile: bool | None,
    max_contacts: int | None,
    min_contacts: int | None,
    include_disabled: bool,
    tag: str | None,
    no_tag: tuple[str, ...],
    full: bool,
    status: str | None,
    export_format: str,
    out_path: str | None,
) -> None:
    """Export companies as tracker NDJSON (one company object per line).

    Stable keys: domain, name, tags, has_profile, contact_count,
    disabled_reason. Domains are lowercased; tags sorted; rows ordered by
    domain. Filters compose with the company list family. Pass --full to
    embed the full profile object (or null). With --out, write the file and
    print a company_export status envelope on stdout; without --out, stream
    NDJSON on stdout (no envelope). Empty set yields zero lines / empty file.
    Not the same as db export (full CRM snapshot).
    """
    import pathlib

    from mailpilot.database import export_companies, initialize_database

    del export_format  # only jsonl is accepted; Choice already enforced
    connection = initialize_database(_database_url())
    try:
        tag_id = _resolve_tag(connection, tag).id if tag is not None else None
        exclude_tag_ids = [_resolve_tag(connection, name).id for name in no_tag]
        effective_include_disabled = include_disabled or status == "disabled"
        rows = export_companies(
            connection,
            has_profile=has_profile,
            max_contacts=max_contacts,
            min_contacts=min_contacts,
            include_disabled=effective_include_disabled,
            tag=tag_id,
            exclude_tags=exclude_tag_ids,
            full=full,
            status=status,
        )
    finally:
        connection.close()

    lines = [json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows]
    body = "\n".join(lines) + ("\n" if lines else "")
    if out_path is not None:
        pathlib.Path(out_path).write_text(body, encoding="utf-8")
        output(
            {
                "company_export": {
                    "path": out_path,
                    "format": "jsonl",
                    "record_count": len(rows),
                }
            },
            record_count=len(rows),
        )
        return
    # Stream exclusion: NDJSON body on stdout, no single-object envelope.
    if body:
        click.echo(body, nl=False)
    else:
        click.echo("", nl=False)


@company.command("import")
@click.option(
    "--from",
    "from_path",
    required=True,
    type=click.Path(dir_okay=False),
    help="Path to tracker NDJSON (one company object per line).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report domain parity only; no writes (required for now).",
)
@enum_option(
    "--status",
    "status",
    _COMPANY_PIPELINE_STATUSES,
    "Scope CRM side to this pipeline cohort (same as company list).",
)
@presence_option("profile", "Scope CRM side by profile presence.")
@range_options(
    "contacts",
    "Scope CRM side: contact_count >= N.",
    "Scope CRM side: contact_count <= N (inclusive).",
)
@tag_filter_options
@include_disabled_option
def company_import(
    from_path: str,
    dry_run: bool,
    has_profile: bool | None,
    max_contacts: int | None,
    min_contacts: int | None,
    include_disabled: bool,
    tag: str | None,
    no_tag: tuple[str, ...],
    status: str | None,
) -> None:
    """Compare a tracker NDJSON file to CRM domains (dry-run only).

    Reads one JSON object per line; each line must carry a domain. Optional
    filters scope the CRM side the same way as company export. Report buckets:
    missing_in_crm, missing_profile, zero_contacts, disabled, extra_in_crm.
    Apply writes are not supported yet -- pass --dry-run.
    """
    import pathlib

    from mailpilot.database import company_import_diff, initialize_database

    if not dry_run:
        output_error(
            "company import apply is not supported; pass --dry-run",
            "validation_error",
        )

    path = pathlib.Path(from_path)
    if not path.is_file():
        output_error(f"tracker file not found: {from_path}", "not_found")

    file_domains: set[str] = set()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        output_error(f"cannot read tracker file: {exc}", "validation_error")
    for line_no, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            output_error(
                f"invalid NDJSON on line {line_no}: {exc}",
                "validation_error",
            )
        if not isinstance(obj, dict):
            output_error(
                f"invalid NDJSON on line {line_no}: expected object",
                "validation_error",
            )
        domain = obj.get("domain")
        if not isinstance(domain, str) or not domain.strip():
            output_error(
                f"invalid NDJSON on line {line_no}: missing domain",
                "validation_error",
            )
        file_domains.add(domain.strip().lower())

    connection = initialize_database(_database_url())
    try:
        tag_id = _resolve_tag(connection, tag).id if tag is not None else None
        exclude_tag_ids = [_resolve_tag(connection, name).id for name in no_tag]
        effective_include_disabled = include_disabled or status == "disabled"
        diff = company_import_diff(
            connection,
            file_domains,
            has_profile=has_profile,
            max_contacts=max_contacts,
            min_contacts=min_contacts,
            include_disabled=effective_include_disabled,
            tag=tag_id,
            exclude_tags=exclude_tag_ids,
            status=status,
        )
    finally:
        connection.close()

    record_count = int(diff.pop("record_count"))
    output({"company_import_diff": diff}, record_count=record_count)


# -- Contact commands ----------------------------------------------------------


@main.group()
def contact() -> None:
    """Manage contacts."""


@contact.command("create")
@click.option("--email", default=None, help="Email address (single-entity mode).")
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
    "--meta-json",
    default=None,
    help=(
        "Operator-only verification meta as a JSON object "
        "(e.g. bouncer_status, source). Never injected into agent prompts."
    ),
)
@click.option(
    "--note",
    default=None,
    help="Optional first note body. Appended atomically as a `note` row.",
)
@click.option(
    "--stdin",
    "from_stdin",
    is_flag=True,
    default=False,
    help=(
        "Batch mode: read NDJSON from stdin, one object per line with "
        "contact create fields (email required; first_name, last_name, "
        "company_domain, title, email_confidence, meta, note, upsert "
        "optional). Exclusive with single-entity create options. "
        "Duplicate email is an ok skip unless upsert:true (field-selective "
        "update). Exit 0 when every row is ok; exit 1 if any row errors "
        "(full results JSON still on stdout)."
    ),
)
@click.option(
    "--upsert",
    is_flag=True,
    default=False,
    help=(
        "On natural-key conflict, update title / email_confidence / "
        "company_domain / meta when those flags are present (never clobber "
        "omitted fields). Without this flag, duplicate email returns "
        "duplicate_key. Preferred agent path."
    ),
)
def contact_create(
    email: str | None,
    first_name: str | None,
    last_name: str | None,
    company_domain: str | None,
    title: str | None,
    email_confidence: int | None,
    meta_json: str | None,
    note: str | None,
    from_stdin: bool,
    upsert: bool,
) -> None:
    """Create a new contact (single-entity or ``--stdin`` NDJSON batch)."""
    from mailpilot.database import (
        add_contact_note,
        create_contact,
        get_contact_by_email,
        initialize_database,
        update_contact,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    single_entity_set = any(
        value is not None
        for value in (
            email,
            first_name,
            last_name,
            company_domain,
            title,
            email_confidence,
            meta_json,
            note,
        )
    )
    if from_stdin:
        if single_entity_set or upsert:
            output_error(
                "--stdin is exclusive with single-entity create options",
                "validation_error",
            )
        _run_contact_create_stdin()
        return

    if email is None:
        output_error(
            "--email is required (or pass --stdin)",
            "validation_error",
        )
    verification_meta = (
        _parse_verification_meta_json(meta_json) if meta_json is not None else None
    )
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        company_id = (
            _resolve_company(connection, company_domain).id
            if company_domain is not None
            else None
        )
        with cli_mutation(
            "contact", "create", email=email, company_id=company_id, upsert=upsert
        ):
            created_row = create_contact(
                connection,
                email=email,
                first_name=first_name,
                last_name=last_name,
                company_id=company_id,
                title=title,
                email_confidence=email_confidence,
                verification_meta=verification_meta,
            )
            if created_row is None:
                if not upsert:
                    output_error(
                        f"contact with email={email!r} already exists",
                        "duplicate_key",
                    )
                existing = get_contact_by_email(connection, email)
                if existing is None:
                    output_error(
                        f"contact with email={email!r} already exists",
                        "duplicate_key",
                    )
                update_fields = _contact_upsert_fields(
                    title=title,
                    email_confidence=email_confidence,
                    company_id=company_id,
                    company_domain_set=company_domain is not None,
                    verification_meta=verification_meta,
                    meta_set=meta_json is not None,
                )
                contact_out = existing
                if update_fields:
                    updated = update_contact(connection, existing.id, **update_fields)
                    if updated is None:
                        output_error(
                            f"contact with email={email!r} already exists",
                            "duplicate_key",
                        )
                    contact_out = updated
                operator_event(
                    "contact.upsert",
                    entity_id=contact_out.id,
                    email=contact_out.email,
                    company_id=company_id,
                    created=False,
                    changed=sorted(update_fields) if update_fields else ["none"],
                )
                output_entity("contact", contact_out, created=False)
                return
            changed = ["email", "first_name", "last_name", "company_id"]
            if title is not None:
                changed.append("title")
            if email_confidence is not None:
                changed.append("email_confidence")
            if verification_meta is not None:
                changed.append("verification_meta")
            if note:
                add_contact_note(connection, created_row.id, note)
                changed.append("note")
            operator_event(
                "contact.create",
                entity_id=created_row.id,
                email=created_row.email,
                company_id=company_id,
                changed=changed,
            )
            output_entity("contact", created_row, created=True)
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
@click.option(
    "--meta-json",
    default=None,
    help=(
        "Operator-only verification meta as a JSON object "
        "(replaces existing meta; never injected into agent prompts)."
    ),
)
def contact_update(
    contact_ref: str,
    email: str | None,
    first_name: str | None,
    last_name: str | None,
    company_domain: str | None,
    title: str | None,
    email_confidence: int | None,
    meta_json: str | None,
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
        if meta_json is not None:
            fields["verification_meta"] = _parse_verification_meta_json(meta_json)
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
                    "verification_meta",
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
@click.option(
    "--include-meta",
    is_flag=True,
    default=False,
    help=(
        "Project operator-only verification_meta (null when unset). "
        "Default view and the agent prompt path omit meta."
    ),
)
def contact_view(contact_ref: str, include_meta: bool) -> None:
    """Show a contact by email or ID with inlined notes (own + parent company)."""
    from mailpilot.database import get_contact, initialize_database, load_contact_view

    connection = initialize_database(_database_url())
    try:
        contact_id = _resolve_contact_id(connection, contact_ref)
        found = load_contact_view(connection, contact_id)
        if found is None:
            output_error(f"contact not found: {contact_ref}", "not_found")
        if not include_meta:
            output_entity("contact", found)
            return
        # Default ContactView is agent-safe; merge meta only when operator asks.
        payload = found.model_dump(mode="json")
        row = get_contact(connection, contact_id)
        payload["verification_meta"] = (
            row.verification_meta if row is not None else None
        )
        output({"contact": payload})
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
@click.option(
    "--contact-email",
    "contact_emails",
    multiple=True,
    help="Owner contact (email or ID); repeatable. XOR with --company-domain.",
)
@click.option(
    "--company-domain",
    "company_domains",
    multiple=True,
    help="Owner company (domain or ID); repeatable. XOR with --contact-email.",
)
def tag_add(
    tag_name: str,
    contact_emails: tuple[str, ...],
    company_domains: tuple[str, ...],
) -> None:
    """Link a defined tag to one or more contacts or companies.

    Pass repeatable ``--company-domain`` or repeatable ``--contact-email``
    (owner-kind XOR, at least one owner). One owner returns a
    ``tag_assignment`` entity envelope; multiple owners return a ``results``
    batch envelope (already-linked rows are ok skips). Errors ``not_found``
    when the tag is undefined -- never creates the tag as a side effect
    (define vocabulary with ``tag create``).
    """
    from mailpilot.database import (
        assign_tag_to_company,
        assign_tag_to_contact,
        initialize_database,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    has_contacts = len(contact_emails) > 0
    has_companies = len(company_domains) > 0
    if has_contacts and has_companies:
        output_error(
            "pass --contact-email or --company-domain, not both",
            "validation_error",
        )
    if not has_contacts and not has_companies:
        output_error(
            "at least one --contact-email or --company-domain is required",
            "validation_error",
        )
    owner_kind = "contact" if has_contacts else "company"
    owner_refs = contact_emails if has_contacts else company_domains
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        tag_row = _resolve_tag(connection, tag_name)
        if len(owner_refs) == 1:
            ref = owner_refs[0]
            if owner_kind == "contact":
                owner_id = _resolve_contact(connection, ref).id
            else:
                owner_id = _resolve_company(connection, ref).id
            with cli_mutation(
                "tag",
                "add",
                name=tag_row.name,
                owner_type=owner_kind,
                owner_id=owner_id,
            ):
                if owner_kind == "contact":
                    created = assign_tag_to_contact(
                        connection, tag_id=tag_row.id, contact_id=owner_id
                    )
                else:
                    created = assign_tag_to_company(
                        connection, tag_id=tag_row.id, company_id=owner_id
                    )
                if created is None:
                    output_error(
                        f"tag '{tag_row.name}' already on {owner_kind} {owner_id}",
                        "already_exists",
                    )
                operator_event(
                    "tag.add",
                    name=tag_row.name,
                    owner_type=owner_kind,
                    owner_id=owner_id,
                    changed=["tag_id"],
                )
                output_entity("tag_assignment", created)
            return

        with cli_mutation(
            "tag",
            "add",
            name=tag_row.name,
            owner_type=owner_kind,
            owner_count=len(owner_refs),
        ):
            results: list[dict[str, object]] = []
            for ref in owner_refs:
                if owner_kind == "contact":
                    owner = _lookup_contact_soft(connection, ref)
                else:
                    owner = _lookup_company_soft(connection, ref)
                if owner is None:
                    results.append(
                        _batch_error(
                            ref,
                            "not_found",
                            f"{owner_kind} not found: {ref}",
                        )
                    )
                    continue
                if owner_kind == "contact":
                    created = assign_tag_to_contact(
                        connection, tag_id=tag_row.id, contact_id=owner.id
                    )
                else:
                    created = assign_tag_to_company(
                        connection, tag_id=tag_row.id, company_id=owner.id
                    )
                if created is not None:
                    operator_event(
                        "tag.add",
                        name=tag_row.name,
                        owner_type=owner_kind,
                        owner_id=owner.id,
                        changed=["tag_id"],
                    )
                # Already-linked multi row is status ok skip (§V.141).
                results.append(_batch_ok(ref))
            _emit_batch_results(results)
    finally:
        connection.close()


@tag.command("set")
@click.option(
    "--contact-email",
    default=None,
    help="Owner contact (email or ID). XOR with --company-domain.",
)
@click.option(
    "--company-domain",
    default=None,
    help="Owner company (domain or ID). XOR with --contact-email.",
)
@click.option(
    "--tags",
    "tags_csv",
    required=True,
    help="Comma-separated defined tag names; empty string clears all assignments.",
)
def tag_set(
    contact_email: str | None,
    company_domain: str | None,
    tags_csv: str,
) -> None:
    """Replace an owner's full tag assignment set.

    Pass exactly one of ``--company-domain`` or ``--contact-email`` plus
    ``--tags a,b,c``. Empty ``--tags`` clears every assignment. Undefined
    names error ``not_found`` with zero writes. Company success returns the
    company entity (including final ``tags``); contact success returns the
    contact entity. Vocabulary is never auto-created -- define names with
    ``tag create`` first.
    """
    from mailpilot.database import (
        get_tag_by_name,
        initialize_database,
        load_company_view,
        set_company_tags,
        set_contact_tags,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    if (contact_email is None) == (company_domain is None):
        output_error(
            "exactly one of --contact-email or --company-domain is required",
            "validation_error",
        )
    raw_names = [part.strip() for part in tags_csv.split(",")]
    tag_names = [name for name in raw_names if name]
    # Empty CSV ("") or whitespace-only is a clear; bare commas with no names too.
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        tag_ids: list[str] = []
        for name in tag_names:
            try:
                tag_row = get_tag_by_name(connection, name)
            except ValueError:
                tag_row = None
            if tag_row is None:
                output_error(f"tag not found: {name}", "not_found")
            tag_ids.append(tag_row.id)
        if company_domain is not None:
            company = _resolve_company(connection, company_domain)
            with cli_mutation(
                "tag",
                "set",
                owner_type="company",
                owner_id=company.id,
                tag_count=len(tag_ids),
            ):
                final_names = set_company_tags(
                    connection, company_id=company.id, tag_ids=tag_ids
                )
                operator_event(
                    "tag.set",
                    owner_type="company",
                    owner_id=company.id,
                    changed=["tags"],
                    tags=",".join(final_names),
                )
                view = load_company_view(connection, company.id)
                if view is None:
                    output_error(f"company not found: {company.id}", "not_found")
                output_entity("company", view)
        else:
            assert contact_email is not None
            contact = _resolve_contact(connection, contact_email)
            with cli_mutation(
                "tag",
                "set",
                owner_type="contact",
                owner_id=contact.id,
                tag_count=len(tag_ids),
            ):
                final_names = set_contact_tags(
                    connection, contact_id=contact.id, tag_ids=tag_ids
                )
                operator_event(
                    "tag.set",
                    owner_type="contact",
                    owner_id=contact.id,
                    changed=["tags"],
                    tags=",".join(final_names),
                )
                output_entity("contact", contact)
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


@note.command("remove")
@click.argument("note_id")
def note_remove(note_id: str) -> None:
    """Delete a single note by ID (the sole note hard-delete).

    Removes only the named note row; the owner's other notes and the note_added
    activity trail stay intact. Operator-only -- the agent never deletes notes.
    """
    # Single-note delete is the sole CLI hard-delete per §V.14; the owner's other
    # notes survive and the activity trail stays append-only per §V.91.
    # Operator-only, never an agent tool.
    from mailpilot.database import delete_note, get_note, initialize_database
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        found = get_note(connection, note_id)
        if found is None:
            output_error(f"note {note_id} not found", "not_found")
        with cli_mutation("note", "remove", entity_id=note_id):
            delete_note(connection, note_id)
            operator_event("note.remove", entity_id=note_id)
            output_entity("note", found)
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
    goal: str | None,
    resolved_instructions: str | None,
    activate: bool,
) -> tuple[Any, list[str]] | None:
    """Run the §V.54 mutation sequence: create -> update extras -> optional activate.

    Returns the populated workflow row and the list of fields written, or
    ``None`` when ``create_workflow`` collided on the global ``name`` unique
    constraint per §V.16(+).
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
    if goal is not None:
        extras["goal"] = goal
    if resolved_instructions is not None:
        extras["instructions"] = resolved_instructions
    if extras:
        created = update_workflow(connection, created.id, **extras) or created
    if activate:
        created = activate_workflow(connection, created.id)
    changed = ["name", "template", "account_id", "theme"]
    if goal is not None:
        changed.append("goal")
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
@click.option("--goal", default=None, help="Workflow goal.")
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
    goal: str | None,
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
    has_goal = goal is not None
    has_instructions = instructions is not None or instructions_file is not None
    if not draft and not (has_goal and has_instructions):
        output_error(
            "cannot activate workflow without goal and instructions. "
            "Use --draft to create without them.",
            "validation_error",
        )
    resolved = _resolve_instructions(instructions, instructions_file)
    activate = not draft and has_goal and has_instructions
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
                goal=goal,
                resolved_instructions=resolved,
                activate=activate,
            )
            if result is None:
                output_error(
                    f"workflow {name!r} already exists",
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
@click.argument("workflow_ref")
@click.option(
    "--account-email",
    default=None,
    help="Re-bind the owning Gmail account (email or ID).",
)
def workflow_update(
    workflow_ref: str,
    account_email: str | None,
) -> None:
    """Update a workflow's non-def fields by name or ID.

    Def fields ``{name, template, theme, goal, instructions}`` are import-only:
    edit the ``workflows/*.toml`` and re-import to change them. ``update`` mutates
    only non-def fields -- account binding here, status via ``start`` / ``stop``.
    """
    # Def fields import-only; update restricted to non-def fields per §V.103.
    from mailpilot.database import get_workflow, initialize_database, update_workflow
    from mailpilot.operator_log import cli_mutation, operator_event

    if account_email is None:
        output_error(
            "nothing to update: provide --account-email to re-bind the account "
            "(def fields are import-only -- edit the TOML and re-import)",
            "validation_error",
        )
    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        workflow_id = _resolve_workflow_id(connection, workflow_ref)
        before = get_workflow(connection, workflow_id)
        if before is None:
            output_error(f"workflow not found: {workflow_ref}", "not_found")
        account_id = _resolve_account(connection, account_email).id
        with cli_mutation("workflow", "update", entity_id=workflow_id):
            updated = update_workflow(connection, workflow_id, account_id=account_id)
            if updated is None:
                output_error(f"workflow not found: {workflow_id}", "not_found")
            changed = ["account_id"] if before.account_id != updated.account_id else []
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
    """Search workflows by name or goal."""
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
@click.argument("workflow_ref")
def workflow_view(workflow_ref: str) -> None:
    """Show a workflow by name or ID."""
    from mailpilot.database import get_workflow, initialize_database

    connection = initialize_database(_database_url())
    try:
        workflow_id = _resolve_workflow_id(connection, workflow_ref)
        found = get_workflow(connection, workflow_id)
        if found is None:
            output_error(f"workflow not found: {workflow_ref}", "not_found")
        output_entity("workflow", found)
    finally:
        connection.close()


@workflow.command("stats")
@click.argument("workflow_ref")
def workflow_stats(workflow_ref: str) -> None:
    """Show the per-campaign funnel for a workflow by name or ID."""
    from mailpilot.database import get_workflow_stats, initialize_database

    connection = initialize_database(_database_url())
    try:
        workflow_id = _resolve_workflow_id(connection, workflow_ref)
        stats = get_workflow_stats(connection, workflow_id)
        if stats is None:
            output_error(f"workflow not found: {workflow_ref}", "not_found")
        output({"workflow_stats": stats.model_dump(mode="json")})
    finally:
        connection.close()


def _read_workflow_check_catalog(
    files: tuple[str, ...],
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Read catalog ``*.toml`` defs into a ``{name -> entry}`` map (§V.134).

    Reuses the import loader's TOML-only file-vs-dir dispatch (§V.103) but keys
    each entry on its ``name`` field -- ``workflow check`` reads the field, not
    the file stem (§V.134). ``--file`` is repeatable, so every passed source is
    read and merged; on a duplicate ``name`` across files the last def wins
    (§V.134). A malformed file or an entry missing ``name`` exits
    ``validation_error`` per the closed error vocabulary (§V.54); an empty
    ``files`` exits ``validation_error`` too.

    Returns:
        The merged catalog and ``scope_to_catalog``: ``True`` when every source
        is a specific ``.toml`` file (the report presents only the inquired
        names), ``False`` when any source is a directory (a full-catalog check
        that still surfaces unaccounted rows as ``orphaned``, §V.134).
    """
    import pathlib

    if not files:
        output_error(
            "no input: provide --file PATH (a '.toml' file or a directory)",
            "validation_error",
        )
    catalog: dict[str, dict[str, Any]] = {}
    scope_to_catalog = True
    for file in files:
        if pathlib.Path(file).is_dir():
            scope_to_catalog = False
        entries, pre_errors = _load_workflow_import_entries(file)
        if pre_errors:
            output_error(str(pre_errors[0]["message"]), "validation_error")
        for _stem, entry in entries:
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                output_error(
                    "catalog entry missing required 'name' field",
                    "validation_error",
                )
            catalog[name] = entry
    return catalog, scope_to_catalog


@workflow.command("check")
@click.option(
    "--file",
    "files",
    multiple=True,
    type=click.Path(exists=True),
    help=(
        "Catalog source (TOML only): a '.toml' file or a directory of '*.toml' "
        "defs to check against the live workflow rows. Repeatable: pass --file "
        "once per source. A specific-file check reports only the passed "
        "workflows; a directory check also flags unaccounted rows as orphaned."
    ),
)
def workflow_check(files: tuple[str, ...]) -> None:
    """Report wording drift between catalog defs and live workflow rows.

    A read-only 2-way live SHA-256 over the wording fields
    {template, theme, goal, instructions}, joined by the globally unique name.
    Mirrors ``db check`` but is report-only: every state (in_sync, out_of_sync,
    not_imported, orphaned) exits 0 with ``ok:true`` -- the check informs, it is
    never a deploy gate.

    ``--file`` is repeatable. Passing specific ``.toml`` files scopes the report
    to those workflows only; a live row you did not pass never appears as
    orphaned. Pass a directory to check the full catalog, where a row with no
    def surfaces as orphaned drift.
    """
    from mailpilot.database import check_workflow_wording, initialize_database

    catalog, scope_to_catalog = _read_workflow_check_catalog(files)
    connection = initialize_database(_database_url())
    try:
        report = check_workflow_wording(
            connection, catalog, scope_to_catalog=scope_to_catalog
        )
    finally:
        connection.close()
    output({"workflow_check": report.model_dump(mode="json")})


@workflow.command("start")
@click.argument("workflow_ref")
def workflow_start(workflow_ref: str) -> None:
    """Start a workflow by name or ID (requires non-empty goal and instructions)."""
    from mailpilot.database import activate_workflow, initialize_database
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        workflow_id = _resolve_workflow_id(connection, workflow_ref)
        with cli_mutation("workflow", "start", entity_id=workflow_id):
            try:
                activated = activate_workflow(connection, workflow_id)
            except ValueError as exc:
                message = str(exc)
                if "goal" in message:
                    output_error(
                        "cannot start: goal is empty. Set 'goal' in the "
                        "workflow's TOML and re-import: workflow import --file <path>",
                        "invalid_state",
                    )
                if "instructions" in message:
                    output_error(
                        "cannot start: instructions are empty. Set 'instructions' "
                        "in the workflow's TOML and re-import: "
                        "workflow import --file <path>",
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
@click.argument("workflow_ref")
def workflow_stop(workflow_ref: str) -> None:
    """Stop an active workflow by name or ID."""
    from mailpilot.database import initialize_database, pause_workflow
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        workflow_id = _resolve_workflow_id(connection, workflow_ref)
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

    Single-line def fields (``name``, ``template``, ``theme``, ``goal``)
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


def _workflow_to_toml(workflow: Any) -> str:
    """Serialize a ``Workflow`` row to a one-workflow TOML catalog entry (§V.103).

    Emits the def fields ``{name, template, theme, goal[, touches,
    touch_interval_days], instructions}`` in a fixed order; ``instructions`` uses
    a multi-line literal string so pipes and quotes survive verbatim. The leading
    newline after the opening ``'''`` is trimmed by the TOML parser, so the value
    re-parses byte-identically. The cadence pair (§V.136) emits as bare TOML ints
    and is omitted entirely for a single-touch workflow (both columns NULL), so a
    non-cadence catalog stays byte-identical to prior exports.
    """
    parts = [
        f"name = {_toml_basic_string(workflow.name)}\n",
        f"template = {_toml_basic_string(workflow.template)}\n",
        f"theme = {_toml_basic_string(workflow.theme)}\n",
        f"goal = {_toml_basic_string(workflow.goal)}\n",
    ]
    if workflow.touches is not None:
        parts.append(f"touches = {workflow.touches}\n")
    if workflow.touch_interval_days is not None:
        parts.append(f"touch_interval_days = {workflow.touch_interval_days}\n")
    parts.append(f"instructions = '''\n{workflow.instructions}'''\n")
    return "".join(parts)


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
    ``{name, template, theme, goal, instructions}`` plus the optional cadence
    pair ``touches`` / ``touch_interval_days`` when set, name-sorted) and prints
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
            path = directory / f"{current.name}.toml"
            path.write_text(_workflow_to_toml(current))
            written.append({"name": current.name, "path": str(path)})
        output({"workflows": written})
    finally:
        connection.close()


_WORKFLOW_IMPORT_UPDATABLE = (
    "goal",
    "instructions",
    "theme",
    "touches",
    "touch_interval_days",
)


def _workflow_import_extras(entry: dict[str, Any]) -> dict[str, object]:
    """Def fields an import writes onto a freshly created workflow (§V.103).

    Beyond ``name`` / ``template`` / ``theme`` / account set at create time:
    ``goal`` and ``instructions`` when non-empty, plus the cadence pair
    ``touches`` / ``touch_interval_days`` when the def carries them (§V.136). The
    schema CHECK rejects a half-configured cadence, surfacing as a per-row import
    error.
    """
    extras: dict[str, object] = {}
    goal = entry.get("goal")
    instructions = entry.get("instructions")
    if goal:
        extras["goal"] = goal
    if instructions:
        extras["instructions"] = instructions
    for cadence_field in ("touches", "touch_interval_days"):
        if cadence_field in entry:
            extras[cadence_field] = entry[cadence_field]
    return extras


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
            "message": f"workflow {name!r} already exists (name is globally unique)",
        }
    extras = _workflow_import_extras(entry)
    if extras:
        update_workflow(connection, created.id, **extras)
    activated = bool(entry.get("goal") and entry.get("instructions"))
    if activated:
        activate_workflow(connection, created.id)
    changed = ["name", "template", "account_id", "theme", *extras.keys()]
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


def _validate_workflow_import_name(name: str, stem: str) -> str | None:
    """Return an error if ``name`` is not a valid import key, else None (§V.103).

    The name is the canonical cross-environment key (§V.107). It must be
    kebab-shaped (lowercase letters, digits, single hyphens, mirroring the
    schema CHECK), must not be UUID-shaped (resolver ambiguity, §V.107), and
    must equal the source file stem so the file-to-row bijection holds.
    """
    import re

    if _looks_like_uuid(name):
        return f"workflow name {name!r} must not be UUID-shaped"
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name):
        return (
            f"workflow name {name!r} must be kebab-case: lowercase letters, "
            "digits, single hyphens, no leading/trailing hyphen"
        )
    if name != stem:
        return (
            f"workflow name {name!r} must equal the file stem {stem!r}; "
            "rename the file or the 'name' field so they match"
        )
    return None


def _import_workflow_row(
    connection: Any,
    account_id: str,
    existing: dict[str, Any],
    stem: str,
    entry: dict[str, Any],
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
    name_error = _validate_workflow_import_name(name, stem)
    if name_error is not None:
        operator_event(
            "workflow.import",
            account_id=account_id,
            name=name,
            changed=[],
        )
        return {
            "name": name,
            "error": "validation_error",
            "message": name_error,
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
) -> tuple[list[tuple[str, dict[str, Any]]], list[dict[str, object]]]:
    """Glob ``*.toml`` in a catalog dir; collect each as a (stem, entry) pair (§V.103).

    Each entry is paired with its file stem so import can enforce the
    ``name == stem`` bijection (§V.103). A file that fails to parse becomes a
    per-row error so the rest of the catalog still imports (§V.63).
    """
    import tomllib

    entries: list[tuple[str, dict[str, Any]]] = []
    pre_errors: list[dict[str, object]] = []
    for toml_path in sorted(path.glob("*.toml")):
        try:
            with toml_path.open("rb") as handle:
                entries.append((toml_path.stem, tomllib.load(handle)))
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
) -> tuple[list[tuple[str, dict[str, Any]]], list[dict[str, object]]]:
    """Parse a ``workflow import`` source into (stem, entry) pairs + errors (§V.103).

    TOML-only per §V.103, §V.63 (no JSON, no stdin). Dispatch by shape: a
    directory globs ``*.toml`` (catalog batch, per-file parse errors become
    per-row pre-errors) and a single ``.toml`` file parses to one entry. Each
    entry carries its file stem so import can enforce the ``name == stem``
    bijection (§V.103). A missing ``--file`` or a non-TOML path exits via
    ``output_error`` with ``validation_error``.
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
                return [(path.stem, tomllib.load(handle))], []
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
    ``goal`` and ``instructions`` are non-empty), present workflows are
    updated for changed fields only, ``template`` differences emit a per-row
    ``template_immutable`` error, and ``status`` is never written by import.

    The terminal envelope aggregates: top-level ``applied`` and ``rejected``
    counts on every import envelope; zero applied rows -> an ``import_failed``
    error envelope on stderr (per-row rows inlined) and exit 1, so scripts
    gating on the exit code never mistake a no-op import for success.
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
                _import_workflow_row(connection, account_id, existing, stem, entry)
                for stem, entry in entries
            )
            rejected = sum(1 for row in results if "error" in row)
            applied = len(results) - rejected
            if applied == 0:
                # Loud failure per §V.103 / §B.123: an import that lands zero
                # rows must not report success. Per-row detail rides inside the
                # error envelope, mirroring `db check` report inlining (§V.109).
                message = (
                    f"workflow import applied 0 of {len(results)} rows; "
                    "every row was rejected"
                    if results
                    else "workflow import found no importable rows in source"
                )
                output_error(
                    message,
                    "import_failed",
                    extra={"workflows": results, "applied": 0, "rejected": rejected},
                )
            output(
                {"workflows": results, "applied": applied, "rejected": rejected},
                record_count=len(results),
            )
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
@enum_option("--trigger", "trigger", _TASK_TRIGGERS, "Filter by task trigger.")
@time_window_options("scheduled_at")
@limit_option
def task_list(
    workflow_id: str | None,
    contact_email: str | None,
    status: str | None,
    trigger: str | None,
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
            trigger=trigger,
            limit=limit,
            since=since,
            until=until,
        )
        output({"tasks": [t.model_dump(mode="json") for t in tasks]})
    finally:
        connection.close()


@task.command("stats")
@scope_option("--workflow-id", "workflow_id", "Filter by workflow ID.")
@enum_option("--trigger", "trigger", _TASK_TRIGGERS, "Filter by task trigger.")
@click.option(
    "--bucket-tz",
    default="UTC",
    help="IANA timezone for day-bucketing distinct_scheduled_days.",
)
def task_stats(
    workflow_id: str | None,
    trigger: str | None,
    bucket_tz: str,
) -> None:
    """Show the task-cadence aggregate over the task queue."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from mailpilot.database import (
        get_task_stats,
        get_workflow,
        initialize_database,
    )

    connection = initialize_database(_database_url())
    try:
        if workflow_id is not None and get_workflow(connection, workflow_id) is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        try:
            ZoneInfo(bucket_tz)
        except ZoneInfoNotFoundError, ValueError:
            output_error(f"unknown timezone: {bucket_tz}", "validation_error")
        stats = get_task_stats(
            connection,
            workflow_id=workflow_id,
            trigger=trigger,
            bucket_tz=bucket_tz,
        )
        output({"task_stats": stats.model_dump(mode="json")})
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


# -- Meeting commands ----------------------------------------------------------


@main.group()
def meeting() -> None:
    """Inspect calendar meetings ingested from Google Calendar.

    Rows are created by calendar ingestion, never by the operator -- there is
    no `meeting create`.
    """


@meeting.command("list")
@scope_option("--contact-email", "contact_email", "Filter by attendee (email or ID).")
@enum_option("--status", "status", _MEETING_STATUSES, "Filter by meeting status.")
@time_window_options("scheduled_at")
@limit_option
def meeting_list(
    contact_email: str | None,
    status: str | None,
    limit: int,
    since: str | None,
    until: str | None,
) -> None:
    """List meetings, newest scheduled first, with optional filters."""
    from mailpilot.database import initialize_database, list_meetings

    connection = initialize_database(_database_url())
    try:
        contact_id = (
            _resolve_contact(connection, contact_email).id
            if contact_email is not None
            else None
        )
        meetings = list_meetings(
            connection,
            limit=limit,
            contact_id=contact_id,
            status=status,
            since=since,
            until=until,
        )
        output({"meetings": [m.model_dump(mode="json") for m in meetings]})
    finally:
        connection.close()


@meeting.command("view")
@click.argument("meeting_id")
def meeting_view(meeting_id: str) -> None:
    """Show a meeting by ID with its attendee contacts inlined."""
    from mailpilot.database import initialize_database, load_meeting_view

    connection = initialize_database(_database_url())
    try:
        found = load_meeting_view(connection, meeting_id)
        if found is None:
            output_error(f"meeting not found: {meeting_id}", "not_found")
        output_entity("meeting", found)
    finally:
        connection.close()


@meeting.command("add")
@click.argument("meeting_id")
@click.option(
    "--contact-email",
    required=True,
    help="Attendee contact to link (email or ID).",
)
def meeting_add(meeting_id: str, contact_email: str) -> None:
    """Link an attendee contact to a meeting.

    A repeat link on the same (meeting, contact) pair is rejected
    `already_exists` -- the pair is unique.
    """
    from mailpilot.database import (
        get_meeting,
        initialize_database,
        link_meeting_attendee,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        if get_meeting(connection, meeting_id) is None:
            output_error(f"meeting not found: {meeting_id}", "not_found")
        contact_id = _resolve_contact(connection, contact_email).id
        with cli_mutation(
            "meeting", "add", entity_id=meeting_id, contact_id=contact_id
        ):
            created = link_meeting_attendee(connection, meeting_id, contact_id)
            if created is None:
                output_error(
                    f"contact {contact_id} already attends meeting {meeting_id}",
                    "already_exists",
                )
            operator_event(
                "meeting.add",
                entity_id=meeting_id,
                contact_id=contact_id,
                changed=["contact_id"],
            )
            output_entity("meeting_attendee", created)
    finally:
        connection.close()


@meeting.command("update")
@click.argument("meeting_id")
@click.option("--summary", default=None, help="Meeting summary/title.")
@click.option(
    "--status",
    type=click.Choice(_MEETING_STATUSES),
    default=None,
    help="Meeting status (record-keeping only, gates nothing).",
)
def meeting_update(meeting_id: str, summary: str | None, status: str | None) -> None:
    """Edit a meeting's summary or status."""
    from mailpilot.database import get_meeting, initialize_database, update_meeting
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        before = get_meeting(connection, meeting_id)
        if before is None:
            output_error(f"meeting not found: {meeting_id}", "not_found")
        fields: dict[str, object] = {}
        if summary is not None:
            fields["summary"] = summary
        if status is not None:
            fields["status"] = status
        with cli_mutation("meeting", "update", entity_id=meeting_id):
            updated = update_meeting(connection, meeting_id, **fields)
            if updated is None:
                output_error(f"meeting not found: {meeting_id}", "not_found")
            changed = [
                field
                for field in ("summary", "status")
                if getattr(before, field) != getattr(updated, field)
            ]
            operator_event("meeting.update", entity_id=meeting_id, changed=changed)
            output_entity("meeting", updated)
    finally:
        connection.close()


@meeting.command("cancel")
@click.argument("meeting_id")
def meeting_cancel(meeting_id: str) -> None:
    """Cancel a meeting by setting its status to `cancelled`."""
    from mailpilot.database import get_meeting, initialize_database, update_meeting
    from mailpilot.operator_log import cli_mutation, operator_event

    connection = initialize_database(_database_url(), require_current_schema=True)
    try:
        if get_meeting(connection, meeting_id) is None:
            output_error(f"meeting not found: {meeting_id}", "not_found")
        with cli_mutation("meeting", "cancel", entity_id=meeting_id):
            updated = update_meeting(connection, meeting_id, status="cancelled")
            if updated is None:
                output_error(f"meeting not found: {meeting_id}", "not_found")
            operator_event("meeting.cancel", entity_id=meeting_id, changed=["status"])
            output_entity("meeting", updated)
    finally:
        connection.close()
