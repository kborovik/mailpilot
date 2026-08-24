"""Provision and migrate the database schema."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json

import click

from mailpilot.cli.main import (
    _database_url,
    _db,
    main,
    output,
    output_error,
)

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
