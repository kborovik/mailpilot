"""Live E2E skills gate on logfire_environment == development (§V.165)."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SKILLS = (
    _REPO / ".claude" / "skills" / "mailpilot-campaign-test" / "scripts",
    _REPO / ".claude" / "skills" / "mailpilot-reply-test" / "scripts",
)


def _load_preflight(scripts: Path) -> types.ModuleType:
    saved_common = sys.modules.pop("_common", None)
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location(
        f"preflight_{scripts.parent.name}", scripts / "preflight.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(scripts))
        sys.modules.pop("_common", None)
        if saved_common is not None:
            sys.modules["_common"] = saved_common
    return module


@pytest.fixture(params=_SKILLS, ids=["campaign-test", "reply-test"])
def preflight(request: pytest.FixtureRequest) -> types.ModuleType:
    return _load_preflight(request.param)


def test_required_environment_is_development(preflight: types.ModuleType) -> None:
    assert preflight.REQUIRED_LOGFIRE_ENVIRONMENT == "development"


def test_development_passes(preflight: types.ModuleType) -> None:
    result: dict[str, object] = {}
    issues: list[str] = []
    preflight._check_logfire_environment("development", result, issues)
    assert result["logfire_environment"] == "development"
    assert result["logfire_environment_ok"] is True
    assert issues == []


def test_production_blocks(preflight: types.ModuleType) -> None:
    result: dict[str, object] = {}
    issues: list[str] = []
    preflight._check_logfire_environment("production", result, issues)
    assert result["logfire_environment"] == "production"
    assert result["logfire_environment_ok"] is False
    assert issues
    assert "logfire_environment" in issues[0]
    assert "development" in issues[0]


@pytest.mark.parametrize("value", [None, "", "staging"])
def test_non_development_blocks(preflight: types.ModuleType, value: object) -> None:
    result: dict[str, object] = {}
    issues: list[str] = []
    preflight._check_logfire_environment(value, result, issues)
    assert result["logfire_environment_ok"] is False
    assert any("logfire_environment" in i for i in issues)
