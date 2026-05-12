"""V39 round-trip integration tests for ``workflow export`` / ``workflow import``.

Uses the real ``database_connection`` fixture and the Click ``CliRunner`` to
exercise the end-to-end declarative flow against a Postgres instance. The
mock-based unit tests live in ``tests/test_cli.py``; this file owns the
seed -> export -> truncate -> import -> diff loop and the idempotence
assertion.
"""

from __future__ import annotations

import json
import pathlib
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
    update_workflow(
        connection,
        alpha.id,
        objective="Book demos with mid-market accounts.",
        instructions="You are a courteous sales rep.",
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


def test_workflow_export_import_round_trip_and_idempotence(
    runner: CliRunner,
    database_connection: psycopg.Connection[dict[str, Any]],
    tmp_path: pathlib.Path,
) -> None:
    """V39: declarative round-trip preserves payload and second import is a no-op.

    1. seed three workflows (mixed templates / themes / statuses)
    2. ``workflow export`` -> capture payload
    3. truncate workflow rows for the account
    4. ``workflow import`` from the captured payload -> all ``created``
    5. re-export -> payload byte-equal to original
    6. re-import on unchanged DB -> all ``unchanged``, ``update_workflow`` not called
    """
    account = make_test_account(database_connection)
    _seed_workflows(database_connection, account.id)

    original = _invoke(
        runner, database_connection, ["workflow", "export", "--account-id", account.id]
    )["workflows"]
    assert len(original) == 3

    database_connection.execute(
        "DELETE FROM workflow WHERE account_id = %s", (account.id,)
    )
    database_connection.commit()

    payload_file = tmp_path / "workflows.json"
    payload_file.write_text(json.dumps(original))
    import_result = _invoke(
        runner,
        database_connection,
        [
            "workflow",
            "import",
            "--account-id",
            account.id,
            "--file",
            str(payload_file),
        ],
    )
    assert all(row["action"] == "created" for row in import_result["workflows"])

    round_tripped = _invoke(
        runner, database_connection, ["workflow", "export", "--account-id", account.id]
    )["workflows"]
    assert round_tripped == original
    for row in round_tripped:
        assert set(row.keys()) == set(_EXPORT_FIELDS)

    with patch("mailpilot.database.update_workflow") as mock_update:
        idempotent = _invoke(
            runner,
            database_connection,
            [
                "workflow",
                "import",
                "--account-id",
                account.id,
                "--file",
                str(payload_file),
            ],
        )
    assert all(row["action"] == "unchanged" for row in idempotent["workflows"])
    mock_update.assert_not_called()
