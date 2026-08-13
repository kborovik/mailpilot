"""PostgreSQL database for CRM persistence.

Single flat module with section headers per entity. All CRUD functions follow
consistent signatures and return domain models from ``models.py``.

Convention:
    create_X(connection, ...) -> X
    get_X(connection, id) -> X | None
    list_X(connection, ...) -> list[X]
    update_X(connection, id, ...) -> X
"""

import hashlib
import json
import re
import urllib.parse
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any, cast

import logfire
import psycopg
from psycopg.rows import dict_row
from psycopg.sql import SQL, Composable, Composed, Identifier, Placeholder
from psycopg.types.json import Json

from mailpilot.models import (
    Account,
    AccountSummary,
    Activity,
    ActivitySummary,
    Company,
    CompanyProfile,
    CompanySummary,
    CompanyView,
    Contact,
    ContactSummary,
    ContactView,
    Email,
    EmailSummary,
    Enrollment,
    EnrollmentPreview,
    EnrollmentPreviewContact,
    EnrollmentPreviewExcluded,
    EnrollmentSummary,
    EnrollmentWithOutcome,
    Meeting,
    MeetingAttendee,
    MeetingSummary,
    MeetingView,
    Note,
    NoteSummary,
    QueueReport,
    QueueTaskRow,
    QueueWorkflowRow,
    SchemaMetadata,
    SchemaStatus,
    SchemaVerdict,
    SyncStatus,
    Tag,
    TagAssignment,
    TagSummary,
    Task,
    TaskStats,
    TaskSummary,
    TouchStageCounts,
    Workflow,
    WorkflowCheck,
    WorkflowCheckEntry,
    WorkflowReport,
    WorkflowReportMeta,
    WorkflowStats,
    WorkflowStatusHealth,
    WorkflowSummary,
)
from mailpilot.operator_log import operator_event
from mailpilot.settings import SECRET_KEYS, Settings

# Distribution name is mailpilot-crm; the import package stays mailpilot.
_MAILPILOT_VERSION = _pkg_version("mailpilot-crm")


def _sql_parse_touch(context_col: SQL) -> Composed:
    """SQL that parses ``context.touch`` (2 / digit string / T<n>) to int; else NULL.

    ``context_col`` is a caller-owned fragment such as ``t.context`` or
    ``nt.context`` -- never user input. Replaces raw ``::int`` casts that
    raise ``InvalidTextRepresentation`` on ``T2`` (§V.162 / §B.132).
    """
    return SQL(
        "CASE "
        "WHEN ({col}->>'touch') ~ '^[0-9]+$' THEN ({col}->>'touch')::int "
        "WHEN ({col}->>'touch') ~ '^T[0-9]+$' "
        "THEN substring({col}->>'touch' from 2)::int "
        "ELSE NULL END"
    ).format(col=context_col)


_INLINE_NOTES_CAP = 10

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

MIGRATIONS_PATH = Path(__file__).parent / "migrations"

# Filename grammar for forward-only migrations: NNN_snake_description.sql with a
# monotonic integer prefix (§V.108). Files that do not match (e.g. README, a
# stray .sql scratch file) are ignored by discovery.
_MIGRATION_FILENAME_RE = re.compile(r"^(\d+)_([a-z0-9_]+)\.sql$")

# The migration ledger is the machinery's own bookkeeping table. It is also
# declared in schema.sql for fresh-DB builds; both definitions MUST match (the
# init==migrations identity test enforces it, §V.108).
_ENSURE_MIGRATIONS_LEDGER_SQL = """\
CREATE TABLE IF NOT EXISTS schema_migrations (
    version            INTEGER PRIMARY KEY,
    name               TEXT NOT NULL,
    applied_at         TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    mailpilot_version  TEXT NOT NULL
);"""


def _new_id() -> str:
    """Generate a UUIDv7 string for use as a primary key."""
    return str(uuid.uuid7())


def _build_update(
    table: str,
    updates: dict[str, object],
    where: Composed | SQL,
) -> Composed:
    """Build a dynamic UPDATE ... SET ... WHERE ... RETURNING * query.

    Args:
        table: Table name.
        updates: Column-name to value mapping for SET clause.
        where: WHERE clause (psycopg.sql fragment).

    Returns:
        Composed SQL query ready for execute().
    """
    set_parts = [SQL("{} = {}").format(Identifier(k), Placeholder(k)) for k in updates]
    set_clause = SQL(", ").join([*set_parts, SQL("updated_at = CURRENT_TIMESTAMP")])
    return SQL("UPDATE {} SET {} WHERE {} RETURNING *").format(
        Identifier(table), set_clause, where
    )


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
    migrations: list[tuple[int, str, Path]] = []
    if not MIGRATIONS_PATH.is_dir():
        return migrations
    for path in MIGRATIONS_PATH.glob("*.sql"):
        match = _MIGRATION_FILENAME_RE.match(path.name)
        if match is None:
            continue
        migrations.append((int(match.group(1)), match.group(2), path))
    migrations.sort(key=lambda item: item[0])
    versions = [version for version, _name, _path in migrations]
    if len(versions) != len(set(versions)):
        raise ValueError(f"duplicate migration version prefix in {MIGRATIONS_PATH}")
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
    db_name = database_url.rsplit("/", 1)[-1]
    try:
        return cast(
            psycopg.Connection[dict[str, Any]],
            psycopg.connect(database_url, row_factory=dict_row, autocommit=True),  # type: ignore[arg-type]
        )
    except psycopg.OperationalError as exc:
        message = str(exc)
        hint = _connect_failure_hint(message, db_name)
        logfire.error("database connection failed", database=db_name, hint=hint)
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
        status = determine_schema_verdict(connection)
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
        status = determine_schema_verdict(connection)
        if status.verdict != "current":
            logfire.warn(
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


# -- Status --------------------------------------------------------------------


def _scrub_database_url(url: str) -> str:
    """Reduce a PostgreSQL URL to ``scheme://host[:port]/db``, dropping creds.

    Uses ``urllib.parse.urlsplit`` so a passwordless URL round-trips unchanged
    and a credentialed URL loses its userinfo netloc segment. Path is preserved
    verbatim (the leading ``/`` is part of urlsplit's ``path``).
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port is not None else host
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _schema_block(
    connection: psycopg.Connection[dict[str, Any]],
) -> dict[str, object]:
    """Return the `schema` block per §V.11: three-state verdict + facts.

    Carries ``verdict`` in {current, pending, drift} (not a bare drift bool,
    §V.109), the recorded vs canonical schema hashes, and applied/pending
    migration counts. Read-only diagnosis: reports whatever the verdict is,
    never dead-stops (the gate lives on the write-path per §V.18).
    """
    status = determine_schema_verdict(connection)
    return {
        "verdict": status.verdict,
        "recorded_hash": status.recorded_hash,
        "current_hash": status.current_hash,
        "applied": status.applied,
        "pending": status.pending,
    }


def _sync_loop_block(
    connection: psycopg.Connection[dict[str, Any]],
) -> dict[str, object] | None:
    """Return the `sync_loop` block per §V.11, or None when not running."""
    row = connection.execute(
        """\
        SELECT
            pid,
            started_at,
            heartbeat_at,
            EXTRACT(EPOCH FROM (now() - heartbeat_at))::int AS heartbeat_age_seconds
        FROM sync_status WHERE id = 'singleton'
        """
    ).fetchone()
    if row is None:
        return None
    return {
        "running": True,
        "pid": row["pid"],  # type: ignore[index]
        "started_at": row["started_at"].isoformat(),  # type: ignore[index]
        "heartbeat_at": row["heartbeat_at"].isoformat(),  # type: ignore[index]
        "heartbeat_age_seconds": row["heartbeat_age_seconds"],  # type: ignore[index]
    }


def _accounts_block(
    connection: psycopg.Connection[dict[str, Any]],
) -> list[dict[str, object]]:
    """Return per-account status rows per §V.11.

    Server-computed ages (``last_synced_age_seconds``, ``watch_expires_in_hours``)
    avoid Python clock skew; null inputs → null ages.
    """
    rows = connection.execute(
        """\
        SELECT
            id,
            email,
            (disabled_reason IS NOT NULL) AS disabled,
            gmail_history_id,
            watch_expiration,
            last_synced_at,
            EXTRACT(EPOCH FROM (now() - last_synced_at))::int
                AS last_synced_age_seconds,
            (EXTRACT(EPOCH FROM (watch_expiration - now()))::int / 3600)
                AS watch_expires_in_hours
        FROM account
        ORDER BY email
        """
    ).fetchall()
    accounts: list[dict[str, object]] = []
    for row in rows:
        watch_expiration = row["watch_expiration"]
        last_synced_at = row["last_synced_at"]
        accounts.append(
            {
                "id": row["id"],
                "email": row["email"],
                "disabled": row["disabled"],
                "last_synced_at": (
                    last_synced_at.isoformat() if last_synced_at is not None else None
                ),
                "last_synced_age_seconds": row["last_synced_age_seconds"],
                "gmail_history_id": row["gmail_history_id"],
                "watch_expiration": (
                    watch_expiration.isoformat()
                    if watch_expiration is not None
                    else None
                ),
                "watch_expires_in_hours": row["watch_expires_in_hours"],
            }
        )
    return accounts


def _tasks_block(
    connection: psycopg.Connection[dict[str, Any]],
) -> dict[str, object]:
    """Return task-queue aggregates per §V.11.

    Single SQL statement: pending counts split by due-vs-future, oldest
    pending age (due-only), max attempt_count among pending, and failed_24h.
    """
    row = connection.execute(
        """\
        SELECT
            (SELECT count(*) FROM task WHERE status = 'pending') AS pending,
            (SELECT count(*) FROM task
             WHERE status = 'pending' AND scheduled_at > now())
                AS scheduled_future,
            (SELECT EXTRACT(EPOCH FROM (now() - min(scheduled_at)))::int
             FROM task
             WHERE status = 'pending' AND scheduled_at <= now())
                AS oldest_pending_age_seconds,
            (SELECT max(attempt_count) FROM task WHERE status = 'pending')
                AS max_attempt_count_pending,
            (SELECT count(*) FROM task
             WHERE status = 'failed'
               AND completed_at >= now() - interval '24 hours')
                AS failed_24h
        """
    ).fetchone()
    return {
        "pending": row["pending"],  # type: ignore[index]
        "failed_24h": row["failed_24h"],  # type: ignore[index]
        "scheduled_future": row["scheduled_future"],  # type: ignore[index]
        "oldest_pending_age_seconds": row["oldest_pending_age_seconds"],  # type: ignore[index]
        "max_attempt_count_pending": row["max_attempt_count_pending"],  # type: ignore[index]
    }


def _counts_block(
    connection: psycopg.Connection[dict[str, Any]],
) -> dict[str, object]:
    """Return entity counts (sanity tail per §V.11). ``accounts`` excluded."""
    row = connection.execute(
        """\
        SELECT
            (SELECT COUNT(*) FROM company) AS companies,
            (SELECT COUNT(*) FROM contact) AS contacts,
            (SELECT COUNT(*) FROM workflow) AS workflows,
            (SELECT COUNT(*) FROM email) AS emails,
            (SELECT COUNT(*) FROM activity) AS activities,
            (SELECT COUNT(*) FROM tag) AS tags,
            (SELECT COUNT(*) FROM note) AS notes
        """
    ).fetchone()
    return {
        "companies": row["companies"],  # type: ignore[index]
        "contacts": row["contacts"],  # type: ignore[index]
        "workflows": row["workflows"],  # type: ignore[index]
        "emails": row["emails"],  # type: ignore[index]
        "activities": row["activities"],  # type: ignore[index]
        "tags": row["tags"],  # type: ignore[index]
        "notes": row["notes"],  # type: ignore[index]
    }


def _config_block(settings: Settings) -> dict[str, object]:
    """Return the `config` block per §V.11.

    Secret keys collapsed to ``*_set: bool`` so values never reach agent
    transcripts or Logfire spans. ``database_url`` is scrubbed of userinfo
    rather than dropped entirely so operators can still see host/db/port.
    """
    assert "anthropic_api_key" in SECRET_KEYS
    assert "xai_api_key" in SECRET_KEYS
    assert "logfire_token" in SECRET_KEYS
    assert "database_url" in SECRET_KEYS
    return {
        "llm_provider": settings.llm_provider,
        "anthropic_api_key_set": bool(settings.anthropic_api_key),
        "anthropic_model": settings.anthropic_model,
        "xai_api_key_set": bool(settings.xai_api_key),
        "xai_model": settings.xai_model,
        "logfire_environment": settings.logfire_environment,
        "logfire_token_set": bool(settings.logfire_token),
        "google_pubsub_topic": settings.google_pubsub_topic,
        "google_pubsub_subscription": settings.google_pubsub_subscription,
        "database_url": _scrub_database_url(str(settings.database_url)),
    }


def get_status_payload(
    connection: psycopg.Connection[dict[str, Any]],
    settings: Settings,
) -> dict[str, object]:
    """Build the full ``mailpilot status`` payload per §V.11.

    Top-level blocks (``version``, ``schema``, ``sync_loop``, ``accounts``,
    ``tasks``, ``config``, ``counts``) are layout-stable for LLM-agent
    troubleshooting; secrets are collapsed to booleans and the database URL
    is scrubbed (§V.11).

    Args:
        connection: Open database connection.
        settings: Loaded settings (callers pass ``get_settings()``).

    Returns:
        Dict matching the §V.11 envelope, ready to wrap as
        ``{"status": <payload>, "ok": true}``.
    """
    with logfire.span("db.status.payload"):
        return {
            "version": _MAILPILOT_VERSION,
            "schema": _schema_block(connection),
            "sync_loop": _sync_loop_block(connection),
            "accounts": _accounts_block(connection),
            "tasks": _tasks_block(connection),
            "config": _config_block(settings),
            "counts": _counts_block(connection),
        }


# -- Account -------------------------------------------------------------------


def _empty_to_none(value: str | None) -> str | None:
    """Map empty strings to NULL for optional TEXT columns."""
    if value is None or value == "":
        return None
    return value


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


# -- Company -------------------------------------------------------------------


def _normalize_company_domain(domain: str) -> str:
    """Lowercase + strip a company domain natural key (§V.90 / §V.142)."""
    return domain.strip().lower()


def _merged_into_reason(into_domain: str) -> str:
    """Structured soft-disable reason written by ``merge_companies`` (§V.143)."""
    return f"merged:into {_normalize_company_domain(into_domain)}"


def _tombstone_merged_domain(company_id: str) -> str:
    """Unique domain left on an absorbed company after merge (§V.142 space)."""
    return f"__merged__.{company_id}"


def domain_in_use(
    connection: psycopg.Connection[dict[str, Any]],
    domain: str,
) -> bool:
    """True when *domain* is a canonical company.domain or an alias (§V.142)."""
    normalized = _normalize_company_domain(domain)
    if not normalized:
        return False
    row = connection.execute(
        """\
        SELECT EXISTS (
            SELECT 1 FROM company WHERE domain = %(domain)s
            UNION ALL
            SELECT 1 FROM company_alias WHERE domain = %(domain)s
        ) AS taken
        """,
        {"domain": normalized},
    ).fetchone()
    return bool(row and row["taken"])


def list_company_aliases(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
) -> list[str]:
    """Return sorted lowercased alias domains for a company (§V.142)."""
    rows = connection.execute(
        """\
        SELECT domain FROM company_alias
        WHERE company_id = %(company_id)s
        ORDER BY domain
        """,
        {"company_id": company_id},
    ).fetchall()
    return [str(r["domain"]) for r in rows]


def add_company_alias(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
    domain: str,
    *,
    commit: bool = True,
) -> bool:
    """Register one alias domain for a company (§V.142).

    Returns ``True`` when a row was inserted, ``False`` when the alias already
    pointed at this company (idempotent skip). Raises ``ValueError`` when the
    domain collides with another company.domain or another owner's alias.
    """
    normalized = _normalize_company_domain(domain)
    if not normalized:
        raise ValueError("alias domain cannot be empty")
    company = get_company(connection, company_id)
    if company is None:
        raise ValueError(f"company not found: {company_id}")
    if normalized == company.domain:
        raise ValueError(f"alias {normalized!r} equals company domain")
    existing = connection.execute(
        "SELECT company_id FROM company_alias WHERE domain = %(domain)s",
        {"domain": normalized},
    ).fetchone()
    if existing is not None:
        if existing["company_id"] == company_id:
            return False
        raise ValueError(
            f"domain {normalized!r} is already an alias of another company"
        )
    if (
        connection.execute(
            "SELECT 1 FROM company WHERE domain = %(domain)s",
            {"domain": normalized},
        ).fetchone()
        is not None
    ):
        raise ValueError(f"domain {normalized!r} is already a company domain")
    connection.execute(
        """\
        INSERT INTO company_alias (domain, company_id)
        VALUES (%(domain)s, %(company_id)s)
        """,
        {"domain": normalized, "company_id": company_id},
    )
    if commit:
        connection.commit()
    return True


def create_company(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    domain: str,
    *,
    aliases: Sequence[str] | None = None,
) -> Company | None:
    """Create a new company, optionally with alias domains (§V.142).

    Uses ``ON CONFLICT (domain) DO NOTHING`` per §V.16(+) so callers can
    safely re-invoke without catching ``UniqueViolation``. Returns ``None``
    when the canonical domain already exists as a company row or is already
    an alias (shared domain space). Alias domains are lowercased and
    registered in the same transaction.

    Args:
        connection: Open database connection.
        name: Company name.
        domain: Primary domain.
        aliases: Optional alternate domains (repeatable CLI ``--alias``).

    Returns:
        Created company, or ``None`` if the domain space was already taken.
    """
    normalized = _normalize_company_domain(domain)
    if not normalized:
        return None
    alias_list = sorted(
        {
            _normalize_company_domain(a)
            for a in (aliases or ())
            if _normalize_company_domain(a)
        }
    )
    if normalized in alias_list:
        return None
    if domain_in_use(connection, normalized):
        return None
    for alias in alias_list:
        if domain_in_use(connection, alias):
            return None
    company_id = _new_id()
    row = connection.execute(
        """\
        INSERT INTO company (id, name, domain)
        VALUES (%(id)s, %(name)s, %(domain)s)
        ON CONFLICT (domain) DO NOTHING
        RETURNING *
        """,
        {"id": company_id, "name": name, "domain": normalized},
    ).fetchone()
    if row is None:
        connection.commit()
        return None
    for alias in alias_list:
        connection.execute(
            """\
            INSERT INTO company_alias (domain, company_id)
            VALUES (%(domain)s, %(company_id)s)
            """,
            {"domain": alias, "company_id": company_id},
        )
    connection.commit()
    return Company.model_validate(row)


def get_company(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
) -> Company | None:
    """Get a company by ID.

    Args:
        connection: Open database connection.
        company_id: Company ID.

    Returns:
        Company if found, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM company WHERE id = %(id)s",
        {"id": company_id},
    ).fetchone()
    if row is None:
        return None
    return Company.model_validate(row)


def _exclude_tags_conditions(
    exclude_tags: Sequence[str] | None,
    owner_column: str,
    params: dict[str, object],
) -> list[Composed]:
    """Build one ``NOT EXISTS`` predicate per excluded tag (§V.116).

    ``--no-tag`` is repeatable, so the discover set can exclude several
    memoization classes at once (``no-contacts-found`` and
    ``contacts-exhausted``, §V.96). Each tag becomes its own intersected
    ``NOT EXISTS`` over ``tag_assignment`` on ``owner_column``
    (``company_id`` or ``contact_id``); the caller appends the predicates and
    this fn mutates ``params`` with a uniquely-named placeholder per tag.

    Args:
        exclude_tags: Resolved tag ids to exclude (empty/None -> no predicate).
        owner_column: ``tag_assignment`` owner FK column to join on.
        params: Query parameter map, mutated in place with one entry per tag.

    Returns:
        One ``NOT EXISTS`` predicate per excluded tag.
    """
    conditions: list[Composed] = []
    if not exclude_tags:
        return conditions
    for index, exclude_tag_id in enumerate(exclude_tags):
        param_name = f"exclude_tag_id_{index}"
        conditions.append(
            SQL(
                "NOT EXISTS (SELECT 1 FROM tag_assignment ta "
                "WHERE ta.{} = c.id AND ta.tag_id = {})"
            ).format(Identifier(owner_column), Placeholder(param_name))
        )
        params[param_name] = exclude_tag_id
    return conditions


_COMPANY_TAGS_SQL = (
    "COALESCE("
    "(SELECT array_agg(t.name ORDER BY t.name) "
    "FROM tag_assignment ta JOIN tag t ON t.id = ta.tag_id "
    "WHERE ta.company_id = c.id), "
    "ARRAY[]::text[]) AS tags"
)
"""Correlated assigned-tag names for company list/search rows (§V.8 / §V.116)."""

_COMPANY_SORT_SQL: dict[str, SQL] = {
    "name": SQL("LOWER(c.name)"),
    "domain": SQL("LOWER(c.domain)"),
    "created_at": SQL("c.created_at"),
    "contact_count": SQL("COUNT(ct.id)"),
}
"""Company list|search ORDER BY expressions keyed by ``--sort`` Choice."""


def _company_order_by(sort: str, desc: bool) -> Composed:
    """Build ``ORDER BY <sort> ASC|DESC, LOWER(c.name) ASC`` for stable pages."""
    col = _COMPANY_SORT_SQL.get(sort, _COMPANY_SORT_SQL["name"])
    direction = SQL("DESC") if desc else SQL("ASC")
    return SQL("ORDER BY {} {}, LOWER(c.name) ASC").format(col, direction)


def _company_pipeline_status_predicates(
    status: str | None,
    include_disabled: bool,
) -> tuple[list[SQL], list[SQL]]:
    """Build WHERE/HAVING predicates for the company pipeline cohort filter.

    Args:
        status: Pipeline cohort name (``ready`` / ``needs_contacts`` /
            ``needs_profile`` / ``disabled``) or ``None``.
        include_disabled: When ``status`` is unset, controls the default
            soft-disable hide (§V.114).

    Returns:
        ``(conditions, having)`` SQL fragments. Active cohort buckets force
        not-disabled; ``disabled`` selects only disabled rows and overrides
        the default hide (§V.138).
    """
    conditions: list[SQL] = []
    having: list[SQL] = []
    if status == "ready":
        conditions.append(SQL("c.profile IS NOT NULL"))
        conditions.append(SQL("c.disabled_reason IS NULL"))
        having.append(SQL("COUNT(ct.id) >= 1"))
    elif status == "needs_contacts":
        conditions.append(SQL("c.profile IS NOT NULL"))
        conditions.append(SQL("c.disabled_reason IS NULL"))
        having.append(SQL("COUNT(ct.id) = 0"))
    elif status == "needs_profile":
        conditions.append(SQL("c.profile IS NULL"))
        conditions.append(SQL("c.disabled_reason IS NULL"))
    elif status == "disabled":
        conditions.append(SQL("c.disabled_reason IS NOT NULL"))
    elif not include_disabled:
        conditions.append(SQL("c.disabled_reason IS NULL"))
    return conditions, having


def list_companies(
    connection: psycopg.Connection[dict[str, Any]],
    limit: int = 100,
    offset: int = 0,
    sort: str = "name",
    desc: bool = False,
    since: str | None = None,
    until: str | None = None,
    has_profile: bool | None = None,
    max_contacts: int | None = None,
    min_contacts: int | None = None,
    include_disabled: bool = False,
    tag: str | None = None,
    exclude_tags: Sequence[str] | None = None,
    full: bool = False,
    status: str | None = None,
) -> list[CompanySummary]:
    """List companies as summaries.

    Joins ``contact`` once (LEFT JOIN) so each summary carries
    ``contact_count`` (child cardinality, **including disabled** rows per
    §V.96) without an N+1 probe; the count tracks the discovery-memoization
    rule, so disabled contacts are counted, not the active-only set.

    Disabled companies (``disabled_reason IS NOT NULL``) are hidden by default
    (§V.114) -- a company memoized as having no discoverable contacts drops
    out of the listing and so out of the lead-contacts discover set (§V.96).
    Pass ``include_disabled=True`` to surface them.

    Every row projects ``tags`` (assigned names, empty ok) and
    ``disabled_reason`` (null when enabled). Pass ``full=True`` to embed
    lean ``profile.summary`` only — never products/target_customers/sources
    on the list path (§V.8).

    Args:
        connection: Open database connection.
        limit: Maximum results.
        offset: Rows to skip before the page (default 0).
        sort: Order key in {name, domain, created_at, contact_count}.
        desc: When ``True``, sort descending; default ascending.
        since: ISO datetime inclusive lower bound on ``created_at``.
        until: ISO datetime inclusive upper bound on ``created_at``.
        has_profile: ``True`` returns only rows where ``profile IS NOT NULL``;
            ``False`` returns only rows where ``profile IS NULL``; ``None``
            (default) returns all rows. Per §V.72 operator filter surface.
        max_contacts: When set, returns only companies whose ``contact_count``
            is ``<= N`` (inclusive upper bound). Mirrors
            ``--max-email-confidence`` (§V.95); ``--has-profile --max-contacts
            4`` expresses the lead-contacts discover set in one query (§V.96).
        min_contacts: When set, returns only companies whose ``contact_count``
            is ``>= N`` (inclusive lower bound); composes with ``max_contacts``
            into a closed range.
        include_disabled: When ``True``, includes disabled companies; the
            default (``False``) hides them (§V.114).
        tag: When set (a resolved tag id), returns only companies carrying that
            tag -- an Enum-family membership filter over ``tag_assignment``
            (§V.116). Composes with ``exclude_tags`` as an intersection.
        exclude_tags: When set (resolved tag ids), returns only companies
            carrying NONE of the given tags -- one ``NOT EXISTS`` predicate per
            tag, all intersected (§V.116). The repeatable negated membership
            filter, for memoization (drop a memoized company from the discover
            set without ``company disable``); the lead-contacts discover set
            excludes both ``no-contacts-found`` and ``contacts-exhausted``
            (§V.96).
        full: When ``True``, embeds ``profile`` as ``{"summary": ...}`` (or
            null when the company has no profile). Default lean list leaves
            ``profile`` null (§V.8).
        status: Pipeline cohort filter (§V.138). One of ``ready`` (profile +
            contact_count >= 1 + not disabled), ``needs_contacts`` (profile +
            contact_count = 0 + not disabled), ``needs_profile`` (no profile +
            not disabled), ``disabled`` (disabled_reason set; overrides the
            default hide). AND-composes with the other filters. ``None``
            (default) applies no cohort predicate.

    Returns:
        List of company summaries ordered by ``sort`` (default name).
    """
    conditions: list[Composed | SQL] = []
    having: list[SQL] = []
    params: dict[str, object] = {"limit": limit, "offset": offset}
    if since is not None:
        conditions.append(SQL("c.created_at >= %(since)s"))
        params["since"] = since
    if until is not None:
        conditions.append(SQL("c.created_at <= %(until)s"))
        params["until"] = until
    if has_profile is True:
        conditions.append(SQL("c.profile IS NOT NULL"))
    elif has_profile is False:
        conditions.append(SQL("c.profile IS NULL"))
    status_conditions, status_having = _company_pipeline_status_predicates(
        status, include_disabled
    )
    conditions.extend(status_conditions)
    having.extend(status_having)
    if max_contacts is not None:
        having.append(SQL("COUNT(ct.id) <= %(max_contacts)s"))
        params["max_contacts"] = max_contacts
    if min_contacts is not None:
        having.append(SQL("COUNT(ct.id) >= %(min_contacts)s"))
        params["min_contacts"] = min_contacts
    if tag is not None:
        conditions.append(
            SQL(
                "EXISTS (SELECT 1 FROM tag_assignment ta "
                "WHERE ta.company_id = c.id AND ta.tag_id = %(tag_id)s)"
            )
        )
        params["tag_id"] = tag
    conditions.extend(_exclude_tags_conditions(exclude_tags, "company_id", params))
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    having_clause = SQL("HAVING ") + SQL(" AND ").join(having) if having else SQL("")
    profile_select = (
        SQL(
            ", CASE WHEN c.profile IS NULL THEN NULL "
            "ELSE jsonb_build_object('summary', c.profile->>'summary') "
            "END AS profile"
        )
        if full
        else SQL("")
    )
    order_by = _company_order_by(sort, desc)
    query = SQL(
        "SELECT c.id, c.name, c.domain, (c.profile IS NOT NULL) AS has_profile, "
        "c.disabled_reason, c.created_at, COUNT(ct.id) AS contact_count, "
        "{tags}{profile} "
        "FROM company c LEFT JOIN contact ct ON ct.company_id = c.id "
        "{where} GROUP BY c.id {having} {order} "
        "LIMIT %(limit)s OFFSET %(offset)s"
    ).format(
        tags=SQL(_COMPANY_TAGS_SQL),
        profile=profile_select,
        where=where,
        having=having_clause,
        order=order_by,
    )
    rows = connection.execute(query, params).fetchall()
    return [CompanySummary.model_validate(row) for row in rows]


def search_companies(
    connection: psycopg.Connection[dict[str, Any]],
    query: str,
    limit: int = 100,
    offset: int = 0,
    sort: str = "name",
    desc: bool = False,
) -> list[CompanySummary]:
    """Search companies by name or domain.

    Args:
        connection: Open database connection.
        query: Search term (matched against name and domain).
        limit: Maximum number of results.
        offset: Rows to skip before the page (default 0).
        sort: Order key in {name, domain, created_at, contact_count}.
        desc: When ``True``, sort descending; default ascending.

    Returns:
        Matching company summaries ordered by ``sort``. Each carries
        ``contact_count`` (LEFT JOIN contact COUNT, incl. disabled per §V.96)
        and ``tags`` (assigned names, empty ok), mirroring ``list_companies``.
    """
    pattern = f"%{query}%"
    order_by = _company_order_by(sort, desc)
    sql = SQL(
        "SELECT c.id, c.name, c.domain, (c.profile IS NOT NULL) AS has_profile, "
        "c.disabled_reason, c.created_at, COUNT(ct.id) AS contact_count, "
        "{tags} "
        "FROM company c "
        "LEFT JOIN contact ct ON ct.company_id = c.id "
        "WHERE LOWER(c.name) LIKE LOWER(%(pattern)s) "
        "OR LOWER(c.domain) LIKE LOWER(%(pattern)s) "
        "OR EXISTS ("
        "  SELECT 1 FROM company_alias a "
        "  WHERE a.company_id = c.id "
        "    AND LOWER(a.domain) LIKE LOWER(%(pattern)s)"
        ") "
        "GROUP BY c.id "
        "{order} "
        "LIMIT %(limit)s OFFSET %(offset)s"
    ).format(tags=SQL(_COMPANY_TAGS_SQL), order=order_by)
    rows = connection.execute(
        sql,
        {"pattern": pattern, "limit": limit, "offset": offset},
    ).fetchall()
    return [CompanySummary.model_validate(row) for row in rows]


def export_companies(
    connection: psycopg.Connection[dict[str, Any]],
    has_profile: bool | None = None,
    max_contacts: int | None = None,
    min_contacts: int | None = None,
    include_disabled: bool = False,
    tag: str | None = None,
    exclude_tags: Sequence[str] | None = None,
    status: str | None = None,
    full: bool = False,
) -> list[dict[str, Any]]:
    """Export companies as tracker NDJSON-ready dicts (§V.145).

    Stable keys: ``domain``, ``name``, ``tags``, ``has_profile``,
    ``contact_count``, ``disabled_reason``. Domains are lowercased; tags are
    sorted; rows ordered by domain ASC. No result-limit (unlike ``list``).
    Filters match the company list family (§V.138/§V.116/§V.114/§V.96).
    Pass ``full=True`` to embed the full ``profile`` object (or null).

    Args:
        connection: Open database connection.
        has_profile: Presence filter; ``None`` means no filter.
        max_contacts: Inclusive upper bound on contact_count.
        min_contacts: Inclusive lower bound on contact_count.
        include_disabled: When ``True``, includes disabled companies.
        tag: Resolved tag id membership filter.
        exclude_tags: Resolved tag ids excluded via NOT EXISTS.
        status: Pipeline cohort filter (§V.138).
        full: When ``True``, embed full profile JSON (or null).

    Returns:
        List of tracker-shaped dicts ordered by domain ASC.
    """
    conditions: list[Composed | SQL] = []
    having: list[SQL] = []
    params: dict[str, object] = {}
    if has_profile is True:
        conditions.append(SQL("c.profile IS NOT NULL"))
    elif has_profile is False:
        conditions.append(SQL("c.profile IS NULL"))
    status_conditions, status_having = _company_pipeline_status_predicates(
        status, include_disabled
    )
    conditions.extend(status_conditions)
    having.extend(status_having)
    if max_contacts is not None:
        having.append(SQL("COUNT(ct.id) <= %(max_contacts)s"))
        params["max_contacts"] = max_contacts
    if min_contacts is not None:
        having.append(SQL("COUNT(ct.id) >= %(min_contacts)s"))
        params["min_contacts"] = min_contacts
    if tag is not None:
        conditions.append(
            SQL(
                "EXISTS (SELECT 1 FROM tag_assignment ta "
                "WHERE ta.company_id = c.id AND ta.tag_id = %(tag_id)s)"
            )
        )
        params["tag_id"] = tag
    conditions.extend(_exclude_tags_conditions(exclude_tags, "company_id", params))
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    having_clause = SQL("HAVING ") + SQL(" AND ").join(having) if having else SQL("")
    profile_select = SQL(", c.profile") if full else SQL("")
    query = SQL(
        "SELECT LOWER(c.domain) AS domain, c.name, "
        "(c.profile IS NOT NULL) AS has_profile, "
        "c.disabled_reason, COUNT(ct.id) AS contact_count, "
        "{tags}{profile} "
        "FROM company c LEFT JOIN contact ct ON ct.company_id = c.id "
        "{where} GROUP BY c.id {having} ORDER BY LOWER(c.domain)"
    ).format(
        tags=SQL(_COMPANY_TAGS_SQL),
        profile=profile_select,
        where=where,
        having=having_clause,
    )
    rows = connection.execute(query, params).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        entry: dict[str, Any] = {
            "domain": row["domain"],
            "name": row["name"],
            "tags": list(row["tags"] or []),
            "has_profile": bool(row["has_profile"]),
            "contact_count": int(row["contact_count"]),
            "disabled_reason": row["disabled_reason"],
        }
        if full:
            entry["profile"] = row["profile"]
        results.append(entry)
    return results


def company_import_diff(
    connection: psycopg.Connection[dict[str, Any]],
    file_domains: set[str],
    has_profile: bool | None = None,
    max_contacts: int | None = None,
    min_contacts: int | None = None,
    include_disabled: bool = False,
    tag: str | None = None,
    exclude_tags: Sequence[str] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Compare tracker file domains to CRM scope (dry-run only, §V.146).

    CRM side is filtered with the same list-family flags as export. Bucket
    lists are sorted lowercased domains. ``record_count`` is the size of the
    union of file domains and CRM-scope domains.

    Args:
        connection: Open database connection.
        file_domains: Lowercased domains from the tracker NDJSON file.
        has_profile: Presence filter on CRM scope.
        max_contacts: Inclusive upper bound on contact_count.
        min_contacts: Inclusive lower bound on contact_count.
        include_disabled: When ``True``, includes disabled CRM companies.
        tag: Resolved tag id membership filter.
        exclude_tags: Resolved tag ids excluded via NOT EXISTS.
        status: Pipeline cohort filter (§V.138).

    Returns:
        Diff dict with ``missing_in_crm``, ``missing_profile``,
        ``zero_contacts``, ``disabled``, ``extra_in_crm``, and
        ``record_count``.
    """
    crm_rows = export_companies(
        connection,
        has_profile=has_profile,
        max_contacts=max_contacts,
        min_contacts=min_contacts,
        include_disabled=include_disabled,
        tag=tag,
        exclude_tags=exclude_tags,
        status=status,
        full=False,
    )
    crm_by_domain = {str(row["domain"]).lower(): row for row in crm_rows}
    crm_domains = set(crm_by_domain)
    file_set = {d.lower() for d in file_domains}

    missing_in_crm = sorted(file_set - crm_domains)
    extra_in_crm = sorted(crm_domains - file_set)
    missing_profile = sorted(
        domain for domain, row in crm_by_domain.items() if not row["has_profile"]
    )
    zero_contacts = sorted(
        domain for domain, row in crm_by_domain.items() if row["contact_count"] == 0
    )
    disabled = sorted(
        domain
        for domain, row in crm_by_domain.items()
        if row["disabled_reason"] is not None
    )
    return {
        "missing_in_crm": missing_in_crm,
        "missing_profile": missing_profile,
        "zero_contacts": zero_contacts,
        "disabled": disabled,
        "extra_in_crm": extra_in_crm,
        "record_count": len(file_set | crm_domains),
    }


def get_company_by_domain_exact(
    connection: psycopg.Connection[dict[str, Any]],
    domain: str,
) -> Company | None:
    """Get a company by canonical domain only (no alias resolve).

    Used by merge ``--from`` so an already-absorbed brand alias is not
    mistaken for a live source row (§V.143 idempotent path).
    """
    normalized = _normalize_company_domain(domain)
    if not normalized:
        return None
    row = connection.execute(
        "SELECT * FROM company WHERE domain = %(domain)s",
        {"domain": normalized},
    ).fetchone()
    if row is None:
        return None
    return Company.model_validate(row)


def get_company_by_domain(
    connection: psycopg.Connection[dict[str, Any]],
    domain: str,
) -> Company | None:
    """Get a company by primary domain or alias (§V.142).

    Args:
        connection: Open database connection.
        domain: Company domain or registered alias (case-insensitive).

    Returns:
        Canonical company if found, None otherwise.
    """
    normalized = _normalize_company_domain(domain)
    if not normalized:
        return None
    row = connection.execute(
        "SELECT * FROM company WHERE domain = %(domain)s",
        {"domain": normalized},
    ).fetchone()
    if row is not None:
        return Company.model_validate(row)
    row = connection.execute(
        """\
        SELECT c.*
        FROM company_alias a
        JOIN company c ON c.id = a.company_id
        WHERE a.domain = %(domain)s
        """,
        {"domain": normalized},
    ).fetchone()
    if row is None:
        return None
    return Company.model_validate(row)


def merge_companies(
    connection: psycopg.Connection[dict[str, Any]],
    from_company_id: str,
    into_company_id: str,
    *,
    move_contacts: bool = False,
    original_from_domain: str | None = None,
) -> Company | None:
    """Absorb *from* into *into* (§V.143).

    Records ``original_from_domain`` (or the source's current domain) as an
    alias on the survivor, soft-disables the source with
    ``merged:into <into.domain>``, and rewrites the source domain to a
    tombstone so the shared domain space stays unique (§V.142). Optional
    contact reassignment runs in the same transaction.

    Idempotent when the source is already disabled with the matching reason
    and the original domain is already an alias of the survivor.

    Returns:
        The survivor company, or ``None`` if either id is missing.
    """
    if from_company_id == into_company_id:
        raise ValueError("cannot merge a company into itself")
    source = get_company(connection, from_company_id)
    survivor = get_company(connection, into_company_id)
    if source is None or survivor is None:
        return None
    if survivor.disabled_reason is not None:
        raise ValueError(
            f"survivor company is disabled (reason: {survivor.disabled_reason})"
        )
    absorbed_domain = _normalize_company_domain(
        original_from_domain if original_from_domain is not None else source.domain
    )
    expected_reason = _merged_into_reason(survivor.domain)
    existing_alias = connection.execute(
        """\
        SELECT company_id FROM company_alias
        WHERE domain = %(domain)s
        """,
        {"domain": absorbed_domain},
    ).fetchone()
    if (
        source.disabled_reason == expected_reason
        and existing_alias is not None
        and existing_alias["company_id"] == survivor.id
    ):
        return survivor
    if source.disabled_reason is not None and source.disabled_reason != expected_reason:
        raise ValueError(
            f"source company is disabled (reason: {source.disabled_reason})"
        )
    tombstone = _tombstone_merged_domain(source.id)
    # Free the canonical domain before inserting the alias (shared space).
    if source.domain == absorbed_domain or not source.domain.startswith("__merged__."):
        connection.execute(
            """\
            UPDATE company
            SET domain = %(tombstone)s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %(id)s
            """,
            {"tombstone": tombstone, "id": source.id},
        )
    if existing_alias is None:
        connection.execute(
            """\
            INSERT INTO company_alias (domain, company_id)
            VALUES (%(domain)s, %(company_id)s)
            """,
            {"domain": absorbed_domain, "company_id": survivor.id},
        )
    elif existing_alias["company_id"] != survivor.id:
        raise ValueError(
            f"domain {absorbed_domain!r} is already an alias of another company"
        )
    connection.execute(
        """\
        UPDATE company
        SET disabled_reason = %(reason)s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %(id)s
        """,
        {"reason": expected_reason, "id": source.id},
    )
    if move_contacts:
        connection.execute(
            """\
            UPDATE contact
            SET company_id = %(into_id)s,
                updated_at = CURRENT_TIMESTAMP
            WHERE company_id = %(from_id)s
            """,
            {"into_id": survivor.id, "from_id": source.id},
        )
    connection.commit()
    return get_company(connection, survivor.id)


def update_company(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
    **fields: object,
) -> Company | None:
    """Update a company by ID.

    ``profile`` (if present and non-None) is validated via
    ``CompanyProfile.model_validate`` per §V.72 and persisted as JSONB; an
    invalid payload raises ``pydantic.ValidationError`` which the
    ``cli_mutation`` boundary translates to a ``validation_error`` envelope.

    Args:
        connection: Open database connection.
        company_id: Company ID.
        **fields: Fields to update (must be valid Company field names).

    Returns:
        Updated company, or None if not found.
    """
    allowed = set(Company.model_fields) - {"id", "created_at"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if "profile" in updates and updates["profile"] is not None:
        validated = CompanyProfile.model_validate(updates["profile"])
        updates["profile"] = Json(validated.model_dump(exclude_unset=True))
    if not updates:
        return get_company(connection, company_id)
    updates["id"] = company_id
    query = _build_update("company", updates, SQL("id = %(id)s"))
    row = connection.execute(query, updates).fetchone()
    connection.commit()
    if row is None:
        return None
    return Company.model_validate(row)


def disable_company(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
    reason: str,
) -> Company | None:
    """Soft-disable a company by writing ``disabled_reason``.

    A ``disabled_reason IS NULL`` gate blocks double-disable: an already
    disabled company does not match, so the call returns ``None`` without
    overwriting an earlier reason. Disable is reversible -- ``enable_company``
    clears ``disabled_reason`` to re-enable the company (a company with no
    discoverable contacts this cycle may have some next).

    Args:
        connection: Open database connection.
        company_id: Company ID.
        reason: Explanation written to ``disabled_reason`` (stored verbatim);
            operator-facing (out-of-business / not-a-fit). The lead-contacts
            negative-verdict memoization no longer disables a company -- it
            tags it ``no-contacts-found`` or ``contacts-exhausted`` instead
            (§V.96, §V.116).

    Returns:
        Updated company, or ``None`` when no active (not-yet-disabled) company
        with that id exists -- i.e. missing or already disabled.
    """
    row = connection.execute(
        """\
        UPDATE company
        SET disabled_reason = %(reason)s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %(id)s
          AND disabled_reason IS NULL
        RETURNING *
        """,
        {"id": company_id, "reason": reason},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Company.model_validate(row)


def enable_company(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
) -> Company | None:
    """Re-enable a soft-disabled company by clearing ``disabled_reason``.

    Mirror of ``disable_company``. A ``disabled_reason IS NOT NULL`` gate
    blocks enabling an already-active company: an active company does not
    match, so the call returns ``None``. A re-enabled company reappears in the
    default ``company list``.

    Raises ``ValueError`` when this company's domain is registered as an
    alias of a different company (§V.143 — cannot revive a domain that
    still belongs to a survivor's alias set).

    Args:
        connection: Open database connection.
        company_id: Company ID.

    Returns:
        Updated company, or ``None`` when no disabled company with that id
        exists -- i.e. missing or already active.
    """
    current = get_company(connection, company_id)
    if current is None:
        return None
    alias_owner = connection.execute(
        "SELECT company_id FROM company_alias WHERE domain = %(domain)s",
        {"domain": current.domain},
    ).fetchone()
    if alias_owner is not None and alias_owner["company_id"] != company_id:
        raise ValueError(
            f"company domain {current.domain!r} is an alias of another company"
        )
    row = connection.execute(
        """\
        UPDATE company
        SET disabled_reason = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %(id)s
          AND disabled_reason IS NOT NULL
        RETURNING *
        """,
        {"id": company_id},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Company.model_validate(row)


# -- Contact -------------------------------------------------------------------


def create_contact(
    connection: psycopg.Connection[dict[str, Any]],
    email: str,
    company_id: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    title: str | None = None,
    email_confidence: int | None = None,
    verification_meta: dict[str, Any] | None = None,
) -> Contact | None:
    """Create a new contact.

    Uses ``ON CONFLICT (email) DO NOTHING`` per §V.16(+) so callers can
    safely re-invoke without catching ``UniqueViolation``. Returns ``None``
    when the row already exists (sync-path callers re-fetch via
    ``get_contact_by_email``).

    Args:
        connection: Open database connection.
        email: Contact email address.
        company_id: Optional company FK.
        first_name: Optional first name.
        last_name: Optional last name.
        title: Optional role label (lead-metadata, §V.95).
        email_confidence: Optional deliverability score 0-100; ``None`` =
            Bouncer-unknown (§V.95). Schema CHECK enforces the range.
        verification_meta: Optional operator-only verification audit object
            (§V.144). Never injected into agent prompts.

    Returns:
        Created contact, or ``None`` if a contact with this email already
        existed.
    """
    # Canonicalize the natural key lowercase before insert (§V.90). The
    # case-sensitive ``email`` UNIQUE would otherwise let a recased local-part
    # (Outlook/Exchange) mint a duplicate row past ON CONFLICT (§B.121).
    email = email.lower()
    row = connection.execute(
        """\
        INSERT INTO contact (id, email, company_id, first_name, last_name,
                             title, email_confidence, verification_meta)
        VALUES (%(id)s, %(email)s, %(company_id)s,
                %(first_name)s, %(last_name)s,
                %(title)s, %(email_confidence)s, %(verification_meta)s)
        ON CONFLICT (email) DO NOTHING
        RETURNING *
        """,
        {
            "id": _new_id(),
            "email": email,
            "company_id": company_id,
            "first_name": first_name,
            "last_name": last_name,
            "title": title,
            "email_confidence": email_confidence,
            "verification_meta": (
                Json(verification_meta) if verification_meta is not None else None
            ),
        },
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Contact.model_validate(row)


def get_contact(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
) -> Contact | None:
    """Get a contact by ID.

    Args:
        connection: Open database connection.
        contact_id: Contact ID.

    Returns:
        Contact if found, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM contact WHERE id = %(id)s",
        {"id": contact_id},
    ).fetchone()
    if row is None:
        return None
    return Contact.model_validate(row)


def get_contact_by_email(
    connection: psycopg.Connection[dict[str, Any]],
    email: str,
) -> Contact | None:
    """Get a contact by email address.

    Args:
        connection: Open database connection.
        email: Contact email address.

    Returns:
        Contact if found, None otherwise.
    """
    # Match against the lowercase natural key (§V.90) so a mixed-case lookup
    # resolves the canonical row.
    email = email.lower()
    row = connection.execute(
        "SELECT * FROM contact WHERE email = %(email)s",
        {"email": email},
    ).fetchone()
    if row is None:
        return None
    return Contact.model_validate(row)


def create_or_get_contact_by_email(
    connection: psycopg.Connection[dict[str, Any]],
    email: str,
    first_name: str | None = None,
    last_name: str | None = None,
) -> Contact:
    """Return an existing contact by email, creating one if missing.

    If the contact already exists, backfills ``first_name`` / ``last_name``
    only when the stored value is NULL and the caller provided one. Existing
    non-null names are never overwritten.

    Used during inbound sync to resolve a ``From`` header to a contact row
    without forcing callers to branch on existence.

    Args:
        connection: Open database connection.
        email: Contact email address.
        first_name: Optional first name (from From header display name).
        last_name: Optional last name (from From header display name).

    Returns:
        Existing or newly created contact.
    """
    # Canonicalize the natural key lowercase (§V.90) so a recased sender resolves
    # the enrolled row rather than minting a bare duplicate (§B.121). The
    # delegated ``get_contact_by_email`` / ``create_contact`` also lowercase; the
    # explicit normalization keeps the error message and re-fetch key canonical.
    email = email.lower()
    existing = get_contact_by_email(connection, email)
    if existing is not None:
        backfill: dict[str, object] = {}
        if existing.first_name is None and first_name is not None:
            backfill["first_name"] = first_name
        if existing.last_name is None and last_name is not None:
            backfill["last_name"] = last_name
        if not backfill:
            return existing
        updated = update_contact(connection, existing.id, **backfill)
        return updated if updated is not None else existing
    created = create_contact(
        connection,
        email=email,
        first_name=first_name,
        last_name=last_name,
    )
    if created is not None:
        return created
    # Concurrent worker won the race per §V.16(+); re-fetch the existing row.
    racer = get_contact_by_email(connection, email)
    assert racer is not None, (
        f"create_contact returned None for {email!r} but no row was found on re-fetch"
    )
    return racer


def get_contacts_by_emails(
    connection: psycopg.Connection[dict[str, Any]],
    emails: Iterable[str],
) -> dict[str, Contact]:
    """Fetch contacts for a batch of email addresses in one round-trip.

    Used by the sync pipeline to eliminate per-message contact lookups. The
    caller should feed in the set of distinct sender addresses from a batch
    of Gmail messages.

    Args:
        connection: Open database connection.
        emails: Email addresses to look up. Duplicates are tolerated.

    Returns:
        Mapping from lowercase email to Contact for every input email that has
        an existing row. Keys are the canonical lowercase natural key (§V.90),
        so a mixed-case input resolves under its lowercase form. Missing emails
        are simply absent from the dict.
    """
    # Canonicalize and dedupe on the lowercase natural key (§V.90) so case
    # variants collapse to one lookup value and resolve the canonical row.
    unique = list({email.lower() for email in emails})
    if not unique:
        return {}
    rows = connection.execute(
        "SELECT * FROM contact WHERE email = ANY(%(emails)s)",
        {"emails": unique},
    ).fetchall()
    return {row["email"]: Contact.model_validate(row) for row in rows}


def create_contacts_bulk(
    connection: psycopg.Connection[dict[str, Any]],
    emails: Iterable[str],
) -> dict[str, Contact]:
    """Ensure a contact row exists for every input email, in one round-trip.

    Inserts any missing rows with ``ON CONFLICT (email) DO NOTHING``, then
    re-reads every requested email so the returned mapping covers rows
    that were already present (either pre-existing or inserted by a
    concurrent transaction). Safe to run in parallel from multiple sync
    workers; no ``UniqueViolation`` can escape.

    Args:
        connection: Open database connection.
        emails: Email addresses to ensure. Duplicates are tolerated.

    Returns:
        Mapping from lowercase email to Contact for every input email. Keys are
        the canonical lowercase natural key (§V.90); case-variant inputs collapse
        to a single row.
    """
    # Canonicalize and dedupe on the lowercase natural key (§V.90) before insert
    # so case variants share one row and ON CONFLICT never mints a duplicate
    # (§B.121).
    unique = list({email.lower() for email in emails})
    if not unique:
        return {}
    ids = [_new_id() for _ in unique]
    rows = connection.execute(
        """\
        INSERT INTO contact (id, email)
        SELECT id, email
        FROM unnest(%(ids)s::text[], %(emails)s::text[])
             AS t(id, email)
        ON CONFLICT (email) DO NOTHING
        RETURNING *
        """,
        {"ids": ids, "emails": unique},
    ).fetchall()
    connection.commit()
    inserted = {row["email"]: Contact.model_validate(row) for row in rows}
    # Re-fetch any row that was not inserted by this transaction. These
    # cover both pre-existing rows and rows inserted by a concurrent
    # worker (ON CONFLICT DO NOTHING swallows those silently).
    remaining = [email for email in unique if email not in inserted]
    if remaining:
        existing = get_contacts_by_emails(connection, remaining)
        inserted.update(existing)
    return inserted


def list_contacts(
    connection: psycopg.Connection[dict[str, Any]],
    limit: int = 100,
    company_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    include_disabled: bool = False,
    max_email_confidence: int | None = None,
    min_email_confidence: int | None = None,
    title: str | None = None,
    tag: str | None = None,
    exclude_tags: Sequence[str] | None = None,
) -> list[ContactSummary]:
    """List contacts as summaries with optional filters.

    Joins ``company`` once (LEFT JOIN) so each summary carries
    ``company_domain`` without an N+1 lookup per §V.5; ``company_domain``
    is NULL when ``company_id`` is NULL.

    Args:
        connection: Open database connection.
        limit: Maximum results.
        company_id: Filter by company ID.
        since: ISO datetime inclusive lower bound on ``created_at``.
        until: ISO datetime inclusive upper bound on ``created_at``.
        include_disabled: When False (default), only contacts with
            ``disabled_reason IS NULL`` are returned.
        max_email_confidence: When set, surfaces rows with
            ``email_confidence <= N OR email_confidence IS NULL`` for
            cross-run operator review of low-score (high-risk) leads
            (§V.95). NULL = Bouncer-unknown = unverified = high risk, so
            it is surfaced for review, never skipped; admit-all (§V.96)
            must not drop unknowns (§B.76).
        min_email_confidence: When set, surfaces only rows with
            ``email_confidence >= N``; composes with
            ``max_email_confidence`` into a closed range. NULL-score rows
            are excluded by the lower bound (§V.95).
        title: When set, a case-insensitive exact match on ``contact.title``.
            Substring/fuzzy title matching is the ``contact search`` verb's
            job, never the ``list`` filter (§V.115 family 5).
        tag: When set (a resolved tag id), returns only contacts carrying that
            tag -- an Enum-family membership filter over ``tag_assignment``
            (§V.116). Composes with ``exclude_tags`` as an intersection.
        exclude_tags: When set (resolved tag ids), returns only contacts
            carrying NONE of the given tags -- one ``NOT EXISTS`` predicate per
            tag, all intersected (§V.116).

    Returns:
        List of contact summaries ordered by email.
    """
    conditions: list[Composed | SQL] = []
    params: dict[str, object] = {"limit": limit}
    if company_id is not None:
        conditions.append(SQL("c.company_id = %(company_id)s"))
        params["company_id"] = company_id
    if since is not None:
        conditions.append(SQL("c.created_at >= %(since)s"))
        params["since"] = since
    if until is not None:
        conditions.append(SQL("c.created_at <= %(until)s"))
        params["until"] = until
    if not include_disabled:
        conditions.append(SQL("c.disabled_reason IS NULL"))
    if max_email_confidence is not None:
        conditions.append(
            SQL(
                "(c.email_confidence <= %(max_email_confidence)s "
                "OR c.email_confidence IS NULL)"
            )
        )
        params["max_email_confidence"] = max_email_confidence
    if min_email_confidence is not None:
        conditions.append(SQL("c.email_confidence >= %(min_email_confidence)s"))
        params["min_email_confidence"] = min_email_confidence
    if title is not None:
        conditions.append(SQL("LOWER(c.title) = LOWER(%(title)s)"))
        params["title"] = title
    if tag is not None:
        conditions.append(
            SQL(
                "EXISTS (SELECT 1 FROM tag_assignment ta "
                "WHERE ta.contact_id = c.id AND ta.tag_id = %(tag_id)s)"
            )
        )
        params["tag_id"] = tag
    conditions.extend(_exclude_tags_conditions(exclude_tags, "contact_id", params))
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT c.id, c.email, c.first_name, c.last_name, c.title, "
        "c.company_id, co.domain AS company_domain, "
        "c.email_confidence, c.disabled_reason, c.created_at "
        "FROM contact c LEFT JOIN company co ON c.company_id = co.id "
        "{} ORDER BY c.email LIMIT %(limit)s"
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [ContactSummary.model_validate(row) for row in rows]


def search_contacts(
    connection: psycopg.Connection[dict[str, Any]],
    query: str,
    limit: int = 100,
) -> list[ContactSummary]:
    """Search contacts by email, name, or title (§V.158).

    Args:
        connection: Open database connection.
        query: Search term (single-token or multi-token).
        limit: Maximum number of results.

    Returns:
        Matching contact summaries ordered by email. Each carries
        ``title`` + ``company_domain`` (LEFT JOIN company per §V.5),
        mirroring ``list_contacts``.

    Match rules (§V.158):
        - Single-token: per-field LIKE on email / first_name / last_name /
          title (status quo).
        - Full-name: order-preserving match on
          ``TRIM(first_name || ' ' || last_name)`` for the whole query string.
        - Multi-token (whitespace-split): every token AND-matches at least one
          of the same fields (no flood from partial noise).
        - Disabled contacts remain searchable (forensics).
    """
    tokens = [t for t in query.strip().split() if t]
    if not tokens:
        return []

    # Whole-query full-name pattern (covers "David Drouin" as one string).
    params: dict[str, object] = {
        "full_pattern": f"%{query.strip()}%",
        "limit": limit,
    }
    token_clauses: list[Composable] = []
    for i, token in enumerate(tokens):
        key = f"tok_{i}"
        params[key] = f"%{token}%"
        ph = Placeholder(key)
        token_clauses.append(
            SQL(
                "(LOWER(c.email) LIKE LOWER({})"
                " OR LOWER(COALESCE(c.first_name, '')) LIKE LOWER({})"
                " OR LOWER(COALESCE(c.last_name, '')) LIKE LOWER({})"
                " OR LOWER(COALESCE(c.title, '')) LIKE LOWER({}))"
            ).format(ph, ph, ph, ph)
        )
    multi_and = SQL(" AND ").join(token_clauses)
    full_name = SQL(
        "LOWER(TRIM(COALESCE(c.first_name, '') || ' ' || COALESCE(c.last_name, ''))) "
        "LIKE LOWER(%(full_pattern)s)"
    )
    where = SQL("({}) OR ({})").format(full_name, multi_and)
    query_sql = SQL(
        """\
        SELECT c.id, c.email, c.first_name, c.last_name, c.title,
               c.company_id, co.domain AS company_domain,
               c.email_confidence, c.disabled_reason, c.created_at
        FROM contact c
        LEFT JOIN company co ON c.company_id = co.id
        WHERE {}
        ORDER BY c.email
        LIMIT %(limit)s
        """
    ).format(where)
    rows = connection.execute(query_sql, params).fetchall()
    return [ContactSummary.model_validate(row) for row in rows]


def update_contact(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
    **fields: object,
) -> Contact | None:
    """Update a contact by ID.

    Args:
        connection: Open database connection.
        contact_id: Contact ID.
        **fields: Fields to update (must be valid Contact field names).

    Returns:
        Updated contact, or None if not found.
    """
    allowed = set(Contact.model_fields) - {"id", "created_at"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_contact(connection, contact_id)
    if "verification_meta" in updates and updates["verification_meta"] is not None:
        updates["verification_meta"] = Json(updates["verification_meta"])
    updates["id"] = contact_id
    query = _build_update("contact", updates, SQL("id = %(id)s"))
    row = connection.execute(query, updates).fetchone()
    connection.commit()
    if row is None:
        return None
    return Contact.model_validate(row)


def disable_contact(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
    reason: str,
) -> Contact | None:
    """Set a global block on a contact.

    This is a hard block across all workflows. ``send_email`` and
    ``reply_email`` refuse contacts whose ``disabled_reason`` is non-null.
    Any non-empty reason string disables the contact; conventional values
    include ``"bounced: <detail>"`` and ``"unsubscribed: <detail>"``.

    Args:
        connection: Open database connection.
        contact_id: Contact ID.
        reason: Explanation for the block (stored verbatim in
            ``disabled_reason``).

    Returns:
        Updated contact, or None if not found.
    """
    row = connection.execute(
        """\
        UPDATE contact
        SET disabled_reason = %(reason)s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %(id)s
        RETURNING *
        """,
        {"id": contact_id, "reason": reason},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Contact.model_validate(row)


def enable_contact(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
) -> Contact | None:
    """Clear a contact's global block by clearing ``disabled_reason``.

    Clears any reason regardless of prefix -- the operator owns consent, so a
    ``"bounced:"`` or ``"unsubscribed:"`` block re-enables the same way (no
    unsubscribe carve-out). This is operator-only; the agent disables a contact
    on bounce or unsubscribe but never re-enables it. A ``disabled_reason IS
    NOT NULL`` gate blocks enabling an already-active contact (returns
    ``None``).

    Args:
        connection: Open database connection.
        contact_id: Contact ID.

    Returns:
        Updated contact, or ``None`` when no disabled contact with that id
        exists -- i.e. missing or already active.
    """
    row = connection.execute(
        """\
        UPDATE contact
        SET disabled_reason = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %(id)s
          AND disabled_reason IS NOT NULL
        RETURNING *
        """,
        {"id": contact_id},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Contact.model_validate(row)


# -- Workflow ------------------------------------------------------------------


def create_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    template: str,
    account_id: str,
    theme: str = "blue",
) -> Workflow | None:
    """Create a new workflow.

    The workflow's ``type`` (``inbound`` / ``outbound``) is derived from
    the template's declared direction -- callers do not pass ``type``.

    Uses ``ON CONFLICT (name) DO NOTHING`` per §V.16(+) so callers can safely
    re-invoke without catching ``UniqueViolation``. ``name`` is globally unique
    (§V.90/§V.103), so a collision against any account returns ``None``.

    Args:
        connection: Open database connection.
        name: Workflow name. Globally unique, kebab-shaped (§V.90/§V.103).
        template: Template name (e.g. ``outbound-general``). Drives both
            the agent shape and the workflow's direction.
        account_id: Account FK.
        theme: Email color theme (default "blue").

    Returns:
        Created workflow, or ``None`` if a workflow with this ``name``
        already existed.
    """
    from mailpilot.agent.templates import TEMPLATES

    if template not in TEMPLATES:
        raise ValueError(
            f"unknown workflow template {template!r}; valid: {sorted(TEMPLATES.keys())}"
        )
    direction = TEMPLATES[template].direction  # pyright: ignore[reportArgumentType]
    row = connection.execute(
        """\
        WITH inserted AS (
            INSERT INTO workflow (id, name, template, type, account_id, theme)
            VALUES (
                %(id)s, %(name)s, %(template)s, %(type)s, %(account_id)s, %(theme)s
            )
            ON CONFLICT (name) DO NOTHING
            RETURNING *
        )
        SELECT inserted.*, account.email AS account_email
        FROM inserted JOIN account ON account.id = inserted.account_id
        """,
        {
            "id": _new_id(),
            "name": name,
            "template": template,
            "type": direction,
            "account_id": account_id,
            "theme": theme,
        },
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Workflow.model_validate(row)


def get_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> Workflow | None:
    """Get a workflow by ID.

    Args:
        connection: Open database connection.
        workflow_id: Workflow ID.

    Returns:
        Workflow if found, None otherwise.
    """
    row = connection.execute(
        """\
        SELECT workflow.*, account.email AS account_email
        FROM workflow JOIN account ON account.id = workflow.account_id
        WHERE workflow.id = %(id)s
        """,
        {"id": workflow_id},
    ).fetchone()
    if row is None:
        return None
    return Workflow.model_validate(row)


def get_workflow_by_name(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
) -> Workflow | None:
    """Resolve a workflow by its globally unique ``name`` (§V.90/§V.107).

    The name is the canonical cross-environment key (§V.103). Stored names are
    kebab-shaped (lowercase), so the lookup lowercases the input to resolve the
    natural key case-insensitively, mirroring the CLI polymorphic resolver.
    Returns ``None`` when no workflow carries the name -- the caller surfaces
    ``not_found``.

    Args:
        connection: Open database connection.
        name: Workflow name (case-insensitive).

    Returns:
        Workflow if found, None otherwise.
    """
    row = connection.execute(
        """\
        SELECT workflow.*, account.email AS account_email
        FROM workflow JOIN account ON account.id = workflow.account_id
        WHERE workflow.name = %(name)s
        """,
        {"name": name.lower()},
    ).fetchone()
    if row is None:
        return None
    return Workflow.model_validate(row)


def list_workflows(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str | None = None,
    status: str | None = None,
    workflow_type: str | None = None,
    template: str | None = None,
    limit: int = 100,
    since: str | None = None,
    until: str | None = None,
) -> list[WorkflowSummary]:
    """List workflows as summaries with optional filters.

    Args:
        connection: Open database connection.
        account_id: Filter by account ID.
        status: Filter by workflow status (e.g., "active").
        workflow_type: Filter by workflow type ("inbound" or "outbound").
        template: Filter by template name.
        limit: Maximum results.
        since: ISO datetime inclusive lower bound on ``created_at``.
        until: ISO datetime inclusive upper bound on ``created_at``.

    Returns:
        List of workflow summaries ordered by creation time.
    """
    conditions: list[SQL] = []
    params: dict[str, object] = {"limit": limit}
    if account_id is not None:
        conditions.append(SQL("workflow.account_id = %(account_id)s"))
        params["account_id"] = account_id
    if status is not None:
        conditions.append(SQL("workflow.status = %(status)s"))
        params["status"] = status
    if workflow_type is not None:
        conditions.append(SQL("workflow.type = %(workflow_type)s"))
        params["workflow_type"] = workflow_type
    if template is not None:
        conditions.append(SQL("workflow.template = %(template)s"))
        params["template"] = template
    if since is not None:
        conditions.append(SQL("workflow.created_at >= %(since)s"))
        params["since"] = since
    if until is not None:
        conditions.append(SQL("workflow.created_at <= %(until)s"))
        params["until"] = until
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT workflow.id, workflow.name, workflow.template, workflow.type, "
        "workflow.account_id, account.email AS account_email, "
        "workflow.status, workflow.created_at "
        "FROM workflow JOIN account ON account.id = workflow.account_id "
        "{} ORDER BY workflow.created_at LIMIT %(limit)s"
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [WorkflowSummary.model_validate(row) for row in rows]


def list_workflows_full(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str | None = None,
) -> list[Workflow]:
    """List workflows as full rows ordered by name.

    Used by ``workflow export`` (account-scoped) to emit a declarative payload
    keyed on the globally unique ``name`` (§V.90/§V.103) and by
    ``workflow check`` (account omitted -> every row) to join the live rows
    against the catalog by ``name`` (§V.134). Ordering by ``name`` makes the
    output deterministic for diffs and round-trip testing.

    Args:
        connection: Open database connection.
        account_id: Owning account ID; ``None`` lists every account's rows.

    Returns:
        Full ``Workflow`` rows ordered by ``name``.
    """
    where = SQL("WHERE workflow.account_id = %(account_id)s") if account_id else SQL("")
    query = SQL(
        "SELECT workflow.*, account.email AS account_email "
        "FROM workflow JOIN account ON account.id = workflow.account_id "
        "{} ORDER BY workflow.name"
    ).format(where)
    rows = connection.execute(query, {"account_id": account_id}).fetchall()
    return [Workflow.model_validate(row) for row in rows]


def get_workflow_stats(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> WorkflowStats | None:
    """Compute the per-campaign funnel for one workflow (§V.132).

    A single deterministic SQL aggregate over the workflow's enrollments -- no
    LLM. The enrollment row (one per contact) is the grain, so each stage is
    contact-distinct and a multi-touch outbound sequence never double-counts.
    Eight stages:

    - ``enrolled``: the workflow's enrollment rows.
    - ``sent`` / ``bounced``: enrollments with at least one outbound email of
      that status (send auto-resolves ``email.contact_id`` from the recipient,
      ``email.workflow_id`` is set at spawn).
    - ``replied``: enrollments with at least one routed inbound email (routing
      sets ``contact_id`` + ``workflow_id`` per §V.27).
    - ``meeting_booked``: enrollments whose latest terminal outcome is
      ``enrollment_completed`` (disposition-independent -- completed maps only
      to meeting_booked).
    - ``contact_later`` / ``do_not_contact``: enrollments whose latest terminal
      outcome is ``enrollment_failed``, split by ``detail->>'disposition'``.
    - ``active``: ``status='active'`` enrollments with no terminal outcome.

    Outcomes are timeline-only (§V.15): the latest ``enrollment_completed`` /
    ``enrollment_failed`` activity per enrollment wins (cf
    ``list_enrollments_with_outcomes``). Pre-§V.132 failed rows lack a
    disposition key, so they fall out of both failure splits (legacy gap).

    Args:
        connection: Open database connection.
        workflow_id: Workflow ID (entity ref per §V.107).

    Returns:
        ``WorkflowStats`` for the workflow, or ``None`` when no workflow
        matches the id (the CLI maps None -> ``not_found``).
    """
    workflow = get_workflow(connection, workflow_id)
    if workflow is None:
        return None
    row = connection.execute(
        """\
        WITH per_enrollment AS (
            SELECT
                e.status,
                EXISTS (
                    SELECT 1 FROM email
                    WHERE email.workflow_id = e.workflow_id
                      AND email.contact_id = e.contact_id
                      AND email.direction = 'outbound'
                      AND email.status = 'sent'
                ) AS has_sent,
                EXISTS (
                    SELECT 1 FROM email
                    WHERE email.workflow_id = e.workflow_id
                      AND email.contact_id = e.contact_id
                      AND email.direction = 'outbound'
                      AND email.status = 'bounced'
                ) AS has_bounced,
                EXISTS (
                    SELECT 1 FROM email
                    WHERE email.workflow_id = e.workflow_id
                      AND email.contact_id = e.contact_id
                      AND email.direction = 'inbound'
                      AND email.is_routed = TRUE
                ) AS has_replied,
                outcome.latest_outcome,
                outcome.disposition
            FROM enrollment e
            LEFT JOIN LATERAL (
                SELECT
                    CASE a.type
                        WHEN 'enrollment_completed' THEN 'completed'
                        WHEN 'enrollment_failed' THEN 'failed'
                    END AS latest_outcome,
                    a.detail->>'disposition' AS disposition
                FROM activity a
                WHERE a.contact_id = e.contact_id
                  AND a.workflow_id = e.workflow_id
                  AND a.type IN ('enrollment_completed', 'enrollment_failed')
                ORDER BY a.created_at DESC
                LIMIT 1
            ) outcome ON TRUE
            WHERE e.workflow_id = %(workflow_id)s
        )
        SELECT
            COUNT(*) AS enrolled,
            COUNT(*) FILTER (WHERE has_sent) AS sent,
            COUNT(*) FILTER (WHERE has_bounced) AS bounced,
            COUNT(*) FILTER (WHERE has_replied) AS replied,
            COUNT(*) FILTER (WHERE latest_outcome = 'completed')
                AS meeting_booked,
            COUNT(*) FILTER (
                WHERE latest_outcome = 'failed' AND disposition = 'contact_later'
            ) AS contact_later,
            COUNT(*) FILTER (
                WHERE latest_outcome = 'failed' AND disposition = 'do_not_contact'
            ) AS do_not_contact,
            COUNT(*) FILTER (
                WHERE status = 'active' AND latest_outcome IS NULL
            ) AS active,
            COUNT(*) FILTER (
                WHERE status = 'active'
                  AND latest_outcome IS NULL
                  AND NOT has_sent
            ) AS awaiting_first_touch,
            COUNT(*) FILTER (WHERE status = 'disabled') AS disabled
        FROM per_enrollment
        """,
        {"workflow_id": workflow_id},
    ).fetchone()
    assert row is not None  # aggregate over a present workflow always returns 1 row

    touches: dict[str, TouchStageCounts] = {}
    configured_touches = workflow.touches
    if configured_touches is not None and configured_touches >= 1:
        touch_rows = connection.execute(
            SQL(
                """\
                WITH touch_nums AS (
                    SELECT generate_series(1, %(touches)s) AS touch
                )
                SELECT
                    tn.touch::text AS touch_key,
                    (
                        SELECT COUNT(*)::int FROM enrollment e
                        WHERE e.workflow_id = %(workflow_id)s
                          AND (
                              SELECT COUNT(*)::int FROM email
                              WHERE email.workflow_id = e.workflow_id
                                AND email.contact_id = e.contact_id
                                AND email.direction = 'outbound'
                                AND email.status = 'sent'
                          ) >= tn.touch
                    ) AS sent,
                    (
                        SELECT COUNT(*)::int FROM task t
                        WHERE t.workflow_id = %(workflow_id)s
                          AND t.status = 'pending'
                          AND {touch} = tn.touch
                    ) AS pending
                FROM touch_nums tn
                ORDER BY tn.touch
                """
            ).format(touch=_sql_parse_touch(SQL("t.context"))),
            {"workflow_id": workflow_id, "touches": configured_touches},
        ).fetchall()
        for tr in touch_rows:
            touches[tr["touch_key"]] = TouchStageCounts(
                sent=tr["sent"], pending=tr["pending"]
            )

    return WorkflowStats(
        workflow_id=workflow.id,
        workflow_name=workflow.name,
        enrolled=row["enrolled"],
        sent=row["sent"],
        bounced=row["bounced"],
        replied=row["replied"],
        meeting_booked=row["meeting_booked"],
        contact_later=row["contact_later"],
        do_not_contact=row["do_not_contact"],
        active=row["active"],
        touches=touches,
        awaiting_first_touch=row["awaiting_first_touch"],
        disabled=row["disabled"],
    )


def get_workflow_report(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    *,
    stuck: bool = False,
    touch: int | None = None,
    status: str | None = None,
    limit: int = 500,
) -> WorkflowReport | None:
    """Composite campaign report: funnel + tasks + enrollment matrix (§V.153).

    Pure SQL / deterministic reuses of ``get_workflow_stats``,
    ``get_task_stats``, and ``list_enrollments_detailed(full=True)``. No LLM,
    no CRM writes.
    """
    workflow = get_workflow(connection, workflow_id)
    if workflow is None:
        return None
    funnel = get_workflow_stats(connection, workflow_id)
    assert funnel is not None
    tasks = get_task_stats(connection, workflow_id=workflow_id)
    enrollments = list_enrollments_detailed(
        connection,
        workflow_id=workflow_id,
        status=status,
        limit=limit,
        full=True,
        touch=touch,
        stuck=stuck,
        sort="next_scheduled_at",
    )
    return WorkflowReport(
        workflow=WorkflowReportMeta(
            name=workflow.name,
            touches=workflow.touches,
            touch_interval_days=workflow.touch_interval_days,
            status=workflow.status,
        ),
        funnel=funnel,
        tasks=tasks,
        enrollments=enrollments,
    )


def get_workflow_status_health(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> WorkflowStatusHealth | None:
    """Ops-health composite for one workflow (§V.157).

    Reuses funnel active count, wording state when catalog is empty
    (unknown), and task overdue / failed-24h counts. No LLM.
    """
    workflow = get_workflow(connection, workflow_id)
    if workflow is None:
        return None
    funnel = get_workflow_stats(connection, workflow_id)
    assert funnel is not None

    overdue_row = connection.execute(
        """\
        SELECT COUNT(*)::int AS n FROM task
        WHERE workflow_id = %(workflow_id)s
          AND status = 'pending'
          AND scheduled_at < NOW()
        """,
        {"workflow_id": workflow_id},
    ).fetchone()
    failed_row = connection.execute(
        """\
        SELECT COUNT(*)::int AS n FROM task
        WHERE workflow_id = %(workflow_id)s
          AND status = 'failed'
          AND completed_at >= NOW() - INTERVAL '24 hours'
        """,
        {"workflow_id": workflow_id},
    ).fetchone()
    assert overdue_row is not None
    assert failed_row is not None

    sync = _sync_loop_block(connection)
    if sync is None:
        run_loop = "stopped"
    else:
        # Heartbeat age: > 120s without tick counts as stale (2x default run_interval).
        age = sync.get("heartbeat_age_seconds")
        run_loop = "stale" if isinstance(age, int) and age > 120 else "ok"

    return WorkflowStatusHealth(
        workflow=WorkflowReportMeta(
            name=workflow.name,
            touches=workflow.touches,
            touch_interval_days=workflow.touch_interval_days,
            status=workflow.status,
        ),
        wording="unknown",
        run_loop=run_loop,
        overdue_tasks=overdue_row["n"],
        failed_tasks_24h=failed_row["n"],
        enrollments_never_sent=funnel.awaiting_first_touch,
        funnel_active=funnel.active,
    )


def get_queue_report(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    detail: bool = False,
    workflow_id: str | None = None,
    tz: str = "UTC",
    limit: int = 100,
    overdue: bool = False,
    now: datetime | None = None,
) -> QueueReport:
    """Build the ``show queue`` report (§V.166).

    Workflow grain: one row per in-scope workflow (draft/active/paused),
    sorted by next pending ``scheduled_at`` ascending (empty last) then name.
    Task grain: pending tasks only, sorted by ``scheduled_at`` ascending
    (does not change ``list_tasks`` DESC). ``--limit`` and ``--overdue``
    apply to task grain only. No LLM, no write.
    """
    from zoneinfo import ZoneInfo

    ZoneInfo(tz)  # raise ZoneInfoNotFoundError for the CLI to map
    clock = now if now is not None else datetime.now(UTC)
    if detail:
        rows = _queue_task_rows(
            connection,
            workflow_id=workflow_id,
            tz=tz,
            limit=limit,
            overdue=overdue,
            now=clock,
        )
        return QueueReport(grain="task", tz=tz, rows=rows)
    workflow_rows = _queue_workflow_rows(connection, workflow_id=workflow_id, tz=tz)
    return QueueReport(grain="workflow", tz=tz, rows=workflow_rows)


def _queue_workflow_rows(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    workflow_id: str | None,
    tz: str,
) -> list[QueueWorkflowRow]:
    """Aggregate one row per workflow for the default queue grain."""
    conditions: list[SQL] = []
    params: dict[str, object] = {"tz": tz}
    if workflow_id is not None:
        conditions.append(SQL("w.id = %(workflow_id)s"))
        params["workflow_id"] = workflow_id
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        """\
        WITH enroll AS (
            SELECT
                e.workflow_id,
                COUNT(*) FILTER (
                    WHERE e.status = 'active' AND outcome.latest_outcome IS NULL
                )::int AS active,
                COUNT(*) FILTER (
                    WHERE e.status = 'active'
                      AND outcome.latest_outcome IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM email
                          WHERE email.workflow_id = e.workflow_id
                            AND email.contact_id = e.contact_id
                            AND email.direction = 'outbound'
                            AND email.status = 'sent'
                      )
                )::int AS never_sent
            FROM enrollment e
            LEFT JOIN LATERAL (
                SELECT
                    CASE a.type
                        WHEN 'enrollment_completed' THEN 'completed'
                        WHEN 'enrollment_failed' THEN 'failed'
                    END AS latest_outcome
                FROM activity a
                WHERE a.contact_id = e.contact_id
                  AND a.workflow_id = e.workflow_id
                  AND a.type IN ('enrollment_completed', 'enrollment_failed')
                ORDER BY a.created_at DESC
                LIMIT 1
            ) outcome ON TRUE
            GROUP BY e.workflow_id
        ),
        tasks AS (
            SELECT
                t.workflow_id,
                COUNT(*) FILTER (WHERE t.status = 'pending')::int AS pending,
                COUNT(*) FILTER (
                    WHERE t.status = 'pending' AND t.scheduled_at < NOW()
                )::int AS overdue,
                COUNT(*) FILTER (
                    WHERE t.status = 'pending'
                      AND (t.scheduled_at AT TIME ZONE %(tz)s)::date
                        = (NOW() AT TIME ZONE %(tz)s)::date
                )::int AS due_today,
                MIN(t.scheduled_at) FILTER (WHERE t.status = 'pending')
                    AS next_at,
                COUNT(*) FILTER (
                    WHERE t.status = 'failed'
                      AND t.completed_at >= NOW() - INTERVAL '24 hours'
                )::int AS failed_24h
            FROM task t
            GROUP BY t.workflow_id
        )
        SELECT
            w.name AS workflow,
            w.status,
            COALESCE(enroll.active, 0) AS active,
            COALESCE(tasks.pending, 0) AS pending,
            COALESCE(tasks.overdue, 0) AS overdue,
            COALESCE(tasks.due_today, 0) AS due_today,
            tasks.next_at,
            COALESCE(tasks.failed_24h, 0) AS failed_24h,
            COALESCE(enroll.never_sent, 0) AS never_sent
        FROM workflow w
        LEFT JOIN enroll ON enroll.workflow_id = w.id
        LEFT JOIN tasks ON tasks.workflow_id = w.id
        {}
        ORDER BY tasks.next_at ASC NULLS LAST, w.name ASC
        """
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [QueueWorkflowRow.model_validate(row) for row in rows]


def _queue_task_rows(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    workflow_id: str | None,
    tz: str,
    limit: int,
    overdue: bool,
    now: datetime,
) -> list[QueueTaskRow]:
    """Pending-task grain, queue order (scheduled_at ASC)."""
    from zoneinfo import ZoneInfo

    from mailpilot.queue import format_queue_touch, format_queue_when

    zone = ZoneInfo(tz)
    conditions: list[SQL] = [SQL("t.status = 'pending'")]
    params: dict[str, object] = {"limit": limit}
    if workflow_id is not None:
        conditions.append(SQL("t.workflow_id = %(workflow_id)s"))
        params["workflow_id"] = workflow_id
    if overdue:
        conditions.append(SQL("t.scheduled_at < NOW()"))
    where = SQL("WHERE ") + SQL(" AND ").join(conditions)
    query = SQL(
        """\
        SELECT
            t.id AS task_id,
            t.enrollment_id,
            t.scheduled_at,
            COALESCE(
                NULLIF(
                    TRIM(BOTH FROM CONCAT_WS(' ', c.first_name, c.last_name)),
                    ''
                ),
                c.email
            ) AS contact,
            c.email AS email,
            COALESCE(co.domain, '') AS company,
            w.name AS workflow,
            t.context,
            COALESCE(t.context->>'trigger', '') AS trigger,
            t.status AS state,
            t.attempt_count AS attempts
        FROM task t
        JOIN workflow w ON w.id = t.workflow_id
        JOIN contact c ON c.id = t.contact_id
        LEFT JOIN company co ON co.id = c.company_id
        {}
        ORDER BY t.scheduled_at ASC
        LIMIT %(limit)s
        """
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    result: list[QueueTaskRow] = []
    for row in rows:
        context = row["context"]
        context_dict = context if isinstance(context, dict) else None
        result.append(
            QueueTaskRow(
                when=format_queue_when(row["scheduled_at"], now=now, tz=zone),
                scheduled_at=row["scheduled_at"],
                contact=row["contact"],
                email=row["email"],
                company=row["company"],
                workflow=row["workflow"],
                touch=format_queue_touch(context_dict),
                trigger=row["trigger"],
                state=row["state"],
                attempts=row["attempts"],
                task_id=row["task_id"],
                enrollment_id=row["enrollment_id"],
            )
        )
    return result


def _compute_workflow_wording_hash(
    template: str,
    theme: str,
    goal: str,
    instructions: str,
    touches: int | None,
    touch_interval_days: int | None,
) -> str:
    """SHA-256 over the def fields, name excluded (§V.134).

    Hashes ``{template, theme, goal, instructions, touches, touch_interval_days}``
    -- the cadence pair joined the def fields per §V.136. The workflow ``name``
    is the join key, never a hashed field (§V.134), so it is excluded here. A
    canonical JSON serialization (sorted keys) keeps the hash stable across field
    order and is safe for the pipe and newline content that ``instructions``
    carries -- a delimiter-joined string could collide. The nullable cadence
    ints serialize as JSON numbers or ``null``.

    Args:
        template: Template name.
        theme: Email color theme.
        goal: Enrollment success goal.
        instructions: Workflow instructions.
        touches: Total sends in the touch cadence, or None for single-touch.
        touch_interval_days: Days between touches, or None for single-touch.

    Returns:
        Hex SHA-256 digest of the canonical def payload.
    """
    canonical = json.dumps(
        {
            "template": template,
            "theme": theme,
            "goal": goal,
            "instructions": instructions,
            "touches": touches,
            "touch_interval_days": touch_interval_days,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _catalog_wording_hash(entry: dict[str, Any]) -> str:
    """Hash a parsed catalog def the way an import would persist it (§V.134).

    Applies the same field defaults ``workflow import`` applies (theme -> blue,
    goal/instructions -> empty string) so an unchanged def hashes equal to the
    row it produced. ``template`` defaults to empty so a malformed def without
    one simply fails to match any row rather than raising. The cadence pair has
    no default -- an omitted ``touches`` / ``touch_interval_days`` is None,
    matching a single-touch row's NULL columns (§V.136).
    """
    return _compute_workflow_wording_hash(
        template=str(entry.get("template") or ""),
        theme=str(entry.get("theme") or "blue"),
        goal=str(entry.get("goal") or ""),
        instructions=str(entry.get("instructions") or ""),
        touches=entry.get("touches"),
        touch_interval_days=entry.get("touch_interval_days"),
    )


def check_workflow_wording(
    connection: psycopg.Connection[dict[str, Any]],
    catalog: dict[str, dict[str, Any]],
    scope_to_catalog: bool = False,
) -> WorkflowCheck:
    """Compare catalog defs against live rows by name and classify each (§V.134).

    A read-only 2-way live SHA-256 over the def fields
    ``{template, theme, goal, instructions, touches, touch_interval_days}``
    mirroring ``db check``: no stored column, both sides hashed on the fly. The
    cadence pair joined the hashed set per §V.136. The globally unique ``name``
    (§V.90)
    is the join key, so the comparison spans every account's rows. Each name
    lands in one of four states:

    - ``in_sync``: name on both sides, hashes equal.
    - ``out_of_sync``: name on both sides, hashes differ (re-import due).
    - ``not_imported``: name in a catalog def, no row.
    - ``orphaned``: name in a row, no catalog def.

    Def fields are import-only (§V.103), so there is no row-ahead state -- a
    mismatch always means the catalog leads. The report is informational
    (``ok:true`` regardless of state); it is never a deploy gate.

    Args:
        connection: Open database connection.
        catalog: Parsed catalog defs keyed by the def's ``name`` field (the CLI
            reader applies last-def-wins on duplicate names per §V.134).
        scope_to_catalog: When ``True``, report only the catalog names -- a DB
            row with no def is dropped, never ``orphaned`` (§V.134). The CLI
            sets this for a specific-file check so the report presents only the
            inquired workflows; a directory check leaves it ``False`` so an
            unaccounted DB row still surfaces as ``orphaned`` drift.

    Returns:
        ``WorkflowCheck`` carrying one entry per name plus rollup counts.
    """
    row_hashes = {
        row.name: _compute_workflow_wording_hash(
            template=row.template,
            theme=row.theme,
            goal=row.goal,
            instructions=row.instructions,
            touches=row.touches,
            touch_interval_days=row.touch_interval_days,
        )
        for row in list_workflows_full(connection)
    }
    catalog_hashes = {
        name: _catalog_wording_hash(entry) for name, entry in catalog.items()
    }

    names = set(catalog_hashes)
    if not scope_to_catalog:
        names |= set(row_hashes)
    entries: list[WorkflowCheckEntry] = []
    for name in sorted(names):
        catalog_hash = catalog_hashes.get(name)
        row_hash = row_hashes.get(name)
        if catalog_hash is not None and row_hash is not None:
            state = "in_sync" if catalog_hash == row_hash else "out_of_sync"
        elif catalog_hash is not None:
            state = "not_imported"
        else:
            state = "orphaned"
        entries.append(
            WorkflowCheckEntry(
                name=name,
                state=state,
                catalog_hash=catalog_hash,
                row_hash=row_hash,
            )
        )
    return WorkflowCheck(
        workflows=entries,
        in_sync=sum(1 for entry in entries if entry.state == "in_sync"),
        out_of_sync=sum(1 for entry in entries if entry.state == "out_of_sync"),
        not_imported=sum(1 for entry in entries if entry.state == "not_imported"),
        orphaned=sum(1 for entry in entries if entry.state == "orphaned"),
    )


def search_workflows(
    connection: psycopg.Connection[dict[str, Any]],
    query: str,
    limit: int = 100,
) -> list[WorkflowSummary]:
    """Search workflows by name or goal.

    Args:
        connection: Open database connection.
        query: Search term (matched against name and goal).
        limit: Maximum number of results.

    Returns:
        Matching workflow summaries ordered by name.
    """
    pattern = f"%{query}%"
    rows = connection.execute(
        """\
        SELECT workflow.id, workflow.name, workflow.template, workflow.type,
               workflow.account_id, account.email AS account_email,
               workflow.status, workflow.created_at
        FROM workflow JOIN account ON account.id = workflow.account_id
        WHERE LOWER(workflow.name) LIKE LOWER(%(pattern)s)
           OR LOWER(workflow.goal) LIKE LOWER(%(pattern)s)
        ORDER BY LOWER(workflow.name)
        LIMIT %(limit)s
        """,
        {"pattern": pattern, "limit": limit},
    ).fetchall()
    return [WorkflowSummary.model_validate(row) for row in rows]


def update_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    **fields: object,
) -> Workflow | None:
    """Update a workflow by ID.

    Writable fields: the def fields ``name``, ``goal``, ``instructions``,
    ``theme``, ``touches``, ``touch_interval_days`` (import-only writers per
    §V.103, the cadence pair per §V.136) plus the non-def ``account_id`` (account
    re-binding, the sole field ``workflow update`` exposes). Status transitions
    use ``activate_workflow()`` / ``pause_workflow()``. ``type`` and ``template``
    are immutable after creation (§V.44).

    Args:
        connection: Open database connection.
        workflow_id: Workflow ID.
        **fields: Fields to update.

    Returns:
        Updated workflow, or None if not found.
    """
    allowed = {
        "name",
        "goal",
        "instructions",
        "theme",
        "touches",
        "touch_interval_days",
        "account_id",
    }
    if "template" in fields:
        raise ValueError(
            "workflow.template is immutable; "
            "delete and recreate the workflow to change template"
        )
    if "type" in fields:
        raise ValueError(
            "workflow.type is derived from the template at create time "
            "and cannot be updated"
        )
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_workflow(connection, workflow_id)
    updates["id"] = workflow_id
    query = _build_update("workflow", updates, SQL("id = %(id)s"))
    row = connection.execute(query, updates).fetchone()
    connection.commit()
    if row is None:
        return None
    return get_workflow(connection, workflow_id)


def activate_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> Workflow:
    """Transition a workflow to active status.

    Valid transitions: ``draft -> active``, ``paused -> active``.
    Guards: ``goal`` and ``instructions`` must be non-empty.

    Args:
        connection: Open database connection.
        workflow_id: Workflow ID.

    Returns:
        Updated workflow.

    Raises:
        ValueError: If workflow not found, already active, or missing
            goal/instructions.
    """
    workflow = get_workflow(connection, workflow_id)
    if workflow is None:
        raise ValueError(f"workflow {workflow_id} not found")
    if workflow.status == "active":
        raise ValueError("workflow is already active")
    if not workflow.goal.strip():
        raise ValueError("goal must be non-empty to activate")
    if not workflow.instructions.strip():
        raise ValueError("instructions must be non-empty to activate")
    row = connection.execute(
        """\
        WITH updated AS (
            UPDATE workflow
            SET status = 'active', updated_at = CURRENT_TIMESTAMP
            WHERE id = %(id)s
            RETURNING *
        )
        SELECT updated.*, account.email AS account_email
        FROM updated JOIN account ON account.id = updated.account_id
        """,
        {"id": workflow_id},
    ).fetchone()
    connection.commit()
    return Workflow.model_validate(row)


def pause_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> Workflow:
    """Transition a workflow to paused status.

    Valid transition: ``active -> paused``.

    Args:
        connection: Open database connection.
        workflow_id: Workflow ID.

    Returns:
        Updated workflow.

    Raises:
        ValueError: If workflow not found or not active.
    """
    workflow = get_workflow(connection, workflow_id)
    if workflow is None:
        raise ValueError(f"workflow {workflow_id} not found")
    if workflow.status != "active":
        raise ValueError(f"cannot pause workflow in status '{workflow.status}'")
    row = connection.execute(
        """\
        WITH updated AS (
            UPDATE workflow
            SET status = 'paused', updated_at = CURRENT_TIMESTAMP
            WHERE id = %(id)s
            RETURNING *
        )
        SELECT updated.*, account.email AS account_email
        FROM updated JOIN account ON account.id = updated.account_id
        """,
        {"id": workflow_id},
    ).fetchone()
    connection.commit()
    return Workflow.model_validate(row)


# -- Enrollment ----------------------------------------------------------------


def create_enrollment(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> Enrollment | None:
    """Enroll a contact in a workflow.

    Uses ON CONFLICT DO NOTHING so callers can safely re-invoke without
    catching unique-constraint errors. Returns None when the row already
    exists (same pattern as ``create_email``). ``id`` is minted client-side
    per §V.12 (UUIDv7).

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        contact_id: Contact FK.

    Returns:
        Created enrollment, or None if it already existed.
    """
    row = connection.execute(
        """\
        WITH inserted AS (
            INSERT INTO enrollment (id, workflow_id, contact_id)
            VALUES (%(id)s, %(workflow_id)s, %(contact_id)s)
            ON CONFLICT (workflow_id, contact_id) DO NOTHING
            RETURNING *
        )
        SELECT
            inserted.*,
            workflow.name AS workflow_name,
            contact.email AS contact_email,
            TRIM(
                COALESCE(contact.first_name, '')
                || ' '
                || COALESCE(contact.last_name, '')
            ) AS contact_name
        FROM inserted
        JOIN workflow ON workflow.id = inserted.workflow_id
        JOIN contact ON contact.id = inserted.contact_id
        """,
        {"id": _new_id(), "workflow_id": workflow_id, "contact_id": contact_id},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Enrollment.model_validate(row)


def get_enrollment(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> Enrollment | None:
    """Get an enrollment by composite ``(workflow_id, contact_id)`` key.

    The ``(workflow_id, contact_id)`` pair remains a UNIQUE constraint
    post-migration to scalar ``id`` so composite-key lookups stay valid for
    the inbound routing and run-loop call sites that already carry the pair.

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        contact_id: Contact FK.

    Returns:
        Enrollment if found, None otherwise.
    """
    row = connection.execute(
        """\
        SELECT
            enrollment.*,
            workflow.name AS workflow_name,
            contact.email AS contact_email,
            TRIM(
                COALESCE(contact.first_name, '')
                || ' '
                || COALESCE(contact.last_name, '')
            ) AS contact_name
        FROM enrollment
        JOIN workflow ON workflow.id = enrollment.workflow_id
        JOIN contact ON contact.id = enrollment.contact_id
        WHERE enrollment.workflow_id = %(workflow_id)s
          AND enrollment.contact_id = %(contact_id)s
        """,
        {"workflow_id": workflow_id, "contact_id": contact_id},
    ).fetchone()
    if row is None:
        return None
    return Enrollment.model_validate(row)


def get_enrollment_by_id(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
) -> Enrollment | None:
    """Get an enrollment by scalar id (§V.12).

    Args:
        connection: Open database connection.
        enrollment_id: Enrollment ID.

    Returns:
        Enrollment if found, None otherwise.
    """
    row = connection.execute(
        """\
        SELECT
            enrollment.*,
            workflow.name AS workflow_name,
            contact.email AS contact_email,
            TRIM(
                COALESCE(contact.first_name, '')
                || ' '
                || COALESCE(contact.last_name, '')
            ) AS contact_name
        FROM enrollment
        JOIN workflow ON workflow.id = enrollment.workflow_id
        JOIN contact ON contact.id = enrollment.contact_id
        WHERE enrollment.id = %(id)s
        """,
        {"id": enrollment_id},
    ).fetchone()
    if row is None:
        return None
    return Enrollment.model_validate(row)


def list_enrollments(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    status: str | None = None,
) -> list[Enrollment]:
    """List enrollments in a workflow with optional status filter.

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        status: Filter by enrollment status.

    Returns:
        List of enrollments.
    """
    params: dict[str, object] = {"workflow_id": workflow_id}
    status_filter = SQL("")
    if status is not None:
        status_filter = SQL("AND enrollment.status = %(status)s")
        params["status"] = status
    query = SQL(
        "SELECT enrollment.*, "
        "workflow.name AS workflow_name, "
        "contact.email AS contact_email, "
        "TRIM(COALESCE(contact.first_name, '') || ' ' "
        "|| COALESCE(contact.last_name, '')) AS contact_name "
        "FROM enrollment "
        "JOIN workflow ON workflow.id = enrollment.workflow_id "
        "JOIN contact ON contact.id = enrollment.contact_id "
        "WHERE enrollment.workflow_id = %(workflow_id)s {} "
        "ORDER BY enrollment.created_at"
    ).format(status_filter)
    rows = connection.execute(query, params).fetchall()
    return [Enrollment.model_validate(row) for row in rows]


def preview_enrollment_tag_cohort(
    connection: psycopg.Connection[dict[str, Any]],
    workflow: Workflow,
    tag: Tag,
    *,
    min_contacts: int | None = None,
    account_email: str | None = None,
) -> EnrollmentPreview:
    """Dry-run company-tag enrollment cohort for one workflow (§V.150).

    Read-only: expands companies carrying ``tag`` (disabled companies excluded
    from candidates but counted), then enabled contacts on those companies.
    Drops already-enrolled contacts for the workflow, self-loop contacts
    (§V.33: contact email matches the workflow account email), and disabled
    contacts. Optional ``min_contacts`` filters companies before expand
    (same contact_count grain as ``list_companies``, incl. disabled children).

    Args:
        connection: Open database connection.
        workflow: Resolved workflow row (name projected into the report).
        tag: Resolved vocabulary tag row.
        min_contacts: Inclusive lower bound on company contact_count.
        account_email: Workflow account email for self-loop exclusion; when
            None, the self-loop branch never fires.

    Returns:
        ``EnrollmentPreview`` with candidate contacts + exclusion counters.
    """
    companies = list_companies(
        connection,
        tag=tag.id,
        min_contacts=min_contacts,
        include_disabled=True,
        limit=100_000,
        sort="domain",
    )
    enrolled_ids = {e.contact_id for e in list_enrollments(connection, workflow.id)}
    account_email_lower = account_email.lower() if account_email is not None else None
    excluded = EnrollmentPreviewExcluded()
    contacts: list[EnrollmentPreviewContact] = []
    for company in companies:
        if company.disabled_reason is not None:
            excluded.disabled_companies += 1
            continue
        for contact in list_contacts(
            connection,
            company_id=company.id,
            include_disabled=True,
            limit=100_000,
        ):
            if contact.disabled_reason is not None:
                excluded.disabled_contacts += 1
                continue
            if (
                account_email_lower is not None
                and contact.email.lower() == account_email_lower
            ):
                excluded.self_loop += 1
                continue
            if contact.id in enrolled_ids:
                excluded.already_enrolled += 1
                continue
            contacts.append(
                EnrollmentPreviewContact(
                    email=contact.email,
                    company_domain=company.domain,
                )
            )
    contacts.sort(key=lambda c: (c.company_domain or "", c.email))
    return EnrollmentPreview(
        workflow=workflow.name,
        tag=tag.name,
        count=len(contacts),
        contacts=contacts,
        excluded=excluded,
    )


def list_enrollments_with_outcomes(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> list[EnrollmentWithOutcome]:
    """List enrollments in a workflow with their latest outcome activity.

    Outcomes (`completed` / `failed`) are timeline-only and do not change
    `enrollment.status` (§V.15). This helper LEFT JOINs the most recent
    `enrollment_completed` / `enrollment_failed` activity per row so the
    agent can answer "has this goal already been satisfied for any
    contact in this workflow?" in a single query.

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.

    Returns:
        List of `EnrollmentWithOutcome`, ordered by enrollment `created_at`.
    """
    rows = connection.execute(
        """\
        SELECT
            e.id,
            e.workflow_id,
            e.contact_id,
            e.status,
            e.reason,
            e.created_at,
            e.updated_at,
            CASE a.type
                WHEN 'enrollment_completed' THEN 'completed'
                WHEN 'enrollment_failed' THEN 'failed'
            END AS latest_outcome,
            COALESCE(a.detail->>'reason', a.summary) AS latest_outcome_reason,
            a.created_at AS latest_outcome_at
        FROM enrollment e
        LEFT JOIN LATERAL (
            SELECT type, summary, detail, created_at
            FROM activity
            WHERE activity.contact_id = e.contact_id
              AND activity.workflow_id = e.workflow_id
              AND activity.type IN ('enrollment_completed', 'enrollment_failed')
            ORDER BY created_at DESC
            LIMIT 1
        ) a ON TRUE
        WHERE e.workflow_id = %(workflow_id)s
        ORDER BY e.created_at
        """,
        {"workflow_id": workflow_id},
    ).fetchall()
    return [EnrollmentWithOutcome.model_validate(row) for row in rows]


def get_latest_enrollment_outcome(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
) -> str | None:
    """Return the enrollment's most recent terminal outcome, else None (§V.83).

    Outcomes are timeline-only (§V.15): the newest ``enrollment_completed`` /
    ``enrollment_failed`` activity for the enrollment is its current outcome.
    Returns ``"completed"`` or ``"failed"`` when one exists, ``None`` when the
    enrollment has no recorded outcome yet.

    The touch pre-flight (§V.83) reads this to cancel a queued follow-up touch
    once the sequence has concluded -- a booked meeting, opt-out, or
    contact-later disposition -- without an LLM call.

    Args:
        connection: Open database connection.
        enrollment_id: Enrollment FK (outcome activities carry it).

    Returns:
        ``"completed"``, ``"failed"``, or ``None``.
    """
    row = connection.execute(
        """\
        SELECT CASE type
            WHEN 'enrollment_completed' THEN 'completed'
            WHEN 'enrollment_failed' THEN 'failed'
        END AS outcome
        FROM activity
        WHERE enrollment_id = %(enrollment_id)s
          AND type IN ('enrollment_completed', 'enrollment_failed')
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        {"enrollment_id": enrollment_id},
    ).fetchone()
    if row is None:
        return None
    outcome = row["outcome"]
    return outcome if isinstance(outcome, str) else None


def list_active_outbound_enrollments_for_contact(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
) -> list[Enrollment]:
    """List a contact's active enrollments in outbound workflows (§V.128).

    The booking-conclusion fan-out feeds on this: a meeting booked by an
    attendee outranks every cold outbound sequence, so the system concludes
    each active outbound enrollment the contact holds (§V.128). Inbound
    enrollments and disabled enrollments are excluded -- a booking concludes
    only live cold sequences.

    Args:
        connection: Open database connection.
        contact_id: Contact FK.

    Returns:
        Active enrollments in outbound workflows for the contact, ordered by
        ``created_at`` (denormalised parent identifiers joined per §V.5).
    """
    rows = connection.execute(
        """\
        SELECT
            enrollment.*,
            workflow.name AS workflow_name,
            contact.email AS contact_email,
            TRIM(
                COALESCE(contact.first_name, '')
                || ' '
                || COALESCE(contact.last_name, '')
            ) AS contact_name
        FROM enrollment
        JOIN workflow ON workflow.id = enrollment.workflow_id
        JOIN contact ON contact.id = enrollment.contact_id
        WHERE enrollment.contact_id = %(contact_id)s
          AND enrollment.status = 'active'
          AND workflow.type = 'outbound'
        ORDER BY enrollment.created_at
        """,
        {"contact_id": contact_id},
    ).fetchall()
    return [Enrollment.model_validate(row) for row in rows]


def record_enrollment_outcome(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
    outcome: str,
    reason: str,
    disposition: str | None = None,
) -> Activity:
    """Record a completed/failed outcome on the enrollment timeline (§V.15).

    System-internal recorder: the outcome is purely an activity-timeline event
    (``enrollment_completed`` / ``enrollment_failed``); the ``enrollment`` row
    status is never modified (§V.15). Both the deterministic booking conclusion
    (§V.128) and the agent terminal route through here.

    When supplied, ``disposition`` is persisted into the activity ``detail``
    JSONB under the ``disposition`` key (§V.132) so the per-campaign funnel can
    split ``failed`` outcomes into ``do_not_contact`` versus ``contact_later``
    and confirm ``completed`` maps to ``meeting_booked``. The key is omitted
    when ``disposition`` is None, so pre-change rows carry no key (legacy gap).

    Args:
        connection: Open database connection.
        enrollment_id: Enrollment ID (scalar).
        outcome: ``"completed"`` or ``"failed"``.
        reason: Explanation inlined into the activity (e.g. ``"meeting booked"``).
        disposition: Terminal disposition (§V.127) in {meeting_booked,
            do_not_contact, contact_later}, or None to write no disposition key.

    Returns:
        The created outcome ``Activity``.

    Raises:
        ValueError: If ``outcome`` is not completed/failed, or the enrollment
            does not exist.
    """
    if outcome not in ("completed", "failed"):
        raise ValueError(f"outcome must be completed or failed, got: {outcome}")
    enrollment = get_enrollment_by_id(connection, enrollment_id)
    if enrollment is None:
        raise ValueError(f"enrollment not found: {enrollment_id}")
    contact = get_contact(connection, enrollment.contact_id)
    detail: dict[str, object] = {"reason": reason}
    if disposition is not None:
        detail["disposition"] = disposition
    return create_activity(
        connection,
        contact_id=enrollment.contact_id,
        activity_type=f"enrollment_{outcome}",
        summary=reason or f"Enrollment {outcome}",
        detail=detail,
        company_id=contact.company_id if contact is not None else None,
        workflow_id=enrollment.workflow_id,
        enrollment_id=enrollment.id,
    )


def disable_enrollment(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
    reason: str,
) -> Enrollment | None:
    """Soft-disable an enrollment via terminal lifecycle exit (§V.10, §V.15).

    Single transaction: flips ``status='disabled'`` + writes ``disabled_reason``,
    then appends an ``enrollment_disabled`` activity carrying the reason. The
    coupling CHECK on ``enrollment`` rejects empty reasons at the schema level;
    callers MUST validate ``reason.strip() != ""`` upstream for a friendlier
    error envelope.

    Returns the updated row with denormalised parent identifiers (workflow
    name, contact email/name) so the CLI envelope can ship the full
    Enrollment model unchanged. Returns ``None`` when no row matches the id.

    Args:
        connection: Open database connection.
        enrollment_id: Enrollment ID.
        reason: Operator-supplied explanation written to ``disabled_reason``
            and inlined into the ``enrollment_disabled`` activity row.

    Returns:
        Updated ``Enrollment`` (status='disabled'), or ``None`` if not found.
    """
    row = connection.execute(
        """\
        WITH updated AS (
            UPDATE enrollment
            SET status = 'disabled',
                disabled_reason = %(reason)s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %(id)s
            RETURNING *
        )
        SELECT
            updated.*,
            workflow.name AS workflow_name,
            contact.email AS contact_email,
            contact.company_id AS contact_company_id,
            TRIM(
                COALESCE(contact.first_name, '')
                || ' '
                || COALESCE(contact.last_name, '')
            ) AS contact_name
        FROM updated
        JOIN workflow ON workflow.id = updated.workflow_id
        JOIN contact ON contact.id = updated.contact_id
        """,
        {"id": enrollment_id, "reason": reason},
    ).fetchone()
    if row is None:
        connection.commit()
        return None
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, workflow_id, enrollment_id,
            type, summary, detail
        )
        VALUES (
            %(id)s, %(contact_id)s, %(company_id)s, %(workflow_id)s,
            %(enrollment_id)s, 'enrollment_disabled', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "contact_id": row["contact_id"],
            "company_id": row["contact_company_id"],
            "workflow_id": row["workflow_id"],
            "enrollment_id": row["id"],
            "summary": reason,
            "detail": Json({"reason": reason}),
        },
    )
    connection.commit()
    row.pop("contact_company_id", None)
    return Enrollment.model_validate(row)


def enable_enrollment(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
) -> Enrollment | None:
    """Re-enable a disabled enrollment: flip ``status`` to ``active``.

    Mirror of ``disable_enrollment`` (§V.15): single transaction flips
    ``status='active'`` + clears ``disabled_reason``, then appends an
    ``enrollment_enabled`` activity. A ``status='disabled'`` gate blocks
    enabling a live enrollment -- an already-active row does not match, so the
    call returns ``None`` and writes no activity.

    Returns the updated row with denormalised parent identifiers (workflow
    name, contact email/name) so the CLI envelope can ship the full Enrollment
    model unchanged.

    Args:
        connection: Open database connection.
        enrollment_id: Enrollment ID.

    Returns:
        Updated ``Enrollment`` (status='active'), or ``None`` when no disabled
        enrollment with that id exists -- i.e. missing or already active.
    """
    row = connection.execute(
        """\
        WITH updated AS (
            UPDATE enrollment
            SET status = 'active',
                disabled_reason = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %(id)s
              AND status = 'disabled'
            RETURNING *
        )
        SELECT
            updated.*,
            workflow.name AS workflow_name,
            contact.email AS contact_email,
            contact.company_id AS contact_company_id,
            TRIM(
                COALESCE(contact.first_name, '')
                || ' '
                || COALESCE(contact.last_name, '')
            ) AS contact_name
        FROM updated
        JOIN workflow ON workflow.id = updated.workflow_id
        JOIN contact ON contact.id = updated.contact_id
        """,
        {"id": enrollment_id},
    ).fetchone()
    if row is None:
        connection.commit()
        return None
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, workflow_id, enrollment_id,
            type, summary, detail
        )
        VALUES (
            %(id)s, %(contact_id)s, %(company_id)s, %(workflow_id)s,
            %(enrollment_id)s, 'enrollment_enabled', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "contact_id": row["contact_id"],
            "company_id": row["contact_company_id"],
            "workflow_id": row["workflow_id"],
            "enrollment_id": row["id"],
            "summary": "Enrollment re-enabled",
            "detail": Json({}),
        },
    )
    connection.commit()
    row.pop("contact_company_id", None)
    return Enrollment.model_validate(row)


def list_enrollments_detailed(  # noqa: C901, PLR0912
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str | None = None,
    contact_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    since: str | None = None,
    until: str | None = None,
    *,
    full: bool = False,
    has_pending_task: bool | None = None,
    touch: int | None = None,
    sort: str = "updated_at",
    desc: bool = False,
    stuck: bool = False,
    first_send_sla_hours: int = 24,
    disposition: str | None = None,
) -> list[EnrollmentSummary]:
    """List enrollments with denormalised contact info as summaries.

    JOINs the contact table to include email and name. Separate from
    ``list_enrollments`` to avoid breaking agent tools which expect
    ``list[Enrollment]``. Both ``workflow_id`` and ``contact_id`` are
    optional independent filters; either or both can be supplied.

    When ``full=True`` (§V.152), also projects company, touch progress,
    next pending task, disposition, and ``created_at``.

    Args:
        connection: Open database connection.
        workflow_id: Optional workflow FK filter.
        contact_id: Optional contact FK filter.
        status: Filter by enrollment status.
        limit: Maximum results.
        since: ISO datetime inclusive lower bound on ``e.updated_at``.
        until: ISO datetime inclusive upper bound on ``e.updated_at``.
        full: When True, denser execution projection (§V.152).
        has_pending_task: When True/False, filter by presence of pending task.
        touch: Filter to enrollments whose next pending touch equals N, or
            (when no pending) whose last sent touch equals N.
        sort: ``updated_at`` (default) or ``next_scheduled_at`` (full path).
        desc: Sort descending when True.
        stuck: When True (§V.155), only stuck enrollments (heuristics below).
        first_send_sla_hours: SLA for never-sent active enrollments (default 24).
        disposition: When set (§V.160), filter by latest terminal disposition
            in {meeting_booked, do_not_contact, contact_later}.

    Returns:
        List of enrollment summaries.
    """
    params: dict[str, object] = {
        "limit": limit,
        "first_send_sla_hours": first_send_sla_hours,
    }
    where_parts: list[Composed | SQL] = []
    if workflow_id is not None:
        where_parts.append(SQL("e.workflow_id = %(workflow_id)s"))
        params["workflow_id"] = workflow_id
    if contact_id is not None:
        where_parts.append(SQL("e.contact_id = %(contact_id)s"))
        params["contact_id"] = contact_id
    if status is not None:
        where_parts.append(SQL("e.status = %(status)s"))
        params["status"] = status
    if since is not None:
        where_parts.append(SQL("e.updated_at >= %(since)s"))
        params["since"] = since
    if until is not None:
        where_parts.append(SQL("e.updated_at <= %(until)s"))
        params["until"] = until
    if disposition is not None:
        params["disposition"] = disposition
        where_parts.append(SQL("outcome.disposition = %(disposition)s"))
    if stuck:
        # Force full joins for stuck heuristics that need next task / bounce.
        full = True
        where_parts.append(
            SQL(
                "("
                # active, no terminal outcome, no pending, never-sent past SLA
                "("
                "e.status = 'active' "
                "AND outcome.disposition IS NULL "
                "AND nt.scheduled_at IS NULL "
                "AND NOT EXISTS ("
                "SELECT 1 FROM email em "
                "WHERE em.workflow_id = e.workflow_id "
                "AND em.contact_id = e.contact_id "
                "AND em.direction = 'outbound' AND em.status = 'sent'"
                ") "
                "AND e.created_at < NOW() "
                "- make_interval(hours => %(first_send_sla_hours)s)"
                ") "
                "OR "
                # bounced without disposition
                "("
                "EXISTS ("
                "SELECT 1 FROM email em "
                "WHERE em.workflow_id = e.workflow_id "
                "AND em.contact_id = e.contact_id "
                "AND em.direction = 'outbound' AND em.status = 'bounced'"
                ") "
                "AND outcome.disposition IS NULL"
                ") "
                "OR "
                # high attempt_count failed task
                "EXISTS ("
                "SELECT 1 FROM task t "
                "WHERE t.enrollment_id = e.id "
                "AND t.status = 'failed' AND t.attempt_count >= 3"
                ")"
                ")"
            )
        )
    if has_pending_task is True:
        where_parts.append(
            SQL(
                "EXISTS ("
                "SELECT 1 FROM task t "
                "WHERE t.enrollment_id = e.id AND t.status = 'pending'"
                ")"
            )
        )
    elif has_pending_task is False:
        where_parts.append(
            SQL(
                "NOT EXISTS ("
                "SELECT 1 FROM task t "
                "WHERE t.enrollment_id = e.id AND t.status = 'pending'"
                ")"
            )
        )
    if touch is not None:
        params["touch"] = touch
        where_parts.append(
            SQL(
                "("
                "EXISTS ("
                "SELECT 1 FROM task t "
                "WHERE t.enrollment_id = e.id AND t.status = 'pending' "
                "AND {touch} = %(touch)s"
                ") "
                "OR ("
                "NOT EXISTS ("
                "SELECT 1 FROM task t "
                "WHERE t.enrollment_id = e.id AND t.status = 'pending'"
                ") "
                "AND ("
                "SELECT COUNT(*)::int FROM email em "
                "WHERE em.workflow_id = e.workflow_id "
                "AND em.contact_id = e.contact_id "
                "AND em.direction = 'outbound' AND em.status = 'sent'"
                ") = %(touch)s"
                ")"
                ")"
            ).format(touch=_sql_parse_touch(SQL("t.context")))
        )
    where_clause = (
        SQL("WHERE ") + SQL(" AND ").join(where_parts) if where_parts else SQL("")
    )
    outcome_lateral = SQL(
        "LEFT JOIN LATERAL ("
        "SELECT a.detail->>'disposition' AS disposition "
        "FROM activity a "
        "WHERE a.contact_id = e.contact_id "
        "AND a.workflow_id = e.workflow_id "
        "AND a.type IN ('enrollment_completed', 'enrollment_failed') "
        "ORDER BY a.created_at DESC LIMIT 1"
        ") outcome ON TRUE "
    )
    if full:
        select_cols = SQL(
            "SELECT e.id, e.workflow_id, w.name AS workflow_name, "
            "e.contact_id, e.status, e.updated_at, e.created_at, "
            "c.email AS contact_email, "
            "TRIM(COALESCE(c.first_name, '') || ' ' || COALESCE(c.last_name, '')) "
            "AS contact_name, "
            "co.domain AS company_domain, "
            "co.name AS company_name, "
            "("
            "SELECT COUNT(*)::int FROM email em "
            "WHERE em.workflow_id = e.workflow_id "
            "AND em.contact_id = e.contact_id "
            "AND em.direction = 'outbound' AND em.status = 'sent'"
            ") AS emails_sent, "
            "("
            "SELECT COUNT(*)::int FROM email em "
            "WHERE em.workflow_id = e.workflow_id "
            "AND em.contact_id = e.contact_id "
            "AND em.direction = 'outbound' AND em.status = 'sent'"
            ") AS last_touch, "
            "nt.scheduled_at AS next_scheduled_at, "
            "{next_touch} AS next_touch, "
            "outcome.disposition AS disposition "
        ).format(next_touch=_sql_parse_touch(SQL("nt.context")))
        from_joins = (
            SQL(
                "FROM enrollment e "
                "JOIN workflow w ON w.id = e.workflow_id "
                "JOIN contact c ON c.id = e.contact_id "
                "LEFT JOIN company co ON co.id = c.company_id "
                "LEFT JOIN LATERAL ("
                "SELECT t.scheduled_at, t.context FROM task t "
                "WHERE t.enrollment_id = e.id AND t.status = 'pending' "
                "ORDER BY t.scheduled_at ASC NULLS LAST LIMIT 1"
                ") nt ON TRUE "
            )
            + outcome_lateral
        )
    else:
        select_cols = SQL(
            "SELECT e.id, e.workflow_id, w.name AS workflow_name, "
            "e.contact_id, e.status, e.updated_at, "
            "c.email AS contact_email, "
            "TRIM(COALESCE(c.first_name, '') || ' ' || COALESCE(c.last_name, '')) "
            "AS contact_name "
        )
        from_joins = SQL(
            "FROM enrollment e "
            "JOIN workflow w ON w.id = e.workflow_id "
            "JOIN contact c ON c.id = e.contact_id "
        )
        # §V.160 disposition filter needs outcome lateral even on lean rows.
        if disposition is not None:
            from_joins = from_joins + outcome_lateral
    order_col = (
        SQL("nt.scheduled_at")
        if full and sort == "next_scheduled_at"
        else SQL("e.updated_at")
    )
    order_dir = SQL("DESC") if desc else SQL("ASC")
    query = (
        select_cols
        + from_joins
        + where_clause
        + SQL(" ORDER BY ")
        + order_col
        + SQL(" ")
        + order_dir
        + SQL(" NULLS LAST LIMIT %(limit)s")
    )
    rows = connection.execute(query, params).fetchall()
    return [EnrollmentSummary.model_validate(row) for row in rows]


# -- Email ---------------------------------------------------------------------


def create_email(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    direction: str,
    subject: str = "",
    body_text: str = "",
    gmail_message_id: str | None = None,
    gmail_thread_id: str | None = None,
    contact_id: str | None = None,
    workflow_id: str | None = None,
    status: str = "received",
    is_routed: bool = False,
    route_method: str | None = None,
    received_at: datetime | None = None,
    sent_at: datetime | None = None,
    labels: list[str] | None = None,
    rfc2822_message_id: str | None = None,
    in_reply_to: str | None = None,
    references_header: str | None = None,
    sender: str = "",
    recipients: dict[str, list[str]] | None = None,
) -> Email | None:
    """Create a new email record, or return None on gmail_message_id conflict.

    Insert is atomic via ``ON CONFLICT (gmail_message_id) DO NOTHING``, so two
    concurrent workers attempting to store the same Gmail message will never
    raise ``UniqueViolation``: one wins and returns the row, the other
    returns ``None``. Outbound rows with ``gmail_message_id=NULL`` never
    trigger the conflict (NULLs are distinct under a UNIQUE constraint).

    Args:
        connection: Open database connection.
        account_id: Account FK.
        direction: "inbound" or "outbound".
        subject: Email subject.
        body_text: Plain text body.
        gmail_message_id: Gmail message ID.
        gmail_thread_id: Gmail thread ID.
        contact_id: Optional contact FK.
        workflow_id: Optional workflow FK.
        status: Email status ("sent" or "received").
        is_routed: Whether the routing pipeline has processed this email.
        route_method: Persisted routing decision (per §I email projection;
            e.g. ``thread_match``, ``classified``, ``skipped_outside_window``).
        received_at: When Gmail reports the message arrived (UTC datetime).
        sent_at: When the outbound message was handed to Gmail (UTC datetime).
        labels: Gmail label IDs attached to the message.
        rfc2822_message_id: RFC 2822 Message-ID header value.
        in_reply_to: RFC 2822 In-Reply-To header value (parent message id).
        references_header: RFC 2822 References header value (full
            whitespace-separated chain of ancestor message ids). Stored as
            ``references_header`` because ``references`` is a reserved SQL
            keyword.
        sender: Sender email address (lowercase).
        recipients: Recipient addresses grouped by type
            (``{"to": [...], "cc": [...], "bcc": [...]}``)

    Returns:
        Created email, or None if another worker already stored a row with
        the same ``gmail_message_id``.
    """
    row = connection.execute(
        """\
        INSERT INTO email (id, account_id, direction, subject,
            body_text, gmail_message_id, gmail_thread_id,
            contact_id, workflow_id, status, is_routed, route_method,
            received_at, sent_at, labels, rfc2822_message_id,
            in_reply_to, references_header,
            sender, recipients)
        VALUES (%(id)s, %(account_id)s, %(direction)s,
            %(subject)s, %(body_text)s, %(gmail_message_id)s,
            %(gmail_thread_id)s, %(contact_id)s, %(workflow_id)s,
            %(status)s, %(is_routed)s, %(route_method)s,
            %(received_at)s, %(sent_at)s,
            %(labels)s, %(rfc2822_message_id)s,
            %(in_reply_to)s, %(references_header)s,
            %(sender)s, %(recipients)s)
        ON CONFLICT (gmail_message_id) DO NOTHING
        RETURNING *
        """,
        {
            "id": _new_id(),
            "account_id": account_id,
            "direction": direction,
            "subject": subject,
            "body_text": body_text,
            "gmail_message_id": gmail_message_id,
            "gmail_thread_id": gmail_thread_id,
            "contact_id": contact_id,
            "workflow_id": workflow_id,
            "status": status,
            "is_routed": is_routed,
            "route_method": route_method,
            "received_at": received_at,
            "sent_at": sent_at,
            "labels": Json(labels or []),
            "rfc2822_message_id": rfc2822_message_id,
            "in_reply_to": in_reply_to,
            "references_header": references_header,
            "sender": sender,
            "recipients": Json(recipients or {}),
        },
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Email.model_validate(row)


def get_email(
    connection: psycopg.Connection[dict[str, Any]],
    email_id: str,
) -> Email | None:
    """Get an email by ID.

    Args:
        connection: Open database connection.
        email_id: Email ID.

    Returns:
        Email if found, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM email WHERE id = %(id)s",
        {"id": email_id},
    ).fetchone()
    if row is None:
        return None
    return Email.model_validate(row)


def list_emails(
    connection: psycopg.Connection[dict[str, Any]],
    limit: int = 100,
    contact_id: str | None = None,
    account_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    thread_id: str | None = None,
    direction: str | None = None,
    workflow_id: str | None = None,
    status: str | None = None,
    sender: str | None = None,
    recipient: str | None = None,
    route_method: str | None = None,
) -> list[EmailSummary]:
    """List emails as summaries with optional filters.

    Args:
        connection: Open database connection.
        limit: Maximum results.
        contact_id: Filter by contact ID.
        account_id: Filter by account ID.
        since: ISO datetime inclusive lower bound for
            ``COALESCE(sent_at, received_at)``.
        until: ISO datetime inclusive upper bound for
            ``COALESCE(sent_at, received_at)``.
        thread_id: Filter by Gmail thread ID.
        direction: Filter by direction ("inbound" or "outbound").
        workflow_id: Filter by workflow ID.
        status: Filter by email status ("sent", "received", "bounced").
        sender: Filter by sender email address (case-insensitive).
        recipient: Filter by recipient address in recipients JSONB
            (case-insensitive, matches to/cc/bcc).
        route_method: Filter by persisted routing decision (per
            §I email projection).

    Returns:
        List of email summaries ordered by ``COALESCE(sent_at, received_at)``
        descending -- the same expression used by the ``since`` filter, so
        operators can page newest-first using a timestamp visible in
        ``EmailSummary``.
    """
    conditions: list[Composable] = []
    params: dict[str, object] = {"limit": limit}
    # Simple equality filters keyed (param_name -> (column_name, value)).
    equality_filters: dict[str, tuple[str, str | None]] = {
        "contact_id": ("contact_id", contact_id),
        "account_id": ("account_id", account_id),
        "thread_id": ("gmail_thread_id", thread_id),
        "direction": ("direction", direction),
        "workflow_id": ("workflow_id", workflow_id),
        "status": ("status", status),
        "route_method": ("route_method", route_method),
    }
    for param_name, (column, value) in equality_filters.items():
        if value is None:
            continue
        conditions.append(
            SQL("{} = {}").format(Identifier(column), Placeholder(param_name))
        )
        params[param_name] = value
    if since is not None:
        conditions.append(SQL("COALESCE(sent_at, received_at) >= %(since)s"))
        params["since"] = since
    if until is not None:
        conditions.append(SQL("COALESCE(sent_at, received_at) <= %(until)s"))
        params["until"] = until
    if sender is not None:
        conditions.append(SQL("LOWER(sender) = LOWER(%(sender)s)"))
        params["sender"] = sender
    if recipient is not None:
        conditions.append(
            SQL("LOWER(recipients::text) LIKE LOWER(%(recipient_pattern)s)")
        )
        params["recipient_pattern"] = f"%{recipient}%"
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT id, account_id, contact_id, workflow_id, direction, "
        "subject, sender, recipients, status, is_routed, route_method, "
        "gmail_thread_id, sent_at, received_at "
        "FROM email {} "
        "ORDER BY COALESCE(sent_at, received_at) DESC LIMIT %(limit)s"
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [EmailSummary.model_validate(row) for row in rows]


def search_emails(
    connection: psycopg.Connection[dict[str, Any]],
    query: str,
    limit: int = 100,
    account_id: str | None = None,
) -> list[EmailSummary]:
    """Search emails by subject, body text, sender, or recipients.

    Args:
        connection: Open database connection.
        query: Search term.
        limit: Maximum number of results.
        account_id: Filter by account ID.

    Returns:
        Matching email summaries ordered by creation time descending.
    """
    pattern = f"%{query}%"
    params: dict[str, object] = {"pattern": pattern, "limit": limit}
    account_filter = SQL("")
    if account_id is not None:
        account_filter = SQL("AND account_id = %(account_id)s")
        params["account_id"] = account_id
    query_sql = SQL(
        "SELECT id, account_id, contact_id, workflow_id, direction, "
        "subject, sender, recipients, status, is_routed, route_method, "
        "gmail_thread_id, sent_at, received_at "
        "FROM email "
        "WHERE (LOWER(subject) LIKE LOWER(%(pattern)s) "
        "   OR LOWER(body_text) LIKE LOWER(%(pattern)s) "
        "   OR LOWER(sender) LIKE LOWER(%(pattern)s) "
        "   OR LOWER(recipients::text) LIKE LOWER(%(pattern)s)) "
        "{} "
        "ORDER BY created_at DESC "
        "LIMIT %(limit)s"
    ).format(account_filter)
    rows = connection.execute(query_sql, params).fetchall()
    return [EmailSummary.model_validate(row) for row in rows]


def get_email_by_gmail_message_id(
    connection: psycopg.Connection[dict[str, Any]],
    gmail_message_id: str,
) -> Email | None:
    """Get an email by Gmail message ID.

    Args:
        connection: Open database connection.
        gmail_message_id: Gmail message ID (unique).

    Returns:
        Email if found, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM email WHERE gmail_message_id = %(gmail_message_id)s",
        {"gmail_message_id": gmail_message_id},
    ).fetchone()
    if row is None:
        return None
    return Email.model_validate(row)


def get_emails_by_gmail_thread_id(
    connection: psycopg.Connection[dict[str, Any]],
    gmail_thread_id: str,
) -> list[Email]:
    """Get all emails in a Gmail thread.

    Args:
        connection: Open database connection.
        gmail_thread_id: Gmail thread ID.

    Returns:
        Emails in the thread ordered by creation time.
    """
    rows = connection.execute(
        """\
        SELECT * FROM email
        WHERE gmail_thread_id = %(gmail_thread_id)s
        ORDER BY created_at
        """,
        {"gmail_thread_id": gmail_thread_id},
    ).fetchall()
    return [Email.model_validate(row) for row in rows]


def has_inbound_email_from_contact_after(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
    after: datetime,
) -> bool:
    """Return True if the contact sent an inbound email after ``after`` (§V.83).

    The touch pre-flight (§V.83) reads this to cancel a queued follow-up touch
    when the contact has replied since the prior touch -- an engaged contact
    must not receive the next cold touch. Complements the reply-time
    cancellation (§V.123) by catching the touch already due when the reply
    landed. Compares against the arrival timestamp (``received_at``, with
    ``sent_at`` as a fallback for any row lacking it).

    Args:
        connection: Open database connection.
        contact_id: Contact FK (set on inbound rows via sender resolution).
        after: The prior touch's send moment -- only later inbound counts.

    Returns:
        True when at least one such inbound email exists.
    """
    row = connection.execute(
        """\
        SELECT 1 FROM email
        WHERE contact_id = %(contact_id)s
          AND direction = 'inbound'
          AND COALESCE(received_at, sent_at) > %(after)s
        LIMIT 1
        """,
        {"contact_id": contact_id, "after": after},
    ).fetchone()
    return row is not None


def get_latest_email_in_thread(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    gmail_thread_id: str,
) -> Email | None:
    """Get the most recently created email in a Gmail thread for an account.

    Used when sending a reply into an existing thread to pull the prior
    message's ``rfc2822_message_id`` for the outgoing ``In-Reply-To`` /
    ``References`` headers. Scoping by ``account_id`` keeps the lookup
    deterministic when the same Gmail thread ID is observed on multiple
    delegated mailboxes (e.g. sender + recipient on the same domain).

    Args:
        connection: Open database connection.
        account_id: Account FK.
        gmail_thread_id: Gmail thread ID.

    Returns:
        Most recently created email in the thread, or None if the thread
        has no rows for this account.
    """
    row = connection.execute(
        """\
        SELECT * FROM email
        WHERE account_id = %(account_id)s
          AND gmail_thread_id = %(gmail_thread_id)s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"account_id": account_id, "gmail_thread_id": gmail_thread_id},
    ).fetchone()
    if row is None:
        return None
    return Email.model_validate(row)


def find_email_by_rfc2822_message_id(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    message_ids: list[str],
) -> Email | None:
    """Find the most recent email matching any of the given RFC 2822 message ids.

    Used by the routing pipeline as a fallback when ``gmail_thread_id`` no
    longer joins inbound replies to their outbound parents (Gmail re-threads
    on the recipient side, producing a different ``threadId`` for the same
    conversation). Restricted to a single ``account_id`` so cross-account
    collisions on a shared Message-ID cannot leak workflow assignments.

    Args:
        connection: Open database connection.
        account_id: Account scope -- only rows belonging to this account
            are considered.
        message_ids: Candidate RFC 2822 Message-ID values, typically the
            inbound email's ``In-Reply-To`` plus every entry in its
            ``References`` chain.

    Returns:
        The most-recently created matching email, or ``None`` when no row
        in this account stores any of ``message_ids`` in its
        ``rfc2822_message_id`` column.
    """
    if not message_ids:
        return None
    row = connection.execute(
        """\
        SELECT * FROM email
        WHERE account_id = %(account_id)s
          AND rfc2822_message_id = ANY(%(message_ids)s)
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"account_id": account_id, "message_ids": message_ids},
    ).fetchone()
    if row is None:
        return None
    return Email.model_validate(row)


def get_last_cold_outbound(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    contact_id: str,
    workflow_id: str,
) -> Email | None:
    """Get the most recent cold outbound email to a contact within a workflow.

    A cold outbound email is the first outbound message in its Gmail
    thread (no prior outbound in the same thread). This distinguishes
    initial outreach from follow-up replies within an existing
    conversation. Used by the ``send_email`` agent tool for cooldown
    enforcement. Scoped to a single workflow so that independent
    campaigns can each send their first outreach independently.

    Args:
        connection: Open database connection.
        account_id: Sending account.
        contact_id: Recipient contact.
        workflow_id: Workflow scope for cooldown.

    Returns:
        Most recent cold outbound email, or None if none exists.
    """
    row = connection.execute(
        """\
        SELECT e.* FROM email e
        WHERE e.account_id = %(account_id)s
          AND e.contact_id = %(contact_id)s
          AND e.workflow_id = %(workflow_id)s
          AND e.direction = 'outbound'
          AND NOT EXISTS (
              SELECT 1 FROM email prior
              WHERE prior.gmail_thread_id = e.gmail_thread_id
                AND prior.gmail_thread_id IS NOT NULL
                AND prior.account_id = e.account_id
                AND prior.direction = 'outbound'
                AND prior.created_at < e.created_at
          )
        ORDER BY e.created_at DESC
        LIMIT 1
        """,
        {
            "account_id": account_id,
            "contact_id": contact_id,
            "workflow_id": workflow_id,
        },
    ).fetchone()
    if row is None:
        return None
    return Email.model_validate(row)


def update_email(
    connection: psycopg.Connection[dict[str, Any]],
    email_id: str,
    **fields: object,
) -> Email | None:
    """Update an email by ID.

    Args:
        connection: Open database connection.
        email_id: Email ID.
        **fields: Fields to update (must be valid Email field names).

    Returns:
        Updated email, or None if not found.
    """
    allowed = {
        "workflow_id",
        "is_routed",
        "route_method",
        "status",
        "contact_id",
        "rfc2822_message_id",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_email(connection, email_id)
    updates["id"] = email_id
    # email table has no updated_at column -- use raw SQL instead of _build_update
    set_parts = [
        SQL("{} = {}").format(Identifier(k), Placeholder(k))
        for k in updates
        if k != "id"
    ]
    set_clause = SQL(", ").join(set_parts)
    query = SQL("UPDATE email SET {} WHERE id = %(id)s RETURNING *").format(set_clause)
    row = connection.execute(query, updates).fetchone()
    connection.commit()
    if row is None:
        return None
    return Email.model_validate(row)


# -- Task ----------------------------------------------------------------------


def create_task(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
    workflow_id: str,
    contact_id: str,
    description: str,
    scheduled_at: str,
    context: dict[str, object] | None = None,
    email_id: str | None = None,
) -> Task:
    """Create a deferred task.

    Per §V.28: every task row belongs to an enrollment. ``enrollment_id``
    is NOT NULL at the schema level; callers resolve it from the
    ``(workflow_id, contact_id)`` UNIQUE pair before invoking this fn.
    The denormalised ``workflow_id`` + ``contact_id`` columns stay for
    filter-path and dashboard compat.

    Args:
        connection: Open database connection.
        enrollment_id: Enrollment FK (NOT NULL per schema).
        workflow_id: Workflow FK (denormalised from enrollment row).
        contact_id: Contact FK (denormalised from enrollment row).
        description: What the agent should do.
        scheduled_at: When to execute (ISO timestamp).
        context: Arbitrary JSON context for the agent.
        email_id: Optional triggering email FK.

    Returns:
        Created task.
    """
    row = connection.execute(
        """\
        INSERT INTO task (id, enrollment_id, workflow_id, contact_id, email_id,
            description, context, scheduled_at)
        VALUES (%(id)s, %(enrollment_id)s, %(workflow_id)s, %(contact_id)s,
                %(email_id)s, %(description)s, %(context)s, %(scheduled_at)s)
        RETURNING *
        """,
        {
            "id": _new_id(),
            "enrollment_id": enrollment_id,
            "workflow_id": workflow_id,
            "contact_id": contact_id,
            "email_id": email_id,
            "description": description,
            "context": Json(context or {}),
            "scheduled_at": scheduled_at,
        },
    ).fetchone()
    connection.commit()
    return Task.model_validate(row)


def get_task(
    connection: psycopg.Connection[dict[str, Any]],
    task_id: str,
) -> Task | None:
    """Get a task by ID.

    Args:
        connection: Open database connection.
        task_id: Task ID.

    Returns:
        Task if found, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM task WHERE id = %(id)s",
        {"id": task_id},
    ).fetchone()
    if row is None:
        return None
    return Task.model_validate(row)


def list_pending_tasks(
    connection: psycopg.Connection[dict[str, Any]],
) -> list[Task]:
    """List tasks due for execution.

    Args:
        connection: Open database connection.

    Returns:
        Pending tasks where scheduled_at <= now(), ordered by scheduled_at.
    """
    rows = connection.execute(
        """\
        SELECT * FROM task
        WHERE scheduled_at <= CURRENT_TIMESTAMP AND status = 'pending'
        ORDER BY scheduled_at
        """
    ).fetchall()
    return [Task.model_validate(row) for row in rows]


def find_pending_first_touch_task(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
) -> Task | None:
    """Return a pending first-touch task for ``enrollment_id`` if any.

    A first-touch task is the CLI-scheduled initial outbound send per §V.32:
    ``email_id IS NULL`` (not tied to a triggering inbound email) and
    ``status='pending'`` (not yet drained, not cancelled, not failed). Used
    by ``mailpilot enrollment add --scheduled-at ...`` to skip a duplicate
    insert when the operator re-runs against an enrollment that already has
    one queued. Keyed on scalar ``enrollment_id`` per §V.32 post-migration.
    """
    row = connection.execute(
        """\
        SELECT * FROM task
        WHERE enrollment_id = %(enrollment_id)s
          AND email_id IS NULL
          AND status = 'pending'
        ORDER BY scheduled_at
        LIMIT 1
        """,
        {"enrollment_id": enrollment_id},
    ).fetchone()
    if row is None:
        return None
    return Task.model_validate(row)


def complete_task(
    connection: psycopg.Connection[dict[str, Any]],
    task_id: str,
    status: str = "completed",
    result: dict[str, object] | None = None,
) -> Task | None:
    """Mark a task as completed or failed, optionally storing a result.

    Args:
        connection: Open database connection.
        task_id: Task ID.
        status: "completed" or "failed".
        result: Agent reasoning and outcome to persist.

    Returns:
        Updated task, or None if not found.
    """
    result_json = result or {}
    row = connection.execute(
        """\
        UPDATE task
        SET status = %(status)s,
            result = %(result)s,
            completed_at = CURRENT_TIMESTAMP
        WHERE id = %(id)s RETURNING *
        """,
        {
            "id": task_id,
            "status": status,
            "result": Json(result_json),
        },
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Task.model_validate(row)


def cancel_task(
    connection: psycopg.Connection[dict[str, Any]],
    task_id: str,
) -> Task | None:
    """Cancel a pending task.

    Only cancels tasks with status 'pending'. Already completed or failed
    tasks are not affected.

    Args:
        connection: Open database connection.
        task_id: Task ID.

    Returns:
        Cancelled task, or None if not found or not pending.
    """
    row = connection.execute(
        """\
        UPDATE task SET status = 'cancelled', completed_at = CURRENT_TIMESTAMP
        WHERE id = %(id)s AND status = 'pending'
        RETURNING *
        """,
        {"id": task_id},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Task.model_validate(row)


def cancel_enrollment_followup_tasks(
    connection: psycopg.Connection[dict[str, Any]],
    enrollment_id: str,
) -> list[Task]:
    """Cancel an enrollment's pending future follow-up tasks (§V.123).

    Bulk-cancels every ``pending`` task for ``enrollment_id`` whose
    ``scheduled_at`` is still in the future, excluding the operator
    first-touch task (the row carrying ``context->>'trigger' =
    'enrollment_schedule'`` per §V.32). Called from ``routing.route_email``
    when an inbound reply routes to the enrollment: the prospect engaged,
    so any later cold follow-up touch is cancelled before it wakes.

    Already-due tasks (``scheduled_at <= now``) and non-pending tasks are
    left untouched; status moves ``pending`` -> ``cancelled`` (mirrors
    ``cancel_task``). Agent-created follow-ups may carry a NULL
    ``email_id``, so the first-touch is identified by the trigger label,
    not by ``email_id``.

    Args:
        connection: Open database connection.
        enrollment_id: Enrollment whose follow-up tasks to cancel.

    Returns:
        The cancelled tasks, ordered by scheduled_at; empty when none matched.
    """
    rows = connection.execute(
        """\
        UPDATE task SET status = 'cancelled', completed_at = CURRENT_TIMESTAMP
        WHERE enrollment_id = %(enrollment_id)s
          AND status = 'pending'
          AND scheduled_at > CURRENT_TIMESTAMP
          AND COALESCE(context->>'trigger', '') <> 'enrollment_schedule'
        RETURNING *
        """,
        {"enrollment_id": enrollment_id},
    ).fetchall()
    connection.commit()
    return [Task.model_validate(row) for row in rows]


def list_tasks(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str | None = None,
    contact_id: str | None = None,
    status: str | None = None,
    trigger: str | None = None,
    limit: int = 100,
    since: str | None = None,
    until: str | None = None,
    *,
    overdue: bool = False,
) -> list[TaskSummary]:
    """List tasks as summaries with optional filters.

    Args:
        connection: Open database connection.
        workflow_id: Filter by workflow ID.
        contact_id: Filter by contact ID.
        status: Filter by task status.
        trigger: Filter by caller path stored in ``context->>'trigger'``
            (§V.26 taxonomy); deterministic first-touch select on
            ``enrollment_schedule`` (§V.32), never reads ``description``.
        limit: Maximum results.
        since: ISO datetime inclusive lower bound on ``scheduled_at``.
        until: ISO datetime inclusive upper bound on ``scheduled_at``.
        overdue: When True (§V.155), only pending tasks with
            ``scheduled_at < now()``.

    Returns:
        List of task summaries ordered by scheduled_at descending.
    """
    conditions: list[SQL] = []
    params: dict[str, object] = {"limit": limit}
    if workflow_id is not None:
        conditions.append(SQL("workflow_id = %(workflow_id)s"))
        params["workflow_id"] = workflow_id
    if contact_id is not None:
        conditions.append(SQL("contact_id = %(contact_id)s"))
        params["contact_id"] = contact_id
    if overdue:
        conditions.append(SQL("status = 'pending'"))
        conditions.append(SQL("scheduled_at < NOW()"))
    if status is not None:
        conditions.append(SQL("status = %(status)s"))
        params["status"] = status
    if trigger is not None:
        conditions.append(SQL("COALESCE(context->>'trigger', '') = %(trigger)s"))
        params["trigger"] = trigger
    if since is not None:
        conditions.append(SQL("scheduled_at >= %(since)s"))
        params["since"] = since
    if until is not None:
        conditions.append(SQL("scheduled_at <= %(until)s"))
        params["until"] = until
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT id, enrollment_id, workflow_id, contact_id, email_id, "
        "description, scheduled_at, status, attempt_count "
        "FROM task {} ORDER BY scheduled_at DESC LIMIT %(limit)s"
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [TaskSummary.model_validate(row) for row in rows]


def get_task_stats(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str | None = None,
    trigger: str | None = None,
    bucket_tz: str = "UTC",
) -> TaskStats:
    """Compute the task-cadence aggregate over the task queue (§V.133).

    A single deterministic SQL aggregate at task grain -- no LLM. Returns
    per-status counts {pending, completed, failed, cancelled} plus ``total``,
    the count of distinct calendar days the tasks are scheduled across (bucketed
    in ``bucket_tz``), and the first/last ``scheduled_at``. Optional
    ``workflow_id`` and ``trigger`` narrow the task set before aggregation, the
    same filter axes ``list_tasks`` carries.

    ``distinct_scheduled_days`` buckets each ``scheduled_at`` (a ``TIMESTAMPTZ``)
    into its wall-clock date in ``bucket_tz`` -- ``AT TIME ZONE`` shifts the
    instant into that zone before truncating to a date, so a midnight-straddling
    instant lands on the operator's local day, not UTC's. The window filters
    (§V.115 lifecycle) stay on ``list_tasks``; this aggregate keeps to the
    cadence question.

    Args:
        connection: Open database connection.
        workflow_id: Filter by workflow ID (entity ref per §V.107).
        trigger: Filter by ``context->>'trigger'`` (§V.26 taxonomy);
            ``enrollment_schedule`` selects the first-touch tasks (§V.32).
        bucket_tz: IANA timezone name for day-bucketing ``distinct_scheduled_days``
            (caller validates; an unknown zone raises at query time).

    Returns:
        ``TaskStats`` over the filtered task set (all-zero counts and NULL
        first/last when no task matches).
    """
    conditions: list[SQL] = []
    params: dict[str, object] = {"bucket_tz": bucket_tz}
    if workflow_id is not None:
        conditions.append(SQL("workflow_id = %(workflow_id)s"))
        params["workflow_id"] = workflow_id
    if trigger is not None:
        conditions.append(SQL("COALESCE(context->>'trigger', '') = %(trigger)s"))
        params["trigger"] = trigger
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        """\
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE status = 'pending') AS pending,
            COUNT(*) FILTER (WHERE status = 'completed') AS completed,
            COUNT(*) FILTER (WHERE status = 'failed') AS failed,
            COUNT(*) FILTER (WHERE status = 'cancelled') AS cancelled,
            COUNT(DISTINCT (scheduled_at AT TIME ZONE %(bucket_tz)s)::date)
                AS distinct_scheduled_days,
            MIN(scheduled_at) AS first_scheduled_at,
            MAX(scheduled_at) AS last_scheduled_at
        FROM task {}
        """
    ).format(where)
    row = connection.execute(query, params).fetchone()
    assert row is not None  # an aggregate without GROUP BY always returns one row
    return TaskStats.model_validate(row)


def reschedule_task_for_retry(
    connection: psycopg.Connection[dict[str, Any]],
    task_id: str,
    backoff_seconds: int,
    exc: BaseException,
) -> Task | None:
    """Reschedule a transient-failure task for another attempt.

    Status remains ``pending``. ``attempt_count`` is incremented and
    ``scheduled_at`` is advanced by ``backoff_seconds``. The row's
    ``result`` JSON captures a summary of the last failure so an
    operator inspecting the row mid-retry-loop sees what's been tried.

    Args:
        connection: Open database connection.
        task_id: Task ID.
        backoff_seconds: Delay to add before the next attempt fires.
        exc: Exception from the failed attempt; used to populate the
            ``result.last_error`` summary.

    Returns:
        Updated task, or ``None`` if the row does not exist.
    """
    summary = {
        "last_error": {
            "type": type(exc).__name__,
            "message": str(exc),
        },
    }
    row = connection.execute(
        """\
        UPDATE task
        SET attempt_count = attempt_count + 1,
            scheduled_at = CURRENT_TIMESTAMP + (%(delay)s || ' seconds')::interval,
            result = %(result)s
        WHERE id = %(id)s
        RETURNING *
        """,
        {
            "id": task_id,
            "delay": str(backoff_seconds),
            "result": Json(summary),
        },
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Task.model_validate(row)


def reschedule_task_for_lock_contention(
    connection: psycopg.Connection[dict[str, Any]],
    task_id: str,
    backoff_seconds: int,
) -> Task | None:
    """Push ``scheduled_at`` forward without bumping ``attempt_count``.

    Used when the agent advisory lock was held by another worker (§V.25).
    Lock contention is not a retry: the task ran nothing, side-effect
    budget is untouched, ``attempt_count`` stays put. Bumping
    ``scheduled_at`` fires the ``task_pending_trigger`` ``UPDATE`` notify
    (§V.49 trigger extension) so the drain loop wakes again instead of
    leaving the task ``pending`` with no signal (§B.42).

    Args:
        connection: Open database connection.
        task_id: Task ID.
        backoff_seconds: Delay before the next attempt fires.

    Returns:
        Updated task, or ``None`` if the row does not exist.
    """
    row = connection.execute(
        """\
        UPDATE task
        SET scheduled_at = CURRENT_TIMESTAMP + (%(delay)s || ' seconds')::interval
        WHERE id = %(id)s
        RETURNING *
        """,
        {
            "id": task_id,
            "delay": str(backoff_seconds),
        },
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Task.model_validate(row)


def manual_retry_task(
    connection: psycopg.Connection[dict[str, Any]],
    task_id: str,
) -> Task | None:
    """Reset a terminal task row for a fresh retry, operator-initiated.

    Allowed only on rows with status ``failed`` or ``cancelled``.
    Refuses ``completed`` rows (tools already fired - retry would
    duplicate side-effects) and ``pending`` rows (already queued, no-op).

    Resets ``status='pending'``, ``attempt_count=0``, ``scheduled_at=now()``,
    and clears ``completed_at``. The row's ``UPDATE`` of ``status`` and
    ``scheduled_at`` fires ``pg_notify('task_pending')`` via
    ``task_pending_trigger`` so the run loop wakes immediately.

    Args:
        connection: Open database connection.
        task_id: Task ID.

    Returns:
        Reset task, or ``None`` if the row does not exist or is not in
        a retryable state.
    """
    row = connection.execute(
        """\
        UPDATE task
        SET status = 'pending',
            attempt_count = 0,
            scheduled_at = CURRENT_TIMESTAMP,
            completed_at = NULL
        WHERE id = %(id)s AND status IN ('failed', 'cancelled')
        RETURNING *
        """,
        {"id": task_id},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Task.model_validate(row)


def get_unprocessed_inbound_email(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> Email | None:
    """Return the most recent inbound email for a contact+workflow without a task.

    Uses the same filtering logic as ``create_tasks_for_routed_emails`` but
    scoped to a single contact and returning at most one email.

    Args:
        connection: Open database connection.
        workflow_id: Workflow FK.
        contact_id: Contact FK.

    Returns:
        The most recent unprocessed inbound email, or None.
    """
    row = connection.execute(
        """\
        SELECT e.* FROM email e
        JOIN workflow w ON w.id = e.workflow_id
        WHERE e.direction = 'inbound'
          AND e.workflow_id = %(workflow_id)s
          AND e.contact_id = %(contact_id)s
          AND e.created_at >= w.created_at
          AND NOT EXISTS (SELECT 1 FROM task t WHERE t.email_id = e.id)
        ORDER BY e.created_at DESC
        LIMIT 1
        """,
        {"workflow_id": workflow_id, "contact_id": contact_id},
    ).fetchone()
    if row is None:
        return None
    return Email.model_validate(row)


def create_tasks_for_routed_emails(
    connection: psycopg.Connection[dict[str, Any]],
) -> list[Task]:
    """Create immediate tasks for routed inbound emails without tasks.

    Finds inbound emails with workflow_id set but no corresponding task
    row, and creates a task with scheduled_at=now() for each. Joins
    ``enrollment`` so ``task.enrollment_id`` is populated per §V.28 --
    the enrollment row is guaranteed present because
    ``routing._ensure_enrollment`` runs earlier in the inbound pipeline.

    Uses ``e.created_at`` (DB insert time) rather than ``e.received_at``
    (Gmail timestamp) to filter historical emails. An email can be received
    by Gmail before a workflow exists but synced into our DB after -- using
    ``received_at`` would incorrectly skip such emails.

    Args:
        connection: Open database connection.

    Returns:
        List of newly created tasks.
    """
    unmatched = connection.execute(
        """\
        SELECT e.id, e.workflow_id, e.contact_id, en.id AS enrollment_id
        FROM email e
        JOIN workflow w ON w.id = e.workflow_id
        JOIN enrollment en
          ON en.workflow_id = e.workflow_id AND en.contact_id = e.contact_id
        WHERE e.direction = 'inbound'
          AND e.contact_id IS NOT NULL
          AND e.created_at >= w.created_at
          AND NOT EXISTS (SELECT 1 FROM task t WHERE t.email_id = e.id)
        ORDER BY e.created_at
        """
    ).fetchall()
    tasks: list[Task] = []
    for email_row in unmatched:
        now = datetime.now(UTC).isoformat()
        t = create_task(
            connection,
            enrollment_id=email_row["enrollment_id"],
            workflow_id=email_row["workflow_id"],
            contact_id=email_row["contact_id"],
            description="handle inbound email",
            scheduled_at=now,
            email_id=email_row["id"],
        )
        tasks.append(t)
    return tasks


# -- Activity ------------------------------------------------------------------


def create_activity(
    connection: psycopg.Connection[dict[str, Any]],
    activity_type: str,
    summary: str = "",
    detail: dict[str, object] | None = None,
    contact_id: str | None = None,
    company_id: str | None = None,
    email_id: str | None = None,
    workflow_id: str | None = None,
    task_id: str | None = None,
    enrollment_id: str | None = None,
) -> Activity:
    """Create an activity event.

    At least one of ``contact_id`` or ``company_id`` must be set.
    Structured FK columns (``email_id``, ``workflow_id``, ``task_id``,
    ``enrollment_id``) let reports join activity to source records without
    parsing ``detail`` JSON. ``enrollment_id`` is nullable -- non-enrollment
    activity types (``email_sent``, ``note_added``, etc.) leave it null;
    enrollment-lifecycle types (``enrollment_added`` / ``enrollment_completed``
    / etc.) populate it.

    Raises:
        ValueError: If neither contact_id nor company_id is provided.
    """
    if contact_id is None and company_id is None:
        raise ValueError("at least one of contact_id or company_id is required")
    row = connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, email_id, workflow_id, task_id,
            enrollment_id, type, summary, detail
        )
        VALUES (
            %(id)s, %(contact_id)s, %(company_id)s, %(email_id)s,
            %(workflow_id)s, %(task_id)s, %(enrollment_id)s,
            %(type)s, %(summary)s, %(detail)s
        )
        RETURNING *
        """,
        {
            "id": _new_id(),
            "contact_id": contact_id,
            "company_id": company_id,
            "email_id": email_id,
            "workflow_id": workflow_id,
            "task_id": task_id,
            "enrollment_id": enrollment_id,
            "type": activity_type,
            "summary": summary,
            "detail": Json(detail or {}),
        },
    ).fetchone()
    connection.commit()
    return Activity.model_validate(row)


def list_activities(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str | None = None,
    company_id: str | None = None,
    activity_type: str | None = None,
    limit: int = 100,
    since: str | None = None,
    until: str | None = None,
    workflow_id: str | None = None,
) -> list[ActivitySummary]:
    """List activities as summaries with required scope filter (§V.154).

    At least one of ``contact_id``, ``company_id``, or ``workflow_id`` must be
    provided.

    Args:
        connection: Open database connection.
        contact_id: Filter by contact ID.
        company_id: Filter by company ID.
        activity_type: Filter by activity type.
        limit: Maximum number of results.
        since: ISO datetime inclusive lower bound for created_at.
        until: ISO datetime inclusive upper bound for created_at.
        workflow_id: Filter by workflow ID (campaign timeline).

    Returns:
        Activity summaries ordered by created_at descending.

    Raises:
        ValueError: If no scope filter is provided.
    """
    if contact_id is None and company_id is None and workflow_id is None:
        raise ValueError(
            "at least one of contact_id, company_id, or workflow_id is required"
        )
    conditions: list[SQL] = []
    params: dict[str, object] = {"limit": limit}
    if contact_id is not None:
        conditions.append(SQL("contact_id = %(contact_id)s"))
        params["contact_id"] = contact_id
    if company_id is not None:
        conditions.append(SQL("company_id = %(company_id)s"))
        params["company_id"] = company_id
    if workflow_id is not None:
        conditions.append(SQL("workflow_id = %(workflow_id)s"))
        params["workflow_id"] = workflow_id
    if activity_type is not None:
        conditions.append(SQL("type = %(activity_type)s"))
        params["activity_type"] = activity_type
    if since is not None:
        conditions.append(SQL("created_at >= %(since)s"))
        params["since"] = since
    if until is not None:
        conditions.append(SQL("created_at <= %(until)s"))
        params["until"] = until
    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT id, contact_id, company_id, email_id, workflow_id, task_id, "
        "enrollment_id, type, summary, created_at "
        "FROM activity {} ORDER BY created_at DESC LIMIT %(limit)s"
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [ActivitySummary.model_validate(row) for row in rows]


# -- Tag -----------------------------------------------------------------------


_TAG_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


def _normalize_tag_name(name: str) -> str:
    """Normalize a tag name to lowercase hyphenated form.

    Strips whitespace, lowercases, replaces whitespace and underscores
    with hyphens, collapses repeated hyphens, trims leading/trailing
    hyphens, and validates against ``[a-z0-9][a-z0-9-]*``.

    Raises:
        ValueError: If the result is empty or contains disallowed
        characters.
    """
    cleaned = name.strip().lower()
    cleaned = re.sub(r"[\s_]+", "-", cleaned)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    if not _TAG_NAME_RE.fullmatch(cleaned):
        raise ValueError(f"invalid tag name: {name!r} (normalized to {cleaned!r})")
    return cleaned


def create_tag(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
) -> Tag | None:
    """Define a tag in the controlled vocabulary (§V.116).

    The name is normalized via ``_normalize_tag_name`` then inserted into the
    ``tag`` vocabulary table. ``name`` is globally unique (§V.90), so a
    second create of the same name uses ON CONFLICT DO NOTHING and returns
    ``None`` (the caller surfaces ``already_exists``).

    Raises:
        ValueError: If the tag name fails normalization.
    """
    normalized = _normalize_tag_name(name)
    row = connection.execute(
        """\
        INSERT INTO tag (id, name)
        VALUES (%(id)s, %(name)s)
        ON CONFLICT (name) DO NOTHING
        RETURNING *
        """,
        {"id": _new_id(), "name": normalized},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Tag.model_validate(row)


def get_tag(
    connection: psycopg.Connection[dict[str, Any]],
    tag_id: str,
) -> Tag | None:
    """Resolve a vocabulary tag by id (§V.107 polymorphic UUID branch).

    Operators address tags by name; this id branch keeps a tag id round-trip
    from one command's output into the next. Returns ``None`` when absent.
    """
    row = connection.execute("SELECT * FROM tag WHERE id = %s", (tag_id,)).fetchone()
    if row is None:
        return None
    return Tag.model_validate(row)


def get_tag_by_name(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
) -> Tag | None:
    """Resolve a vocabulary tag by its globally unique name (§V.90/§V.107).

    The name is normalized first. Returns ``None`` when no tag is defined --
    the caller surfaces ``not_found``. Used to resolve ``--tag <name>`` to a
    tag id without the operator pasting ids.

    Raises:
        ValueError: If the tag name fails normalization.
    """
    normalized = _normalize_tag_name(name)
    row = connection.execute(
        "SELECT * FROM tag WHERE name = %s", (normalized,)
    ).fetchone()
    if row is None:
        return None
    return Tag.model_validate(row)


def get_tag_summary_by_name(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
) -> TagSummary | None:
    """Resolve a vocabulary tag by name with its ``usage_count`` (§V.116).

    Backs ``tag view``: the vocabulary row plus the number of owners carrying
    it (``tag_assignment`` count). Returns ``None`` when no tag is defined.

    Raises:
        ValueError: If the tag name fails normalization.
    """
    normalized = _normalize_tag_name(name)
    row = connection.execute(
        """\
        SELECT t.id, t.name, t.disabled_reason, t.created_at,
               COUNT(a.id) AS usage_count
        FROM tag t
        LEFT JOIN tag_assignment a ON a.tag_id = t.id
        WHERE t.name = %s
        GROUP BY t.id
        """,
        (normalized,),
    ).fetchone()
    if row is None:
        return None
    return TagSummary.model_validate(row)


def list_tags(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str | None = None,
    company_id: str | None = None,
    limit: int = 100,
    since: str | None = None,
    until: str | None = None,
    include_disabled: bool = False,
) -> list[TagSummary]:
    """List vocabulary tags with a projected ``usage_count`` (§V.116).

    Owner-free (neither ``contact_id`` nor ``company_id``): returns the whole
    vocabulary. With an owner: returns only the tags assigned to that contact
    or company. ``usage_count`` is always the tag's global assignment count, so
    the owner-scoped view still reports how widely each tag is used.

    Disabled vocabulary rows (``disabled_reason IS NOT NULL``) are excluded by
    default; pass ``include_disabled=True`` to include them (§V.10). ``since`` /
    ``until`` bound the vocabulary row's ``created_at``.

    Raises:
        ValueError: If both ``contact_id`` and ``company_id`` are set.
    """
    if contact_id is not None and company_id is not None:
        raise ValueError("at most one of contact_id or company_id may be set")
    params: dict[str, object] = {"limit": limit}
    conditions: list[Composed | SQL] = []
    if since is not None:
        conditions.append(SQL("t.created_at >= %(since)s"))
        params["since"] = since
    if until is not None:
        conditions.append(SQL("t.created_at <= %(until)s"))
        params["until"] = until
    if not include_disabled:
        conditions.append(SQL("t.disabled_reason IS NULL"))

    owner_join = SQL("")
    if contact_id is not None:
        owner_join = SQL(
            "JOIN tag_assignment owner ON owner.tag_id = t.id "
            "AND owner.contact_id = %(contact_id)s"
        )
        params["contact_id"] = contact_id
    elif company_id is not None:
        owner_join = SQL(
            "JOIN tag_assignment owner ON owner.tag_id = t.id "
            "AND owner.company_id = %(company_id)s"
        )
        params["company_id"] = company_id

    where = SQL("WHERE ") + SQL(" AND ").join(conditions) if conditions else SQL("")
    query = SQL(
        "SELECT t.id, t.name, t.disabled_reason, t.created_at, "
        "(SELECT COUNT(*) FROM tag_assignment a WHERE a.tag_id = t.id) "
        "AS usage_count "
        "FROM tag t {owner_join} {where} "
        "ORDER BY t.name LIMIT %(limit)s"
    ).format(owner_join=owner_join, where=where)
    rows = connection.execute(query, params).fetchall()
    return [TagSummary.model_validate(row) for row in rows]


def search_tags(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    limit: int = 100,
    include_disabled: bool = False,
) -> list[TagSummary]:
    """Search the vocabulary by name substring, with ``usage_count`` (§V.116).

    Substring/fuzzy matching is the ``search`` verb's job (§V.115); ``tag
    list`` does exact/owner filtering. Disabled rows are excluded by default.

    Args:
        connection: Open database connection.
        name: Substring to LIKE-match against the vocabulary ``name``.
        limit: Maximum number of results.
        include_disabled: When ``True`` include disabled rows.
    """
    pattern = f"%{name.strip().lower()}%"
    params: dict[str, object] = {"pattern": pattern, "limit": limit}
    disabled_filter = (
        SQL("") if include_disabled else SQL("AND t.disabled_reason IS NULL")
    )
    query = SQL(
        "SELECT t.id, t.name, t.disabled_reason, t.created_at, "
        "(SELECT COUNT(*) FROM tag_assignment a WHERE a.tag_id = t.id) "
        "AS usage_count "
        "FROM tag t WHERE t.name LIKE %(pattern)s {disabled_filter} "
        "ORDER BY t.name LIMIT %(limit)s"
    ).format(disabled_filter=disabled_filter)
    rows = connection.execute(query, params).fetchall()
    return [TagSummary.model_validate(row) for row in rows]


def disable_tag(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    reason: str,
) -> Tag | None:
    """Soft-retire a vocabulary tag (§V.10/§V.116).

    Flips ``disabled_reason`` on the matching active vocabulary row
    (``disabled_reason IS NULL`` gate blocks double-disable). A retired tag
    stays linked to its owners but drops out of the default ``tag list``.
    Returns ``None`` when no active tag with the name exists (undefined or
    already disabled) -- the caller distinguishes the two. This is a vocabulary
    lifecycle, not a per-owner event, so it writes no activity (an activity
    needs a contact or company owner, §V.17).

    Raises:
        ValueError: If the tag name fails normalization.
    """
    normalized = _normalize_tag_name(name)
    row = connection.execute(
        """\
        UPDATE tag
        SET disabled_reason = %(reason)s
        WHERE name = %(name)s AND disabled_reason IS NULL
        RETURNING *
        """,
        {"name": normalized, "reason": reason},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Tag.model_validate(row)


def enable_tag(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
) -> Tag | None:
    """Re-enable a retired vocabulary tag by clearing ``disabled_reason``.

    Mirror of ``disable_tag``. A ``disabled_reason IS NOT NULL`` gate blocks
    enabling an already-active tag, so the call returns ``None`` when no
    disabled tag with the name exists (undefined or already active) -- the
    caller distinguishes the two. Being owner-free, the vocabulary lifecycle
    writes no activity (an activity needs a contact or company owner).

    Raises:
        ValueError: If the tag name fails normalization.
    """
    normalized = _normalize_tag_name(name)
    row = connection.execute(
        """\
        UPDATE tag
        SET disabled_reason = NULL
        WHERE name = %(name)s AND disabled_reason IS NOT NULL
        RETURNING *
        """,
        {"name": normalized},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Tag.model_validate(row)


def assign_tag_to_contact(
    connection: psycopg.Connection[dict[str, Any]],
    tag_id: str,
    contact_id: str,
) -> TagAssignment | None:
    """Link a vocabulary tag to a contact and emit ``tag_added`` (§V.91/§V.116).

    The assignment INSERT and the ``tag_added`` activity commit in one
    transaction (§V.91). Returns ``None`` if the link already exists (ON
    CONFLICT DO NOTHING) -- no activity is written in that case. The activity
    carries the contact's company so it surfaces on the company timeline too
    (§V.17 multi-target).

    Raises:
        ValueError: If the contact does not exist.
    """
    contact_row = connection.execute(
        "SELECT company_id FROM contact WHERE id = %s", (contact_id,)
    ).fetchone()
    if contact_row is None:
        raise ValueError(f"contact not found: {contact_id}")
    tag_row = connection.execute(
        "SELECT name FROM tag WHERE id = %s", (tag_id,)
    ).fetchone()
    if tag_row is None:
        raise ValueError(f"tag not found: {tag_id}")
    assignment_row = connection.execute(
        """\
        INSERT INTO tag_assignment (id, tag_id, contact_id, company_id)
        VALUES (%(id)s, %(tag_id)s, %(contact_id)s, NULL)
        ON CONFLICT DO NOTHING
        RETURNING *
        """,
        {"id": _new_id(), "tag_id": tag_id, "contact_id": contact_id},
    ).fetchone()
    if assignment_row is None:
        connection.commit()
        return None
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, type, summary, detail
        )
        VALUES (
            %(id)s, %(contact_id)s, %(company_id)s,
            'tag_added', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "contact_id": contact_id,
            "company_id": contact_row["company_id"],
            "summary": f"Tagged as {tag_row['name']}",
            "detail": Json({"tag": tag_row["name"]}),
        },
    )
    connection.commit()
    return TagAssignment.model_validate(assignment_row)


def assign_tag_to_company(
    connection: psycopg.Connection[dict[str, Any]],
    tag_id: str,
    company_id: str,
) -> TagAssignment | None:
    """Link a vocabulary tag to a company and emit ``tag_added`` (§V.91/§V.116).

    Mirrors ``assign_tag_to_contact``. Returns ``None`` if the link already
    exists.

    Raises:
        ValueError: If the company does not exist.
    """
    if (
        connection.execute(
            "SELECT 1 FROM company WHERE id = %s", (company_id,)
        ).fetchone()
        is None
    ):
        raise ValueError(f"company not found: {company_id}")
    tag_row = connection.execute(
        "SELECT name FROM tag WHERE id = %s", (tag_id,)
    ).fetchone()
    if tag_row is None:
        raise ValueError(f"tag not found: {tag_id}")
    assignment_row = connection.execute(
        """\
        INSERT INTO tag_assignment (id, tag_id, contact_id, company_id)
        VALUES (%(id)s, %(tag_id)s, NULL, %(company_id)s)
        ON CONFLICT DO NOTHING
        RETURNING *
        """,
        {"id": _new_id(), "tag_id": tag_id, "company_id": company_id},
    ).fetchone()
    if assignment_row is None:
        connection.commit()
        return None
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, type, summary, detail
        )
        VALUES (
            %(id)s, NULL, %(company_id)s,
            'tag_added', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "company_id": company_id,
            "summary": f"Tagged as {tag_row['name']}",
            "detail": Json({"tag": tag_row["name"]}),
        },
    )
    connection.commit()
    return TagAssignment.model_validate(assignment_row)


def remove_tag_from_contact(
    connection: psycopg.Connection[dict[str, Any]],
    tag_id: str,
    contact_id: str,
) -> TagAssignment | None:
    """Unlink a vocabulary tag from a contact and emit ``tag_removed`` (§V.116).

    Inverse of ``assign_tag_to_contact``: deletes the link and appends a
    ``tag_removed`` activity in one transaction (§V.91), retiring neither the
    tag vocabulary nor the contact. Returns ``None`` when no such link exists
    (the caller surfaces ``not_found``).

    Raises:
        ValueError: If the contact does not exist.
    """
    contact_row = connection.execute(
        "SELECT company_id FROM contact WHERE id = %s", (contact_id,)
    ).fetchone()
    if contact_row is None:
        raise ValueError(f"contact not found: {contact_id}")
    deleted_row = connection.execute(
        """\
        DELETE FROM tag_assignment
        WHERE tag_id = %(tag_id)s AND contact_id = %(contact_id)s
        RETURNING *
        """,
        {"tag_id": tag_id, "contact_id": contact_id},
    ).fetchone()
    if deleted_row is None:
        connection.commit()
        return None
    tag_row = connection.execute(
        "SELECT name FROM tag WHERE id = %s", (tag_id,)
    ).fetchone()
    tag_name = tag_row["name"] if tag_row is not None else tag_id
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, type, summary, detail
        )
        VALUES (
            %(id)s, %(contact_id)s, %(company_id)s,
            'tag_removed', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "contact_id": contact_id,
            "company_id": contact_row["company_id"],
            "summary": f"Untagged {tag_name}",
            "detail": Json({"tag": tag_name}),
        },
    )
    connection.commit()
    return TagAssignment.model_validate(deleted_row)


def remove_tag_from_company(
    connection: psycopg.Connection[dict[str, Any]],
    tag_id: str,
    company_id: str,
) -> TagAssignment | None:
    """Unlink a vocabulary tag from a company and emit ``tag_removed`` (§V.116).

    Mirrors ``remove_tag_from_contact``. Returns ``None`` when no such link
    exists.

    Raises:
        ValueError: If the company does not exist.
    """
    if (
        connection.execute(
            "SELECT 1 FROM company WHERE id = %s", (company_id,)
        ).fetchone()
        is None
    ):
        raise ValueError(f"company not found: {company_id}")
    deleted_row = connection.execute(
        """\
        DELETE FROM tag_assignment
        WHERE tag_id = %(tag_id)s AND company_id = %(company_id)s
        RETURNING *
        """,
        {"tag_id": tag_id, "company_id": company_id},
    ).fetchone()
    if deleted_row is None:
        connection.commit()
        return None
    tag_row = connection.execute(
        "SELECT name FROM tag WHERE id = %s", (tag_id,)
    ).fetchone()
    tag_name = tag_row["name"] if tag_row is not None else tag_id
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, type, summary, detail
        )
        VALUES (
            %(id)s, NULL, %(company_id)s,
            'tag_removed', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "company_id": company_id,
            "summary": f"Untagged {tag_name}",
            "detail": Json({"tag": tag_name}),
        },
    )
    connection.commit()
    return TagAssignment.model_validate(deleted_row)


def _tag_names_by_id(
    connection: psycopg.Connection[dict[str, Any]],
    tag_ids: Sequence[str],
) -> dict[str, str]:
    """Map tag ids to vocabulary names; missing ids are omitted."""
    if not tag_ids:
        return {}
    rows = connection.execute(
        "SELECT id, name FROM tag WHERE id = ANY(%s)",
        (list(tag_ids),),
    ).fetchall()
    return {str(row["id"]): str(row["name"]) for row in rows}


def set_company_tags(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
    tag_ids: Sequence[str],
) -> list[str]:
    """Replace a company's full tag assignment set in one transaction (§V.141).

    Adds missing links, removes extras, and writes ``tag_added`` /
    ``tag_removed`` activity per change (§V.14). Empty ``tag_ids`` clears every
    assignment. Caller must pre-resolve vocabulary names so undefined tags never
    reach this function (zero partial writes on undefined). Returns the final
    assigned tag names sorted.

    Raises:
        ValueError: If the company does not exist, or a ``tag_id`` is unknown.
    """
    if (
        connection.execute(
            "SELECT 1 FROM company WHERE id = %s", (company_id,)
        ).fetchone()
        is None
    ):
        raise ValueError(f"company not found: {company_id}")
    # Preserve first-seen order while dropping duplicates.
    desired_ids = list(dict.fromkeys(tag_ids))
    name_by_id = _tag_names_by_id(connection, desired_ids)
    missing = [tid for tid in desired_ids if tid not in name_by_id]
    if missing:
        raise ValueError(f"tag not found: {missing[0]}")
    current_rows = connection.execute(
        "SELECT tag_id FROM tag_assignment WHERE company_id = %s",
        (company_id,),
    ).fetchall()
    current_ids = {str(row["tag_id"]) for row in current_rows}
    desired_set = set(desired_ids)
    to_add = [tid for tid in desired_ids if tid not in current_ids]
    to_remove = sorted(current_ids - desired_set)
    if to_remove:
        remove_names = _tag_names_by_id(connection, to_remove)
        for tag_id in to_remove:
            connection.execute(
                """\
                DELETE FROM tag_assignment
                WHERE tag_id = %(tag_id)s AND company_id = %(company_id)s
                """,
                {"tag_id": tag_id, "company_id": company_id},
            )
            tag_name = remove_names.get(tag_id, tag_id)
            connection.execute(
                """\
                INSERT INTO activity (
                    id, contact_id, company_id, type, summary, detail
                )
                VALUES (
                    %(id)s, NULL, %(company_id)s,
                    'tag_removed', %(summary)s, %(detail)s
                )
                """,
                {
                    "id": _new_id(),
                    "company_id": company_id,
                    "summary": f"Untagged {tag_name}",
                    "detail": Json({"tag": tag_name}),
                },
            )
    for tag_id in to_add:
        tag_name = name_by_id[tag_id]
        connection.execute(
            """\
            INSERT INTO tag_assignment (id, tag_id, contact_id, company_id)
            VALUES (%(id)s, %(tag_id)s, NULL, %(company_id)s)
            ON CONFLICT DO NOTHING
            """,
            {"id": _new_id(), "tag_id": tag_id, "company_id": company_id},
        )
        connection.execute(
            """\
            INSERT INTO activity (
                id, contact_id, company_id, type, summary, detail
            )
            VALUES (
                %(id)s, NULL, %(company_id)s,
                'tag_added', %(summary)s, %(detail)s
            )
            """,
            {
                "id": _new_id(),
                "company_id": company_id,
                "summary": f"Tagged as {tag_name}",
                "detail": Json({"tag": tag_name}),
            },
        )
    connection.commit()
    final_names = [
        t.name
        for t in list_tags(
            connection,
            company_id=company_id,
            limit=1_000_000,
            include_disabled=True,
        )
    ]
    return final_names


def set_contact_tags(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
    tag_ids: Sequence[str],
) -> list[str]:
    """Replace a contact's full tag assignment set in one transaction (§V.141).

    Mirrors ``set_company_tags``: add missing, remove extras, activity per
    change (§V.14), empty ``tag_ids`` clears. Returns final tag names sorted.

    Raises:
        ValueError: If the contact does not exist, or a ``tag_id`` is unknown.
    """
    contact_row = connection.execute(
        "SELECT company_id FROM contact WHERE id = %s", (contact_id,)
    ).fetchone()
    if contact_row is None:
        raise ValueError(f"contact not found: {contact_id}")
    parent_company_id = contact_row["company_id"]
    desired_ids = list(dict.fromkeys(tag_ids))
    name_by_id = _tag_names_by_id(connection, desired_ids)
    missing = [tid for tid in desired_ids if tid not in name_by_id]
    if missing:
        raise ValueError(f"tag not found: {missing[0]}")
    current_rows = connection.execute(
        "SELECT tag_id FROM tag_assignment WHERE contact_id = %s",
        (contact_id,),
    ).fetchall()
    current_ids = {str(row["tag_id"]) for row in current_rows}
    desired_set = set(desired_ids)
    to_add = [tid for tid in desired_ids if tid not in current_ids]
    to_remove = sorted(current_ids - desired_set)
    if to_remove:
        remove_names = _tag_names_by_id(connection, to_remove)
        for tag_id in to_remove:
            connection.execute(
                """\
                DELETE FROM tag_assignment
                WHERE tag_id = %(tag_id)s AND contact_id = %(contact_id)s
                """,
                {"tag_id": tag_id, "contact_id": contact_id},
            )
            tag_name = remove_names.get(tag_id, tag_id)
            connection.execute(
                """\
                INSERT INTO activity (
                    id, contact_id, company_id, type, summary, detail
                )
                VALUES (
                    %(id)s, %(contact_id)s, %(company_id)s,
                    'tag_removed', %(summary)s, %(detail)s
                )
                """,
                {
                    "id": _new_id(),
                    "contact_id": contact_id,
                    "company_id": parent_company_id,
                    "summary": f"Untagged {tag_name}",
                    "detail": Json({"tag": tag_name}),
                },
            )
    for tag_id in to_add:
        tag_name = name_by_id[tag_id]
        connection.execute(
            """\
            INSERT INTO tag_assignment (id, tag_id, contact_id, company_id)
            VALUES (%(id)s, %(tag_id)s, %(contact_id)s, NULL)
            ON CONFLICT DO NOTHING
            """,
            {"id": _new_id(), "tag_id": tag_id, "contact_id": contact_id},
        )
        connection.execute(
            """\
            INSERT INTO activity (
                id, contact_id, company_id, type, summary, detail
            )
            VALUES (
                %(id)s, %(contact_id)s, %(company_id)s,
                'tag_added', %(summary)s, %(detail)s
            )
            """,
            {
                "id": _new_id(),
                "contact_id": contact_id,
                "company_id": parent_company_id,
                "summary": f"Tagged as {tag_name}",
                "detail": Json({"tag": tag_name}),
            },
        )
    connection.commit()
    return [
        t.name
        for t in list_tags(
            connection,
            contact_id=contact_id,
            limit=1_000_000,
            include_disabled=True,
        )
    ]


# -- Note ----------------------------------------------------------------------


def create_note(
    connection: psycopg.Connection[dict[str, Any]],
    body: str,
    contact_id: str | None = None,
    company_id: str | None = None,
) -> Note:
    """Create a freeform note on a contact or company.

    Raises:
        ValueError: If neither or both of contact_id/company_id are set.
    """
    if (contact_id is None) == (company_id is None):
        raise ValueError("exactly one of contact_id or company_id is required")
    row = connection.execute(
        """\
        INSERT INTO note (id, contact_id, company_id, body)
        VALUES (%(id)s, %(contact_id)s, %(company_id)s, %(body)s)
        RETURNING *
        """,
        {
            "id": _new_id(),
            "contact_id": contact_id,
            "company_id": company_id,
            "body": body,
        },
    ).fetchone()
    connection.commit()
    return Note.model_validate(row)


def list_notes(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str | None = None,
    company_id: str | None = None,
    limit: int = 100,
    since: str | None = None,
    until: str | None = None,
) -> list[NoteSummary]:
    """List notes on a contact or company as summaries with body previews.

    The full body is replaced by ``body_preview`` -- the first 80 characters
    with a trailing ellipsis when the body is longer.

    Raises:
        ValueError: If neither or both of contact_id/company_id are set.
    """
    if (contact_id is None) == (company_id is None):
        raise ValueError("exactly one of contact_id or company_id is required")
    params: dict[str, object] = {"limit": limit}
    where_parts: list[Composed | SQL] = []
    if contact_id is not None:
        where_parts.append(SQL("contact_id = %(contact_id)s"))
        params["contact_id"] = contact_id
    else:
        where_parts.append(SQL("company_id = %(company_id)s"))
        params["company_id"] = company_id
    if since is not None:
        where_parts.append(SQL("created_at >= %(since)s"))
        params["since"] = since
    if until is not None:
        where_parts.append(SQL("created_at <= %(until)s"))
        params["until"] = until
    where = SQL("WHERE ") + SQL(" AND ").join(where_parts)
    query = SQL(
        "SELECT id, contact_id, company_id, "
        "CASE WHEN LENGTH(body) > 80 THEN LEFT(body, 80) || '...' "
        "ELSE body END AS body_preview, "
        "created_at "
        "FROM note {} ORDER BY created_at DESC LIMIT %(limit)s"
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [NoteSummary.model_validate(row) for row in rows]


def get_note(
    connection: psycopg.Connection[dict[str, Any]],
    note_id: str,
) -> Note | None:
    """Get a note by ID.

    Args:
        connection: Open database connection.
        note_id: Note ID.

    Returns:
        Note if found, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM note WHERE id = %(id)s",
        {"id": note_id},
    ).fetchone()
    if row is None:
        return None
    return Note.model_validate(row)


def add_contact_note(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
    body: str,
) -> Note:
    """Add a note to a contact and emit a `note_added` activity atomically."""
    contact_row = connection.execute(
        "SELECT company_id FROM contact WHERE id = %s", (contact_id,)
    ).fetchone()
    if contact_row is None:
        raise ValueError(f"contact not found: {contact_id}")
    note_row = connection.execute(
        """\
        INSERT INTO note (id, contact_id, company_id, body)
        VALUES (%(id)s, %(contact_id)s, NULL, %(body)s)
        RETURNING *
        """,
        {"id": _new_id(), "contact_id": contact_id, "body": body},
    ).fetchone()
    note = Note.model_validate(note_row)
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, type, summary, detail
        )
        VALUES (
            %(id)s, %(contact_id)s, %(company_id)s,
            'note_added', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "contact_id": contact_id,
            "company_id": contact_row["company_id"],
            "summary": "Note added",
            "detail": Json({"note_id": note.id}),
        },
    )
    connection.commit()
    return note


def add_company_note(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
    body: str,
) -> Note:
    """Add a note to a company and emit a `note_added` company activity atomically."""
    if (
        connection.execute(
            "SELECT 1 FROM company WHERE id = %s", (company_id,)
        ).fetchone()
        is None
    ):
        raise ValueError(f"company not found: {company_id}")
    note_row = connection.execute(
        """\
        INSERT INTO note (id, contact_id, company_id, body)
        VALUES (%(id)s, NULL, %(company_id)s, %(body)s)
        RETURNING *
        """,
        {"id": _new_id(), "company_id": company_id, "body": body},
    ).fetchone()
    note = Note.model_validate(note_row)
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, type, summary, detail
        )
        VALUES (
            %(id)s, NULL, %(company_id)s,
            'note_added', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "company_id": company_id,
            "summary": "Note added",
            "detail": Json({"note_id": note.id}),
        },
    )
    connection.commit()
    return note


def delete_note(
    connection: psycopg.Connection[dict[str, Any]],
    note_id: str,
) -> bool:
    """Delete a single note by id; return whether a row was deleted.

    Single-id hard-delete path per §V.14 -- removes one `note` row only, never
    its owner's other notes. The `note_added` activity trail stays append-only
    and intact (§V.91). Operator-only, never an agent tool.

    Args:
        connection: Open database connection.
        note_id: Note ID to delete.

    Returns:
        True if a note row was deleted, False if no note had that id.
    """
    cursor = connection.execute("DELETE FROM note WHERE id = %(id)s", {"id": note_id})
    connection.commit()
    return cursor.rowcount > 0


def delete_notes(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str | None = None,
    company_id: str | None = None,
) -> list[str]:
    """Delete every note owned by a contact or company; return deleted ids.

    Owner-bulk hard-delete path per §V.14 -- one transaction, note rows only.
    The `note_added` activity trail stays append-only and intact. Zero notes
    is a no-op returning ``[]``. Operator-only, never an agent tool.

    Args:
        connection: Open database connection.
        contact_id: Delete all notes on this contact (XOR with company_id).
        company_id: Delete all notes on this company (XOR with contact_id).

    Returns:
        Deleted note ids (order undefined / DB-dependent), empty when none.

    Raises:
        ValueError: If neither or both of contact_id/company_id are set.
    """
    if (contact_id is None) == (company_id is None):
        raise ValueError("exactly one of contact_id or company_id is required")
    if contact_id is not None:
        rows = connection.execute(
            "DELETE FROM note WHERE contact_id = %(id)s RETURNING id",
            {"id": contact_id},
        ).fetchall()
    else:
        rows = connection.execute(
            "DELETE FROM note WHERE company_id = %(id)s RETURNING id",
            {"id": company_id},
        ).fetchall()
    connection.commit()
    return [str(row["id"]) for row in rows]


# -- Meeting -------------------------------------------------------------------


def create_meeting(
    connection: psycopg.Connection[dict[str, Any]],
    google_event_id: str | None = None,
    meet_url: str | None = None,
    summary: str = "",
    scheduled_at: datetime | None = None,
    ends_at: datetime | None = None,
    status: str = "scheduled",
) -> Meeting | None:
    """Create a meeting row, or return None on google_event_id conflict (§V.125).

    Insert is atomic via ``ON CONFLICT (google_event_id) DO NOTHING`` so a
    repeat ingest of the same calendar event never raises ``UniqueViolation``:
    one insert wins and returns the row, a racing duplicate returns ``None``
    (mirrors ``create_email`` §V.90). Rows with ``google_event_id=NULL`` never
    trigger the conflict (NULLs are distinct under a UNIQUE constraint).

    Args:
        connection: Open database connection.
        google_event_id: Google Calendar event id (nullable-unique ingest key).
        meet_url: Google Meet join URL.
        summary: Event summary/title.
        scheduled_at: Event start time (UTC datetime).
        ends_at: Event end time (UTC datetime).
        status: Meeting status (``scheduled``/``completed``/``cancelled``/
            ``no_show``); operator record-keeping only, gates nothing (§V.125).

    Returns:
        Created meeting, or None if a row with the same ``google_event_id``
        already exists.
    """
    row = connection.execute(
        """\
        INSERT INTO meeting (id, google_event_id, meet_url, summary,
            scheduled_at, ends_at, status)
        VALUES (%(id)s, %(google_event_id)s, %(meet_url)s, %(summary)s,
            %(scheduled_at)s, %(ends_at)s, %(status)s)
        ON CONFLICT (google_event_id) DO NOTHING
        RETURNING *
        """,
        {
            "id": _new_id(),
            "google_event_id": google_event_id,
            "meet_url": meet_url,
            "summary": summary,
            "scheduled_at": scheduled_at,
            "ends_at": ends_at,
            "status": status,
        },
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Meeting.model_validate(row)


def get_meeting(
    connection: psycopg.Connection[dict[str, Any]],
    meeting_id: str,
) -> Meeting | None:
    """Get a meeting by ID.

    Args:
        connection: Open database connection.
        meeting_id: Meeting ID.

    Returns:
        Meeting if found, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM meeting WHERE id = %(id)s",
        {"id": meeting_id},
    ).fetchone()
    if row is None:
        return None
    return Meeting.model_validate(row)


def get_meeting_by_google_event_id(
    connection: psycopg.Connection[dict[str, Any]],
    google_event_id: str,
) -> Meeting | None:
    """Resolve a meeting by its Google Calendar event id (§V.125).

    The idempotent-ingest lookup key (mirrors
    ``get_email_by_gmail_message_id`` §V.90). Returns ``None`` when no row
    carries the event id yet.
    """
    row = connection.execute(
        "SELECT * FROM meeting WHERE google_event_id = %(google_event_id)s",
        {"google_event_id": google_event_id},
    ).fetchone()
    if row is None:
        return None
    return Meeting.model_validate(row)


def list_meetings(
    connection: psycopg.Connection[dict[str, Any]],
    limit: int = 100,
    contact_id: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[MeetingSummary]:
    """List meetings, newest scheduled first, with optional filters (§V.125).

    Each summary carries ``attendee_emails`` + ``attendee_count`` (child
    aggregate over ``meeting_attendee`` joined to ``contact``, mirroring
    ``contact_count`` §V.96) via a single LATERAL join, so a
    ``--contact-email``-scoped result names who attends without a per-row
    attendee probe (§V.8, §B.112).

    Args:
        connection: Open database connection.
        limit: Maximum rows to return.
        contact_id: Scope to meetings linking this attendee contact (join over
            ``meeting_attendee``).
        status: Filter by meeting status.
        since: Lower bound (inclusive) on ``scheduled_at`` (ISO 8601).
        until: Upper bound (inclusive) on ``scheduled_at`` (ISO 8601).

    Returns:
        Matching meetings ordered by ``scheduled_at`` DESC NULLS LAST, each
        carrying its attendee summary.
    """
    clauses: list[Composable] = []
    params: dict[str, Any] = {"limit": limit}
    if contact_id is not None:
        clauses.append(
            SQL(
                "EXISTS (SELECT 1 FROM meeting_attendee ma "
                "WHERE ma.meeting_id = m.id AND ma.contact_id = %(contact_id)s)"
            )
        )
        params["contact_id"] = contact_id
    if status is not None:
        clauses.append(SQL("m.status = %(status)s"))
        params["status"] = status
    if since is not None:
        clauses.append(SQL("m.scheduled_at >= %(since)s"))
        params["since"] = since
    if until is not None:
        clauses.append(SQL("m.scheduled_at <= %(until)s"))
        params["until"] = until
    where = SQL("")
    if clauses:
        where = SQL("WHERE ") + SQL(" AND ").join(clauses)
    query = (
        SQL(
            "SELECT m.*, "
            "COALESCE(att.emails, ARRAY[]::text[]) AS attendee_emails, "
            "COALESCE(att.cnt, 0) AS attendee_count "
            "FROM meeting m "
            "LEFT JOIN LATERAL ("
            "SELECT array_agg(ct.email ORDER BY ct.email) AS emails, "
            "COUNT(*) AS cnt "
            "FROM meeting_attendee ma "
            "JOIN contact ct ON ct.id = ma.contact_id "
            "WHERE ma.meeting_id = m.id"
            ") att ON TRUE "
        )
        + where
        + SQL(
            " ORDER BY m.scheduled_at DESC NULLS LAST, m.created_at DESC "
            "LIMIT %(limit)s"
        )
    )
    rows = connection.execute(query, params).fetchall()
    return [MeetingSummary.model_validate(row) for row in rows]


def update_meeting(
    connection: psycopg.Connection[dict[str, Any]],
    meeting_id: str,
    **fields: object,
) -> Meeting | None:
    """Update a meeting by ID (§V.125).

    Only ``summary`` and ``status`` are operator-editable -- the ingest-owned
    columns (``google_event_id``, ``meet_url``, ``scheduled_at``, ``ends_at``)
    are refreshed by CalendarClient re-poll (§V.126), never edited from the CLI.
    ``status`` is record-keeping only and gates nothing (§V.125). Non-allowed
    fields are silently dropped; an empty update returns the row unchanged.

    Args:
        connection: Open database connection.
        meeting_id: Meeting ID.
        **fields: Fields to update (only ``summary`` / ``status`` honoured).

    Returns:
        Updated meeting, or None if not found.
    """
    allowed = {"summary", "status"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_meeting(connection, meeting_id)
    updates["id"] = meeting_id
    query = _build_update("meeting", updates, SQL("id = %(id)s"))
    row = connection.execute(query, updates).fetchone()
    connection.commit()
    if row is None:
        return None
    return Meeting.model_validate(row)


def upsert_meeting(
    connection: psycopg.Connection[dict[str, Any]],
    google_event_id: str,
    meet_url: str | None = None,
    summary: str = "",
    scheduled_at: datetime | None = None,
    ends_at: datetime | None = None,
    status: str = "scheduled",
) -> Meeting:
    """Insert or update a meeting keyed on google_event_id (§V.125, idempotent).

    Re-polling the same calendar event updates the existing row in place rather
    than creating a duplicate (``ON CONFLICT (google_event_id) DO UPDATE``,
    mirrors ``upsert_sync_status``). The ingest key is required here -- a
    NULL-keyed meeting cannot be idempotently upserted, so callers without an
    event id use ``create_meeting``. ``updated_at`` is bumped on every update.

    Args:
        connection: Open database connection.
        google_event_id: Google Calendar event id (required ingest key).
        meet_url: Google Meet join URL.
        summary: Event summary/title.
        scheduled_at: Event start time (UTC datetime).
        ends_at: Event end time (UTC datetime).
        status: Meeting status (operator record-keeping only, §V.125).

    Returns:
        The inserted or updated meeting row.
    """
    row = connection.execute(
        """\
        INSERT INTO meeting (id, google_event_id, meet_url, summary,
            scheduled_at, ends_at, status)
        VALUES (%(id)s, %(google_event_id)s, %(meet_url)s, %(summary)s,
            %(scheduled_at)s, %(ends_at)s, %(status)s)
        ON CONFLICT (google_event_id) DO UPDATE
            SET meet_url = EXCLUDED.meet_url,
                summary = EXCLUDED.summary,
                scheduled_at = EXCLUDED.scheduled_at,
                ends_at = EXCLUDED.ends_at,
                status = EXCLUDED.status,
                updated_at = CURRENT_TIMESTAMP
        RETURNING *
        """,
        {
            "id": _new_id(),
            "google_event_id": google_event_id,
            "meet_url": meet_url,
            "summary": summary,
            "scheduled_at": scheduled_at,
            "ends_at": ends_at,
            "status": status,
        },
    ).fetchone()
    connection.commit()
    return Meeting.model_validate(row)


def link_meeting_attendee(
    connection: psycopg.Connection[dict[str, Any]],
    meeting_id: str,
    contact_id: str,
) -> MeetingAttendee | None:
    """Link a contact to a meeting as an attendee (§V.125).

    Idempotent via ``ON CONFLICT DO NOTHING`` on the ``(meeting_id,
    contact_id)`` UNIQUE pair: a repeat link returns ``None`` (no duplicate
    row), a fresh link returns the created row.

    Raises:
        ValueError: If the meeting or contact does not exist.
    """
    if (
        connection.execute(
            "SELECT 1 FROM meeting WHERE id = %s", (meeting_id,)
        ).fetchone()
        is None
    ):
        raise ValueError(f"meeting not found: {meeting_id}")
    if (
        connection.execute(
            "SELECT 1 FROM contact WHERE id = %s", (contact_id,)
        ).fetchone()
        is None
    ):
        raise ValueError(f"contact not found: {contact_id}")
    row = connection.execute(
        """\
        INSERT INTO meeting_attendee (id, meeting_id, contact_id)
        VALUES (%(id)s, %(meeting_id)s, %(contact_id)s)
        ON CONFLICT (meeting_id, contact_id) DO NOTHING
        RETURNING *
        """,
        {"id": _new_id(), "meeting_id": meeting_id, "contact_id": contact_id},
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return MeetingAttendee.model_validate(row)


def list_meeting_attendees(
    connection: psycopg.Connection[dict[str, Any]],
    meeting_id: str,
) -> list[Contact]:
    """List the contacts attending a meeting (§V.8, the reader for §B.112).

    Joins ``meeting_attendee`` to ``contact`` so the operator reads who attends.
    The reader half of the link relation whose writer is
    ``link_meeting_attendee`` and whose filter is ``meeting list
    --contact-email`` -- without it the booking conclusion (§V.128) is reachable
    only by raw SQL.

    Args:
        connection: Open database connection.
        meeting_id: Meeting ID.

    Returns:
        Attendee contacts ordered by email; empty list when none are linked.
    """
    rows = connection.execute(
        """\
        SELECT ct.*
        FROM meeting_attendee ma
        JOIN contact ct ON ct.id = ma.contact_id
        WHERE ma.meeting_id = %(meeting_id)s
        ORDER BY ct.email
        """,
        {"meeting_id": meeting_id},
    ).fetchall()
    return [Contact.model_validate(row) for row in rows]


# -- Composite View Loaders ----------------------------------------------------


def _load_notes_for_owner(
    connection: psycopg.Connection[dict[str, Any]],
    owner_column: str,
    owner_id: str,
) -> tuple[list[Note], int]:
    """Fetch latest notes for a single owner column plus the total row count.

    Two queries (no JOIN) per §V.8: ``LIMIT _INLINE_NOTES_CAP ORDER BY
    created_at DESC`` for the inline list, ``COUNT(*)`` for the total.
    """
    if owner_column not in {"contact_id", "company_id"}:
        raise ValueError(f"unsupported owner column: {owner_column}")
    list_query = SQL(
        "SELECT * FROM note WHERE {col} = %s ORDER BY created_at DESC LIMIT %s"
    ).format(col=Identifier(owner_column))
    rows = connection.execute(list_query, (owner_id, _INLINE_NOTES_CAP)).fetchall()
    notes = [Note.model_validate(row) for row in rows]
    count_query = SQL("SELECT COUNT(*) AS total FROM note WHERE {col} = %s").format(
        col=Identifier(owner_column)
    )
    count_row = connection.execute(count_query, (owner_id,)).fetchone()
    total = int(count_row["total"]) if count_row is not None else 0
    return notes, total


# §V.159: default / hard cap for contact view --timeline section sizes.
_TIMELINE_DEFAULT_LIMIT = 10
_TIMELINE_HARD_CAP = 50


def load_contact_view(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
) -> ContactView | None:
    """Load a contact with inlined notes (own + parent company) per §V.8.

    Returns ``None`` when the contact does not exist. ``notes`` and
    ``company_notes`` are capped at ``_INLINE_NOTES_CAP`` rows each, ordered
    by ``created_at`` DESC, full body verbatim. Totals reflect the actual row
    count, not the cap. ``company_notes`` is always ``[]`` when the contact
    has no parent company.

    The projection is a base-entity superset of agent-facing columns per
    §V.8: every ``Contact`` column except operator-only
    ``verification_meta`` (§V.144) is forwarded, and ``company_domain`` is
    fetched from the parent company (LEFT JOIN semantics, NULL when the
    contact has no company). Meta is never on this path — operators use
    ``contact view --include-meta``.
    """
    contact = get_contact(connection, contact_id)
    if contact is None:
        return None
    notes, notes_total = _load_notes_for_owner(connection, "contact_id", contact_id)
    if contact.company_id is not None:
        company = get_company(connection, contact.company_id)
        company_domain = company.domain if company is not None else None
        company_notes, company_notes_total = _load_notes_for_owner(
            connection, "company_id", contact.company_id
        )
    else:
        company_domain = None
        company_notes = []
        company_notes_total = 0
    # Strip operator-only meta so agent prompt + default CLI stay byte-identical
    # and never carry verification trails (§V.144 / §V.8).
    return ContactView(
        **contact.model_dump(exclude={"verification_meta"}),
        company_domain=company_domain,
        notes=notes,
        notes_total=notes_total,
        company_notes=company_notes,
        company_notes_total=company_notes_total,
    )


def load_contact_timeline(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
    *,
    limit: int = _TIMELINE_DEFAULT_LIMIT,
) -> dict[str, Any] | None:
    """Load contact view plus bounded enrollments/emails/activities (§V.159).

    Returns ``None`` when the contact does not exist. Composes
    ``load_contact_view`` with denser enrollment rows (status, disposition,
    last/next touch), recent emails, and recent activities. Each list is
    capped at ``limit`` (clamped to ``[_TIMELINE_DEFAULT_LIMIT range,
    _TIMELINE_HARD_CAP]``). Disabled / do_not_contact contacts are loaded
    normally (forensics). Does not rewrite Gmail bodies.

    The bare ``load_contact_view`` path is unchanged — timeline keys only
    appear on this opt-in loader.
    """
    view = load_contact_view(connection, contact_id)
    if view is None:
        return None
    n = max(1, min(int(limit), _TIMELINE_HARD_CAP))
    enrollments = list_enrollments_detailed(
        connection, contact_id=contact_id, full=True, limit=n
    )
    emails = list_emails(connection, contact_id=contact_id, limit=n)
    activities = list_activities(connection, contact_id=contact_id, limit=n)
    payload = view.model_dump(mode="json")
    payload["enrollments"] = [e.model_dump(mode="json") for e in enrollments]
    payload["emails"] = [e.model_dump(mode="json") for e in emails]
    payload["activities"] = [a.model_dump(mode="json") for a in activities]
    payload["timeline_limit"] = n
    return payload


def load_company_view(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
) -> CompanyView | None:
    """Load a company with inlined own notes per §V.8.

    Returns ``None`` when the company does not exist. ``notes`` capped at
    ``_INLINE_NOTES_CAP`` rows, ordered by ``created_at`` DESC, full body
    verbatim. ``notes_total`` reflects the actual row count. ``tags`` is the
    assigned tag-name list (empty ok; same shape as ``CompanySummary.tags``
    and ``db export`` company.tags, §V.116).
    """
    company = get_company(connection, company_id)
    if company is None:
        return None
    notes, notes_total = _load_notes_for_owner(connection, "company_id", company_id)
    owner_tags = list_tags(
        connection,
        company_id=company_id,
        limit=1_000_000,
        include_disabled=True,
    )
    return CompanyView(
        **company.model_dump(),
        tags=[t.name for t in owner_tags],
        aliases=list_company_aliases(connection, company_id),
        notes=notes,
        notes_total=notes_total,
    )


def load_meeting_view(
    connection: psycopg.Connection[dict[str, Any]],
    meeting_id: str,
) -> MeetingView | None:
    """Load a meeting with its attendee contacts inlined per §V.8.

    Returns ``None`` when the meeting does not exist. ``attendees`` carries the
    full attendee `Contact` rows (email + name + every base column) joined via
    ``meeting_attendee`` (§V.125); ``attendee_emails`` + ``attendee_count``
    mirror the ``meeting list`` summary denorm (§V.96). The reader for the
    write+filter relation that previously had none (§B.112).

    The projection is a base-entity superset per §V.8: every ``Meeting`` column
    is forwarded via ``**meeting.model_dump()``.
    """
    meeting = get_meeting(connection, meeting_id)
    if meeting is None:
        return None
    attendees = list_meeting_attendees(connection, meeting_id)
    return MeetingView(
        **meeting.model_dump(),
        attendees=attendees,
        attendee_emails=[contact.email for contact in attendees],
        attendee_count=len(attendees),
    )


# -- Sync Status ---------------------------------------------------------------


def upsert_sync_status(
    connection: psycopg.Connection[dict[str, Any]],
    pid: int,
) -> SyncStatus:
    """Insert or update the singleton sync status row.

    Args:
        connection: Open database connection.
        pid: Process ID of the running sync loop.

    Returns:
        Current sync status.
    """
    row = connection.execute(
        """\
        INSERT INTO sync_status (id, pid)
        VALUES ('singleton', %(pid)s)
        ON CONFLICT (id) DO UPDATE
            SET pid = %(pid)s,
                started_at = CURRENT_TIMESTAMP,
                heartbeat_at = CURRENT_TIMESTAMP
        RETURNING *
        """,
        {"pid": pid},
    ).fetchone()
    connection.commit()
    return SyncStatus.model_validate(row)


def get_sync_status(
    connection: psycopg.Connection[dict[str, Any]],
) -> SyncStatus | None:
    """Get the current sync status.

    Args:
        connection: Open database connection.

    Returns:
        SyncStatus if sync is registered, None otherwise.
    """
    row = connection.execute(
        "SELECT * FROM sync_status WHERE id = 'singleton'"
    ).fetchone()
    if row is None:
        return None
    return SyncStatus.model_validate(row)


def delete_sync_status(
    connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Delete the sync status row (called on clean shutdown).

    Args:
        connection: Open database connection.
    """
    connection.execute("DELETE FROM sync_status WHERE id = 'singleton'")
    connection.commit()


def update_sync_heartbeat(
    connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Update the heartbeat timestamp to signal liveness.

    Args:
        connection: Open database connection.
    """
    connection.execute(
        """\
        UPDATE sync_status
        SET heartbeat_at = CURRENT_TIMESTAMP
        WHERE id = 'singleton'
        """
    )
    connection.commit()


# -- Snapshot ------------------------------------------------------------------

_SNAPSHOT_SCHEMA_VERSION = 1
"""Format version of the `db export` / `db import` bundle (§V.121).

Distinct from the database schema hash (§V.19): this versions the JSON bundle
layout, not the live table structure. Bump it when the bundle sections change.
"""


def export_snapshot(
    connection: psycopg.Connection[dict[str, Any]],
) -> dict[str, Any]:
    """Build the database snapshot bundle (§V.121).

    Read-only. Scope is the tag vocabulary plus the company and contact tables
    only -- emails, activities, notes, workflows, enrollments, tasks, and
    accounts are excluded. Tags embed under their owner row; the vocabulary
    rides its own ``tags`` section so a disabled or unassigned tag survives the
    round-trip. Every link is a natural key (company domain, contact email, tag
    name); no source-DB UUID is forwarded, so a fresh import re-links a contact
    to its company by domain, never the exported id (carries the §B.104 lesson
    into the bundle).

    Args:
        connection: Open database connection.

    Returns:
        The bundle dict: ``schema_version``, ``exported_at``, ``tags``,
        ``companies`` (each with embedded ``tags``), and ``contacts`` (each with
        ``company_domain`` and embedded ``tags``).
    """
    vocabulary = list_tags(connection, limit=1_000_000, include_disabled=True)
    tags = [{"name": t.name, "disabled_reason": t.disabled_reason} for t in vocabulary]

    companies: list[dict[str, Any]] = []
    for summary in list_companies(connection, limit=1_000_000, include_disabled=True):
        company = get_company(connection, summary.id)
        if company is None:
            continue
        owner_tags = list_tags(
            connection,
            company_id=summary.id,
            limit=1_000_000,
            include_disabled=True,
        )
        companies.append(
            {
                "name": company.name,
                "domain": company.domain,
                "profile": company.profile,
                "disabled_reason": company.disabled_reason,
                "tags": [t.name for t in owner_tags],
                "aliases": list_company_aliases(connection, summary.id),
            }
        )

    contacts: list[dict[str, Any]] = []
    for summary in list_contacts(connection, limit=1_000_000, include_disabled=True):
        owner_tags = list_tags(
            connection,
            contact_id=summary.id,
            limit=1_000_000,
            include_disabled=True,
        )
        contacts.append(
            {
                "email": summary.email,
                "first_name": summary.first_name,
                "last_name": summary.last_name,
                "title": summary.title,
                "email_confidence": summary.email_confidence,
                "disabled_reason": summary.disabled_reason,
                "company_domain": summary.company_domain,
                "tags": [t.name for t in owner_tags],
            }
        )

    return {
        "schema_version": _SNAPSHOT_SCHEMA_VERSION,
        "exported_at": datetime.now(tz=UTC).isoformat(),
        "tags": tags,
        "companies": companies,
        "contacts": contacts,
    }


def _restore_tag_assignment(
    connection: psycopg.Connection[dict[str, Any]],
    tag_name: object,
    owner_key: str,
    errors: list[dict[str, Any]],
    company_id: str | None = None,
    contact_id: str | None = None,
) -> None:
    """Link a restored owner to a vocabulary tag by name (§V.121).

    Resolves the tag through the vocabulary -- a name absent from the vocabulary
    records a per-row error and the batch continues, never auto-creating the tag
    (§V.116 Enum-family rule). Vocabulary-first restore order guarantees a
    faithful bundle always resolves here.

    Args:
        connection: Open database connection.
        tag_name: Tag name carried by the owner row's ``tags`` list.
        owner_key: The owner's natural key (domain or email) for error reporting.
        errors: Accumulator the helper appends per-row failures onto.
        company_id: Owning company id, or ``None`` for a contact owner.
        contact_id: Owning contact id, or ``None`` for a company owner.
    """
    try:
        tag = (
            get_tag_by_name(connection, tag_name) if isinstance(tag_name, str) else None
        )
    except ValueError:
        tag = None
    if tag is None:
        errors.append(
            {
                "entity": "tag_assignment",
                "key": owner_key,
                "error": "not_found",
                "message": f"tag {tag_name!r} not in vocabulary",
            }
        )
        return
    if company_id is not None:
        assign_tag_to_company(connection, tag.id, company_id)
    else:
        assert contact_id is not None
        assign_tag_to_contact(connection, tag.id, contact_id)


def _restore_tags(
    connection: psycopg.Connection[dict[str, Any]],
    entries: Iterable[Any],
    errors: list[dict[str, Any]],
) -> int:
    """Restore the tag vocabulary, returning the count of rows restored (§V.121)."""
    restored = 0
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str):
            errors.append(
                {
                    "entity": "tag",
                    "key": "",
                    "error": "validation_error",
                    "message": "tag row missing 'name'",
                }
            )
            continue
        try:
            create_tag(connection, name)
        except ValueError as exc:
            errors.append(
                {
                    "entity": "tag",
                    "key": name,
                    "error": "validation_error",
                    "message": str(exc),
                }
            )
            continue
        reason = entry.get("disabled_reason")
        if reason:
            disable_tag(connection, name, reason)
        restored += 1
    return restored


def _restore_companies(  # noqa: C901
    connection: psycopg.Connection[dict[str, Any]],
    entries: Iterable[Any],
    errors: list[dict[str, Any]],
) -> int:
    """Restore companies (profile, disabled state, tags), returning the count."""
    restored = 0
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        domain = entry.get("domain") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not isinstance(domain, str):
            errors.append(
                {
                    "entity": "company",
                    "key": domain if isinstance(domain, str) else "",
                    "error": "validation_error",
                    "message": "company row missing 'name' or 'domain'",
                }
            )
            continue
        company = create_company(connection, name=name, domain=domain)
        if company is None:
            company = get_company_by_domain(connection, domain)
        if company is None:
            errors.append(
                {
                    "entity": "company",
                    "key": domain,
                    "error": "database_error",
                    "message": f"could not create or resolve company {domain!r}",
                }
            )
            continue
        profile = entry.get("profile")
        if profile is not None:
            update_company(connection, company.id, profile=profile)
        reason = entry.get("disabled_reason")
        if reason:
            disable_company(connection, company.id, reason)
        for tag_name in entry.get("tags", []):
            _restore_tag_assignment(
                connection, tag_name, domain, errors, company_id=company.id
            )
        for alias in entry.get("aliases", []):
            if not isinstance(alias, str):
                errors.append(
                    {
                        "entity": "company_alias",
                        "key": domain,
                        "error": "validation_error",
                        "message": f"alias must be a string for company {domain!r}",
                    }
                )
                continue
            try:
                add_company_alias(connection, company.id, alias, commit=False)
            except ValueError as exc:
                errors.append(
                    {
                        "entity": "company_alias",
                        "key": domain,
                        "error": "already_exists",
                        "message": str(exc),
                    }
                )
                continue
        connection.commit()
        restored += 1
    return restored


def _restore_contacts(
    connection: psycopg.Connection[dict[str, Any]],
    entries: Iterable[Any],
    errors: list[dict[str, Any]],
) -> int:
    """Restore contacts re-linked to their company by domain (§B.104 lesson)."""
    restored = 0
    for entry in entries:
        email = entry.get("email") if isinstance(entry, dict) else None
        if not isinstance(email, str):
            errors.append(
                {
                    "entity": "contact",
                    "key": "",
                    "error": "validation_error",
                    "message": "contact row missing 'email'",
                }
            )
            continue
        company_domain = entry.get("company_domain")
        company_id: str | None = None
        if company_domain is not None:
            owner = get_company_by_domain(connection, company_domain)
            if owner is None:
                errors.append(
                    {
                        "entity": "contact",
                        "key": email,
                        "error": "foreign_key_violation",
                        "message": (
                            f"company domain {company_domain!r} not found for "
                            f"contact {email!r}"
                        ),
                    }
                )
                continue
            company_id = owner.id
        contact = create_contact(
            connection,
            email=email,
            first_name=entry.get("first_name"),
            last_name=entry.get("last_name"),
            company_id=company_id,
            title=entry.get("title"),
            email_confidence=entry.get("email_confidence"),
        )
        if contact is None:
            contact = get_contact_by_email(connection, email)
        if contact is None:
            errors.append(
                {
                    "entity": "contact",
                    "key": email,
                    "error": "database_error",
                    "message": f"could not create or resolve contact {email!r}",
                }
            )
            continue
        reason = entry.get("disabled_reason")
        if reason:
            disable_contact(connection, contact.id, reason)
        for tag_name in entry.get("tags", []):
            _restore_tag_assignment(
                connection, tag_name, email, errors, contact_id=contact.id
            )
        restored += 1
    return restored


def import_snapshot(
    connection: psycopg.Connection[dict[str, Any]],
    bundle: dict[str, Any],
) -> dict[str, Any]:
    """Restore a snapshot bundle in dependency order (§V.121).

    Restore order is fixed: tag vocabulary first, then companies, then contacts.
    Vocabulary-first lets each assignment resolve by name without auto-create
    (§V.116 forbids tag auto-create). Every link resolves by natural key --
    company domain, contact email, tag name; a contact re-links to its company
    by the bundle's ``company_domain``, never a source-DB id (the §B.104
    lesson). A row that cannot resolve its foreign key records a per-row error
    entry and the batch continues -- never a batch-aborting raise.

    Args:
        connection: Open database connection.
        bundle: The snapshot bundle dict (see ``export_snapshot``).

    Returns:
        A result dict: ``tags`` / ``companies`` / ``contacts`` counts of rows
        restored, plus an ``errors`` list of per-row ``{entity, key, error,
        message}`` failures.
    """
    errors: list[dict[str, Any]] = []
    tags_restored = _restore_tags(connection, bundle.get("tags", []), errors)
    companies_restored = _restore_companies(
        connection, bundle.get("companies", []), errors
    )
    contacts_restored = _restore_contacts(
        connection, bundle.get("contacts", []), errors
    )
    return {
        "tags": tags_restored,
        "companies": companies_restored,
        "contacts": contacts_restored,
        "errors": errors,
    }
