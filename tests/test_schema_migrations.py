"""Tests for the forward-only migration registry (§V.108).

Covers the ``migrations/NNN_*.sql`` registry, the ``schema_migrations`` ledger,
``migrate_database`` (pending-in-order, own-transaction, ledger recording,
idempotent re-run), and the load-bearing identity invariant: a fresh build from
``schema.sql`` is byte-identical in structure to applying every migration from
an empty database.

The migrate/identity tests build into dedicated PostgreSQL schemas (set via
``search_path``) so they never touch the shared ``public`` schema the session
fixture provisions; each schema is dropped on teardown.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.sql import SQL, Identifier

import mailpilot
from conftest import TEST_DATABASE_URL
from mailpilot import database as db_mod
from mailpilot.database import (
    _ENSURE_MIGRATIONS_LEDGER_SQL,  # pyright: ignore[reportPrivateUsage]
    _MAILPILOT_VERSION,  # pyright: ignore[reportPrivateUsage]
    MIGRATIONS_PATH,
    SCHEMA_PATH,
    _compute_schema_hash,  # pyright: ignore[reportPrivateUsage]
    _discover_migrations,  # pyright: ignore[reportPrivateUsage]
    determine_schema_verdict,
    migrate_database,
)

# -- connection / schema helpers ----------------------------------------------


def _connect(*, autocommit: bool = False) -> psycopg.Connection[dict[str, Any]]:
    return cast(
        psycopg.Connection[dict[str, Any]],
        psycopg.connect(
            TEST_DATABASE_URL,
            row_factory=dict_row,  # type: ignore[arg-type]
            autocommit=autocommit,
        ),
    )


def _reset_schema(admin: psycopg.Connection[dict[str, Any]], name: str) -> None:
    admin.execute(SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(Identifier(name)))
    admin.execute(SQL("CREATE SCHEMA {}").format(Identifier(name)))


def _use_schema(conn: psycopg.Connection[dict[str, Any]], name: str) -> None:
    """Pin the connection's session search_path to ``name`` and commit it.

    Committed so it survives the rollback ``migrate_database`` issues to close
    its read transaction.
    """
    conn.execute(SQL("SET search_path TO {}").format(Identifier(name)))
    conn.commit()


@pytest.fixture
def migration_schema() -> Iterator[psycopg.Connection[dict[str, Any]]]:
    """Yield a manual-commit connection pinned to a fresh, isolated schema."""
    schema = "mp_migration_test"
    admin = _connect(autocommit=True)
    _reset_schema(admin, schema)
    conn = _connect()
    _use_schema(conn, schema)
    yield conn
    conn.close()
    admin.execute(SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(Identifier(schema)))
    admin.close()


def _write_migration(directory: Path, filename: str, body: str) -> None:
    (directory / filename).write_text(body)


def _normalize_sql(text: str) -> str:
    """Strip comments and collapse whitespace (mirrors §V.19 normalization)."""
    return re.sub(r"\s+", " ", re.sub(r"--[^\n]*", "", text)).strip()


# -- registry shipping + discovery (§V.108) -----------------------------------


def test_migrations_dir_ships_under_package_root():
    """`migrations/` lives inside the package dir → bundled in the wheel."""
    assert MIGRATIONS_PATH.is_dir()
    assert MIGRATIONS_PATH.parent == Path(mailpilot.__file__).parent


def test_baseline_migration_present_and_discovered():
    """The frozen `001_initial_schema.sql` baseline is discovered as version 1."""
    discovered = _discover_migrations()
    assert (1, "initial_schema") in [(v, n) for v, n, _p in discovered]


def test_discover_parses_version_and_sorts_numerically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Numeric (not lexical) sort; non-matching files skipped."""
    monkeypatch.setattr(db_mod, "MIGRATIONS_PATH", tmp_path)
    _write_migration(tmp_path, "2_beta.sql", "SELECT 1;")
    _write_migration(tmp_path, "10_gamma.sql", "SELECT 1;")
    _write_migration(tmp_path, "1_alpha.sql", "SELECT 1;")
    _write_migration(tmp_path, "scratch.sql", "SELECT 1;")  # no NNN_ prefix
    _write_migration(tmp_path, "3_bad-name.sql", "SELECT 1;")  # hyphen → skipped
    (tmp_path / "README.txt").write_text("notes")  # not .sql

    discovered = _discover_migrations()
    assert [(v, n) for v, n, _p in discovered] == [
        (1, "alpha"),
        (2, "beta"),
        (10, "gamma"),
    ]


def test_discover_rejects_duplicate_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Two files sharing a version prefix is a packaging error."""
    monkeypatch.setattr(db_mod, "MIGRATIONS_PATH", tmp_path)
    _write_migration(tmp_path, "1_a.sql", "SELECT 1;")
    _write_migration(tmp_path, "1_b.sql", "SELECT 1;")
    with pytest.raises(ValueError, match="duplicate migration version"):
        _discover_migrations()


def test_discover_empty_when_dir_absent(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(db_mod, "MIGRATIONS_PATH", Path("/nonexistent/migrations"))
    assert _discover_migrations() == []


# -- schema_migrations ledger lives in canonical schema.sql (§V.108) ----------


def test_schema_migrations_ledger_declared_in_schema_sql():
    schema_text = SCHEMA_PATH.read_text()
    assert "CREATE TABLE IF NOT EXISTS schema_migrations" in schema_text
    # The machinery's ensure-DDL must match the canonical declaration (lockstep).
    assert _normalize_sql(_ENSURE_MIGRATIONS_LEDGER_SQL) in _normalize_sql(schema_text)


# -- migrate_database behavior (§V.108) ---------------------------------------


def _ledger_rows(
    conn: psycopg.Connection[dict[str, Any]],
) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT version, name, mailpilot_version, applied_at "
        "FROM schema_migrations ORDER BY version"
    ).fetchall()


def test_migrate_applies_pending_in_order_and_records_ledger(
    migration_schema: psycopg.Connection[dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Pending migrations apply ascending; ledger records version/name/version.

    002 ALTERs the table 001 creates, so a wrong order would raise — the
    success proves ascending application.
    """
    monkeypatch.setattr(db_mod, "MIGRATIONS_PATH", tmp_path)
    _write_migration(
        tmp_path,
        "001_create_widget.sql",
        "CREATE TABLE widget (id INTEGER PRIMARY KEY);",
    )
    _write_migration(
        tmp_path, "002_add_label.sql", "ALTER TABLE widget ADD COLUMN label TEXT;"
    )

    applied = migrate_database(migration_schema)
    assert applied == [
        {"version": 1, "name": "create_widget"},
        {"version": 2, "name": "add_label"},
    ]

    rows = _ledger_rows(migration_schema)
    assert [r["version"] for r in rows] == [1, 2]
    assert [r["name"] for r in rows] == ["create_widget", "add_label"]
    assert all(r["mailpilot_version"] == _MAILPILOT_VERSION for r in rows)
    assert all(r["applied_at"] is not None for r in rows)

    cols = migration_schema.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'widget' ORDER BY ordinal_position"
    ).fetchall()
    assert [c["column_name"] for c in cols] == ["id", "label"]


def test_migrate_is_noop_when_current(
    migration_schema: psycopg.Connection[dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A second run with nothing pending applies nothing and leaves the ledger."""
    monkeypatch.setattr(db_mod, "MIGRATIONS_PATH", tmp_path)
    _write_migration(
        tmp_path,
        "001_create_widget.sql",
        "CREATE TABLE widget (id INTEGER PRIMARY KEY);",
    )

    assert len(migrate_database(migration_schema)) == 1
    assert migrate_database(migration_schema) == []
    assert [r["version"] for r in _ledger_rows(migration_schema)] == [1]


def test_migrate_each_migration_is_its_own_transaction(
    migration_schema: psycopg.Connection[dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A failing migration rolls back alone; earlier ones stay applied+recorded."""
    monkeypatch.setattr(db_mod, "MIGRATIONS_PATH", tmp_path)
    _write_migration(tmp_path, "001_ok.sql", "CREATE TABLE okrow (id INTEGER);")
    # Duplicate CREATE → relation-already-exists mid-run.
    _write_migration(tmp_path, "002_boom.sql", "CREATE TABLE okrow (id INTEGER);")

    with pytest.raises(psycopg.Error):
        migrate_database(migration_schema)

    migration_schema.rollback()
    # 001 committed independently; 002 rolled back, never recorded.
    assert [r["version"] for r in _ledger_rows(migration_schema)] == [1]
    exists = migration_schema.execute("SELECT to_regclass('okrow') AS oid").fetchone()
    assert exists is not None
    assert exists["oid"] is not None


# -- re-stamp recorded hash: migrate-forward resolves `current` (§V.108/§B.97) -


def test_migrate_rebaselines_stale_recorded_hash_to_current(
    migration_schema: psycopg.Connection[dict[str, Any]],
):
    """§V.108/§B.97: every shipped migration applied but a stale recorded hash
    (a DB init'd at an older version then migrated forward) must re-stamp on
    migrate so the verdict resolves `current`, not phantom `drift` -- even with
    nothing pending."""
    conn = migration_schema
    # Build the full canonical schema, then reproduce the §B.97 false-drift
    # state: all shipped migrations recorded as applied (-> 0 pending) but the
    # recorded hash frozen at an older value.
    conn.execute(SCHEMA_PATH.read_text())  # type: ignore[arg-type]
    for version, name, _path in _discover_migrations():
        conn.execute(
            "INSERT INTO schema_migrations (version, name, mailpilot_version) "
            "VALUES (%s, %s, %s)",
            (version, name, _MAILPILOT_VERSION),
        )
    conn.execute(
        "INSERT INTO schema_metadata (id, mailpilot_version, schema_hash) "
        "VALUES (1, %s, %s)",
        ("0.0.0", "stale-hash-from-an-older-mailpilot-version"),
    )
    conn.commit()

    # Precondition: this is exactly the phantom drift the bug produced.
    assert determine_schema_verdict(conn).verdict == "drift"

    # Nothing is pending, but migrate must still re-baseline the recorded hash.
    assert migrate_database(conn) == []

    status = determine_schema_verdict(conn)
    assert status.verdict == "current"
    assert status.recorded_hash == _compute_schema_hash(SCHEMA_PATH.read_text())


# -- identity invariant: schema.sql == apply-all-migrations-from-zero (§V.108) -


def _structure_fingerprint(
    conn: psycopg.Connection[dict[str, Any]], schema: str
) -> str:
    """Normalized structural fingerprint of all tables in ``schema``.

    Captures columns, constraints, indexes, and triggers. The schema name is
    stripped so two builds in different schemas compare equal when their
    structure matches.
    """
    lines: list[str] = []

    for row in conn.execute(
        """
        SELECT table_name, ordinal_position, column_name, data_type,
               udt_name, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = %s
        ORDER BY table_name, ordinal_position
        """,
        (schema,),
    ).fetchall():
        lines.append(
            f"COL {row['table_name']}.{row['column_name']} "
            f"{row['data_type']}/{row['udt_name']} "
            f"null={row['is_nullable']} default={row['column_default']}"
        )

    for row in conn.execute(
        """
        SELECT cls.relname AS table_name, con.conname,
               pg_get_constraintdef(con.oid) AS condef
        FROM pg_constraint con
        JOIN pg_class cls ON cls.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid = cls.relnamespace
        WHERE ns.nspname = %s
        ORDER BY cls.relname, con.conname
        """,
        (schema,),
    ).fetchall():
        lines.append(f"CON {row['table_name']}.{row['conname']} {row['condef']}")

    for row in conn.execute(
        """
        SELECT tablename, indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = %s
        ORDER BY tablename, indexname
        """,
        (schema,),
    ).fetchall():
        lines.append(f"IDX {row['tablename']}.{row['indexname']} {row['indexdef']}")

    for row in conn.execute(
        """
        SELECT event_object_table, trigger_name, action_timing,
               event_manipulation, action_statement
        FROM information_schema.triggers
        WHERE trigger_schema = %s
        ORDER BY event_object_table, trigger_name, event_manipulation
        """,
        (schema,),
    ).fetchall():
        lines.append(
            f"TRG {row['event_object_table']}.{row['trigger_name']} "
            f"{row['action_timing']} {row['event_manipulation']} "
            f"{row['action_statement']}"
        )

    return "\n".join(lines).replace(f"{schema}.", "")


def test_fresh_schema_equals_apply_all_migrations_from_zero():
    """Identity invariant (§V.108): a fresh `schema.sql` build is byte-identical
    in structure to applying every migration from an empty database."""
    canonical_schema = "mp_identity_canonical"
    migrated_schema = "mp_identity_migrated"
    admin = _connect(autocommit=True)
    try:
        _reset_schema(admin, canonical_schema)
        _reset_schema(admin, migrated_schema)

        # Path A: fresh build straight from canonical schema.sql.
        canonical_conn = _connect()
        try:
            _use_schema(canonical_conn, canonical_schema)
            canonical_conn.execute(SCHEMA_PATH.read_text())  # type: ignore[arg-type]
            canonical_conn.commit()
        finally:
            canonical_conn.close()

        # Path B: empty schema → apply every migration from zero.
        migrated_conn = _connect()
        try:
            _use_schema(migrated_conn, migrated_schema)
            migrate_database(migrated_conn)
        finally:
            migrated_conn.close()

        fingerprint_conn = _connect(autocommit=True)
        try:
            canonical_fp = _structure_fingerprint(fingerprint_conn, canonical_schema)
            migrated_fp = _structure_fingerprint(fingerprint_conn, migrated_schema)
        finally:
            fingerprint_conn.close()

        assert canonical_fp == migrated_fp
        # Guard against a vacuous comparison of two empty fingerprints.
        assert "COL account.id" in canonical_fp
        assert "COL schema_migrations.version" in canonical_fp
    finally:
        admin.execute(
            SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(Identifier(canonical_schema))
        )
        admin.execute(
            SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(Identifier(migrated_schema))
        )
        admin.close()
