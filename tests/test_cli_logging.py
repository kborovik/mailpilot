"""Console-exporter routing contract test for ``configure_logging`` (§V.3).

§V.3: stdout = strict JSON only (all flags, incl ``--debug``); the Logfire
console exporter must target stderr (``ConsoleOptions(output=sys.stderr)``),
never stdout. Regression guard for §B.73: ``configure_logging`` left the
console ``output`` stream unset, so it defaulted to stdout and ``logfire.warn``
lines (e.g. the schema-drift warning from ``database.py``) printed ahead of the
JSON envelope -- ``json.loads`` over CLI stdout then failed with "Extra data".
"""

from __future__ import annotations

import json
from collections.abc import Callable

import logfire
import pytest

from conftest import make_test_settings
from mailpilot.cli import configure_logging, output


@pytest.mark.parametrize(
    ("debug", "emit", "marker"),
    [
        (
            False,
            lambda: logfire.warn("schema drift detected", current_hash="abc123"),
            "schema drift detected",
        ),
        (
            True,
            lambda: logfire.debug("console diagnostic line"),
            "console diagnostic line",
        ),
    ],
)
def test_console_exporter_writes_stderr_not_stdout(
    debug: bool,
    emit: Callable[[], object],
    marker: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Console diagnostic lines land on stderr; stdout stays clean JSON."""
    test_settings = make_test_settings()
    monkeypatch.setattr("mailpilot.settings.get_settings", lambda: test_settings)

    configure_logging(debug=debug)
    emit()
    output({"company": []})

    captured = capsys.readouterr()

    parsed = json.loads(captured.out)
    assert parsed["ok"] is True
    assert marker in captured.err
    assert marker not in captured.out


@pytest.mark.parametrize(
    ("target", "logfire_env"),
    [("dev", "development"), ("prd", "production")],
)
def test_configure_logging_maps_environment_internally(
    target: str,
    logfire_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§V.52 / §V.176: logfire.configure maps dev→development, prd→production."""
    captured: list[object] = []
    real_configure = logfire.configure

    def wrap(*args: object, **kwargs: object) -> object:
        captured.append(kwargs.get("environment"))
        return real_configure(*args, **kwargs)

    monkeypatch.setattr(logfire, "configure", wrap)
    monkeypatch.setattr(
        "mailpilot.settings.get_settings",
        lambda: make_test_settings(environment=target),
    )
    configure_logging()
    assert captured
    assert captured[-1] == logfire_env
