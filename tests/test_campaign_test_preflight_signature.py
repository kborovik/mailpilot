"""Campaign-test preflight enforces outbound@lab5.ca signature (§V.151 / §V.122)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / ".claude"
    / "skills"
    / "mailpilot-campaign-test"
    / "scripts"
)


def _load(name: str):
    path = _SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Sibling imports inside scripts use bare `from _common import ...`.
    sys.path.insert(0, str(_SCRIPTS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(_SCRIPTS))
    return module


@pytest.fixture(scope="module")
def preflight():
    return _load("preflight")


@pytest.fixture(scope="module")
def common():
    return _load("_common")


def test_required_outbound_signature_fields(common):
    required = common.REQUIRED_OUTBOUND_SIGNATURE
    assert required == {
        "full_name": "Konstantin Borovi",
        "title": "DevOps Engineer",
        "website": "https://lab5.ca",
        "phone": "+1-416-670-0621",
    }


def test_check_outbound_signature_missing_blocks(preflight):
    result: dict[str, object] = {}
    issues: list[str] = []
    preflight._check_outbound_signature(
        {"id": "x", "email": "outbound@lab5.ca", "signature": None},
        result,
        issues,
    )
    assert result["outbound_signature_ok"] is False
    assert any("signature missing" in i for i in issues)


def test_check_outbound_signature_mismatch_blocks(preflight, common):
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


def test_check_outbound_signature_match_ok(preflight, common):
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
