"""ADR-07 span-emission contract tests for database.py.

Verifies that ``database connection failed`` error log is emitted when
the database connection fails (e.g. database does not exist).
"""

from typing import Any

import pytest
from logfire.testing import CaptureLogfire

from mailpilot.database import initialize_database


def _db_spans(capfire: CaptureLogfire, name: str) -> list[dict[str, Any]]:
    """Return exported spans with the given name."""
    return [
        span
        for span in capfire.exporter.exported_spans_as_dict()
        if span["name"] == name
    ]


def test_initialize_database_error_emits_error_span(capfire: CaptureLogfire):
    with pytest.raises(SystemExit):
        initialize_database("postgresql://localhost/mailpilot_does_not_exist_xyz")

    error_spans = _db_spans(capfire, "database connection failed")
    assert len(error_spans) == 1
    attrs = error_spans[0]["attributes"]
    assert attrs["database"] == "mailpilot_does_not_exist_xyz"
    assert "createdb" in attrs["hint"]
    assert attrs["logfire.span_type"] == "log"
