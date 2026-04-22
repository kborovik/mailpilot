"""ADR-07 span-emission contract tests for database.py.

Manual ``db.*`` CRUD spans were removed in favour of ``instrument_psycopg``
auto-instrumentation.  These tests verify the remaining hand-written spans
(``db.schema.apply``, ``database connection failed``) still emit correctly.
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

    schema_spans = _db_spans(capfire, "db.schema.apply")
    assert len(schema_spans) == 1
    assert any(e["name"] == "exception" for e in schema_spans[0].get("events", []))
