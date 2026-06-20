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
    update_workflow,
)

_EXPORT_FIELDS = ("name", "template", "objective", "instructions", "theme")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _seed_workflows(
    connection: psycopg.Connection[dict[str, Any]], account_id: str
) -> None:
    """Three workflows: active outbound, draft inbound, active KB-grounded."""
    alpha = create_workflow(
        connection,
        name="Alpha outbound",
        template="outbound-general",
        account_id=account_id,
        theme="green",
    )
    assert alpha is not None
    update_workflow(
        connection,
        alpha.id,
        objective="Book demos with mid-market accounts.",
        instructions="You are a courteous sales rep.\nCite every figure.\n",
    )
    activate_workflow(connection, alpha.id)

    create_workflow(
        connection,
        name="Bravo inbound draft",
        template="inbound-general",
        account_id=account_id,
        theme="blue",
    )

    charlie = create_workflow(
        connection,
        name="Charlie KB",
        template="inbound-google-drive",
        account_id=account_id,
        theme="purple",
    )
    assert charlie is not None
    update_workflow(
        connection,
        charlie.id,
        objective="Answer support questions grounded in the KB.",
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
        "Alpha outbound",
        "Bravo inbound draft",
        "Charlie KB",
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


def test_workflow_export_toml_excludes_denormalized_fields(
    runner: CliRunner,
    database_connection: psycopg.Connection[dict[str, Any]],
    tmp_path: pathlib.Path,
) -> None:
    """§V.103 round-trip purity: exported TOML carries only the def fields.

    ``account_email`` (§V.5 parent-NI denorm) and ``account_id`` are read-only
    view fields; an exported catalog entry that carried them would attempt to
    write a non-existent / wrong column on re-import. Every ``*.toml`` must parse
    to exactly ``{name, template, theme, objective, instructions}``.
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
