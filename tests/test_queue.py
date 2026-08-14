"""Unit tests for show-queue formatting (§V.166)."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from mailpilot.queue import (
    format_queue_next_at,
    format_queue_touch,
    queue_table_cells,
    queue_table_headers,
)


def test_format_queue_next_at_is_iso_in_tz() -> None:
    utc = ZoneInfo("UTC")
    toronto = ZoneInfo("America/Toronto")
    assert format_queue_next_at(None, tz=utc) == ""
    assert (
        format_queue_next_at(datetime(2026, 8, 13, 14, 30, tzinfo=UTC), tz=utc)
        == "2026-08-13T14:30:00+00:00"
    )
    # 02:00 UTC is still the previous calendar day in Toronto (UTC-4 in August).
    assert (
        format_queue_next_at(datetime(2026, 8, 14, 2, 0, tzinfo=UTC), tz=toronto)
        == "2026-08-13T22:00:00-04:00"
    )
    assert (
        format_queue_next_at("2026-08-14T02:00:00+00:00", tz=toronto)
        == "2026-08-13T22:00:00-04:00"
    )


def test_queue_table_cells_next_at_is_iso() -> None:
    row = {
        "workflow_name": "alpha-outreach",
        "status": "active",
        "t1": 1,
        "t2": 0,
        "t3": 0,
        "t4p": 0,
        "next_at": "2026-08-13T14:30:00+00:00",
    }
    cells = queue_table_cells(row, detail=False, tz=ZoneInfo("UTC"))
    assert cells == [
        "alpha-outreach",
        "active",
        "1",
        "0",
        "0",
        "0",
        "2026-08-13T14:30:00+00:00",
    ]


def test_queue_table_cells_detail_cols_and_next_at_iso() -> None:
    """§V.166: --detail table cols + next_at ISO in --tz."""
    row = {
        "workflow_name": "alpha-outreach",
        "company_domain": "acme.com",
        "contact": "Ada Lovelace",
        "email": "ada@acme.com",
        "touch": "T2",
        "attempts": 0,
        "next_at": "2026-08-14T02:00:00+00:00",
        "task_id": "01234567-0000-7000-0000-000000000099",
        "when": "in 3d",
        "trigger": "task",
        "state": "pending",
    }
    headers = queue_table_headers(detail=True)
    assert headers == (
        "workflow_name",
        "company_domain",
        "contact",
        "email",
        "touch",
        "attempts",
        "next_at",
    )
    assert "when" not in headers
    assert "trigger" not in headers
    assert "state" not in headers
    cells = queue_table_cells(row, detail=True, tz=ZoneInfo("America/Toronto"))
    assert cells == [
        "alpha-outreach",
        "acme.com",
        "Ada Lovelace",
        "ada@acme.com",
        "T2",
        "0",
        "2026-08-13T22:00:00-04:00",
    ]


def test_format_queue_touch_t_label_and_empty() -> None:
    assert format_queue_touch({"touch": 2}) == "T2"
    assert format_queue_touch({"touch": "2"}) == "T2"
    assert format_queue_touch({"touch": "T2"}) == "T2"
    assert format_queue_touch({"touch": "oops"}) == ""
    assert format_queue_touch(None) == ""
    assert format_queue_touch({}) == ""


def test_format_queue_touch_first_reach_fallback() -> None:
    """§V.162 / §V.166: enrollment_schedule with no touch renders T1."""
    assert format_queue_touch({"trigger": "enrollment_schedule"}) == "T1"
    assert format_queue_touch({}, "enrollment_schedule") == "T1"
    assert format_queue_touch({"touch": "oops"}, "enrollment_schedule") == "T1"
    assert format_queue_touch({"touch": 2, "trigger": "enrollment_schedule"}) == "T2"
