"""Schema init, migrate, provision, and verdict."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, cast

import logfire
import psycopg
from psycopg.rows import dict_row

from mailpilot.database._common import (
    _ENSURE_MIGRATIONS_LEDGER_SQL,
    _MAILPILOT_VERSION,
    _MIGRATION_FILENAME_RE,
    SCHEMA_PATH,
)
from mailpilot.models import (
    SchemaMetadata,
    SchemaStatus,
    SchemaVerdict,
)
from mailpilot.operator_log import operator_event


def _compute_schema_hash(sql: str) -> str:
    """Hash schema.sql modulo comments and whitespace (§V.19).

    Strips `--` line comments, collapses whitespace runs to single spaces,
    then takes a sha256. Reformatting (added comments, blank-line shuffles)
    leaves the hash stable; column / table changes flip it.
    """
    normalized = re.sub(r"--[^\n]*", "", sql)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _discover_migrations() -> list[tuple[int, str, Path]]:
    """Return ``(version, name, path)`` for each forward migration, version-sorted.

    Scans ``migrations/`` (package root, wheel-shipped) for files matching
    ``NNN_snake_description.sql`` per §V.108. Non-matching files are skipped so
    a README or scratch file never derails discovery. A duplicate version
    prefix is a packaging error and raises ``ValueError``.

    Returns:
        Migrations sorted ascending by integer version.

    Raises:
        ValueError: Two migration files share the same version prefix.
    """
    from mailpilot import database as db

    migrations: list[tuple[int, str, Path]] = []
    migrations_path = db.MIGRATIONS_PATH
    if not migrations_path.is_dir():
        return migrations
    for path in migrations_path.glob("*.sql"):
        match = _MIGRATION_FILENAME_RE.match(path.name)
        if match is None:
            continue
        migrations.append((int(match.group(1)), match.group(2), path))
    migrations.sort(key=lambda item: item[0])
    versions = [version for version, _name, _path in migrations]
    if len(versions) != len(set(versions)):
        raise ValueError(f"duplicate migration version prefix in {migrations_path}")
    return migrations


def migrate_database(
    connection: psycopg.Connection[dict[str, Any]],
) -> list[dict[str, object]]:
    """Apply pending forward migrations in version order, each in its own txn.

    Ensures the ``schema_migrations`` ledger exists (so a DB predating the
    migration system catches up), reads the applied versions, then applies
    every ``migrations/NNN_*.sql`` whose version is absent from the ledger, in
    ascending order. Each migration's DDL and its ledger ``INSERT`` commit
    together — one transaction per migration (§V.108) — so a mid-run failure
    leaves earlier migrations applied-and-recorded and the failing one rolled
    back.

    As a final step it re-stamps ``schema_metadata.schema_hash`` (and
    ``mailpilot_version``) to the canonical ``schema.sql`` hash so a
    migrated-forward DB resolves verdict ``current`` not phantom ``drift``
    (§V.108/§V.109, §B.97) — re-baselining even when nothing was pending but the
    recorded hash is stale. Idempotent: a re-run with the hash already current
    applies and re-stamps nothing.

    The connection is expected in manual-commit mode (``autocommit=False``),
    matching ``initialize_database``; commit-per-migration is what makes each
    migration its own transaction.

    Args:
        connection: Open database connection (manual-commit mode).

    Returns:
        Applied-migration records ``[{"version": int, "name": str}, ...]`` in
        apply order; empty when nothing was pending.

    Raises:
        Exception: Re-raised after rollback if a migration's DDL fails.
    """
    connection.execute(_ENSURE_MIGRATIONS_LEDGER_SQL)  # type: ignore[arg-type]
    connection.commit()

    applied_versions = {
        row["version"]
        for row in connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
    }
    # Close the read-only transaction so each migration below opens its own.
    connection.rollback()

    pending = [
        (version, name, path)
        for version, name, path in _discover_migrations()
        if version not in applied_versions
    ]

    applied: list[dict[str, object]] = []
    for version, name, path in pending:
        try:
            connection.execute(path.read_text())  # type: ignore[arg-type]
            connection.execute(
                "INSERT INTO schema_migrations (version, name, mailpilot_version) "
                "VALUES (%s, %s, %s)",
                (version, name, _MAILPILOT_VERSION),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            logfire.exception("schema migration failed", version=version, name=name)
            operator_event(
                "error",
                source="database.migrate",
                message=f"migration {version} failed",
            )
            raise
        operator_event("schema.migrate", version=version, name=name)
        applied.append({"version": version, "name": name})

    # Re-stamp the recorded hash to the canonical `schema.sql` hash so a
    # migrated-forward DB resolves verdict `current`, not phantom `drift`
    # (§V.108/§V.109, §B.97). Runs whether or not a migration applied this
    # call: it also re-baselines the 0-pending case -- every shipped migration
    # already applied but the recorded hash frozen at an older value. No-op
    # when `schema_metadata` is absent (isolated migrate-from-zero) or already
    # current.
    current_hash = _compute_schema_hash(SCHEMA_PATH.read_text())
    recorded = _read_schema_metadata(connection)
    if recorded is not None and recorded.schema_hash != current_hash:
        connection.execute(
            "UPDATE schema_metadata SET schema_hash = %s, mailpilot_version = %s, "
            "applied_at = CURRENT_TIMESTAMP WHERE id = 1",
            (current_hash, _MAILPILOT_VERSION),
        )
        connection.commit()
    else:
        connection.rollback()
    return applied


def _read_schema_metadata(
    connection: psycopg.Connection[dict[str, Any]],
) -> SchemaMetadata | None:
    """Return the singleton `schema_metadata` row, or None if missing.

    Row-missing and table-missing both collapse to None per §V.18 — both
    are "drift" branches from the caller's view.
    """
    try:
        row = connection.execute(
            "SELECT mailpilot_version, schema_hash, applied_at "
            "FROM schema_metadata WHERE id = 1"
        ).fetchone()
    except psycopg.errors.UndefinedTable:
        # Even under autocommit=True, psycopg3 surfaces a failed-transaction
        # state on the following query unless the rolled-back state is cleared.
        connection.rollback()
        return None
    if row is None:
        return None
    return SchemaMetadata.model_validate(row)


def _read_applied_migration_versions(
    connection: psycopg.Connection[dict[str, Any]],
) -> set[int]:
    """Return the set of migration versions recorded in `schema_migrations`.

    A missing ledger table (a DB predating the migration system, §V.108)
    yields the empty set; the failed transaction is rolled back so the next
    query on the connection runs clean (cf. ``_read_schema_metadata``).
    """
    try:
        rows = connection.execute("SELECT version FROM schema_migrations").fetchall()
    except psycopg.errors.UndefinedTable:
        connection.rollback()
        return set()
    return {row["version"] for row in rows}


def determine_schema_verdict(
    connection: psycopg.Connection[dict[str, Any]],
) -> SchemaStatus:
    """Classify the live schema as `current`, `pending`, or `drift` (§V.109).

    Breaks the metadata-row-missing vs table-missing → None collapse by
    consulting the migration ledger alongside the recorded hash:

    - ``drift`` when ``schema_metadata`` is absent (broken provisioning).
    - ``current`` when the recorded hash equals the canonical ``schema.sql``
      hash — the structure matches the latest declaration, so any ledger gap
      is an un-recorded baseline, not a real pending change.
    - ``pending`` when the hash diverges *and* shipped migrations are absent
      from the ledger — a forward path exists (run ``db migrate``).
    - ``drift`` when the hash diverges with no migration to explain it
      (manual edit | DB ahead of code).

    The verdict is purely diagnostic here; tiered enforcement (tolerate for
    read-only diagnosis, dead-stop for ``run`` + mutations) lives in the
    callers per §V.18.
    """
    current_hash = _compute_schema_hash(SCHEMA_PATH.read_text())
    recorded = _read_schema_metadata(connection)
    applied_versions = _read_applied_migration_versions(connection)
    pending_versions = [
        version
        for version, _name, _path in _discover_migrations()
        if version not in applied_versions
    ]

    if recorded is None:
        verdict: SchemaVerdict = "drift"
    elif recorded.schema_hash == current_hash:
        verdict = "current"
    elif pending_versions:
        verdict = "pending"
    else:
        verdict = "drift"

    return SchemaStatus(
        verdict=verdict,
        recorded_hash=recorded.schema_hash if recorded else None,
        current_hash=current_hash,
        applied=len(applied_versions),
        pending=len(pending_versions),
    )


def _connect_failure_hint(message: str, db_name: str) -> str:
    """Map an OperationalError message to an ordered operator hint (§V.137).

    Match order is load-bearing: role-missing must win over the generic
    ``does not exist`` path so a missing role never suggests ``createdb``.

    Args:
        message: ``str(OperationalError)`` text from libpq / psycopg.
        db_name: Database name segment from the connection URL (for createdb).

    Returns:
        One-line operator hint for the SystemExit message.
    """
    if 'role "' in message and "does not exist" in message:
        hint = (
            "set database_url to a URL with an existing role "
            "(e.g. postgresql://mailpilot@localhost/mailpilot), "
            "or run as that OS user / createuser"
        )
    elif 'database "' in message and "does not exist" in message:
        hint = f"run 'createdb {db_name}' to create it"
    elif "no pg_hba.conf entry" in message:
        hint = "allow this client host in pg_hba.conf (or connect from an allowed host)"
    elif (
        "failed to resolve host" in message
        or "nodename nor servname" in message
        or "Name or service not known" in message
    ):
        hint = "hostname did not resolve; check host, VPN, or /etc/hosts"
    elif (
        "password authentication failed" in message
        or "Peer authentication failed" in message
    ):
        hint = "check credentials / auth method in database_url and pg_hba.conf"
    elif "Connection refused" in message:
        hint = "is PostgreSQL running? check your system's service manager"
    else:
        hint = "check your database_url setting"
    return hint


def _connect_database(database_url: str) -> psycopg.Connection[dict[str, Any]]:
    """Open an autocommit PostgreSQL connection or dead-stop with a hint.

    A connect failure is mapped to a ``SystemExit`` carrying an operator hint
    (§V.137). Expected failures log via ``logfire.error`` (not
    ``logfire.exception``) so the operator console has no Traceback, paired
    with ``operator_event("error")`` so the console is never silent.

    Args:
        database_url: PostgreSQL connection URL.

    Returns:
        Open autocommit connection (``dict_row`` factory).
    """
    from mailpilot.database import logfire as _logfire
    from mailpilot.database import psycopg as _psycopg

    db_name = database_url.rsplit("/", 1)[-1]
    try:
        return cast(
            psycopg.Connection[dict[str, Any]],
            _psycopg.connect(database_url, row_factory=dict_row, autocommit=True),  # type: ignore[arg-type]
        )
    except _psycopg.OperationalError as exc:
        message = str(exc)
        hint = _connect_failure_hint(message, db_name)
        _logfire.error("database connection failed", database=db_name, hint=hint)
        operator_event("error", source="database.connect", message=str(exc))
        raise SystemExit(f"database connection failed: {hint}") from None


def _provision_schema(
    connection: psycopg.Connection[dict[str, Any]],
    schema_sql: str,
    current_hash: str,
) -> None:
    """Apply ``schema.sql`` on an empty database and stamp the ledgers (§V.110).

    Runs the canonical full-schema build, records the singleton
    ``schema_metadata`` row, then baseline-stamps the migration ledger
    (§V.108/§V.109): a fresh build already embeds every shipped migration's
    structure, so each is recorded as applied — keeping ``db migrate`` a no-op
    and the verdict ``current`` (zero pending) on a fresh DB. The caller owns
    the autocommit connection and the empty-DB precondition.

    Args:
        connection: Open autocommit connection on an empty database.
        schema_sql: Canonical ``schema.sql`` body.
        current_hash: Normalized hash of ``schema_sql`` (§V.19).
    """
    connection.execute(schema_sql)  # type: ignore[arg-type]
    connection.execute(
        "INSERT INTO schema_metadata (mailpilot_version, schema_hash) VALUES (%s, %s)",
        (_MAILPILOT_VERSION, current_hash),
    )
    for migration_version, migration_name, _path in _discover_migrations():
        connection.execute(
            "INSERT INTO schema_migrations (version, name, mailpilot_version) "
            "VALUES (%s, %s, %s)",
            (migration_version, migration_name, _MAILPILOT_VERSION),
        )


def provision_database(database_url: str) -> dict[str, object]:
    """Provision an empty database or report an existing one (``db init``, §V.110).

    Connects, probes for the ``account`` table, and provisions from
    ``schema.sql`` only when it is absent — the data-loss-free path with no
    ``--force`` to wipe a populated DB. A populated database is never mutated as
    a structural side-effect: ``provisioned`` reads False and the caller
    (``db init``) no-ops on a ``current`` verdict or refuses otherwise.

    Args:
        database_url: PostgreSQL connection URL.

    Returns:
        Report dict ``{provisioned, verdict, recorded_hash, current_hash,
        applied, pending}`` — ``provisioned`` says whether structure was just
        created; the remaining fields mirror ``db check``.
    """
    connection = _connect_database(database_url)
    try:
        schema_sql = SCHEMA_PATH.read_text()
        current_hash = _compute_schema_hash(schema_sql)
        probe = connection.execute("SELECT to_regclass('account') AS oid").fetchone()
        provisioned = probe is None or probe.get("oid") is None
        if provisioned:
            _provision_schema(connection, schema_sql, current_hash)
        from mailpilot import database as db

        status = db.determine_schema_verdict(connection)
        return {
            "provisioned": provisioned,
            "verdict": status.verdict,
            "recorded_hash": status.recorded_hash,
            "current_hash": status.current_hash,
            "applied": status.applied,
            "pending": status.pending,
        }
    finally:
        connection.close()


def initialize_database(
    database_url: str, *, require_current_schema: bool = False
) -> psycopg.Connection[dict[str, Any]]:
    """Open a PostgreSQL connection, provisioning or verifying the schema.

    An empty database is provisioned from ``schema.sql`` (the data-loss-free
    auto-provision path) and the migration ledger is baseline-stamped so a
    fresh build reads ``current`` with zero pending migrations. A populated
    database is only verified: when ``require_current_schema`` is set (the
    ``run`` + mutation write-path per §V.109) a non-``current`` verdict
    dead-stops with the matching error envelope and exit 1; otherwise drift is
    tolerated and reported (read-only diagnosis path).

    Args:
        database_url: PostgreSQL connection URL.
        require_current_schema: When True, refuse to return a connection unless
            the schema verdict is ``current`` (write-path gate, §V.109/§V.18).

    Returns:
        Open database connection with schema provisioned or verified.
    """
    connection = _connect_database(database_url)
    # Skip the schema apply when the database is already initialized.
    # schema.sql contains DROP TRIGGER + CREATE TRIGGER on the task table
    # which takes AccessExclusiveLock and deadlocks against the sync loop's
    # INSERT INTO task (RowExclusiveLock). New columns/tables added to the
    # schema still flow through the canonical `make clean` workflow, which
    # drops everything and re-applies on an empty database.
    schema_sql = SCHEMA_PATH.read_text()
    current_hash = _compute_schema_hash(schema_sql)
    probe = connection.execute("SELECT to_regclass('account') AS oid").fetchone()  # type: ignore[union-attr]
    if probe is None or probe.get("oid") is None:
        _provision_schema(connection, schema_sql, current_hash)
    elif require_current_schema:
        # Write-path gate (§V.109): dead-stop before any write lands on a
        # mismatched schema. Distinct codes since the remedy differs — `drift`
        # = investigate divergence, `pending` = run `db migrate`.
        from mailpilot.database import (
            determine_schema_verdict as _determine_schema_verdict,
        )
        from mailpilot.database import logfire as _logfire

        status = _determine_schema_verdict(connection)
        if status.verdict != "current":
            _logfire.warn(
                "schema not current; refusing write-path command",
                verdict=status.verdict,
                recorded_hash=status.recorded_hash,
                current_hash=status.current_hash,
                pending=status.pending,
            )
            operator_event(
                "error",
                source="database.schema_gate",
                verdict=status.verdict,
                recorded_hash=status.recorded_hash,
                current_hash=status.current_hash,
            )
            connection.close()
            from mailpilot.cli import output_error

            if status.verdict == "drift":
                output_error(
                    "schema drift detected "
                    f"(recorded_hash={status.recorded_hash} "
                    f"current_hash={status.current_hash}); "
                    "investigate divergence -- no migration path",
                    "schema_drift",
                )
            output_error(
                f"{status.pending} schema migration(s) pending; "
                "run 'mailpilot db migrate'",
                "schema_migration_pending",
            )
    else:
        recorded = _read_schema_metadata(connection)
        if recorded is None or recorded.schema_hash != current_hash:
            recorded_version = recorded.mailpilot_version if recorded else None
            recorded_hash = recorded.schema_hash if recorded else None
            logfire.warn(
                "schema drift detected",
                recorded_version=recorded_version,
                current_version=_MAILPILOT_VERSION,
                recorded_hash=recorded_hash,
                current_hash=current_hash,
            )
            operator_event(
                "schema.drift",
                recorded_version=recorded_version,
                current_version=_MAILPILOT_VERSION,
                recorded_hash=recorded_hash,
                current_hash=current_hash,
            )
    connection.autocommit = False
    return connection
