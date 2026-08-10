"""Campaign-test preflight enforces outbound@lab5.ca identity (§V.151 / §V.122)."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / ".claude"
    / "skills"
    / "mailpilot-campaign-test"
    / "scripts"
)


def _load(name: str) -> types.ModuleType:
    path = _SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Sibling imports inside scripts use bare `from _common import ...`.
    sys.path.insert(0, str(_SCRIPTS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(_SCRIPTS))
    return module


@pytest.fixture(scope="module")
def preflight() -> types.ModuleType:
    return _load("preflight")


@pytest.fixture(scope="module")
def common() -> types.ModuleType:
    return _load("_common")


def test_required_outbound_signature_fields(common: types.ModuleType) -> None:
    required = common.REQUIRED_OUTBOUND_SIGNATURE
    assert required == {
        "full_name": "Konstantin Borovik",
        "title": "DevOps Engineer",
        "website": "https://lab5.ca",
        "phone": "416-670-0621",
    }
    assert common.REQUIRED_OUTBOUND_DISPLAY_NAME == "Konstantin Borovik"
    # Spelling guard: never the truncated Borovi form.
    assert "Borovi" not in required["full_name"] or required["full_name"].endswith("k")
    assert required["full_name"].endswith("Borovik")


def test_check_outbound_signature_missing_blocks(preflight: types.ModuleType) -> None:
    result: dict[str, object] = {}
    issues: list[str] = []
    preflight._check_outbound_signature(
        {"id": "x", "email": "outbound@lab5.ca", "signature": None},
        result,
        issues,
    )
    assert result["outbound_signature_ok"] is False
    assert any("signature missing" in i for i in issues)


def test_check_outbound_signature_mismatch_blocks(
    preflight: types.ModuleType, common: types.ModuleType
) -> None:
    result: dict[str, object] = {}
    issues: list[str] = []
    bad = dict(common.REQUIRED_OUTBOUND_SIGNATURE)
    bad["full_name"] = "Wrong"
    preflight._check_outbound_signature(
        {"id": "x", "email": "outbound@lab5.ca", "signature": bad},
        result,
        issues,
    )
    assert result["outbound_signature_ok"] is False
    assert any("signature mismatch" in i for i in issues)


def test_check_outbound_signature_match_ok(
    preflight: types.ModuleType, common: types.ModuleType
) -> None:
    result: dict[str, object] = {}
    issues: list[str] = []
    preflight._check_outbound_signature(
        {
            "id": "x",
            "email": "outbound@lab5.ca",
            "signature": dict(common.REQUIRED_OUTBOUND_SIGNATURE),
        },
        result,
        issues,
    )
    assert result["outbound_signature_ok"] is True
    assert issues == []


def test_check_outbound_display_name_missing_blocks(
    preflight: types.ModuleType, common: types.ModuleType
) -> None:
    result: dict[str, object] = {}
    issues: list[str] = []
    preflight._check_outbound_display_name(
        {"id": "x", "email": "outbound@lab5.ca", "display_name": None},
        result,
        issues,
    )
    assert result["outbound_display_name_ok"] is False
    assert any("display_name" in i for i in issues)


def test_check_outbound_display_name_mismatch_blocks(
    preflight: types.ModuleType, common: types.ModuleType
) -> None:
    result: dict[str, object] = {}
    issues: list[str] = []
    preflight._check_outbound_display_name(
        {
            "id": "x",
            "email": "outbound@lab5.ca",
            "display_name": "MailPilot Outbound",
        },
        result,
        issues,
    )
    assert result["outbound_display_name_ok"] is False
    assert any("display_name" in i for i in issues)
    assert common.REQUIRED_OUTBOUND_DISPLAY_NAME in issues[0]


def test_check_outbound_display_name_match_ok(
    preflight: types.ModuleType, common: types.ModuleType
) -> None:
    result: dict[str, object] = {}
    issues: list[str] = []
    preflight._check_outbound_display_name(
        {
            "id": "x",
            "email": "outbound@lab5.ca",
            "display_name": common.REQUIRED_OUTBOUND_DISPLAY_NAME,
        },
        result,
        issues,
    )
    assert result["outbound_display_name_ok"] is True
    assert issues == []
