"""ADR-07 span-emission contract tests for database.py.

These tests lock the span naming and attribute conventions defined in
``docs/adr-07-observability-with-logfire.md``. They cover one function per
pattern shape (create / get-hit / get-miss / list / search / update /
update-noop / error) rather than every CRUD function, since all functions
within a shape share the same wrapper.
"""

import json
from typing import Any

import psycopg
import pytest
from logfire.testing import CaptureLogfire

from conftest import make_test_account, make_test_company, make_test_contact
from mailpilot.database import (
    get_account,
    initialize_database,
    list_companies,
    search_companies,
    update_account,
    update_contact,
)


def _db_spans(capfire: CaptureLogfire, name: str) -> list[dict[str, Any]]:
    """Return exported spans with the given name."""
    return [
        span
        for span in capfire.exporter.exported_spans_as_dict()
        if span["name"] == name
    ]


def test_create_span_sets_entity_id(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    spans = _db_spans(capfire, "db.account.create")
    assert len(spans) == 1
    attrs = spans[0]["attributes"]
    assert attrs["email"] == "test@example.com"
    assert attrs["account_id"] == account.id


def test_get_span_hit_true_when_found(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    get_account(database_connection, account.id)
    spans = _db_spans(capfire, "db.account.get")
    assert len(spans) == 1
    assert spans[0]["attributes"]["hit"] is True


def test_get_span_hit_false_when_missing(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
):
    get_account(database_connection, "does-not-exist")
    spans = _db_spans(capfire, "db.account.get")
    assert len(spans) == 1
    assert spans[0]["attributes"]["hit"] is False


def test_list_span_sets_count(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
):
    make_test_company(database_connection, name="A", domain="a.com")
    make_test_company(database_connection, name="B", domain="b.com")
    list_companies(database_connection)
    spans = _db_spans(capfire, "db.company.list")
    assert len(spans) == 1
    assert spans[0]["attributes"]["company_count"] == 2


def test_search_span_sets_count(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
):
    make_test_company(database_connection, name="Acme", domain="acme.com")
    make_test_company(database_connection, name="Other", domain="other.com")
    search_companies(database_connection, query="acme")
    spans = _db_spans(capfire, "db.company.search")
    assert len(spans) == 1
    attrs = spans[0]["attributes"]
    assert attrs["query"] == "acme"
    assert attrs["company_count"] == 1


def test_update_span_sets_updated_fields_sorted(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
):
    contact = make_test_contact(database_connection)
    update_contact(
        database_connection,
        contact.id,
        last_name="Z",
        first_name="A",
    )
    spans = _db_spans(capfire, "db.contact.update")
    assert len(spans) == 1
    attrs = spans[0]["attributes"]
    assert json.loads(attrs["updated_fields"]) == ["first_name", "last_name"]
    assert attrs["hit"] is True


def test_update_noop_still_emits_span(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Empty-update calls must emit a span so the call-site is discoverable."""
    account = make_test_account(database_connection)
    update_account(database_connection, account.id)
    spans = _db_spans(capfire, "db.account.update")
    assert len(spans) == 1
    assert json.loads(spans[0]["attributes"]["updated_fields"]) == []


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
