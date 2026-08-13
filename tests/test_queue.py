"""Unit tests for show-queue formatting (§V.166)."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from mailpilot.queue import format_queue_touch, format_queue_when


def test_format_queue_when_overdue_days() -> None:
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    scheduled = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    assert format_queue_when(scheduled, now=now, tz=ZoneInfo("UTC")) == "overdue 2d"


def test_format_queue_when_today() -> None:
    now = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)
    scheduled = datetime(2026, 8, 13, 14, 0, tzinfo=UTC)
    assert format_queue_when(scheduled, now=now, tz=ZoneInfo("UTC")) == "today 14:00"


def test_format_queue_when_in_days() -> None:
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    scheduled = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    assert format_queue_when(scheduled, now=now, tz=ZoneInfo("UTC")) == "in 3d"


def test_format_queue_when_past_same_day_is_overdue() -> None:
    now = datetime(2026, 8, 13, 15, 0, tzinfo=UTC)
    scheduled = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)
    assert format_queue_when(scheduled, now=now, tz=ZoneInfo("UTC")) == "overdue 5h"


def test_format_queue_touch_t_label_and_empty() -> None:
    assert format_queue_touch({"touch": 2}) == "T2"
    assert format_queue_touch({"touch": "2"}) == "T2"
    assert format_queue_touch({"touch": "T2"}) == "T2"
    assert format_queue_touch({"touch": "oops"}) == ""
    assert format_queue_touch(None) == ""
    assert format_queue_touch({}) == ""
