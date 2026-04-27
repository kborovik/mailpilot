"""Tests for the operator_log module."""

from __future__ import annotations

import re
from typing import Any

import pytest

from mailpilot.operator_log import operator_event


def test_emits_single_line_with_event_and_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    operator_event("loop.tick", iteration=3, wakeup="event", full_sweep=True)
    captured = capsys.readouterr()
    assert captured.err == ""
    line = captured.out.rstrip("\n")
    assert "\n" not in line
    assert re.match(r"^\d{2}:\d{2}:\d{2} event=loop\.tick ", line)
    assert "iteration=3" in line
    assert "wakeup=event" in line
    assert "full_sweep=True" in line


def test_quotes_values_with_spaces(capsys: pytest.CaptureFixture[str]) -> None:
    operator_event("error", source="sync_account", message="Gmail timeout 504")
    line = capsys.readouterr().out.rstrip("\n")
    assert 'message="Gmail timeout 504"' in line


def test_no_fields_emits_just_event(capsys: pytest.CaptureFixture[str]) -> None:
    operator_event("loop.stop")
    line = capsys.readouterr().out.rstrip("\n")
    assert re.match(r"^\d{2}:\d{2}:\d{2} event=loop\.stop$", line)


def test_flushes_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    flushed = {"called": False}

    class FakeStream:
        def write(self, text: str) -> int:
            return len(text)

        def flush(self) -> None:
            flushed["called"] = True

    monkeypatch.setattr("sys.stdout", FakeStream())
    operator_event("loop.tick", iteration=1)
    assert flushed["called"] is True


def test_escapes_double_quotes_in_quoted_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    operator_event("error", source="x", message='He said "hi"')
    line = capsys.readouterr().out.rstrip("\n")
    assert 'message="He said \\"hi\\""' in line


def test_collapses_newlines_to_spaces(
    capsys: pytest.CaptureFixture[str],
) -> None:
    operator_event("error", source="x", message="line one\nline two\rline three")
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert '"line one line two line three"' in out


def test_accepts_non_string_field_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload: dict[str, Any] = {"count": 42, "ratio": 0.5, "flag": False}
    operator_event("metric", **payload)
    line = capsys.readouterr().out.rstrip("\n")
    assert "count=42" in line
    assert "ratio=0.5" in line
    assert "flag=False" in line
