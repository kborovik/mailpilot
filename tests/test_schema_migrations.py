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


def _read_shipped_migration(version_prefix: str) -> str:
    """Return the body of the shipped migration whose file starts ``NNN_``."""
    matches = sorted(MIGRATIONS_PATH.glob(f"{version_prefix}_*.sql"))
    assert matches, f"no shipped migration with prefix {version_prefix}"
    return matches[0].read_text()


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


# -- migration 003 data fold: legacy per-owner tags -> vocabulary (§V.116) -----


def test_migration_003_folds_legacy_tags_into_vocabulary(
    migration_schema: psycopg.Connection[dict[str, Any]],
):
    """§V.116: migration 003 folds the legacy per-owner `tag` table into a
    vocabulary `tag` (one row per distinct name) plus a `tag_assignment` link
    (one row per legacy tag row). A name carried by multiple owners collapses to
    one vocabulary row; a disabled legacy row carries its reason onto the vocab
    row."""
    conn = migration_schema
    # Build the pre-003 world: initial schema + the 002 column add.
    conn.execute(_read_shipped_migration("001"))  # type: ignore[arg-type]
    conn.execute(_read_shipped_migration("002"))  # type: ignore[arg-type]
    conn.commit()

    # Seed two owners and legacy (per-owner) tag rows in the old shape.
    conn.execute(
        "INSERT INTO company (id, name, domain) VALUES (%s, %s, %s)",
        ("co1", "Acme", "acme.com"),
    )
    conn.execute(
        "INSERT INTO contact (id, email, company_id) VALUES (%s, %s, %s)",
        ("ct1", "a@acme.com", "co1"),
    )
    # 'vip' on a company AND a contact -> one vocab row, two assignments.
    conn.execute(
        "INSERT INTO tag (id, company_id, name) VALUES (%s, %s, %s)",
        ("t1", "co1", "vip"),
    )
    conn.execute(
        "INSERT INTO tag (id, contact_id, name) VALUES (%s, %s, %s)",
        ("t2", "ct1", "vip"),
    )
    # 'cold' on the contact, disabled -> vocab row carries the reason.
    conn.execute(
        "INSERT INTO tag (id, contact_id, name, disabled_reason) "
        "VALUES (%s, %s, %s, %s)",
        ("t3", "ct1", "cold", "stale"),
    )
    conn.commit()

    # Apply the fold.
    conn.execute(_read_shipped_migration("003"))  # type: ignore[arg-type]
    conn.commit()

    vocab = conn.execute(
        "SELECT name, disabled_reason FROM tag ORDER BY name"
    ).fetchall()
    assert [(r["name"], r["disabled_reason"]) for r in vocab] == [
        ("cold", "stale"),
        ("vip", None),
    ]

    assignments = conn.execute(
        "SELECT t.name, a.contact_id, a.company_id "
        "FROM tag_assignment a JOIN tag t ON t.id = a.tag_id "
        "ORDER BY t.name, a.company_id NULLS LAST"
    ).fetchall()
    assert [(r["name"], r["contact_id"], r["company_id"]) for r in assignments] == [
        ("cold", "ct1", None),
        ("vip", None, "co1"),
        ("vip", "ct1", None),
    ]
    # Minted ids are valid (uuidv7()::text), distinct across vocab + links.
    all_ids = conn.execute(
        "SELECT id FROM tag UNION ALL SELECT id FROM tag_assignment"
    ).fetchall()
    assert len({r["id"] for r in all_ids}) == 5  # 2 vocab + 3 assignments


# -- migration 004: collapse enrollment.status paused -> disabled (§V.15) ------


def test_migration_004_folds_paused_enrollments_to_disabled(
    migration_schema: psycopg.Connection[dict[str, Any]],
):
    """§V.15: migration 004 folds existing ``paused`` enrollments into
    ``disabled`` (carrying a literal reason so the coupling CHECK holds), then
    tightens the status CHECK to {active, disabled} and admits the new
    ``enrollment_enabled`` activity type."""
    conn = migration_schema
    # Build the pre-004 world: every shipped migration up to 003.
    conn.execute(_read_shipped_migration("001"))  # type: ignore[arg-type]
    conn.execute(_read_shipped_migration("002"))  # type: ignore[arg-type]
    conn.execute(_read_shipped_migration("003"))  # type: ignore[arg-type]
    conn.commit()

    # Seed a paused enrollment (valid under the pre-004 three-value CHECK).
    conn.execute(
        "INSERT INTO account (id, email) VALUES (%s, %s)", ("ac1", "a@example.com")
    )
    conn.execute(
        "INSERT INTO workflow (id, account_id, template, type, name) "
        "VALUES (%s, %s, %s, %s, %s)",
        ("wf1", "ac1", "outbound-general", "outbound", "W"),
    )
    conn.execute(
        "INSERT INTO contact (id, email) VALUES (%s, %s)", ("ct1", "c@example.com")
    )
    conn.execute(
        "INSERT INTO enrollment (id, workflow_id, contact_id, status) "
        "VALUES (%s, %s, %s, %s)",
        ("en1", "wf1", "ct1", "paused"),
    )
    conn.commit()

    # Apply the collapse.
    conn.execute(_read_shipped_migration("004"))  # type: ignore[arg-type]
    conn.commit()

    folded = conn.execute(
        "SELECT status, disabled_reason FROM enrollment WHERE id = 'en1'"
    ).fetchone()
    assert folded is not None
    assert folded["status"] == "disabled"
    assert folded["disabled_reason"] == "paused: collapsed to disabled"

    # The tightened status CHECK now rejects `paused`.
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE enrollment SET status = 'paused' WHERE id = 'en1'")
    conn.rollback()

    # The activity CHECK now admits `enrollment_enabled`.
    conn.execute(
        "INSERT INTO activity (id, contact_id, type) VALUES (%s, %s, %s)",
        ("av1", "ct1", "enrollment_enabled"),
    )
    conn.commit()
    admitted = conn.execute("SELECT type FROM activity WHERE id = 'av1'").fetchone()
    assert admitted is not None
    assert admitted["type"] == "enrollment_enabled"


# -- migration 005: drop dead `tag_disabled` activity type (§V.10/§V.17) -------


def test_migration_005_drops_tag_disabled_activity_type(
    migration_schema: psycopg.Connection[dict[str, Any]],
):
    """§V.10/§V.17: migration 005 drops the never-emitted ``tag_disabled``
    activity type from the CHECK. Vocabulary-tag disable/enable write no
    activity (an activity targets a contact/company and a tag row owns neither,
    §V.17), so no historical row carries it -- the CHECK tightens with no fold,
    unlike the enrollment collapse that retained ``paused``/``resumed``."""
    conn = migration_schema
    # Build the pre-005 world: every shipped migration up to 004.
    conn.execute(_read_shipped_migration("001"))  # type: ignore[arg-type]
    conn.execute(_read_shipped_migration("002"))  # type: ignore[arg-type]
    conn.execute(_read_shipped_migration("003"))  # type: ignore[arg-type]
    conn.execute(_read_shipped_migration("004"))  # type: ignore[arg-type]
    conn.commit()

    conn.execute(
        "INSERT INTO contact (id, email) VALUES (%s, %s)", ("ct1", "c@example.com")
    )
    conn.commit()

    # Pre-005, the dead value is still admitted by the 004 CHECK.
    conn.execute(
        "INSERT INTO activity (id, contact_id, type) VALUES (%s, %s, %s)",
        ("av0", "ct1", "tag_disabled"),
    )
    conn.execute("DELETE FROM activity WHERE id = 'av0'")
    conn.commit()

    # Apply the tighten.
    conn.execute(_read_shipped_migration("005"))  # type: ignore[arg-type]
    conn.commit()

    # The tightened CHECK now rejects `tag_disabled`.
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO activity (id, contact_id, type) VALUES (%s, %s, %s)",
            ("av1", "ct1", "tag_disabled"),
        )
    conn.rollback()

    # A retained value is still admitted.
    conn.execute(
        "INSERT INTO activity (id, contact_id, type) VALUES (%s, %s, %s)",
        ("av2", "ct1", "tag_removed"),
    )
    conn.commit()
    admitted = conn.execute("SELECT type FROM activity WHERE id = 'av2'").fetchone()
    assert admitted is not None
    assert admitted["type"] == "tag_removed"


# -- migration 007: add meeting + meeting_attendee tables (§V.125) -------------


def test_migration_007_adds_meeting_tables(
    migration_schema: psycopg.Connection[dict[str, Any]],
):
    """§V.125: migration 007 adds the `meeting` + `meeting_attendee` tables.

    A meeting is keyed on `google_event_id` (nullable-unique idempotent ingest)
    and links attendees through `meeting_attendee` (UNIQUE per pair). The DDL
    is byte-identical to schema.sql, so the identity test guards the structure;
    this test exercises the new constraints on a migrate-from-zero build.
    """
    conn = migration_schema
    # Build the pre-007 world: every shipped migration up to 006.
    for prefix in ("001", "002", "003", "004", "005", "006"):
        conn.execute(_read_shipped_migration(prefix))  # type: ignore[arg-type]
    conn.commit()

    # Pre-007 the tables do not exist.
    pre = conn.execute("SELECT to_regclass('meeting') AS oid").fetchone()
    assert pre is not None
    assert pre["oid"] is None

    # Apply the add.
    conn.execute(_read_shipped_migration("007"))  # type: ignore[arg-type]
    conn.commit()

    # The status CHECK admits the four documented values and rejects others.
    conn.execute(
        "INSERT INTO meeting (id, google_event_id, status) VALUES (%s, %s, %s)",
        ("m1", "evt-1", "scheduled"),
    )
    conn.commit()
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO meeting (id, status) VALUES (%s, %s)", ("m2", "postponed")
        )
    conn.rollback()

    # google_event_id is unique (idempotent ingest key).
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "INSERT INTO meeting (id, google_event_id) VALUES (%s, %s)",
            ("m3", "evt-1"),
        )
    conn.rollback()

    # meeting_attendee enforces UNIQUE per (meeting_id, contact_id) pair.
    conn.execute(
        "INSERT INTO contact (id, email) VALUES (%s, %s)", ("ct1", "a@acme.com")
    )
    conn.execute(
        "INSERT INTO meeting_attendee (id, meeting_id, contact_id) VALUES (%s, %s, %s)",
        ("ma1", "m1", "ct1"),
    )
    conn.commit()
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "INSERT INTO meeting_attendee (id, meeting_id, contact_id) "
            "VALUES (%s, %s, %s)",
            ("ma2", "m1", "ct1"),
        )
    conn.rollback()


# -- migration 008: lowercase contact.email + merge case-variants (§V.90/§B.121) -


def test_migration_008_merges_case_variant_contacts_and_lowercases(
    migration_schema: psycopg.Connection[dict[str, Any]],
):
    """§V.90/§B.121: migration 008 folds case-variant duplicate contacts onto the
    canonical (earliest) lowercase row, repoints child rows, drops the
    duplicates, then lowercases every surviving address.

    A pre-fix build could mint a bare duplicate when an inbound reply recased the
    From local-part (`CThorne@` vs the enrolled `cthorne@`). The migration heals
    that: the canonical row keeps its enrollment, the duplicate's email row is
    repointed onto it, and the duplicate contact is removed.
    """
    conn = migration_schema
    # Build the pre-008 world: every shipped migration up to 007.
    for prefix in ("001", "002", "003", "004", "005", "006", "007"):
        conn.execute(_read_shipped_migration(prefix))  # type: ignore[arg-type]
    conn.commit()

    conn.execute(
        "INSERT INTO account (id, email) VALUES (%s, %s)", ("ac1", "rep@lab5.ca")
    )
    conn.execute(
        "INSERT INTO workflow (id, account_id, template, type, name) "
        "VALUES (%s, %s, %s, %s, %s)",
        ("wf1", "ac1", "outbound-general", "outbound", "W"),
    )
    # Canonical (earliest) lowercase contact with an enrollment.
    conn.execute(
        "INSERT INTO contact (id, email, created_at) VALUES (%s, %s, %s)",
        ("ct_canon", "cthorne@example.com", "2026-01-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO enrollment (id, workflow_id, contact_id) VALUES (%s, %s, %s)",
        ("en1", "wf1", "ct_canon"),
    )
    # Bare duplicate minted later from a recased inbound From, carrying one email.
    conn.execute(
        "INSERT INTO contact (id, email, created_at) VALUES (%s, %s, %s)",
        ("ct_dup", "CThorne@example.com", "2026-02-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO email (id, account_id, contact_id, direction) "
        "VALUES (%s, %s, %s, %s)",
        ("em1", "ac1", "ct_dup", "inbound"),
    )
    # An unrelated already-lowercase contact must survive untouched.
    conn.execute(
        "INSERT INTO contact (id, email) VALUES (%s, %s)",
        ("ct_other", "bob@example.com"),
    )
    conn.commit()

    # Apply the merge.
    conn.execute(_read_shipped_migration("008"))  # type: ignore[arg-type]
    conn.commit()

    # The duplicate is gone; exactly one row carries the lowercased key.
    rows = conn.execute(
        "SELECT id FROM contact WHERE email = %s", ("cthorne@example.com",)
    ).fetchall()
    assert [r["id"] for r in rows] == ["ct_canon"]
    assert (
        conn.execute("SELECT id FROM contact WHERE id = %s", ("ct_dup",)).fetchone()
        is None
    )

    # The duplicate's email row was repointed onto the canonical contact.
    email_row = conn.execute(
        "SELECT contact_id FROM email WHERE id = %s", ("em1",)
    ).fetchone()
    assert email_row is not None
    assert email_row["contact_id"] == "ct_canon"

    # Every surviving address is lowercase; the unrelated row is untouched.
    remaining = conn.execute("SELECT id, email FROM contact ORDER BY id").fetchall()
    assert {(r["id"], r["email"]) for r in remaining} == {
        ("ct_canon", "cthorne@example.com"),
        ("ct_other", "bob@example.com"),
    }


def test_migration_008_merges_colliding_enrollment_and_repoints_tasks(
    migration_schema: psycopg.Connection[dict[str, Any]],
):
    """§V.90/§B.121: when duplicate and canonical share a workflow, the duplicate
    enrollment collides on UNIQUE (workflow_id, contact_id). The migration
    repoints the duplicate enrollment's tasks onto the canonical enrollment, then
    drops the duplicate enrollment so the repoint never violates the constraint.
    """
    conn = migration_schema
    for prefix in ("001", "002", "003", "004", "005", "006", "007"):
        conn.execute(_read_shipped_migration(prefix))  # type: ignore[arg-type]
    conn.commit()

    conn.execute(
        "INSERT INTO account (id, email) VALUES (%s, %s)", ("ac1", "rep@lab5.ca")
    )
    conn.execute(
        "INSERT INTO workflow (id, account_id, template, type, name) "
        "VALUES (%s, %s, %s, %s, %s)",
        ("wf1", "ac1", "outbound-general", "outbound", "W"),
    )
    conn.execute(
        "INSERT INTO contact (id, email, created_at) VALUES (%s, %s, %s)",
        ("ct_canon", "dana@example.com", "2026-01-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO contact (id, email, created_at) VALUES (%s, %s, %s)",
        ("ct_dup", "Dana@Example.com", "2026-02-01T00:00:00Z"),
    )
    # Both contacts enrolled in the same workflow -> repoint would collide.
    conn.execute(
        "INSERT INTO enrollment (id, workflow_id, contact_id) VALUES (%s, %s, %s)",
        ("en_canon", "wf1", "ct_canon"),
    )
    conn.execute(
        "INSERT INTO enrollment (id, workflow_id, contact_id) VALUES (%s, %s, %s)",
        ("en_dup", "wf1", "ct_dup"),
    )
    conn.execute(
        "INSERT INTO task (id, enrollment_id, workflow_id, contact_id, description, "
        "scheduled_at) VALUES (%s, %s, %s, %s, %s, %s)",
        ("tk1", "en_dup", "wf1", "ct_dup", "follow up", "2026-03-01T00:00:00Z"),
    )
    conn.commit()

    conn.execute(_read_shipped_migration("008"))  # type: ignore[arg-type]
    conn.commit()

    # The duplicate enrollment is gone; the canonical one survives.
    enrollments = conn.execute(
        "SELECT id, contact_id FROM enrollment ORDER BY id"
    ).fetchall()
    assert [(r["id"], r["contact_id"]) for r in enrollments] == [
        ("en_canon", "ct_canon"),
    ]
    # The duplicate enrollment's task was repointed onto the canonical enrollment
    # and the canonical contact.
    task = conn.execute(
        "SELECT enrollment_id, contact_id FROM task WHERE id = %s", ("tk1",)
    ).fetchone()
    assert task is not None
    assert task["enrollment_id"] == "en_canon"
    assert task["contact_id"] == "ct_canon"


def test_migration_009_normalizes_names_and_swaps_constraint(
    migration_schema: psycopg.Connection[dict[str, Any]],
):
    """§V.90/§V.103/§V.108: migration 009 normalizes existing workflow names to
    kebab, drops the per-account ``(account_id, name)`` composite for a global
    ``UNIQUE (name)``, and adds the kebab CHECK.
    """
    conn = migration_schema
    for prefix in ("001", "002", "003", "004", "005", "006", "007", "008"):
        conn.execute(_read_shipped_migration(prefix))  # type: ignore[arg-type]
    conn.commit()

    conn.execute(
        "INSERT INTO account (id, email) VALUES (%s, %s)", ("ac1", "a@lab5.ca")
    )
    conn.execute(
        "INSERT INTO account (id, email) VALUES (%s, %s)", ("ac2", "b@lab5.ca")
    )
    # Pre-009 names carry uppercase + spaces, distinct apexes across accounts.
    conn.execute(
        "INSERT INTO workflow (id, account_id, template, type, name) "
        "VALUES (%s, %s, %s, %s, %s)",
        ("wf1", "ac1", "outbound-general", "outbound", "AI Engineering"),
    )
    conn.execute(
        "INSERT INTO workflow (id, account_id, template, type, name) "
        "VALUES (%s, %s, %s, %s, %s)",
        ("wf2", "ac2", "inbound-general", "inbound", "MailPilot Demo"),
    )
    conn.commit()

    conn.execute(_read_shipped_migration("009"))  # type: ignore[arg-type]
    conn.commit()

    # Names are kebab-normalized.
    names = conn.execute("SELECT id, name FROM workflow ORDER BY id").fetchall()
    assert {(r["id"], r["name"]) for r in names} == {
        ("wf1", "ai-engineering"),
        ("wf2", "mailpilot-demo"),
    }

    # The composite is gone; a global UNIQUE + kebab CHECK replace it. Scope to
    # this test schema's table via regclass -- a bare relname match would also
    # see workflow tables in other schemas (public, identity-test schemas).
    constraints = {
        r["conname"]
        for r in conn.execute(
            "SELECT con.conname FROM pg_constraint con "
            "WHERE con.conrelid = 'workflow'::regclass"
        ).fetchall()
    }
    assert "workflow_account_id_name_key" not in constraints
    assert "workflow_name_key" in constraints
    assert "workflow_name_check" in constraints

    # The kebab CHECK rejects a non-kebab insert.
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO workflow (id, account_id, template, type, name) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("wf3", "ac1", "outbound-general", "outbound", "Bad Name"),
        )
    conn.rollback()


# -- migration 010: touch-cadence def cols + legacy touch-task rewrite (§V.136) -


def test_migration_010_adds_cadence_cols_and_rewrites_touch_tasks(
    migration_schema: psycopg.Connection[dict[str, Any]],
):
    """§V.136/§V.108: migration 010 adds the nullable cadence pair
    (``touches``, ``touch_interval_days``) with a both-or-neither CHECK, then
    one-time rewrites every pending ``^Touch N of M`` task so its context
    carries ``touch = N`` -- the parse lives and dies in the migration. A
    completed touch task and a non-touch task are left untouched, and an
    existing context key survives the merge.
    """
    conn = migration_schema
    for prefix in ("001", "002", "003", "004", "005", "006", "007", "008", "009"):
        conn.execute(_read_shipped_migration(prefix))  # type: ignore[arg-type]
    conn.commit()

    conn.execute(
        "INSERT INTO account (id, email) VALUES (%s, %s)", ("ac1", "a@lab5.ca")
    )
    conn.execute(
        "INSERT INTO workflow (id, account_id, template, type, name) "
        "VALUES (%s, %s, %s, %s, %s)",
        ("wf1", "ac1", "outbound-general", "outbound", "ai-engineering"),
    )
    conn.execute(
        "INSERT INTO contact (id, email) VALUES (%s, %s)", ("ct1", "c@example.com")
    )
    conn.execute(
        "INSERT INTO enrollment (id, workflow_id, contact_id) VALUES (%s, %s, %s)",
        ("en1", "wf1", "ct1"),
    )

    def _seed_task(task_id: str, description: str, status: str, context: str) -> None:
        conn.execute(
            "INSERT INTO task (id, enrollment_id, workflow_id, contact_id, "
            "description, context, scheduled_at, status) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
            (
                task_id,
                "en1",
                "wf1",
                "ct1",
                description,
                context,
                "2026-08-01T00:00:00Z",
                status,
            ),
        )

    _seed_task("tk_t2", "Touch 2 of 3: value-add bump", "pending", "{}")
    _seed_task("tk_t3", "Touch 3 of 3: breakup", "pending", '{"prior_email_id": "em9"}')
    _seed_task(
        "tk_first",
        "scheduled first reach-out",
        "pending",
        '{"trigger": "enrollment_schedule"}',
    )
    _seed_task("tk_done", "Touch 2 of 3: value-add bump", "completed", "{}")
    conn.commit()

    # Pre-010 the cadence columns do not exist. Scope to this test schema --
    # a bare table_name match would also see the public workflow table the
    # session fixture provisions from the (already-cadenced) schema.sql.
    pre = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'workflow' "
        "AND column_name IN ('touches', 'touch_interval_days')"
    ).fetchall()
    assert pre == []

    conn.execute(_read_shipped_migration("010"))  # type: ignore[arg-type]
    conn.commit()

    # Columns exist, nullable, default NULL on the pre-existing row.
    row = conn.execute(
        "SELECT touches, touch_interval_days FROM workflow WHERE id = 'wf1'"
    ).fetchone()
    assert row is not None
    assert row["touches"] is None
    assert row["touch_interval_days"] is None

    # The both-or-neither CHECK rejects a half-configured cadence...
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE workflow SET touches = 3 WHERE id = 'wf1'")
    conn.rollback()
    # ...and a non-positive count...
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "UPDATE workflow SET touches = 0, touch_interval_days = 7 WHERE id = 'wf1'"
        )
    conn.rollback()
    # ...but accepts a full pair.
    conn.execute(
        "UPDATE workflow SET touches = 3, touch_interval_days = 7 WHERE id = 'wf1'"
    )
    conn.commit()

    # Pending ``Touch N of M`` tasks gained context.touch = N; an existing key
    # survives the JSONB merge. First-touch and completed tasks are untouched.
    contexts = {
        r["id"]: r["context"]
        for r in conn.execute(
            "SELECT id, context FROM task WHERE workflow_id = 'wf1'"
        ).fetchall()
    }
    assert contexts["tk_t2"] == {"touch": 2}
    assert contexts["tk_t3"] == {"prior_email_id": "em9", "touch": 3}
    assert contexts["tk_first"] == {"trigger": "enrollment_schedule"}
    assert contexts["tk_done"] == {}


# -- migration 015: drop leftover app_config.xai_api_host (§V.191) -------------


def test_migration_015_drops_xai_api_host(
    migration_schema: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.191: leftover app_config.xai_api_host is dropped."""
    conn = migration_schema
    conn.execute(
        """
        CREATE TABLE app_config (
            id TEXT PRIMARY KEY DEFAULT 'singleton' CHECK (id = 'singleton'),
            xai_api_host TEXT NOT NULL DEFAULT '',
            xai_api_key TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "INSERT INTO app_config (id, xai_api_host) "
        "VALUES ('singleton', 'gateway.example.com:443')"
    )
    conn.commit()

    pre = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'app_config' "
        "AND column_name = 'xai_api_host'"
    ).fetchone()
    assert pre is not None

    conn.execute(_read_shipped_migration("015"))  # type: ignore[arg-type]
    conn.commit()

    post = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'app_config' "
        "AND column_name = 'xai_api_host'"
    ).fetchone()
    assert post is None


# -- migration 016: workflow.touch_copy JSONB (§V.194) -------------------------


def test_migration_016_adds_touch_copy_jsonb(
    migration_schema: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.194/§V.108: migration 016 adds touch_copy JSONB default []."""
    conn = migration_schema
    for prefix in (
        "001",
        "002",
        "003",
        "004",
        "005",
        "006",
        "007",
        "008",
        "009",
        "010",
        "011",
        "012",
        "013",
        "014",
        "015",
    ):
        conn.execute(_read_shipped_migration(prefix))  # type: ignore[arg-type]
    conn.commit()

    conn.execute(
        "INSERT INTO account (id, email) VALUES (%s, %s)", ("ac1", "a@lab5.ca")
    )
    conn.execute(
        "INSERT INTO workflow (id, account_id, template, type, name) "
        "VALUES (%s, %s, %s, %s, %s)",
        ("wf1", "ac1", "outbound-general", "outbound", "ai-engineering"),
    )
    conn.commit()

    pre = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'workflow' "
        "AND column_name = 'touch_copy'"
    ).fetchall()
    assert pre == []

    conn.execute(_read_shipped_migration("016"))  # type: ignore[arg-type]
    conn.commit()

    row = conn.execute("SELECT touch_copy FROM workflow WHERE id = 'wf1'").fetchone()
    assert row is not None
    assert row["touch_copy"] == []


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
