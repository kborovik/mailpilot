"""Operator-log emissions from the run loop's account-sync error path."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest

from conftest import make_test_account, make_test_settings


def test_sync_all_accounts_emits_error_on_account_failure(
    capsys: pytest.CaptureFixture[str],
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """logfire.exception at run.sync.account_failed pairs with operator event=error."""
    from mailpilot.run import _sync_all_accounts  # pyright: ignore[reportPrivateUsage]

    make_test_account(database_connection, email="boom@example.com")

    monkeypatch.setattr("mailpilot.run.GmailClient", lambda *_a, **_k: MagicMock())

    def _explode(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("Gmail timeout 504")

    monkeypatch.setattr("mailpilot.run.sync_account", _explode)

    _sync_all_accounts(database_connection, make_test_settings())

    err = capsys.readouterr().err
    assert "event=error" in err
    assert "source=run.sync.account_failed" in err
    assert 'message="Gmail timeout 504"' in err
