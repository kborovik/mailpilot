"""Live E2E skills gate on environment == dev (§V.165 / §V.176)."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SKILLS = (
    _REPO / ".grok" / "skills" / "mailpilot-campaign-test" / "scripts",
    _REPO / ".grok" / "skills" / "mailpilot-reply-test" / "scripts",
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


def test_required_environment_is_dev(preflight: types.ModuleType) -> None:
    assert preflight.REQUIRED_ENVIRONMENT == "dev"


def test_dev_passes(preflight: types.ModuleType) -> None:
    result: dict[str, object] = {}
    issues: list[str] = []
    preflight._check_environment("dev", result, issues)
    assert result["environment"] == "dev"
    assert result["environment_ok"] is True
    assert result["logfire_environment"] == "development"
    assert issues == []


def test_prd_blocks(preflight: types.ModuleType) -> None:
    result: dict[str, object] = {}
    issues: list[str] = []
    preflight._check_environment("prd", result, issues)
    assert result["environment"] == "prd"
    assert result["environment_ok"] is False
    assert result["logfire_environment"] == "production"
    assert issues
    assert "environment" in issues[0]
    assert "dev" in issues[0]


@pytest.mark.parametrize("value", [None, "", "staging", "production", "development"])
def test_non_dev_blocks(preflight: types.ModuleType, value: object) -> None:
    result: dict[str, object] = {}
    issues: list[str] = []
    preflight._check_environment(value, result, issues)
    assert result["environment_ok"] is False
    assert any("environment" in i for i in issues)
