"""§V.63/§V.103 round-trip integration for ``workflow export`` / ``workflow import``.

Uses the real ``database_connection`` fixture and the Click ``CliRunner`` to
exercise the end-to-end declarative flow against a Postgres instance. The
mock-based unit tests live in ``tests/test_cli.py``; this file owns the
seed -> export-to-dir -> truncate -> import-from-dir -> re-export -> diff loop
and the idempotence assertion. Export/import is TOML-only per §V.103.
"""

from __future__ import annotations

import json
import pathlib
import tomllib
from typing import Any
from unittest.mock import patch

import psycopg
import pytest
from click.testing import CliRunner

from conftest import make_test_account, make_test_settings
from mailpilot.cli import main
from mailpilot.database import (
    activate_workflow,
    create_workflow,
    get_workflow_by_name,
    update_workflow,
)

_EXPORT_FIELDS = ("name", "template", "goal", "instructions", "theme")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _seed_workflows(
    connection: psycopg.Connection[dict[str, Any]], account_id: str
) -> None:
    """Three workflows: active outbound, draft inbound, active KB-grounded."""
    alpha = create_workflow(
        connection,
        name="alpha-outbound",
        template="outbound-general",
        account_id=account_id,
        theme="green",
    )
    assert alpha is not None
    update_workflow(
        connection,
        alpha.id,
        goal="Book demos with mid-market accounts.",
        instructions="You are a courteous sales rep.\nCite every figure.\n",
    )
    activate_workflow(connection, alpha.id)

    create_workflow(
        connection,
        name="bravo-inbound-draft",
        template="inbound-general",
        account_id=account_id,
        theme="blue",
    )

    charlie = create_workflow(
        connection,
        name="charlie-kb",
        template="inbound-google-drive",
        account_id=account_id,
        theme="purple",
    )
    assert charlie is not None
    update_workflow(
        connection,
        charlie.id,
        goal="Answer support questions grounded in the KB.",
        instructions="Cite the source markdown file in every reply.",
    )
    activate_workflow(connection, charlie.id)


class _NonClosingConnection:
    """Wrapper passed to the CLI in place of ``initialize_database()``'s output.

    The CLI calls ``connection.close()`` in a ``finally`` block, but we need to
    keep the underlying ``database_connection`` open across multiple ``runner``
    invocations within a single test. Delegating everything except ``close``
    leaves teardown to the real fixture.
    """

    def __init__(self, conn: psycopg.Connection[dict[str, Any]]) -> None:
        self._conn = conn

    def close(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def _invoke(
    runner: CliRunner,
    connection: psycopg.Connection[dict[str, Any]],
    args: list[str],
) -> dict[str, Any]:
    proxy = _NonClosingConnection(connection)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=proxy),
    ):
        result = runner.invoke(main, args)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _read_catalog(directory: pathlib.Path) -> dict[str, str]:
    """Map ``filename -> file text`` for every ``*.toml`` in ``directory``."""
    return {p.name: p.read_text() for p in sorted(directory.glob("*.toml"))}


def test_workflow_export_import_round_trip_and_idempotence(
    runner: CliRunner,
    database_connection: psycopg.Connection[dict[str, Any]],
    tmp_path: pathlib.Path,
) -> None:
    """§V.63/§V.103: TOML round-trip preserves files and a second import is a no-op.

    1. seed three workflows (mixed templates / themes / statuses)
    2. ``workflow export --out-dir`` -> one ``*.toml`` per workflow
    3. truncate workflow rows for the account
    4. ``workflow import --file <dir>`` -> all ``created``
    5. re-export to a fresh dir -> catalog byte-equal to the original
    6. re-import on unchanged DB -> all ``unchanged``, ``update_workflow`` not called
    """
    account = make_test_account(database_connection)
    _seed_workflows(database_connection, account.id)

    out_one = tmp_path / "export_one"
    export = _invoke(
        runner,
        database_connection,
        [
            "workflow",
            "export",
            "--account-email",
            account.id,
            "--out-dir",
            str(out_one),
        ],
    )
    assert len(export["workflows"]) == 3
    assert [row["name"] for row in export["workflows"]] == [
        "alpha-outbound",
        "bravo-inbound-draft",
        "charlie-kb",
    ]
    original_catalog = _read_catalog(out_one)
    assert len(original_catalog) == 3

    database_connection.execute(
        "DELETE FROM workflow WHERE account_id = %s", (account.id,)
    )
    database_connection.commit()

    import_result = _invoke(
        runner,
        database_connection,
        ["workflow", "import", "--account-email", account.id, "--file", str(out_one)],
    )
    assert all(row["action"] == "created" for row in import_result["workflows"])

    out_two = tmp_path / "export_two"
    _invoke(
        runner,
        database_connection,
        [
            "workflow",
            "export",
            "--account-email",
            account.id,
            "--out-dir",
            str(out_two),
        ],
    )
    assert _read_catalog(out_two) == original_catalog

    with patch("mailpilot.database.update_workflow") as mock_update:
        idempotent = _invoke(
            runner,
            database_connection,
            [
                "workflow",
                "import",
                "--account-email",
                account.id,
                "--file",
                str(out_one),
            ],
        )
    assert all(row["action"] == "unchanged" for row in idempotent["workflows"])
    mock_update.assert_not_called()


def _write_minimal_workflow_toml(path: pathlib.Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'name = "{name}"\ntemplate = "outbound-general"\n')


def test_workflow_check_slug_dir_excludes_other_account_workflows(
    runner: CliRunner,
    database_connection: psycopg.Connection[dict[str, Any]],
    tmp_path: pathlib.Path,
) -> None:
    """§V.134: --file campaigns/<slug>/workflows/ reports that slug only.

    A second account's workflow and an unpassed same-account workflow stay
    out of the path-scoped envelope. --account-email + --file restores
    that account's orphans.
    """
    account_a = make_test_account(database_connection, email="slug-a@example.com")
    account_b = make_test_account(database_connection, email="slug-b@example.com")
    create_workflow(
        database_connection,
        name="slug-a",
        template="outbound-general",
        account_id=account_a.id,
    )
    create_workflow(
        database_connection,
        name="other-a",
        template="outbound-general",
        account_id=account_a.id,
    )
    create_workflow(
        database_connection,
        name="slug-b",
        template="outbound-general",
        account_id=account_b.id,
    )

    campaigns = tmp_path / "campaigns"
    slug_a_dir = campaigns / "slug-a" / "workflows"
    slug_b_dir = campaigns / "slug-b" / "workflows"
    _write_minimal_workflow_toml(slug_a_dir / "slug-a.toml", "slug-a")
    _write_minimal_workflow_toml(slug_b_dir / "slug-b.toml", "slug-b")

    slug_only = _invoke(
        runner,
        database_connection,
        ["workflow", "check", "--file", str(slug_a_dir)],
    )
    slug_names = {row["name"] for row in slug_only["workflow_check"]["workflows"]}
    assert slug_names == {"slug-a"}
    assert slug_only["workflow_check"]["orphaned"] == 0

    tree = _invoke(
        runner,
        database_connection,
        ["workflow", "check", "--file", str(campaigns)],
    )
    tree_names = {row["name"] for row in tree["workflow_check"]["workflows"]}
    assert tree_names == {"slug-a", "slug-b"}
    assert tree["workflow_check"]["orphaned"] == 0
    assert "other-a" not in tree_names

    full = _invoke(
        runner,
        database_connection,
        [
            "workflow",
            "check",
            "--file",
            str(campaigns),
            "--account-email",
            account_a.email,
        ],
    )
    full_by_name = {
        row["name"]: row["state"] for row in full["workflow_check"]["workflows"]
    }
    assert full_by_name["slug-a"] == "in_sync"
    assert full_by_name["other-a"] == "orphaned"
    assert full_by_name["slug-b"] == "not_imported"
    assert full["workflow_check"]["orphaned"] == 1


def test_workflow_export_toml_excludes_denormalized_fields(
    runner: CliRunner,
    database_connection: psycopg.Connection[dict[str, Any]],
    tmp_path: pathlib.Path,
) -> None:
    """§V.103 round-trip purity: exported TOML carries only the def fields.

    ``account_email`` (§V.5 parent-NI denorm) and ``account_id`` are read-only
    view fields; an exported catalog entry that carried them would attempt to
    write a non-existent / wrong column on re-import. Every ``*.toml`` must parse
    to exactly ``{name, template, theme, goal, instructions}``.
    """
    account = make_test_account(database_connection)
    _seed_workflows(database_connection, account.id)
    out_dir = tmp_path / "catalog"
    _invoke(
        runner,
        database_connection,
        [
            "workflow",
            "export",
            "--account-email",
            account.id,
            "--out-dir",
            str(out_dir),
        ],
    )
    for path in sorted(out_dir.glob("*.toml")):
        with path.open("rb") as handle:
            parsed = tomllib.load(handle)
        assert set(parsed.keys()) == set(_EXPORT_FIELDS)


def test_workflow_cadence_fields_round_trip(
    runner: CliRunner,
    database_connection: psycopg.Connection[dict[str, Any]],
    tmp_path: pathlib.Path,
) -> None:
    """§V.103/§V.136: the cadence pair exports as bare TOML ints, re-imports onto
    the row, and round-trips idempotently; a single-touch (NULL cadence)
    workflow omits both keys entirely.
    """
    account = make_test_account(database_connection)

    cadenced = create_workflow(
        database_connection,
        name="cadence-flow",
        template="outbound-general",
        account_id=account.id,
        theme="green",
    )
    assert cadenced is not None
    update_workflow(
        database_connection,
        cadenced.id,
        goal="Book demos.",
        instructions="Be brief.\n",
        touches=3,
        touch_interval_days=7,
    )
    activate_workflow(database_connection, cadenced.id)

    single = create_workflow(
        database_connection,
        name="single-flow",
        template="outbound-general",
        account_id=account.id,
    )
    assert single is not None
    update_workflow(
        database_connection, single.id, goal="Say hi.", instructions="Once."
    )
    activate_workflow(database_connection, single.id)

    out_dir = tmp_path / "cat"
    _invoke(
        runner,
        database_connection,
        [
            "workflow",
            "export",
            "--account-email",
            account.id,
            "--out-dir",
            str(out_dir),
        ],
    )
    cadence_toml = (out_dir / "cadence-flow.toml").read_text()
    assert "touches = 3" in cadence_toml
    assert "touch_interval_days = 7" in cadence_toml
    # A single-touch workflow carries neither cadence key.
    single_toml = (out_dir / "single-flow.toml").read_text()
    assert "touches" not in single_toml
    assert "touch_interval_days" not in single_toml

    database_connection.execute(
        "DELETE FROM workflow WHERE account_id = %s", (account.id,)
    )
    database_connection.commit()

    _invoke(
        runner,
        database_connection,
        ["workflow", "import", "--account-email", account.id, "--file", str(out_dir)],
    )
    restored = get_workflow_by_name(database_connection, "cadence-flow")
    assert restored is not None
    assert restored.touches == 3
    assert restored.touch_interval_days == 7
    restored_single = get_workflow_by_name(database_connection, "single-flow")
    assert restored_single is not None
    assert restored_single.touches is None
    assert restored_single.touch_interval_days is None

    out_two = tmp_path / "cat2"
    _invoke(
        runner,
        database_connection,
        [
            "workflow",
            "export",
            "--account-email",
            account.id,
            "--out-dir",
            str(out_two),
        ],
    )
    assert _read_catalog(out_two) == _read_catalog(out_dir)
