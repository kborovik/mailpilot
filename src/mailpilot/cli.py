"""CLI interface for MailPilot.

Startup-critical: only ``click`` is imported at module level. All heavy
dependencies (logfire, psycopg, httpx, pydantic, mailpilot.database,
mailpilot.settings) are lazy-imported inside command functions so that
``--help`` / ``--version`` stay fast (~50 ms).
When adding new commands, keep imports inside the function body.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from importlib.metadata import distribution
from typing import TYPE_CHECKING, Any, Literal, NoReturn, TypedDict

import click

from mailpilot._filters import (
    COMPANY_SORT_KEYS,
    DIRECTIONS,
    desc_option,
    enum_option,
    include_disabled_option,
    limit_option,
    offset_option,
    presence_option,
    range_options,
    scope_option,
    sort_option,
    tag_filter_options,
    time_window_options,
    touch_option,
)

if TYPE_CHECKING:
    import pathlib

    from logfire import ScrubMatch

    from mailpilot.models import Account, Company, Contact, EnrollmentBatchAction

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
# Terminal dispositions on enrollment outcome activity (§V.127 / §V.160).
_ENROLLMENT_DISPOSITIONS = ["meeting_booked", "do_not_contact", "contact_later"]
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


@contextmanager
def _db(*, mutate: bool = False) -> Generator[Any]:
    """Yield a CLI database connection.

    Lazy-imports ``initialize_database`` so ``--help`` stays click-only.
    ``mutate=True`` is the write-path schema gate.
    """
    from mailpilot.database import initialize_database

    connection = initialize_database(
        _database_url(),
        require_current_schema=mutate,
    )
    try:
        yield connection
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


def _resolve_disable_reason(reason: str | None, reason_file: str | None) -> str:
    r"""Resolve single-entity disable reason from ``--reason`` XOR ``--reason-file``.

    Exactly one source required. File is UTF-8; one trailing newline is stripped
    (``\n`` or ``\r\n``). Empty after resolve → ``validation_error``. Missing
    path → ``not_found``.
    """
    import pathlib

    if reason is not None and reason_file is not None:
        output_error(
            "pass only one of --reason or --reason-file",
            "validation_error",
        )
    if reason is None and reason_file is None:
        output_error(
            "pass --reason or --reason-file",
            "validation_error",
        )
    if reason_file is not None:
        path = pathlib.Path(reason_file)
        if not path.is_file():
            output_error(f"reason file not found: {reason_file}", "not_found")
        text = path.read_text(encoding="utf-8")
        if text.endswith("\r\n"):
            text = text[:-2]
        elif text.endswith("\n"):
            text = text[:-1]
        if text.strip() == "":
            output_error("reason cannot be empty", "validation_error")
        return text
    assert reason is not None
    if reason.strip() == "":
        output_error("reason cannot be empty", "validation_error")
    return reason


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
    from mailpilot.operator_log import cli_mutation

    lines = _read_stdin_ndjson_lines()
    with (
        _db(mutate=True) as connection,
        cli_mutation("company", "disable", mode="stdin", row_count=len(lines)),
    ):
        results = [
            _company_disable_stdin_row(connection, line_number, line)
            for line_number, line in lines
        ]
        _emit_batch_results(results)


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
        return None, _batch_error(ref, "validation_error", "meta must be a JSON object")
    upsert_raw = payload.get("upsert")
    if upsert_raw is None:
        upsert = False
    elif isinstance(upsert_raw, bool):
        upsert = upsert_raw
    else:
        return None, _batch_error(ref, "validation_error", "upsert must be a boolean")
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


def _contact_create_stdin_row(  # noqa: C901, PLR0911, PLR0912
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
    from mailpilot.operator_log import cli_mutation

    lines = _read_stdin_ndjson_lines()
    with (
        _db(mutate=True) as connection,
        cli_mutation("contact", "create", mode="stdin", row_count=len(lines)),
    ):
        results = [
            _contact_create_stdin_row(connection, line_number, line)
            for line_number, line in lines
        ]
        _emit_batch_results(results)


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
    dist = distribution("mailpilot-crm")
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
    ctx.ensure_object(dict)
    ctx.obj["debug"] = debug
    if ctx.invoked_subcommand is not None:
        configure_logging(debug=debug)


# -- Status command ------------------------------------------------------------


@main.command()
def status() -> None:
    """Show application state summary including sync loop status."""
    from mailpilot.database import get_status_payload
    from mailpilot.settings import get_settings

    settings = get_settings()
    with _db() as connection:
        output({"status": get_status_payload(connection, settings)})


# -- Show report hub -----------------------------------------------------------


@main.group()
def show() -> None:
    """Human report hub."""


@show.command("queue")
@click.option(
    "--detail",
    is_flag=True,
    default=False,
    help="Task-grain queue: one row per pending task.",
)
@scope_option("--workflow-name", "workflow_name", "Filter by workflow (name or ID).")
@click.option(
    "--tz",
    "tz_name",
    default=None,
    show_default="host local",
    help="IANA timezone for table and JSON next_at.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "table"], case_sensitive=False),
    default="table",
    show_default=True,
    help="Output format (default table).",
)
@limit_option
@click.option(
    "--overdue",
    is_flag=True,
    default=False,
    help="Only pending tasks with scheduled_at in the past.",
)
def show_queue(
    detail: bool,
    workflow_name: str | None,
    tz_name: str | None,
    output_format: str,
    limit: int,
    overdue: bool,
) -> None:
    """Show the outbound queue as a human table (JSON opt-in).

    Default grain is one row per workflow (draft, active, paused) with
    pending counts by touch (t1/t2/t3/t4p). --detail lists pending tasks
    (workflow_name, company_domain, contact, email, touch, attempts,
    next_at). Empty prints (no rows).
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from tabulate import tabulate

    from mailpilot.database import get_queue_report, get_workflow
    from mailpilot.queue import (
        project_queue_json_next_at,
        queue_table_cells,
        queue_table_headers,
        resolve_host_tz,
    )

    resolved_tz = resolve_host_tz() if tz_name is None else tz_name
    try:
        zone = ZoneInfo(resolved_tz)
    except ZoneInfoNotFoundError, ValueError:
        output_error(f"unknown timezone: {resolved_tz}", "validation_error")

    with _db() as connection:
        resolved_workflow_id: str | None = None
        if workflow_name is not None:
            resolved_workflow_id = _resolve_workflow_id(connection, workflow_name)
            if get_workflow(connection, resolved_workflow_id) is None:
                output_error(f"workflow not found: {workflow_name}", "not_found")
        report = get_queue_report(
            connection,
            detail=detail,
            workflow_id=resolved_workflow_id,
            tz=resolved_tz,
            limit=limit if detail else 100,
            overdue=overdue if detail else False,
        )

    dumped = project_queue_json_next_at(report.model_dump(mode="json"), tz=zone)
    if output_format.lower() == "json":
        output({"queue": dumped}, record_count=len(report.rows))
        return
    if not report.rows:
        click.echo("(no rows)")
        return
    headers = queue_table_headers(detail=detail)
    table_rows = [
        queue_table_cells(row, detail=detail, tz=zone) for row in dumped["rows"]
    ]
    click.echo(tabulate(table_rows, headers=headers, tablefmt="simple"))


@main.command()
def run() -> None:
    """Start the sync loop (Pub/Sub + task runner, foreground)."""
    from mailpilot.settings import get_settings, require_active_provider_key
    from mailpilot.sync import start_sync_loop

    settings = get_settings()
    try:
        require_active_provider_key(settings)
    except ValueError as exc:
        output_error(str(exc), "validation_error")
    with _db(mutate=True) as connection:
        start_sync_loop(connection, settings)


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
    from mailpilot.database import migrate_database

    with _db() as connection:
        applied = migrate_database(connection)
    output({"db": {"applied": applied, "count": len(applied)}})


@db.command("check")
def db_check() -> None:
    """Report the schema verdict; exit 1 on pending or drift.

    A scriptable deploy gate: ``current`` -> ok envelope + exit 0;
    ``pending``/``drift`` -> ``schema_migration_pending``/``schema_drift``
    error envelope with the report inlined + exit 1.
    """
    from mailpilot.database import determine_schema_verdict

    with _db() as connection:
        status = determine_schema_verdict(connection)
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

    from mailpilot.database import export_snapshot

    with _db() as connection:
        bundle = export_snapshot(connection)
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

    from mailpilot.database import import_snapshot
    from mailpilot.operator_log import cli_mutation, operator_event

    raw = pathlib.Path(file).read_text()
    try:
        bundle = json.loads(raw)
    except json.JSONDecodeError as exc:
        output_error(f"malformed JSON: {exc}", "validation_error")
    if not isinstance(bundle, dict):
        output_error("snapshot bundle must be a JSON object", "validation_error")

    with _db(mutate=True) as connection, cli_mutation("db", "import", file=file):
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


# -- Company commands ----------------------------------------------------------


@main.group()
def company() -> None:
    """Manage target companies."""


def _company_profile_options(fn: Any) -> Any:
    """Stack profile write flags shared by ``company create`` and ``update``."""
    fn = click.option(
        "--target-customers",
        default=None,
        help="Patch profile.target_customers (merge).",
    )(fn)
    fn = click.option(
        "--timezone",
        default=None,
        help="Patch profile.timezone (empty string clears to null).",
    )(fn)
    fn = click.option(
        "--source",
        multiple=True,
        help="Patch profile.sources (repeatable; replaces the sources list).",
    )(fn)
    fn = click.option(
        "--product",
        multiple=True,
        help="Patch profile.products (repeatable; replaces the products list).",
    )(fn)
    fn = click.option("--summary", default=None, help="Patch profile.summary (merge).")(
        fn
    )
    fn = click.option(
        "--profile",
        default=None,
        help="Full-replace profile; pass '-' to read a JSON object from stdin.",
    )(fn)
    fn = click.option(
        "--profile-file",
        default=None,
        type=click.Path(exists=True, dir_okay=False),
        help="Full-replace profile from a JSON file path.",
    )(fn)
    fn = click.option(
        "--profile-json",
        default=None,
        help=(
            "Full-replace profile as an inline JSON object "
            "(prefer --profile-file or --profile -)."
        ),
    )(fn)
    return fn


def _profile_replace_and_patch_flags(
    profile_json: str | None,
    profile_file: str | None,
    profile: str | None,
    summary: str | None,
    product: tuple[str, ...],
    source: tuple[str, ...],
    timezone: str | None,
    target_customers: str | None,
) -> tuple[list[str], bool]:
    """Return (replace flag names, has_patch); error on exclusive violations."""
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
            "full-replace profile options are exclusive: " + ", ".join(replace_flags),
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
    return replace_flags, has_patch


def _parse_json_object(text: str, *, what: str) -> dict[str, object]:
    """Parse JSON text into an object dict.

    Invalid JSON or a non-object root becomes ``validation_error`` (no DB write).
    """
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError as exc:
        output_error(f"invalid JSON: {exc}", "validation_error")
    if not isinstance(parsed, dict):
        output_error(f"{what} must be a JSON object", "validation_error")
    return parsed


def _read_replace_profile(
    profile_json: str | None,
    profile_file: str | None,
    profile: str | None,
) -> dict[str, object]:
    """Load a full-replace profile dict from json / file / stdin."""
    import pathlib
    import sys

    if profile_json is not None:
        return _parse_json_object(profile_json, what="profile")
    if profile_file is not None:
        raw = pathlib.Path(profile_file).read_text(encoding="utf-8")
        return _parse_json_object(raw, what="profile")
    return _parse_json_object(sys.stdin.read(), what="profile")


def _validate_company_profile_payload(payload: dict[str, object]) -> dict[str, object]:
    """Validate a profile dict before any CRM write (§V.72 / §V.167)."""
    from pydantic import ValidationError

    from mailpilot.models import CompanyProfile

    try:
        return CompanyProfile.model_validate(payload).model_dump(exclude_unset=True)
    except ValidationError as exc:
        output_error(str(exc), "validation_error")


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
        "missing aliases only (never wipe profile unless profile flags "
        "are also passed). Without this flag, duplicate domain returns "
        "already_exists. Preferred agent path."
    ),
)
@_company_profile_options
@click.option(
    "--tag",
    "tags",
    multiple=True,
    help=(
        "Defined vocabulary tag to link (repeatable, additive). "
        "Undefined names return not_found; never auto-created."
    ),
)
def company_create(  # noqa: C901, PLR0912, PLR0915
    domain: str,
    name: str,
    aliases: tuple[str, ...],
    note: str | None,
    upsert: bool,
    profile_json: str | None,
    profile_file: str | None,
    profile: str | None,
    summary: str | None,
    product: tuple[str, ...],
    source: tuple[str, ...],
    timezone: str | None,
    target_customers: str | None,
    tags: tuple[str, ...],
) -> None:
    """Create a company; optional profile flags and --tag are one transaction."""
    from mailpilot.database import (
        add_company_alias,
        add_company_note,
        assign_tag_to_company,
        create_company,
        get_company_by_domain_exact,
        load_company_view,
        write_company_fields,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    if not domain.strip():
        output_error("domain cannot be empty", "validation_error")
    replace_flags, has_patch = _profile_replace_and_patch_flags(
        profile_json,
        profile_file,
        profile,
        summary,
        product,
        source,
        timezone,
        target_customers,
    )
    replace_payload = (
        _read_replace_profile(profile_json, profile_file, profile)
        if replace_flags
        else None
    )
    tag_names = list(dict.fromkeys(tags))
    with _db(mutate=True) as connection:
        tag_rows = [_resolve_tag(connection, tag_name) for tag_name in tag_names]
        with cli_mutation("company", "create", domain=domain, upsert=upsert):
            existing_before = (
                get_company_by_domain_exact(connection, domain) if has_patch else None
            )
            profile_payload: dict[str, object] | None = None
            if replace_payload is not None:
                profile_payload = _validate_company_profile_payload(replace_payload)
            elif has_patch:
                existing_profile = (
                    existing_before.profile
                    if existing_before is not None
                    and isinstance(existing_before.profile, dict)
                    else None
                )
                profile_payload = _validate_company_profile_payload(
                    _merge_company_profile_patch(
                        existing_profile,
                        summary=summary,
                        products=product,
                        sources=source,
                        timezone=timezone,
                        target_customers=target_customers,
                    )
                )
            created_row = create_company(
                connection,
                name=name,
                domain=domain,
                aliases=list(aliases) if aliases else None,
                commit=False,
            )
            created = created_row is not None
            if created_row is None:
                if not upsert:
                    output_error(
                        f"company domain or alias already exists: {domain!r}",
                        "already_exists",
                    )
                # Canonical domain only — alias-of-other stays already_exists
                # (never move ownership, §V.147 / §V.142).
                existing = existing_before or get_company_by_domain_exact(
                    connection, domain
                )
                if existing is None:
                    output_error(
                        f"company domain or alias already exists: {domain!r}",
                        "already_exists",
                    )
                row = existing
            else:
                row = created_row
            changed: list[str] = []
            if created:
                changed.extend(["name", "domain"])
                if aliases:
                    changed.append("aliases")
            update_fields: dict[str, object] = {}
            if not created and name:
                update_fields["name"] = name
            if profile_payload is not None:
                update_fields["profile"] = profile_payload
            if update_fields:
                updated = write_company_fields(
                    connection, row.id, update_fields, commit=False
                )
                if updated is not None:
                    row = updated
                    if "name" in update_fields and "name" not in changed:
                        changed.append("name")
                    if "profile" in update_fields:
                        changed.append("profile")
            if not created:
                for alias in aliases:
                    try:
                        if (
                            add_company_alias(connection, row.id, alias, commit=False)
                            and "aliases" not in changed
                        ):
                            changed.append("aliases")
                    except ValueError as exc:
                        output_error(str(exc), "already_exists")
            for tag_row in tag_rows:
                assign_tag_to_company(
                    connection, tag_id=tag_row.id, company_id=row.id, commit=False
                )
            if tag_names:
                changed.append("tags")
            if note:
                add_company_note(connection, row.id, note, commit=False)
                changed.append("note")
            connection.commit()
            event_name = "company.create" if created else "company.upsert"
            operator_event(
                event_name,
                entity_id=row.id,
                domain=row.domain,
                created=created,
                changed=changed or ["none"],
            )
            viewed = load_company_view(connection, row.id)
            _output_company_create_entity(
                viewed if viewed is not None else row,
                created=created,
            )


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
@_company_profile_options
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
    from mailpilot.database import update_company
    from mailpilot.operator_log import cli_mutation, operator_event

    replace_flags, has_patch = _profile_replace_and_patch_flags(
        profile_json,
        profile_file,
        profile,
        summary,
        product,
        source,
        timezone,
        target_customers,
    )

    with _db(mutate=True) as connection:
        before = _resolve_company(connection, company_ref)
        company_id = before.id
        fields: dict[str, object] = {}
        if name is not None:
            fields["name"] = name
        if replace_flags:
            fields["profile"] = _read_replace_profile(
                profile_json, profile_file, profile
            )
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


@company.command("disable")
@click.argument("company_ref", required=False, default=None)
@click.option(
    "--reason",
    default=None,
    help="Explanation written to disabled_reason (single-entity mode).",
)
@click.option(
    "--reason-file",
    "reason_file",
    default=None,
    type=click.Path(dir_okay=False),
    help=(
        "Read disable reason from a UTF-8 file (single-entity; exclusive "
        "with --reason)."
    ),
)
@click.option(
    "--stdin",
    "from_stdin",
    is_flag=True,
    default=False,
    help=(
        "Batch mode: read NDJSON from stdin, one object per line with "
        "domain and reason. Exclusive with COMPANY_REF / --reason / "
        "--reason-file. Re-disable of an already-disabled company is an "
        "ok no-op. Exit 0 when every row is ok; exit 1 if any row errors "
        "(full results JSON still on stdout)."
    ),
)
def company_disable(
    company_ref: str | None,
    reason: str | None,
    reason_file: str | None,
    from_stdin: bool,
) -> None:
    """Soft-disable a company by writing disabled_reason.

    A disabled company is hidden from `company list` unless `--include-disabled`
    is passed. Disable is reversible -- re-enable with `company enable`.
    Single-entity mode takes ``--reason`` or ``--reason-file`` (XOR) and
    rejects an already-disabled company; ``--stdin`` batch mode treats
    re-disable as an ok no-op so a lead pass can re-run safely.
    """
    from mailpilot.database import disable_company
    from mailpilot.operator_log import cli_mutation, operator_event

    if from_stdin:
        if company_ref is not None:
            output_error(
                "--stdin is exclusive with a company positional target",
                "validation_error",
            )
        if reason is not None or reason_file is not None:
            output_error(
                "--stdin is exclusive with --reason / --reason-file "
                "(supply reason per NDJSON line)",
                "validation_error",
            )
        _run_company_disable_stdin()
        return

    if company_ref is None:
        output_error(
            "COMPANY_REF is required (or pass --stdin)",
            "validation_error",
        )
    resolved_reason = _resolve_disable_reason(reason, reason_file)
    with _db(mutate=True) as connection:
        before = _resolve_company(connection, company_ref)
        company_id = before.id
        if before.disabled_reason is not None:
            output_error(
                f"company {company_id} is already disabled "
                f"(reason: {before.disabled_reason})",
                "validation_error",
            )
        with cli_mutation("company", "disable", entity_id=company_id):
            updated = disable_company(connection, company_id, resolved_reason)
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


@company.command("enable")
@click.argument("company_ref")
def company_enable(company_ref: str) -> None:
    """Re-enable a soft-disabled company by clearing disabled_reason.

    The company reappears in the default `company list`. Enabling a company
    that is not disabled is rejected. Enabling a company whose domain is an
    alias of another company is rejected (`invalid_state`).
    """
    from mailpilot.database import enable_company
    from mailpilot.operator_log import cli_mutation, operator_event

    with _db(mutate=True) as connection:
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
    contacts. Source and survivor may already be disabled — no prior enable
    is required. A disabled survivor stays disabled with its existing
    reason. Re-running the same merge is an ok no-op.
    """
    from mailpilot.database import (
        get_company,
        get_company_by_domain,
        get_company_by_domain_exact,
        load_company_view,
        merge_companies,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    with _db(mutate=True) as connection:
        # Survivor resolves aliases (canonical firm). Disabled survivor is
        # allowed; merge keeps its disabled_reason (§V.143).
        into_company = _resolve_company(connection, into_ref)

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


@company.command("search")
@click.argument("query")
@sort_option(COMPANY_SORT_KEYS, default="name")
@desc_option
@offset_option
@limit_option(default=500)
def company_search(query: str, limit: int, offset: int, sort: str, desc: bool) -> None:
    """Search companies by name or domain.

    Lean rows match company list (domain, name, has_profile, contact_count,
    tags, disabled_reason). Default --limit is 500 for tag-sized cohorts.
    Sort keys: name (default), domain, created_at, contact_count; pass --desc
    for descending. Use --offset with --limit for pages.
    """
    from mailpilot.database import search_companies

    with _db() as connection:
        companies = search_companies(
            connection,
            query,
            limit=limit,
            offset=offset,
            sort=sort,
            desc=desc,
        )
        output({"companies": [c.model_dump(mode="json") for c in companies]})


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
@sort_option(COMPANY_SORT_KEYS, default="name")
@desc_option
@offset_option
@limit_option(default=500)
def company_list(
    limit: int,
    offset: int,
    sort: str,
    desc: bool,
    since: str | None,
    until: str | None,
    has_profile: bool | None,
    max_contacts: int | None,
    min_contacts: int | None,
    include_disabled: bool,
    tag: tuple[str, ...],
    no_tag: tuple[str, ...],
    full: bool,
    status: str | None,
) -> None:
    """List companies as summaries.

    Lean rows project domain, name, has_profile, contact_count, tags,
    disabled_reason. Pass --full to embed profile.summary for triage without
    N company view calls.

    Default --limit is 500 (tag-cohort sized). Sort keys: name (default),
    domain, created_at, contact_count; pass --desc for descending. Use
    --offset with --limit for pages (record_count is the page length).

    --status filters a pipeline cohort: ready (profile + at least one contact,
    not disabled), needs_contacts (profile + zero contacts, not disabled),
    needs_profile (no profile, not disabled), disabled (disabled_reason set;
    overrides the default hide). Status AND-composes with --tag, --no-tag,
    --min/max-contacts, --has-profile, and --include-disabled.

    Repeatable --tag is AND (row must carry every named tag). Repeatable
    --no-tag is AND (row must carry none of the named tags).
    """
    from mailpilot.database import list_companies

    with _db() as connection:
        companies = list_companies(
            connection,
            limit=limit,
            offset=offset,
            sort=sort,
            desc=desc,
            since=since,
            until=until,
            has_profile=has_profile,
            max_contacts=max_contacts,
            min_contacts=min_contacts,
            full=full,
            status=status,
            **_company_cohort_kwargs(connection, tag, no_tag, include_disabled, status),
        )
        output({"companies": [c.model_dump(mode="json") for c in companies]})


@company.command("view")
@click.argument("company_ref")
@click.option(
    "--full",
    is_flag=True,
    default=False,
    help=(
        "Embed contacts (lean fields including tags) with existing "
        "company tags and notes. Distinct from company list --full "
        "(profile.summary only)."
    ),
)
@click.option(
    "--include-meta",
    is_flag=True,
    default=False,
    help=(
        "With --full, project verification_meta on each contact "
        "(null when unset). Lean view omits meta."
    ),
)
def company_view(company_ref: str, full: bool, include_meta: bool) -> None:
    """Show a company by domain or ID with inlined notes.

    Lean view is unchanged (profile, tags, aliases, notes). Pass --full to
    embed contacts[] (lean contact fields including tags) in the same
    envelope. Pass --include-meta with --full to project verification_meta
    on those contacts. Distinct from company list --full, which only
    embeds profile.summary.
    """
    from mailpilot.database import (
        list_company_inspect_contacts,
        load_company_view,
    )

    with _db() as connection:
        company_id = _resolve_company_id(connection, company_ref)
        found = load_company_view(connection, company_id)
        if found is None:
            output_error(f"company not found: {company_ref}", "not_found")
        if not full:
            output_entity("company", found)
            return
        payload = found.model_dump(mode="json")
        payload["contacts"] = list_company_inspect_contacts(
            connection, found.id, include_meta=include_meta
        )
        output({"company": payload})


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
    tag: tuple[str, ...],
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

    from mailpilot.database import export_companies

    del export_format  # only jsonl is accepted; Choice already enforced
    with _db() as connection:
        rows = export_companies(
            connection,
            has_profile=has_profile,
            max_contacts=max_contacts,
            min_contacts=min_contacts,
            full=full,
            status=status,
            **_company_cohort_kwargs(connection, tag, no_tag, include_disabled, status),
        )

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
    tag: tuple[str, ...],
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

    from mailpilot.database import company_import_diff

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

    with _db() as connection:
        diff = company_import_diff(
            connection,
            file_domains,
            has_profile=has_profile,
            max_contacts=max_contacts,
            min_contacts=min_contacts,
            status=status,
            **_company_cohort_kwargs(connection, tag, no_tag, include_disabled, status),
        )

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
def contact_create(  # noqa: C901
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
        _parse_json_object(meta_json, what="meta") if meta_json is not None else None
    )
    with _db(mutate=True) as connection:
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
    from mailpilot.database import update_contact
    from mailpilot.operator_log import cli_mutation, operator_event

    with _db(mutate=True) as connection:
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
            fields["verification_meta"] = _parse_json_object(meta_json, what="meta")
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


@contact.command("disable")
@click.argument("contact_ref")
@click.option(
    "--reason",
    default=None,
    help="Explanation written to disabled_reason.",
)
@click.option(
    "--reason-file",
    "reason_file",
    default=None,
    type=click.Path(dir_okay=False),
    help=("Read disable reason from a UTF-8 file (exclusive with --reason)."),
)
def contact_disable(
    contact_ref: str, reason: str | None, reason_file: str | None
) -> None:
    """Soft-disable a contact by writing disabled_reason (email or ID).

    Pass ``--reason`` or ``--reason-file`` (exactly one).
    """
    from mailpilot.database import disable_contact
    from mailpilot.operator_log import cli_mutation, operator_event

    resolved_reason = _resolve_disable_reason(reason, reason_file)
    with _db(mutate=True) as connection:
        before = _resolve_contact(connection, contact_ref)
        contact_id = before.id
        with cli_mutation("contact", "disable", entity_id=contact_id):
            updated = disable_contact(connection, contact_id, resolved_reason)
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


@contact.command("enable")
@click.argument("contact_ref")
def contact_enable(contact_ref: str) -> None:
    """Re-enable a disabled contact by clearing disabled_reason.

    Clears any reason, including a `bounced:` or `unsubscribed:` block -- the
    operator owns consent. Enabling a contact that is not disabled is rejected.
    Addressed by email or ID.
    """
    from mailpilot.database import enable_contact
    from mailpilot.operator_log import cli_mutation, operator_event

    with _db(mutate=True) as connection:
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


@contact.command("search")
@click.argument("query")
@limit_option
def contact_search(query: str, limit: int) -> None:
    """Search contacts by email, name, or title.

    Rows project tags (assigned names, empty ok) with title and
    company_domain. Single-token: substring match on email, first_name,
    last_name, or title. Full name (e.g. "David Drouin"): order-preserving
    match on first+last. Multi-token: every token must match at least one
    of those fields (AND). Disabled contacts remain searchable.
    """
    from mailpilot.database import search_contacts

    with _db() as connection:
        contacts = search_contacts(connection, query, limit=limit)
        output({"contacts": [c.model_dump(mode="json") for c in contacts]})


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
    tag: tuple[str, ...],
    no_tag: tuple[str, ...],
) -> None:
    """List contacts as summaries.

    Each row projects tags (assigned names, empty ok) with title and
    company_domain. Repeatable --tag is AND (row must carry every named
    tag). Repeatable --no-tag is AND (row must carry none of the named
    tags).
    """
    from mailpilot.database import list_contacts

    with _db() as connection:
        company_id = (
            _resolve_company(connection, company_domain).id
            if company_domain is not None
            else None
        )
        tag_ids = _resolve_tag_ids(connection, tag)
        exclude_tag_ids = _resolve_tag_ids(connection, no_tag)
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
            tag=tag_ids or None,
            exclude_tags=exclude_tag_ids,
        )
        output({"contacts": [c.model_dump(mode="json") for c in contacts]})


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
@click.option(
    "--timeline",
    is_flag=True,
    default=False,
    help=(
        "Include bounded dossier: enrollments (status, disposition, last/next "
        "touch), recent emails, and recent activities. Default view is notes "
        "only. Default 10 rows per section; use --limit (hard cap 50)."
    ),
)
@click.option(
    "--limit",
    default=10,
    show_default=True,
    help=(
        "Max rows per --timeline section (enrollments, emails, activities). "
        "Hard cap 50."
    ),
)
def contact_view(
    contact_ref: str, include_meta: bool, timeline: bool, limit: int
) -> None:
    """Show a contact by email or ID with inlined notes (own + parent company).

    Projects tags (assigned names, empty ok). Pass --timeline for a
    bounded dossier (enrollments + emails + activities). Default path
    stays notes-only for agent prompt budget.
    """
    from mailpilot.database import (
        get_contact,
        load_contact_timeline,
        load_contact_view,
    )

    with _db() as connection:
        contact_id = _resolve_contact_id(connection, contact_ref)
        if timeline:
            payload = load_contact_timeline(connection, contact_id, limit=limit)
            if payload is None:
                output_error(f"contact not found: {contact_ref}", "not_found")
            if include_meta:
                row = get_contact(connection, contact_id)
                payload["verification_meta"] = (
                    row.verification_meta if row is not None else None
                )
            output({"contact": payload})
            return
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


# -- Email commands ------------------------------------------------------------


@main.group()
def email() -> None:
    """Manage emails."""


@email.command("search")
@click.argument("query")
@limit_option
def email_search(query: str, limit: int) -> None:
    """Search emails by subject or body."""
    from mailpilot.database import search_emails

    with _db() as connection:
        emails = search_emails(connection, query, limit=limit)
        output({"emails": [e.model_dump(mode="json") for e in emails]})


@email.command("list")
@scope_option("--contact-email", "contact_email", "Filter by contact (email or ID).")
@scope_option("--account-email", "account_email", "Filter by account (email or ID).")
@scope_option("--workflow-id", "workflow_id", "Filter by workflow (name or ID).")
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
    """List emails with optional filters (requires at least one scope filter)."""
    from mailpilot.database import (
        get_workflow,
        list_emails,
    )

    if (
        contact_email is None
        and account_email is None
        and workflow_id is None
        and thread_id is None
        and sender is None
        and recipient is None
        and direction is None
        and status is None
        and route_method is None
        and since is None
        and until is None
    ):
        output_error(
            "at least one scope or filter is required "
            "(--contact-email, --account-email, --workflow-id, --thread-id, "
            "--from, --to, --direction, --status, --route-method, or time window)",
            "missing_filter",
        )
    with _db() as connection:
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
        # Polymorphic name|UUID resolve (§V.107/§V.154); UUID existence still
        # validated so unknown ids stay not_found (same envelope as task list).
        resolved_workflow_id: str | None = None
        if workflow_id is not None:
            resolved_workflow_id = _resolve_workflow_id(connection, workflow_id)
            if get_workflow(connection, resolved_workflow_id) is None:
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
            workflow_id=resolved_workflow_id,
            status=status,
            sender=sender,
            recipient=recipient,
            route_method=route_method,
        )
        output({"emails": [e.model_dump(mode="json") for e in emails]})


@email.command("view")
@click.argument("email_id")
def email_view(email_id: str) -> None:
    """View a single email by ID."""
    from mailpilot.database import get_email

    with _db() as connection:
        found = get_email(connection, email_id)
        if found is None:
            output_error(f"email not found: {email_id}", "not_found")
        output_entity("email", found)


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
    from mailpilot.database import get_workflow
    from mailpilot.gmail import GmailClient
    from mailpilot.settings import get_settings

    if not subject.strip():
        output_error("subject cannot be empty", "validation_error")
    if not body.strip():
        output_error("body cannot be empty", "validation_error")

    to_joined = ",".join(to)
    settings = get_settings()
    with _db(mutate=True) as connection:
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
    from mailpilot.database import get_workflow
    from mailpilot.gmail import GmailClient
    from mailpilot.settings import get_settings

    if not body.strip():
        output_error("body cannot be empty", "validation_error")

    settings = get_settings()
    with _db(mutate=True) as connection:
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
    )

    if not summary.strip():
        output_error("summary cannot be empty", "validation_error")
    if contact_email is None and company_domain is None:
        output_error(
            "at least one of --contact-email or --company-domain is required",
            "validation_error",
        )
    detail_dict: dict[str, object] = json.loads(detail) if detail else {}
    with _db() as connection:
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


@activity.command("list")
@scope_option("--contact-email", "contact_email", "Filter by contact (email or ID).")
@scope_option("--company-domain", "company_domain", "Filter by company (domain or ID).")
@scope_option("--workflow-id", "workflow_id", "Filter by workflow (name or ID).")
@enum_option("--type", "activity_type", _ACTIVITY_TYPES, "Filter by activity type.")
@time_window_options("created_at")
@limit_option
def activity_list(
    contact_email: str | None,
    company_domain: str | None,
    workflow_id: str | None,
    activity_type: str | None,
    limit: int,
    since: str | None,
    until: str | None,
) -> None:
    """List activities (requires contact, company, or workflow scope)."""
    from mailpilot.database import (
        list_activities,
    )

    if contact_email is None and company_domain is None and workflow_id is None:
        output_error(
            "at least one of --contact-email, --company-domain, or --workflow-id "
            "is required",
            "missing_filter",
        )
    with _db() as connection:
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
        resolved_workflow_id: str | None = (
            _resolve_workflow_id(connection, workflow_id)
            if workflow_id is not None
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
            workflow_id=resolved_workflow_id,
        )
        output({"activities": [a.model_dump(mode="json") for a in activities]})


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
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    if not name.strip():
        output_error("tag name cannot be empty", "validation_error")
    with _db(mutate=True) as connection, cli_mutation("tag", "create", name=name):
        try:
            created = create_tag(connection, name=name)
        except ValueError as exc:
            output_error(str(exc), "validation_error")
        if created is None:
            normalized = _normalize_tag_name(name)
            output_error(f"tag '{normalized}' already exists", "already_exists")
        operator_event("tag.create", name=created.name, changed=["name"])
        output_entity("tag", created)


@tag.command("view")
@click.argument("name")
def tag_view(name: str) -> None:
    """Show a vocabulary tag by name with its usage_count."""
    from mailpilot.database import get_tag_summary_by_name

    with _db() as connection:
        try:
            found = get_tag_summary_by_name(connection, name)
        except ValueError:
            found = None
        if found is None:
            output_error(f"tag not found: {name}", "not_found")
        output_entity("tag", found)


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
    from mailpilot.database import disable_tag
    from mailpilot.operator_log import cli_mutation, operator_event

    if reason.strip() == "":
        output_error("reason cannot be empty", "validation_error")
    with _db(mutate=True) as connection:
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


@tag.command("enable")
@click.argument("name")
def tag_enable(name: str) -> None:
    """Re-enable a retired tag by clearing disabled_reason.

    The tag reappears in the default `tag list`. Enabling a tag that is not
    disabled is rejected.
    """
    from mailpilot.database import enable_tag
    from mailpilot.operator_log import cli_mutation, operator_event

    with _db(mutate=True) as connection:
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
def tag_add(  # noqa: C901, PLR0912
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
    with _db(mutate=True) as connection:
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
    with _db(mutate=True) as connection:
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


@tag.command("remove")
@click.option(
    "--tag", "tag_name", required=True, help="Defined tag to unlink (name or ID)."
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
def tag_remove(  # noqa: C901, PLR0912
    tag_name: str,
    contact_emails: tuple[str, ...],
    company_domains: tuple[str, ...],
) -> None:
    """Unlink a defined tag from one or more contacts or companies.

    Pass repeatable ``--company-domain`` or repeatable ``--contact-email``
    (owner-kind XOR, at least one owner). One owner returns a
    ``tag_assignment`` entity envelope; multiple owners return a ``results``
    batch envelope (already-unlinked rows are ok skips). Removes only the
    link; the tag vocabulary entry and the owners both survive.
    """
    from mailpilot.database import (
        remove_tag_from_company,
        remove_tag_from_contact,
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
    with _db(mutate=True) as connection:
        tag_row = _resolve_tag(connection, tag_name)
        if len(owner_refs) == 1:
            ref = owner_refs[0]
            if owner_kind == "contact":
                owner_id = _resolve_contact(connection, ref).id
            else:
                owner_id = _resolve_company(connection, ref).id
            with cli_mutation(
                "tag",
                "remove",
                name=tag_row.name,
                owner_type=owner_kind,
                owner_id=owner_id,
            ):
                if owner_kind == "contact":
                    removed = remove_tag_from_contact(
                        connection, tag_id=tag_row.id, contact_id=owner_id
                    )
                else:
                    removed = remove_tag_from_company(
                        connection, tag_id=tag_row.id, company_id=owner_id
                    )
                if removed is None:
                    output_error(
                        f"tag '{tag_row.name}' not on {owner_kind} {owner_id}",
                        "not_found",
                    )
                operator_event(
                    "tag.remove",
                    name=tag_row.name,
                    owner_type=owner_kind,
                    owner_id=owner_id,
                    changed=["tag_id"],
                )
                output_entity("tag_assignment", removed)
            return

        with cli_mutation(
            "tag",
            "remove",
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
                    removed = remove_tag_from_contact(
                        connection, tag_id=tag_row.id, contact_id=owner.id
                    )
                else:
                    removed = remove_tag_from_company(
                        connection, tag_id=tag_row.id, company_id=owner.id
                    )
                if removed is not None:
                    operator_event(
                        "tag.remove",
                        name=tag_row.name,
                        owner_type=owner_kind,
                        owner_id=owner.id,
                        changed=["tag_id"],
                    )
                # Already-unlinked multi row is status ok skip (§V.141).
                results.append(_batch_ok(ref))
            _emit_batch_results(results)


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
    from mailpilot.database import list_tags

    if contact_email is not None and company_domain is not None:
        output_error(
            "pass at most one of --contact-email or --company-domain",
            "validation_error",
        )
    with _db() as connection:
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


@tag.command("search")
@click.argument("name")
@include_disabled_option
@limit_option
def tag_search(name: str, limit: int, include_disabled: bool) -> None:
    """Search the tag vocabulary by name substring."""
    from mailpilot.database import search_tags

    with _db() as connection:
        tags = search_tags(
            connection,
            name=name,
            limit=limit,
            include_disabled=include_disabled,
        )
        output({"tags": [t.model_dump(mode="json") for t in tags]})


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
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    if not body.strip():
        output_error("note body cannot be empty", "validation_error")
    if (contact_email is None) == (company_domain is None):
        output_error(
            "exactly one of --contact-email or --company-domain is required",
            "validation_error",
        )
    with _db(mutate=True) as connection:
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


@note.command("remove")
@click.argument("note_id", required=False, default=None)
@click.option("--contact-email", default=None, help="Bulk: all notes on contact.")
@click.option("--company-domain", default=None, help="Bulk: all notes on company.")
@click.option(
    "--yes",
    "confirmed",
    is_flag=True,
    default=False,
    help="Required for owner bulk remove (confirmation gate).",
)
def note_remove(
    note_id: str | None,
    contact_email: str | None,
    company_domain: str | None,
    confirmed: bool,
) -> None:
    """Delete one note by ID, or all notes on an owner with --yes.

    Single-id: ``note remove <note_id>``. Owner bulk: exactly one of
    ``--contact-email`` / ``--company-domain`` plus required ``--yes``.
    Deletes note rows only; the note_added activity trail stays intact.
    Operator-only -- the agent never deletes notes.
    """
    # Dual-mode hard-delete per §V.14: single-id or owner bulk with --yes.
    # Activity trail stays append-only. Operator-only, never an agent tool.
    from mailpilot.database import (
        delete_note,
        delete_notes,
        get_note,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    has_owner = contact_email is not None or company_domain is not None
    if note_id is not None and has_owner:
        output_error(
            "NOTE_ID is exclusive with --contact-email / --company-domain",
            "validation_error",
        )
    if note_id is None and not has_owner:
        output_error(
            "NOTE_ID or --contact-email / --company-domain is required",
            "validation_error",
        )
    if has_owner:
        if (contact_email is None) == (company_domain is None):
            output_error(
                "exactly one of --contact-email or --company-domain is required",
                "validation_error",
            )
        if not confirmed:
            output_error(
                "owner bulk remove requires --yes",
                "validation_error",
            )

    with _db(mutate=True) as connection:
        if note_id is not None:
            found = get_note(connection, note_id)
            if found is None:
                output_error(f"note {note_id} not found", "not_found")
            with cli_mutation("note", "remove", entity_id=note_id):
                delete_note(connection, note_id)
                operator_event("note.remove", entity_id=note_id)
                output_entity("note", found)
            return

        if contact_email is not None:
            owner = _resolve_contact(connection, contact_email)
            owner_key: dict[str, object] = {"contact_email": owner.email}
            owner_kwargs: dict[str, str] = {"contact_id": owner.id}
            mutation_attrs: dict[str, object] = {
                "owner_type": "contact",
                "owner_id": owner.id,
            }
        else:
            assert company_domain is not None
            company = _resolve_company(connection, company_domain)
            owner_key = {"company_domain": company.domain}
            owner_kwargs = {"company_id": company.id}
            mutation_attrs = {
                "owner_type": "company",
                "owner_id": company.id,
            }
        with cli_mutation("note", "remove", **mutation_attrs):
            note_ids = delete_notes(connection, **owner_kwargs)
            operator_event(
                "note.remove",
                removed_count=len(note_ids),
                **mutation_attrs,
            )
            output(
                {
                    "notes_removed": {
                        "owner": owner_key,
                        "removed_count": len(note_ids),
                        "note_ids": note_ids,
                    }
                },
                record_count=len(note_ids),
            )


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
        list_notes,
    )

    if (contact_email is None) == (company_domain is None):
        output_error(
            "exactly one of --contact-email or --company-domain is required",
            "validation_error",
        )
    with _db() as connection:
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


@note.command("view")
@click.argument("note_id")
def note_view(note_id: str) -> None:
    """View a note by ID."""
    from mailpilot.database import get_note

    with _db() as connection:
        found = get_note(connection, note_id)
        if found is None:
            output_error(f"note {note_id} not found", "not_found")
        output_entity("note", found)


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
    with _db(mutate=True) as connection:
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
    from mailpilot.database import get_workflow, update_workflow
    from mailpilot.operator_log import cli_mutation, operator_event

    if account_email is None:
        output_error(
            "nothing to update: provide --account-email to re-bind the account "
            "(def fields are import-only -- edit the TOML and re-import)",
            "validation_error",
        )
    with _db(mutate=True) as connection:
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


@workflow.command("search")
@click.argument("query")
@limit_option
def workflow_search(query: str, limit: int) -> None:
    """Search workflows by name or goal."""
    from mailpilot.database import search_workflows

    with _db() as connection:
        workflows = search_workflows(connection, query, limit=limit)
        output({"workflows": [w.model_dump(mode="json") for w in workflows]})


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
    from mailpilot.database import list_workflows

    with _db() as connection:
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


@workflow.command("view")
@click.argument("workflow_ref")
def workflow_view(workflow_ref: str) -> None:
    """Show a workflow by name or ID."""
    from mailpilot.database import get_workflow

    with _db() as connection:
        workflow_id = _resolve_workflow_id(connection, workflow_ref)
        found = get_workflow(connection, workflow_id)
        if found is None:
            output_error(f"workflow not found: {workflow_ref}", "not_found")
        output_entity("workflow", found)


@workflow.command("stats")
@click.argument("workflow_ref")
def workflow_stats(workflow_ref: str) -> None:
    """Show the per-campaign funnel for a workflow by name or ID."""
    from mailpilot.database import get_workflow_stats

    with _db() as connection:
        workflow_id = _resolve_workflow_id(connection, workflow_ref)
        stats = get_workflow_stats(connection, workflow_id)
        if stats is None:
            output_error(f"workflow not found: {workflow_ref}", "not_found")
        output({"workflow_stats": stats.model_dump(mode="json")})


@workflow.command("report")
@click.argument("workflow_ref")
@click.option(
    "--stuck",
    is_flag=True,
    default=False,
    help="Only enrollments matching stuck heuristics.",
)
@click.option(
    "--touch",
    type=int,
    default=None,
    help="Filter enrollment matrix by touch number.",
)
@enum_option("--status", "status", _ENROLLMENT_STATUSES, "Filter enrollment matrix.")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "table", "csv", "ndjson"], case_sensitive=False),
    default="json",
    show_default=True,
    help="Output format (default JSON envelope).",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, writable=True, path_type=str),
    default=None,
    help="Write csv/ndjson to this path (status envelope on stdout).",
)
@limit_option(default=500)
def workflow_report(
    workflow_ref: str,
    stuck: bool,
    touch: int | None,
    status: str | None,
    output_format: str,
    out_path: str | None,
    limit: int,
) -> None:
    """Composite campaign report: funnel + tasks + enrollment matrix."""
    from mailpilot.database import get_workflow_report

    with _db() as connection:
        workflow_id = _resolve_workflow_id(connection, workflow_ref)
        report = get_workflow_report(
            connection,
            workflow_id,
            stuck=stuck,
            touch=touch,
            status=status,
            limit=limit,
        )
        if report is None:
            output_error(f"workflow not found: {workflow_ref}", "not_found")
        payload = report.model_dump(mode="json")
        _emit_formatted(
            "workflow_report",
            payload,
            rows=payload.get("enrollments", []),
            output_format=output_format,
            out_path=out_path,
        )


@workflow.command("review")
@click.argument("workflow_ref")
@click.option(
    "--since",
    default=None,
    help="ISO datetime inclusive lower bound on the review window.",
)
@click.option(
    "--until",
    default=None,
    help="ISO datetime inclusive upper bound on the review window.",
)
def workflow_review(
    workflow_ref: str,
    since: str | None,
    until: str | None,
) -> None:
    """Dated campaign collect: funnel, tasks, window mail, enrollments."""
    from datetime import UTC, datetime

    from mailpilot.database import (
        get_workflow,
        get_workflow_review,
        list_active_workflows,
    )

    if since is None or until is None:
        output_error(
            "--since and --until are required ISO datetimes",
            "validation_error",
        )
    try:
        since_dt = datetime.fromisoformat(since)
        until_dt = datetime.fromisoformat(until)
    except ValueError as exc:
        output_error(f"invalid --since/--until value: {exc}", "validation_error")
    if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=UTC)
    if until_dt.tzinfo is None:
        until_dt = until_dt.replace(tzinfo=UTC)

    with _db() as connection:
        if workflow_ref.casefold() == "all":
            workflow_ids = [w.id for w in list_active_workflows(connection)]
        else:
            workflow_id = _resolve_workflow_id(connection, workflow_ref)
            found = get_workflow(connection, workflow_id)
            if found is None:
                output_error(f"workflow not found: {workflow_ref}", "not_found")
            workflow_ids = [workflow_id]
        review = get_workflow_review(
            connection,
            workflow_ids,
            since=since_dt.isoformat(),
            until=until_dt.isoformat(),
        )
        output(
            {"workflow_review": review.model_dump(mode="json")},
            record_count=len(review.reviews),
        )


@workflow.command("status")
@click.argument("workflow_ref")
def workflow_status_cmd(workflow_ref: str) -> None:
    """Ops-health for a workflow (wording, run loop, overdue/failed tasks)."""
    from mailpilot.database import get_workflow_status_health

    with _db() as connection:
        workflow_id = _resolve_workflow_id(connection, workflow_ref)
        health = get_workflow_status_health(connection, workflow_id)
        if health is None:
            output_error(f"workflow not found: {workflow_ref}", "not_found")
        output({"workflow_status": health.model_dump(mode="json")})


def _read_workflow_check_catalog(
    files: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    """Read catalog ``*.toml`` defs into a ``{name -> entry}`` map (§V.134).

    Reuses the import loader's TOML-only file-vs-dir dispatch and ``**/*.toml``
    recurse (§V.103) but keys each entry on its ``name`` field -- ``workflow
    check`` reads the field, not the file stem (§V.134). ``--file`` is
    repeatable, so every passed source is read and merged; on a duplicate
    ``name`` across files the last def wins (§V.134). A malformed file or an
    entry missing ``name`` exits ``validation_error`` per the closed error
    vocabulary (§V.54); an empty ``files`` exits ``validation_error`` too.

    Returns:
        The merged catalog keyed by each def's ``name`` field.
    """
    if not files:
        output_error(
            "no input: provide --file PATH (a '.toml' file or a directory)",
            "validation_error",
        )
    catalog: dict[str, dict[str, Any]] = {}
    for file in files:
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
    return catalog


@workflow.command("check")
@click.option(
    "--file",
    "files",
    multiple=True,
    type=click.Path(exists=True),
    help=(
        "Catalog source (TOML only): a '.toml' file or a directory of '*.toml' "
        "defs (directories recurse). Repeatable. The report lists only "
        "workflows found under --file."
    ),
)
@click.option(
    "--account-email",
    default=None,
    help=(
        "Owning Gmail account (email or ID). With --file, report that "
        "account's full envelope including orphaned rows."
    ),
)
def workflow_check(files: tuple[str, ...], account_email: str | None) -> None:
    """Report wording drift between catalog defs and live workflow rows.

    A read-only 2-way live SHA-256 over the wording fields
    {template, theme, goal, instructions}, joined by the globally unique name.
    Mirrors ``db check`` but is report-only: every state (in_sync, out_of_sync,
    not_imported, orphaned) exits 0 with ``ok:true`` -- the check informs, it is
    never a deploy gate.

    ``--file`` is repeatable and always path-scopes the report to discovered
    catalog names (file or directory). A live row you did not pass never
    appears as orphaned. Pass ``--account-email`` with ``--file`` to restore
    that account's full envelope, where a row with no def surfaces as
    orphaned drift.
    """
    from mailpilot.database import check_workflow_wording

    catalog = _read_workflow_check_catalog(files)
    with _db() as connection:
        account_id = None
        scope_to_catalog = True
        if account_email is not None:
            account_id = _resolve_account(connection, account_email).id
            scope_to_catalog = False
        report = check_workflow_wording(
            connection,
            catalog,
            scope_to_catalog=scope_to_catalog,
            account_id=account_id,
        )
    output({"workflow_check": report.model_dump(mode="json")})


@workflow.command("start")
@click.argument("workflow_ref")
def workflow_start(workflow_ref: str) -> None:
    """Start a workflow by name or ID (requires non-empty goal and instructions)."""
    from mailpilot.database import activate_workflow
    from mailpilot.operator_log import cli_mutation, operator_event

    with _db(mutate=True) as connection:
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


@workflow.command("stop")
@click.argument("workflow_ref")
def workflow_stop(workflow_ref: str) -> None:
    """Stop an active workflow by name or ID."""
    from mailpilot.database import pause_workflow
    from mailpilot.operator_log import cli_mutation, operator_event

    with _db(mutate=True) as connection:
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
        list_workflows_full,
    )

    with _db() as connection:
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


_WORKFLOW_IMPORT_UPDATABLE = (
    "goal",
    "instructions",
    "theme",
    "touches",
    "touch_interval_days",
)
_IMPORT_EXCERPT_HEAD = 160
_IMPORT_EXCERPT_TAIL = 160


def _import_field_excerpt(value: object) -> str:
    """Short preview of a mutated def field (§V.103).

    Instructions keep a tail so ready-copy at the end of a long body is
    visible without a follow-up ``workflow view``.
    """
    if value is None:
        return ""
    text = str(value)
    limit = _IMPORT_EXCERPT_HEAD + _IMPORT_EXCERPT_TAIL + 5
    if len(text) <= limit:
        return text
    return f"{text[:_IMPORT_EXCERPT_HEAD]}...{text[-_IMPORT_EXCERPT_TAIL:]}"


def _projected_import_def(entry: dict[str, Any], current: Any | None) -> dict[str, Any]:
    """Def fields as import would persist them, for the post-apply hash."""

    def merged(field: str, default: object) -> object:
        if field in entry:
            return entry[field]
        if current is not None:
            return getattr(current, field)
        return default

    theme = merged("theme", "blue")
    goal = merged("goal", "")
    instructions = merged("instructions", "")
    return {
        "template": str(entry.get("template") or (current.template if current else "")),
        "theme": str(theme or "blue"),
        "goal": str(goal or ""),
        "instructions": str(instructions or ""),
        "touches": merged("touches", None),
        "touch_interval_days": merged("touch_interval_days", None),
    }


def _import_applied_preview(
    name: str,
    action: str,
    entry: dict[str, Any],
    current: Any | None,
    changed: dict[str, object],
) -> dict[str, object]:
    """Per-row import preview: action + post-apply sync + changed excerpts."""
    from mailpilot.database import import_row_in_sync

    projected = _projected_import_def(entry, current)
    in_sync = import_row_in_sync(entry, projected)
    return {
        "name": name,
        "action": action,
        "in_sync": in_sync,
        "changed": {
            key: _import_field_excerpt(value) for key, value in changed.items()
        },
    }


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
    preview_changed: dict[str, object] = {"theme": entry.get("theme") or "blue"}
    preview_changed.update(extras)
    event_changed = ["name", "template", "account_id", "theme", *extras.keys()]
    if activated:
        event_changed.append("status")
    operator_event(
        "workflow.import",
        entity_id=created.id,
        account_id=account_id,
        name=name,
        changed=event_changed,
    )
    return _import_applied_preview(name, "created", entry, None, preview_changed)


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
        return _import_applied_preview(current.name, "unchanged", entry, current, {})
    update_workflow(connection, current.id, **diff)
    operator_event(
        "workflow.import",
        entity_id=current.id,
        account_id=current.account_id,
        name=current.name,
        changed=list(diff.keys()),
    )
    return _import_applied_preview(current.name, "updated", entry, current, diff)


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
    ``name == stem`` bijection (§V.103). Recurses ``**/*.toml`` so a campaigns
    tree (``campaigns/<slug>/workflows/<slug>.toml``) is one ``--file`` source.
    A file that fails to parse becomes a per-row error so the rest of the
    catalog still imports (§V.63).
    """
    import tomllib

    entries: list[tuple[str, dict[str, Any]]] = []
    pre_errors: list[dict[str, object]] = []
    for toml_path in sorted(path.rglob("*.toml")):
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
    directory recurses ``**/*.toml`` (catalog batch, per-file parse errors
    become per-row pre-errors) and a single ``.toml`` file parses to one
    entry. Each entry carries its file stem so import can enforce the
    ``name == stem`` bijection (§V.103). A missing ``--file`` or a non-TOML
    path exits via ``output_error`` with ``validation_error``.
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
        "(catalog entry); a directory recurses and imports every '*.toml' "
        "under it."
    ),
)
def workflow_import(account_email: str | None, file: str | None) -> None:
    """Import workflows for an account from TOML catalog files.

    TOML-only -- no JSON, no stdin. Dispatch is by ``--file`` shape:

    * ``--file X.toml`` -- one workflow as pure TOML; ``instructions`` may use a
      multi-line literal string.
    * ``--file <dir>`` -- every ``*.toml`` under the directory, recursively
      (catalog batch); a file that fails to parse becomes a per-row error and
      the batch continues.

    Each parsed entry feeds the same upsert (keyed on ``(account_id, name)``):
    workflows absent from the DB are created (and activated when both
    ``goal`` and ``instructions`` are non-empty), present workflows are
    updated for changed fields only, ``template`` differences emit a per-row
    ``template_immutable`` error, and ``status`` is never written by import.

    Applied rows carry ``action`` (created / updated / unchanged), ``in_sync``
    (post-apply wording-hash match), and ``changed`` (mutated def fields with
    a short excerpt). The terminal envelope aggregates: top-level ``applied``
    and ``rejected`` counts on every import envelope; zero applied rows -> an
    ``import_failed`` error envelope on stderr (per-row rows inlined) and exit
    1, so scripts gating on the exit code never mistake a no-op import for
    success.
    """
    from mailpilot.database import (
        list_workflows_full,
    )
    from mailpilot.operator_log import cli_mutation

    entries, pre_errors = _load_workflow_import_entries(file)

    with _db(mutate=True) as connection:
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


def _parse_future_scheduled_at(value: str | None) -> str | None:
    """Parse ``--scheduled-at`` and reject past or unparseable values."""
    if value is None:
        return None
    from datetime import UTC, datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        output_error(f"invalid --scheduled-at value: {exc}", "validation_error")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if parsed <= datetime.now(UTC):
        output_error("--scheduled-at must be in the future", "validation_error")
    return parsed.isoformat()


def _same_scheduled_instant(existing: Any, scheduled_iso: str) -> bool:
    """True when ``existing`` and the parsed ISO string are the same instant."""
    from datetime import UTC, datetime

    wanted = datetime.fromisoformat(scheduled_iso)
    if wanted.tzinfo is None:
        wanted = wanted.replace(tzinfo=UTC)
    have = existing
    if have.tzinfo is None:
        have = have.replace(tzinfo=UTC)
    return have == wanted


def _is_first_reach_task(task: Any) -> bool:
    """True when the pending row is a first-reach (T1), not T2+ (§V.32)."""
    from mailpilot.cadence import resolve_touch_number

    context = task.context or {}
    trigger = str(context.get("trigger") or "")
    touch = resolve_touch_number(context, trigger)
    if touch is not None and touch >= 2:
        return False
    return trigger in ("enrollment_schedule", "enrollment_run")


def _maybe_schedule_first_touch(
    connection: Any,
    enrollment_id: str,
    workflow_id: str,
    contact_id: str,
    scheduled_iso: str | None,
    changed: list[str],
    *,
    enrollment_status: str,
    emails_sent: int,
    commit: bool = True,
) -> None:
    """Insert or last-write-wins a pending first-touch task per §V.32.

    New enrollment (no pending first-reach): insert once. Re-run on an
    active ``emails_sent=0`` enrollment with a pending first-reach: UPDATE
    ``scheduled_at`` in place when the parsed instant differs, persist
    ``touch`` 1 if absent, and append ``scheduled_first_send`` to
    ``changed``. Same instant, later touch, already-sent, or non-active
    enrollment: no-op. Never inserts a second first-reach.
    """
    if scheduled_iso is None:
        return
    from mailpilot.database import (
        create_task,
        find_pending_first_touch_task,
        update_pending_first_touch_schedule,
    )

    existing = find_pending_first_touch_task(connection, enrollment_id)
    if existing is not None:
        if not _is_first_reach_task(existing):
            return
        if emails_sent > 0 or enrollment_status != "active":
            return
        if _same_scheduled_instant(existing.scheduled_at, scheduled_iso):
            return
        update_pending_first_touch_schedule(
            connection,
            task=existing,
            scheduled_at=scheduled_iso,
            commit=commit,
        )
        changed.append("scheduled_first_send")
        return
    if emails_sent > 0:
        return
    create_task(
        connection,
        enrollment_id=enrollment_id,
        workflow_id=workflow_id,
        contact_id=contact_id,
        description="scheduled first reach-out",
        scheduled_at=scheduled_iso,
        context={"trigger": "enrollment_schedule", "touch": 1},
        email_id=None,
        commit=commit,
    )
    changed.append("scheduled_first_send")


def _reject_enrollment_add_source_xor(
    contact_email: str | None,
    tag_ref: str | None,
    file_path: str | None,
    dry_run: bool,
    scheduled_at: str | None,
) -> None:
    """Reject exclusive / required source flag combinations."""
    if tag_ref is not None and contact_email is not None:
        output_error(
            "--tag is exclusive with --contact-email",
            "validation_error",
        )
    if file_path is not None and tag_ref is not None:
        output_error("--file is exclusive with --tag", "validation_error")
    if file_path is not None and contact_email is not None:
        output_error(
            "--file is exclusive with --contact-email",
            "validation_error",
        )
    if dry_run and tag_ref is None and file_path is None:
        output_error(
            "--dry-run requires --tag or --file",
            "validation_error",
        )
    if tag_ref is not None and not dry_run and scheduled_at is None:
        output_error(
            "--tag apply requires --scheduled-at (or pass --dry-run)",
            "validation_error",
        )
    if file_path is not None and not dry_run and scheduled_at is None:
        output_error(
            "--file apply requires --scheduled-at (or pass --dry-run)",
            "validation_error",
        )
    if tag_ref is None and file_path is None and contact_email is None:
        output_error(
            "--contact-email is required (or --tag / --file for a batch)",
            "validation_error",
        )


def _reject_enrollment_add_pack_flags(
    tag_ref: str | None,
    file_path: str | None,
    min_contacts: int | None,
    limit: int | None,
    company_atomic: bool,
    exclude_peer: bool,
) -> None:
    """Reject packing / filter flags used on the wrong source."""
    if min_contacts is not None and tag_ref is None:
        output_error(
            "--min-contacts is only valid with --tag",
            "validation_error",
        )
    if min_contacts is not None and min_contacts < 0:
        output_error("--min-contacts must be >= 0", "validation_error")
    if limit is not None and limit < 1:
        output_error("--limit must be >= 1", "validation_error")
    batch_source = tag_ref is not None or file_path is not None
    if (limit is not None or company_atomic or exclude_peer) and not batch_source:
        output_error(
            "--limit / --company-atomic / --exclude-peer require --file or --tag",
            "validation_error",
        )


def _validate_enrollment_add_args(
    contact_email: str | None,
    tag_ref: str | None,
    file_path: str | None,
    dry_run: bool,
    min_contacts: int | None,
    scheduled_at: str | None,
    limit: int | None,
    company_atomic: bool,
    exclude_peer: bool,
) -> str | None:
    """Validate flag combinations for ``enrollment add``; return scheduled ISO."""
    from datetime import datetime

    _reject_enrollment_add_source_xor(
        contact_email, tag_ref, file_path, dry_run, scheduled_at
    )
    _reject_enrollment_add_pack_flags(
        tag_ref, file_path, min_contacts, limit, company_atomic, exclude_peer
    )
    if dry_run and scheduled_at is not None:
        output_error(
            "--scheduled-at is exclusive with --dry-run",
            "validation_error",
        )
    if scheduled_at is None:
        return None
    if (tag_ref is not None or file_path is not None) and not dry_run:
        return _parse_future_scheduled_at(scheduled_at)
    try:
        return datetime.fromisoformat(scheduled_at).isoformat()
    except ValueError as exc:
        output_error(f"invalid --scheduled-at value: {exc}", "validation_error")


def _calendar_day(iso: str) -> Any:
    """Local calendar date of an ISO instant (naive treated as UTC)."""
    from datetime import UTC, datetime

    parsed = datetime.fromisoformat(iso)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.date()


def _read_enrollment_batch_file(path: str) -> list[tuple[str, str | None]]:
    """Parse ``--file`` JSON into ``(email, scheduled_at_override)`` rows."""
    import pathlib

    file_path = pathlib.Path(path)
    if not file_path.is_file():
        output_error(f"file not found: {path}", "not_found")
    try:
        raw: object = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        output_error(f"invalid JSON: {exc}", "validation_error")
    if not isinstance(raw, list):
        output_error("enrollment file must be a JSON array", "validation_error")
    rows: dict[str, str | None] = {}
    for index, item in enumerate(raw):
        if isinstance(item, str):
            email = item.strip()
            if not email:
                output_error(f"missing email at index {index}", "validation_error")
            rows[email.lower()] = None
            continue
        if isinstance(item, dict):
            email_val = item.get("email")
            if not isinstance(email_val, str) or not email_val.strip():
                output_error(f"missing email at index {index}", "validation_error")
            override = item.get("scheduled_at")
            if override is not None and not isinstance(override, str):
                output_error(
                    f"scheduled_at must be a string at index {index}",
                    "validation_error",
                )
            rows[email_val.strip().lower()] = override
            continue
        output_error(f"invalid entry at index {index}", "validation_error")
    return list(rows.items())


def _pack_enrollment_preview(
    preview: Any,
    *,
    limit: int | None,
    company_atomic: bool,
    exclude_peer: bool,
) -> Any:
    """Apply §V.171 packing flags onto a dry-run preview (no writes)."""
    from mailpilot.database import apply_enrollment_packing
    from mailpilot.models import EnrollmentPreview

    contacts, excluded = apply_enrollment_packing(
        preview.contacts,
        preview.excluded,
        limit=limit,
        company_atomic=company_atomic,
        exclude_peer=exclude_peer,
    )
    return EnrollmentPreview(
        workflow=preview.workflow,
        tag=preview.tag,
        count=len(contacts),
        contacts=contacts,
        excluded=excluded,
    )


def _emit_enrollment_preview(preview: Any) -> None:
    """Write the ``enrollment_preview`` envelope."""
    output(
        {"enrollment_preview": preview.model_dump(mode="json")},
        record_count=preview.count,
    )


def _enrollment_add_tag_preview(
    connection: Any,
    workflow: Any,
    tag_ref: str,
    min_contacts: int | None,
    *,
    limit: int | None = None,
    company_atomic: bool = False,
    exclude_peer: bool = False,
) -> None:
    """Dry-run company-or-contact tag cohort enrollment preview (no writes)."""
    from mailpilot.database import get_account, preview_enrollment_tag_cohort

    tag = _resolve_tag(connection, tag_ref)
    account = get_account(connection, workflow.account_id)
    account_email = account.email if account is not None else None
    preview = preview_enrollment_tag_cohort(
        connection,
        workflow,
        tag,
        min_contacts=min_contacts,
        account_email=account_email,
    )
    packed = _pack_enrollment_preview(
        preview,
        limit=limit,
        company_atomic=company_atomic,
        exclude_peer=exclude_peer,
    )
    _emit_enrollment_preview(packed)


def _enrollment_add_file_preview(
    connection: Any,
    workflow: Any,
    file_rows: list[tuple[str, str | None]],
    *,
    limit: int | None = None,
    company_atomic: bool = False,
    exclude_peer: bool = False,
) -> None:
    """Dry-run ``--file`` cohort preview (no writes)."""
    from mailpilot.database import get_account, preview_enrollment_file_cohort

    rows = file_rows
    account = get_account(connection, workflow.account_id)
    account_email = account.email if account is not None else None
    preview, _missing = preview_enrollment_file_cohort(
        connection,
        workflow,
        [email for email, _override in rows],
        account_email=account_email,
        drop_already_enrolled=False,
    )
    packed = _pack_enrollment_preview(
        preview,
        limit=limit,
        company_atomic=company_atomic,
        exclude_peer=exclude_peer,
    )
    _emit_enrollment_preview(packed)


def _apply_one_enrollment(
    connection: Any,
    workflow: Any,
    contact: Any,
    scheduled_iso: str | None,
    *,
    commit: bool = True,
) -> tuple[Any, list[str]] | None:
    """Create or reuse an enrollment and optionally schedule first touch.

    Returns ``(enrollment, changed)`` or None when the existing row cannot
    be loaded after an insert race.
    """
    from mailpilot.database import (
        count_outbound_sent,
        create_activity,
        create_enrollment,
        get_enrollment,
    )

    created = create_enrollment(connection, workflow.id, contact.id, commit=commit)
    if created is not None:
        create_activity(
            connection,
            contact_id=contact.id,
            activity_type="enrollment_added",
            summary=f"Assigned to {workflow.name}",
            detail={"workflow_name": workflow.name},
            company_id=contact.company_id,
            workflow_id=workflow.id,
            enrollment_id=created.id,
            commit=commit,
        )
        target = created
        changed = ["status"]
        emails_sent = 0
    else:
        existing = get_enrollment(connection, workflow.id, contact.id)
        if existing is None:
            return None
        target = existing
        changed = []
        emails_sent = (
            count_outbound_sent(connection, workflow.id, contact.id)
            if scheduled_iso is not None
            else 0
        )
    _maybe_schedule_first_touch(
        connection,
        target.id,
        workflow.id,
        contact.id,
        scheduled_iso,
        changed,
        enrollment_status=target.status,
        emails_sent=emails_sent,
        commit=commit,
    )
    return target, changed


def _batch_action(created: bool, changed: list[str]) -> EnrollmentBatchAction:
    """Map single-seat changed tokens to a batch ``action`` (§V.171)."""
    if created:
        return "created"
    if "scheduled_first_send" in changed:
        return "scheduled_first_send"
    return "unchanged"


def _assert_company_atomic_days(
    seats: list[tuple[Any, str]],
) -> None:
    """Reject mixed calendar days on one domain when ``--company-atomic``."""
    days: dict[str, Any] = {}
    for contact, iso in seats:
        domain = contact.company_domain
        if not domain:
            continue
        day = _calendar_day(iso)
        previous = days.get(domain)
        if previous is not None and previous != day:
            output_error(
                f"--company-atomic: {domain} has seats on more than one calendar day",
                "validation_error",
            )
        days[domain] = day


def _enrollment_add_contact(
    connection: Any,
    workflow: Any,
    contact_email: str,
    scheduled_iso: str | None,
) -> None:
    """Enroll a single contact, optionally scheduling first touch."""
    from mailpilot.database import get_account
    from mailpilot.operator_log import cli_mutation, operator_event

    if scheduled_iso is not None and workflow.type != "outbound":
        output_error(
            "--scheduled-at only valid for outbound workflows",
            "invalid_state",
        )
    contact = _resolve_contact(connection, contact_email)
    account = get_account(connection, workflow.account_id)
    _reject_enrollment_self_loop(account, contact, workflow.name)
    mutation_attrs: dict[str, Any] = {
        "workflow_id": workflow.id,
        "contact_id": contact.id,
    }
    if scheduled_iso is not None:
        mutation_attrs["scheduled_at"] = scheduled_iso
    with cli_mutation("enrollment", "add", **mutation_attrs):
        applied = _apply_one_enrollment(connection, workflow, contact, scheduled_iso)
        if applied is None:
            return
        target, changed = applied
        event_fields: dict[str, Any] = {
            "enrollment_id": target.id,
            "workflow_id": workflow.id,
            "contact_id": contact.id,
        }
        if scheduled_iso is not None:
            event_fields["scheduled_at"] = scheduled_iso
        event_fields["changed"] = changed
        operator_event("enrollment.add", **event_fields)
        output_entity("enrollment", target)


def _resolve_seat_schedule(
    override: str | None,
    scheduled_iso: str,
) -> str:
    """Per-row ``scheduled_at`` override, else the batch flag instant."""
    if override is None:
        return scheduled_iso
    parsed = _parse_future_scheduled_at(override)
    assert parsed is not None
    return parsed


def _enrollment_add_batch(
    connection: Any,
    workflow: Any,
    *,
    scheduled_iso: str,
    source: Literal["file", "tag"],
    tag_name: str | None,
    file_rows: list[tuple[str, str | None]] | None,
    min_contacts: int | None,
    limit: int | None,
    company_atomic: bool,
    exclude_peer: bool,
) -> None:
    """Apply a reviewed tag or file cohort with first-touch schedules."""
    from mailpilot.database import (
        apply_enrollment_packing,
        get_account,
        get_contact_by_email,
        preview_enrollment_file_cohort,
        preview_enrollment_tag_cohort,
    )
    from mailpilot.models import EnrollmentBatch, EnrollmentBatchRow
    from mailpilot.operator_log import cli_mutation, operator_event

    if workflow.type != "outbound":
        output_error(
            "--scheduled-at only valid for outbound workflows",
            "invalid_state",
        )
    account = get_account(connection, workflow.account_id)
    account_email = account.email if account is not None else None
    overrides: dict[str, str | None] = {}
    if source == "tag":
        assert tag_name is not None
        tag = _resolve_tag(connection, tag_name)
        preview = preview_enrollment_tag_cohort(
            connection,
            workflow,
            tag,
            min_contacts=min_contacts,
            account_email=account_email,
        )
    else:
        assert file_rows is not None
        rows = file_rows
        overrides = dict(rows)
        preview, missing = preview_enrollment_file_cohort(
            connection,
            workflow,
            [email for email, _override in rows],
            account_email=account_email,
            drop_already_enrolled=False,
        )
        if missing:
            output_error(
                "contact not found: " + ", ".join(missing),
                "not_found",
            )
    contacts, excluded = apply_enrollment_packing(
        preview.contacts,
        preview.excluded,
        limit=limit,
        company_atomic=company_atomic,
        exclude_peer=exclude_peer,
    )
    seats: list[tuple[Any, str]] = [
        (
            contact,
            _resolve_seat_schedule(overrides.get(contact.email.lower()), scheduled_iso),
        )
        for contact in contacts
    ]
    if company_atomic:
        _assert_company_atomic_days(seats)
    enrolled: list[EnrollmentBatchRow] = []
    mutation_attrs: dict[str, Any] = {
        "workflow_id": workflow.id,
        "source": source,
        "scheduled_at": scheduled_iso,
        "count": len(seats),
    }
    with cli_mutation("enrollment", "add", **mutation_attrs):
        try:
            for contact_row, seat_iso in seats:
                contact = get_contact_by_email(connection, contact_row.email)
                if contact is None:
                    output_error(
                        f"contact not found: {contact_row.email}",
                        "not_found",
                    )
                applied = _apply_one_enrollment(
                    connection,
                    workflow,
                    contact,
                    seat_iso,
                    commit=False,
                )
                if applied is None:
                    continue
                target, changed = applied
                enrolled.append(
                    EnrollmentBatchRow(
                        email=contact_row.email,
                        company_domain=contact_row.company_domain,
                        enrollment_id=target.id,
                        scheduled_at=seat_iso,
                        action=_batch_action("status" in changed, changed),
                    )
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        operator_event(
            "enrollment.add",
            workflow_id=workflow.id,
            source=source,
            count=len(enrolled),
            scheduled_at=scheduled_iso,
            changed=["scheduled_first_send"] if enrolled else ["none"],
        )
    batch = EnrollmentBatch(
        workflow=workflow.name,
        scheduled_at=scheduled_iso,
        source=source,
        tag=preview.tag,
        limit=limit,
        company_atomic=company_atomic,
        count=len(enrolled),
        enrolled=enrolled,
        excluded=excluded,
    )
    output(
        {"enrollment_batch": batch.model_dump(mode="json")},
        record_count=batch.count,
    )


@enrollment.command("add")
@click.option(
    "--workflow-id",
    "workflow_ref",
    required=True,
    help="Workflow name or ID.",
)
@click.option(
    "--contact-email",
    default=None,
    help="Contact (email or ID). Required when not using --tag or --file.",
)
@click.option(
    "--tag",
    "tag_ref",
    default=None,
    help=(
        "Company-or-contact tag cohort. With --dry-run: preview. With "
        "--scheduled-at: apply the packed set."
    ),
)
@click.option(
    "--file",
    "file_path",
    default=None,
    type=click.Path(dir_okay=False),
    help=(
        "JSON array of email strings or {email, scheduled_at} objects. "
        "Exclusive with --tag and --contact-email."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview a --tag or --file cohort; no writes.",
)
@click.option(
    "--min-contacts",
    type=int,
    default=None,
    help="Tag path only: include companies with at least N contacts.",
)
@click.option(
    "--limit",
    "seat_limit",
    type=int,
    default=None,
    help=(
        "Cap included seats (first N by company_domain then email). "
        "Soft cap when combined with --company-atomic."
    ),
)
@click.option(
    "--company-atomic",
    is_flag=True,
    default=False,
    help=(
        "Never split a domain. Last company may exceed --limit. Included "
        "seats on a domain share the same calendar day."
    ),
)
@click.option(
    "--exclude-peer",
    is_flag=True,
    default=False,
    help="Drop contacts with an active enrollment in another workflow.",
)
@click.option(
    "--scheduled-at",
    "scheduled_at",
    default=None,
    help=(
        "ISO 8601 timestamp for scheduled first reach-out (outbound workflows "
        "only). Required for --file / --tag apply. Re-run updates an existing "
        "pending first-reach in place. File rows may override per contact."
    ),
)
def enrollment_add(
    workflow_ref: str,
    contact_email: str | None,
    tag_ref: str | None,
    file_path: str | None,
    dry_run: bool,
    min_contacts: int | None,
    seat_limit: int | None,
    company_atomic: bool,
    exclude_peer: bool,
    scheduled_at: str | None,
) -> None:
    """Enroll a contact, preview a cohort, or apply a scheduled batch.

    Single-contact path: ``--workflow-id`` + ``--contact-email``. When
    ``--scheduled-at`` is given on an outbound workflow, a pending first
    reach-out is inserted, or an existing never-sent first-reach is
    updated in place. Tag / file dry-run: ``--tag`` or ``--file`` plus
    ``--dry-run`` returns ``enrollment_preview`` with no writes. Tag /
    file apply: same source plus ``--scheduled-at`` writes one
    ``enrollment_batch`` envelope. ``--tag`` matches company tags or
    contact tags (union, unique by contact).
    """
    from mailpilot.database import get_workflow

    scheduled_iso = _validate_enrollment_add_args(
        contact_email,
        tag_ref,
        file_path,
        dry_run,
        min_contacts,
        scheduled_at,
        seat_limit,
        company_atomic,
        exclude_peer,
    )
    file_rows = (
        _read_enrollment_batch_file(file_path) if file_path is not None else None
    )
    with _db(mutate=True) as connection:
        workflow_id = _resolve_workflow_id(connection, workflow_ref)
        workflow = get_workflow(connection, workflow_id)
        if workflow is None:
            output_error(f"workflow not found: {workflow_ref}", "not_found")
        if dry_run and tag_ref is not None:
            _enrollment_add_tag_preview(
                connection,
                workflow,
                tag_ref,
                min_contacts,
                limit=seat_limit,
                company_atomic=company_atomic,
                exclude_peer=exclude_peer,
            )
            return
        if dry_run and file_path is not None:
            assert file_rows is not None
            _enrollment_add_file_preview(
                connection,
                workflow,
                file_rows,
                limit=seat_limit,
                company_atomic=company_atomic,
                exclude_peer=exclude_peer,
            )
            return
        if tag_ref is not None or file_path is not None:
            assert scheduled_iso is not None
            _enrollment_add_batch(
                connection,
                workflow,
                scheduled_iso=scheduled_iso,
                source="tag" if tag_ref is not None else "file",
                tag_name=tag_ref,
                file_rows=file_rows,
                min_contacts=min_contacts,
                limit=seat_limit,
                company_atomic=company_atomic,
                exclude_peer=exclude_peer,
            )
            return
        assert contact_email is not None
        _enrollment_add_contact(connection, workflow, contact_email, scheduled_iso)


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
    )
    from mailpilot.settings import get_settings

    settings = get_settings()
    with _db(mutate=True) as connection:
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
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    if reason.strip() == "":
        output_error("reason cannot be empty", "validation_error")
    with _db(mutate=True) as connection:
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
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    with _db(mutate=True) as connection:
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


@enrollment.command("view")
@click.argument("enrollment_id")
def enrollment_view(enrollment_id: str) -> None:
    """View an enrollment by id."""
    from mailpilot.database import get_enrollment_by_id

    with _db() as connection:
        record = get_enrollment_by_id(connection, enrollment_id)
        if record is None:
            output_error("enrollment not found", "not_found")
        output_entity("enrollment", record)


@enrollment.command("list")
@scope_option("--workflow-id", "workflow_id", "Filter by workflow (name or ID).")
@scope_option("--contact-email", "contact_email", "Filter by contact (email or ID).")
@enum_option("--status", "status", _ENROLLMENT_STATUSES, "Filter by enrollment status.")
@click.option(
    "--disposition",
    default=None,
    help=(
        "Filter by latest terminal disposition: meeting_booked, do_not_contact, "
        "or contact_later. Unknown values return validation_error with allowed set."
    ),
)
@click.option(
    "--full",
    "full",
    is_flag=True,
    default=False,
    help="Denser projection: company, touch progress, next send, disposition.",
)
@click.option(
    "--stuck",
    is_flag=True,
    default=False,
    help="Only enrollments matching stuck heuristics (implies denser fields).",
)
@presence_option("pending-task", "Filter on presence of a pending follow-up task.")
@click.option(
    "--touch",
    type=int,
    default=None,
    help=(
        "Filter by next pending touch number (or last sent when none pending). "
        "Touch 1 also matches never-sent rows that have a scheduled first send."
    ),
)
@sort_option(["updated_at", "next_scheduled_at"], default="updated_at")
@desc_option
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "table", "csv", "ndjson"], case_sensitive=False),
    default="json",
    show_default=True,
    help="Output format (default JSON envelope).",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, writable=True, path_type=str),
    default=None,
    help="Write csv/ndjson to this path (status envelope on stdout).",
)
@time_window_options("updated_at")
@limit_option
def enrollment_list(
    workflow_id: str | None,
    contact_email: str | None,
    status: str | None,
    disposition: str | None,
    full: bool,
    stuck: bool,
    has_pending_task: bool | None,
    touch: int | None,
    sort: str,
    desc: bool,
    output_format: str,
    out_path: str | None,
    limit: int,
    since: str | None,
    until: str | None,
) -> None:
    """List enrollments as summaries. Filter by workflow, contact, or both.

    Default rows stay lean. Pass --full for company, touch progress, next send,
    and disposition fields used in campaign triage. --disposition filters by
    latest terminal disposition (meeting_booked, do_not_contact, contact_later).
    """
    from mailpilot.database import (
        get_workflow,
        list_enrollments_detailed,
    )
    from mailpilot.models import ENROLLMENT_FULL_FIELDS

    if disposition is not None and disposition not in _ENROLLMENT_DISPOSITIONS:
        allowed = ", ".join(_ENROLLMENT_DISPOSITIONS)
        output_error(
            f"invalid disposition {disposition!r}; allowed: {allowed}",
            "validation_error",
        )

    with _db() as connection:
        # Polymorphic name|UUID resolve (§V.107/§V.152); UUID existence still
        # validated so unknown ids stay not_found (same envelope as today).
        resolved_workflow_id: str | None = None
        if workflow_id is not None:
            resolved_workflow_id = _resolve_workflow_id(connection, workflow_id)
            if get_workflow(connection, resolved_workflow_id) is None:
                output_error(f"workflow not found: {workflow_id}", "not_found")
        contact_id = (
            _resolve_contact(connection, contact_email).id
            if contact_email is not None
            else None
        )
        use_full = full or stuck
        rows = list_enrollments_detailed(
            connection,
            workflow_id=resolved_workflow_id,
            contact_id=contact_id,
            status=status,
            limit=limit,
            since=since,
            until=until,
            full=use_full,
            has_pending_task=has_pending_task,
            touch=touch,
            sort=sort,
            desc=desc,
            stuck=stuck,
            disposition=disposition,
        )
        exclude = None if use_full else set(ENROLLMENT_FULL_FIELDS)
        dumped = [r.model_dump(mode="json", exclude=exclude) for r in rows]
        if output_format.lower() == "json":
            output({"enrollments": dumped})
        else:
            _emit_formatted(
                "enrollments",
                {"enrollments": dumped},
                rows=dumped,
                output_format=output_format,
                out_path=out_path,
            )


# -- Task commands -------------------------------------------------------------


@main.group()
def task() -> None:
    """Manage deferred agent tasks."""


@task.command("list")
@scope_option("--workflow-id", "workflow_id", "Filter by workflow (name or ID).")
@scope_option("--contact-email", "contact_email", "Filter by contact (email or ID).")
@enum_option("--status", "status", _TASK_STATUSES, "Filter by task status.")
@enum_option("--trigger", "trigger", _TASK_TRIGGERS, "Filter by task trigger.")
@click.option(
    "--overdue",
    is_flag=True,
    default=False,
    help="Only pending tasks with scheduled_at in the past.",
)
@touch_option
@time_window_options("scheduled_at")
@limit_option
def task_list(
    workflow_id: str | None,
    contact_email: str | None,
    status: str | None,
    trigger: str | None,
    overdue: bool,
    touches: tuple[int, ...],
    limit: int,
    since: str | None,
    until: str | None,
) -> None:
    """List tasks as summaries with optional filters."""
    from mailpilot.database import (
        get_workflow,
        list_tasks,
    )

    with _db() as connection:
        # Polymorphic name|UUID resolve (§V.107); UUID existence still
        # validated so unknown ids stay not_found (same envelope as today).
        resolved_workflow_id: str | None = None
        if workflow_id is not None:
            resolved_workflow_id = _resolve_workflow_id(connection, workflow_id)
            if get_workflow(connection, resolved_workflow_id) is None:
                output_error(f"workflow not found: {workflow_id}", "not_found")
        contact_id = (
            _resolve_contact(connection, contact_email).id
            if contact_email is not None
            else None
        )
        tasks = list_tasks(
            connection,
            workflow_id=resolved_workflow_id,
            contact_id=contact_id,
            status=status,
            trigger=trigger,
            limit=limit,
            since=since,
            until=until,
            overdue=overdue,
            touches=list(touches) if touches else None,
        )
        output({"tasks": [t.model_dump(mode="json") for t in tasks]})


@task.command("stats")
@scope_option("--workflow-id", "workflow_id", "Filter by workflow (name or ID).")
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
    )

    with _db() as connection:
        # Polymorphic name|UUID resolve (§V.107); UUID existence still
        # validated so unknown ids stay not_found (same envelope as today).
        resolved_workflow_id: str | None = None
        if workflow_id is not None:
            resolved_workflow_id = _resolve_workflow_id(connection, workflow_id)
            if get_workflow(connection, resolved_workflow_id) is None:
                output_error(f"workflow not found: {workflow_id}", "not_found")
        try:
            ZoneInfo(bucket_tz)
        except ZoneInfoNotFoundError, ValueError:
            output_error(f"unknown timezone: {bucket_tz}", "validation_error")
        stats = get_task_stats(
            connection,
            workflow_id=resolved_workflow_id,
            trigger=trigger,
            bucket_tz=bucket_tz,
        )
        output({"task_stats": stats.model_dump(mode="json")})


@task.command("view")
@click.argument("task_id")
def task_view(task_id: str) -> None:
    """Show a task by ID."""
    from mailpilot.database import get_task

    with _db() as connection:
        found = get_task(connection, task_id)
        if found is None:
            output_error(f"task not found: {task_id}", "not_found")
        output_entity("task", found)


@task.command("cancel")
@click.argument("task_id", required=False, default=None)
@scope_option("--workflow-id", "workflow_id", "Filter by workflow (name or ID).")
@scope_option("--contact-email", "contact_email", "Filter by contact (email or ID).")
@enum_option("--status", "status", _TASK_STATUSES, "Filter by task status.")
@enum_option("--trigger", "trigger", _TASK_TRIGGERS, "Filter by task trigger.")
@click.option(
    "--overdue",
    is_flag=True,
    default=False,
    help="Only pending tasks with scheduled_at in the past.",
)
@touch_option
@time_window_options("scheduled_at")
def task_cancel(
    task_id: str | None,
    workflow_id: str | None,
    contact_email: str | None,
    status: str | None,
    trigger: str | None,
    overdue: bool,
    touches: tuple[int, ...],
    since: str | None,
    until: str | None,
) -> None:
    """Cancel one pending task by ID, or every matching pending task.

    Filter-mode (no TASK_ID) needs at least one of --touch, --workflow-id,
    --contact-email, --trigger, or --overdue. --status defaults to pending;
    any other status is rejected. TASK_ID and filters are exclusive.
    """
    from mailpilot.database import (
        cancel_task,
        cancel_tasks_matching,
        get_workflow,
    )

    has_required_filter = bool(
        touches
        or workflow_id is not None
        or contact_email is not None
        or trigger is not None
        or overdue
    )
    has_any_filter = bool(
        has_required_filter
        or status is not None
        or since is not None
        or until is not None
    )
    if task_id is not None and has_any_filter:
        output_error(
            "TASK_ID is exclusive with filter flags",
            "validation_error",
        )
    if task_id is None and not has_required_filter:
        output_error(
            "TASK_ID or a filter (--touch, --workflow-id, "
            "--contact-email, --trigger, --overdue) is required",
            "validation_error",
        )
    if task_id is None and status is not None and status != "pending":
        output_error(
            f"filter-mode --status must be pending, got {status!r}",
            "validation_error",
        )

    with _db(mutate=True) as connection:
        if task_id is not None:
            cancelled = cancel_task(connection, task_id)
            if cancelled is None:
                output_error(
                    f"task not found or not pending: {task_id}",
                    "not_found",
                )
            output_entity("task", cancelled)
            return

        resolved_workflow_id: str | None = None
        if workflow_id is not None:
            resolved_workflow_id = _resolve_workflow_id(connection, workflow_id)
            if get_workflow(connection, resolved_workflow_id) is None:
                output_error(f"workflow not found: {workflow_id}", "not_found")
        contact_id = (
            _resolve_contact(connection, contact_email).id
            if contact_email is not None
            else None
        )
        result = cancel_tasks_matching(
            connection,
            workflow_id=resolved_workflow_id,
            contact_id=contact_id,
            trigger=trigger,
            overdue=overdue,
            since=since,
            until=until,
            touches=list(touches) if touches else None,
        )
        output(
            {"task_cancel": result.model_dump(mode="json")},
            record_count=result.cancelled_count,
        )


def _validate_task_retry_mode(
    *,
    task_id: str | None,
    touches: tuple[int, ...],
    workflow_id: str | None,
    contact_email: str | None,
    trigger: str | None,
    status: str | None,
    overdue: bool,
    since: str | None,
    until: str | None,
) -> None:
    """Reject TASK_ID+filters XOR, missing scope, and non-retryable status."""
    has_required_filter = bool(
        touches
        or workflow_id is not None
        or contact_email is not None
        or trigger is not None
    )
    has_any_filter = bool(
        has_required_filter
        or status is not None
        or overdue
        or since is not None
        or until is not None
    )
    if task_id is not None and has_any_filter:
        output_error(
            "TASK_ID is exclusive with filter flags",
            "validation_error",
        )
    if task_id is None and not has_required_filter:
        output_error(
            "TASK_ID or a filter (--touch, --workflow-id, "
            "--contact-email, --trigger) is required",
            "validation_error",
        )
    if task_id is None and status is not None and status not in ("failed", "cancelled"):
        output_error(
            f"filter-mode --status must be failed or cancelled, got {status!r}",
            "validation_error",
        )


@task.command("retry")
@click.argument("task_id", required=False, default=None)
@scope_option("--workflow-id", "workflow_id", "Filter by workflow (name or ID).")
@scope_option("--contact-email", "contact_email", "Filter by contact (email or ID).")
@enum_option("--status", "status", _TASK_STATUSES, "Filter by task status.")
@enum_option("--trigger", "trigger", _TASK_TRIGGERS, "Filter by task trigger.")
@click.option(
    "--overdue",
    is_flag=True,
    default=False,
    help="Only pending tasks with scheduled_at in the past.",
)
@touch_option
@time_window_options("scheduled_at")
@click.option(
    "--scheduled-at",
    "scheduled_at",
    default=None,
    help=(
        "ISO 8601 timestamp to requeue at. Applies to every selected row. "
        "Omit to keep a still-future stored time, or now when the stored "
        "time is past."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview matching ids and companies; do not write.",
)
def task_retry(
    task_id: str | None,
    workflow_id: str | None,
    contact_email: str | None,
    status: str | None,
    trigger: str | None,
    overdue: bool,
    touches: tuple[int, ...],
    since: str | None,
    until: str | None,
    scheduled_at: str | None,
    dry_run: bool,
) -> None:
    """Reset failed or cancelled tasks for a fresh attempt.

    Pass TASK_ID to retry one row, or filters to retry every matching
    failed (default) or cancelled row. Filter-mode needs at least one of
    --touch, --workflow-id, --contact-email, or --trigger. --status
    defaults to failed; only failed and cancelled are allowed. TASK_ID
    and filters are exclusive. --scheduled-at applies to every selected
    row. --dry-run previews ids and companies with no writes.
    """
    from mailpilot.database import (
        get_task,
        get_workflow,
        manual_retry_task,
        retry_tasks_matching,
    )

    _validate_task_retry_mode(
        task_id=task_id,
        touches=touches,
        workflow_id=workflow_id,
        contact_email=contact_email,
        trigger=trigger,
        status=status,
        overdue=overdue,
        since=since,
        until=until,
    )
    scheduled_iso = _parse_future_scheduled_at(scheduled_at)
    with _db(mutate=True) as connection:
        if task_id is not None:
            existing = get_task(connection, task_id)
            if existing is None:
                output_error(f"task not found: {task_id}", "not_found")
            if existing.status not in ("failed", "cancelled"):
                output_error(
                    f"task not retryable in status {existing.status!r}: {task_id}",
                    "invalid_state",
                )
            if dry_run:
                result = retry_tasks_matching(
                    connection,
                    status=existing.status,
                    scheduled_at=scheduled_iso,
                    dry_run=True,
                    task_id=task_id,
                )
                output(
                    {"task_retry": result.model_dump(mode="json")},
                    record_count=result.retried_count,
                )
                return
            reset = manual_retry_task(connection, task_id, scheduled_at=scheduled_iso)
            if reset is None:
                output_error(
                    f"task not retryable in status {existing.status!r}: {task_id}",
                    "invalid_state",
                )
            output_entity("task", reset)
            return

        resolved_workflow_id: str | None = None
        if workflow_id is not None:
            resolved_workflow_id = _resolve_workflow_id(connection, workflow_id)
            if get_workflow(connection, resolved_workflow_id) is None:
                output_error(f"workflow not found: {workflow_id}", "not_found")
        contact_id = (
            _resolve_contact(connection, contact_email).id
            if contact_email is not None
            else None
        )
        result = retry_tasks_matching(
            connection,
            workflow_id=resolved_workflow_id,
            contact_id=contact_id,
            status=status if status is not None else "failed",
            trigger=trigger,
            overdue=overdue,
            since=since,
            until=until,
            touches=list(touches) if touches else None,
            scheduled_at=scheduled_iso,
            dry_run=dry_run,
        )
        output(
            {"task_retry": result.model_dump(mode="json")},
            record_count=result.retried_count,
        )


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
    from mailpilot.database import list_meetings

    with _db() as connection:
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


@meeting.command("view")
@click.argument("meeting_id")
def meeting_view(meeting_id: str) -> None:
    """Show a meeting by ID with its attendee contacts inlined."""
    from mailpilot.database import load_meeting_view

    with _db() as connection:
        found = load_meeting_view(connection, meeting_id)
        if found is None:
            output_error(f"meeting not found: {meeting_id}", "not_found")
        output_entity("meeting", found)


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
        link_meeting_attendee,
    )
    from mailpilot.operator_log import cli_mutation, operator_event

    with _db(mutate=True) as connection:
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
    from mailpilot.database import get_meeting, update_meeting
    from mailpilot.operator_log import cli_mutation, operator_event

    with _db(mutate=True) as connection:
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


@meeting.command("cancel")
@click.argument("meeting_id")
def meeting_cancel(meeting_id: str) -> None:
    """Cancel a meeting by setting its status to `cancelled`."""
    from mailpilot.database import get_meeting, update_meeting
    from mailpilot.operator_log import cli_mutation, operator_event

    with _db(mutate=True) as connection:
        if get_meeting(connection, meeting_id) is None:
            output_error(f"meeting not found: {meeting_id}", "not_found")
        with cli_mutation("meeting", "cancel", entity_id=meeting_id):
            updated = update_meeting(connection, meeting_id, status="cancelled")
            if updated is None:
                output_error(f"meeting not found: {meeting_id}", "not_found")
            operator_event("meeting.cancel", entity_id=meeting_id, changed=["status"])
            output_entity("meeting", updated)
