"""Workflow commands."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import pathlib
from typing import Any

import click

from mailpilot._filters import (
    DIRECTIONS,
    enum_option,
    limit_option,
    scope_option,
    time_window_options,
)
from mailpilot.cli.enrollment import _ENROLLMENT_STATUSES
from mailpilot.cli.main import (
    _db,
    _emit_formatted,
    _looks_like_uuid,
    _resolve_account,
    _resolve_workflow,
    _resolve_workflow_id,
    main,
    output,
    output_entity,
    output_error,
)

_WORKFLOW_STATUSES = ["draft", "active", "paused"]

_WORKFLOW_TEMPLATES = ["outbound-general", "inbound-general", "inbound-google-drive"]

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
    from mailpilot.database import update_workflow
    from mailpilot.operator_log import cli_mutation, operator_event

    if account_email is None:
        output_error(
            "nothing to update: provide --account-email to re-bind the account "
            "(def fields are import-only -- edit the TOML and re-import)",
            "validation_error",
        )
    with _db(mutate=True) as connection:
        before = _resolve_workflow(connection, workflow_ref)
        workflow_id = before.id
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


def _attach_workflow_health(connection: Any, summary: Any) -> Any:
    """List rows lack ``touches``; stats needs the loaded Workflow."""
    from mailpilot.database import (
        get_workflow,
        get_workflow_stats,
        get_workflow_status_health,
    )
    from mailpilot.models import WorkflowListOps

    loaded = get_workflow(connection, summary.id)
    if loaded is None:
        return summary
    funnel = get_workflow_stats(connection, loaded)
    status_health = get_workflow_status_health(connection, summary.id)
    ops = None
    if status_health is not None:
        ops = WorkflowListOps(
            wording=status_health.wording,
            run_loop=status_health.run_loop,
            overdue_tasks=status_health.overdue_tasks,
            failed_tasks_24h=status_health.failed_tasks_24h,
        )
    return summary.model_copy(update={"funnel": funnel, "ops": ops})


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
@click.option(
    "--health",
    is_flag=True,
    default=False,
    help=(
        "Embed funnel (same object as workflow stats) and ops "
        "(wording, run_loop, overdue_tasks, failed_tasks_24h) on each row."
    ),
)
@limit_option
def workflow_list(
    account_email: str | None,
    status: str | None,
    workflow_type: str | None,
    template: str | None,
    limit: int,
    since: str | None,
    until: str | None,
    health: bool,
) -> None:
    """List workflows as summaries.

    Pass --health to embed funnel and ops on each row. Lean list (no flag)
    is unchanged. --health composes with existing filters.
    """
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
        if health:
            workflows = [
                _attach_workflow_health(connection, summary) for summary in workflows
            ]
        output({"workflows": [w.model_dump(mode="json") for w in workflows]})


@workflow.command("view")
@click.argument("workflow_ref")
def workflow_view(workflow_ref: str) -> None:
    """Show a workflow by name or ID."""
    with _db() as connection:
        output_entity("workflow", _resolve_workflow(connection, workflow_ref))


@workflow.command("stats")
@click.argument("workflow_ref")
def workflow_stats(workflow_ref: str) -> None:
    """Show the per-campaign funnel for a workflow, or every active one.

    Pass a name or ID for one campaign. Pass ``all`` for every active
    workflow (same set as ``workflow review all``). Envelope key
    ``workflow_stats`` is an object for one slug and an array for ``all``.
    """
    from mailpilot.database import get_workflow_stats, list_active_workflows

    with _db() as connection:
        if workflow_ref.casefold() == "all":
            items = [
                get_workflow_stats(connection, workflow).model_dump(mode="json")
                for workflow in list_active_workflows(connection)
            ]
            output({"workflow_stats": items})
            return
        workflow = _resolve_workflow(connection, workflow_ref)
        stats = get_workflow_stats(connection, workflow)
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
            workflow_ids = [_resolve_workflow_id(connection, workflow_ref)]
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
    """Ops-health for a workflow, or every active one.

    Pass a name or ID for one campaign. Pass ``all`` for every active
    workflow (same set as ``workflow review all``). Envelope key
    ``workflow_status`` is an object for one slug and an array for ``all``.
    """
    from mailpilot.database import get_workflow_status_health, list_active_workflows

    with _db() as connection:
        if workflow_ref.casefold() == "all":
            items: list[dict[str, Any]] = []
            for workflow in list_active_workflows(connection):
                health = get_workflow_status_health(connection, workflow.id)
                if health is None:
                    continue
                items.append(health.model_dump(mode="json"))
            output({"workflow_status": items})
            return
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


def _row_def_payload(row: Any) -> dict[str, Any]:
    """Live workflow def fields for the post-apply wording hash (§V.103)."""
    return {
        "template": row.template,
        "theme": row.theme,
        "goal": row.goal,
        "instructions": row.instructions,
        "touches": row.touches,
        "touch_interval_days": row.touch_interval_days,
    }


def _import_applied_preview(
    name: str,
    action: str,
    entry: dict[str, Any],
    written: Any,
    mutated: dict[str, object],
) -> dict[str, object]:
    """Per-row import preview from the live written row (§V.103/§B.143)."""
    from mailpilot.database import workflow_import_sync_report

    report = workflow_import_sync_report(entry, _row_def_payload(written))
    in_sync = bool(report["in_sync"])
    remaining = report["remaining"]
    changed_src: dict[str, object]
    if in_sync:
        changed_src = mutated
    elif isinstance(remaining, dict):
        changed_src = remaining
    else:
        changed_src = {}
    return {
        "name": name,
        "action": action,
        "in_sync": in_sync,
        "catalog_hash": report["catalog_hash"],
        "row_hash": report["row_hash"],
        "changed": {
            key: _import_field_excerpt(value) for key, value in changed_src.items()
        },
    }


def _workflow_import_extras(entry: dict[str, Any]) -> dict[str, object]:
    """Def fields an import writes onto a freshly created workflow (§V.103).

    Beyond ``name`` / ``template`` / ``theme`` / account set at create time:
    ``goal`` and ``instructions`` when non-empty, plus the cadence pair when
    the catalog projection carries both ints (§V.136). Incomplete cadence
    (one side omitted) persists as single-touch NULL/NULL, matching check.
    """
    from mailpilot.database import catalog_def_fields

    catalog = catalog_def_fields(entry)
    extras: dict[str, object] = {}
    goal = catalog["goal"]
    instructions = catalog["instructions"]
    if goal:
        extras["goal"] = goal
    if instructions:
        extras["instructions"] = instructions
    if catalog["touches"] is not None:
        extras["touches"] = catalog["touches"]
        extras["touch_interval_days"] = catalog["touch_interval_days"]
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
    written = created
    if extras:
        updated = update_workflow(connection, created.id, **extras)
        if updated is not None:
            written = updated
    activated = bool(entry.get("goal") and entry.get("instructions"))
    if activated:
        written = activate_workflow(connection, created.id)
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
    return _import_applied_preview(name, "created", entry, written, preview_changed)


def _import_workflow_update(
    connection: Any, current: Any, entry: dict[str, Any]
) -> dict[str, object]:
    from mailpilot.database import catalog_def_fields, update_workflow
    from mailpilot.operator_log import operator_event

    catalog = catalog_def_fields(entry)
    diff: dict[str, object] = {}
    for field in _WORKFLOW_IMPORT_UPDATABLE:
        catalog_value = catalog[field]
        if getattr(current, field) != catalog_value:
            diff[field] = catalog_value
    if not diff:
        operator_event(
            "workflow.import",
            entity_id=current.id,
            account_id=current.account_id,
            name=current.name,
            changed=[],
        )
        return _import_applied_preview(current.name, "unchanged", entry, current, {})
    written = update_workflow(connection, current.id, **diff) or current
    operator_event(
        "workflow.import",
        entity_id=current.id,
        account_id=current.account_id,
        name=current.name,
        changed=list(diff.keys()),
    )
    return _import_applied_preview(current.name, "updated", entry, written, diff)


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
    (live-row wording-hash match vs the catalog, same hash as check),
    ``catalog_hash`` / ``row_hash``, and ``changed`` (mutated def-field
    excerpts when in sync; remaining differing keys when not). The terminal
    envelope aggregates: top-level ``applied`` and ``rejected`` counts on
    every import envelope; zero applied rows -> an ``import_failed`` error
    envelope on stderr (per-row rows inlined) and exit 1, so scripts gating
    on the exit code never mistake a no-op import for success.
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
